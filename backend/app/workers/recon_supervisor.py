"""Recon Supervisor — Layer 2 Kafka + Boundary Poller + Execution Pool

Implements the Asynchronous Handoff Pattern (3 threads inside ONE service):

    THREAD A — Kafka Consumer (millisecond latency)
        invoice.extracted.events -> Redis batch_buffer:{batch_id} / single
        buffer, then commits offsets immediately. NEVER touches the LLM or
        Postgres, so max.poll.interval.ms can never be breached even while
        Groq rate limiting parks 40 of 50 sub-graphs for minutes.

    THREAD B — DB Boundary Poller
        Every layer2_poll_interval_s:
        1. drains the single buffer -> immediate SINGLE runs in the pool
        2. polls batch_jobs for status='COMPLETED' with no layer2_batch_runs
           row yet; claims each via INSERT ... ON CONFLICT DO NOTHING
        3. waits (bounded grace) for batch_buffer:{batch_id} to fill to
           total_invoices, then submits the batch to the execution pool
        4. resumes stale SEALED/RUNNING runs (>15 min) from Postgres fallback
           (crash recovery — no double processing thanks to UNIQUE guards)

    THREAD C — Isolated Execution Pool (ThreadPoolExecutor)
        Completely divorced from Kafka. Runs the LangGraph per-invoice
        sub-graphs (thread_id = batch_id::document_id) under the Groq Redis
        token bucket. Waits/sleeps/retries here never affect Kafka offsets.

Usage:
    python -m app.workers.recon_supervisor

Environment:
    LAYER2_CONSUMER_GROUP   consumer group (layer2-supervisor-cg)
    LAYER2_MAX_CONCURRENT   execution pool size (default 4)
    LAYER2_POLL_INTERVAL_S  boundary poll cadence (default 3.0)
    GROQ_RPM_LIMIT          distributed Groq token bucket (default 28)
"""

import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.config import get_settings
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


# =============================================================================
# Execution pool — per-invoice sub-graph runner (Thread C payload)
# =============================================================================


def _mask_payload_with_vault(run_token: str, payload: dict) -> dict:
    """Tokenize structured PII in the extracted payload (RAM vault only)."""
    from app.agent.pii.vault import get_vault

    vault = get_vault(run_token)
    if vault is None:
        return payload
    return vault.mask_invoice_payload(payload)


def run_invoice_subgraph(
    *,
    run_token: str,
    batch_id: str | None,  # None for single-invoice runs (UUID column stays NULL)
    thread_key: str,       # batch_id::document_id / single_{document_id}::document_id
    vendor_code: str,
    document_id: str,
    invoice_number: str,
    payload: dict,
    supervisor_llm=None,
    telemetry=None,        # optional BatchTelemetryWriter (live dashboard)
) -> dict:
    """Invoke one isolated per-invoice LangGraph sub-graph (Map leaf).

    Runs inside a pool worker thread; the compiled graph + PostgresSaver are
    thread-local, so concurrent invoices never share a psycopg connection.
    """
    from app.agent.graph.supervisor import (
        build_persisted_graph,
        invoke_invoice,
        make_thread_config,
    )

    masked = _mask_payload_with_vault(run_token, payload)

    initial_state = {
        "batch_id": batch_id,  # NULL for singles -> invoice_reconciliations.batch_id NULL
        "vendor_code": vendor_code,
        "document_id": document_id,
        "invoice_number": invoice_number,
        "masked_payload": masked,
        "waterfall_flags": [],
        "matched_invoice_ids": [],
        "fuzzy_attempted": False,
    }

    graph = build_persisted_graph(supervisor_model=supervisor_llm)
    config = make_thread_config(thread_key, document_id, run_token=run_token)

    outcome = invoke_invoice(graph, state=initial_state, config=config)

    # ---- live telemetry (best-effort; never affects the outcome) ----------
    from app.telemetry.events import (
        build_invoice_events,
        classify_invoice_path,
    )

    terminal = outcome.get("terminal_status") or "ERROR"
    classification = classify_invoice_path(outcome.get("messages") or [])

    if telemetry is not None and batch_id is not None:
        telemetry.publish(
            batch_id,
            build_invoice_events(
                invoice_number=invoice_number,
                subset_status=outcome.get("subset_status"),
                subset_message=outcome.get("subset_message"),
                path=classification["path"],
                llm_invoked=classification["llm_invoked"],
                tool_calls=classification["tool_calls"],
                terminal=terminal,
                terminal_detail=outcome.get("terminal_detail") or "",
                terminal_utr=outcome.get("terminal_utr"),
            ),
        )

    return {
        "document_id": document_id,
        "invoice_number": invoice_number,
        "vendor_code": vendor_code,
        "batch_id": batch_id,
        "terminal": terminal,
        "terminal_detail": outcome.get("terminal_detail") or "",
        "terminal_utr": outcome.get("terminal_utr"),
        "razorpay_payout_id": outcome.get("terminal_payout_id"),
        "path": classification["path"],
        "llm_invoked": classification["llm_invoked"],
        "tool_calls": classification["tool_calls"],
    }


