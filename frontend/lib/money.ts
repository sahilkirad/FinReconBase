/**
 * Monetary display helpers.
 *
 * Backend amounts are integer paise (BIGINT) — never floats. The frontend
 * divides by 100 ONLY for presentation, using Indian (en-IN) grouping, e.g.
 * ₹ 10,48,94,114.00
 */

const inrFormatter = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Whole-rupee integer part of a paise value (safe integer math). */
export function paiseWhole(paise: number): number {
  return Math.floor(paise / 100);
}

/** Remainder paise (0-99), zero-padded to two digits. */
export function paiseFraction(paise: number): string {
  return String(paise % 100).padStart(2, "0");
}

/** "10,48,94,114.00" (no currency symbol). */
export function groupRupees(paise: number): string {
  const value = paiseWhole(paise) + (paise % 100) / 100;
  return inrFormatter.format(value);
}

/** "₹ 10,48,94,114.00" — default for amounts on dashboard pages. */
export function formatINR(paise: number): string {
  return `₹ ${groupRupees(paise)}`;
}

/** Signed variant for variance/audit deltas: "-₹ 2,000.00" / "+₹ 0.00". */
export function formatSignedINR(paise: number): string {
  const sign = paise < 0 ? "-" : "+";
  return `${sign}${formatINR(Math.abs(paise))}`;
}
