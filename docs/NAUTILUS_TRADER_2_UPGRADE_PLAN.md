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

- [ ] Khởi tạo git (thư mục chưa có `.git`) hoặc zip snapshot; tạo nhánh.
- [ ] `requirements.v1.lock.txt` từ `.venv-nt1` (1.231.0); sao lưu `*.db`, catalog, `.env`.
- [ ] Dọn file rác trong `backend/data` (backup do test tạo).

### Phase A — Khóa bề mặt tấn công (BLOCKING) — phần lớn đã làm (2026-10-29)

Findings #1, #2, #3, #8 (RBAC backend), #12, #13.

- [x] Admin API gộp vào backend chính (`/api/admin/*`, `require_admin` JWT); bỏ service và cổng 8001; không trả `password_hash`/`api_key`; role user admin API được validate; `components/sync` gọi trực tiếp, không HTTP tới chính nó. 6 test mới (`test_admin_api.py`).
- [x] Fail-closed khi khởi động: `ENVIRONMENT` mặc định `production`; thiếu/yếu `SECRET_KEY` (< 32), `ADMIN_PASSWORD` (< 12 hoặc giá trị yếu) hoặc `ENCRYPTION_KEY` thì backend không chạy. Chỉ `ENVIRONMENT=development` rõ ràng mới hạ xuống cảnh báo. `docker-compose.yml` dùng `${VAR:?}`; cổng backend chỉ bind `127.0.0.1`. Test: `test_startup_security.py`.
- [x] `ENCRYPTION_KEY`: Fernet hợp lệ, bắt buộc ngoài dev, hỗ trợ xoay khóa (`MultiFernet`, `NEW,OLD`), log cảnh báo khi giải mã lỗi; `.env.example` đã cập nhật. **Còn lại:** sửa khóa trong CI (`ci.yml`) cho hợp lệ, và quy trình sao lưu khóa tách khỏi DB trên Dokploy.
- [x] Phân quyền: `require_trader` (admin hoặc trader) cho order, cancel, close/sync vị thế, strategy, backtest; token của user bị vô hiệu hóa/xóa mất quyền ngay và role lấy từ DB (không tin role trong JWT cũ). Mọi route `/api/*` ngoài danh sách public đều qua JWT middleware. **Lưu ý:** hiện chỉ có hai role `admin`/`trader`, chưa có `viewer` chỉ-đọc.
- [x] Frontend: Admin pages dùng API client có auth; xóa `DatabaseManagementPage` (host bên thứ ba), các service không dùng, `mockApi.ts`, `.bak`, `ManusDialog`; thống nhất env (`VITE_NAUTILUS_API_URL`, `VITE_WS_URL`); `node:22`; bỏ `jsx-loc`/`express`/`pnpm`…; `npm ci` sạch, `tsc` và `build` pass; `RequireRole` cho `/admin*` (UX, backend vẫn là nơi cưỡng chế).
- [x] Rate limit: `uvicorn` chạy `proxy_headers` với `FORWARDED_ALLOW_IPS` (mặc định chỉ `127.0.0.1`; trên Dokploy phải đặt mạng của Traefik), dọn dict khi lớn, khóa tạm theo username sau 10 lần sai/15 phút.
- [ ] Còn lại của Phase A: `ApiConfigPage` gọi `/api/admin/routes/*` mà backend không có (trang cũ chưa bao giờ chạy đủ); `App.tsx` logout và `LoginPage` vẫn dùng `fetch` thô; `src/utils/testHelper.ts` còn nhắc `manusvm.computer`; hai đường dẫn tài liệu cũ nhắc cổng 8001 (`deploy_to_vps.sh`, `DEPLOYMENT_GUIDE.md`, `CLOUDFLARE_PAGES_SETUP.md`, các `FINAL_*_REPORT.md`); trên Dokploy cần đặt `FORWARDED_ALLOW_IPS` đúng mạng proxy và `CORS_ORIGINS` đúng domain.

**Cổng:** quét thủ công không còn endpoint ghi/nhạy cảm nào không xác thực (route inventory: mọi GET qua JWT middleware, mọi mutation qua `require_trader`/`require_admin`); deploy thiếu env thì container từ chối chạy. Đã kiểm chứng ở dev; chưa kiểm chứng trong container Docker/Dokploy.

### Phase B — Nền tảng chất lượng (BLOCKING)

