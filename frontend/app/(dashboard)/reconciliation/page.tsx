"use client";

import clsx from "clsx";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Dropzone } from "@/components/reconciliation/dropzone";
import { Button } from "@/components/ui/button";
import { StatusPill } from "@/components/ui/status-pill";
import { rememberActiveBatch } from "@/lib/active-batch";
import { api, extractApiError } from "@/lib/api";
import { useAutoGenerateFeeds } from "@/lib/queries";
import { useAuthStore } from "@/store/auth";

interface FeedResult {
  accepted: number;
  duplicates: number;
  total: number;
  message: string;
}

interface BatchUploadResult {
  batch_id: string;
  vendor_code: string;
  filename: string;
  total_invoices: number;
  status: string;
  message: string;
}

type FeedKey = "razorpay" | "bank";

interface FeedState {
  file: File | null;
  busy: boolean;
  result: FeedResult | null;
  error: string | null;
}

function emptyFeed(): FeedState {
  return { file: null, busy: false, result: null, error: null };
}

const MANUAL_STEP_LABELS = [
  "Push settlement feeds",
  "Upload invoice batch",
  "Run reconciliation",
];

const AUTO_STEP_LABELS = [
  "Settlement feeds (auto)",
  "Upload invoice batch",
  "Run reconciliation",
];

const ANOMALY_OPTIONS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

