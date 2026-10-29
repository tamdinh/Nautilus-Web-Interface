# syntax=docker/dockerfile:1
# Frontend image: Vite build -> non-root nginx.
#
# Targets:
#   (default)      production nginx image serving the SPA on port 8080
#   --target dist  scratch image holding only /app/dist, so CI can build the bundle on the
#                  native runner architecture (avoids running esbuild under QEMU when the
#                  final image is built for another arch):
#                    docker buildx build --target dist --output type=local,dest=./dist-out .
#                  Idea credited to the chingyap1/Nautilus-Web-Interface fork (Dockerfile.deploy).

FROM node:22-alpine AS builder

WORKDIR /app

# Install dependencies first (layer cache)
COPY package.json package-lock.json ./
RUN npm ci

# Copy source and build
COPY . .

# Build-time variables embedded into the JS bundle by Vite
ARG VITE_NAUTILUS_API_URL=http://localhost:8000
ARG VITE_WS_URL=ws://localhost:8000
ENV VITE_NAUTILUS_API_URL=$VITE_NAUTILUS_API_URL \
    VITE_WS_URL=$VITE_WS_URL

RUN npm run build

# ── Bundle only (CI: --target dist) ──────────────────────────────────────────
FROM scratch AS dist
COPY --from=builder /app/dist /

# ── Production image ─────────────────────────────────────────────────────────
FROM nginxinc/nginx-unprivileged:stable-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

# Unprivileged nginx listens on 8080 (map host:80 or a proxy to this port)
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]

CMD ["nginx", "-g", "daemon off;"]
