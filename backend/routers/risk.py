from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import database
from auth_jwt import require_admin
from state import nautilus_system

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/limits")
async def get_risk_limits():
    return await database.get_risk_limits()


class RiskLimitsUpdate(BaseModel):
    """Validated limits. Unknown keys are ignored; values must be finite, non-negative numbers."""
    model_config = ConfigDict(extra="ignore")

    max_position_size: Optional[float] = Field(None, ge=0, le=1e12, allow_inf_nan=False)
    max_daily_loss: Optional[float] = Field(None, ge=0, le=1e12, allow_inf_nan=False)
    max_drawdown_pct: Optional[float] = Field(None, ge=0, le=100, allow_inf_nan=False)
    max_leverage: Optional[float] = Field(None, ge=0, le=1000, allow_inf_nan=False)
    max_orders_per_day: Optional[int] = Field(None, ge=0, le=1_000_000)
    max_open_exposure: Optional[float] = Field(None, ge=0, le=1e12, allow_inf_nan=False)
    totp_code: Optional[str] = Field(None, max_length=8)   # step-up code, required while trading mode is live


@router.post("/limits")
async def update_risk_limits(body: RiskLimitsUpdate, _admin: dict = Depends(require_admin)):
    updates = body.model_dump(exclude_none=True, exclude={"totp_code"})
    if not updates:
        raise HTTPException(status_code=422, detail="No risk limits provided")
    import trading_control, step_up
    if (await trading_control.get_state())["mode"] == "live":
        await step_up.verify(_admin["sub"], body.totp_code)   # lifting limits while live moves real money
    limits = await database.update_risk_limits(updates)
    await database.log_action(action="risk_limits_updated", user_id=_admin.get("sub", ""),
                              resource="risk", details=str(updates))
    return {"success": True, "limits": limits}


@router.get("/metrics")
async def get_risk_metrics():
    total_pnl = 0.0
    total_trades = 0
    max_drawdown = 0.0
    sharpe_ratio = 0.0

    for results in nautilus_system.backtest_results.values():
        total_pnl += results.get("total_pnl", 0.0)
        total_trades += results.get("total_trades", 0)
        if results.get("max_drawdown", 0.0) > max_drawdown:
            max_drawdown = results["max_drawdown"]
        if results.get("sharpe_ratio", 0.0) > sharpe_ratio:
            sharpe_ratio = results["sharpe_ratio"]

    # Calculate total_exposure from open DB positions (qty * entry_price)
    open_positions = await database.list_db_positions(open_only=True)
    total_exposure = sum(
        float(p.get("quantity", 0)) * float(p.get("entry_price") or 0)
        for p in open_positions
    )

    # Simplified 95% VaR estimate
    var_95 = total_exposure * (max_drawdown / 100.0) * 1.65 if total_exposure > 0 else 0.0

    # Daily risk metrics (Sprint 3)
    daily_realized_loss = await database.get_daily_realized_loss()
    orders_today = await database.count_orders_today()

    # Limit utilization
    limits = await database.get_risk_limits()
    max_pos = float(limits.get("max_position_size", 0))
    max_daily_loss = float(limits.get("max_daily_loss", 0))
    position_size_utilization = (total_exposure / max_pos) if max_pos > 0 else 0.0
    daily_loss_utilization = (abs(daily_realized_loss) / max_daily_loss) if max_daily_loss > 0 else 0.0

    return {
        "total_exposure": round(total_exposure, 2),
        "var_95": round(var_95, 2),
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "open_positions": len(open_positions),
        # Sprint 3 additions
        "daily_realized_loss": round(daily_realized_loss, 2),
        "orders_today": orders_today,
        "position_size_utilization": round(position_size_utilization, 4),
        "daily_loss_utilization": round(daily_loss_utilization, 4),
    }
