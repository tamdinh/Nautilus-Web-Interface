"""
Pure signal logic for the paper-trading runtime (no I/O, no Nautilus engine).

The Nautilus strategy classes in `strategies/` are written for the BacktestEngine. The paper runtime
evaluates the same three rules on closed exchange candles, so the rules are re-implemented here as
small pure functions. They follow the same entry conditions as the strategy classes:

  sma_crossover  BUY  when fast SMA crosses above slow SMA, SELL when it crosses below
  rsi            BUY  when RSI rises through `oversold_level`, SELL when it falls through `overbought_level`
  macd           BUY  when the MACD line crosses above its signal line, SELL when it crosses below

Indicator warm-up differs slightly from Nautilus' own indicators (Wilder RSI, EMA seeded with the first
value), so paper results are close to, but not identical to, a backtest of the same strategy.
"""

from typing import Any, Dict, List, Optional, Sequence

BUY = "BUY"
SELL = "SELL"


def _sma(values: Sequence[float], period: int, end: int) -> Optional[float]:
    """SMA of values[end-period+1 .. end] (inclusive end index)."""
    if end + 1 < period or period < 1:
        return None
    window = values[end - period + 1:end + 1]
    return sum(window) / period


def _ema_series(values: Sequence[float], period: int) -> List[float]:
    alpha = 2.0 / (period + 1)
    out: List[float] = []
    for v in values:
        out.append(v if not out else alpha * v + (1 - alpha) * out[-1])
    return out


def rsi_series(closes: Sequence[float], period: int) -> List[Optional[float]]:
    """Wilder RSI; None until `period` price changes are available."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def min_bars(strategy_type: str, cfg: Dict[str, Any]) -> int:
    """Closed candles needed before the strategy can produce a signal."""
    if strategy_type == "sma_crossover":
        return int(cfg.get("slow_period", 20)) + 1
    if strategy_type == "rsi":
        return int(cfg.get("rsi_period", 14)) + 2
    if strategy_type == "macd":
        return int(cfg.get("slow_period", 26)) + int(cfg.get("signal_period", 9)) + 1
    raise ValueError(f"unsupported strategy type: {strategy_type}")


def signal_for(strategy_type: str, closes: Sequence[float], cfg: Dict[str, Any]) -> Optional[str]:
    """Signal on the latest closed candle (compares it with the one before). None = no signal."""
    if len(closes) < min_bars(strategy_type, cfg):
        return None
    last = len(closes) - 1

    if strategy_type == "sma_crossover":
        fast, slow = int(cfg.get("fast_period", 10)), int(cfg.get("slow_period", 20))
        f0, s0 = _sma(closes, fast, last - 1), _sma(closes, slow, last - 1)
        f1, s1 = _sma(closes, fast, last), _sma(closes, slow, last)
        if None in (f0, s0, f1, s1):
            return None
        if f0 <= s0 and f1 > s1:
            return BUY
        if f0 >= s0 and f1 < s1:
            return SELL
        return None

    if strategy_type == "rsi":
        oversold = float(cfg.get("oversold_level", 30.0))
        overbought = float(cfg.get("overbought_level", 70.0))
        series = rsi_series(closes, int(cfg.get("rsi_period", 14)))
        prev, cur = series[last - 1], series[last]
        if prev is None or cur is None:
            return None
        if prev < oversold <= cur:
            return BUY
        if prev > overbought >= cur:
            return SELL
        return None

    if strategy_type == "macd":
        fast, slow = int(cfg.get("fast_period", 12)), int(cfg.get("slow_period", 26))
        sig = int(cfg.get("signal_period", 9))
        macd = [f - s for f, s in zip(_ema_series(closes, fast), _ema_series(closes, slow))]
        signal = _ema_series(macd, sig)
        if macd[last - 1] <= signal[last - 1] and macd[last] > signal[last]:
            return BUY
        if macd[last - 1] >= signal[last - 1] and macd[last] < signal[last]:
            return SELL
        return None

    raise ValueError(f"unsupported strategy type: {strategy_type}")