def run_batch_reconciliation(
    *,
    run_token: str,
    batch_id: str | None,  # ledger batch (None for singles)
    thread_key: str,       # run marker key used for LangGraph thread ids
    invoices: list[dict],
    supervisor_llm=None,
    telemetry=None,        # optional BatchTelemetryWriter (live dashboard)
) -> tuple[int, int, list[dict]]:
    """Execute the Map phase for one sealed batch: one sub-graph per invoice.

    Returns (matched_count, exception_count, outcomes). Rows already in
    invoice_reconciliations short-circuit inside the graph; millisecond races
    resolve through UNIQUE(document_id) + DUPLICATE_EVENT absorption.
    """
    from app.telemetry.events import build_batch_event

    if telemetry is not None and batch_id is not None:
        telemetry.publish(
            batch_id,
            [build_batch_event(batch_id, "batch_started", "Execution pool running the Map phase")],
        )

    matched = 0
    exceptions = 0
    outcomes: list[dict] = []
    for inv in invoices:
        result = run_invoice_subgraph(
            run_token=run_token,
            batch_id=batch_id,
            thread_key=thread_key,
            vendor_code=inv["vendor_code"],
            document_id=inv["document_id"],
            invoice_number=inv["invoice_number"],
            payload=inv.get("payload", {}),
            supervisor_llm=supervisor_llm,
            telemetry=telemetry,
        )
        outcomes.append(result)
        if result["terminal"] in ("LEDGER_COMMITTED", "ALREADY_COMMITTED"):
            matched += 1
        else:
            exceptions += 1
        logger.info(
            "Invoice sub-graph finished",
            extra={
                "batch_id": batch_id,
                "document_id": result["document_id"],
                "terminal": result["terminal"],
            },
        )

    if telemetry is not None and batch_id is not None:
        telemetry.publish(
            batch_id,
            [
                build_batch_event(
                    batch_id,
                    "batch_terminal",
                    f"Run complete: {matched} settled / {exceptions} exceptions",
                    matched=matched,
                    exceptions=exceptions,
                )
            ],
        )
    return matched, exceptions, outcomes


# =============================================================================
# Boundary poller (Thread B)
# =============================================================================


