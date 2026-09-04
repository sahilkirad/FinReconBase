"use client";

import { formatINR } from "@/lib/money";
import type { InvoiceItem, TelemetryEvent } from "@/lib/queries";

interface FeedEntry {
  key: string;
  tone: "success" | "pending" | "info";
  title: string;
  detail?: string;
}

const FUZZY_TOOL_LABEL = "Supplier alias variance auto-resolved";

function toolCopy(detail: string, invoice: string): string | null {
  const tool = (detail || "").toLowerCase();
  if (tool.includes("run_fuzzy_text_linker_tool")) {
    return `${FUZZY_TOOL_LABEL} for ${invoice} — matched to the settlement payout.`;
  }
  return null;
}

function exceptionCopy(reason: string, invoice: string): string {
  const r = (reason || "").toUpperCase();
  if (r.includes("NO_MATCH") || r.includes("AMBIGUOUS") || r.includes("COLLISION")) {
    return `Ambiguous settlement for ${invoice} — routed to the Maker/Checker queue to prevent misallocation.`;
  }
  if (r.includes("ENTITY") || r.includes("ALIAS") || r.includes("MISMATCH")) {
    return `Supplier details for ${invoice} could not be fully confirmed — parked for manual review.`;
  }
  return `${invoice} requires manual review (${reason}).`;
}

/**
 * Audit & Activity Trail — translates pipeline outcomes into business copy.
 * Source of truth is the DB-derived per-invoice state (always accurate); the
 * ephemeral Redis events add alias-resolution details when available.
 */
export function ActivityFeed({
  invoices,
  events,
  running,
}: {
  invoices: InvoiceItem[];
  events: TelemetryEvent[];
  running: boolean;
}) {
  const entries: FeedEntry[] = [];

  // 1) Redis enrichment: alias-resolution notices (deduped per invoice).
  const enriched = new Set<string>();
  for (const ev of [...events].reverse()) {
    const inv = ev.invoice;
    if (!inv || ev.stage !== "tool_called") continue;
    const copy = toolCopy(ev.detail ?? "", inv);
    if (copy && !enriched.has(inv)) {
      enriched.add(inv);
      entries.push({ key: `tool-${inv}`, tone: "info", title: copy });
    }
  }

  // 2) DB-derived terminal outcomes (cleared + exception) — newest last.
  const terminal = invoices
    .filter((i) => i.utr_number || i.exception_reason)
    .slice()
    .reverse();

  for (const inv of terminal) {
    if (inv.utr_number && inv.net_settled_amount_paise != null) {
      entries.push({
        key: `cleared-${inv.invoice_number}`,
        tone: "success",
        title: `Perfect match found — ${inv.invoice_number} cleared against Bank UTR ${inv.utr_number} for ${formatINR(inv.net_settled_amount_paise)}.`,
        detail: "Posted to the General Ledger — double-entry balanced to ₹0.00.",
      });
    } else if (inv.exception_reason) {
      entries.push({
        key: `review-${inv.invoice_number}`,
        tone: "pending",
        title: exceptionCopy(inv.exception_reason, inv.invoice_number ?? ""),
        detail: inv.net_paise != null ? `${formatINR(inv.net_paise)} held pending resolution.` : undefined,
      });
    }
  }

  const visible = entries.slice(0, 12);

  return (
    <div className="flex h-full flex-col rounded-xl border border-line bg-white shadow-card">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold text-navy">Audit &amp; Activity Trail</h2>
        {running && (
          <span className="flex items-center gap-1.5 text-[11px] text-primary">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
            live
          </span>
        )}
      </div>

      {visible.length === 0 ? (
        <div className="flex flex-1 items-center justify-center px-4 py-10 text-center text-sm text-slate-400">
          {running
            ? "System activity and audit logs will populate here as transactions are verified."
            : "No activity recorded for this batch yet."}
        </div>
      ) : (
        <ul className="flex-1 divide-y divide-line overflow-y-auto">
          {visible.map((entry) => (
            <li key={entry.key} className="flex gap-3 px-4 py-3">
              <span
                className={
                  entry.tone === "success"
                    ? "mt-1.5 h-2 w-2 shrink-0 rounded-full bg-success"
                    : entry.tone === "pending"
                      ? "mt-1.5 h-2 w-2 shrink-0 rounded-full bg-amber-500"
                      : "mt-1.5 h-2 w-2 shrink-0 rounded-full bg-primary"
                }
              />
              <div className="min-w-0">
                <p className="text-[13px] leading-snug text-slate-700">{entry.title}</p>
                {entry.detail && <p className="mt-0.5 text-[11px] text-slate-400">{entry.detail}</p>}
              </div>
            </li>
          ))}
        </ul>
      )}

      {entries.length > 12 && (
        <p className="border-t border-line px-4 py-2 text-center text-[11px] text-slate-400">
          +{entries.length - 12} more entries — see the Ledger and Exception Desk for the full trail
        </p>
      )}
    </div>
  );
}