- [ ] Cô lập test: `Path(database.DB_PATH)` đọc lúc gọi (sửa `database_ops.py:15`), env `NAUTILUS_DB_PATH`, fixture autouse bảo vệ DB thật.
- [ ] CI: `tsc` và lint chặn merge (bỏ `|| true`); không ghi đè `package.json`; sửa E2E có auth; thêm `timeout-minutes`, `pytest-timeout`, build Docker + `curl /health`; Node 22; `npm audit` và `pip-audit`.
- [ ] Smoke test chạy SMA/RSI/MACD trong `BacktestEngine` (assert lệnh > 0, có FILLED, log không `[ERROR]`); test demo backtest end-to-end; test report.
- [ ] Lockfile `uv` có hash; Dockerfile dùng `uv`, `--only-binary nautilus_trader` (wheel chỉ `manylinux_2_34`, không alpine); pin digest image; trivy.
- [ ] SQLite: helper `_connect()` với WAL, `busy_timeout`, `foreign_keys`; dùng cho cả `live_trading.py`.
- [ ] Catalog: đọc `NAUTILUS_CATALOG_PATH` thật (bỏ ghi đè trong `state.py`), mặc định `/app/data/catalog`, volume, kiểm tra khi khởi động.
- [ ] Thêm `pandas`; `.dockerignore` loại `.venv*/`; xóa module không dùng.

**Cổng:** CI xanh trên nhánh sạch, image build từ lockfile, `import nautilus_fastapi` và `/health` thật trong container.

### Phase C — An toàn giao dịch (BLOCKING, phần việc lớn nhất)

Thứ tự: #4, #5, #6, #11, #7, #9, #26, #10.

- [ ] Chế độ `paper`/`live` tường minh; live mà không tới được sàn thì 503, không fallback; reconnect adapter khi khởi động.
- [ ] Client order id (`newClientOrderId`/`orderLinkId`), trạng thái `PENDING_SUBMIT` ghi trước khi gửi, `Idempotency-Key`; timeout thì truy vấn theo client id.
- [ ] Vòng đời lệnh: poll hoặc user-data stream, reconcile lệnh và số dư khi khởi động; ghi `exchange_order_id`; hủy/đóng chỉ ghi trạng thái cuối khi sàn xác nhận; phân biệt từ chối 4xx với kết quả không rõ.
- [ ] Risk fail-closed: định giá MARKET theo mark price, tính lỗ từ fill thật, lock/reserve, validate bằng Pydantic, bắt buộc đặt limit trước khi live, whitelist instrument và `max_qty` cứng.
- [ ] Exchange: đồng bộ giờ (`/time`), xử lý 429, cache `exchangeInfo` và validate lot/tick/minNotional, `Decimal` thay float, định tuyến theo venue; gỡ `binance_futures` và Bybit khỏi đường live đến khi có test.
- [ ] Đóng vị thế: chỉ sau fill xác nhận, từ chối vị thế không rõ, chạy risk check, ghi audit.
- [ ] Kill switch: cờ `trading_enabled` lưu bền, hủy mọi lệnh mở, tùy chọn flatten; nút trong Admin panel.
- [ ] 2FA bắt buộc cho admin; TOTP lưu mã hóa, chống replay.

- [ ] *(học từ fork chingyap1)* **Interlock có lease tự gia hạn** làm nền cho kill switch: cờ `trading_enabled`/paused lưu bền kèm lease 30s; backend gia hạn mỗi 10s chỉ khi lease còn hạn, nên backend chết hoặc treo thì giao dịch tự dừng (fail-closed); resume dùng compare-and-set trong transaction để lệnh pause của người vận hành luôn thắng race. Tham khảo: `stores.py` (`InterlockStore.resume_if_unchanged`/`renew_if_fresh`), `_renew_interlock_loop` trong `nautilus_fastapi.py`, `tests/test_interlock_renewal.py`.
- [ ] *(học từ chingyap1)* **Step-up TOTP cho hành động nguy hiểm** (resume interlock, kill switch, thay đổi limit, kết nối adapter): mã mới mỗi lần, cửa sổ nâng quyền 5 phút, từ chối dùng lại mã. Tham khảo `step_up.py` và `tests/test_2fa.py`. **Phải làm khác bản gốc:** lưu bảng mã đã dùng bền (không chỉ trong bộ nhớ), mã hóa secret TOTP bằng Fernet của ta (bản gốc lưu plaintext), dùng `pyotp`.
- [ ] *(học từ chingyap1)* **Bảng `commands`/`orders` có máy trạng thái** PENDING_SUBMIT → SUBMITTED → ACCEPTED/FILLED/… → RECONCILIATION_REQUIRED, kèm `client_order_id`, `venue_order_id`. Tham khảo `commands.py` và `tests/test_commands*.py`, `test_command_processor_recovery.py`. **Phải làm khác:** số lượng/giá dùng `Decimal`/TEXT (bản gốc dùng REAL float); idempotency key do client gửi (`Idempotency-Key`), không suy từ instrument-side-qty-price (key đó chặn cả lệnh lặp hợp lệ); thêm quy tắc "approval/proposal đã dùng thì retry cần cái mới".
- [ ] Độ tươi heartbeat/live-node: tính từ tuổi heartbeat; lỗi lưu trữ hoặc tuổi âm thì coi là **không online**, không bao giờ coi là online (dùng cho `/readyz` và banner kill switch).

**Cổng:** bộ kịch bản testnet (đặt, hủy, trùng lặp, timeout, restart-reconcile, vượt risk, kill switch) đạt.

