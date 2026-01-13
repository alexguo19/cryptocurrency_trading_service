# app.py
# Streamlit 管理台：
# - 编辑并保存 config.yaml
# - 查看 /state（仓位、移动止损、收益估算、最近信号/动作）
# - 管理操作（暂停/恢复/只平不反手/紧急平仓）：带 X-ADMIN-SECRET

import time
import yaml
import requests
import streamlit as st
from pathlib import Path

CONFIG_PATH = Path("config.yaml")

# 生产建议：用 Nginx 或安全组限制访问
API_BASE = st.secrets.get("API_BASE", "http://127.0.0.1:8000")


def load_cfg():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def save_cfg(cfg: dict):
    CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8"
    )


def api_get(path: str):
    r = requests.get(f"{API_BASE}{path}", timeout=5)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict | None = None, admin_secret: str | None = None):
    headers = {}
    if admin_secret:
        headers["X-ADMIN-SECRET"] = admin_secret
    r = requests.post(f"{API_BASE}{path}", json=payload or {}, headers=headers, timeout=8)
    r.raise_for_status()
    return r.json()


st.set_page_config(page_title="OKX USDT Perpetual Bot", layout="wide")
st.title("OKX USDT 永续 · Neural Momentum 自动交易控制台")

cfg = load_cfg()
admin_secret = cfg.get("admin", {}).get("secret", "")

# -------------------------
# Sidebar: 配置编辑
# -------------------------
with st.sidebar:
    st.header("配置编辑（config.yaml）")

    st.subheader("运行参数")
    cfg["app"]["poll_interval_sec"] = st.number_input(
        "止损检查周期(秒)",
        min_value=30,
        value=int(cfg["app"]["poll_interval_sec"]),
        step=30
    )

    st.subheader("交易参数")
    cfg["trade"]["leverage"] = st.number_input(
        "杠杆",
        min_value=1,
        max_value=50,
        value=int(cfg["trade"]["leverage"])
    )
    cfg["trade"]["margin_mode"] = st.selectbox(
        "保证金模式",
        ["cross", "isolated"],
        index=0 if cfg["trade"]["margin_mode"] == "cross" else 1
    )

    st.subheader("移动止损")
    cfg["trailing_stop"]["enabled"] = st.checkbox(
        "启用移动止损",
        value=bool(cfg["trailing_stop"]["enabled"])
    )
    cfg["trailing_stop"]["initial_trail_pct"] = st.number_input(
        "初始移动止损(%)",
        min_value=0.1,
        value=float(cfg["trailing_stop"]["initial_trail_pct"]),
        step=0.1
    )
    cfg["trailing_stop"]["tighten_trigger_profit_pct"] = st.number_input(
        "收益触发点(%)",
        min_value=0.1,
        value=float(cfg["trailing_stop"]["tighten_trigger_profit_pct"]),
        step=0.1
    )
    cfg["trailing_stop"]["tightened_trail_pct"] = st.number_input(
        "收紧后止损(%)",
        min_value=0.05,
        value=float(cfg["trailing_stop"]["tightened_trail_pct"]),
        step=0.05
    )

    if st.button("保存配置", type="primary"):
        save_cfg(cfg)
        st.success("已保存到 config.yaml（服务端会热加载）")

    st.divider()
    st.caption(f"API_BASE: {API_BASE}")
    if not admin_secret or "CHANGE_ME" in admin_secret:
        st.warning("admin.secret 似乎未正确配置（控制按钮会被拒绝）")

# -------------------------
# Main: 状态与控制
# -------------------------
colA, colB, colC = st.columns([1.2, 1, 1])

with colA:
    st.subheader("运行控制（Admin）")
    try:
        state = api_get("/state")
        runtime = state.get("runtime", {})
        paused = bool(runtime.get("paused", False))
        close_only = bool(runtime.get("close_only", False))
        reason = runtime.get("pause_reason", "")

        st.write(f"**Paused:** {paused}  |  **Close-only:** {close_only}")
        if paused and reason:
            st.warning(f"暂停原因：{reason}")

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            if st.button("暂停交易"):
                api_post("/control/pause", {"paused": True, "reason": "manual"}, admin_secret=admin_secret)
                st.rerun()
        with c2:
            if st.button("恢复交易"):
                api_post("/control/pause", {"paused": False, "reason": ""}, admin_secret=admin_secret)
                st.rerun()
        with c3:
            if st.button("只平不反手"):
                api_post("/control/close_only", {"close_only": True}, admin_secret=admin_secret)
                st.rerun()
        with c4:
            if st.button("恢复正常模式"):
                api_post("/control/close_only", {"close_only": False}, admin_secret=admin_secret)
                st.rerun()

    except Exception as e:
        st.error(f"无法获取 /state：{e}")

with colB:
    st.subheader("紧急操作（Admin）")
    st.error("⚠️ 紧急按钮会立刻触发市价 reduceOnly 平仓，请谨慎。")
    try:
        symbols = cfg["trade"]["symbols"]
        sym = st.selectbox("选择交易对", symbols)

        c1, c2 = st.columns(2)
        with c1:
            if st.button("紧急平仓（单个）", type="secondary"):
                res = api_post("/control/emergency_close", {"symbol": sym}, admin_secret=admin_secret)
                st.json(res)
        with c2:
            if st.button("紧急全平（全部）", type="primary"):
                res = api_post("/control/emergency_close_all", {}, admin_secret=admin_secret)
                st.json(res)
    except Exception as e:
        st.error(str(e))

with colC:
    st.subheader("自动刷新")
    refresh_sec = st.slider("刷新间隔(秒)", min_value=1, max_value=30, value=3)
    auto = st.toggle("开启自动刷新", value=True)
    if auto:
        time.sleep(refresh_sec)
        st.rerun()

st.divider()

# -------------------------
# 仓位状态
# -------------------------
st.subheader("仓位状态（positions）")
try:
    state = api_get("/state")
    positions = state.get("positions", {})

    if not positions:
        st.info("暂无仓位状态（可能未触发任何信号）")
    else:
        rows = []
        for sym, p in positions.items():
            rows.append({
                "symbol": sym,
                "side": p.get("side", "FLAT"),
                "entry_price": p.get("entry_price"),
                "qty": p.get("qty"),
                "last_price": p.get("last_price"),
                "profit_pct_est": p.get("profit_pct_est"),
                "trail_pct": p.get("trail_pct"),
                "trail_price": p.get("trail_price"),
                "last_bar_time": p.get("last_bar_time"),
                "updated_at": p.get("updated_at"),
            })
        st.dataframe(rows, use_container_width=True)

    st.subheader("最近信号 / 最近动作")
    runtime = state.get("runtime", {})
    st.write("**last_signal**")
    st.json(runtime.get("last_signal", {}))
    st.write("**last_action**")
    st.json(runtime.get("last_action", {}))

except Exception as e:
    st.error(f"状态展示失败：{e}")
