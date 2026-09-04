"use client";

const R = 70;
const CIRC = 2 * Math.PI * R;

function arc(circumference: number, fraction: number, offset: number): string {
  const dash = Math.max(0, Math.min(1, fraction)) * circumference;
  return `${dash} ${circumference - dash}`;
}

/**
 * Clean circular progress — invoices "Unreconciled" -> "Ledger Committed".
 * The green ring fills as invoices clear; amber marks what needs a human eye.
 */
export function ProgressRing({
  cleared,
  review,
  extracted,
  batchTotal,
  running,
}: {
  cleared: number;
  review: number;
  extracted: number;
  batchTotal: number;
  running: boolean;
}) {
  const open = Math.max(0, extracted - cleared - review);
  const pct = extracted > 0 ? (cleared / extracted) * 100 : 0;

  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-line bg-white p-5 shadow-card sm:flex-row sm:gap-6">
      <div className="relative h-44 w-44 shrink-0">
        <svg viewBox="0 0 160 160" className="h-full w-full -rotate-90">
          {/* track */}
          <circle cx="80" cy="80" r={R} fill="none" stroke="#e2e8f0" strokeWidth="12" />
          {/* manual-review share (amber) — drawn first so green over-draws it */}
          {review > 0 && (
            <circle
              cx="80"
              cy="80"
              r={R}
              fill="none"
              stroke="#d97706"
              strokeWidth="12"
              strokeDasharray={arc(CIRC, review / Math.max(1, extracted), 0)}
              className="transition-all duration-700 ease-out"
            />
          )}
          {/* cleared share (green) */}
          <circle
            cx="80"
            cy="80"
            r={R}
            fill="none"
            stroke="#059669"
            strokeWidth="12"
            strokeDasharray={arc(CIRC, cleared / Math.max(1, extracted), review > 0 ? (review / Math.max(1, extracted)) * CIRC : 0)}
            className="transition-all duration-700 ease-out"
          />
          {/* open share (slate, drawn on top as dotted cap when idle) */}
          {open > 0 && running && (
            <circle
              cx="80"
              cy="80"
              r={R}
              fill="none"
              stroke="#94a3b8"
              strokeWidth="12"
              strokeDasharray={arc(CIRC, open / Math.max(1, extracted), ((cleared + review) / Math.max(1, extracted)) * CIRC)}
              className="opacity-40"
            />
          )}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <p className="font-mono text-3xl font-semibold tabular-nums text-navy">
            {cleared}
            <span className="text-base text-slate-400">/{batchTotal}</span>
          </p>
          <p className="mt-1 text-[11px] text-slate-400">ledger-committed</p>
        </div>
      </div>

      <div className="w-full space-y-2.5">
        <p className="text-sm font-semibold text-navy">
          {running ? "Reconciling batch…" : pct >= 100 && extracted > 0 ? "Batch closed" : "Batch complete"}
        </p>
        <div className="flex items-center justify-between text-xs">
          <span className="flex items-center gap-2 text-slate-500">
            <span className="h-2.5 w-2.5 rounded-full bg-[#059669]" />
            Cleared — ledger committed
          </span>
          <span className="font-mono tabular-nums text-navy">{cleared}</span>
        </div>
        <div className="flex items-center justify-between text-xs">
          <span className="flex items-center gap-2 text-slate-500">
            <span className="h-2.5 w-2.5 rounded-full bg-[#d97706]" />
            Manual review required
          </span>
          <span className="font-mono tabular-nums text-navy">{review}</span>
        </div>
        <div className="flex items-center justify-between text-xs">
          <span className="flex items-center gap-2 text-slate-500">
            <span className="h-2.5 w-2.5 rounded-full bg-slate-300" />
            In progress / queued
          </span>
          <span className="font-mono tabular-nums text-navy">{Math.max(0, batchTotal - cleared - review)}</span>
        </div>
        <p className="border-t border-line pt-2 text-[11px] leading-relaxed text-slate-400">
          Every cleared invoice posts a paired Debit/Credit to the General Ledger — balanced to ₹0.00.
        </p>
      </div>
    </div>
  );
}
