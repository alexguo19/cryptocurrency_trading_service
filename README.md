# OKX USDT Perpetual Neural Momentum Bot (TradingView Webhook + Auto Trading)

A runnable production-enhanced skeleton for automated futures trading on **OKX USDT Perpetual Swap** using **TradingView alerts** (BUY/SELL) from Neural Momentum Strategy.

## Key Features
- FastAPI webhook receiver for TradingView BUY/SELL
- OKX USDT perpetual trading via CCXT (`swap`, `tdMode`, `posSide`, `reduceOnly`)
- Reverse on opposite signal (close + flip)
- Two-stage trailing stop:
  - initial trailing stop (e.g. 3%)
  - tighten to (e.g. 0.1%) when profit >= 1%
- Background loop to update trailing stop every N seconds (default 300s)
- **Order fill confirmation**:
  - after placing an order, poll `fetch_order` to get `filled` and `average`
  - update local `qty` and `entry_price` with real fill data
  - when closing, only set local state to FLAT after close is confirmed; otherwise reconcile
- **Exchange position reconciliation**:
  - reconcile at startup
  - periodic reconcile (default every 600s)
  - manual reconcile via admin endpoint
- `/state` endpoint for monitoring
- Admin-protected `/control/*` endpoints via header `X-ADMIN-SECRET`
- Streamlit dashboard for config + monitoring + emergency controls (pause/close-only/emergency close/reconcile)
- Docker / docker-compose ready for AWS

## Disclaimer
Educational/demo software only. You assume all risks: leverage, liquidation, slippage, fees, latency, and strategy performance. Test on small size or demo first.

## 1) Configure `config.yaml`
1. Set secrets:
   - `webhook.secret`
   - `admin.secret`
2. Fill OKX API credentials:
   - `exchange.api_key`
   - `exchange.api_secret`
   - `exchange.password` (OKX passphrase)
3. Use OKX swap symbols:
   - `BTC/USDT:USDT`
   - `ETH/USDT:USDT`

## 2) Run locally
```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

## 3) Run with Docker
docker compose up -d --build

## 4) TradingView Alert Webhook

Webhook URL:

http(s)://YOUR_SERVER:8000/webhook/tradingview

Example BUY message:

{
  "secret": "CHANGE_ME_TV_WEBHOOK_SECRET_LONG_RANDOM",
  "symbol": "OKX:BTCUSDT.P",
  "action": "BUY",
  "time": "{{time}}",
  "timeframe": "{{interval}}",
  "price": "{{close}}",
  "bar_time": "{{time}}"
}

## 5) Admin Controls

All /control/* endpoints require:

Header X-ADMIN-SECRET: <admin.secret>

Endpoints:

POST /control/pause { paused: true/false, reason?: string }

POST /control/close_only { close_only: true/false }

POST /control/emergency_close { symbol: "BTC/USDT:USDT" }

POST /control/emergency_close_all {}

POST /control/reconcile {} (manual position reconciliation)

## 6) AWS Notes

Protect Streamlit (8501): IP whitelist / VPN / Nginx auth

Use HTTPS for webhook (Nginx/ALB + TLS)

Consider persisting state/logs in Redis/DB

