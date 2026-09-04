# ---- Stage 1: dependencies + build -----------------------------------------
FROM node:20-alpine AS builder
WORKDIR /app

COPY package.json package-lock.json ./
# Deterministic install from the committed lockfile.
RUN npm ci --no-audit --no-fund

COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# In-container rewrite target: the FastAPI service on the compose network.
ENV API_PROXY_TARGET=http://backend-api:8000
RUN npm run build

# ---- Stage 2: minimal standalone runtime -----------------------------------
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
ENV API_PROXY_TARGET=http://backend-api:8000

# standalone output already bundles node_modules/.next for the server
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
