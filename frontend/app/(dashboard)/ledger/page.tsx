"use client";

import clsx from "clsx";
import { Fragment, useMemo, useState } from "react";

import { SkeletonLines } from "@/components/ui/skeleton";
import { extractApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { formatINR } from "@/lib/money";
import { useLedgerEntries, type LedgerBatchView } from "@/lib/queries";

function LockIcon() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      aria-hidden
    >
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

export default function LedgerPage() {
  const [utrFilter, setUtrFilter] = useState("");
  const [submittedUtr, setSubmittedUtr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const query = useLedgerEntries(submittedUtr);

  const totals = useMemo(() => {
    const items = query.data?.items ?? [];
    const debit = items.reduce(
      (sum, b) => sum + b.entries.filter((e) => e.entry_type === "DEBIT").reduce((s, e) => s + e.amount_paise, 0),
      0
    );
    const credit = items.reduce(
      (sum, b) => sum + b.entries.filter((e) => e.entry_type === "CREDIT").reduce((s, e) => s + e.amount_paise, 0),
      0
    );
    const imbalance = items.reduce((sum, b) => sum + b.imbalance_paise, 0);
    return { batches: items.length, debit, credit, imbalance };
  }, [query.data]);

  function toggle(batchId: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(batchId)) {
        next.delete(batchId);
      } else {
        next.add(batchId);
      }
      return next;
    });
  }

  function submitFilter(e: React.FormEvent) {
    e.preventDefault();
    setSubmittedUtr(utrFilter.trim().toUpperCase() || null);
  }

  const debitAccount = (row: LedgerBatchView) =>
    row.entries.find((e) => e.entry_type === "DEBIT")?.account_name ?? "—";
  const creditAccount = (row: LedgerBatchView) =>
    row.entries.find((e) => e.entry_type === "CREDIT")?.account_name ?? "—";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-navy">Immutable Ledger</h1>
          <p className="mt-1 text-sm text-slate-500">
            Double-entry audit trail from the Layer 5 Ledger Writer — read-only,
            append-only rows enforced by database WORM triggers.
          </p>
        </div>
        <form onSubmit={submitFilter} className="flex gap-2">
          <input
            value={utrFilter}
            onChange={(e) => setUtrFilter(e.target.value)}
            placeholder="Filter by UTR"
            className="h-9 w-48 rounded-md border border-line bg-white px-3 font-mono text-xs placeholder:text-slate-400 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/25"
          />
          <button
            type="submit"
            className="h-9 rounded-md bg-navy px-3 text-xs font-medium text-white transition-colors hover:bg-navy-800"
          >
            Filter
          </button>
          {(submittedUtr || utrFilter) && (
            <button
              type="button"
              onClick={() => {
                setUtrFilter("");
                setSubmittedUtr(null);
              }}
              className="h-9 rounded-md border border-line px-3 text-xs text-slate-500 hover:text-danger"
            >
              Clear
            </button>
          )}
        </form>
      </div>

      {/* Balance proof strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-xl border border-line bg-white p-4 shadow-card">
          <p className="text-[11px] uppercase tracking-wider text-slate-400">Ledger batches</p>
          <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-navy">
            {totals.batches}
          </p>
        </div>
        <div className="rounded-xl border border-line bg-white p-4 shadow-card">
          <p className="text-[11px] uppercase tracking-wider text-slate-400">Total debited (AP)</p>
          <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-navy">
            {formatINR(totals.debit)}
          </p>
        </div>
        <div className="rounded-xl border border-line bg-white p-4 shadow-card">
          <p className="text-[11px] uppercase tracking-wider text-slate-400">Total credited (Bank)</p>
          <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-navy">
            {formatINR(totals.credit)}
          </p>
        </div>
        <div className="rounded-xl border border-success-soft bg-white p-4 shadow-card">
          <p className="text-[11px] uppercase tracking-wider text-success">Balance proof</p>
          <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-success">
            {formatINR(totals.imbalance)}
          </p>
          <p className="text-[10px] text-slate-400">DR − CR across every batch</p>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-hidden rounded-xl border border-line bg-white shadow-card">
        {query.isLoading ? (
          <div className="p-5">
            <SkeletonLines rows={6} />
          </div>
        ) : query.isError ? (
          <p className="p-5 text-sm text-danger">{extractApiError(query.error)}</p>
        ) : (query.data?.items.length ?? 0) === 0 ? (
          <p className="p-8 text-center text-sm text-slate-400">
            {submittedUtr
              ? `No ledger batches match UTR ${submittedUtr}.`
              : "No ledger batches yet — reconciled invoices will appear here."}
          </p>
        ) : (
          <table className="w-full min-w-[960px] text-left text-sm">
            <thead>
              <tr className="border-b border-line text-[11px] uppercase tracking-wider text-slate-400">
                <th className="px-4 py-3" />
                <th className="px-4 py-3">Settled at</th>
                <th className="px-4 py-3">UTR</th>
                <th className="px-4 py-3">Cleared invoices</th>
                <th className="px-4 py-3 text-right">Amount</th>
                <th className="px-4 py-3">Balance</th>
                <th className="px-4 py-3 text-right">WORM</th>
              </tr>
            </thead>
            <tbody>
              {(query.data?.items ?? []).map((row) => {
                const open = expanded.has(row.batch_id);
                return (
                  <Fragment key={row.batch_id}>
                    <tr
                      className="cursor-pointer border-b border-line last:border-0 hover:bg-slate-50/60"
                      onClick={() => toggle(row.batch_id)}
                    >
                      <td className="px-4 py-3">
                        <span
                          className={clsx(
                            "inline-block text-slate-300 transition-transform",
                            open && "rotate-90"
                          )}
                        >
                          ▶
                        </span>
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-500">
                        {formatDateTime(row.created_at)}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs font-medium text-navy">
                        {row.utr_number}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap gap-1">
                          {row.matched_invoice_ids.map((inv) => (
                            <span
                              key={inv}
                              className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600"
                            >
                              {inv}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-sm font-medium tabular-nums text-navy">
                        {formatINR(row.total_reconciled_amount_paise)}
                      </td>
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-1 text-xs text-success">
                          {row.imbalance_paise === 0 ? "0.00 ✓" : formatINR(row.imbalance_paise)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                          <LockIcon /> WORM
                        </span>
                      </td>
                    </tr>
                    {open && (
                      <tr key={`${row.batch_id}-proof`} className="border-b border-line bg-slate-50/50">
                        <td colSpan={7} className="px-6 py-4">
                          <div className="grid gap-4 md:grid-cols-2">
                            {row.entries.map((entry, idx) => (
                              <div
                                key={`${entry.entry_type}-${entry.account_name}-${idx}`}
                                className="rounded-lg border border-line bg-white p-3"
                              >
                                <div className="flex items-center justify-between">
                                  <p className="font-mono text-[10px] uppercase tracking-widest text-slate-400">
                                    {entry.entry_type} · {entry.account_type}
                                  </p>
                                  <p className="font-mono text-sm font-semibold tabular-nums text-navy">
                                    {formatINR(entry.amount_paise)}
                                  </p>
                                </div>
                                <p className="mt-1 text-xs font-medium text-slate-600">{entry.account_name}</p>
                                <p className="mt-0.5 font-mono text-[10px] text-slate-400">
                                  cleared: {entry.cleared_invoice_ids.join(", ")}
                                </p>
                              </div>
                            ))}
                          </div>
                          <p className="mt-3 rounded-md bg-success-soft px-3 py-2 text-[11px] text-success">
                            Subset-sum proof: UTR {row.utr_number} of {formatINR(row.total_reconciled_amount_paise)} settled{" "}
                            {row.matched_invoice_ids.join(", ")} — Debit ({debitAccount(row)}) mirrors Credit ({creditAccount(row)}) to the paise.
                          </p>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
