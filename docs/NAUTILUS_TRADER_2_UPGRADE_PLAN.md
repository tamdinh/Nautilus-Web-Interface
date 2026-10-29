# Kế hoạch nâng cấp hệ thống: NautilusTrader 2.0.0rc8 và chuẩn production

## Mục tiêu

1. Chạy backend trên `nautilus_trader==2.0.0rc8` (Python 3.13), giữ ổn định backtest, strategies, live adapters.
2. Nâng cấp toàn hệ thống (Trader panel, Admin panel, backend, hạ tầng Docker trên Dokploy) lên chuẩn production trước khi giao dịch thật.

> **Trạng thái (2026-10-29):** migration kỹ thuật sang RC8 đã chạy được ở môi trường dev: backend, Trader panel và Admin panel khởi động; 203 test backend pass. Hệ thống **chưa đạt chuẩn production và chưa được phép giao dịch thật**: audit toàn hệ thống (mục "Kết quả audit") tìm thấy các điểm chặn về bảo mật, vòng đời lệnh và vận hành. RC8 yêu cầu Python `>=3.13,<3.15`.

## Quyết định đã chốt

| # | Chủ đề | Quyết định |
|---|---|---|
| 1 | Phí backtest | Phí thực tế (`MakerTakerFeeModel`, mặc định maker 0.01% / taker 0.02%, chỉnh bằng `NAUTILUS_MAKER_FEE` / `NAUTILUS_TAKER_FEE`). PnL lệch baseline v1 là dự kiến. |
| 2 | Binance/Bybit REST | Giữ lớp REST `httpx` ký HMAC (`backend/exchange_clients.py`). Không dùng `TradingNode` trong đợt này (xem "Ghi chú adapter"). |
| 3 | Module không dùng | Xóa `nautilus_api.py`, `nautilus_instance.py`, `nautilus_trader_api.py`, và `nautilus_integration.py` (chỉ `nautilus_trader_api.py` import nó). |
| 4 | Demo backtest | QuoteTick để DataEngine tự tổng hợp bar. **Đã làm.** |
| 5 | Live | Chạy live trên RC8 sau khi đạt **tất cả** cổng ở Phase A–D và tiêu chí staging bên dưới. |
| 6 | Môi trường | Docker trên Dokploy, `uv` + lockfile. `deploy_to_vps.sh` và hướng dẫn VPS thủ công là tài liệu cũ. |
| 7 | Catalog | Đường dẫn qua `NAUTILUS_CATALOG_PATH`, mount volume. **Chưa thực sự hoạt động:** `state.py` truyền đường dẫn cứng nên biến môi trường bị bỏ qua. |
| 8 | Report | Cần xuất report: thêm `pandas`. |

Các quyết định đề xuất từ audit (chờ bạn chốt, xem cuối tài liệu): gộp Admin API vào backend chính, token lưu cookie HttpOnly, live đầu tiên chỉ spot trên một sàn, giữ SQLite với đúng 1 replica.

### Ghi chú adapter (quyết định 2)

NautilusTrader v2 hỗ trợ Binance/Bybit ở dạng **`TradingNode` + factory/config**, không còn các lớp HTTP Python cũ. `live_trading.py` chỉ làm REST đơn giản nên dùng `httpx`. Đổi sang `TradingNode` (reconciliation, order state, risk, vòng đời process do Nautilus quản lý) là dự án riêng. Lưu ý: nhiều điểm chặn ở Phase C (idempotency, reconcile, cancel) chính là những thứ `TradingNode` đã làm sẵn; nếu chi phí Phase C vượt dự tính thì cân nhắc chuyển sang `TradingNode` thay vì tự viết tiếp.

## Tiến độ migration kỹ thuật (đã làm)

- [x] Pin `nautilus_trader==2.0.0rc8`; Dockerfile/CI/README sang Python 3.13.
- [x] `nautilus_core.py`: import v2, `LoggerConfig(stdout_level=...)`, `fee_model`, `portfolio.account()`, `query_quote_ticks`, demo bằng QuoteTick, `dispose()` an toàn.
- [x] `strategies/*.py`: config `@dataclass`, `is_net_flat`. (MACD chưa được chạy qua engine.)
- [x] `live_trading.py` viết lại bằng `exchange_clients.py` (Binance Spot, Bybit v5); 11 test MockTransport.
- [x] Fixture autouse chặn mạng thật trong test (`BINANCE_BASE_URL`/`BYBIT_BASE_URL` trỏ cổng đóng).
- [x] Sửa CORS (CORS middleware phải thêm sau cùng); sửa Components Management (bulk `"all"`, `/api/components` đọc trạng thái đã lưu).
- [ ] Còn sót: `nautilus_integration.py:42` vẫn `LoggerConfig(log_level=...)` (sẽ xóa cùng module); `pytest-timeout` chưa có trong `requirements-dev.txt`; `pandas` chưa thêm; chưa có lockfile; CI chưa build Docker; chưa xóa module không dùng; `docs/DEPLOYMENT_GUIDE.md`, `docs/github-workflow-template.yml`, `deploy_to_vps.sh` còn Python 3.11.

