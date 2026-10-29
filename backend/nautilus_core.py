"""
Nautilus Trader Core Integration
Real integration with Nautilus Trader engine - NOT MOCK DATA
Uses low-level BacktestEngine API for direct control
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.config import LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    Currency,
    InstrumentId,
    Money,
    OmsType,
    TraderId,
    Venue,
)
from nautilus_trader.persistence import ParquetDataCatalog

USD = Currency.from_str("USD")


def _fee_model() -> MakerTakerFeeModel:
    """Realistic maker/taker commission (fractions of notional), configurable via env."""
    return MakerTakerFeeModel(
        Decimal(os.getenv("NAUTILUS_MAKER_FEE", "0.0001")),
        Decimal(os.getenv("NAUTILUS_TAKER_FEE", "0.0002")),
    )

# Import our real strategies
from strategies.sma_crossover import SMACrossoverStrategy, SMACrossoverConfig
from strategies.rsi_strategy import RSIStrategy, RSIStrategyConfig


class NautilusTradingSystem:
    """
    Real Nautilus Trader integration for web interface.
    This is NOT a mock - it uses actual Nautilus BacktestEngine.
    """
    
    def __init__(self, catalog_path: str = None):
        """
        Initialize the trading system.
        
        Args:
            catalog_path: Path to Nautilus data catalog
        """
        self.trader_id = TraderId("TRADER-001")
        self.catalog_path = catalog_path or os.getenv(
            "NAUTILUS_CATALOG_PATH", "/home/ubuntu/nautilus_data/catalog"
        )
        
        # Engine
        self.engine: Optional[BacktestEngine] = None
        
        # Catalog for data
        self.catalog: Optional[ParquetDataCatalog] = None
        
        # State tracking
        self.strategies: Dict[str, Any] = {}
        self.backtest_results: Dict[str, Any] = {}
        self.is_initialized = False
        self.instruments = []
        
    def initialize(self) -> Dict[str, Any]:
        """
        Initialize the Nautilus engine and load data catalog.
        """
        try:
            # Load data catalog
            if os.path.exists(self.catalog_path):
                self.catalog = ParquetDataCatalog(self.catalog_path)
                self.instruments = self.catalog.instruments()
                
                logger.info("Loaded catalog from %s", self.catalog_path)
                logger.info("Available instruments: %d", len(self.instruments))
                for instrument in self.instruments:
                    logger.info("  - %s", instrument.id)
            else:
                logger.warning("Catalog path not found: %s", self.catalog_path)
                return {
                    "success": False,
                    "message": f"Data catalog not found at {self.catalog_path}"
                }
            
            self.is_initialized = True
            
            return {
                "success": True,
                "message": "Nautilus Trading System initialized",
                "trader_id": str(self.trader_id),
                "catalog_path": self.catalog_path,
                "instruments_count": len(self.instruments)
            }
            
        except Exception as e:
            import traceback
            return {
                "success": False,
                "message": f"Failed to initialize: {str(e)}",
                "error": str(e),
                "trace": traceback.format_exc()
            }
    
    def get_system_info(self) -> Dict[str, Any]:
        """Get system information."""
        return {
            "is_initialized": self.is_initialized,
            "trader_id": str(self.trader_id),
            "catalog_path": self.catalog_path,
            "strategies_count": len(self.strategies),
            "backtests_count": len(self.backtest_results)
        }
    
    def start_strategy(self, strategy_id: str) -> bool:
        """Mark strategy as running. Called by the strategies router on start."""
        if strategy_id in self.strategies:
            self.strategies[strategy_id]["status"] = "running"
            return True
        return False

    def stop_strategy(self, strategy_id: str) -> bool:
        """Mark strategy as stopped. Called by the strategies router on stop."""
        if strategy_id in self.strategies:
            self.strategies[strategy_id]["status"] = "stopped"
            return True
        return False

    def create_strategy(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a real Nautilus strategy.
        
        Args:
            config: Strategy configuration
        """
        try:
            strategy_id = config.get("id", f"strategy_{len(self.strategies) + 1}")
            strategy_type = config.get("type", "sma_crossover")
            
            if strategy_type == "sma_crossover":
                strategy_config = SMACrossoverConfig(
                    strategy_id=strategy_id,
                    instrument_id=config.get("instrument_id", "EUR/USD.SIM"),
                    bar_type=config.get("bar_type", "EUR/USD.SIM-1-MINUTE-BID-INTERNAL"),
                    fast_period=config.get("fast_period", 10),
                    slow_period=config.get("slow_period", 20),
                    trade_size=Decimal(str(config.get("trade_size", "100000"))),
                )
                name = config.get("name", "SMA Crossover")
            elif strategy_type == "rsi":
                strategy_config = RSIStrategyConfig(
                    strategy_id=strategy_id,
                    instrument_id=config.get("instrument_id", "EUR/USD.SIM"),
                    bar_type=config.get("bar_type", "EUR/USD.SIM-1-MINUTE-BID-INTERNAL"),
                    rsi_period=config.get("rsi_period", 14),
                    oversold_level=config.get("oversold_level", 30.0),
                    overbought_level=config.get("overbought_level", 70.0),
                    trade_size=Decimal(str(config.get("trade_size", "100000"))),
                )
                name = config.get("name", "RSI Mean-Reversion")
            elif strategy_type == "macd":
                fast = int(config.get("fast_period", 12))
                slow = int(config.get("slow_period", 26))
                signal = int(config.get("signal_period", 9))
                name = config.get("name", "MACD Crossover")
                # MACD doesn't map to a Nautilus engine config — stored as plain dict
                self.strategies[strategy_id] = {
                    "id": strategy_id,
                    "name": name,
                    "type": strategy_type,
                    "config": {
                        "fast_period": fast,
                        "slow_period": slow,
                        "signal_period": signal,
                        "instrument_id": config.get("instrument_id", "EUR/USD.SIM"),
                        "bar_type": config.get("bar_type", "EUR/USD.SIM-1-MINUTE-BID-INTERNAL"),
                        "trade_size": str(config.get("trade_size", "100000")),
                    },
                    "status": "created",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                return {
                    "success": True,
                    "message": f"Strategy {strategy_id} created",
                    "strategy_id": strategy_id,
                    "type": strategy_type,
                }
            else:
                return {
                    "success": False,
                    "message": f"Unknown strategy type: {strategy_type}",
                }

            # Store strategy info
            self.strategies[strategy_id] = {
                "id": strategy_id,
                "name": name,
                "type": strategy_type,
                "config": strategy_config,
                "status": "created",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "success": True,
                "message": f"Strategy {strategy_id} created",
                "strategy_id": strategy_id,
                "type": strategy_type,
            }
                
        except Exception as e:
            import traceback
            return {
                "success": False,
                "message": f"Failed to create strategy: {str(e)}",
                "error": str(e),
                "trace": traceback.format_exc()
            }
    
    def run_backtest(
        self,
        strategy_id: str,
        start_date: str = "2020-01-01",
        end_date: str = "2020-01-31",
        starting_balance: float = 100000.0
    ) -> Dict[str, Any]:
        """
        Run a real backtest using Nautilus BacktestEngine (low-level API).
        
        Args:
            strategy_id: ID of the strategy to backtest
            start_date: Start date for backtest (YYYY-MM-DD)
            end_date: End date for backtest (YYYY-MM-DD)
            starting_balance: Starting account balance
        """
        engine = None
        try:
            if not self.is_initialized:
                return {
                    "success": False,
                    "message": "System not initialized. Call initialize() first."
                }
            
            if strategy_id not in self.strategies:
                return {
                    "success": False,
                    "message": f"Strategy {strategy_id} not found"
                }
            
            strategy_info = self.strategies[strategy_id]
            strategy_config = strategy_info["config"]
            strategy_type = strategy_info["type"]

            # MACD has no real BacktestEngine implementation — return early
            if strategy_type == "macd":
                return {
                    "success": False,
                    "message": "MACD backtest is not yet supported in the engine",
                }

            # Resolve instrument_id safely for both config objects and plain dicts
            if isinstance(strategy_config, dict):
                cfg_instrument_id = strategy_config.get("instrument_id", "EUR/USD.SIM")
            else:
                cfg_instrument_id = strategy_config.instrument_id

            logger.info("Starting backtest for %s", strategy_id)
            logger.info("Period: %s to %s", start_date, end_date)
            logger.info("Starting balance: $%.2f", starting_balance)

            # Create BacktestEngine with configuration
            engine_config = BacktestEngineConfig(
                trader_id=self.trader_id,
                logging=LoggerConfig(stdout_level=LogLevel.INFO),
            )

            engine = BacktestEngine(config=engine_config)

            # Add venue (simulated exchange)
            VENUE = Venue("SIM")
            engine.add_venue(
                venue=VENUE,
                oms_type=OmsType.HEDGING,
                account_type=AccountType.MARGIN,
                base_currency=USD,
                fee_model=_fee_model(),
                starting_balances=[Money(starting_balance, USD)],
            )

            # Get instrument from catalog
            instrument = None
            for instr in self.instruments:
                if str(instr.id) == cfg_instrument_id:
                    instrument = instr
                    break

            if not instrument:
                return {
                    "success": False,
                    "message": f"Instrument {cfg_instrument_id} not found in catalog"
                }
            
            # Add instrument
            engine.add_instrument(instrument)
            
            # Load quote tick data from catalog
            logger.info("Loading quote tick data for %s...", instrument.id)
            start_ns = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp() * 1e9)
            end_ns = int((datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp() + 86400) * 1e9) - 1
            quote_ticks = self.catalog.query_quote_ticks(
                identifiers=[str(instrument.id)],
                start=start_ns,
                end=end_ns,
            )
            
            if not quote_ticks:
                return {
                    "success": False,
                    "message": f"No quote tick data found for {instrument.id} between {start_date} and {end_date}"
                }
            
            logger.info("Loaded %d quote ticks", len(quote_ticks))
            
            # Add data to engine
            engine.add_data(quote_ticks)
            
            # Create and add strategy (dispatch by type)
            if strategy_info["type"] == "rsi":
                strategy = RSIStrategy(config=strategy_config)
            else:
                strategy = SMACrossoverStrategy(config=strategy_config)
            engine.add_strategy(strategy=strategy)
            
            # Run backtest
            logger.info("Running backtest...")
            engine.run()
            
            logger.info("Backtest completed")
            
            # Extract results from engine
            # Get account
            account = engine.portfolio.account(VENUE)
            
            # Get all orders
            orders = list(engine.cache.orders())
            
            # Get all positions
            positions = list(engine.cache.positions())
            
            # Calculate statistics
            total_pnl = 0.0
            if account:
                total_pnl = float(account.balance_total(USD).as_double()) - starting_balance
            
            winning_trades = 0
            losing_trades = 0
            total_trades = len([p for p in positions if p.is_closed])
            
            for position in positions:
                if position.is_closed:
                    pnl = float(position.realized_pnl.as_double()) if position.realized_pnl else 0.0
                    if pnl > 0:
                        winning_trades += 1
                    elif pnl < 0:
                        losing_trades += 1
            
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
            
            equity_curve = self._build_equity_curve(positions, starting_balance, start_date)
            max_drawdown = self._calc_max_drawdown(equity_curve)
            sharpe_ratio = self._calc_sharpe(equity_curve)

            # Store results
            backtest_result = {
                "strategy_id": strategy_id,
                "start_date": start_date,
                "end_date": end_date,
                "starting_balance": starting_balance,
                "ending_balance": float(account.balance_total(USD).as_double()) if account else starting_balance,
                "total_pnl": total_pnl,
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "win_rate": win_rate,
                "max_drawdown": max_drawdown,
                "sharpe_ratio": sharpe_ratio,
                "total_orders": len(orders),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "equity_curve": equity_curve,
                "orders": [self._order_to_dict(o) for o in orders[:200]],
                "positions": [self._position_to_dict(p) for p in positions[:200]],
            }
            
            self.backtest_results[strategy_id] = backtest_result
            self.strategies[strategy_id]["status"] = "backtested"
            self.strategies[strategy_id]["last_backtest"] = datetime.now(timezone.utc).isoformat()
            
            logger.info("Total PnL: $%.2f", total_pnl)
            logger.info("Total Trades: %d", total_trades)
            logger.info("Win Rate: %.2f%%", win_rate)
            
            return {
                "success": True,
                "message": "Backtest completed successfully",
                "result": backtest_result
            }

        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            logger.error("Backtest failed: %s", e)
            logger.debug(error_trace)

            return {
                "success": False,
                "message": f"Backtest failed: {str(e)}",
                "error": str(e),
                "trace": error_trace
            }
        finally:
            # Always dispose engine to release resources, regardless of success/failure
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass
    
    def get_backtest_results(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """Get backtest results for a strategy."""
        return self.backtest_results.get(strategy_id)
    
    def get_all_strategies(self) -> List[Dict[str, Any]]:
        """Get all strategies."""
        return list(self.strategies.values())
    
    def get_strategy(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific strategy."""
        return self.strategies.get(strategy_id)
    
    def _build_equity_curve(self, positions, starting_balance: float, start_date: str) -> List[Dict]:
        """Build equity curve time-series from closed positions."""
        equity_curve = [{"time": f"{start_date}T00:00:00", "equity": starting_balance}]
        running = starting_balance
        closed = [p for p in positions if p.is_closed]
        for pos in sorted(closed, key=lambda p: p.ts_closed or 0):
            pnl = float(pos.realized_pnl.as_double()) if pos.realized_pnl else 0.0
            running += pnl
            ts_secs = (pos.ts_closed or 0) / 1e9
            try:
                time_str = datetime.fromtimestamp(ts_secs, tz=timezone.utc).isoformat()
            except Exception:
                time_str = datetime.now(timezone.utc).isoformat()
            equity_curve.append({"time": time_str, "equity": round(running, 2)})
        return equity_curve

    def _calc_max_drawdown(self, equity_curve: List[Dict]) -> float:
        """Calculate maximum drawdown percentage from equity curve."""
        max_dd = 0.0
        peak = 0.0
        for point in equity_curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        return round(max_dd, 2)

    def _calc_sharpe(self, equity_curve: List[Dict]) -> float:
        """Approximate annualised Sharpe ratio from equity curve returns."""
        import statistics
        if len(equity_curve) < 3:
            return 0.0
        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]["equity"]
            curr = equity_curve[i]["equity"]
            if prev > 0:
                returns.append((curr - prev) / prev)
        if len(returns) < 2:
            return 0.0
        try:
            mean_r = statistics.mean(returns)
            std_r = statistics.stdev(returns)
            return round((mean_r / std_r) * (252 ** 0.5), 3) if std_r > 0 else 0.0
        except Exception:
            return 0.0

    def run_demo_backtest(
        self,
        fast_period: int = 10,
        slow_period: int = 20,
        starting_balance: float = 100000.0,
        num_bars: int = 500,
    ) -> Dict[str, Any]:
        """
        Run a demo backtest using synthetic price data.
        Works without a real data catalog – uses TestInstrumentProvider.
        """
        import random
        try:
            from nautilus_trader.testkit.providers import TestInstrumentProvider
            from nautilus_trader.model import BarType, Price, Quantity, QuoteTick

            engine_config = BacktestEngineConfig(
                trader_id=TraderId("DEMO-001"),
                logging=LoggerConfig(stdout_level=LogLevel.WARNING),
            )
            engine = BacktestEngine(config=engine_config)

            VENUE = Venue("SIM")
            engine.add_venue(
                venue=VENUE,
                oms_type=OmsType.HEDGING,
                account_type=AccountType.MARGIN,
                base_currency=USD,
                fee_model=_fee_model(),
                starting_balances=[Money(starting_balance, USD)],
            )

            # Use a well-known test instrument
            try:
                instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD", venue=VENUE)
            except TypeError:
                instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")

            engine.add_instrument(instrument)

            # Generate synthetic quote ticks (GBM, slight upward drift); the DataEngine
            # aggregates them into 1-minute bars for the strategy.
            bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-BID-INTERNAL")
            ticks = []
            current_price = 1.10000
            start_ts = 1_609_459_200_000_000_000  # 2021-01-01 00:00:00 UTC (nanoseconds)
            bar_ns = 60_000_000_000  # 1 minute in nanoseconds
            spread = 0.00002

            def _tick(px: float, ts: int) -> QuoteTick:
                return QuoteTick(
                    instrument_id=instrument.id,
                    bid_price=Price.from_str(f"{px:.5f}"),
                    ask_price=Price.from_str(f"{px + spread:.5f}"),
                    bid_size=Quantity.from_int(1_000_000),
                    ask_size=Quantity.from_int(1_000_000),
                    ts_event=ts,
                    ts_init=ts,
                )

            random.seed(42)
            for i in range(num_bars):
                change_pct = random.gauss(0.00003, 0.00030)
                open_p = current_price
                close_p = max(0.5, current_price * (1 + change_pct))
                high_p = max(open_p, close_p) * (1 + abs(random.gauss(0, 0.00008)))
                low_p = min(open_p, close_p) * (1 - abs(random.gauss(0, 0.00008)))
                base = start_ts + i * bar_ns
                for k, px in enumerate((open_p, high_p, low_p, close_p)):
                    ticks.append(_tick(px, base + k * 15_000_000_000))
                current_price = close_p

            engine.add_data(ticks)

            strategy_config = SMACrossoverConfig(
                strategy_id="demo_sma",
                instrument_id=str(instrument.id),
                bar_type=str(bar_type),
                fast_period=fast_period,
                slow_period=slow_period,
                trade_size=Decimal("100000"),
            )
            strategy = SMACrossoverStrategy(config=strategy_config)
            engine.add_strategy(strategy=strategy)

            logger.info("Running demo backtest (%d bars, fast=%d, slow=%d)...", num_bars, fast_period, slow_period)
            engine.run()
            logger.info("Demo backtest complete")

            account = engine.portfolio.account(VENUE)
            orders = list(engine.cache.orders())
            positions = list(engine.cache.positions())

            final_balance = float(account.balance_total(USD).as_double()) if account else starting_balance
            total_pnl = final_balance - starting_balance

            closed_pos = [p for p in positions if p.is_closed]
            winning = sum(1 for p in closed_pos if p.realized_pnl and float(p.realized_pnl.as_double()) > 0)
            losing = sum(1 for p in closed_pos if p.realized_pnl and float(p.realized_pnl.as_double()) < 0)
            total_trades = len(closed_pos)
            win_rate = (winning / total_trades * 100) if total_trades > 0 else 0.0

            equity_curve = self._build_equity_curve(positions, starting_balance, "2021-01-01")
            max_drawdown = self._calc_max_drawdown(equity_curve)
            sharpe_ratio = self._calc_sharpe(equity_curve)

            result = {
                "strategy_id": "demo",
                "strategy_name": f"SMA Crossover (fast={fast_period}, slow={slow_period})",
                "start_date": "2021-01-01",
                "end_date": "2021-01-08",
                "starting_balance": starting_balance,
                "ending_balance": round(final_balance, 2),
                "total_pnl": round(total_pnl, 2),
                "total_trades": total_trades,
                "winning_trades": winning,
                "losing_trades": losing,
                "win_rate": round(win_rate, 2),
                "max_drawdown": max_drawdown,
                "sharpe_ratio": sharpe_ratio,
                "total_orders": len(orders),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "equity_curve": equity_curve,
                "orders": [self._order_to_dict(o) for o in orders[:200]],
                "positions": [self._position_to_dict(p) for p in positions[:200]],
                "fast_period": fast_period,
                "slow_period": slow_period,
                "num_bars": num_bars,
            }

            engine.dispose()
            return {"success": True, "result": result}

        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            logger.error("Demo backtest failed: %s", e)
            logger.debug(error_trace)
            return {
                "success": False,
                "message": str(e),
                "error": str(e),
                "trace": error_trace,
            }

    def _order_to_dict(self, order) -> Dict[str, Any]:
        """Convert Nautilus Order to dictionary."""
        return {
            "id": str(order.client_order_id),
            "instrument_id": str(order.instrument_id),
            "side": str(order.side),
            "type": str(order.order_type),
            "quantity": float(order.quantity),
            "status": str(order.status),
            "filled_qty": float(order.filled_qty),
            "avg_px": float(order.avg_px) if order.avg_px else None,
            "ts_init": order.ts_init,
        }
    
    def _position_to_dict(self, position) -> Dict[str, Any]:
        """Convert Nautilus Position to dictionary."""
        return {
            "id": str(position.id),
            "instrument_id": str(position.instrument_id),
            "side": str(position.side),
            "quantity": float(position.quantity),
            "avg_px_open": float(position.avg_px_open),
            "avg_px_close": float(position.avg_px_close) if position.avg_px_close else None,
            "realized_pnl": float(position.realized_pnl.as_double()) if position.realized_pnl else 0.0,
            "unrealized_pnl": 0.0,
            "is_open": position.is_open,
            "is_closed": position.is_closed,
            "ts_opened": position.ts_opened,
            "ts_closed": position.ts_closed if position.is_closed else None,
        }


# Global instance
nautilus_system = NautilusTradingSystem()