class BoundaryPoller:
    """Seals COMPLETED batches / dispatches singles to the execution pool."""

    def __init__(self, buffer, pool: ThreadPoolExecutor, supervisor_llm=None, telemetry=None):
        settings = get_settings()
        self.buffer = buffer
        self.pool = pool
        self.supervisor_llm = supervisor_llm
        self.telemetry = telemetry  # optional BatchTelemetryWriter (live dashboard)
        self.interval_s = settings.layer2_poll_interval_s
        self.grace_polls = settings.layer2_buffer_grace_polls
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        from app.agent.runtime import boundary

        logger.info("Layer 2 boundary poller started")
        while not self._stop.is_set():
            try:
                self._dispatch_singles()
                self._dispatch_completed_batches(boundary)
                self._resume_stale_runs(boundary)
            except Exception as e:
                logger.error("Boundary poller cycle failed", extra={"error": str(e)})
            self._stop.wait(self.interval_s)
        logger.info("Layer 2 boundary poller stopped")

    # ---- single (non-batch) events: immediate run -------------------------

    def _dispatch_singles(self):
        from app.agent.runtime import boundary

        for event in self.buffer.drain_singles(limit=20):
            try:
                self._submit_single(event, boundary)
            except Exception as e:
                logger.error("Single dispatch failed", extra={"error": str(e)})

    def _submit_single(self, event: dict, boundary):
        data = event.get("data", {})
        document_id = str(data.get("document_id") or "")
        vendor_code = str(data.get("vendor_code") or "")
        invoice_number = str(data.get("invoice_number") or "")
        payload = data.get("extracted_invoice") or {}
        if not document_id or not vendor_code:
            logger.warning("Single event missing identity; skipped")
            return
        run_batch_id = f"single_{document_id}"
        claimed = boundary.claim_run(
            run_batch_id, vendor_code, run_type="SINGLE", total=1
        )
        if not claimed:
            return  # already running / completed
        logger.info(
            "Dispatching single invoice run",
            extra={"run_batch_id": run_batch_id, "document_id": document_id},
        )
        self._submit_run(
            run_batch_id,
            vendor_code,
            [{
                "document_id": document_id,
                "invoice_number": invoice_number,
                "vendor_code": vendor_code,
                "payload": payload,
            }],
            single_run=True,
        )

    # ---- COMPLETED batches -----------------------------------------------

    def _dispatch_completed_batches(self, boundary):
        for batch in boundary.find_sealed_batches(limit=5):
            batch_id = batch["batch_id"]
            claimed = boundary.claim_run(
                batch_id,
                batch["vendor_code"],
                run_type="BATCH",
                total=batch["total_invoices"],
            )
            if not claimed:
                continue
            logger.info("Batch sealed for reconciliation", extra={"batch_id": batch_id})
            self._submit_batch_after_grace(batch_id, batch["vendor_code"], batch["total_invoices"], boundary)

    def _submit_batch_after_grace(self, batch_id, vendor_code, total, boundary):
        """Wait (bounded) for the buffer to fill, then materialize + submit."""
        for _ in range(self.grace_polls):
            if self.buffer.length(batch_id) >= total:
                break
            time.sleep(self.interval_s)

        events = self.buffer.drain_batch(batch_id)
        invoices = self._events_to_inputs(events, batch_id, vendor_code)
        shortfall = max(0, total - len(invoices))

        if shortfall > 0 and not invoices:
            # Redis lost the events (crash / eviction): reconstruct from DB.
            from app.agent.runtime import boundary as b

            invoices = b.build_invoice_inputs_from_db(batch_id)
            shortfall = max(0, total - len(invoices))

        if not invoices:
            logger.error("Sealed batch has no reconcilable invoices", extra={"batch_id": batch_id})
            boundary.close_run(
                batch_id, status="PARTIAL", matched_count=0,
                exception_count=0, shortfall=total,
                last_error="no reconcilable invoice payloads",
            )
            return

        logger.info(
            "Submitting sealed batch to execution pool",
            extra={"batch_id": batch_id, "invoices": len(invoices), "shortfall": shortfall},
        )
        self._submit_run(batch_id, vendor_code, invoices, shortfall=shortfall)

    def _events_to_inputs(self, events: list[dict], batch_id: str, vendor_code: str) -> list[dict]:
        invoices = []
        for ev in events:
            data = ev.get("data", {})
            document_id = str(data.get("document_id") or "")
            if not document_id:
                continue
            invoices.append({
                "document_id": document_id,
                "invoice_number": str(data.get("invoice_number") or ""),
                "vendor_code": str(data.get("vendor_code") or vendor_code),
                "payload": data.get("extracted_invoice") or {},
            })
        return invoices

    def _resume_stale_runs(self, boundary):
        """Crash recovery: resume SEALED/RUNNING runs older than 15 minutes."""
        for run in boundary.find_stale_runs(limit=5):
            run_batch_id = run["batch_id"]
            run_type = run["run_type"]
            logger.warning("Resuming stale Layer 2 run", extra={"batch_id": run_batch_id})
            if run_type == "SINGLE":
                # Rebuild from the extracted invoice row (document_id suffix).
                document_id = run_batch_id[len("single_"):]
                invoices = [
                    {
                        "document_id": document_id,
                        "invoice_number": "",
                        "vendor_code": run["vendor_code"],
                        "payload": self._fetch_payload(document_id),
                    }
                ]
                self._submit_run(run_batch_id, run["vendor_code"], invoices, single_run=True)
            else:
                invoices = boundary.build_invoice_inputs_from_db(run_batch_id)
                self._submit_run(run_batch_id, run["vendor_code"], invoices)

    def _fetch_payload(self, document_id: str) -> dict:
        from sqlalchemy import text

        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT parsed_payload FROM extracted_invoices WHERE document_id = :doc"),
                {"doc": document_id},
            ).first()
            if row is None:
                return {}
            payload = row[0]
            import json
            return json.loads(payload) if isinstance(payload, str) else dict(payload)
        finally:
            db.close()

    # ---- pool submission + run-row lifecycle -----------------------------

    def _submit_run(
        self,
        run_batch_id,
        vendor_code,
        invoices: list[dict],
        shortfall: int = 0,
        single_run: bool = False,
    ):
        from app.agent.pii.vault import new_run_token, register_run, release_run
        from app.agent.runtime import boundary

        run_token = new_run_token()
        register_run(run_token)

        boundary.mark_running(run_batch_id)

        # state batch_id must be NULL (UUID column) for single runs, while the
        # run row / thread key keep the single_ marker.
        ledger_batch_id = None if single_run else run_batch_id

        def _job():
            matched = 0
            exceptions = 0
            last_error = None
            try:
                matched, exceptions, _outcomes = run_batch_reconciliation(
                    run_token=run_token,
                    batch_id=ledger_batch_id,
                    thread_key=run_batch_id,
                    invoices=invoices,
                    supervisor_llm=self.supervisor_llm,
                    telemetry=self.telemetry,
                )
            except Exception as e:
                last_error = str(e)
                logger.error("Batch reconciliation run failed", extra={"batch_id": run_batch_id, "error": last_error})
            finally:
                status = "COMPLETED" if last_error is None else "PARTIAL"
                boundary.close_run(
                    run_batch_id,
                    status=status,
                    matched_count=matched,
                    exception_count=exceptions,
                    shortfall=shortfall,
                    last_error=last_error,
                )
                self.buffer.cleanup_batch(run_batch_id)
                release_run(run_token)

        # Fire-and-forget with outcome logging (the run row is the source of
        # truth for recovery, not this future).
        future = self.pool.submit(_job)

        def _log_done(f):
            if f.exception() is not None:
                logger.error("Pool job raised", extra={"batch_id": run_batch_id, "error": str(f.exception())})

        future.add_done_callback(_log_done)


