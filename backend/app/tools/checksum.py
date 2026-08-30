"""
Fail-Fast Mathematical Checksum Validation

Validates extracted invoice amounts BEFORE data touches downstream
financial engines. Uses Decimal exclusively - no floats anywhere.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def _to_decimal(value: Any) -> Decimal:
    """Safely convert any value to Decimal. Never uses float."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        return Decimal(str(value))
    if isinstance(value, float):
        raise ValueError(f"Float detected in money field: {value}. Use Decimal strings.")
    raise ValueError(f"Cannot convert {type(value)} to Decimal")


def validate_line_items(line_items: list[dict]) -> list[str]:
    """Validate each line item's internal math.

    Checks:
    - unit_price x quantity == taxable_value
    - taxable_value + igst + cgst + sgst == total
    """
    errors = []
    for item in line_items:
        ln = item.get("line_number", "?")
        try:
            qty = _to_decimal(item.get("quantity", 0))
            unit_price = _to_decimal(item.get("unit_price_paise", 0))
            taxable = _to_decimal(item.get("taxable_value_paise", 0))
            igst = _to_decimal(item.get("igst_paise", 0))
            cgst = _to_decimal(item.get("cgst_paise", 0))
            sgst = _to_decimal(item.get("sgst_paise", 0))
            total = _to_decimal(item.get("total_paise", 0))

            # Check taxable = unit_price x quantity
            expected_taxable = (unit_price * qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if expected_taxable != taxable:
                errors.append(
                    f"Line {ln}: taxable_value_paise {taxable} != "
                    f"unit_price({unit_price}) x qty({qty}) = {expected_taxable}"
                )

            # Check total = taxable + GST components
            expected_total = taxable + igst + cgst + sgst
            if expected_total != total:
                errors.append(
                    f"Line {ln}: total_paise {total} != "
                    f"taxable({taxable}) + igst({igst}) + cgst({cgst}) + sgst({sgst}) = {expected_total}"
                )
        except (ValueError, TypeError) as e:
            errors.append(f"Line {ln}: Invalid decimal value - {e}")

    return errors


def validate_financial_summary(summary: dict, line_items: list[dict]) -> list[str]:
    """Validate the financial summary against line items and internal consistency.

    Checks:
    - sum of line item taxable values == subtotal
    - sum of line item GST == total_tax
    - grand_total = subtotal + tax - discount + other_charges + rounding
    """
    errors = []
    try:
        subtotal = _to_decimal(summary.get("subtotal_paise", 0))
        total_tax = _to_decimal(summary.get("total_tax_paise", 0))
        total_igst = _to_decimal(summary.get("total_igst_paise", 0))
        total_cgst = _to_decimal(summary.get("total_cgst_paise", 0))
        total_sgst = _to_decimal(summary.get("total_sgst_paise", 0))
        tds = _to_decimal(summary.get("tds_deduction_paise", 0))
        other = _to_decimal(summary.get("other_charges_paise", 0))
        discount = _to_decimal(summary.get("discount_paise", 0))
        rounding = _to_decimal(summary.get("rounding_adjustment_paise", 0))
        grand_total = _to_decimal(summary.get("grand_total_paise", 0))

        # Sum line items
        line_subtotal = sum(_to_decimal(li.get("taxable_value_paise", 0)) for li in line_items)
        line_total_tax = sum(
            _to_decimal(li.get("igst_paise", 0))
            + _to_decimal(li.get("cgst_paise", 0))
            + _to_decimal(li.get("sgst_paise", 0))
            for li in line_items
        )

        # Validate subtotal
        if line_subtotal != subtotal:
            errors.append(
                f"subtotal_paise {subtotal} != sum of line item taxable values {line_subtotal}"
            )

        # Validate total tax
        if line_total_tax != total_tax:
            errors.append(
                f"total_tax_paise {total_tax} != sum of line item GST {line_total_tax}"
            )

        # Validate GST split
        expected_gst_split = total_igst + total_cgst + total_sgst
        if expected_gst_split != total_tax:
            errors.append(
                f"GST split {expected_gst_split} (igst+cgst+sgst) != total_tax_paise {total_tax}"
            )

        # Validate grand total: subtotal + tax - discount + other + rounding
        expected_grand = subtotal + total_tax - discount + other + rounding
        if expected_grand != grand_total:
            errors.append(
                f"grand_total_paise {grand_total} != "
                f"subtotal({subtotal}) + tax({total_tax}) - discount({discount}) "
                f"+ other({other}) + rounding({rounding}) = {expected_grand}"
            )

        # TDS sanity check
        if tds < 0:
            errors.append(f"tds_deduction_paise is negative: {tds}")
        if tds > subtotal and subtotal > 0:
            errors.append(f"tds_deduction_paise {tds} exceeds subtotal {subtotal}")

    except (ValueError, TypeError) as e:
        errors.append(f"Financial summary validation error: {e}")

    return errors


def run_checksum(payload: dict) -> list[str]:
    """Run full mathematical checksum on the extracted invoice payload.

    Returns list of error strings. Empty list = checksum passed.
    """
    all_errors = []

    line_items = payload.get("line_items", [])
    if not line_items:
        all_errors.append("No line items found in invoice")
        return all_errors

    all_errors.extend(validate_line_items(line_items))
    all_errors.extend(validate_financial_summary(payload.get("financial_summary", {}), line_items))

    return all_errors
