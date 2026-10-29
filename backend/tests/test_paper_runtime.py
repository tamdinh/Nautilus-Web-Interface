"""Paper runtime: pure signals, seeding, simulated fills, safety caps, API wiring."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import database  # noqa: E402
import paper_runtime  # noqa: E402
import strategy_signals as sig  # noqa: E402


# ── pure signals ─────────────────────────────────────────────────────────────

def test_sma_cross_up_and_down():
    cfg = {"fast_period": 2, "slow_period": 4}
    assert sig.signal_for("sma_crossover", [10, 10, 10, 10, 9, 13], cfg) == sig.BUY
    assert sig.signal_for("sma_crossover", [10, 10, 10, 10, 11, 7], cfg) == sig.SELL
    assert sig.signal_for("sma_crossover", [10, 10, 10, 10, 10, 10], cfg) is None


def test_not_enough_bars_gives_no_signal():
    assert sig.signal_for("sma_crossover", [1, 2, 3], {"fast_period": 2, "slow_period": 4}) is None


def test_rsi_crosses():
    cfg = {"rsi_period": 3, "oversold_level": 30, "overbought_level": 70}
    falling = [100, 98, 96, 94, 92, 90]
    series = sig.rsi_series(falling + [95], 3)
    assert series[-2] < 30 <= series[-1]
    assert sig.signal_for("rsi", falling + [95], cfg) == sig.BUY
    rising = [100, 102, 104, 106, 108, 110]
    assert sig.signal_for("rsi", rising + [105], cfg) == sig.SELL


def test_macd_cross_signals_are_buy_or_sell_on_reversal():
    cfg = {"fast_period": 3, "slow_period": 6, "signal_period": 3}
    down = [100 - i for i in range(15)]
    up = down + [down[-1] + 3 * (i + 1) for i in range(6)]
    seen = {sig.signal_for("macd", up[:n], cfg) for n in range(10, len(up) + 1)}
    assert sig.BUY in seen


# ── runtime ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "paper.db")
    asyncio.run(database.init_db())


def _strategy(**cfg):
    base = {"instrument_id": "BTCUSDT", "interval": "1m", "fast_period": 2, "slow_period": 4, "trade_size": "0.1"}
    return {"id": "S1", "type": "sma_crossover", "config": {**base, **cfg}}


def _feed(monkeypatch, closes_by_call):
    calls = iter(closes_by_call)

    async def fake(symbol, interval):
        closes = next(calls)
        return [{"t": 60_000 * (i + 1), "close": float(c)} for i, c in enumerate(closes)]
    monkeypatch.setattr(paper_runtime, "fetch_closed_candles", fake)


def test_first_run_seeds_without_trading(db, monkeypatch):
    _feed(monkeypatch, [[10, 10, 10, 10, 9, 13]])
    state = asyncio.run(paper_runtime.evaluate(_strategy()))
    assert state["trades"] == 0 and state["last_bar"] == 6 * 60_000
    assert asyncio.run(paper_runtime.list_trades("S1")) == []


def test_buy_then_sell_records_net_pnl(db, monkeypatch):
    base = [10, 10, 10, 10, 10]
    _feed(monkeypatch, [base, base + [9, 13], base + [9, 13, 12, 5]])
    strat = _strategy()
    asyncio.run(paper_runtime.evaluate(strat))                       # seed
    s1 = asyncio.run(paper_runtime.evaluate(strat))                  # BUY at 13
    assert s1["position_qty"] == pytest.approx(0.1) and s1["entry_price"] == 13
    s2 = asyncio.run(paper_runtime.evaluate(strat))                  # SELL at 5
    assert s2["position_qty"] == 0 and s2["closed_trades"] == 1
    fees = 0.1 * 13 * paper_runtime.FEE_RATE + 0.1 * 5 * paper_runtime.FEE_RATE
    assert s2["realized_pnl"] == pytest.approx((5 - 13) * 0.1 - fees)
    sides = [t["side"] for t in asyncio.run(paper_runtime.list_trades("S1"))]
    assert sorted(sides) == ["BUY", "SELL"]


def test_same_bar_is_not_evaluated_twice(db, monkeypatch):
    series = [10, 10, 10, 10, 10, 9, 13]
    _feed(monkeypatch, [series[:5], series, series])
    strat = _strategy()
    asyncio.run(paper_runtime.evaluate(strat))
    asyncio.run(paper_runtime.evaluate(strat))
    again = asyncio.run(paper_runtime.evaluate(strat))
    assert again["trades"] == 1


def test_oversized_order_is_refused_and_reported(db, monkeypatch):
    series = [10, 10, 10, 10, 10, 9, 13]
    _feed(monkeypatch, [series[:5], series])
    strat = _strategy(trade_size="100000")
    asyncio.run(paper_runtime.evaluate(strat))
    state = asyncio.run(paper_runtime.evaluate(strat))
    assert state["trades"] == 0 and "paper cap" in state["last_error"]


def test_network_error_is_recorded_not_raised(db, monkeypatch):
    async def boom(symbol, interval):
        raise RuntimeError("offline")
    monkeypatch.setattr(paper_runtime, "fetch_closed_candles", boom)
    state = asyncio.run(paper_runtime.evaluate(_strategy()))
    assert "offline" in state["last_error"]


def test_executable_only_for_exchange_symbols():
    assert paper_runtime.is_executable({"instrument_id": "BTCUSDT"})
    assert not paper_runtime.is_executable({"instrument_id": "EUR/USD.SIM"})
    assert not paper_runtime.is_executable({"instrument_id": "BTCUSDT", "interval": "7m"})


def test_runtime_never_touches_order_or_exchange_code():
    src = Path(paper_runtime.__file__).read_text(encoding="utf-8")
    for forbidden in ("order_service", "exchange_clients", "live_trading", "live_manager"):
        assert forbidden not in src


# ── API ──────────────────────────────────────────────────────────────────────

def test_api_start_btc_strategy_reports_paper_execution(client):
    r = client.post("/api/strategies", json={"name": "btc", "type": "sma_crossover", "instrument_id": "BTCUSDT",
                                             "trade_size": "0.01", "fast_period": 3, "slow_period": 9})
    sid = r.json()["strategy_id"]
    started = client.post(f"/api/strategies/{sid}/start").json()
    assert started["executes_orders"] is True and started["paper_only"] is True
    listed = [s for s in client.get("/api/strategies").json()["strategies"] if s["id"] == sid][0]
    assert listed["executes_orders"] is True and listed["config"]["interval"] == "1m"
    paper = client.get(f"/api/strategies/{sid}/paper").json()
    assert paper["executes_orders"] is True and paper["trades"] == []
    assert client.delete(f"/api/strategies/{sid}").status_code == 200
