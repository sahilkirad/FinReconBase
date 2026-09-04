# FinRecon Frontend (Next.js · Track 4)

Native vendor-auth onboarding + dashboard shell. Reconciliation Command
Center (M5), Live Telemetry (M6) and Ledger/Exception pages (M7) land on top
of this scaffold.

## Local development (host)

Backend must be running (Docker compose) with the M1–M3 routes live.

```bash
npm install
# point the API proxy at the host-exposed backend (bypasses CORS entirely)
echo "API_PROXY_TARGET=http://localhost:8000" > .env.local
npm run dev
# http://localhost:3000
```

Same-origin rewrites: the browser only ever calls `/api/v1/*`; the Next
server forwards to FastAPI. No CORS middleware needed on the backend.

## Type-check & production build

```bash
npm run typecheck
npm run build
npm run start   # http://localhost:3000 (standalone build needs no `next` dev deps)
```

## Docker (added to compose in M8)

```bash
docker build -t reconbase-frontend .
docker run --rm -p 3000:3000 --network reconbase_default reconbase-frontend
```

The container defaults `API_PROXY_TARGET` to `http://backend-api:8000` (the
compose network service name).

## Design system (locked)

- Primary action `#0D94FB` · Nav/deep header `#012652` · Canvas white ·
  hairline border `#EBECF0`
- Sans: Inter (system fallback) · Mono for UTRs/batch ids/amounts
  (`font-mono` + `tabular-nums`)
- Framer Motion spring physics (< 1.2 s), TanStack Query caching/polling,
  Zustand sessionStorage persistence, axios JWT interceptors + 401 bounce
