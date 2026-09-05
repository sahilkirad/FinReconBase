# FinRecon 

**One invoice batch in. A perfectly balanced ledger out.**

FinRecon is a production-grade, event-driven invoice-reconciliation platform built for the **Razorpay Buildathon (Track 4)**. It ingests unstructured vendor invoices (PDF / images), automatically synchronizes **Razorpay settlement** and **bank statement** feeds, reconciles them against the invoices with a **deterministic-first, AI-second** matching engine, and posts mathematically balanced double-entry records to an **immutable ledger** — surfacing only genuine exceptions to a human review desk.

```
        ┌──────────────────────────────────────────────────────────────────┐
        │                    FINRECON — LAYERED PIPELINE                   │
        └──────────────────────────────────────────────────────────────────┘

 Stream 1           Stream 2 (Razorpay)         Stream 3 (Bank)
 Vendor PDFs   ─┐   Settlements  ─┐             CREDIT records ─┐
 (upload)       │   (webhook/JSON)│             (JSON/CSV/auto) │
                ▼                ▼                              ▼
        ┌─────────────────┐   ┌──────────────────────────────────────┐
        │  LAYER 1        │   │  Ingestion APIs                       │
        │  Extraction     │   │  POST /webhooks/razorpay(/batch)      │
        │  FastAPI +     │   │  POST /ingestion/bank                 │
        │  invoice workers│   │  POST /demo/auto-generate-feeds       │
        └────────┬────────┘   └───────────────────┬──────────────────┘
                 │  invoice.processing.events     │
                 │  (5 partitions)                │  razorpay_settlements /
                 ▼                                │  bank_transactions (PG)
        ┌─────────────────┐                       │
        │  OCR · Blur ·   │                       │
        │  Guardrail ·    │                       │
        │  Presidio PII   │                       │
        │  Gemini VLM ·   │                       │
        │  Checksum (math)│                       │
        └────────┬────────┘                       │
                 │  invoice.extracted.events      │
                 ▼                                ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  LAYER 2 — Recon Supervisor (LangGraph + Groq, ReAct)       │
        │  Map-Reduce: one isolated sub-graph per invoice             │
        │  Deterministic fast-path → fuzzy linker → human exception   │
        │  Deterministic 3-phase waterfall (amount → entity → date)   │
        └────────┬────────────────────────────────┬───────────────────┘
                 │                                │
   reconciliation.completed.events      reconciliation.dlq.events
                 ▼                                ▼
        ┌────────────────────────┐   ┌──────────────────────────────┐
        │  LAYER 5 — Ledger      │   │  Exception Materializer      │
        │  Writer (double-entry) │   │  → exception_tickets         │
        │  Idempotency gate ·    │   │  (Maker/Checker HITL queue)  │
        │  DR − CR = 0 guardrail │   └──────────────────────────────┘
        │  ledger_entries (WORM) │
        └────────────────────────┘
                 │ ledger.fatal.dlq.events (unbalanced payloads)
                 ▼
        Frontend — Financial Operations Center (Next.js)
        KPIs · Audit trail · Immutable Ledger · Exception Desk
```

---

## Table of contents

