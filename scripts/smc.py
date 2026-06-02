# -*- coding: utf-8 -*-
"""
Cipher SMC 模块 — Smart Money Concepts 分析
当前包含:
  - FVG (Fair Value Gap) 公平价值缺口检测
  - 价格与FVG区域的关系判定
后续可扩展: Order Block, BOS/CHoCH, Liquidity Sweep
"""
from typing import List, Dict, Optional, Tuple


def detect_fvg(klines: List[dict], join_consecutive: bool = False) -> List[dict]:
    """
    检测 Fair Value Gap (FVG) — 三根K线的价格不平衡区间

    看涨FVG: 第1根高点 < 第3根低点 → 中间留下未成交缺口
    看跌FVG: 第1根低点 > 第3根高点

    Args:
        klines: OHLCV K线数据列表
        join_consecutive: 是否合并连续的同向FVG

    Returns:
        FVG列表，每项包含 type/top/bottom/mid/index/mitigated
    """
    fvg_list = []
    n = len(klines)

    for i in range(2, n):
        prev_high = klines[i - 2]["high"]
        prev_low = klines[i - 2]["low"]
        curr_high = klines[i]["high"]
        curr_low = klines[i]["low"]
        curr_close = klines[i]["close"]
        curr_open = klines[i]["open"]

        # 看涨FVG: 前高 < 现低 且 当前阳线
        if prev_high < curr_low and curr_close > curr_open:
            top = curr_low
            bottom = prev_high
            mid = (top + bottom) / 2
            fvg_list.append({
                "type": "bullish",
                "top": top,
                "bottom": bottom,
                "mid": mid,
                "index": i,
                "time": klines[i]["time"],
                "mitigated": False,
                "mitigated_index": None,
            })

        # 看跌FVG: 前低 > 现高 且 当前阴线
        elif prev_low > curr_high and curr_close < curr_open:
            top = prev_low
            bottom = curr_high
            mid = (top + bottom) / 2
            fvg_list.append({
                "type": "bearish",
                "top": top,
                "bottom": bottom,
                "mid": mid,
                "index": i,
                "time": klines[i]["time"],
                "mitigated": False,
                "mitigated_index": None,
            })

    # 合并连续同向FVG
    if join_consecutive and len(fvg_list) >= 2:
        merged = [fvg_list[0]]
        for fvg in fvg_list[1:]:
            last = merged[-1]
            if fvg["type"] == last["type"]:
                last["top"] = max(last["top"], fvg["top"])
                last["bottom"] = min(last["bottom"], fvg["bottom"])
                last["mid"] = (last["top"] + last["bottom"]) / 2
                last["index"] = fvg["index"]
                last["time"] = fvg["time"]
            else:
                merged.append(fvg)
        fvg_list = merged

    return fvg_list


def update_mitigation(fvg_list: List[dict], klines: List[dict]) -> List[dict]:
    """
    更新FVG的填补状态 — 价格返回FVG区域 = 部分或完全填补

    Args:
        fvg_list: 之前检测到的FVG列表
        klines: 从FVG时刻之后的所有K线

    Returns:
        更新mitigated状态的FVG列表
    """
    for fvg in fvg_list:
        if fvg["mitigated"]:
            continue
        fvg_start_idx = fvg["index"]

        for j in range(fvg_start_idx, len(klines)):
            k = klines[j]
            if fvg["type"] == "bullish":
                # 看涨FVG被填补：价格跌到FVG区域内
                if k["low"] <= fvg["top"]:
                    fvg["mitigated"] = True
                    fvg["mitigated_index"] = j
                    break
            else:
                # 看跌FVG被填补：价格涨到FVG区域内
                if k["high"] >= fvg["bottom"]:
                    fvg["mitigated"] = True
                    fvg["mitigated_index"] = j
                    break

    return fvg_list


def is_price_in_fvg(price: float, fvg_list: List[dict],
                    direction: str, tolerance_pct: float = 0.15) -> Tuple[bool, Optional[dict]]:
    """
    判断价格是否在一个未填补的FVG区域内（或紧邻0.15%以内）

    做多时匹配看涨FVG，做空时匹配看跌FVG

    Returns:
        (in_zone: bool, matched_fvg: dict or None)
    """
    for fvg in fvg_list:
        if fvg["mitigated"]:
            continue

        if direction == "long" and fvg["type"] == "bullish":
            zone_top = fvg["top"] * (1 + tolerance_pct / 100)
            zone_bottom = fvg["bottom"] * (1 - tolerance_pct / 100)
            if zone_bottom <= price <= zone_top:
                return True, fvg

        elif direction == "short" and fvg["type"] == "bearish":
            zone_top = fvg["top"] * (1 + tolerance_pct / 100)
            zone_bottom = fvg["bottom"] * (1 - tolerance_pct / 100)
            if zone_bottom <= price <= zone_top:
                return True, fvg

    return False, None


def find_nearest_fvg(price: float, fvg_list: List[dict],
                     direction: str, max_distance_pct: float = 0.5) -> Optional[dict]:
    """
    找到距离价格最近的未填补FVG（用于显示距离信息）

    Returns:
        最近的FVG dict, 或 None
    """
    nearest = None
    nearest_dist = float("inf")

    for fvg in fvg_list:
        if fvg["mitigated"]:
            continue

        if direction == "long" and fvg["type"] != "bullish":
            continue
        if direction == "short" and fvg["type"] != "bearish":
            continue

        if direction == "long":
            dist = max(0, fvg["bottom"] - price) / price * 100
        else:
            dist = max(0, price - fvg["top"]) / price * 100

        if dist < nearest_dist:
            nearest_dist = dist
            nearest = fvg

    if nearest and nearest_dist <= max_distance_pct:
        return nearest
    return None


def describe_fvg_state(price: float, fvg_list: List[dict]) -> str:
    """生成FVG状态文本（用于Telegram推送）"""
    bullish_unmitigated = [f for f in fvg_list if f["type"] == "bullish" and not f["mitigated"]]
    bearish_unmitigated = [f for f in fvg_list if f["type"] == "bearish" and not f["mitigated"]]

    parts = []
    if bullish_unmitigated:
        parts.append(f"看涨FVGx{len(bullish_unmitigated)}")
    if bearish_unmitigated:
        parts.append(f"看跌FVGx{len(bearish_unmitigated)}")

    # 价格是否在某个FVG内
    in_fvg_bull, fvg_bull = is_price_in_fvg(price, fvg_list, "long")
    in_fvg_bear, fvg_bear = is_price_in_fvg(price, fvg_list, "short")

    if in_fvg_bull:
        parts.append("🟢价格在看涨FVG内")
    if in_fvg_bear:
        parts.append("🔴价格在看跌FVG内")

    return " | ".join(parts) if parts else "无活跃FVG"