### Phase D — Vận hành trên Dokploy (BLOCKING một phần)

- [ ] Frontend dùng đường dẫn tương đối `/api`, `/ws` qua Traefik (không nhúng URL lúc build); nginx với CSP, `nosniff`, `X-Frame-Options`; một đích deploy duy nhất (bỏ Cloudflare Pages).
- [ ] `/readyz` kiểm tra DB và engine; logging JSON có request id; audit mọi route đổi trạng thái; alert vận hành (live-node ngắt, lệnh bị từ chối, vi phạm limit) qua outbox có retry; sửa Telegram `raise_for_status`.
- [ ] Backup theo lịch ra off-host (volume backup/S3), mã hóa, retention, diễn tập restore, bao phủ cả `admin_panel.db`; `/clean` không xóa FILLED; `/backups` cần admin.
- [ ] Compose: bỏ `version`, thêm `start_period`, `init`, grace period, log rotation; đo RSS/khởi động trên RC8 để chốt `mem_limit` (512m chưa được đo).
- [ ] *(học từ chingyap1)* Frontend Docker: stage `dist` + `Dockerfile.deploy` chỉ COPY để tránh chạy esbuild dưới QEMU khi build amd64 trên runner ARM (commit `028f795`); cần tự viết bước CI `buildx --target dist --output type=local`. Thay nginx config sinh bằng `printf` trong Dockerfile (dễ hỏng, từng có bug thiếu dấu `\n\` ở một dòng, fork Timsaysso790) bằng file `nginx.conf` thật được `COPY`; cân nhắc `proxy_read_timeout`/tắt buffering cho request dài và WebSocket.
- [ ] *(cân nhắc sau)* Cảnh báo push VAPID (`push_notify.py`, `push_vapid.py`, có test): best-effort, không bao giờ raise, dedupe 300s, tự dọn subscription 404/410; nguyên tắc "push chỉ để gây chú ý, không bao giờ là quyền". Dedupe cần lưu bền nếu dùng.
- [ ] Runbook Dokploy: env/secret, volume, domain, WebSocket, rollback. Tắt `/docs` ở production.

### Phase E — Nâng cấp frontend (Trader panel và Admin panel)

- **E1 (blocking trước live):** một API client duy nhất, ném lỗi khi `success:false` (hiện nhiều mutation bỏ qua); token an toàn hơn (cookie HttpOnly + WS ticket một lần, hoặc tối thiểu CSP nghiêm); `RequireRole`; banner môi trường paper/live; `AlertDialog` thay `window.confirm`; nút kill switch; xóa trang giả (`FeaturesPage`, test adapter giả); WebSocket dùng chung một kết nối.
- **E2:** `React.lazy` theo route và `manualChunks` (hiện một chunk ~857 kB); a11y; vitest cho Orders/Risk/Auth; dọn dependency (`express`, `add`, `pnpm`, `axios` nếu không cần), chọn npm, `npm update zod nanoid`; xóa `mockApi.ts`, `.bak`, `src/const.ts`.
- **E2 (học từ fork):** *(chingyap1)* `InterlockBanner` làm mẫu cho banner kill switch/paused và UX xác nhận cho điều khiển khẩn cấp; `ControlsPage.test.tsx` (Vitest) làm mẫu test frontend. *(Timsaysso790)* `ChartView.tsx` (lightweight-charts, nến + marker vào/ra lệnh) cho trang Backtesting/Positions; `engines/metrics.py` (Sharpe/Sortino/Calmar thuần Python) cho kết quả backtest; endpoint "giới hạn dữ liệu theo ticker" để UI tự giới hạn khoảng ngày theo catalog.
- **Backtest bất đồng bộ (E2/D):** *(ý tưởng từ Timsaysso790)* `POST /run` trả `job_id`, client polling `/status` và `/result`. Không copy bản của họ: dùng dict trong tiến trình hoặc bảng SQLite `jobs` với `asyncio.Semaphore(1)`; chạy engine trong **subprocess** (BacktestEngine gắn thread, và subprocess mới hủy được khi timeout); dọn job cũ; giải phóng khóa khi crash. Bản gốc dùng file khóa `O_EXCL` không bao giờ được giải phóng sau crash/timeout và timeout không dừng thread.
- **E3 (sau khi live ổn định):** React 19, react-query 5, recharts 3, TypeScript mới; từng bước, có test.

### Phase F — Dọn dẹp và tài liệu

- [ ] Disclaimer rủi ro ở README, màn đăng nhập, DocsPage; bỏ "Production Ready".
- [ ] Chuyển các báo cáo cũ vào `docs/archive/` kèm banner "lịch sử"; giữ `REVIEW_OVERVIEW.md` làm bảng Real/Fake đã kiểm lại với RC8.
- [ ] Thay `DEPLOYMENT_GUIDE.md`, `github-workflow-template.yml`, `deploy_to_vps.sh` bằng hướng dẫn Dokploy + uv, hoặc ghi rõ không còn hỗ trợ.
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
