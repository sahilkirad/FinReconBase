"use client";

import { useEffect, useState } from "react";

import { ActivityFeed } from "@/components/finops/activity-feed";
import { ClearedList } from "@/components/finops/cleared-list";
import { KpiRow, type FinOpsKpis } from "@/components/finops/kpi-row";
import { ProgressRing } from "@/components/finops/progress-ring";
import { ErrorBoundary } from "@/components/error-boundary";
import { SkeletonLines } from "@/components/ui/skeleton";
import { StatusPill, toneForStatus } from "@/components/ui/status-pill";
import { extractApiError } from "@/lib/api";
import { rememberActiveBatch } from "@/lib/active-batch";
import {
  isRunActive,
  useBatchTelemetry,
  useTelemetryEvents,
  type InvoiceItem,
} from "@/lib/queries";
import { useAuthStore } from "@/store/auth";

function deriveKpis(invoices: InvoiceItem[], batchTotal: number): FinOpsKpis {
  let clearedPaise = 0;
  let clearedCount = 0;
  let extractedCount = 0;
  let reviewPaise = 0;
  let reviewCount = 0;

  for (const inv of invoices) {
    const terminal = Boolean(inv.utr_number) || Boolean(inv.exception_reason);
    const extracted = Boolean(inv.processing_status === "VALIDATED") || terminal;
    if (extracted) extractedCount += 1;

    if (inv.utr_number && inv.net_settled_amount_paise != null) {
      clearedCount += 1;
      clearedPaise += inv.net_settled_amount_paise;
    } else if (inv.exception_reason) {
      reviewCount += 1;
      if (inv.net_paise != null) reviewPaise += inv.net_paise;
    }
  }

  return {
    clearedPaise,
    clearedCount,
    extractedCount: Math.max(extractedCount, clearedCount + reviewCount),
    batchTotal,
    reviewPaise,
    reviewCount,
  };
}

export function FinOpsView({ batchId }: { batchId: string }) {
  const profile = useAuthStore((s) => s.profile);
  const telemetry = useBatchTelemetry(batchId);
  const [running, setRunning] = useState(true);
  const events = useTelemetryEvents(batchId, running);
  const [refreshing, setRefreshing] = useState(false);

  // URL holds the batch id; sessionStorage lets the top-nav return here after
  // the user visits Ledger / Exception Desk / Command Center. Stored per
  // vendor so a pointer never leaks across tenants.
  useEffect(() => {
    if (profile?.vendor_code) rememberActiveBatch(batchId, profile.vendor_code);
  }, [batchId, profile?.vendor_code]);

  useEffect(() => {
    setRunning(isRunActive(telemetry.data?.layer2));
  }, [telemetry.data]);

  if (telemetry.isError) {
    return (
      <div className="rounded-lg border border-danger-soft bg-danger-soft p-5 text-sm text-danger">
        <p className="font-medium">Could not load batch data.</p>
        <p className="mt-1">{extractApiError(telemetry.error)}</p>
        <p className="mt-1 font-mono text-xs opacity-70">batch: {batchId}</p>
      </div>
    );
  }

  if (telemetry.isLoading || !telemetry.data) {
    return (
      <div className="space-y-4">
        <SkeletonLines rows={2} />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-24 rounded-xl bg-slate-100" />
          ))}
        </div>
      </div>
    );
  }

  const data = telemetry.data;
  const layer2 = data.layer2;
  const kpis = deriveKpis(data.invoices, data.total_invoices);
  const settledCount = kpis.clearedCount;
  const reviewCount = kpis.reviewCount;

  const documentsDone = data.status === "COMPLETED";
  const settlementDone = layer2?.status === "COMPLETED";

  async function refresh() {
    setRefreshing(true);
    await Promise.all([telemetry.refetch(), events.refetch()]);
    setRefreshing(false);
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-navy">
            Financial Operations Center
          </h1>
          <p className="mt-1 font-mono text-xs text-slate-400">
            {batchId} · {data.vendor_code}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <StatusPill tone={toneForStatus(data.status)}>
            {documentsDone ? "Documents Processed" : "Processing Documents"}
          </StatusPill>
          <StatusPill tone={toneForStatus(layer2?.status ?? "PENDING")}>
            {settlementDone ? "Settlement Sync Complete" : "Awaiting Settlement Sync"}
          </StatusPill>
          {running && <StatusPill tone="pending">● live</StatusPill>}
          {!running && settledCount === data.total_invoices && (
            <StatusPill tone="success">Batch closed</StatusPill>
          )}
          <button
            type="button"
            onClick={refresh}
            disabled={refreshing}
            className="rounded-md border border-line px-2.5 py-1 text-xs text-slate-500 transition-colors hover:border-primary/40 hover:text-primary disabled:opacity-50"
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {/* CFO KPIs */}
      <ErrorBoundary label="kpis">
        <KpiRow kpis={kpis} running={running} />
      </ErrorBoundary>

      {/* Progress + activity */}
      <div className="grid gap-4 lg:grid-cols-5">
        <div className="lg:col-span-2">
          <ErrorBoundary label="progress">
            <ProgressRing
              cleared={settledCount}
              review={reviewCount}
              extracted={kpis.extractedCount}
              batchTotal={data.total_invoices}
              running={running}
            />
          </ErrorBoundary>
        </div>
        <div className="h-72 lg:col-span-3">
          <ErrorBoundary label="activity">
            <ActivityFeed
              invoices={data.invoices}
              events={events.data?.events ?? []}
              running={running}
            />
          </ErrorBoundary>
        </div>
      </div>

      {/* Settlement ledger */}
      <ErrorBoundary label="ledger">
        <ClearedList invoices={data.invoices} loading={telemetry.isFetching} />
      </ErrorBoundary>
    </div>
  );
}
