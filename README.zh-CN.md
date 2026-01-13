# OKX USDT 永续 Neural Momentum Bot（TradingView Webhook + 自动合约交易）

本项目是一个可运行的自动化合约交易骨架：使用 TradingView *Neural Momentum Strategy* 的 BUY/SELL 信号（Alert Webhook），在 **OKX USDT 永续（Perpetual Swap）**上自动开平仓，并提供状态面板与紧急开关。

## 功能
- FastAPI 接收 TradingView Webhook 信号
- CCXT 对接 OKX USDT 永续（关键参数：`swap` / `tdMode` / `posSide` / `reduceOnly`）
- 反向信号：平仓 + 反手
- 双阶段移动止损：
  - 初始移动止损（如 3%）
  - 收益率 >= 1% 时收紧到（如 0.1%）
- 后台线程每 N 秒（默认 300 秒）更新止损并检查触发
- `/state` 状态接口：仓位、止损、收益估算、最近信号/动作
- `/control/*` 管理接口（必须 Header：`X-ADMIN-SECRET`）
- Streamlit 面板：编辑配置 + 实时状态 + 紧急控制
- Docker / docker-compose 部署（适合 AWS）

## 风险声明
该项目为示例/教育用途。你需要自行承担：
- 交易所风险、杠杆风险、爆仓风险
- 滑点、手续费、延迟
- 策略效果不保证
强烈建议先用模拟/小仓测试。

## 1) 目录结构
server.py # Webhook + 控制接口
trader.py # OKX 交易执行引擎
risk.py # 收益率 & 止损工具函数
app.py # Streamlit 管理面板
config.yaml # 所有配置（含密钥）
Dockerfile
docker-compose.yml

markdown
Copy code

## 2) 配置 config.yaml
1. 修改以下为长随机串：
   - `webhook.secret`
   - `admin.secret`
2. 填写 OKX API：
   - `exchange.api_key`
   - `exchange.api_secret`
   - `exchange.password`（OKX passphrase）

重要：OKX USDT 永续交易对建议使用 ccxt 标准格式：
- `BTC/USDT:USDT`
- `ETH/USDT:USDT`

## 3) 本地运行（非 Docker）
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
访问：

API 健康检查：http://localhost:8000/health

面板：http://localhost:8501

## 4) Docker 运行
bash
Copy code
docker compose up -d --build
## 5) TradingView Alert 设置
在 TradingView 策略/指标上创建 Alert，并设置 Webhook URL：

http(s)://YOUR_SERVER:8000/webhook/tradingview

示例 BUY 消息：

json
Copy code
{
  "secret": "CHANGE_ME_TV_WEBHOOK_SECRET_LONG_RANDOM",
  "symbol": "OKX:BTCUSDT.P",
  "action": "BUY",
  "time": "{{time}}",
  "timeframe": "{{interval}}",
  "price": "{{close}}",
  "bar_time": "{{time}}"
}
SELL 同理，只是 "action":"SELL"。

服务端会把 OKX:BTCUSDT.P 归一化为 BTC/USDT:USDT，并校验必须在 trade.symbols 内。

## 6) 管理/紧急控制（强制鉴权）
所有 /control/* 接口必须带 Header：

X-ADMIN-SECRET: <admin.secret>

接口：

POST /control/pause：暂停/恢复 { paused: true/false, reason?: string }

POST /control/close_only：只平不反手 { close_only: true/false }

POST /control/emergency_close：紧急平单个 { symbol: "BTC/USDT:USDT" }

POST /control/emergency_close_all：紧急全平 {}

Streamlit 面板会自动从 config.yaml 读取 admin.secret 并携带 Header。

## 7) AWS 部署建议（强烈建议）
不要把 Streamlit（8501）直接暴露公网：

安全组只放行你自己的 IP；或 VPN；或 Nginx Basic Auth

Webhook 建议上 HTTPS（Nginx/ALB + TLS）

生产建议：用 Redis/DB 持久化状态与日志，并加入成交确认/对账逻辑

## 8) 已知限制
状态仅保存在内存：服务重启会丢失本地状态（但交易所真实仓位仍在）

未实现成交回报确认、部分成交处理

未实现启动时与交易所仓位对账

策略并不保证盈利

## License
MIT（或你自行指定）