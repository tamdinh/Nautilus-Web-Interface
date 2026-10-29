"""
MACD (Moving Average Convergence Divergence) Strategy — Sprint 3 (S3-02).

Entry signals:
  - BUY  when MACD line crosses above signal line (bullish crossover)
  - SELL when MACD line crosses below signal line (bearish crossover)
"""

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import MovingAverageConvergenceDivergence
from nautilus_trader.model import Bar, BarType, InstrumentId, OrderSide
from nautilus_trader.trading import Strategy


def _order_id_tag(strategy_id: str) -> str:
    tag = "".join(c for c in (strategy_id or "001") if c.isalnum())
    return tag[:12] or "001"


@dataclass(frozen=True)
class MACDStrategyConfig:
    strategy_id: str = ""
    instrument_id: str = "EUR/USD.SIM"
    bar_type: str = "EUR/USD.SIM-1-MINUTE-BID-INTERNAL"
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    trade_size: Decimal = Decimal("100000")


class MACDStrategy(Strategy):
    """MACD crossover strategy for Nautilus Trader."""

    def __init__(self, config: MACDStrategyConfig) -> None:
        super().__init__(StrategyConfig(order_id_tag=_order_id_tag(config.strategy_id)))
        # In v2 self.config is the base StrategyConfig, so keep our own settings explicitly.
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.trade_size = config.trade_size
        self.macd = MovingAverageConvergenceDivergence(
            config.fast_period,
            config.slow_period,
            config.signal_period,
        )
        self._prev_macd: float = 0.0
        self._prev_signal: float = 0.0
        self._initialized_prev: bool = False
        self.instrument = None

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.instrument_id)
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self.instrument_id}")
            self.stop()
            return
        self.register_indicator_for_bars(self.bar_type, self.macd)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        if not self.macd.initialized:
            return

        macd_val = float(self.macd.value)
        signal_val = float(self.macd.signal)

        if self._initialized_prev:
            # Bullish crossover: MACD crosses above signal
            if self._prev_macd <= self._prev_signal and macd_val > signal_val:
                self._close_shorts()
                if not self.portfolio.is_net_long(self.instrument_id):
                    self._buy()
            # Bearish crossover: MACD crosses below signal
            elif self._prev_macd >= self._prev_signal and macd_val < signal_val:
                self._close_longs()
                if not self.portfolio.is_net_short(self.instrument_id):
                    self._sell()

        self._prev_macd = macd_val
        self._prev_signal = signal_val
        self._initialized_prev = True

    def _close_longs(self) -> None:
        for pos in self.cache.positions_open(instrument_id=self.instrument_id):
            if pos.is_long:
                self.close_position(pos)

    def _close_shorts(self) -> None:
        for pos in self.cache.positions_open(instrument_id=self.instrument_id):
            if pos.is_short:
                self.close_position(pos)

    def _buy(self) -> None:
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(self.trade_size),
        )
        self.submit_order(order)

    def _sell(self) -> None:
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL,
            quantity=self.instrument.make_qty(self.trade_size),
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        self.close_all_positions(self.instrument_id)
        self.unsubscribe_bars(self.bar_type)
