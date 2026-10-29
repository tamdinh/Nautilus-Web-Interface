"""
Trading control API: paper/live mode, arm, kill switch.

* GET  /api/trading/state  — mode, armed flag and what is still missing before live can be enabled
* POST /api/trading/mode   — admin + fresh TOTP (step-up) to enter live; leaving live needs no step-up
* POST /api/trading/arm    — admin + fresh TOTP; allows live orders (a restart always disarms again)
* POST /api/trading/kill   — any trader/admin, no step-up on purpose: stopping must always be easy
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import database
import step_up
import trading_control
from auth_jwt import get_current_user, require_admin, require_trader
from risk_engine import risk_engine
from state import live_manager
from order_service import order_service

router = APIRouter(prefix="/api/trading", tags=["trading"])


class ModeRequest(BaseModel):
    mode: Literal["paper", "live"]
    totp_code: Optional[str] = Field(None, max_length=8)
    reason: str = Field("", max_length=200)


class ArmRequest(BaseModel):
    totp_code: Optional[str] = Field(None, max_length=8)
    reason: str = Field("", max_length=200)


class KillRequest(BaseModel):
    reason: str = Field("manual kill switch", max_length=200)
    cancel_open: bool = True


async def _readiness() -> dict:
    problems = list(await risk_engine.live_readiness_problems())
    venues = live_manager.ready_venues()
    if not venues:
        problems.append("no exchange adapter is connected and verified")
    return {"ready_for_live": not problems, "problems": problems, "venues": venues}


@router.get("/state")
async def get_state(_user: dict = Depends(get_current_user)):
    state = await trading_control.get_state()
    return {**state, **await _readiness(), "step_up_required": step_up.required()}


@router.post("/mode")
async def set_mode(req: ModeRequest, admin: dict = Depends(require_admin)):
    user = admin["sub"]
    if req.mode == "live":
        await step_up.verify(user, req.totp_code)
        readiness = await _readiness()
        if not readiness["ready_for_live"]:
            raise HTTPException(status_code=409, detail={
                "error": "not_ready_for_live",
                "message": "Live trading cannot be enabled yet.", "problems": readiness["problems"]})
    state = await trading_control.set_mode(req.mode, user, req.reason)
    await database.log_action(action="trading_mode", user_id=user, resource="trading",
                              details=f"mode={req.mode} armed=False reason={req.reason}")
    return {"success": True, **state}


@router.post("/arm")
async def arm(req: ArmRequest, admin: dict = Depends(require_admin)):
    user = admin["sub"]
    await step_up.verify(user, req.totp_code)
    state = await trading_control.get_state()
    if state["mode"] != "live":
        raise HTTPException(status_code=409, detail={"error": "not_live", "message": "Switch to live mode first."})
    readiness = await _readiness()
    if not readiness["ready_for_live"]:
        raise HTTPException(status_code=409, detail={
            "error": "not_ready_for_live", "message": "Live trading cannot be armed yet.",
            "problems": readiness["problems"]})
    new_state = await trading_control.arm(user, req.reason or "armed")
    await database.log_action(action="trading_arm", user_id=user, resource="trading", details=req.reason)
    return {"success": True, **new_state}


@router.get("/ops-alerts")
async def list_ops_alerts(limit: int = 100, _admin: dict = Depends(require_admin)):
    """Operational alert outbox (delivery status, attempts, last error) for admins."""
    import ops_alerts
    return {"alerts": await ops_alerts.list_outbox(max(1, min(limit, 500)))}


@router.post("/kill")
async def kill(req: KillRequest, user: dict = Depends(require_trader)):
    result = await order_service.kill(user=user["sub"], reason=req.reason, cancel_open=req.cancel_open)
    return {"success": True, **result}
