# -*- coding: utf-8 -*-
"""
Cipher VRVP — 成交量分布分析（Volume Range Visible Profile）
基于现有15m K线数据计算，无需额外API

核心概念:
  POC (Point of Control) = 成交量最大的价格 = 价格磁铁
  价值区间 (Value Area) = 70%成交量覆盖的价格范围
  HVN (高量节点) = 强支撑/阻力
  LVN (低量节点) = 价格快速通过区

用法:
  from vrvp import calculate_vrvp, describe_vrvp
  vrvp = calculate_vrvp(klines_15m)
  # 返回: {poc, va_high, va_low, current_position, signal}
"""
from typing import List, Optional, Dict


def calculate_vrvp(klines: List[dict], num_bins: int = 50) -> Optional[Dict]:
    """
    计算成交量分布

    Args:
        klines: OHLCV K线列表（至少20根）
        num_bins: 价格区间分割数

    Returns:
        poc: 控制点（最高量价格）
        va_high: 价值区间上沿
        va_low: 价值区间下沿
        current_price: 当前价格
        current_position: 当前价格在价值区间的相对位置
          - "above_va": 在价值区间上方 → 偏强
          - "in_va": 在价值区间内 → 中性
          - "below_va": 在价值区间下方 → 偏弱
          - "near_poc": 在POC附近 → 关键位置
        signal: 基于VRVP的倾向
        strength: 0-100
    """
    if not klines or len(klines) < 10:
        return None

    price = klines[-1]["close"]

    # 价格范围
    min_price = min(k["low"] for k in klines)
    max_price = max(k["high"] for k in klines)
    price_range = max_price - min_price

    if price_range <= 0:
        return None

    bin_size = price_range / num_bins
    bins = [0.0] * num_bins

    # 将每根K线的成交量分摊到价格区间
    for k in klines:
        lo = k["low"]
        hi = k["high"]
        vol = k["volume"]
        if vol <= 0 or hi <= lo:
            continue

        start = max(0, int((lo - min_price) / bin_size))
        end = min(num_bins - 1, int((hi - min_price) / bin_size))
        n = end - start + 1

        if n > 0:
            vol_per = vol / n
            for i in range(start, end + 1):
                bins[i] += vol_per

    if max(bins) == 0:
        return None

    # POC: 量最大的价格区间
    poc_bin = bins.index(max(bins))
    poc_price = min_price + (poc_bin + 0.5) * bin_size

    # 价值区间: 从POC往外扩，覆盖70%总成交量
    total_vol = sum(bins)
    target_vol = total_vol * 0.7

    cum = bins[poc_bin]
    l = poc_bin - 1
    r = poc_bin + 1
    while cum < target_vol and (l >= 0 or r < num_bins):
        lv = bins[l] if l >= 0 else 0
        rv = bins[r] if r < num_bins else 0
        if lv >= rv:
            cum += lv
            l -= 1
        else:
            cum += rv
            r += 1

    va_high = min_price + min(num_bins - 1, r - 1) * bin_size
    va_low = min_price + max(0, l + 1) * bin_size

    # 当前价格的位置
    pos = "in_va"
    if price > va_high:
        pos = "above_va"
    elif price < va_low:
        pos = "below_va"

    # POC附近 (±0.15%)
    near_poc = abs(price - poc_price) / price * 100 < 0.15
    if near_poc:
        pos = "near_poc"

    # 信号判断
    signal = "neutral"
    strength = 30

    if pos == "above_va":
        signal = "mild_bullish"
        strength = 55
    elif pos == "below_va":
        signal = "mild_bearish"
        strength = 55
    elif pos == "near_poc":
        signal = "key_level"
        strength = 70

    # 趋势加强：如果价格在价值区间外且远离POC
    poc_dist = abs(price - poc_price) / price * 100
    if poc_dist > 0.5 and pos in ("above_va", "below_va"):
        strength = min(85, strength + 20)

    return {
        "poc": round(poc_price, 1),
        "va_high": round(va_high, 1),
        "va_low": round(va_low, 1),
        "current_price": round(price, 1),
        "current_position": pos,
        "near_poc": near_poc,
        "poc_distance_pct": round(poc_dist, 2),
        "signal": signal,
        "strength": strength,
    }


def describe(vrvp: Optional[Dict]) -> str:
    """生成VRVP状态文本"""
    if not vrvp:
        return "VRVP: 数据不足"
    pos_map = {
        "above_va": "价值区上方📈",
        "below_va": "价值区下方📉",
        "in_va": "价值区内⚖️",
        "near_poc": "POC附近🎯",
    }
    p = pos_map.get(vrvp["current_position"], "?")
    return (f"POC ${vrvp['poc']} 价值区 ${vrvp['va_low']}-${vrvp['va_high']} "
            f"| 当前位置 {p}")