## Kết quả audit toàn hệ thống (2026-10-29)

Audit đọc code theo 8 mảng; các điểm dưới đã được kiểm chứng lại bằng grep/đọc code. Một số con số (npm audit 11 lỗ hổng, 1 lỗi `tsc`, đo RSS) lấy từ audit, chưa chạy lại.

### Chặn live (blocker)

| # | Khu vực | Vấn đề | Bằng chứng |
|---|---|---|---|
| 1 | Admin API (đã sửa, xem Phase A) | `admin_db_api` (cổng 8001) không xác thực khi `ADMIN_API_KEY` rỗng; frontend không gửi key; endpoint lộ `password_hash`/`api_key`, tạo user admin | `admin_db_api.py:27,267`; compose publish 8001 |
| 2 | Secrets | Compose mặc định `SECRET_KEY` dev, `ADMIN_PASSWORD=admin`, `ENVIRONMENT=development`; guard chỉ chặn khi `production` | `docker-compose.yml` |
| 3 | Khóa mã hóa | `ENCRYPTION_KEY` không có trong compose/`.env.example`; Fernet sinh khóa mới mỗi lần restart nên không giải mã được API key đã lưu | grep không có |
| 4 | Order routing | Có adapter là mọi lệnh đi sàn, không có cờ paper/live; sau restart lệnh non-`.SIM` rơi về paper nhưng UI báo đã đặt | `orders.py:55,65` |
| 5 | Idempotency | Không có client order id; gửi sàn trước, ghi DB sau; `exchange_order_id` không được ghi | `exchange_clients.py`, `live_trading.py` |
| 6 | Vòng đời lệnh | `process_order_update` chỉ test gọi; lệnh live kẹt `PENDING`; không reconcile khi khởi động | grep |
| 7 | Risk | Risk bỏ qua lệnh MARKET (không có giá); leverage do client gửi; daily-loss dựa trên `status='filled'` mà live không bao giờ đặt; limit 0/thiếu = không giới hạn; không lock | `risk_engine.py` |
| 8 | Phân quyền | Order/cancel chỉ cần `get_current_user`; frontend không có role guard (`nautilus_role` không ai đọc); một số GET không yêu cầu auth | `orders.py:42,99` |
| 9 | Đóng vị thế | `close_position` nuốt lỗi sàn rồi vẫn đánh dấu closed; "position" là số dư spot ánh xạ thành `<ASSET>USDT`; không audit | `routers/positions.py` |
| 10 | Kill switch | Không có; disconnect không hủy lệnh mở | |
| 11 | Hủy lệnh | `cancel_order` nuốt lỗi rồi ghi CANCELLED; timeout/5xx coi như từ chối | `live_trading.py` |
| 12 | Frontend | `DatabaseManagementPage` gọi SQL/backup/restore tới host bên thứ ba cứng (`manusvm.computer`), không auth | dòng 52 |
| 13 | Frontend/Docker | 3 service đọc `VITE_API_URL` trong khi `config.ts` đọc `VITE_NAUTILUS_API_URL` nên bundle Docker gọi `localhost`; Dockerfile dùng `node:18` (EOL, Vite 7 cần ≥ 20.19) | `services/*.ts:6` |

### Cao (nên sửa trước live)

JWT trong localStorage và URL WebSocket, refresh không thu hồi; rate limit theo IP proxy và dict không dọn; SQLite không WAL/busy_timeout, ~50 chỗ mở connection riêng; backup cùng volume, không off-host, `/clean` xóa lệnh FILLED > 30 ngày; catalog không có volume; không lockfile, base image trôi; `/health` luôn "healthy" (không kiểm tra DB/engine), logging không cấu hình; cảnh báo Telegram không retry; E2E thiếu auth, CI ghi đè `package.json`, lint `|| true`, không chạy `tsc`; test backup/clean chạy trên DB thật (`database_ops._DB_PATH` bind lúc import, dòng 15) và để lại file backup rác; FeaturesPage/AdaptersPage giả; không đồng bộ giờ/xử lý 429/validate lot-size, số lượng dạng float, `binance_futures` dùng REST spot; README ghi "Production Ready" và không có disclaimer rủi ro.

### Trung bình và thấp

