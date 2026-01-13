# OKX USDT 永续 Neural Momentum Bot（TradingView Webhook + 自动合约交易）

这是一个“更接近生产”的自动化合约交易骨架：使用 TradingView Neural Momentum Strategy 的 BUY/SELL（Alert Webhook），在 **OKX USDT 永续（Perpetual Swap）**上自动执行开平仓，并提供状态面板与紧急开关。

## 核心功能
- FastAPI 接收 TradingView Webhook（BUY/SELL）
- CCXT 对接 OKX USDT 永续（关键参数：`swap` / `tdMode` / `posSide` / `reduceOnly`）
- 反向信号：平仓 + 反手
- 双阶段移动止损：初始 3% -> 盈利>=1% 收紧到 0.1%
- 后台每 N 秒（默认 300s）更新止损并检查触发
- **成交确认（增强）**：
  - 下单后轮询 `fetch_order`，读取真实 `filled` 与 `average`
  - 用真实成交均价更新本地 `entry_price`，用真实成交数量更新 `qty`
  - 平仓只有在确认成交后才置为 FLAT；否则触发对账
- **仓位对账（增强）**：
  - 启动时对账一次（避免重启后状态丢失导致重复开仓）
  - 周期性对账（默认 600s 一次）
  - 管理接口手动对账
- `/state` 状态接口
- `/control/*` 管理接口强制鉴权（Header：`X-ADMIN-SECRET`）
- Streamlit 面板：配置编辑 + 状态监控 + 暂停/只平/紧急平仓/立即对账
- Docker / docker-compose 部署（AWS 友好）

## 风险声明
示例/教育用途。你需自行承担杠杆、爆仓、滑点、手续费、延迟与策略失效风险。务必先用模拟/小仓测试。

## 1) 配置 config.yaml
1. 修改密钥：
   - `webhook.secret`
   - `admin.secret`
2. 填写 OKX API：
   - `exchange.api_key`
   - `exchange.api_secret`
   - `exchange.password`（OKX passphrase）
3. 交易对使用 OKX swap 标准格式：
   - `BTC/USDT:USDT`
   - `ETH/USDT:USDT`

## 2) 本地运行
```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

## 3) Docker 运行
docker compose up -d --build

## 4) TradingView Alert

Webhook URL：

http(s)://YOUR_SERVER:8000/webhook/tradingview

BUY 示例：

{
  "secret": "CHANGE_ME_TV_WEBHOOK_SECRET_LONG_RANDOM",
  "symbol": "OKX:BTCUSDT.P",
  "action": "BUY",
  "time": "{{time}}",
  "timeframe": "{{interval}}",
  "price": "{{close}}",
  "bar_time": "{{time}}"
}

## 5) 管理接口（必须鉴权）

所有 /control/* 需要 Header：

X-ADMIN-SECRET: <admin.secret>

接口：

POST /control/pause：暂停/恢复

POST /control/close_only：只平不反手

POST /control/emergency_close：紧急平仓（单个）

POST /control/emergency_close_all：紧急全平

POST /control/reconcile：立即对账（强烈建议在重启后或异常时手动触发）

## 6) AWS 建议

8501 面板不要裸露公网：安全组白名单/VPN/Nginx 认证

webhook 建议 HTTPS（Nginx/ALB + TLS）

生产建议：Redis/DB 持久化状态与日志

