# risk.py
# 计算收益率与移动止损价格（不涉及交易所）

def calc_profit_pct(side: str, entry: float, last: float) -> float:
    """
    side: LONG / SHORT
    返回：百分比收益率（未考虑手续费/滑点）
    """
    if entry <= 0:
        return 0.0
    if side == "LONG":
        return (last - entry) / entry * 100.0
    if side == "SHORT":
        return (entry - last) / entry * 100.0
    return 0.0


def trail_stop_price(side: str, ref_price: float, trail_pct: float) -> float:
    """
    根据参考价格 ref_price 和 trail_pct 得到止损价：
      LONG: ref*(1-trail)
      SHORT: ref*(1+trail)
    注意：
      - 开仓时 ref_price 可以用 entry
      - 更新时 ref_price 用 last，让止损跟随最新价
    """
    t = trail_pct / 100.0
    if side == "LONG":
        return ref_price * (1.0 - t)
    if side == "SHORT":
        return ref_price * (1.0 + t)
    return ref_price