`npm audit` (express, axios, zod, nanoid, lodash), dependency thừa, lệch pnpm/npm; 2FA không bắt buộc, TOTP lưu plaintext; `/backups` không cần admin, `/docs` công khai; `.dockerignore` thiếu `.venv*` (~761 MB); schema không có version; 27 trang import tĩnh (một chunk ~857 kB); 7 báo cáo "PRODUCTION READY" mâu thuẫn nhau; Cloudflare Pages và Dokploy cùng tồn tại.

## Nguyên tắc thực hiện

1. Version cố định bằng `==` và lockfile có hash; nâng RC chỉ qua PR.
2. Làm theo thứ tự phase (đóng bề mặt tấn công, rồi làm hệ thống kiểm chứng được, rồi sửa đường lệnh, rồi mới nâng UI/dep).
3. Mỗi thay đổi có test; test không bao giờ chạm sàn thật hay DB thật.
4. Fail-closed: thiếu secret, thiếu limit, mất kết nối sàn thì từ chối, không fallback im lặng.
5. Một replica, một uvicorn worker (trạng thái nằm trong tiến trình và SQLite); ghi thành ràng buộc triển khai.
6. `BacktestEngine` gắn với thread tạo ra nó: tạo, chạy, `dispose()` trong cùng một thread.
7. Không bật live chỉ vì ứng dụng khởi động thành công.

## Lộ trình

### Phase 0 — Điểm rollback và baseline (làm ngay, rất nhỏ)

- [x] Khởi tạo git và commit baseline (`b13eb6f`, đã push lên `tamdinh/Nautilus-Web-Interface`). Nên tạo thêm tag cho điểm trước-migration nếu có image/commit cũ trên production.
- [x] `docs/requirements.v1.lock.txt` từ `.venv-nt1` (1.231.0) đã tạo (2026-10-29); còn: sao lưu `*.db`, catalog, `.env` của production trước khi deploy.
- [x] Dọn file rác trong `backend/data` (backup do test tạo); test không còn ghi vào thư mục dữ liệu thật.

### Phase A — Khóa bề mặt tấn công (BLOCKING) — phần lớn đã làm (2026-10-29)

Findings #1, #2, #3, #8 (RBAC backend), #12, #13.

- [x] Admin API gộp vào backend chính (`/api/admin/*`, `require_admin` JWT); bỏ service và cổng 8001; không trả `password_hash`/`api_key`; role user admin API được validate; `components/sync` gọi trực tiếp, không HTTP tới chính nó. 6 test mới (`test_admin_api.py`).
- [x] Fail-closed khi khởi động: `ENVIRONMENT` mặc định `production`; thiếu/yếu `SECRET_KEY` (< 32), `ADMIN_PASSWORD` (< 12 hoặc giá trị yếu) hoặc `ENCRYPTION_KEY` thì backend không chạy. Chỉ `ENVIRONMENT=development` rõ ràng mới hạ xuống cảnh báo. `docker-compose.yml` dùng `${VAR:?}`; cổng backend chỉ bind `127.0.0.1`. Test: `test_startup_security.py`.
- [x] `ENCRYPTION_KEY`: Fernet hợp lệ, bắt buộc ngoài dev, hỗ trợ xoay khóa (`MultiFernet`, `NEW,OLD`), log cảnh báo khi giải mã lỗi; `.env.example` đã cập nhật. **Còn lại:** sửa khóa trong CI (`ci.yml`) cho hợp lệ, và quy trình sao lưu khóa tách khỏi DB trên Dokploy.
- [x] Phân quyền: `require_trader` (admin hoặc trader) cho order, cancel, close/sync vị thế, strategy, backtest; token của user bị vô hiệu hóa/xóa mất quyền ngay và role lấy từ DB (không tin role trong JWT cũ). Mọi route `/api/*` ngoài danh sách public đều qua JWT middleware. **Lưu ý:** hiện chỉ có hai role `admin`/`trader`, chưa có `viewer` chỉ-đọc.
- [x] Frontend: Admin pages dùng API client có auth; xóa `DatabaseManagementPage` (host bên thứ ba), các service không dùng, `mockApi.ts`, `.bak`, `ManusDialog`; thống nhất env (`VITE_NAUTILUS_API_URL`, `VITE_WS_URL`); `node:22`; bỏ `jsx-loc`/`express`/`pnpm`…; `npm ci` sạch, `tsc` và `build` pass; `RequireRole` cho `/admin*` (UX, backend vẫn là nơi cưỡng chế).
- [x] Rate limit: `uvicorn` chạy `proxy_headers` với `FORWARDED_ALLOW_IPS` (mặc định chỉ `127.0.0.1`; trên Dokploy phải đặt mạng của Traefik), dọn dict khi lớn, khóa tạm theo username sau 10 lần sai/15 phút.
- [x] Đã dọn: xóa `ApiConfigPage` (gọi `/api/admin/routes/*` không tồn tại); `testHelper` chỉ bật ở development (bỏ cờ URL và `manusvm.computer`); tài liệu cũ nhắc cổng 8001 đã chuyển vào `docs/archive/`. `App.tsx` logout và `LoginPage` giữ `fetch` thô có chủ đích (login/logout không được kích hoạt luồng "401 → về login" của API client).
- [ ] Còn lại của Phase A: trên Dokploy cần đặt `FORWARDED_ALLOW_IPS` đúng mạng proxy và `CORS_ORIGINS` đúng domain.

