"""
Smoke tests: run the real SMA / RSI / MACD strategies inside a NautilusTrader v2 BacktestEngine.

Strategy callbacks swallow exceptions into the engine log, so each strategy is wrapped to record
anything raised from on_bar/on_start — a strategy that is silently broken would otherwise "pass".
"""

import math
import random
from decimal import Decimal

import pytest

from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.config import LoggerConfig
from nautilus_trader.execution import FixedFeeModel
from nautilus_trader.model import (
    AccountType,
    Currency,
    Money,
    OmsType,
    Price,
    Quantity,
    QuoteTick,
    TraderId,
    Venue,
)
from nautilus_trader.testkit.providers import TestInstrumentProvider

from strategies.macd_strategy import MACDStrategy, MACDStrategyConfig
from strategies.rsi_strategy import RSIStrategy, RSIStrategyConfig
from strategies.sma_crossover import SMACrossoverConfig, SMACrossoverStrategy

USD = Currency.from_str("USD")
BAR_TYPE_SUFFIX = "1-MINUTE-BID-INTERNAL"


def _recording(strategy_cls):
    """Subclass that records exceptions raised in callbacks instead of letting the engine log them."""

    class Recording(strategy_cls):
        errors: list

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.errors = []

        def on_start(self):
            try:
                super().on_start()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(exc)
                raise

        def on_bar(self, bar):
            try:
                super().on_bar(bar)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(exc)
                raise

    Recording.__name__ = f"Recording{strategy_cls.__name__}"
    return Recording


def _run(make_strategy, minutes: int = 600):
    """Run a strategy over a sine-plus-noise EUR/USD series; return (engine, strategy)."""
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("SMOKE-001"),
            logging=LoggerConfig(stdout_level=LogLevel.OFF),
        )
    )
    venue = Venue("SIM")
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(1_000_000, USD)],
        fee_model=FixedFeeModel(Money(0, USD)),
    )
    instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD", venue=venue)
    engine.add_instrument(instrument)

    rng = random.Random(7)
    start = 1_609_459_200_000_000_000
    ticks = []
    for i in range(minutes):
        px = 1.10 + 0.01 * math.sin(i / 25) + rng.gauss(0, 0.0004)
        ts = start + i * 60_000_000_000
        for k, p in enumerate((px, px + 0.0002, px - 0.0002, px)):
            t = ts + k * 15_000_000_000
            ticks.append(QuoteTick(
                instrument_id=instrument.id,
                bid_price=Price.from_str(f"{p:.5f}"),
                ask_price=Price.from_str(f"{p + 0.00002:.5f}"),
                bid_size=Quantity.from_int(1_000_000),
                ask_size=Quantity.from_int(1_000_000),
                ts_event=t,
                ts_init=t,
            ))
    engine.add_data(ticks)

    strategy = make_strategy(str(instrument.id), f"{instrument.id}-{BAR_TYPE_SUFFIX}")
    engine.add_strategy(strategy)
    engine.run()
    return engine, strategy


def _summary(engine):
    orders = list(engine.cache.orders())
    positions = list(engine.cache.positions())
    return orders, positions


@pytest.fixture
def engines():
    created = []
    yield created
    for e in created:
        e.dispose()


def _assert_traded(engines, make_strategy):
    engine, strategy = _run(make_strategy)
    engines.append(engine)
    assert strategy.errors == [], f"strategy callback raised: {strategy.errors!r}"
    orders, positions = _summary(engine)
    assert len(orders) > 0, "strategy never submitted an order"
    assert any(o.is_closed for o in orders), "no order reached a terminal state"
    assert any(float(o.filled_qty) > 0 for o in orders), "no order was filled"
    assert len(positions) > 0
    return orders, positions


def test_sma_crossover_trades_in_engine(engines):
    cls = _recording(SMACrossoverStrategy)
    _assert_traded(engines, lambda inst, bar: cls(SMACrossoverConfig(
        instrument_id=inst, bar_type=bar, fast_period=5, slow_period=20,
        trade_size=Decimal("10000"), strategy_id="smoke-sma")))


def test_rsi_trades_in_engine(engines):
    cls = _recording(RSIStrategy)
    _assert_traded(engines, lambda inst, bar: cls(RSIStrategyConfig(
        instrument_id=inst, bar_type=bar, rsi_period=14, oversold_level=40.0,
        overbought_level=60.0, trade_size=Decimal("10000"), strategy_id="smoke-rsi")))


def test_macd_trades_in_engine(engines):
    cls = _recording(MACDStrategy)
    _assert_traded(engines, lambda inst, bar: cls(MACDStrategyConfig(
        instrument_id=inst, bar_type=bar, fast_period=12, slow_period=26,
        signal_period=9, trade_size=Decimal("10000"), strategy_id="smoke-macd")))


def test_strategies_stop_cleanly_and_close_positions(engines):
    cls = _recording(SMACrossoverStrategy)
    engine, strategy = _run(lambda inst, bar: cls(SMACrossoverConfig(
        instrument_id=inst, bar_type=bar, fast_period=5, slow_period=20,
        trade_size=Decimal("10000"), strategy_id="smoke-stop")))
    engines.append(engine)
    assert strategy.errors == []
    # engine.run() stops strategies at the end of data; on_stop closes everything
    assert not list(engine.cache.positions_open())


def test_demo_backtest_endpoint_logic_has_fills():
    """The demo backtest used by the UI must produce filled orders on the v2 engine."""
    from nautilus_core import NautilusTradingSystem

    result = NautilusTradingSystem().run_demo_backtest(num_bars=300)
    assert result["success"], result.get("message")
    body = result["result"]
    assert body["total_orders"] > 0
    assert any(o["status"].upper().endswith("FILLED") for o in body["orders"])
