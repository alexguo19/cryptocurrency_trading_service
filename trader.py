# trader.py
# OKX USDT 永续（swap）交易引擎（生产增强版）：
# - TradingView BUY/SELL 信号
# - 反向信号：平仓 + 反手
# - 双阶段移动止损（3% -> 盈利>=1% 收紧到0.1%）
# - 后台更新止损
# - 管理控制：暂停/恢复、只平不反手、紧急平仓
# - ✅增强：启动/定时从交易所拉取真实仓位并对账（reconcile）
# - ✅增强：下单后轮询订单状态/成交均价，更新 entry/qty，并在平仓确认成交后置为 FLAT

import threading
import time
from typing import Any, Optional

import ccxt
from tenacity import retry, stop_after_attempt, wait_fixed

from risk import calc_profit_pct, trail_stop_price


def normalize_tv_symbol(tv_symbol: str, allowed_symbols: list[str]) -> str:
    """
    TradingView symbol -> OKX ccxt symbol
    常见输入：
      "OKX:BTCUSDT.P" / "BTCUSDT.P" / "BTCUSDT"
    输出：
      "BTC/USDT:USDT"
    """
    raw = tv_symbol.split(":")[-1].upper()
    raw = raw.replace(".P", "")
    if raw.endswith("USDT"):
        base = raw[:-4]
        guess = f"{base}/USDT:USDT"
        if guess in allowed_symbols:
            return guess

    for sym in allowed_symbols:
        base = sym.split("/")[0].upper()
        if raw.startswith(base):
            return sym

    return raw


class TradeEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg

        self._lock_map: dict[str, threading.Lock] = {}
        self._state: dict[str, dict] = {}

        self._runtime = {
            "paused": False,
            "pause_reason": "",
            "close_only": False,
            "last_signal": {},
            "last_action": {},
            "last_reconcile": None,
        }

        self.exchange = self._init_exchange(cfg)

        # ✅ 启动时对账（防止重启后状态丢失导致重复开仓）
        self.reconcile_positions(reason="startup")

        self._stop_flag = False
        self._bg_trail = threading.Thread(target=self._trailing_loop, daemon=True)
        self._bg_trail.start()

        # ✅ 可选：后台定时对账（建议 5~15 分钟一次）
        self._bg_reconcile = threading.Thread(target=self._reconcile_loop, daemon=True)
        self._bg_reconcile.start()

    # -------------------------
    # Config hot reload
    # -------------------------
    def reload_config(self, cfg: dict):
        self.cfg = cfg

    # -------------------------
    # Public state
    # -------------------------
    def get_state(self) -> dict:
        return {
            "runtime": self._runtime,
            "positions": self._state,
            "config_summary": {
                "symbols": self.cfg["trade"]["symbols"],
                "leverage": self.cfg["trade"]["leverage"],
                "margin_mode": self.cfg["trade"]["margin_mode"],
                "poll_interval_sec": self.cfg["app"]["poll_interval_sec"],
                "reconcile_interval_sec": int(self.cfg.get("app", {}).get("reconcile_interval_sec", 600)),
            },
        }

    # -------------------------
    # Controls
    # -------------------------
    def set_paused(self, paused: bool, reason: str = ""):
        self._runtime["paused"] = bool(paused)
        self._runtime["pause_reason"] = reason or ""
        return {"paused": self._runtime["paused"], "reason": self._runtime["pause_reason"]}

    def set_close_only(self, close_only: bool):
        self._runtime["close_only"] = bool(close_only)
        return {"close_only": self._runtime["close_only"]}

    def emergency_close(self, symbol: str):
        res = self._close(symbol)
        self._runtime["last_action"][symbol] = {"action": "EMERGENCY_CLOSE", "ts": int(time.time()), "detail": res}
        return res

    def emergency_close_all(self):
        results = {}
        for symbol in self.cfg["trade"]["symbols"]:
            try:
                results[symbol] = self.emergency_close(symbol)
            except Exception as e:
                results[symbol] = {"error": str(e)}
        return results

    # -------------------------
    # Locks
    # -------------------------
    def _get_lock(self, symbol: str) -> threading.Lock:
        if symbol not in self._lock_map:
            self._lock_map[symbol] = threading.Lock()
        return self._lock_map[symbol]

    # -------------------------
    # Exchange init
    # -------------------------
    def _init_exchange(self, cfg: dict):
        ex = ccxt.okx({
            "apiKey": cfg["exchange"]["api_key"],
            "secret": cfg["exchange"]["api_secret"],
            "password": cfg["exchange"]["password"],
            "enableRateLimit": cfg["exchange"].get("enable_rate_limit", True),
        })
        ex.options = ex.options or {}
        ex.options["defaultType"] = cfg["exchange"].get("market_type", "swap")
        ex.options["settle"] = cfg["exchange"].get("settle", "USDT")

        ex.load_markets()
        return ex

    # -------------------------
    # Market data
    # -------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    def _fetch_last_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    # -------------------------
    # Qty
    # -------------------------
    def _qty_for_symbol(self, symbol: str) -> float:
        tcfg = self.cfg["trade"]
        mode = tcfg["qty_mode"]
        if mode == "base":
            return float(tcfg["qty_base"][symbol])
        if mode == "quote":
            quote_amt = float(tcfg["qty_quote"][symbol])
            price = self._fetch_last_price(symbol)
            return quote_amt / price
        raise ValueError("qty_mode must be base or quote")

    # -------------------------
    # Local position state
    # -------------------------
    def _position(self, symbol: str) -> dict:
        return self._state.get(symbol, {"side": "FLAT"})

    def _update_state_open(self, symbol: str, side: str, entry_price: float, qty: float, bar_time: str | None):
        tcfg = self.cfg["trailing_stop"]
        init_pct = float(tcfg["initial_trail_pct"])
        self._state[symbol] = {
            "side": side,  # LONG / SHORT
            "entry_price": entry_price,
            "qty": qty,
            "last_bar_time": bar_time,
            "trail_pct": init_pct,
            "trail_price": trail_stop_price(side, entry_price, init_pct),
            "updated_at": int(time.time()),
        }

    def _update_state_flat(self, symbol: str):
        self._state[symbol] = {"side": "FLAT", "updated_at": int(time.time())}

    def _maybe_dedup(self, symbol: str, bar_time: str | None) -> bool:
        if not self.cfg["runtime"].get("dedup_same_bar", True):
            return False
        if not bar_time:
            return False
        st = self._state.get(symbol)
        if not st:
            return False
        return st.get("last_bar_time") == bar_time

    # -------------------------
    # OKX params
    # -------------------------
    def _set_leverage_and_margin(self, symbol: str):
        lev = int(self.cfg["trade"]["leverage"])
        margin_mode = self.cfg["trade"]["margin_mode"]
        try:
            self.exchange.set_margin_mode(margin_mode, symbol, params={"tdMode": margin_mode})
        except Exception:
            pass
        try:
            self.exchange.set_leverage(lev, symbol)
        except Exception:
            pass

    def _okx_order_params(self, is_close: bool, pos_side: str) -> dict:
        margin_mode = self.cfg["trade"]["margin_mode"]
        params = {
            "tdMode": margin_mode,
            "posSide": "long" if pos_side == "LONG" else "short",
        }
        if is_close:
            params["reduceOnly"] = True
        return params

    # -------------------------
    # ✅ Order fill confirmation
    # -------------------------
    def _safe_num(self, v: Any, default: float = 0.0) -> float:
        try:
            if v is None:
                return default
            return float(v)
        except Exception:
            return default

    def _parse_order_fill(self, order: dict) -> tuple[float, float, str]:
        """
        返回：(filled_qty, avg_price, status)
        """
        status = str(order.get("status") or "").lower()
        filled = self._safe_num(order.get("filled"), 0.0)
        avg = self._safe_num(order.get("average"), 0.0)

        # 有些返回把均价放在 info 里
        info = order.get("info") or {}
        if avg <= 0:
            for k in ("avgPx", "avgpx", "fillPx", "fillpx"):
                if k in info:
                    avg = self._safe_num(info.get(k), 0.0)
                    if avg > 0:
                        break

        # filled 兜底（不同字段）
        if filled <= 0:
            for k in ("accFillSz", "fillSz", "fillSz", "sz"):
                if k in info:
                    filled = self._safe_num(info.get(k), 0.0)
                    if filled > 0:
                        break

        return filled, avg, status

    def _wait_order_filled(self, symbol: str, order_id: str, timeout_sec: int = 12, interval_sec: float = 0.5) -> dict:
        """
        轮询 fetch_order，直到 filled 或超时。
        超时不会抛异常，返回最后一次订单对象并标记 timeout。
        """
        deadline = time.time() + timeout_sec
        last = None
        while time.time() < deadline:
            try:
                o = self.exchange.fetch_order(order_id, symbol)
                last = o
                filled, avg, status = self._parse_order_fill(o)
                if status in ("closed", "filled"):
                    o["__fill__"] = {"filled": filled, "average": avg, "status": status, "timeout": False}
                    return o
                # 有些所 status 可能是 open，但 filled 已经有值且接近下单量
                if filled > 0 and status not in ("canceled", "rejected"):
                    # 继续等一小会儿，争取拿到 closed
                    pass
            except Exception:
                pass
            time.sleep(interval_sec)

        if last is None:
            return {"id": order_id, "symbol": symbol, "__fill__": {"filled": 0.0, "average": 0.0, "status": "unknown", "timeout": True}}
        filled, avg, status = self._parse_order_fill(last)
        last["__fill__"] = {"filled": filled, "average": avg, "status": status, "timeout": True}
        return last

    # -------------------------
    # ✅ Reconcile with exchange positions
    # -------------------------
    def _extract_okx_position(self, p: dict) -> Optional[dict]:
        """
        解析 ccxt fetch_positions 返回的一条 position，转成统一结构：
        {symbol, side(LONG/SHORT), qty, entry_price}
        """
        symbol = p.get("symbol")
        if not symbol:
            return None

        contracts = p.get("contracts")
        size = self._safe_num(contracts, 0.0)
        if size == 0:
            # 有些返回 positionAmt/pos/size 在 info 中
            info = p.get("info") or {}
            for k in ("pos", "position", "sz", "positionAmt"):
                if k in info:
                    size = self._safe_num(info.get(k), 0.0)
                    break

        # OKX 的 side/posSide 通常在 info.posSide
        info = p.get("info") or {}
        pos_side = (p.get("side") or info.get("posSide") or "").lower()

        # entry price
        entry = self._safe_num(p.get("entryPrice"), 0.0)
        if entry <= 0:
            for k in ("avgPx", "avgPx", "avgpx"):
                if k in info:
                    entry = self._safe_num(info.get(k), 0.0)
                    if entry > 0:
                        break

        # size sign: OKX 常用 posSide 区分方向；size 一般是正数
        if size == 0:
            return None

        if pos_side in ("long", "net"):
            side = "LONG"
        elif pos_side == "short":
            side = "SHORT"
        else:
            # 尝试从 p['side'] 推断
            s2 = (p.get("side") or "").lower()
            if s2 == "long":
                side = "LONG"
            elif s2 == "short":
                side = "SHORT"
            else:
                # 无法判断方向：跳过（保守）
                return None

        return {"symbol": symbol, "side": side, "qty": abs(size), "entry_price": entry}

    def reconcile_positions(self, reason: str = "manual") -> dict:
        """
        拉取交易所真实持仓，覆盖本地状态。
        best-effort：接口不可用/权限不足时不会崩溃。
        """
        symbols = self.cfg["trade"]["symbols"]
        result = {"ok": True, "reason": reason, "ts": int(time.time()), "updated": {}, "errors": []}

        try:
            # 尽量只拉我们关心的 symbols
            positions = []
            try:
                positions = self.exchange.fetch_positions(symbols)
            except Exception:
                # 部分 ccxt 版本不支持按 symbols 过滤
                positions = self.exchange.fetch_positions()

            # 先标记全部为 FLAT（再用真实持仓覆盖）
            for sym in symbols:
                self._update_state_flat(sym)

            for p in positions or []:
                parsed = self._extract_okx_position(p)
                if not parsed:
                    continue
                sym = parsed["symbol"]
                if sym not in symbols:
                    continue

                side = parsed["side"]
                qty = parsed["qty"]
                entry = parsed["entry_price"] or self._fetch_last_price(sym)

                # 用交易所 entry 作为本地 entry
                self._update_state_open(sym, side, entry, qty, bar_time=None)
                result["updated"][sym] = {"side": side, "qty": qty, "entry_price": entry}

            self._runtime["last_reconcile"] = result["ts"]
            self._runtime["last_action"]["__reconcile__"] = {"action": "RECONCILE", "ts": result["ts"], "detail": result}
            return result

        except Exception as e:
            result["ok"] = False
            result["errors"].append(str(e))
            self._runtime["last_action"]["__reconcile__"] = {"action": "RECONCILE_FAILED", "ts": int(time.time()), "detail": result}
            return result

    def _reconcile_loop(self):
        interval = int(self.cfg.get("app", {}).get("reconcile_interval_sec", 600))
        # 默认 10 分钟对账一次
        while not self._stop_flag:
            try:
                self.reconcile_positions(reason="periodic")
            except Exception:
                pass
            time.sleep(max(60, interval))

    # -------------------------
    # Signal entry
    # -------------------------
    def on_signal(self, symbol: str, action: str, bar_time: str | None, timeframe: str | None, raw: dict):
        lock = self._get_lock(symbol) if self.cfg["runtime"].get("lock_per_symbol", True) else threading.Lock()
        with lock:
            self._runtime["last_signal"][symbol] = {"action": action, "ts": int(time.time()), "raw": raw}

            if self._runtime.get("paused", False):
                return {"ignored": True, "reason": "paused", "pause_reason": self._runtime.get("pause_reason", "")}

            # ✅ 可选：每次信号前轻量对账（更安全但更慢/更耗频）
            # 这里默认不每次都对账，靠 periodic + manual + startup
            if self._maybe_dedup(symbol, bar_time):
                return {"ignored": True, "reason": "dedup_same_bar"}

            pos = self._position(symbol)
            side = pos.get("side", "FLAT")

            target = "LONG" if action == "BUY" else "SHORT"

            # close-only 模式
            if self._runtime.get("close_only", False):
                if side == "FLAT":
                    return {"ignored": True, "reason": "close_only_flat"}
                if side != target:
                    res = self._close(symbol)
                    self._runtime["last_action"][symbol] = {"action": "CLOSE_ONLY_CLOSE", "ts": int(time.time()), "detail": res}
                    return {"close_only_closed": True, "detail": res}
                return {"ignored": True, "reason": "close_only_same_direction"}

            # 开仓
            if side == "FLAT":
                res = self._open(symbol, target, bar_time)
                self._runtime["last_action"][symbol] = {"action": f"OPEN_{target}", "ts": int(time.time()), "detail": res}
                return res

            # 同向忽略
            if side == target and self.cfg["strategy"].get("ignore_same_direction_signal", True):
                self._state[symbol]["last_bar_time"] = bar_time
                return {"ignored": True, "reason": "same_direction"}

            # 反向信号：平仓 + 反手
            if self.cfg["strategy"].get("reverse_on_opposite_signal", True):
                close_res = self._close(symbol)
                time.sleep(0.3)
                open_res = self._open(symbol, target, bar_time)
                res = {"closed": close_res, "opened": open_res}
                self._runtime["last_action"][symbol] = {"action": f"REVERSE_TO_{target}", "ts": int(time.time()), "detail": res}
                return res

            # 只平仓
            close_res = self._close(symbol)
            self._runtime["last_action"][symbol] = {"action": "CLOSE_ONLY_BY_STRATEGY", "ts": int(time.time()), "detail": close_res}
            return {"closed_only": True, "detail": close_res}

    # -------------------------
    # Open / Close with fill confirmation
    # -------------------------
    def _open(self, symbol: str, target: str, bar_time: str | None):
        self._set_leverage_and_margin(symbol)
        qty_req = self._qty_for_symbol(symbol)
        order_type = self.cfg["trade"]["order_type"]

        if target == "LONG":
            params = self._okx_order_params(is_close=False, pos_side="LONG")
            order = self.exchange.create_order(symbol, order_type, "buy", qty_req, params=params)
        else:
            params = self._okx_order_params(is_close=False, pos_side="SHORT")
            order = self.exchange.create_order(symbol, order_type, "sell", qty_req, params=params)

        order_id = order.get("id") or (order.get("info") or {}).get("ordId")
        filled_qty = 0.0
        avg_price = 0.0

        if order_id:
            fetched = self._wait_order_filled(symbol, order_id, timeout_sec=12, interval_sec=0.5)
            fill = fetched.get("__fill__", {})
            filled_qty = self._safe_num(fill.get("filled"), 0.0)
            avg_price = self._safe_num(fill.get("average"), 0.0)
            order = fetched  # 用更完整的订单信息覆盖

        # 兜底：若拿不到均价，则用 last
        if avg_price <= 0:
            avg_price = self._fetch_last_price(symbol)

        # 兜底：若拿不到 filled，用请求 qty（保守）
        final_qty = filled_qty if filled_qty > 0 else qty_req

        self._update_state_open(symbol, target, avg_price, final_qty, bar_time)
        return {
            "opened": target,
            "qty_requested": qty_req,
            "qty_filled": final_qty,
            "avg_price": avg_price,
            "order": order,
        }

    def _close(self, symbol: str):
        pos = self._position(symbol)
        side_pos = pos.get("side", "FLAT")
        if side_pos == "FLAT":
            return {"already_flat": True}

        qty_req = float(pos.get("qty") or 0.0)
        if qty_req <= 0:
            # 本地状态异常：先对账再试一次
            self.reconcile_positions(reason="close_qty_missing")
            pos = self._position(symbol)
            side_pos = pos.get("side", "FLAT")
            qty_req = float(pos.get("qty") or 0.0)
            if side_pos == "FLAT" or qty_req <= 0:
                return {"error": "cannot_close_no_qty", "side": side_pos, "qty": qty_req}

        order_type = self.cfg["trade"]["order_type"]

        if side_pos == "LONG":
            params = self._okx_order_params(is_close=True, pos_side="LONG")
            order = self.exchange.create_order(symbol, order_type, "sell", qty_req, params=params)
        else:
            params = self._okx_order_params(is_close=True, pos_side="SHORT")
            order = self.exchange.create_order(symbol, order_type, "buy", qty_req, params=params)

        order_id = order.get("id") or (order.get("info") or {}).get("ordId")
        filled_qty = 0.0
        avg_price = 0.0
        timeout = False
        status = "unknown"

        if order_id:
            fetched = self._wait_order_filled(symbol, order_id, timeout_sec=12, interval_sec=0.5)
            fill = fetched.get("__fill__", {})
            filled_qty = self._safe_num(fill.get("filled"), 0.0)
            avg_price = self._safe_num(fill.get("average"), 0.0)
            timeout = bool(fill.get("timeout", False))
            status = str(fill.get("status", "unknown"))
            order = fetched

        # ✅ 只有在“确认平仓已完成（或 filled >= qty_req 的大部分）”才置 FLAT
        # 部分成交/超时：保守做法是触发一次对账，以交易所为准
        if (status in ("closed", "filled")) or (filled_qty >= max(0.0000001, qty_req * 0.999)):
            self._update_state_flat(symbol)
            return {
                "closed": True,
                "qty_requested": qty_req,
                "qty_filled": filled_qty if filled_qty > 0 else qty_req,
                "avg_price": avg_price,
                "order": order,
                "timeout": timeout,
            }

        # 未确认成交：对账一次更新本地状态
        rec = self.reconcile_positions(reason="close_not_confirmed")
        return {
            "closed": False,
            "reason": "close_not_confirmed_reconciled",
            "qty_requested": qty_req,
            "qty_filled": filled_qty,
            "avg_price": avg_price,
            "order": order,
            "reconcile": rec,
            "timeout": timeout,
        }

    # -------------------------
    # Trailing stop loop
    # -------------------------
    def _trailing_loop(self):
        while not self._stop_flag:
            try:
                if self.cfg["trailing_stop"].get("enabled", True):
                    for symbol in list(self.cfg["trade"]["symbols"]):
                        if symbol in self._state:
                            self._update_trailing(symbol)
            except Exception:
                pass

            time.sleep(int(self.cfg["app"].get("poll_interval_sec", 300)))

    def _update_trailing(self, symbol: str):
        pos = self._position(symbol)
        side = pos.get("side", "FLAT")
        if side == "FLAT":
            return

        last = self._fetch_last_price(symbol)
        entry = float(pos.get("entry_price", 0))
        profit_pct = calc_profit_pct(side, entry, last)

        tcfg = self.cfg["trailing_stop"]
        trigger = float(tcfg["tighten_trigger_profit_pct"])
        init_pct = float(tcfg["initial_trail_pct"])
        tight_pct = float(tcfg["tightened_trail_pct"])
        min_pct = float(tcfg.get("min_trail_pct", tight_pct))

        new_trail = init_pct if profit_pct < trigger else max(tight_pct, min_pct)

        old_trail_price = float(pos.get("trail_price", 0))
        candidate = trail_stop_price(side, last, new_trail)

        if side == "LONG":
            trail_price = max(old_trail_price, candidate)
            if last <= trail_price:
                res = self._close(symbol)
                self._runtime["last_action"][symbol] = {"action": "TRAILING_STOP_HIT", "ts": int(time.time()), "detail": res}
                return
        else:
            trail_price = min(old_trail_price, candidate) if old_trail_price > 0 else candidate
            if last >= trail_price:
                res = self._close(symbol)
                self._runtime["last_action"][symbol] = {"action": "TRAILING_STOP_HIT", "ts": int(time.time()), "detail": res}
                return

        pos["trail_pct"] = new_trail
        pos["trail_price"] = trail_price
        pos["last_price"] = last
        pos["profit_pct_est"] = profit_pct
        pos["updated_at"] = int(time.time())
        self._state[symbol] = pos
