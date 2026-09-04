"use client";

import clsx from "clsx";

type Tone = "success" | "pending" | "danger" | "neutral";

const tones: Record<Tone, string> = {
  success: "bg-success-soft text-success",
  pending: "bg-pending-soft text-pending",
  danger: "bg-danger-soft text-danger",
  neutral: "bg-slate-100 text-slate-600",
};

/** Small semantic pill with a low-opacity fill (Blade status pattern). */
export function StatusPill({
  tone = "neutral",
  children,
  className,
}: {
  tone?: Tone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium tracking-wide",
        tones[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

/** Map a reconciliation-ish status to a tone. */
export function toneForStatus(status?: string | null): Tone {
  const s = (status ?? "").toUpperCase();
  if (s.includes("COMPLETED") || s.includes("LEDGER_COMMITTED") || s === "RESOLVED") {
    return "success";
  }
  if (s.includes("RUNNING") || s === "IN_REVIEW" || s === "SEALED" || s === "PENDING") {
    return "pending";
  }
  if (
    s.includes("FAILED") ||
    s.includes("EXCEPTION") ||
    s === "EXCEPTION_ROUTED" ||
    s === "OPEN" ||
    s === "NO_MATCH"
  ) {
    return "danger";
  }
  return "neutral";
}
