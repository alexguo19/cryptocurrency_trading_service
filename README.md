# OKX USDT Perpetual Neural Momentum Bot (TradingView Webhook + Auto Trading)

This project is a runnable skeleton for automated crypto futures trading on **OKX USDT Perpetual Swap** using **TradingView alerts** (BUY/SELL) from *Neural Momentum Strategy*.

## Features
- TradingView webhook receiver (FastAPI)
- OKX USDT perpetual trading via CCXT (`swap`, `tdMode`, `posSide`, `reduceOnly`)
- Reverse on opposite signal (close + flip)
- Two-stage trailing stop:
  - initial trailing stop (e.g. 3%)
  - tighten to (e.g. 0.1%) when profit >= 1%
- Background loop to update trailing stop every N seconds (default 300s)
- Status endpoint `/state` for monitoring
- Admin-protected control endpoints `/control/*` using header `X-ADMIN-SECRET`
- Streamlit dashboard for config + monitoring + emergency controls
- Docker / docker-compose deployment

## Disclaimer
This is educational/demo software. You are responsible for:
- exchange risk, leverage risk, liquidation risk
- slippage, fees, latency, strategy performance
Always test on demo/small size first.

## 1) Project Structure
server.py # FastAPI webhook + control endpoints
trader.py # OKX trading engine
risk.py # PnL and trailing stop utils
app.py # Streamlit dashboard
config.yaml # All configuration (secrets included)
Dockerfile
docker-compose.yml

## 2) Configure `config.yaml`
1. Set long random strings:
   - `webhook.secret`
   - `admin.secret`
2. Fill OKX API credentials:
   - `exchange.api_key`
   - `exchange.api_secret`
   - `exchange.password` (OKX passphrase)

IMPORTANT: For OKX USDT perpetual, use symbols like:
- `BTC/USDT:USDT`
- `ETH/USDT:USDT`

## 3) Run locally (no Docker)

pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
streamlit run app.py --server.port 8501 --server.address 0.0.0.0

Open:
API: http://localhost:8000/health
Dashboard: http://localhost:8501

Run with Docker
docker compose up -d --build

## 4) Run with Docker

Create TradingView alerts on your chart/strategy and set webhook URL:

http(s)://YOUR_SERVER:8000/webhook/tradingview

Example alert message (BUY):

{
  "secret": "CHANGE_ME_TV_WEBHOOK_SECRET_LONG_RANDOM",
  "symbol": "OKX:BTCUSDT.P",
  "action": "BUY",
  "time": "{{time}}",
  "timeframe": "{{interval}}",
  "price": "{{close}}",
  "bar_time": "{{time}}"
}


SELL is the same with "action":"SELL".

The server will normalize OKX:BTCUSDT.P -> BTC/USDT:USDT and validate it is in trade.symbols.

## 6) Admin Controls

All /control/* endpoints require header:

X-ADMIN-SECRET: <admin.secret>

Controls:

POST /control/pause { paused: true/false, reason?: string }

POST /control/close_only { close_only: true/false }

POST /control/emergency_close { symbol: "BTC/USDT:USDT" }

POST /control/emergency_close_all {}

## 7) AWS Deployment Notes

Do NOT expose Streamlit to the public internet without protection.

Prefer security group IP whitelist, VPN, or Nginx basic auth.

Use HTTPS for the webhook (Nginx/ALB + TLS).

Consider persisting state/logs in Redis/DB.

## 8) Known Limitations

In-memory state only; restart loses local state

No trade fill confirmation / partial fill handling

No reconciliation with exchange positions at startup

Strategy performance is not guaranteed

## License

MIT (or your preferred license)