- [Highlights](#highlights)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quick start (Docker)](#quick-start-docker)
  - [1. Environment](#1-environment)
  - [2. Kafka mTLS certificates](#2-kafka-mtls-certificates)
  - [3. Build & start](#3-build--start)
- [Configuration reference](#configuration-reference)
- [Services](#services)
- [Kafka topics & consumer groups](#kafka-topics--consumer-groups)
- [Database tables](#database-tables)
- [API reference](#api-reference)
- [Demo walkthrough](#demo-walkthrough)
- [Generating synthetic test data](#generating-synthetic-test-data)
- [Scaling workers](#scaling-workers)
- [Testing](#testing)
- [Security & fintech guardrails](#security--fintech-guardrails)
- [Troubleshooting](#troubleshooting)

---

## Highlights

| Capability | Where |
|---|---|
| Native multi-tenant vendor auth (PBKDF2 + 120-min JWT, no OAuth) | `POST /auth/vendor/register`, `/auth/vendor/token` |
| 50-invoice batch ingestion with per-page Kafka fan-out — sub-second upload response | `POST /invoices/batch` (202 Accepted) + outbox poller |
| Distributed rate limiting via Redis token buckets (Gemini + Groq) | `REDIS_URL`, `GEMINI_RPM_LIMIT`, `GROQ_RPM_LIMIT` |
| PII masking with Microsoft Presidio before any LLM call | `app/tools/pii_masker.py`, `app/agent/pii/vault.py` |
| Local mathematical checksum — Gemini is **never** used for math | `app/tools/checksum.py` |
| LangGraph map-reduce supervisor with PostgresSaver checkpoints | `app/workers/recon_supervisor.py` |
| Deterministic 3-phase waterfall (amount → fuzzy entity → date window) | `app/agent/tools/subset_sum.py` |
| Immutable double-entry ledger with idempotency + balance guardrails | `app/workers/ledger_writer.py` |
| Transactional outbox → Kafka (no publishes from API threads) | `outbox_events` + `app/workers/outbox_poller.py` |
| DLQ + poison-message handling with materialized HITL exception desk | `reconciliation.dlq.events` → `exception_tickets` |

---

## Tech stack

- **API**: FastAPI, SQLAlchemy 2, psycopg3, Pydantic v2 / pydantic-settings, PyJWT
- **Extraction**: PyMuPDF, OpenCV (blur / boundary detection), Tesseract OCR, Google Gemini (VLM), ONNX document classifier
- **Privacy**: Microsoft Presidio Analyzer + Anonymizer, spaCy
- **Orchestration**: Apache Kafka (KRaft, mTLS), kafka-python, Redis (rate limits / buffers)
- **Agents**: LangGraph + Postgres checkpointer, LangChain Groq (ReAct supervisor)
- **Ledger**: PostgreSQL 16 (WORM-style append-only via triggers/immutability conventions)
- **Frontend**: Next.js 15, React 19, TypeScript, Tailwind CSS, TanStack Query, Zustand, Framer Motion
- **Infra**: Docker Compose (single-node Kafka broker, topics auto-created by `kafka-init`)

---

## Repository layout

```
ReconBase/
├── docker-compose.yml          # Full stack: pg, kafka, redis, API, workers, frontend
├── .env                        # Root env consumed by every compose service (git-ignored)
├── infra/
│   ├── certs/                  # Kafka mTLS keystores/truststores (generated, git-ignored)
│   ├── kafka/client.properties # Kafka CLI client config for topic ops
│   └── migrations/             # 001–005 SQL: schema applied by postgres on first boot
├── backend/
│   ├── app/
│   │   ├── api/                # FastAPI routers (auth, batch, invoices, ingestion, …)
│   │   ├── workers/            # Long-running processes (outbox, invoice, recon, ledger)
│   │   ├── kafka/              # Producer/consumers + SSL config + layer-2 buffer
│   │   ├── agent/              # LangGraph graph, Groq client, tools, PII vault, boundary
│   │   ├── ledger/             # Layer 5 double-entry writer core
│   │   ├── demo/               # Auto-feed generators for Streams 2 & 3
│   │   ├── tools/              # checksum, OCR, VLM, blur, guardrail, preprocessing…
│   │   ├── schemas/  core/  db/  telemetry/
│   │   └── main.py             # FastAPI app factory (all routers)
│   ├── scripts/                # generate_test_batch.py, generate_layer2_feeds.py, seed_demo_vendor.py
│   ├── models/                 # invoice_classifier_fp32.onnx + labels.json
│   ├── tests/                  # 30+ pytest modules (unit, no live Postgres needed)
│   ├── .env.example            # Template of every runtime setting
│   └── Dockerfile
├── frontend/                   # Next.js app (Command Center, FinOps, Ledger, Exceptions)
│   ├── app/(auth)/             # Onboarding / login
│   ├── app/(dashboard)/        # Command Center, Financial Operations Center, Ledger, Exception Desk
│   ├── components/             # finops, onboarding, reconciliation, ui
│   ├── lib/                    # api client, TanStack queries, money formatting, active-batch
│   └── store/                  # Zustand (auth, persisted session)
```

---

## Prerequisites

- Docker + Docker Compose v2
- Git Bash / PowerShell (commands below are PowerShell-safe)
- API keys:
  - **Google Gemini** (Layer 1 VLM extraction) — `GEMINI_API_KEY`
  - **Groq** (Layer 2 supervisor LLM) — `GROQ_API_KEY`
- ~8 GB free RAM recommended (Postgres + Kafka + Redis + 3 OCR workers + Next.js)

---

## Quick start (Docker)

### 1. Environment

Copy the template and fill in the required secrets:

```powershell
cd C:\SK_codebase\code_base\ReconBase
Copy-Item backend\.env.example .env
```

Edit `.env` and set at minimum:

```dotenv
JWT_SECRET_KEY=<random-256-bit-secret>
GOOGLE_OAUTH_CLIENT_ID=<any-string; native auth does not call Google>
GEMINI_API_KEY=<your-gemini-key>
GROQ_API_KEY=<your-groq-key>
GROQ_MODEL=qwen/qwen3.8-27b
```

> The root `.env` is the single env file mounted into every compose service (`env_file: .env`). Keep it in the repo root, **not** in `backend/`.

Generate a JWT secret with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 2. Kafka mTLS certificates

The Kafka broker enforces **mutual TLS** (`KAFKA_SSL_CLIENT_AUTH: required`). The first boot **fails fast** if `infra/certs/` has no keystores. Generate everything (CA, server + client keystores/truststores, `client.properties`) in one shot — run from the **repo root** in PowerShell:

```powershell
@'
set -e
cd /certs
PASSWORD="finrecon-kafka-2024"

openssl genrsa -out ca.key 2048
openssl req -new -x509 -key ca.key -out ca.crt -days 365 -subj "/CN=finrecon-ca"

keytool -genkeypair -alias kafka-server -keyalg RSA -keysize 2048 -keystore kafka.server.keystore.jks -storepass "$PASSWORD" -keypass "$PASSWORD" -dname "CN=kafka" -ext "SAN=dns:kafka,dns:localhost"
keytool -certreq -alias kafka-server -keystore kafka.server.keystore.jks -storepass "$PASSWORD" -file server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server-signed.crt -days 365
keytool -importcert -alias ca -file ca.crt -keystore kafka.server.keystore.jks -storepass "$PASSWORD" -noprompt
keytool -importcert -alias kafka-server -file server-signed.crt -keystore kafka.server.keystore.jks -storepass "$PASSWORD" -noprompt

keytool -importcert -alias ca -file ca.crt -keystore kafka.server.truststore.jks -storepass "$PASSWORD" -noprompt

keytool -genkeypair -alias kafka-client -keyalg RSA -keysize 2048 -keystore kafka.client.keystore.jks -storepass "$PASSWORD" -keypass "$PASSWORD" -dname "CN=client"
keytool -certreq -alias kafka-client -keystore kafka.client.keystore.jks -storepass "$PASSWORD" -file client.csr
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out client-signed.crt -days 365
keytool -importcert -alias ca -file ca.crt -keystore kafka.client.keystore.jks -storepass "$PASSWORD" -noprompt
keytool -importcert -alias kafka-client -file client-signed.crt -keystore kafka.client.keystore.jks -storepass "$PASSWORD" -noprompt

keytool -importcert -alias ca -file ca.crt -keystore kafka.client.truststore.jks -storepass "$PASSWORD" -noprompt

keytool -importkeystore -srckeystore kafka.client.keystore.jks -srcstorepass "$PASSWORD" -destkeystore client.p12 -deststoretype PKCS12 -deststorepass "$PASSWORD"
openssl pkcs12 -in client.p12 -out client.crt -nodes -nokeys -passin pass:"$PASSWORD"
openssl pkcs12 -in client.p12 -out client.key -nodes -nocerts -passin pass:"$PASSWORD"

printf "%s\n" "$PASSWORD" > kafka_keystore_creds
printf "%s\n" "$PASSWORD" > kafka_sslkey_creds
printf "%s\n" "$PASSWORD" > kafka_truststore_creds

printf "%s\n" "security.protocol=SSL" "ssl.truststore.location=/etc/kafka/secrets/kafka.client.truststore.jks" "ssl.truststore.password=$PASSWORD" "ssl.keystore.location=/etc/kafka/secrets/kafka.client.keystore.jks" "ssl.keystore.password=$PASSWORD" "ssl.key.password=$PASSWORD" "ssl.endpoint.identification.algorithm=" > client.properties

rm -f *.csr *-signed.crt ca.srl client.p12
echo "DONE: All certificates generated"
'@ | docker run --rm -i --entrypoint /bin/bash -v "${PWD}\infra\certs:/certs" confluentinc/cp-kafka:7.9.0 -s
```

You should see `DONE: All certificates generated` and a populated `infra/certs/`.

### 3. Build & start

```powershell
docker compose up -d --build
docker compose ps
```

Health checks gate the startup order: `postgres` → `kafka` (mTLS healthcheck) → `kafka-init` (creates the 5 topics) → API/workers/frontend.

| What | URL |
|---|---|
| Frontend (Command Center) | http://localhost:3000 |
| API docs (Swagger UI) | http://localhost:8000/docs |
| API health | http://localhost:8000/health |
| PostgreSQL (DBeaver/pgAdmin) | `localhost:5457` · `postgres` / `postgres` · db `finrecon` |
| Kafka broker (mTLS, host) | `localhost:9094` |

Watch the whole pipeline:

```powershell
docker compose logs -f backend-api outbox_poller invoice_worker recon-supervisor ledger-writer
```

---

## Configuration reference

All settings live in `.env` (repo root). See `backend/.env.example` for the annotated template and `backend/app/core/config.py` for defaults. The most important ones:

| Variable | Default | Purpose |
|---|---|---|
| `JWT_SECRET_KEY` | — (required) | HS256 signing secret |
| `JWT_EXPIRE_MINUTES` | `120` | Access-token lifetime (frontend session length) |
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@postgres:5432/finrecon` | SQLAlchemy DSN (compose network) |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9093` | Internal mTLS listener |
| `KAFKA_SSL_*` | `/app/certs/*` | Client cert/key/CA mounted from `infra/certs` |
| `GEMINI_API_KEY` | — | Layer 1 VLM extraction |
| `GEMINI_RPM_LIMIT` | `15` | Redis token bucket shared across all invoice workers |
| `GEMINI_REQUEST_TIMEOUT_S` | `300` | VLM call timeout (dense OCR needs headroom) |
| `LAYER1_MAX_CONCURRENT` | `3` | Per-worker Gemini concurrency semaphore |
| `LAYER1_CONSUMER_GROUP` | `layer1_extractor_group` | Invoice-worker consumer group |
| `GROQ_API_KEY` / `GROQ_MODEL` | — / `qwen/qwen3.8-27b` | Layer 2 supervisor LLM |
| `GROQ_RPM_LIMIT` | `28` | Redis token bucket for sub-graph LLM calls |
| `LAYER2_MAX_CONCURRENT` | `4` | Isolated LangGraph execution pool size |
| `REDIS_URL` | `redis://redis:6379/0` | Distributed rate limiting |
| `AUTO_FEED_WAIT_S` | `3600` | Bounded server-side wait for demo feed generation |
| `ALLOWED_BATCH_EXTENSIONS` | `.pdf,.jpg,.jpeg,.png` | Batch upload whitelist (**CSV is rejected by design**) |
| `MAX_BATCH_SIZE_MB` | `100` | Upload ceiling |
| `DEMO_VENDOR_SECRET` | `FinReconDemo@2026` | Secret for `scripts/seed_demo_vendor.py` |

---

## Services

| Container | Image / command | Replicas | Role |
|---|---|---|---|
| `postgres` | postgres:16-alpine | 1 | System of record; migrations auto-run on first boot |
| `kafka` | confluentinc/cp-kafka:7.9.0 (KRaft, mTLS) | 1 | Broker on `:9093` (internal SSL), `:9094` (host SSL) |
| `kafka-init` | cp-kafka one-shot | 1 | Creates the 5 topics, then exits |
| `redis` | redis:7-alpine | 1 | Token buckets + Layer 2 batch buffer |
| `backend-api` | FastAPI/uvicorn | 1 | All HTTP routes; never does OCR/VLM in-request |
| `outbox_poller` | python `app.workers.outbox_poller` | 1 | Reads `outbox_events` → publishes raw page events |
| `invoice_worker` | python `app.workers.invoice_worker` | **3** | Layer 1 extraction (OCR → VLM → checksum → fan-in) |
| `recon-supervisor` | python `app.workers.recon_supervisor` | 1 | Layer 2 LangGraph map-reduce + outbox/ledger triggers |
| `ledger-writer` | python `app.workers.ledger_writer` | 1 | Layer 5 double-entry writer (2 consumer threads: completed + DLQ) |
| `frontend` | Next.js standalone | 1 | Business dashboard on `:3000` |

---

## Kafka topics & consumer groups

Topics are created by `kafka-init` (`KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`):

| Topic | Partitions | Producer | Consumers |
|---|---|---|---|
| `invoice.processing.events` | 5 | Outbox poller (raw page events) | `invoice_worker` × 3 — group `layer1_extractor_group` |
| `invoice.extracted.events` | 3 | Invoice workers (fan-in) | `recon-supervisor` — group `layer2-supervisor-cg` |
| `reconciliation.completed.events` | 3 | Recon supervisor / LangGraph | `ledger-writer` — group `layer5-ledger-writer-cg` |
| `reconciliation.dlq.events` | 3 | Workers (poison messages) + supervisor (exceptions) | `ledger-writer` DLQ thread — group `layer5-exception-materializer-cg` |
| `ledger.fatal.dlq.events` | 3 | Ledger writer (unbalanced payloads) | (alerting / ops) |

Inspect offsets/topics:

```powershell
docker compose exec kafka kafka-topics --bootstrap-server kafka:9093 --command-config /etc/kafka/secrets/client.properties --list
docker compose exec kafka kafka-console-consumer --bootstrap-server kafka:9093 --command-config /etc/kafka/secrets/client.properties --topic reconciliation.dlq.events --from-beginning --max-messages 5
```

---

## Database tables

Applied by `infra/migrations/001…005` (idempotent, run on first postgres boot). Connect with DBeaver: `localhost:5457`, user/pass `postgres/postgres`, db `finrecon`.

| Table | Layer | Purpose |
|---|---|---|
| `vendor_users`, `vendor_credentials` | Auth | Tenants + PBKDF2-hashed API secrets (native onboarding) |
| `batch_jobs` | 1 | Batch header + atomic `processed_count`/`failed_count` progress |
| `batch_invoice_items` | 1 | Per-invoice status inside a batch (extraction auditability) |
| `extracted_invoices` | 1 | Layer 1 validated output (integer paise) |
| `razorpay_settlements` | 2/3 | Stream 2 — settlement webhook rows |
| `bank_transactions` | 2/3 | Stream 3 — bank CREDIT rows |
| `invoice_reconciliations` | 2 | Per-invoice reconciliation outcome + matched UTR |
| `layer2_batch_runs` | 2 | Supervisor run telemetry (state, counts, errors) |
| `langgraph_checkpoints` / `checkpoint_*` | 2 | PostgresSaver — crash-safe resume of sub-graphs |
| `outbox_events` | Cross | Transactional outbox → Kafka relay |
| `idempotency_keys` | 5 | Exactly-once gate for ledger writes |
| `reconciliation_batches` | 5 | Ledger header per completed reconciliation |
| `ledger_entries` | 5 | Immutable double-entry lines (DR + CR, net 0) |
| `exception_tickets` | 5 | Materialized DLQ → Maker/Checker review queue |

---

## API reference

Interactive docs: http://localhost:8000/docs. Every route below the auth ones requires `Authorization: Bearer <jwt>`.

### Auth (`/auth`)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/auth/vendor/register` | `{vendor_code, vendor_name, email, api_secret}` | 201 + JWT + profile |
| POST | `/auth/vendor/token` | `{vendor_code, api_secret}` | 200 + JWT (120 min) |
| GET | `/auth/me` | — | Authenticated profile |

### Ingestion (`/invoices`)
| Method | Path | Notes |
|---|---|---|
| POST | `/invoices` | Single PDF/image upload (guarded, checksummed) |
| POST | `/invoices/batch` | **Multipart batch upload** → 202 `{batch_id}` immediately (no OCR in the request thread) |
| GET | `/invoices/batch/{batch_id}` | Batch status + progress counters |
| GET | `/invoices/batch/{batch_id}/invoices` | Per-invoice extraction results |

### Streams 2 & 3 ingestion
| Method | Path | Notes |
|---|---|---|
| POST | `/webhooks/razorpay` | Single settlement webhook |
| POST | `/webhooks/razorpay/batch` | Batch settlements |
| POST | `/ingestion/bank` | List of bank transactions |
| POST | `/demo/auto-generate-feeds` | **Demo mode**: derive razorpay + bank rows from the uploaded batch's invoices, pre-seal |

### Monitoring / ledger / exceptions
| Method | Path | Notes |
|---|---|---|
| GET | `/batches/latest` | Most recent batch for the JWT vendor (nav rehydration) |
| GET | `/batches/{batch_id}/telemetry` | FinOps dashboard aggregates |
| GET | `/batches/{batch_id}/telemetry/events` | Audit/activity feed |
| GET | `/ledger/entries` | Immutable double-entry journal |
| GET | `/exception-tickets` | HITL review queue |
| PATCH | `/exception-tickets/{ticket_id}` | Resolve / update a ticket |
| GET | `/health` | Liveness |

Example — register + upload + poll:

```powershell
$token = (Invoke-RestMethod -Method Post -Uri http://localhost:8000/auth/vendor/register -ContentType "application/json" -Body '{"vendor_code":"VEND_DEMO","vendor_name":"Demo Vendor","email":"demo@finrecon.io","api_secret":"FinReconDemo@2026"}').access_token

$headers = @{ Authorization = "Bearer $token" }
$resp = Invoke-RestMethod -Method Post -Uri http://localhost:8000/invoices/batch -Headers $headers -Form @{ file = Get-Item .\test_batch_50.pdf }
$resp.batch_id

Invoke-RestMethod -Method Get -Uri "http://localhost:8000/invoices/batch/$($resp.batch_id)" -Headers $headers
```

---

## Demo walkthrough

1. **Open** http://localhost:3000 and register a vendor (name, vendor code, email, API secret ≥ 8 chars). You are logged in immediately.
2. **Command Center** — enable **“Simulate Live Payment Gateway & Bank Feeds”** (demo mode), drag your invoice PDF(s), and click **Run Reconciliation**.
   - Upload returns a `batch_id` (202) and routes you to the Financial Operations Center.
   - Behind the scenes the demo feed generator waits for Layer 1 extraction and injects the matching Razorpay + bank rows **before** the Layer 2 seal — so every deterministic pre-node sees them. No manual file generation needed.
3. **Financial Operations Center** — watch KPIs fill in: Total value cleared, auto-clearance rate, pending review value, and the audit/activity feed as invoices move to the ledger.
4. **Immutable Ledger** — verify every cleared invoice posted a matched DR/CR pair that nets to ₹0.00.
5. **Exception Desk** — the N invoices you left unmatched (anomalies) appear as review tickets with a system diagnostic, instead of hallucinated matches.

For a manual/API-only run (no UI), see the API example above and then call:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/demo/auto-generate-feeds -Headers $headers -ContentType "application/json" -Body '{"batch_id":"<batch_id>","anomalies":3}'
```

---

## Generating synthetic test data

All scripts run from `backend/` with the local Python environment (they only need PyMuPDF / SQLAlchemy / psycopg + `.env` reachable).

**Test invoice PDFs** (N invoices, one per page, realistic Indian GST + TDS + bank details):

```powershell
cd backend
python scripts/generate_test_batch.py test_batch_50.pdf 50   # or 25 for a faster run
```

**Seed demo vendors** (idempotent, for UI logins without registration):

```powershell
docker compose exec backend-api python scripts/seed_demo_vendor.py
# secret defaults to FinReconDemo@2026 unless DEMO_VENDOR_SECRET is set
```

**Manual Layer-2 feed files** (legacy path — reads Postgres so amounts always reconcile with what Layer 1 extracted):

```powershell
python scripts/generate_layer2_feeds.py --batch-id <batch_id> [--anomalies N] [--out-dir ./feeds]
```

> The modern path is `POST /demo/auto-generate-feeds` from the UI or API — it uses the exact same builders (`app/demo/feeds.py`) in-process.

---

## Scaling workers

Zero-code scaling — add replicas of the existing containers:

```powershell
docker compose up -d --scale invoice_worker=5
```

Safe because every worker joins the same consumer group (`layer1_extractor_group`) and partitions (`invoice.processing.events` has 5) are rebalanced automatically. Each worker stays a **single sequential lane** — the concurrency model is “more lanes”, never threads inside one worker. Same for `recon-supervisor` (LangGraph checkpointing makes it crash-safe) and `ledger-writer` (idempotency keys make it exactly-once).

---

## Testing

Backend unit tests need **no live Postgres/Kafka** (DB access is faked with substring-dispatching sessions). Two modules require Docker-only system deps (`python-magic`, `httpx2`), so run the suite inside the container for 100% coverage:

```powershell
docker compose exec backend-api python -m pytest -q
```

Locally, point at the modules that only need pure Python:

```powershell
cd backend
python -m pytest tests/test_checksum.py tests/test_subset_sum.py tests/test_tds_mdr.py `
  tests/test_fuzzy_linker.py tests/test_vlm_optimizer.py tests/test_layer2_supervisor_graph.py -q
```

Frontend typecheck / build:

```powershell
cd frontend
npm run typecheck
npm run build
```

---

## Security & fintech guardrails

- **Kafka mTLS** — broker requires client certificates; all service traffic is encrypted (no PLAINTEXT listener except the controller).
- **Deterministic-first, AI-second** — exact matches short-circuit before any LLM call; the LLM is only invoked for genuine exceptions (saves ~90% of Groq quota and removes hallucination risk on routine work).
- **Math is local, never by Gemini** — checksum validation and the 3-phase waterfall run in Python/SQL.
- **PII masking** — Presidio replaces PAN / Aadhaar / account numbers before any VLM/LLM prompt; tokens are rehydrated only inside secure tool calls.
- **Exactly-once ledger writes** — `idempotency_keys` unique-constraint gate drops redelivered events; DR − CR = 0 is validated before commit; unbalanced payloads go to `ledger.fatal.dlq.events`.
- **Transactional outbox** — DB rows + Kafka publishes are never done in the API thread; the outbox poller relays committed rows.
- **Poison-message handling** — terminal extraction errors are published to `reconciliation.dlq.events` and the offset is committed (no infinite retry loops).
- **Rate governance** — Redis token buckets enforce `GEMINI_RPM_LIMIT` and `GROQ_RPM_LIMIT` across all containers; per-worker semaphores cap in-flight calls; full-jitter backoff on 429s.
- **Auditability** — every extraction failure still writes a `batch_invoice_items` row with `status='FAILED'`, so the CFO view is always honest (never “12 of 50 processed”).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Kafka container dies: `keystore.jks exists FAILED` / certs folder empty | Run the certificate-generation command in [Quick start §2](#2-kafka-mtls-certificates) before `docker compose up` |
| `429` / slow VLM or Groq calls | Free-tier limits — watch `GEMINI_RPM_LIMIT`, `GROQ_RPM_LIMIT`; scale `invoice_worker` for more lanes; don’t lower `GEMINI_REQUEST_TIMEOUT_S` |
| Upload returns `422` | Body must be `multipart/form-data` with field `file`; `.csv` is rejected by design (415/422) — PDFs/images only |
| `422` on run-reconciliation from the UI | Token missing/expired — sign in again (the frontend redirects on 401); register creates the JWT immediately |
| “No data in `extracted_invoices` / `batch_invoice_items`” | Check worker logs first: `docker compose logs invoice_worker`; confirm `infra/certs` + real `GEMINI_API_KEY` are present |
| Ledger shows nothing after a clean Layer 2 | Confirm `recon-supervisor` reached the completed topic and `ledger-writer` consumed it (`docker compose logs ledger-writer`) |
| `kafka.net.selector` warnings in worker logs | Library-internal diagnostics, benign when sporadic (TLS handshake > 100 ms). Dampen with `logging.getLogger("kafka.net.selector").setLevel(logging.ERROR)` if noisy |
| Frontend proxy errors | Browser only calls `/api/v1/*`; in local dev set `API_PROXY_TARGET=http://localhost:8000` in `frontend/.env.local` |
| Postgres data inspection | DBeaver → `localhost:5457`, `postgres`/`postgres`, db `finrecon` |
| Reset everything | `docker compose down -v` (wipes volumes — re-run migrations and regenerate certs if needed) |

---

*FinRecon — Razorpay Buildathon Track 4. Backend is FastAPI/LangGraph/Kafka, frontend is Next.js. Not investment advice — balanced to the paise, always.*
