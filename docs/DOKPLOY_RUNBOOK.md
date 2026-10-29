# Dokploy runbook

Single supported deployment: Docker Compose on Dokploy. **Exactly one backend replica and one uvicorn worker** (state lives in-process and in SQLite).

> Live trading is not approved until the gates in `NAUTILUS_TRADER_2_UPGRADE_PLAN.md` ("Tiêu chí ra khỏi staging") pass. Default mode is paper, disarmed.

## 1. Environment (Dokploy → Environment tab)

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes | `openssl rand -hex 32` (>= 32 chars) |
| `ADMIN_PASSWORD` | yes | >= 12 chars, not a common value |
| `ENCRYPTION_KEY` | yes | Fernet key. **Back it up separately from the DB**; losing it makes stored exchange API keys unreadable. Rotate with `NEW,OLD`. |
| `ENVIRONMENT` | no | Leave as `production` (disables `/docs`, enforces secret checks) |
| `CORS_ORIGINS` | yes | Exact frontend origin(s), e.g. `https://app.example.com` |
| `FORWARDED_ALLOW_IPS` | yes | Network/IP of Traefik so rate limits see real client IPs |
| `VITE_NAUTILUS_API_URL`, `VITE_WS_URL` | yes (build args) | Public API/WS URLs baked into the frontend bundle |
| `ALLOW_PRODUCTION_TRADING` | no | Leave unset: exchange adapters default to the Binance Demo environment (fake funds, keys from demo.binance.com) and production connections are refused (403). Set to `1` only after the go-live gates pass. |
| `LOG_LEVEL`, `LOG_FORMAT` | no | JSON logs by default (`LOG_FORMAT=text` for local); each request has an `X-Request-ID` |

The backend refuses to start on missing or weak secrets.

## 2. Services, ports, volumes

- `backend`: port 8000 (bind to localhost in compose; route through Traefik). Data volume `backend_data` -> `/app/data` (SQLite DBs, backups, catalog).
- `frontend`: nginx-unprivileged on **port 8080**; point the Dokploy domain at 8080.
- WebSocket: route `/ws` to the backend with upgrade headers (Traefik does this by default).
- Health: container healthcheck uses `/readyz` (DB + engine); `/health` is liveness only.

## 3. Deploy

1. Back up `/app/data` volume and the `ENCRYPTION_KEY` before every deploy.
2. Deploy; wait for backend healthy (`start_period` 60s).
3. Check `GET /readyz` is 200 and logs show no `ERROR`.
4. Confirm trading mode is `paper` and kill switch state via the admin UI / `/api/trading/status`.

## 4. Rollback

1. Kill switch, cancel open orders, ensure disarmed.
2. Redeploy the previous image/commit (v2 code cannot run on v1 dependencies; `docs/requirements.v1.lock.txt` pins v1).
3. Verify `/readyz`; reconcile open orders and positions on the exchange manually.
4. Restore DB from the off-host backup only if schema/data were affected (restore needs TOTP step-up and always ends disarmed).

Rollback does not reverse orders already on the exchange.

## 5. Backups (still to automate)

`/api/database/backup` creates a consistent SQLite copy on the same volume. Schedule an off-host copy (Dokploy volume backup to S3) of `/app/data`, encrypted, with retention, and rehearse a restore in staging. This is not automated yet.