**Cổng:** quét thủ công không còn endpoint ghi/nhạy cảm nào không xác thực (route inventory: mọi GET qua JWT middleware, mọi mutation qua `require_trader`/`require_admin`); deploy thiếu env thì container từ chối chạy. Đã kiểm chứng ở dev; chưa kiểm chứng trong container Docker/Dokploy.

### Phase B — Nền tảng chất lượng (BLOCKING) — phần lớn đã làm (2026-10-29)

- [x] Cô lập test: `NAUTILUS_DB_PATH`/`ADMIN_DB_PATH` trỏ tới thư mục tạm trước khi import app; fixture autouse fail nếu test dùng thư mục dữ liệu thật; `database_ops` đọc đường dẫn lúc gọi (nguyên nhân 3 test fail cũ, nay hết); `/clean` không còn xóa lệnh FILLED (chỉ CANCELLED).
- [x] Smoke test chạy thật SMA/RSI/MACD trong `BacktestEngine` (có lệnh, có khớp, không lỗi trong callback, đóng vị thế khi stop) + demo backtest end-to-end. Test này bắt được MACD đang hỏng trên v2; đã viết lại `macd_strategy.py` và nối MACD vào `nautilus_core` (trước đây chỉ lưu dict và từ chối backtest). Còn thiếu: test xuất report (`pandas`).
- [x] SQLite: `database.connect()` áp `busy_timeout` và `synchronous=NORMAL`, WAL bật ở `init_db` (bắt buộc đi cùng `synchronous=NORMAL`: WAL với mặc định FULL làm commit chậm ~26 lần trên máy thử, suite test từ 3,5 lên 11 phút); backup dùng `sqlite3.backup()` (an toàn với WAL). **Chưa làm:** `foreign_keys=ON` (có thể làm hỏng hành vi hiện tại, cần rà schema), retry khi locked, hai service ghi chung DB không còn (đã gộp admin API).
- [x] Catalog: `NAUTILUS_CATALOG_PATH` thật sự được đọc (bỏ ghi đè trong `state.py`), mặc định `backend/data/catalog` (`/app/data/catalog` trong image, nằm trên volume).
- [x] Xóa module không dùng: `nautilus_api.py`, `nautilus_instance.py`, `nautilus_integration.py`, `nautilus_trader_api.py`, `alerts_db.py`, file giữ chỗ `nautilus_data`; dọn tham chiếu trong README, DocsPage, workflow template.
- [x] Dependency: thêm `pandas`, `pytest-timeout`; **chuyển `python-jose` sang `PyJWT`** (pip-audit báo `ecdsa`/`python-jose`); `npm audit fix`, bỏ `axios`/`nanoid` không dùng: audit prod deps frontend và `pip-audit` đều 0 lỗ hổng.
- [x] Lockfile có hash bằng `uv` (`backend/requirements.lock`, `requirements-dev.lock`; universal, đã cài thử `--require-hashes`, đủ wheel `manylinux_2_34` x86_64 và aarch64).
- [x] CI viết lại: Python 3.13/Node 22, `timeout-minutes`, `pytest --timeout`, typecheck và build blocking, `npm audit` và `pip-audit` blocking, job Docker (build + smoke test fail-closed + `/health` + header frontend), e2e API-level có auth (65/65 pass khi chạy thử cục bộ), bỏ deploy Cloudflare Pages. **Chưa chạy trên GitHub, Docker chưa build được trên máy này.**
- [x] Docker: backend cài từ lock bằng `uv`, không chứa secret, chạy đúng 1 process; frontend dùng `nginx.conf` thật (security headers, cache), non-root `nginx-unprivileged` cổng 8080 (compose đã đổi `:8080`; **Dokploy phải trỏ domain tới cổng 8080**), stage `dist` (ý tưởng chingyap1).
- [x] **Sửa lỗ hổng phát hiện thêm:** trang bật 2FA gửi secret TOTP (`otpauth://`) tới `api.qrserver.com`; nay render QR cục bộ (`qrcode.react`).
- [ ] Còn lại: pin digest cho image `nginx-unprivileged`/`python` và uv; build Docker + chạy CI thật; `foreign_keys`; test report `pandas`; chunk frontend: đã tách vendor bằng `manualChunks` (còn `React.lazy` theo route, Phase E2); dev-dependency audit (`npm audit` đầy đủ còn lỗi cần nâng major); `ENCRYPTION_KEY` trong CI đã sinh hợp lệ.

