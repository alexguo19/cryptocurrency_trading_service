# server.py
# FastAPI 服务：
# - /webhook/tradingview 接收 TradingView BUY/SELL
# - /state 返回本地状态（不加密，方便面板；如需可加密）
# - /control/* 管理操作（必须 Header: X-ADMIN-SECRET）

import time
import yaml
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

from trader import TradeEngine, normalize_tv_symbol


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def require_admin(x_admin_secret: str | None, cfg_now: dict):
    expected = cfg_now.get("admin", {}).get("secret", "")
    if not expected:
        raise HTTPException(status_code=500, detail="admin secret not configured")
    if not x_admin_secret or x_admin_secret != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


app = FastAPI(title="OKX USDT Perpetual Neural Momentum Bot")

cfg = load_config()
engine = TradeEngine(cfg)


class TVSignal(BaseModel):
    secret: str
    symbol: str          # e.g. OKX:BTCUSDT.P
    action: str          # BUY / SELL
    time: str | None = None
    timeframe: str | None = None
    price: str | None = None
    bar_time: str | None = None


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@app.get("/state")
def state():
    # 读配置热加载，便于面板显示最新配置摘要
    cfg_now = load_config()
    engine.reload_config(cfg_now)
    return engine.get_state()


@app.post("/webhook/tradingview")
def webhook_tradingview(payload: TVSignal):
    cfg_now = load_config()
    engine.reload_config(cfg_now)

    # Webhook secret 校验，防伪造信号
    if payload.secret != cfg_now["webhook"]["secret"]:
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    action = payload.action.upper().strip()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="action must be BUY/SELL")

    symbol = normalize_tv_symbol(payload.symbol, cfg_now["trade"]["symbols"])
    if symbol not in cfg_now["trade"]["symbols"]:
        raise HTTPException(status_code=400, detail=f"symbol not allowed: {symbol}")

    res = engine.on_signal(
        symbol=symbol,
        action=action,
        bar_time=payload.bar_time,
        timeframe=payload.timeframe,
        raw=payload.model_dump(),
    )
    return {"ok": True, "result": res}


# -------------------------
# Control APIs (admin protected)
# -------------------------
class PauseReq(BaseModel):
    paused: bool
    reason: str | None = None


@app.post("/control/pause")
def control_pause(
    req: PauseReq,
    x_admin_secret: str | None = Header(default=None, alias="X-ADMIN-SECRET"),
):
    cfg_now = load_config()
    engine.reload_config(cfg_now)
    require_admin(x_admin_secret, cfg_now)
    return engine.set_paused(req.paused, req.reason or "")


class CloseOnlyReq(BaseModel):
    close_only: bool


@app.post("/control/close_only")
def control_close_only(
    req: CloseOnlyReq,
    x_admin_secret: str | None = Header(default=None, alias="X-ADMIN-SECRET"),
):
    cfg_now = load_config()
    engine.reload_config(cfg_now)
    require_admin(x_admin_secret, cfg_now)
    return engine.set_close_only(req.close_only)


class CloseReq(BaseModel):
    symbol: str


@app.post("/control/emergency_close")
def control_emergency_close(
    req: CloseReq,
    x_admin_secret: str | None = Header(default=None, alias="X-ADMIN-SECRET"),
):
    cfg_now = load_config()
    engine.reload_config(cfg_now)
    require_admin(x_admin_secret, cfg_now)

    if req.symbol not in cfg_now["trade"]["symbols"]:
        raise HTTPException(status_code=400, detail="symbol not allowed")

    return engine.emergency_close(req.symbol)


@app.post("/control/emergency_close_all")
def control_emergency_close_all(
    x_admin_secret: str | None = Header(default=None, alias="X-ADMIN-SECRET"),
):
    cfg_now = load_config()
    engine.reload_config(cfg_now)
    require_admin(x_admin_secret, cfg_now)
    return engine.emergency_close_all()
