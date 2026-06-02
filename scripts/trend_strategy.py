# -*- coding: utf-8 -*-
"""
趋势策略模块 — 独立的主利润来源

职责:
  判断趋势方向、强度、回踩质量
  输出: trend_direction / strength / entry_zone / invalid_level

使用:
  from trend_strategy import evaluate_trend
  result = evaluate_trend(klines_1h, klines_4h, price, rsi_1h, atr_1h, structure)
"""
from typing import Optional, Dict
from indicators import calc_ema, calc_rsi, calc_atr


def evaluate_trend(klines_1h: list, klines_4h: list,
                   price: float, rsi_1h: float,
                   atr_1h: float, structure: dict) -> dict:
    """
    评估趋势状态

    Returns:
        direction: "long"/"short"/"neutral"
        strength: 0-100
        ema_alignment: 1=bullish, -1=bearish, 0=mixed
        pullback_quality: "excellent"/"good"/"poor"/"none"
        entry_zone: (entry_min, entry_max)
        invalid_level: 趋势失效价
        reason: 说明
    """
    result = {
        "direction": "neutral", "strength": 0,
        "ema_alignment": 0, "pullback_quality": "none",
        "entry_zone": (0, 0), "invalid_level": 0,
        "reason": "",
    }
    if not klines_4h or len(klines_4h) < 20:
        return result

    c4 = [k["close"] for k in klines_4h]
    c1 = [k["close"] for k in klines_1h] if klines_1h and len(klines_1h) >= 20 else c4

    ema20_4h = calc_ema(c4, 20)
    ema50_4h = calc_ema(c4, min(50, len(c4)))
    ema20_1h = calc_ema(c1, 20)

    # EMA排列判断趋势
    if price > ema20_4h > ema50_4h:
        result["direction"] = "long"
        result["strength"] = min(100, 50 + int((price - ema20_4h) / ema20_4h * 100 * 2))
        result["ema_alignment"] = 1
        result["reason"] = "EMA20>EMA50多头排列"
        result["invalid_level"] = round(ema50_4h, 1)
    elif price < ema20_4h < ema50_4h:
        result["direction"] = "short"
        result["strength"] = min(100, 50 + int((ema20_4h - price) / ema20_4h * 100 * 2))
        result["ema_alignment"] = -1
        result["reason"] = "EMA20<EMA50空头排列"
        result["invalid_level"] = round(ema50_4h, 1)
    else:
        return result  # 无趋势

    # 回踩质量
    if result["direction"] == "long" and len(c1) >= 10:
        recent_low = min(c1[-10:])
        pullback = (price - recent_low) / recent_low * 100
        if pullback < 0.5 and rsi_1h > 40:
            result["pullback_quality"] = "excellent"
        elif pullback < 1.0:
            result["pullback_quality"] = "good"
        else:
            result["pullback_quality"] = "poor"
    elif result["direction"] == "short" and len(c1) >= 10:
        recent_high = max(c1[-10:])
        pullback = (recent_high - price) / recent_high * 100
        if pullback < 0.5 and rsi_1h < 60:
            result["pullback_quality"] = "excellent"
        elif pullback < 1.0:
            result["pullback_quality"] = "good"
        else:
            result["pullback_quality"] = "poor"

    # 入场区间
    if result["direction"] == "long":
        result["entry_zone"] = (round(price - atr_1h * 0.5, 1), round(price, 1))
    else:
        result["entry_zone"] = (round(price, 1), round(price + atr_1h * 0.5, 1))

    return result
