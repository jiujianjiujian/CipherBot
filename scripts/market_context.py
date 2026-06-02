# -*- coding: utf-8 -*-
"""
Cipher 市场背景过滤器 — 类似 NFI 去风险系统
在全局层面判断是否适合开仓，避免不利行情中硬做

检查项目:
  1. BTC波动率 (ATR%) — 高波动 → 减仓/暂停
  2. BTC趋势结构 (EMA排列) — 空头排列 → 减仓
  3. BTC RSI极端 — 超买/超卖 → 方向限制
  4. 连续下跌/上涨天数 — 极端延续
"""
from typing import List, Dict, Optional

from indicators import calc_ema, calc_rsi, calc_atr


class MarketContext:
    """市场上下文 — 单次扫描快照"""

    def __init__(self):
        self.derisk = False            # 是否进入去风险模式
        self.derisk_factor = 1.0       # 仓位乘数 (0.0~1.0)
        self.reasons: List[str] = []   # 原因说明
        self.btc_volatility_pct = 0.0  # BTC 4h ATR%
        self.btc_trend = "unknown"     # uptrend / downtrend / ranging
        self.btc_rsi = 50.0
        self.long_allowed = True       # 是否允许做多
        self.short_allowed = True      # 是否允许做空
        self.oi_info = {}              # v5: OI/资金费率分析
        self.quality_score = 5.5       # v5: 行情评分 0-10
        self.liquidity_score = 100     # v5: 流动性评分 0-100，低=<30

    def __repr__(self):
        return (f"MarketContext(derisk={self.derisk}, factor={self.derisk_factor:.2f}, "
                f"long={self.long_allowed}, short={self.short_allowed}, "
                f"reasons={self.reasons})")


def detect_trend(closes_4h: List[float]) -> str:
    """用EMA20/50排列判断趋势"""
    if len(closes_4h) < 20:
        return "unknown"
    ema20 = calc_ema(closes_4h, 20)
    ema50 = calc_ema(closes_4h, min(50, len(closes_4h)))
    price = closes_4h[-1]

    if price > ema20 > ema50:
        return "uptrend"
    elif price < ema20 < ema50:
        return "downtrend"
    else:
        return "ranging"


