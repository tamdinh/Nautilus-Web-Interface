# Nautilus Web Interface

> Professional trading interface for Nautilus Trader — FastAPI backend + React frontend

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![React](https://img.shields.io/badge/react-18+-61DAFB.svg)](https://reactjs.org/)
[![TypeScript](https://img.shields.io/badge/typescript-5+-3178C6.svg)](https://www.typescriptlang.org/)

> **Risk disclaimer.** This software is provided as is, without warranty. Trading, especially with leverage, can lose all your capital. It runs on a release-candidate engine and has not completed testnet validation or a staging soak (see `docs/NAUTILUS_TRADER_2_UPGRADE_PLAN.md`). Do not trade real money with it until those gates pass. You are solely responsible for any use.

## Overview

Nautilus Web Interface is a full-stack admin and trading dashboard for [Nautilus Trader](https://nautilustrader.io/) — a high-performance algorithmic trading platform. It provides a modern web UI to manage strategies, monitor positions, control risk, view live market data, and run backtests.

## Features

- **Trader Dashboard** — Strategies, orders, positions, risk, market data, alerts, backtesting, performance
- **Admin Panel** — Engine components, adapters, system monitoring, database operations, settings
- **Live Market Data** — Real prices from Binance public API (no API key needed) with 5s TTL cache and offline fallback
- **Persistent Alerts** — SQLite-backed price alerts that survive server restarts
- **Real-time WebSocket** — Live engine/position/risk updates pushed to the frontend
- **API Key Auth** — Optional API key protection (set `API_KEY` env var to enable)
- **Configurable CORS** — Strict origin allowlist via `CORS_ORIGINS` env var
- **Paper / live modes with kill switch** — Paper by default; live trading is explicit, TOTP step-up protected and fail-closed (see the upgrade plan)

## Architecture

```
┌─────────────────────┐     HTTP/WS      ┌──────────────────────┐     Python
│   React Frontend    │ ────────────────▶ │  nautilus_fastapi.py │ ──────────▶ NautilusTradingSystem
│   (TypeScript/Vite) │ ◀──────────────── │  (production server) │            nautilus_core.py
└─────────────────────┘                   └──────────────────────┘
                                                     │
                                          ┌──────────┴──────────┐
                                          │  market_data_service │  ──▶ Binance API
                                          │  alerts_db           │  ──▶ SQLite (backend/data/alerts.db)
                                          │  auth                │  ──▶ API key middleware
                                          └─────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.13+
- Node.js 18+
- 2 GB RAM minimum

### Backend

```bash
cd backend

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment file
cp .env.example .env

# Start server (production entry point)
python3 nautilus_fastapi.py
```

Server starts at `http://localhost:8000`.
Interactive API docs: `http://localhost:8000/docs`

### Frontend

```bash
cd frontend

# Install dependencies
npm install

# Copy and edit environment file
cp .env.example .env

# Development server
npm run dev
```

Frontend starts at `http://localhost:5173`.

## Project Structure

```
Nautilus-Web-Interface/
├── backend/
│   ├── nautilus_fastapi.py      # Production server (entry point)
│   ├── nautilus_core.py         # NautilusTradingSystem wrapper
│   ├── market_data_service.py   # Live Binance prices with TTL cache
│   ├── auth.py                  # API key middleware
│   ├── admin_db_api.py          # Admin database API router (/api/admin/*, admin JWT)
│   ├── strategies/              # Strategy implementations
│   ├── .env.example             # Environment variable template
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/               # All UI pages (see below)
│   │   ├── services/            # API client (nautilusService.ts)
│   │   ├── lib/                 # api.ts, utils.ts
│   │   ├── hooks/               # useWebSocket.ts
│   │   └── App.tsx              # Router
│   ├── .env.example             # Environment variable template
│   └── package.json
├── docs/
│   ├── NAUTILUS_TRADER_2_UPGRADE_PLAN.md
│   ├── DOKPLOY_RUNBOOK.md
│   └── archive/                 # Historical reports (not current)
```

## Pages

### Trader Section (`/trader/*`)

| Page | Route | Description |
|------|-------|-------------|
| Dashboard | `/trader` | Overview: engine status, active strategies, open positions, risk summary |
| Strategies | `/trader/strategies` | Create, start, stop, and delete trading strategies |
| Orders | `/trader/orders` | Place and cancel orders; filter by status |
| Positions | `/trader/positions` | View and close open positions |
| Risk | `/trader/risk` | Risk metrics and configurable limits |
| Market Data | `/trader/market-data` | Live prices for BTC, ETH, BNB, SOL, ADA, DOT (Binance) |
| Performance | `/trader/performance` | PnL summary, win rate, trade history |
| Alerts | `/trader/alerts` | Price alerts (persisted in SQLite) |
| Backtesting | `/trader/backtesting` | Run and review backtests |

### Admin Section (`/admin/*`)

| Page | Route | Description |
|------|-------|-------------|
| Dashboard | `/admin` | Quick actions and system overview |
| Components | `/admin/components` | Manage Nautilus engine components |
| Adapters | `/admin/adapters` | Exchange/broker adapter connections |
| Monitoring | `/admin/monitoring` | CPU, memory, disk, uptime (via psutil) |
| Settings | `/admin/settings` | System configuration |
| Database | `/admin/database` | PostgreSQL, Parquet, Redis operations |

## API Endpoints

Production server: `nautilus_fastapi.py` on port 8000.

### Core
```
GET  /api/health
POST /api/engine/initialize
GET  /api/engine/info
POST /api/engine/shutdown
```

### Trading
```
GET    /api/strategies
POST   /api/strategies
POST   /api/strategies/{id}/start
POST   /api/strategies/{id}/stop
DELETE /api/strategies/{id}

GET    /api/orders
POST   /api/orders
DELETE /api/orders/{id}

GET  /api/positions
POST /api/positions/{id}/close

GET  /api/trades
GET  /api/account
```

### Risk
```
GET  /api/risk/metrics
GET  /api/risk/limits
POST /api/risk/limits
```

### Market Data (live — Binance)
```
GET /api/market-data/instruments   # BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, ADAUSDT, DOTUSDT
GET /api/market-data/{symbol}      # Single symbol ticker
```

### Alerts (SQLite-persisted)
```
GET    /api/alerts
POST   /api/alerts
DELETE /api/alerts/{id}
```

### System & Admin
```
GET  /api/system/metrics           # CPU, memory, disk, uptime
GET  /api/performance/summary
GET  /api/settings
POST /api/settings
POST /api/database/backup
POST /api/database/optimize
POST /api/database/clean
```

### Backtesting
```
POST /api/nautilus/demo-backtest
POST /api/nautilus/backtest
```

### WebSocket
```
WS /ws    # Real-time engine/strategy/position/risk updates (2s interval)
```

## Configuration

### Backend — `backend/.env`

```env
# Allowed CORS origins (comma-separated). Defaults to localhost:5173 + :3000.
CORS_ORIGINS=http://localhost:5173,http://localhost:3000

# API key for all endpoints. Leave blank to disable auth (dev only).
# Generate: openssl rand -hex 32
API_KEY=

# Server port (default: 8000)
NAUTILUS_API_PORT=8000
```

### Frontend — `frontend/.env`

```env
# Backend API base URL
VITE_NAUTILUS_API_URL=http://localhost:8000

# WebSocket URL
VITE_WS_URL=ws://localhost:8000

# API key (must match server API_KEY if set)
VITE_API_KEY=
```

## Deployment

Docker on Dokploy is the only supported target. See [docs/DOKPLOY_RUNBOOK.md](docs/DOKPLOY_RUNBOOK.md).
Older VPS / Cloudflare Pages guides were moved to `docs/archive/` and are no longer supported.

## Technology Stack

### Backend
| Package | Purpose |
|---------|---------|
| `nautilus_trader==2.0.0rc8` | Core trading engine (Python 3.13+) |
| `fastapi>=0.104.0` | REST API framework |
| `uvicorn[standard]>=0.24.0` | ASGI server |
| `pydantic>=2.4.0` | Data validation |
| `httpx>=0.27.0` | Async HTTP client (Binance API) |
| `aiosqlite>=0.20.0` | Async SQLite (alerts persistence) |
| `psutil>=5.9.0` | System metrics (monitoring page) |
| `python-multipart>=0.0.6` | File upload support |

### Frontend
| Package | Purpose |
|---------|---------|
| React 18 + TypeScript | UI framework |
| Vite | Build tool |
| Tailwind CSS + Radix UI | Styling and components |
| Recharts | Charts and analytics |
| TanStack Query | Server state management |
| Wouter | Client-side routing |
| Framer Motion | Animations |

## Development

```bash
# Backend — auto-reload
cd backend
uvicorn nautilus_fastapi:app --reload --port 8000

# Frontend — dev server
cd frontend
npm run dev

# Frontend — production build
npm run build

# Frontend — lint
npm run lint
```

## Contributing

1. Fork the repository
2. Create your feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m 'feat: add my feature'`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a Pull Request

## License

MIT License — see [LICENSE](LICENSE) for details.

## Acknowledgments

- [Nautilus Trader](https://nautilustrader.io/) — Core trading engine
- [FastAPI](https://fastapi.tiangolo.com/) — Backend framework
- [Binance API](https://binance-docs.github.io/apidocs/) — Live market data

---

**Version**: 2.0.0 (NautilusTrader 2.0.0rc8) | **Status**: Pre-production, live trading not yet approved | **Last Updated**: October 2026