# =============================================================================
# Main — three-thread supervisor
# =============================================================================


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    settings = get_settings()
    logger.info("Starting Recon Supervisor...")

    supervisor_llm = None
    try:
        from app.agent.graph.supervisor import GroqReActModel
        from app.agent.llm.groq_client import get_groq_client

        client = get_groq_client()
        supervisor_llm = GroqReActModel(client)
    except Exception as e:
        logger.warning(f"Groq unavailable — using deterministic fallback agent: {e}")

    pool = ThreadPoolExecutor(
        max_workers=settings.layer2_max_concurrent,
        thread_name_prefix="recon-pool",
    )

    from app.kafka.layer2_buffer import Layer2RedisBuffer
    from app.kafka.layer2_consumer import Layer2ExtractedConsumer

    buffer = Layer2RedisBuffer(settings.redis_url)

    from app.telemetry.events import BatchTelemetryWriter

    telemetry = BatchTelemetryWriter(settings.redis_url)
    poller = BoundaryPoller(buffer, pool, supervisor_llm=supervisor_llm, telemetry=telemetry)

    # THREAD A —  consumer
    consumer_thread = threading.Thread(
        target=_run_consumer,
        name="layer2-consumer",
        daemon=True,
    )
    # THREAD B — boundary poller
    poller_thread = threading.Thread(
        target=poller.run,
        name="layer2-boundary-poller",
        daemon=True,
    )

    try:
        consumer_thread.start()
        poller_thread.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Recon supervisor interrupted")
    finally:
        poller.stop()
        buffer.close()
        pool.shutdown(wait=False, cancel_futures=True)
        logger.info("Recon supervisor stopped")


def _run_consumer() -> None:
    from app.kafka.layer2_consumer import Layer2ExtractedConsumer
    from app.workers.supervision import run_consumer_supervisor

    # Supervisor restarts the consumer with a capped backoff on any crash
    # (e.g. KafkaTimeoutError while the broker is booting), so a Kafka
    # restart can never permanently kill this buffer thread.
    run_consumer_supervisor(
        "layer2-consumer", lambda: Layer2ExtractedConsumer(), log=logger
    )


if __name__ == "__main__":
    main()