def evaluate_market_context(klines_1h: Optional[List[dict]],
                             klines_4h: Optional[List[dict]],
                             oi_data: Optional[dict] = None) -> MarketContext:
    """
    评估全局市场背景，返回 MarketContext

    Args:
        klines_1h: BTC 1h K线 (至少50根，最好200+)
        klines_4h: BTC 4h K线 (至少20根，最好100+)
        oi_data: OI/资金费率数据 (来自 get_contract_data)

    Returns:
        MarketContext 对象
    """
    ctx = MarketContext()

    if not klines_4h or len(klines_4h) < 10:
        ctx.derisk = True
        ctx.derisk_factor = 0.5
        ctx.reasons.append("数据不足，保守交易模式")
        return ctx

    closes_4h = [k["close"] for k in klines_4h]
    price_4h = closes_4h[-1]

    # ─── 1. 波动率检查 ───
    atr_4h = calc_atr(klines_4h, 14)
    atr_pct = atr_4h / price_4h * 100 if price_4h > 0 else 0
    ctx.btc_volatility_pct = atr_pct

    # 计算近期ATR趋势 (近7根 vs 前7根均值)
    if len(klines_4h) >= 28:
        recent_atr = calc_atr(klines_4h[-14:], 7) if len(klines_4h[-14:]) >= 8 else atr_4h
        prior_atr = calc_atr(klines_4h[-28:-14], 7) if len(klines_4h[-28:-14]) >= 8 else atr_4h
        atr_rising = recent_atr > prior_atr * 1.2
    else:
        atr_rising = False

    if atr_pct > 2.0:
        ctx.derisk = True
        ctx.derisk_factor = min(ctx.derisk_factor, 0.4)
        ctx.reasons.append(f"BTC极高波动(ATR%={atr_pct:.1f}%)")
    elif atr_pct > 1.5:
        ctx.derisk = True
        ctx.derisk_factor = min(ctx.derisk_factor, 0.6)
        ctx.reasons.append(f"BTC高波动(ATR%={atr_pct:.1f}%)")
    elif atr_pct > 1.0 and atr_rising:
        ctx.derisk = True
        ctx.derisk_factor = min(ctx.derisk_factor, 0.8)
        ctx.reasons.append(f"BTC波动上升(ATR%={atr_pct:.1f}%)")

    # ─── 2. 趋势结构 ───
    ctx.btc_trend = detect_trend(closes_4h)

    if ctx.btc_trend == "downtrend":
        ctx.derisk = True
        ctx.derisk_factor = min(ctx.derisk_factor, 0.8)  # 原0.6→0.8，不过度压制
        ctx.long_allowed = False
        ctx.reasons.append("BTC空头排列，做多信号仓位打8折")
    elif ctx.btc_trend == "uptrend":
        ctx.short_allowed = False  # 上涨趋势禁止做空
        ctx.reasons.append("BTC多头排列，禁止做空")

    # ─── 3. RSI极端检查 ───
    if klines_1h and len(klines_1h) >= 15:
        closes_1h = [k["close"] for k in klines_1h]
        rsi_1h = calc_rsi(closes_1h, 14)
        ctx.btc_rsi = rsi_1h

        if rsi_1h > 80:
            ctx.derisk = True
            ctx.long_allowed = False
            ctx.derisk_factor = min(ctx.derisk_factor, 0.5)
            ctx.reasons.append(f"BTC 1H RSI超买({rsi_1h:.0f})，不做多")
        elif rsi_1h < 20:
            ctx.derisk = True
            ctx.short_allowed = False
            ctx.derisk_factor = min(ctx.derisk_factor, 0.5)
            ctx.reasons.append(f"BTC 1H RSI超卖({rsi_1h:.0f})，不做空")

    # ─── 4. 连续下跌检查 ───
    if len(closes_4h) >= 12:
        day_closes = closes_4h[-6:]  # 最近6根4h ≈ 1天
        consecutive_down = 0
        for i in range(1, len(day_closes)):
            if day_closes[i] < day_closes[i - 1]:
                consecutive_down += 1
            else:
                consecutive_down = 0
        if consecutive_down >= 4:
            ctx.derisk = True
            ctx.derisk_factor = min(ctx.derisk_factor, 0.5)
            ctx.reasons.append(f"BTC连续{consecutive_down}根4H阴线，超卖风险")

    # 如果没有触发任何条件
    if not ctx.reasons:
        ctx.reasons.append("市场环境正常")

    # ─── 5. OI/G资金费率分析 ───
    if oi_data:
        oi_result = analyze_oi_sentiment(oi_data, ctx.btc_trend)
        ctx.oi_info = oi_result
        if oi_result["signal"] in ("short_boost", "long_boost"):
            ctx.reasons.append(oi_result["reason"])
        elif oi_result["signal"] == "no_chase":
            ctx.reasons.append(oi_result["reason"])
            ctx.derisk = True
            ctx.derisk_factor = min(ctx.derisk_factor, 0.5)

    # ─── 6. 低流动性检测（成交量 + 价差 + 订单簿深度）───
    if klines_1h and len(klines_1h) >= 20 and oi_data:
        liq = check_liquidity(klines_1h, oi_data)
        ctx.liquidity_score = liq["score"]
        if liq["low_liquidity"]:
            ctx.derisk = True
            ctx.derisk_factor = min(ctx.derisk_factor, liq.get("factor", 0.5))
            ctx.reasons.append(liq["reason"])

    # ─── 7. 行情质量评分 (0-10) ───
    score = 5.5  # 默认中性
    if ctx.btc_trend == "uptrend": score += 1.5
    elif ctx.btc_trend == "downtrend": score -= 1.0
    if ctx.btc_volatility_pct < 0.8: score += 0.5
    elif ctx.btc_volatility_pct > 2.0: score -= 1.0
    # RSI
    rsi = ctx.btc_rsi
    if 40 <= rsi <= 60: score += 1.0  # 中性区域最好交易
    elif rsi > 75 or rsi < 25: score -= 1.5  # 极端区域风险高
    # 去风险
    if ctx.derisk: score -= 1.0
    if ctx.derisk_factor < 0.7: score -= 0.5
    # OI信号
    oi_sig = ctx.oi_info.get("signal", "")
    if oi_sig == "short_boost" or oi_sig == "long_boost": score += 0.5
    elif oi_sig == "no_chase": score -= 0.5

    ctx.quality_score = round(max(1, min(10, score)), 1)

    return ctx


