# -*- coding: utf-8 -*-
"""
Cipher 订单流分析模块
从 Binance 公开API获取订单簿数据，计算买卖压力
完全免费，无需 API Key

核心指标:
  OFI (Order Flow Imbalance) = (买单总量 - 卖单总量) / (买单总量 + 卖单总量)
  范围 -1 (纯卖压) 到 +1 (纯买压)
"""
from typing import List, Dict, Optional, Tuple
from urllib.request import Request, urlopen
import json
import time

API_TIMEOUT = 5


def api_get(url: str):
    try:
        req = Request(url, headers={"User-Agent": "CipherBot/5.0"})
        with urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def get_order_book(symbol: str = "BTCUSDT", limit: int = 20) -> Optional[dict]:
    """获取订单簿深度数据（免费公开API）"""
    return api_get(f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit={limit}")


def get_recent_trades(symbol: str = "BTCUSDT", limit: int = 50) -> Optional[List[dict]]:
    """获取最近成交记录（判断吃单方向）"""
    return api_get(f"https://api.binance.com/api/v3/trades?symbol={symbol}&limit={limit}")


def calculate_ofi(depth: dict, trades: List[dict]) -> dict:
    """
    综合订单流分析

    Args:
        depth: Binance order book depth response
        trades: Binance recent trades response

    Returns:
        ofi: -1.0 ~ 1.0 (负=卖压，正=买压)
        bid_volume: 买单总量
        ask_volume: 卖单总量
        imbalance_pct: 不平衡百分比
        taker_buy_ratio: 最近吃单买方占比
        whale_alert: 是否有大单异动
        signal: bullish/bearish/neutral
        strength: 0-100 信号强度
    """
    result = {
        "ofi": 0.0, "bid_volume": 0.0, "ask_volume": 0.0,
        "imbalance_pct": 0, "taker_buy_ratio": 50,
        "whale_alert": False, "signal": "neutral", "strength": 0,
    }

    # 计算订单簿不平衡
    if depth and "bids" in depth and "asks" in depth:
        bid_vol = sum(float(b[1]) for b in depth["bids"] if len(b) >= 2)
        ask_vol = sum(float(a[1]) for a in depth["asks"] if len(a) >= 2)
        total = bid_vol + ask_vol

        result["bid_volume"] = round(bid_vol, 4)
        result["ask_volume"] = round(ask_vol, 4)

        if total > 0:
            ofi = (bid_vol - ask_vol) / total
            result["ofi"] = round(ofi, 4)
            result["imbalance_pct"] = round(ofi * 100, 1)

        # 大单检测：单笔挂单超过平均值3倍
        all_qties = [float(b[1]) for b in depth["bids"]] + [float(a[1]) for a in depth["asks"]]
        if all_qties:
            avg_qty = sum(all_qties) / len(all_qties)
            for q in all_qties:
                if q > avg_qty * 5:
                    result["whale_alert"] = True
                    break

    # 计算吃单方向
    if trades and isinstance(trades, list) and len(trades) > 0:
        total_trades = len(trades)
        buy_trades = sum(1 for t in trades if not t.get("isBuyerMaker", True))
        result["taker_buy_ratio"] = round(buy_trades / total_trades * 100, 1)

    # 综合信号判断
    ofi = result["ofi"]
    taker = result["taker_buy_ratio"]

    if ofi > 0.3 and taker > 55:
        result["signal"] = "bullish"
        result["strength"] = min(100, int((ofi + 0.5) * 60 + (taker - 50) * 2))
    elif ofi < -0.3 and taker < 45:
        result["signal"] = "bearish"
        result["strength"] = min(100, int((abs(ofi) + 0.5) * 60 + (50 - taker) * 2))
    elif ofi > 0.1 or taker > 52:
        result["signal"] = "mild_bullish"
        result["strength"] = 40
    elif ofi < -0.1 or taker < 48:
        result["signal"] = "mild_bearish"
        result["strength"] = 40
    else:
        result["signal"] = "neutral"
        result["strength"] = 20

    if result["whale_alert"]:
        result["strength"] = min(100, result["strength"] + 15)

    return result


def analyze(symbol: str = "BTCUSDT") -> dict:
    """一键分析订单流"""
    depth = get_order_book(symbol, 20)
    trades = get_recent_trades(symbol, 50)
    return calculate_ofi(depth, trades)
