# -*- coding: utf-8 -*-
"""
Cipher 技术指标函数库
集中管理所有技术指标计算，避免多模块重复定义。

所有函数保持纯计算，不依赖外部模块（除 typing 外）。
"""
from typing import List


def calc_sma(values: List[float], period: int) -> float:
    """简单移动平均"""
    if len(values) < period:
        return sum(values) / len(values) if values else 0
    return sum(values[-period:]) / period


def calc_ema(values: List[float], period: int) -> float:
    """指数移动平均"""
    if len(values) < period:
        return sum(values) / len(values) if values else 0
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def calc_rsi(closes: List[float], period: int = 14) -> float:
    """相对强弱指标 (RSI)"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = 0, 0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr(klines: List[dict], period: int = 14) -> float:
    """平均真实波幅 (ATR)"""
    if len(klines) < period + 1:
        if not klines:
            return 0
        return (max(k["high"] for k in klines) - min(k["low"] for k in klines)) / max(len(klines), 1)
    trs = []
    for i in range(-period, 0):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def calc_local_atr(klines: List[dict], n: int = 3) -> float:
    """计算最近N根K线的局部波幅"""
    if len(klines) < n:
        return 0
    ranges = [abs(k["high"] - k["low"]) for k in klines[-n:]]
    return sum(ranges) / len(ranges)


def calc_vwap(klines: List[dict]) -> float:
    """成交量加权均价（VWAP）— 机构参考线

    价格 > VWAP = 偏强，价格 < VWAP = 偏弱
    """
    if not klines:
        return 0
    total_pv = sum(k["close"] * k["volume"] for k in klines)
    total_v = sum(k["volume"] for k in klines)
    return total_pv / total_v if total_v > 0 else 0


def calc_bollinger_bands(klines: List[dict], period: int = 20, std: float = 2.0) -> dict:
    """布林带 — 波动率+超买超卖

    Returns:
        middle: 中轨(SMA)
        upper: 上轨
        lower: 下轨
        bandwidth: 带宽%，<10% = 挤压（变盘前兆）
        position: 价格在中轨的相对位置 0-100
    """
    if len(klines) < period:
        return {"middle": 0, "upper": 0, "lower": 0, "bandwidth": 0, "position": 50}
    closes = [k["close"] for k in klines[-period:]]
    sma = sum(closes) / period
    variance = sum((c - sma) ** 2 for c in closes) / period
    sd = variance ** 0.5
    upper = sma + std * sd
    lower = sma - std * sd
    bandwidth = (upper - lower) / sma * 100 if sma > 0 else 0
    price = closes[-1]
    position = (price - lower) / (upper - lower) * 100 if upper > lower else 50
    return {
        "middle": round(sma, 1), "upper": round(upper, 1), "lower": round(lower, 1),
        "bandwidth": round(bandwidth, 2), "position": round(position, 1),
    }
