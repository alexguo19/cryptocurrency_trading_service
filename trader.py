# trader.py
# OKX USDT 永续（swap）交易引擎：
# - 接收 BUY/SELL 信号
# - 仓位管理（反向信号：平仓 + 反手）
# - 双阶段移动止损（3% -> 盈利>=1% 收紧到0.1%）
# - 运行控制：暂停、只平不反手、紧急平仓
#
# 注意：示例使用“本地内存状态”，服务重启状态会清空（但交易所真实仓位仍在）。
# 生产建议：启动时从交易所拉取仓位，或使用 Redis/DB 持久化。

import threading
import time
from tenacity import retry, stop_after_attempt, wait_fixed
import ccxt

from risk import calc_profit_pct, trail_stop_price


def normalize_tv_symbol(tv_symbol: str, allowed_symbols: list[str]) -> str:
    """
    TradingView symbol -> OKX ccxt symbol
    常见输入：
      "OKX:BTCUSDT.P" / "BTCUSDT.P" / "BTCUSDT"
    输出（建议）：
      "BTC/USDT:USDT"
    """
    raw = tv_symbol.split(":")[-1].upper()
    raw = raw.replace(".P", "")  # perpetual 常带 .P
    if raw.endswith("USDT"):
        base = raw[:-4]
        guess = f"{base}/USDT:USDT"
        if guess in allowed_symbols:
            return guess

    # 兜底：按 allowed_symbols 猜测
    for sym in allowed_symbols:
        base = sym.split("/")[0].upper()
        if raw.startswith(base):
            return sym

    return raw


class TradeEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg

        # 每个 symbol 一个锁，避免并发信号导致重复下单
        self._lock_map: dict[str, threading.Lock] = {}

        # 本地内存状态：symbol -> position info
        self._state: dict[str, dict] = {}

        # 运行控制 & 观测信息
        self._runtime = {
            "paused": False,          # 暂停：忽略新信号
            "pause_reason": "",
            "close_only": False,      # 只平不反手/不开新仓
            "last_signal": {},        # symbol -> {action, ts, raw}
            "last_action": {},        # symbol -> {action, ts, detail}
        }

        self.exchange = self._init_exchange(cfg)

        # 后台线程：周期性更新移动止损并检查是否触发
        self._stop_flag = False
        self._bg = threading.Thread(target=self._trailing_loop, daemon=True)
        self._bg.start()

    # -------------------------
    # 配置热加载
    # -------------------------
    def reload_config(self, cfg: dict):
        self.cfg = cfg

    # -------------------------
    # 状态查询
    # -------------------------
    def get_state(self) -> dict:
        out = {
            "runtime": self._runtime,
            "positions": self._state,
            "config_summary": {
                "symbols": self.cfg["trade"]["symbols"],
                "leverage": self.cfg["trade"]["leverage"],
                "margin_mode": self.cfg["trade"]["margin_mode"],
                "poll_interval_sec": self.cfg["app"]["poll_interval_sec"],
            }
        }
        return out

    # -------------------------
    # 控制开关
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
        self._runtime["last_action"][symbol] = {
            "action": "EMERGENCY_CLOSE",
            "ts": int(time.time()),
            "detail": res
        }
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
    # 内部工具
    # -------------------------
    def _get_lock(self, symbol: str) -> threading.Lock:
        if symbol not in self._lock_map:
            self._lock_map[symbol] = threading.Lock()
        return self._lock_map[symbol]

    def _init_exchange(self, cfg: dict):
        # OKX USDT perpetual: swap + settle=USDT
        ex = ccxt.okx({
            "apiKey": cfg["exchange"]["api_key"],
            "secret": cfg["exchange"]["api_secret"],
            "password": cfg["exchange"]["password"],  # OKX passphrase
            "enableRateLimit": cfg["exchange"].get("enable_rate_limit", True),
        })
        ex.options = ex.options or {}
        ex.options["defaultType"] = cfg["exchange"].get("market_type", "swap")
        ex.options["settle"] = cfg["exchange"].get("settle", "USDT")

        # 提前加载市场，减少 symbol 不识别风险
        ex.load_markets()
        return ex

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    def _fetch_last_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker["last"])

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

    def _position(self, symbol: str) -> dict:
        return self._state.get(symbol, {"side": "FLAT"})

    def _update_state_open(self, symbol: str, side: str, entry_price: float, qty: float, bar_time: str | None):
        # 开仓时设置初始移动止损
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
        """
        同一根 1H K 线内重复信号忽略（防止 TV 重复触发）
        """
        if not self.cfg["runtime"].get("dedup_same_bar", True):
            return False
        if not bar_time:
            return False
        st = self._state.get(symbol)
        if not st:
            return False
        return st.get("last_bar_time") == bar_time

    def _set_leverage_and_margin(self, symbol: str):
        """
        OKX：
          - margin mode 最稳做法：下单 params 里 tdMode 一定带上
          - 这里尝试 set_margin_mode / set_leverage，不成功也不影响（下单仍会带 tdMode）
        """
        lev = int(self.cfg["trade"]["leverage"])
        margin_mode = self.cfg["trade"]["margin_mode"]  # cross/isolated

        try:
            self.exchange.set_margin_mode(margin_mode, symbol, params={"tdMode": margin_mode})
        except Exception:
            pass

        try:
            self.exchange.set_leverage(lev, symbol)
        except Exception:
            pass

    def _okx_order_params(self, is_close: bool, pos_side: str) -> dict:
        """
        OKX 下单关键参数：
          - tdMode: cross / isolated
          - posSide: long / short
          - reduceOnly: 平仓单必须 true，避免“平成反手/加仓”
        """
        margin_mode = self.cfg["trade"]["margin_mode"]
        params = {
            "tdMode": margin_mode,
            "posSide": "long" if pos_side == "LONG" else "short",
        }
        if is_close:
            params["reduceOnly"] = True
        return params

    # -------------------------
    # 信号处理入口
    # -------------------------
    def on_signal(self, symbol: str, action: str, bar_time: str | None, timeframe: str | None, raw: dict):
        lock = self._get_lock(symbol) if self.cfg["runtime"].get("lock_per_symbol", True) else threading.Lock()
        with lock:
            # 记录信号
            self._runtime["last_signal"][symbol] = {"action": action, "ts": int(time.time()), "raw": raw}

            # 暂停：忽略新信号
            if self._runtime.get("paused", False):
                return {"ignored": True, "reason": "paused", "pause_reason": self._runtime.get("pause_reason", "")}

            # 同bar去重
            if self._maybe_dedup(symbol, bar_time):
                return {"ignored": True, "reason": "dedup_same_bar"}

            pos = self._position(symbol)
            side = pos.get("side", "FLAT")

            target = "LONG" if action == "BUY" else "SHORT"

            # close-only：不新开仓不反手，只允许“反向信号触发平仓”
            if self._runtime.get("close_only", False):
                if side == "FLAT":
                    return {"ignored": True, "reason": "close_only_flat"}
                if side != target:
                    res = self._close(symbol)
                    self._runtime["last_action"][symbol] = {"action": "CLOSE_ONLY_CLOSE", "ts": int(time.time()), "detail": res}
                    return {"close_only_closed": True, "detail": res}
                return {"ignored": True, "reason": "close_only_same_direction"}

            # 正常模式
            if side == "FLAT":
                res = self._open(symbol, target, bar_time)
                self._runtime["last_action"][symbol] = {"action": f"OPEN_{target}", "ts": int(time.time()), "detail": res}
                return res

            # 同向信号忽略
            if side == target and self.cfg["strategy"].get("ignore_same_direction_signal", True):
                self._state[symbol]["last_bar_time"] = bar_time
                return {"ignored": True, "reason": "same_direction"}

            # 反向信号：平仓 + 反手
            if self.cfg["strategy"].get("reverse_on_opposite_signal", True):
                close_res = self._close(symbol)
                time.sleep(0.3)  # OKX：避免平仓未成交又开仓导致净仓混乱
                open_res = self._open(symbol, target, bar_time)
                res = {"closed": close_res, "opened": open_res}
                self._runtime["last_action"][symbol] = {"action": f"REVERSE_TO_{target}", "ts": int(time.time()), "detail": res}
                return res

            # 否则只平仓
            close_res = self._close(symbol)
            self._runtime["last_action"][symbol] = {"action": "CLOSE_ONLY_BY_STRATEGY", "ts": int(time.time()), "detail": close_res}
            return {"closed_only": True, "detail": close_res}

    # -------------------------
    # 开仓/平仓
    # -------------------------
    def _open(self, symbol: str, target: str, bar_time: str | None):
        self._set_leverage_and_margin(symbol)
        qty = self._qty_for_symbol(symbol)
        order_type = self.cfg["trade"]["order_type"]

        if target == "LONG":
            params = self._okx_order_params(is_close=False, pos_side="LONG")
            order = self.exchange.create_order(symbol, order_type, "buy", qty, params=params)
        else:
            params = self._okx_order_params(is_close=False, pos_side="SHORT")
            order = self.exchange.create_order(symbol, order_type, "sell", qty, params=params)

        entry = self._fetch_last_price(symbol)
        self._update_state_open(symbol, target, entry, qty, bar_time)
        return {"opened": target, "qty": qty, "entry_price": entry, "order": order}

    def _close(self, symbol: str):
        pos = self._position(symbol)
        side_pos = pos.get("side", "FLAT")
        if side_pos == "FLAT":
            return {"already_flat": True}

        qty = float(pos["qty"])
        order_type = self.cfg["trade"]["order_type"]

        if side_pos == "LONG":
            params = self._okx_order_params(is_close=True, pos_side="LONG")
            order = self.exchange.create_order(symbol, order_type, "sell", qty, params=params)
        else:
            params = self._okx_order_params(is_close=True, pos_side="SHORT")
            order = self.exchange.create_order(symbol, order_type, "buy", qty, params=params)

        self._update_state_flat(symbol)
        return {"closed": True, "order": order}

    # -------------------------
    # 后台：移动止损更新与触发检查
    # -------------------------
    def _trailing_loop(self):
        while not self._stop_flag:
            try:
                if self.cfg["trailing_stop"].get("enabled", True):
                    for symbol in list(self.cfg["trade"]["symbols"]):
                        # 没有仓位状态也没关系
                        if symbol in self._state:
                            self._update_trailing(symbol)
            except Exception:
                # 不让后台线程崩掉
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

        # 根据收益率决定当前 trail_pct
        new_trail = init_pct if profit_pct < trigger else max(tight_pct, min_pct)

        # 计算候选止损价：用 last 作为 ref_price，让止损跟随最新价
        old_trail_price = float(pos.get("trail_price", 0))
        candidate = trail_stop_price(side, last, new_trail)

        # LONG：止损价只上移；SHORT：止损价只下移
        if side == "LONG":
            trail_price = max(old_trail_price, candidate)
            # 触发止损：last <= trail_price
            if last <= trail_price:
                res = self._close(symbol)
                self._runtime["last_action"][symbol] = {"action": "TRAILING_STOP_HIT", "ts": int(time.time()), "detail": res}
                return
        else:
            trail_price = min(old_trail_price, candidate) if old_trail_price > 0 else candidate
            # 触发止损：last >= trail_price
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