def analyze_oi_sentiment(oi_data: dict, price_trend: str = "unknown") -> dict:
    """
    分析OI和资金费率的多空信号（v5新增）

    OI上升+价格下跌+费率偏多 → 散户做多被割，空头信号增强
    OI下跌+价格横盘 → 去杠杆，别追空

    oi_data: {
        "oi": float (当前OI),
        "oi_history": list (历史OI),
        "funding": list (资金费率),
    }
    price_trend: 4h级别趋势 (uptrend/downtrend/ranging/unknown)

    Returns:
        {"signal": "short_boost"/"long_boost"/"no_chase"/"neutral",
         "reason": str, "oi_trend": str, "funding_rate": float}
    """
    result = {
        "signal": "neutral", "reason": "",
        "oi_trend": "flat", "oi_change_pct": 0,
        "funding_rate": 0, "funding_sentiment": "neutral",
    }

    if not oi_data:
        return result

    oi_hist = oi_data.get("oi_history")
    funding = oi_data.get("funding")

    # === OI趋势分析 ===
    if oi_hist and isinstance(oi_hist, list) and len(oi_hist) >= 20:
        try:
            recent = [float(r.get("sumOpenInterest", 0) or 0) for r in oi_hist[-10:]]
            prior = [float(r.get("sumOpenInterest", 0) or 0) for r in oi_hist[-20:-10]]
            if recent and prior and all(recent) and all(prior):
                recent_avg = sum(recent) / len(recent)
                prior_avg = sum(prior) / len(prior)
                oi_change = (recent_avg - prior_avg) / prior_avg * 100 if prior_avg > 0 else 0
                result["oi_trend"] = "rising" if oi_change > 2 else ("falling" if oi_change < -2 else "flat")
                result["oi_change_pct"] = round(oi_change, 1)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # === 资金费率分析 ===
    if funding and isinstance(funding, list) and len(funding) > 0:
        try:
            latest_rate = float(funding[-1].get("fundingRate", 0) or 0)
            result["funding_rate"] = latest_rate
            if latest_rate > 0.0001:
                result["funding_sentiment"] = "positive"   # 多头支付 → 空头有利
            elif latest_rate < -0.0001:
                result["funding_sentiment"] = "negative"   # 空头支付 → 多头有利
        except (TypeError, ValueError):
            pass

    # === 信号规则 ===
    oi_t = result["oi_trend"]
    fs = result["funding_sentiment"]

    # OI升 + 价格跌 + 费率偏多 → 强空头信号
    if oi_t == "rising" and price_trend in ("downtrend", "ranging") and fs == "positive":
        result["signal"] = "short_boost"
        result["reason"] = "OI上升+价格偏弱+费率偏多，空头信号增强"

    # OI升 + 价格涨 + 费率偏空 → 强多头信号
    elif oi_t == "rising" and price_trend in ("uptrend",) and fs == "negative":
        result["signal"] = "long_boost"
        result["reason"] = "OI上升+价格上涨+费率偏空，多头强劲"

    # OI降 + 价格横盘 → 去杠杆，别追
    elif oi_t == "falling" and price_trend == "ranging":
        result["signal"] = "no_chase"
        result["reason"] = "OI下降+价格横盘，去杠杆中不宜追"

    # OI降 + 价格跌 → 多头踩踏，不做多
    elif oi_t == "falling" and price_trend == "downtrend":
        result["signal"] = "no_chase"
        result["reason"] = "OI下降+价格下跌，多头踩踏中"

    # OI升 + 价格涨 + 费率偏多 → 散户FOMO追多，警惕回调
    elif oi_t == "rising" and price_trend == "uptrend" and fs == "positive":
        result["reason"] = "OI上升+费率偏多，散户追多信号，警惕回调"

    return result


def check_liquidity(klines_1h: List[dict], oi_data: dict = None) -> dict:
    """
    低流动性检测 — 成交量 + 价差 + 订单簿综合判断

    低流动性 = 成交量萎缩 + 价差扩大 + 深度不足
    → 容易滑点，不应交易

    Returns:
        score: 0-100 流动性评分（<30=低流动性）
        low_liquidity: True/False
        factor: 去风险乘数
        reason: 说明文字
    """
    result = {"score": 100, "low_liquidity": False, "factor": 1.0, "reason": ""}
    if not klines_1h or len(klines_1h) < 20:
        return result

    vols = [k["volume"] for k in klines_1h]
    if not vols:
        return result

    # 1. 成交量检查：近3根 vs 全部均值
    recent_vol = sum(vols[-3:]) / 3 if len(vols) >= 3 else sum(vols) / len(vols)
    avg_vol = sum(vols) / len(vols) if vols else 1
    vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    # 2. 价差检查（实时订单簿）
    spread_pct = 0.05
    try:
        from order_flow import get_order_book
        ob = get_order_book("BTCUSDT", 5)
        if ob and ob.get("bids") and ob.get("asks"):
            bb = float(ob["bids"][0][0]); ba = float(ob["asks"][0][0])
            mid = (bb + ba) / 2
            if mid > 0: spread_pct = (ba - bb) / mid * 100
    except: pass

    # 3. 综合评分
    vs = min(100, vol_ratio / 1.5 * 100)  # 成交量分
    ss = max(0, 100 - spread_pct * 200)   # 价差分
    result["score"] = round(min(100, vs * 0.5 + ss * 0.5), 1)
    s = result["score"]

    # 4. 判定
    if s < 20:
        result["low_liquidity"] = True; result["factor"] = 0.3
        result["reason"] = f"流动性极低({s:.0f})，量仅为均值{vol_ratio:.0%}"
    elif s < 30:
        result["low_liquidity"] = True; result["factor"] = 0.5
        result["reason"] = f"流动性低({s:.0f})，价差{spread_pct:.3f}%"
    elif s < 50:
        result["low_liquidity"] = True; result["factor"] = 0.7
        result["reason"] = f"流动性偏低({s:.0f})，谨慎交易"

    return result