**Cổng:** CI xanh trên nhánh sạch, image build từ lockfile, `import nautilus_fastapi` và `/health` thật trong container. *Đã kiểm chứng cục bộ: toàn bộ test backend, typecheck, build frontend, e2e API; chưa kiểm chứng: Docker và CI trên GitHub.*

### Phase C — An toàn giao dịch (BLOCKING) — phần lõi đã làm (2026-10-29)

Đã cài và kiểm thử với sàn giả (381 test backend pass). **Chưa kiểm chứng với sàn thật/testnet**: đó là cổng còn lại của phase này.

- [x] **Chế độ paper/live tường minh + kill switch bền** (`trading_control.py`, `/api/trading/*`): mặc định paper và disarmed; adapter kết nối không tự biến lệnh thành thật; live không bao giờ fallback sang paper (503); mọi lần restart/restore DB đều quay về disarmed; vào live và arm cần admin + step-up TOTP và đủ điều kiện (sàn đã xác minh, giới hạn rủi ro do admin lưu); kill không cần step-up và không bị rate limit.
- [x] **Vòng đời lệnh** (`order_state.py`, `order_store.py`, `order_service.py`): lưu `PENDING_SUBMIT` trước khi gửi, client order id của ta, một lần gửi duy nhất, phân loại kết quả (accepted / rejected / not_sent / unknown), `UNKNOWN` được tra theo client id (không retry mù, 202 "đừng gửi lại"), compare-and-set, `order_events` làm nhật ký, `Idempotency-Key` bắt buộc ở chế độ live, gửi lệnh chạy trong task riêng nên client ngắt kết nối không bỏ rơi lệnh.
- [x] **Đối soát** (`reconcile_all`, vòng lặp nền, mỗi 15s): theo dõi mọi lệnh live chưa kết thúc; chỉ kết luận "chưa từng tới sàn" sau 120s và ≥3 lần không thấy; thử lại xác minh sàn offline; hủy tự động các lệnh xuất hiện muộn sau kill (`cancel_pending`).
- [x] **Client sàn** (`exchange_clients.py`): đối chiếu tài liệu chính thức; Bybit market theo base (`marketUnit=baseCoin`), tra theo `orderLinkId` (realtime rồi history); đồng bộ giờ + thử lại -1021/10002; chặn cục bộ khi bị 429/418/10006; phân loại lỗi bảo thủ (chỉ mã lỗi được liệt kê mới là "rejected").
- [x] **Kiểm tra trước khi gửi** (`order_rules.py`): bộ lọc lot/tick/notional của sàn (không làm tròn ngầm), `Decimal` xuyên suốt, chỉ cặp quote USD-like, giá tham chiếu bắt buộc cho lệnh market.
- [x] **Rủi ro fail-closed** (`risk_engine.check_live_order`): bắt buộc đủ giới hạn do admin lưu; giá trị lệnh, số lệnh/ngày, độ phơi nhiễm lệnh treo, lỗ ngày theo equity từng sàn (không đọc được equity thì từ chối); vi phạm thì disarm và thu hồi lệnh đang treo; đổi giới hạn khi đang live cần step-up.
- [x] **Đóng vị thế** bằng lệnh MARKET qua đúng đường đặt lệnh (số dư free thật từ sàn, chống bấm đúp 30s); danh sách vị thế sàn thay snapshot thay vì cộng dồn; từ chối vị thế không rõ.
- [x] **Step-up TOTP** (`step_up.py`): mã dùng một lần (lưu bền chống replay), secret TOTP mã hóa khi lưu; `2fa/setup` không còn ghi đè authenticator đang bật; khôi phục DB cần step-up và luôn kết thúc ở trạng thái disarmed.
- [x] **Frontend** tối thiểu: không tự retry POST/DELETE, gửi `Idempotency-Key` và số dạng chuỗi, hiển thị 202 là cảnh báo (không phải thành công), trạng thái lệnh mới và nút hủy cho lệnh live.
- [x] **Review đối kháng** (45 agent, 38 phát hiện đã xác nhận): các lỗi mức cao/trung bình đã sửa và có test riêng (kill switch giữa chừng, lệnh UNKNOWN sau kill, phân loại lỗi Bybit/Binance, baseline lỗ theo sàn, 2FA setup, disconnect khi còn lệnh mở, `/clean` xóa lệnh live, thiếu Idempotency-Key, v.v.).
- [ ] **Còn lại:** chạy trọn bộ kịch bản trên **testnet** (đặt, hủy, trùng, timeout, restart, vượt risk, kill) — chưa làm; kiểm chứng định dạng/lỗi thật của sàn (đặc biệt Bybit `realtime/history`); cảnh báo vận hành khi lệnh live kẹt `UNKNOWN`/không thấy trên sàn; UI kill switch/banner chế độ (Phase E1); `max_open_exposure` chưa tính lệnh market đang bay và số dư đã khớp; `foreign_keys`; adapter connect cần step-up khi đang live; exposure theo vị thế thật; futures/margin chưa hỗ trợ (chỉ spot, cặp quote USD-like); Binance `canWithdraw`/giới hạn IP của API key chưa kiểm tra.

