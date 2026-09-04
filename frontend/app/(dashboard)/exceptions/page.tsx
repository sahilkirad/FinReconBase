"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useState } from "react";

import { SkeletonLines } from "@/components/ui/skeleton";
import { StatusPill, toneForStatus } from "@/components/ui/status-pill";
import { api, extractApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { formatSignedINR } from "@/lib/money";
import {
  useExceptionTickets,
  type ExceptionStatusFilter,
  type ExceptionTicket,
} from "@/lib/queries";

const TABS: { key: ExceptionStatusFilter; label: string }[] = [
  { key: null, label: "All" },
  { key: "OPEN", label: "Open" },
  { key: "IN_REVIEW", label: "In review" },
  { key: "RESOLVED", label: "Resolved" },
  { key: "CLOSED", label: "Closed" },
];

function reasonTone(reason: string): "danger" | "pending" | "neutral" {
  const r = reason.toUpperCase();
  if (r.includes("NO_MATCH") || r.includes("MISMATCH")) return "danger";
  if (r.includes("COLLISION") || r.includes("ENTITY")) return "pending";
  return "neutral";
}

export default function ExceptionsPage() {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<ExceptionStatusFilter>("OPEN");
  const [pendingId, setPendingId] = useState<string | null>(null);

  const query = useExceptionTickets(tab);

  const transition = useMutation({
    mutationFn: async ({ id, status }: { id: string; status: string }) => {
      const { data } = await api.patch<ExceptionTicket>(`/exception-tickets/${id}`, {
        status,
      });
      return data;
    },
    onMutate: ({ id }) => setPendingId(id),
    onSettled: () => setPendingId(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["exception-tickets"] });
    },
  });

  async function act(ticket: ExceptionTicket, next: "IN_REVIEW" | "RESOLVED" | "CLOSED") {
    try {
      await transition.mutateAsync({ id: ticket.ticket_id, status: next });
    } catch {
      // error surfaces in the card footer below
    }
  }

  const tickets = query.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-navy">Exception Desk</h1>
        <p className="mt-1 text-sm text-slate-500">
          Human-in-the-loop queue fed by{" "}
          <span className="font-mono text-xs">reconciliation.dlq.events</span> — every card
          carries the deterministic trap and the agent&apos;s own stop reason. No
          cherry-picked 100% success rates here.
        </p>
      </div>

      {/* Maker/checker tabs */}
      <div className="flex flex-wrap items-center gap-1 rounded-lg bg-slate-100 p-1 text-sm">
        {TABS.map((t) => (
          <button
            key={t.label}
            type="button"
            onClick={() => setTab(t.key)}
            className={clsx(
              "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              tab === t.key
                ? "bg-white text-navy shadow-sm"
                : "text-slate-500 hover:text-navy"
            )}
          >
            {t.label}
          </button>
        ))}
        <span className="ml-auto hidden pr-2 font-mono text-[11px] text-slate-400 md:inline">
          maker/checker · OPEN → IN_REVIEW → RESOLVED
        </span>
      </div>

      {query.isLoading ? (
        <div className="rounded-xl border border-line bg-white p-5 shadow-card">
          <SkeletonLines rows={5} />
        </div>
      ) : query.isError ? (
        <p className="rounded-lg border border-danger-soft bg-danger-soft p-4 text-sm text-danger">
          {extractApiError(query.error)}
        </p>
      ) : tickets.length === 0 ? (
        <div className="rounded-xl border border-line bg-white p-10 text-center shadow-card">
          <p className="text-2xl">✓</p>
          <p className="mt-2 text-sm font-medium text-navy">No {tab?.toLowerCase() ?? ""} tickets</p>
          <p className="mt-1 text-xs text-slate-400">
            {tab === "OPEN"
              ? "The deterministic-first pipeline routed nothing to human review — clean run."
              : "Try another status filter."}
          </p>
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {tickets.map((ticket) => {
            const busy = pendingId === ticket.ticket_id;
            return (
              <article
                key={ticket.ticket_id}
                className="flex flex-col rounded-xl border border-line bg-white p-4 shadow-card"
              >
                {/* Header */}
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-mono text-[10px] text-slate-400">{ticket.ticket_id}</p>
                    <p className="mt-0.5 text-[11px] text-slate-400">
                      created {formatDateTime(ticket.created_at)} · {ticket.source_topic}
                    </p>
                  </div>
                  <StatusPill tone={toneForStatus(ticket.status)}>{ticket.status}</StatusPill>
                </div>

                {/* Flags */}
                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] uppercase tracking-wider text-slate-400">
                    flagged
                  </span>
                  {ticket.flagged_invoice_ids.map((inv) => (
                    <span
                      key={inv}
                      className="rounded bg-danger-soft px-1.5 py-0.5 font-mono text-[10px] font-medium text-danger"
                    >
                      {inv}
                    </span>
                  ))}
                  {ticket.bank_utr_number && (
                    <span className="ml-auto font-mono text-[10px] text-slate-400">
                      UTR {ticket.bank_utr_number}
                    </span>
                  )}
                </div>

                {/* Deterministic trap + variance */}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <StatusPill tone={reasonTone(ticket.exception_reason)}>
                    {ticket.exception_reason}
                  </StatusPill>
                  {ticket.variance_delta_paise !== null && (
                    <span
                      className={clsx(
                        "rounded-full px-2 py-0.5 font-mono text-[11px] font-semibold tabular-nums",
                        ticket.variance_delta_paise === 0
                          ? "bg-success-soft text-success"
                          : "bg-danger-soft text-danger"
                      )}
                    >
                      variance {formatSignedINR(ticket.variance_delta_paise)}
                    </span>
                  )}
                </div>

                {/* LLM stop reason */}
                <div className="mt-3 flex-1 rounded-md bg-slate-50 px-3 py-2.5">
                  <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400">
                    why the agent stopped
                  </p>
                  <p className="mt-1 text-[13px] leading-relaxed text-slate-600">
                    “{ticket.human_readable_message}”
                  </p>
                </div>

                {/* Resolved audit footer */}
                {(ticket.status === "RESOLVED" || ticket.status === "CLOSED") && (
                  <p className="mt-3 rounded-md bg-success-soft px-3 py-1.5 text-[11px] text-success">
                    {ticket.status.toLowerCase()} {formatDateTime(ticket.resolved_at)} by{" "}
                    <span className="font-mono">{ticket.resolved_by ?? "—"}</span>
                  </p>
                )}

                {/* Actions */}
                {ticket.status === "OPEN" && (
                  <div className="mt-4 flex justify-end">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => act(ticket, "IN_REVIEW")}
                      className="h-9 rounded-md bg-primary px-4 text-xs font-medium text-white transition-colors hover:bg-primary-700 disabled:opacity-50"
                    >
                      {busy ? "Claiming…" : "Start review (claim)"}
                    </button>
                  </div>
                )}
                {ticket.status === "IN_REVIEW" && (
                  <div className="mt-4 flex justify-end gap-2">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => act(ticket, "CLOSED")}
                      className="h-9 rounded-md border border-line px-4 text-xs font-medium text-slate-500 transition-colors hover:text-danger disabled:opacity-50"
                    >
                      Close
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => act(ticket, "RESOLVED")}
                      className="h-9 rounded-md bg-success px-4 text-xs font-medium text-white transition-colors hover:opacity-90 disabled:opacity-50"
                    >
                      {busy ? "Updating…" : "Resolve"}
                    </button>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
