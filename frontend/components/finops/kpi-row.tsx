"use client";

import { formatINR } from "@/lib/money";

export interface FinOpsKpis {
  /** Paise cleared through the ledger (matched + double-entry committed). */
  clearedPaise: number;
  clearedCount: number;
  /** Validated extractions so far (denominator for the auto-rate). */
  extractedCount: number;
  batchTotal: number;
  /** Paise currently sitting on the Exception Desk / manual review. */
  reviewPaise: number;
  reviewCount: number;
}

function KpiCard({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub: string;
  tone: "success" | "pending" | "neutral";
}) {
  const valueTone =
    tone === "success"
      ? "text-success"
      : tone === "pending"
        ? "text-amber-600"
        : "text-navy";
  const dot = tone === "success" ? "bg-success" : tone === "pending" ? "bg-amber-500" : "bg-slate-300";
  return (
    <div className="rounded-xl border border-line bg-white p-4 shadow-card">
      <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-400">
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
        {label}
      </div>
      <p className={`mt-2 font-mono text-2xl font-semibold tabular-nums tracking-tight ${valueTone}`}>
        {value}
      </p>
      <p className="mt-1 text-xs text-slate-400">{sub}</p>
    </div>
  );
}

/** CFO-grade header KPIs — money in, money cleared, money at risk. */
export function KpiRow({ kpis, running }: { kpis: FinOpsKpis; running: boolean }) {
  const extracted = Math.max(1, kpis.extractedCount);
  const autoRate = (kpis.clearedCount / extracted) * 100;

  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <KpiCard
        label="Total value cleared"
        value={formatINR(kpis.clearedPaise)}
        sub={`${kpis.clearedCount} invoice${kpis.clearedCount === 1 ? "" : "s"} committed to the General Ledger`}
        tone="success"
      />
      <KpiCard
        label="Auto-clearance rate"
        value={`${autoRate.toFixed(1)}%`}
        sub={
          running
            ? `${kpis.extractedCount} of ${kpis.batchTotal} invoices processed so far`
            : `${kpis.clearedCount} of ${Math.max(kpis.extractedCount, kpis.clearedCount)} cleared automatically`
        }
        tone="neutral"
      />
      <KpiCard
        label="Pending review"
        value={formatINR(kpis.reviewPaise)}
        sub={`${kpis.reviewCount} invoice${kpis.reviewCount === 1 ? "" : "s"} on the Maker/Checker queue`}
        tone="pending"
      />
    </div>
  );
}
