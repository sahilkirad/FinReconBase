"use client";

import clsx from "clsx";

import { StatusPill } from "@/components/ui/status-pill";
import { formatINR } from "@/lib/money";
import type { InvoiceItem } from "@/lib/queries";

type RowState = "cleared" | "review" | "processing";

function rowState(i: InvoiceItem): RowState {
  if (i.utr_number) return "cleared";
  if (i.exception_reason) return "review";
  return "processing";
}

function Row({ inv }: { inv: InvoiceItem }) {
  const state = rowState(inv);
  const amount =
    state === "cleared" ? inv.net_settled_amount_paise : inv.net_paise;
  return (
    <tr className="border-b border-line last:border-0">
      <td className="px-3 py-2 font-mono text-xs text-navy">
        {inv.invoice_number ?? "—"}
      </td>
      <td className="px-3 py-2">
        {state === "cleared" ? (
          <span className="font-mono text-xs text-slate-600">{inv.utr_number}</span>
        ) : (
          <span className="text-xs text-slate-300">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums text-navy">
        {amount != null ? formatINR(amount) : "—"}
      </td>
      <td className="px-3 py-2 text-right">
        {state === "cleared" && <StatusPill tone="success">Cleared</StatusPill>}
        {state === "review" && <StatusPill tone="danger">Manual review</StatusPill>}
        {state === "processing" && <StatusPill tone="pending">In progress</StatusPill>}
      </td>
    </tr>
  );
}

/** Proof-of-work list — which invoices cleared, against which UTR, for how much. */
export function ClearedList({
  invoices,
  loading,
}: {
  invoices: InvoiceItem[];
  loading: boolean;
}) {
  const clearedCount = invoices.filter((i) => i.utr_number).length;
  const reviewCount = invoices.filter((i) => i.exception_reason).length;

  return (
    <div className="rounded-xl border border-line bg-white shadow-card">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold text-navy">Settlement Ledger</h2>
        <p className="text-[11px] text-slate-400">
          <span className="text-success">{clearedCount} cleared</span>
          {reviewCount > 0 && (
            <>
              {" · "}
              <span className="text-amber-600">{reviewCount} in review</span>
            </>
          )}
        </p>
      </div>
      <div className={clsx("overflow-x-auto", loading && "opacity-60")}>
        {invoices.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-slate-400">
            Invoices will appear here as extraction completes.
          </p>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-slate-400">
                <th className="px-3 py-2 font-medium">Invoice</th>
                <th className="px-3 py-2 font-medium">Matched bank UTR</th>
                <th className="px-3 py-2 text-right font-medium">Amount</th>
                <th className="px-3 py-2 text-right font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((inv) => (
                <Row key={inv.document_id ?? inv.invoice_number ?? "row"} inv={inv} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