export default function ReconciliationPage() {
  const router = useRouter();
  const profile = useAuthStore((s) => s.profile);
  const autoGen = useAutoGenerateFeeds();

  const [autoFeeds, setAutoFeeds] = useState(true); // demo mode default
  const [anomalies, setAnomalies] = useState(4); // last N unmatched -> Exception Desk
  const [feeds, setFeeds] = useState<Record<FeedKey, FeedState>>({
    razorpay: emptyFeed(),
    bank: emptyFeed(),
  });
  const [pdf, setPdf] = useState<File | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [pdfError, setPdfError] = useState<string | null>(null);

  const feedsMaterialized =
    feeds.razorpay.result !== null &&
    feeds.bank.result !== null &&
    feeds.razorpay.result.accepted + feeds.razorpay.result.duplicates > 0 &&
    feeds.bank.result.accepted + feeds.bank.result.duplicates > 0;

  // Auto mode satisfies the feeds stage itself (endpoint pushes them the
  // moment Layer 1 extraction completes — before the L2 seal).
  const feedsReady = autoFeeds || feedsMaterialized;

  function setFeedFile(key: FeedKey, file: File | null) {
    setFeeds((prev) => ({
      ...prev,
      [key]: { ...emptyFeed(), file },
    }));
  }

  async function pushFeed(key: FeedKey) {
    const feed = feeds[key];
    if (!feed.file || feed.busy) return;

    setFeeds((prev) => ({
      ...prev,
      [key]: { ...prev[key], busy: true, error: null, result: null },
    }));

    try {
      const text = await feed.file.text();
      const records = JSON.parse(text);
      if (!Array.isArray(records)) {
        throw new Error("File must contain a JSON array of records.");
      }
      const endpoint =
        key === "razorpay" ? "/webhooks/razorpay/batch" : "/ingestion/bank";
      const { data } = await api.post<FeedResult>(endpoint, records);
      setFeeds((prev) => ({ ...prev, [key]: { ...prev[key], busy: false, result: data } }));
    } catch (err) {
      const message =
        err instanceof SyntaxError
          ? "Invalid JSON — upload the generated feed file as-is."
          : extractApiError(err);
      setFeeds((prev) => ({
        ...prev,
        [key]: { ...prev[key], busy: false, error: message },
      }));
    }
  }

  function canRun(): boolean {
    return feedsReady && pdf !== null && !running;
  }

  async function runReconciliation() {
    if (!canRun() || !pdf) return;
    setRunning(true);
    setRunError(null);
    try {
      const form = new FormData();
      form.append("file", pdf);
      const { data } = await api.post<BatchUploadResult>("/invoices/batch", form);
      if (autoFeeds) {
        // Fire at 202 — the endpoint waits server-side until extraction
        // completes, then ingests Streams 2 & 3 just before the L2 seal.
        autoGen.mutate({ batch_id: data.batch_id, anomalies });
      }
      // batch_id becomes the URL — reload-safe state for the Live page (M6) —
      // and is remembered (per vendor) so the top nav can always return.
      rememberActiveBatch(data.batch_id, data.vendor_code);
      router.push(`/reconciliation/${data.batch_id}`);
    } catch (err) {
      setRunError(extractApiError(err));
      setRunning(false);
    }
  }

  const stepLabels = autoFeeds ? AUTO_STEP_LABELS : MANUAL_STEP_LABELS;
  const stepStates = [
    feedsReady ? "done" : "current",
    pdf ? "done" : feedsReady ? "current" : "locked",
    feedsReady && pdf ? "current" : "locked",
  ];

  return (
    <div className="mx-auto max-w-4xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-navy">Command Center</h1>
          <p className="mt-1 text-sm text-slate-500">
            Central ingestion hub. Upload vendor invoices to initiate automated
            extraction, multi-way matching, and ledger settlement.
          </p>
        </div>
        {profile && (
          <span className="hidden font-mono text-xs text-slate-400 sm:block">
            {profile.vendor_code}
          </span>
        )}
      </div>

      {/* Demo-mode toggle */}
      <div className="mt-6 rounded-xl border border-primary/25 bg-primary-50 p-4">
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={autoFeeds}
            onChange={(e) => setAutoFeeds(e.target.checked)}
            className="mt-0.5 h-4 w-4 accent-[#0D94FB]"
          />
          <span className="text-sm">
            <span className="font-semibold text-navy">
              Simulate Live Payment Gateway &amp; Bank Feeds
            </span>
            <span className="mt-0.5 block text-xs leading-relaxed text-slate-500">
              Automatically injects matching Razorpay settlements and Bank
              records to simulate a real-time production environment. Turn off
              to manually upload historical feed files.
            </span>
          </span>
        </label>
        {autoFeeds && (
          <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-line pt-3">
            <label
              htmlFor="anomalies"
              className="text-xs font-medium text-slate-600"
            >
              Leave unmatched for the Exception Desk
            </label>
            <select
              id="anomalies"
              value={anomalies}
              onChange={(e) => setAnomalies(Number(e.target.value))}
              className="rounded-md border border-line bg-white px-2 py-1 font-mono text-xs text-navy focus:border-primary focus:outline-none"
            >
              {ANOMALY_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  {n} invoice{n === 1 ? "" : "s"}
                </option>
              ))}
            </select>
            <span className="text-[11px] text-slate-400">
              {anomalies === 0
                ? "Clean run — every invoice is matched and cleared automatically."
                : `The last ${anomalies} invoices will never match → routed to the Exception Desk for review.`}
            </span>
          </div>
        )}
      </div>

      {/* Step rail */}
      <ol className="mt-6 grid grid-cols-3 gap-2">
        {stepLabels.map((label, i) => {
          const state = stepStates[i];
          return (
            <li
              key={label}
              className={clsx(
                "flex items-center gap-2 rounded-md border px-3 py-2 text-xs font-medium",
                state === "done" && "border-success-soft bg-success-soft text-success",
                state === "current" && "border-primary/40 bg-primary-50 text-navy",
                state === "locked" && "border-line bg-slate-50 text-slate-400"
              )}
            >
              <span
                className={clsx(
                  "flex h-5 w-5 items-center justify-center rounded-full text-[10px]",
                  state === "done" && "bg-success text-white",
                  state === "current" && "bg-primary text-white",
                  state === "locked" && "bg-slate-200 text-slate-400"
                )}
              >
                {state === "done" ? "✓" : i + 1}
              </span>
              <span className="hidden sm:inline">{label}</span>
            </li>
          );
        })}
      </ol>

      {/* Step 1 — feeds */}
      <section className="mt-6 rounded-xl border border-line bg-white p-5 shadow-card">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-navy">
            Step 1 · {autoFeeds ? "Settlement feeds — automatic" : "Push the disconnected streams (Streams 2 & 3)"}
          </h2>
          {feedsReady && (
            <StatusPill tone="success">
              {autoFeeds ? "auto mode" : "anchors ready"}
            </StatusPill>
          )}
        </div>

        {autoFeeds ? (
          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            The system will securely sync with configured payment gateways and
            banking APIs to establish the settlement baseline.
          </p>
        ) : (
          <>
            <p className="mt-1 text-xs text-slate-400">
              JSON arrays exactly as generated by the backend scripts (e.g.
              razorpay_webhooks.json / bank_transactions.json). Vendor code is
              taken from your session — never from the file.
            </p>

            <div className="mt-4 grid gap-4 md:grid-cols-2">
              <div>
                <Dropzone
                  accept=".json,application/json"
                  label="Razorpay settlements"
                  hint="Array of payout/settlement objects · idempotent replay"
                  file={feeds.razorpay.file}
                  busy={feeds.razorpay.busy}
                  onFile={(f) => setFeedFile("razorpay", f)}
                />
                {feeds.razorpay.result && (
                  <p className="mt-2 text-xs text-success">
                    ✓ {feeds.razorpay.result.message}
                  </p>
                )}
                {feeds.razorpay.error && (
                  <p className="mt-2 text-xs text-danger">{feeds.razorpay.error}</p>
                )}
                <Button
                  className="mt-3 w-full"
                  variant="secondary"
                  size="sm"
                  disabled={!feeds.razorpay.file || feeds.razorpay.busy}
                  onClick={() => pushFeed("razorpay")}
                >
                  {feeds.razorpay.busy ? "Pushing…" : "Push Razorpay feed"}
                </Button>
              </div>

              <div>
                <Dropzone
                  accept=".json,application/json"
                  label="Bank statement feed"
                  hint="Array of bank transaction objects · idempotent replay"
                  file={feeds.bank.file}
                  busy={feeds.bank.busy}
                  onFile={(f) => setFeedFile("bank", f)}
                />
                {feeds.bank.result && (
                  <p className="mt-2 text-xs text-success">✓ {feeds.bank.result.message}</p>
                )}
                {feeds.bank.error && (
                  <p className="mt-2 text-xs text-danger">{feeds.bank.error}</p>
                )}
                <Button
                  className="mt-3 w-full"
                  variant="secondary"
                  size="sm"
                  disabled={!feeds.bank.file || feeds.bank.busy}
                  onClick={() => pushFeed("bank")}
                >
                  {feeds.bank.busy ? "Pushing…" : "Push bank feed"}
                </Button>
              </div>
            </div>
          </>
        )}
      </section>

      {/* Step 2 — invoice PDF */}
      <section
        className={clsx(
          "mt-4 rounded-xl border bg-white p-5 shadow-card transition-opacity",
          feedsReady ? "border-line" : "border-line opacity-50"
        )}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-navy">Step 2 · Invoice batch</h2>
          {!feedsReady && (
            <span className="text-[11px] text-pending">locked — push feeds first</span>
          )}
          {feedsReady && !pdf && autoFeeds && (
            <StatusPill tone="pending">feeds will auto-generate</StatusPill>
          )}
          {pdf && <StatusPill tone="success">queued</StatusPill>}
        </div>
        <div className="mt-3">
          <Dropzone
            accept=".pdf,application/pdf"
            label="invoice PDF"
            hint={
              autoFeeds
                ? "Multi-page PDF or image. Each invoice is extracted, validated, and auto-matched against the settlement feeds."
                : "Multi-page PDF or image. CSV files are not supported."
            }
            file={pdf}
            busy={!feedsReady}
            onFile={setPdf}
          />
        </div>
        {pdfError && <p className="mt-2 text-xs text-danger">{pdfError}</p>}
        {pdf && (
          <p className="mt-2 font-mono text-[11px] text-slate-400">
            {pdf.name} · {(pdf.size / 1024).toFixed(1)} KB
          </p>
        )}
      </section>

      {/* Step 3 — run */}
      <section
        className={clsx(
          "mt-4 rounded-xl border p-5 shadow-card transition-opacity",
          canRun() ? "border-primary/30 bg-white" : "border-line bg-white opacity-50"
        )}
      >
        <h2 className="text-sm font-semibold text-navy">Step 3 · Run reconciliation</h2>
        <p className="mt-1 text-xs text-slate-400">
          Uploads are accepted instantly and processed in the background — you
          will be routed to a live view of the batch as it runs.
          {autoFeeds &&
            " Settlement feeds are injected automatically, then matching and posting begin."}
        </p>
        {runError && <p className="mt-2 text-xs text-danger">{runError}</p>}
        {autoGen.isError && (
          <p className="mt-2 text-xs text-amber-600">
            ⚠ Auto-feed generation failed ({extractApiError(autoGen.error)}) —
            the batch still ran; reconcile may end with exceptions.
          </p>
        )}
        <Button
          className="mt-4 w-full"
          disabled={!canRun()}
          onClick={runReconciliation}
        >
          {running ? "Queuing batch…" : "Run Reconciliation →"}
        </Button>
      </section>
    </div>
  );
}