**Cổng:** kịch bản testnet đạt và soak 7 ngày (xem tiêu chí staging).

### Phase D — Vận hành trên Dokploy (BLOCKING một phần)

- [ ] Frontend dùng đường dẫn tương đối `/api`, `/ws` qua Traefik (không nhúng URL lúc build); nginx với CSP, `nosniff`, `X-Frame-Options`; một đích deploy duy nhất (bỏ Cloudflare Pages).
- [x] `/readyz` (DB + engine, 503 khi lỗi, dùng cho healthcheck compose); logging JSON có `X-Request-ID` (`logging_config.py`); Telegram `raise_for_status`; `/docs`/`/redoc`/`/openapi.json` tắt ở production; `/backups` cần admin.
- [ ] Còn lại: audit mọi route đổi trạng thái; alert vận hành (live-node ngắt, lệnh bị từ chối, vi phạm limit, lệnh kẹt UNKNOWN) qua outbox có retry.
- [ ] Backup theo lịch ra off-host (volume backup/S3), mã hóa, retention, diễn tập restore, bao phủ cả `admin_panel.db`; `/clean` không xóa FILLED; `/backups` cần admin.
- [x] Compose: không có `version`, đã thêm `start_period`, `init`, `stop_grace_period`, log rotation (chưa chạy thật trên Docker). [ ] Đo RSS/khởi động trên RC8 để chốt `mem_limit` (512m chưa được đo).
- [ ] *(học từ chingyap1)* Frontend Docker: stage `dist` + `Dockerfile.deploy` chỉ COPY để tránh chạy esbuild dưới QEMU khi build amd64 trên runner ARM (commit `028f795`); cần tự viết bước CI `buildx --target dist --output type=local`. Thay nginx config sinh bằng `printf` trong Dockerfile (dễ hỏng, từng có bug thiếu dấu `\n\` ở một dòng, fork Timsaysso790) bằng file `nginx.conf` thật được `COPY`; cân nhắc `proxy_read_timeout`/tắt buffering cho request dài và WebSocket.
- [ ] *(cân nhắc sau)* Cảnh báo push VAPID (`push_notify.py`, `push_vapid.py`, có test): best-effort, không bao giờ raise, dedupe 300s, tự dọn subscription 404/410; nguyên tắc "push chỉ để gây chú ý, không bao giờ là quyền". Dedupe cần lưu bền nếu dùng.
- [x] Runbook Dokploy: `docs/DOKPLOY_RUNBOOK.md`; `/docs` tắt ở production. [ ] Backup off-host tự động vẫn chưa làm (chỉ ghi trong runbook).

### Phase E — Nâng cấp frontend (Trader panel và Admin panel)

- **E1 (blocking trước live):** một API client duy nhất, ném lỗi khi `success:false` (hiện nhiều mutation bỏ qua); token an toàn hơn (cookie HttpOnly + WS ticket một lần, hoặc tối thiểu CSP nghiêm); `RequireRole`; banner môi trường paper/live; `AlertDialog` thay `window.confirm`; nút kill switch; xóa trang giả (`FeaturesPage`, test adapter giả); WebSocket dùng chung một kết nối.
- **E2:** `React.lazy` theo route và `manualChunks` (hiện một chunk ~857 kB); a11y; vitest cho Orders/Risk/Auth; dọn dependency (`express`, `add`, `pnpm`, `axios` nếu không cần), chọn npm, `npm update zod nanoid`; xóa `mockApi.ts`, `.bak`, `src/const.ts`.
- **E2 (học từ fork):** *(chingyap1)* `InterlockBanner` làm mẫu cho banner kill switch/paused và UX xác nhận cho điều khiển khẩn cấp; `ControlsPage.test.tsx` (Vitest) làm mẫu test frontend. *(Timsaysso790)* `ChartView.tsx` (lightweight-charts, nến + marker vào/ra lệnh) cho trang Backtesting/Positions; `engines/metrics.py` (Sharpe/Sortino/Calmar thuần Python) cho kết quả backtest; endpoint "giới hạn dữ liệu theo ticker" để UI tự giới hạn khoảng ngày theo catalog.
- **Backtest bất đồng bộ (E2/D):** *(ý tưởng từ Timsaysso790)* `POST /run` trả `job_id`, client polling `/status` và `/result`. Không copy bản của họ: dùng dict trong tiến trình hoặc bảng SQLite `jobs` với `asyncio.Semaphore(1)`; chạy engine trong **subprocess** (BacktestEngine gắn thread, và subprocess mới hủy được khi timeout); dọn job cũ; giải phóng khóa khi crash. Bản gốc dùng file khóa `O_EXCL` không bao giờ được giải phóng sau crash/timeout và timeout không dừng thread.
- **E3 (sau khi live ổn định):** React 19, react-query 5, recharts 3, TypeScript mới; từng bước, có test.

### Phase F — Dọn dẹp và tài liệu

- [x] Disclaimer rủi ro ở README và màn đăng nhập; bỏ "Production Ready" khỏi README. [ ] DocsPage.
- [x] Báo cáo cũ đã chuyển vào `docs/archive/` kèm banner "lịch sử". [ ] `REVIEW_OVERVIEW.md` chưa kiểm lại với RC8.
- [x] `DEPLOYMENT_GUIDE.md`, `github-workflow-template.yml`, `deploy_to_vps.sh`, `CLOUDFLARE_PAGES_SETUP.md` đã archive; thay bằng `DOKPLOY_RUNBOOK.md`.
- [ ] Schema có version (`PRAGMA user_version` + migration có thứ tự), gộp hai nơi lưu alerts, index `orders(exchange_order_id)`, retention cho audit/`revoked_tokens`.

## Học hỏi từ hai fork (đã rà soát 2026-10-29)

Hai fork cùng tách từ upstream `Black101081/Nautilus-Web-Interface` (commit chung `4bd0618`, 2026-03-19). Cả hai MIT: nếu copy code phải giữ ghi công. Kết quả dựa trên việc đọc code (reviewer không chạy gì từ fork); chưa tự kiểm chứng từng claim. Chưa kéo code nào về.

- **chingyap1/Nautilus-Web-Interface:** chuyển hẳn việc thực thi lệnh ra khỏi FastAPI (`live_trading.py` chỉ còn stub; lệnh là dòng `commands`, gửi qua kênh file tới agent Nautilus ngoài). Nhiều module phụ thuộc `mcp_gateway.*`/`live.*` không có trong repo (từ monorepo riêng) nên không chạy/test được độc lập, và kiến trúc mâu thuẫn với hướng của ta (backend tự giao dịch qua `exchange_clients.py`). **Không lấy nguyên khối.** Lấy ý tưởng: interlock lease, step-up TOTP, máy trạng thái lệnh, Dockerfile `dist`, push alert (xem các mục đánh dấu "học từ" ở Phase C/D/E). Bỏ: Strategy Copilot (~4k dòng LLM), `/api/mcp/*`, `ci.yml` cũ, báo cáo "production ready". Điểm yếu cần tránh: tiền lưu float, secret TOTP plaintext, chống replay chỉ trong bộ nhớ, idempotency key suy từ nội dung lệnh. Các tuyên bố "fail-closed" chỉ đúng một phần (không giải quyết mark price, reserve, Decimal, đồng bộ giờ sàn). Không thấy secret/telemetry trên `origin/main`, chưa quét hết 23 nhánh.
- **Timsaysso790/Nautilus-Web-Interface:** đã chuyển thành sản phẩm backtest quyền chọn, **không dùng NautilusTrader** (engine pandas/numpy tự viết, cần dữ liệu ThetaData trả phí), phần lớn do AI sinh, nhiều engine trùng, không có test cho logic backtest. Dockerfile chạy `gunicorn --workers 4` — xung đột ràng buộc 1 worker của ta, không copy. Chỉ lấy: mô hình backtest bất đồng bộ (làm lại), `ChartView`, `metrics.py`, kiểm tra lỗi `printf` nginx (xem Phase D/E). Bỏ: engine options, dữ liệu ThetaData, scanner, AI tool-calling (rủi ro bảo mật cho ứng dụng giao dịch), `auth_jwt.py` (secret mặc định cứng), báo cáo `FINAL_*`.
- **Quy trình khi dùng:** đọc lại đúng file liên quan ở thời điểm làm, viết lại theo code hiện tại của ta, ghi nguồn và giấy phép trong commit nếu có đoạn copy.

## Tiêu chí ra khỏi staging (đề xuất)

Chỉ bật live khi **tất cả** đạt:

1. Phase A–D hoàn tất, E1 xong; CI xanh trên đúng image sẽ deploy.
2. Staging chạy liên tục tối thiểu 7 ngày, không restart ngoài kế hoạch, RSS ổn định, không `[ERROR]` mới.
3. Backtest các strategy hợp lý; chênh PnL so với v1 giải thích được (chủ yếu phí).
4. Testnet: đặt, hủy, truy vấn lệnh/vị thế/số dư đúng; xử lý 401/403/429/timeout, không có lệnh trùng hay mồ côi; reconcile sau restart đúng.
5. Risk limits và kill switch được chứng minh chặn/hủy lệnh.
6. Đã diễn tập backup/restore và rollback (quay về image cũ) trong staging.
7. Có người phê duyệt; ngày đầu live dùng vốn rất nhỏ, whitelist instrument, giám sát thủ công, tăng dần.

RC8 chưa phải bản final: theo dõi 2.0.0 final và các lỗi đã biết; chỉ nâng bản qua PR và lặp lại tiêu chí.

## Rủi ro và cách giảm thiểu

| Rủi ro | Giảm thiểu |
|---|---|
| Triển khai thiếu env vẫn khởi động với secret mặc định | Phase A: fail-closed, `${VAR:?required}` |
| Mất `ENCRYPTION_KEY` làm mất API key đã lưu | Sao lưu khóa tách DB, `MultiFernet` |
| Lệnh trùng/mồ côi do timeout | Client order id, truy vấn lại theo id, reconcile |
| Risk bị vượt (MARKET, race, limit 0) | Định giá mark price, lock, bắt buộc limit |
| RC8 chưa final | Pin, staging 7 ngày, theo dõi release |
| SQLite với nhiều connection/service | WAL, busy_timeout, 1 replica, gộp Admin API |
| Hồi quy do nâng frontend lớn | E3 sau live; test vitest/E2E trước |
| Báo cáo cũ gây hiểu nhầm "Production Ready" | Phase F: archive + disclaimer |

## Rollback

1. Dùng kill switch, hủy lệnh mở, tắt live.
2. Quay lại **commit và Docker image trước thay đổi** (code API v2 không chạy lại được trên v1; dependency từ `requirements.v1.lock.txt`).
3. Xác nhận `/readyz`, API và luồng vận hành quan trọng.
4. Đối soát lệnh và vị thế mở trên sàn; người quyết định rollback là người phê duyệt live.
5. Khôi phục DB từ backup off-host nếu schema/dữ liệu bị ảnh hưởng.
6. Ghi lại lỗi, log và điều kiện tái hiện.

Rollback không tự đảo ngược trạng thái lệnh trên sàn.

## Quyết định cần bạn chốt (kèm khuyến nghị)

1. **Admin API (8001):** gộp vào backend chính sau JWT `require_admin`, không publish cổng. Frontend không thể giữ admin key an toàn.
2. **Lưu token:** cookie HttpOnly + SameSite và WS ticket một lần; tối thiểu CSP nghiêm.
3. **`DatabaseManagementPage`, `FeaturesPage`:** xóa khỏi route ngay, làm lại sau nếu cần.
4. **Phạm vi live đầu tiên:** chỉ spot trên một sàn (Binance); gỡ `binance_futures` và Bybit khỏi đường live đến khi có test.
5. **Vốn ban đầu:** số nhỏ, whitelist instrument, `max_qty` cứng, 2FA bắt buộc cho admin, testnet và soak 7 ngày trước.
6. **SQLite hay Postgres:** giữ SQLite với WAL và đúng 1 replica; Postgres + Alembic chỉ khi cần scale ngang.
7. **Frontend host:** Dokploy là đích duy nhất, bỏ Cloudflare Pages.
8. **Package manager:** npm (đã có `package-lock.json`); xóa `pnpm`/`packageManager`.
9. **Nâng major (React 19, recharts 3, TS):** hoãn đến sau khi live ổn định.
10. **Tự viết tiếp REST hay chuyển `TradingNode`** cho Phase C: bắt đầu bằng REST hiện có; đánh giá lại sau khi ước lượng chi phí idempotency + reconcile.

## Ghi nhận sau hoàn tất

- Ghi version đã phát hành, kết quả CI/staging/testnet, các khác biệt API đã xử lý.
- Gỡ ghi chú tương thích cũ và tài liệu VPS thủ công.
