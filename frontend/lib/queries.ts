"use client";

import { useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

// =============================================================================
// Demo auto-feed generator (POST /api/v1/demo/auto-generate-feeds)
// =============================================================================

export interface AutoGenerateFeedsPayload {
  batch_id: string;
  anomalies: number;
}

export interface AutoGenerateFeedsResult {
  batch_id: string;
  status: "WAITING" | "PUSHED";
  message: string;
  invoices_generated: number | null;
  anomalies: number | null;
  razorpay_accepted: number | null;
  razorpay_duplicates: number | null;
  bank_accepted: number | null;
  bank_duplicates: number | null;
}

/**
 * Fire-and-forget feed materialization. The backend replies 202 WAITING
 * immediately and pushes the razorpay + bank rows itself the moment Layer 1
 * extraction completes — before the Layer 2 seal — so the caller never needs
 * to poll or retry.
 */
export function useAutoGenerateFeeds() {
  return useMutation<AutoGenerateFeedsResult, unknown, AutoGenerateFeedsPayload>({
    mutationFn: async (payload) => {
      const { data } = await api.post<AutoGenerateFeedsResult>(
        "/demo/auto-generate-feeds",
        payload
      );
      return data;
    },
  });
}

// =============================================================================
// Types (mirror app/schemas/dashboard.py)
// =============================================================================

export interface Layer2Run {
  status: string;
  run_type: string;
  total_extracted: number;
  matched_count: number;
  exception_count: number;
  shortfall: number;
  last_error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface InvoiceItem {
  row_number: number | null;
  invoice_number: string | null;
  document_id: string | null;
  l1_status: string | null;
  error_message: string | null;
  processing_status: string | null;
  utr_number: string | null;
  razorpay_payout_id: string | null;
  net_settled_amount_paise: number | null;
  /** Extracted net (grand_total - tds) — present once Layer 1 validated. */
  net_paise: number | null;
  reconciled_at: string | null;
  exception_reason: string | null;
  path: string | null;
  llm_invoked: boolean | null;
  tool_calls: string[] | null;
}

// =============================================================================
// Active-batch bookkeeping lives in lib/active-batch.ts (layout-safe, no hooks)
// =============================================================================

/** True while the pipeline is still making progress (keeps polling alive). */
export function isRunActive(layer2: Layer2Run | null | undefined): boolean {
  if (!layer2) return true; // L2 hasn't claimed the batch yet
  return layer2.status === "SEALED" || layer2.status === "RUNNING";
}

export interface TelemetryFunnel {
  total: number;
  settled: number;
  exceptions: number;
  open: number;
  fast_path: number | null;
  agent_routed: number | null;
}

export interface BatchTelemetry {
  batch_id: string;
  vendor_code: string;
  source_type: string | null;
  filename: string | null;
  status: string;
  total_invoices: number;
  processed_count: number;
  failed_count: number;
  created_at: string;
  completed_at: string | null;
  layer2: Layer2Run | null;
  funnel: TelemetryFunnel;
  invoices: InvoiceItem[];
}

export interface TelemetryEvent {
  ts?: string;
  invoice?: string;
  batch_id?: string;
  stage: string;
  detail?: string;
  terminal_status?: string;
  utr?: string;
  matched?: number;
  exceptions?: number;
}

export interface TelemetryEvents {
  batch_id: string;
  total: number;
  events: TelemetryEvent[];
}

export function useBatchTelemetry(batchId: string) {
  return useQuery<BatchTelemetry>({
    queryKey: ["batch-telemetry", batchId],
    queryFn: async () => {
      const { data } = await api.get<BatchTelemetry>(
        `/batches/${batchId}/telemetry`
      );
      return data;
    },
    // Poll every 2.5s while active; stop automatically once terminal.
    refetchInterval: (query) =>
      isRunActive(query.state.data?.layer2) ? 2500 : false,
    staleTime: 1000,
  });
}

// =============================================================================
// Ledger view (GET /api/v1/ledger/entries)
// =============================================================================

export interface LedgerEntryLine {
  entry_type: string; // DEBIT | CREDIT
  account_type: string;
  account_name: string;
  amount_paise: number;
  cleared_invoice_ids: string[];
  created_at: string;
}

export interface LedgerBatchView {
  batch_id: string;
  idempotency_event_id: string;
  vendor_code: string;
  utr_number: string;
  razorpay_payout_id: string | null;
  total_reconciled_amount_paise: number;
  matched_invoice_ids: string[];
  created_at: string;
  entries: LedgerEntryLine[];
  imbalance_paise: number;
}

export interface LedgerEntries {
  vendor_code: string;
  total: number;
  items: LedgerBatchView[];
}

export function useLedgerEntries(utr: string | null) {
  return useQuery<LedgerEntries>({
    queryKey: ["ledger-entries", utr ?? null],
    queryFn: async () => {
      const { data } = await api.get<LedgerEntries>("/ledger/entries", {
        params: utr ? { utr_number: utr } : undefined,
      });
      return data;
    },
  });
}

// =============================================================================
// Exception Desk (GET /api/v1/exception-tickets)
// =============================================================================

export interface ExceptionTicket {
  ticket_id: string;
  vendor_code: string;
  source_topic: string;
  source_event_id: string | null;
  bank_utr_number: string | null;
  flagged_invoice_ids: string[];
  exception_reason: string;
  variance_delta_paise: number | null;
  human_readable_message: string;
  flagged_payload: Record<string, unknown>;
  status: "OPEN" | "IN_REVIEW" | "RESOLVED" | "CLOSED";
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface ExceptionTicketList {
  vendor_code: string;
  total: number;
  items: ExceptionTicket[];
}

export type ExceptionStatusFilter =
  | "OPEN"
  | "IN_REVIEW"
  | "RESOLVED"
  | "CLOSED"
  | null;

export function useExceptionTickets(status: ExceptionStatusFilter) {
  return useQuery<ExceptionTicketList>({
    queryKey: ["exception-tickets", status ?? "ALL"],
    queryFn: async () => {
      const { data } = await api.get<ExceptionTicketList>("/exception-tickets", {
        params: status ? { status } : undefined,
      });
      return data;
    },
    // New DLQs from a live run show up while the desk is open.
    refetchInterval: (query) =>
      query.state.data?.items.some((t) => t.status === "OPEN") ? 8000 : false,
  });
}

export function useTelemetryEvents(batchId: string, running: boolean) {
  return useQuery<TelemetryEvents>({
    queryKey: ["batch-telemetry-events", batchId],
    queryFn: async () => {
      const { data } = await api.get<TelemetryEvents>(
        `/batches/${batchId}/telemetry/events`
      );
      return data;
    },
    enabled: true,
    refetchInterval: running ? 2500 : false,
    staleTime: 1000,
  });
}
