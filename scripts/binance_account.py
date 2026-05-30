#!/usr/bin/env python3
"""
币安合约账户查询模块 — 查仓位/余额/订单
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Optional, Dict, List
from urllib.request import Request, urlopen

logger = logging.getLogger("Cipher")

FAPI = "https://fapi.binance.com"

def _sign(query_string: str, secret: str) -> str:
    return hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def _request(method: str, path: str, params: dict = None, api_key: str = "", secret: str = "") -> Optional[dict]:
    if not api_key or not secret:
        return None
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    query += f"&signature={_sign(query, secret)}"
    url = f"{FAPI}{path}?{query}"
    try:
        req = Request(url, method=method, headers={"X-MBX-APIKEY": api_key})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"币安API失败 [{path}]: {e}")
        return None

def get_account_info(api_key: str, secret: str) -> Optional[dict]:
    """账户信息（含持仓）"""
    data = _request("GET", "/fapi/v2/account", api_key=api_key, secret=secret)
    if data:
        positions = [p for p in data.get("positions", []) if float(p.get("positionAmt", 0)) != 0]
        return {
            "total_wallet": float(data.get("totalWalletBalance", 0)),
            "total_unrealized": float(data.get("totalUnrealizedProfit", 0)),
            "positions": [{
                "symbol": p["symbol"],
                "amount": float(p["positionAmt"]),
                "entry": float(p["entryPrice"]),
                "mark": float(p["markPrice"]),
                "unrealized": float(p["unRealizedProfit"]),
                "leverage": int(p["leverage"]),
                "liquidation": float(p.get("liquidationPrice", 0)),
            } for p in positions],
        }
    return data

def get_open_orders(symbol: str, api_key: str, secret: str) -> Optional[List[dict]]:
    """当前挂单"""
    data = _request("GET", f"/fapi/v1/openOrders", {"symbol": symbol}, api_key, secret)
    return data if isinstance(data, list) else None

def format_positions(api_key: str, secret: str) -> str:
    """格式化仓位信息"""
    info = get_account_info(api_key, secret)
    if info is None:
        return "❌ 未配置币安API密钥\n请在 `/positions` 前先配置 Binance API Key"
    if not info:
        return "❌ 查询失败（API可能无权限）"

    lines = [f"💰 *币安合约账户*\n━━━━━━━━━━━"]
    lines.append(f"钱包余额：${info['total_wallet']:,.2f}")
    lines.append(f"未实现盈亏：${info['total_unrealized']:,.2f}")
    pnl = info['total_unrealized']
    lines.append(f"总权益：${info['total_wallet']+pnl:,.2f}")

    pos = info.get("positions", [])
    if not pos:
        lines.append("\n📭 *当前无持仓*")
    else:
        lines.append(f"\n📊 *持仓 ({len(pos)}个)*")
        for p in pos:
            direction = "🟢做多" if p["amount"] > 0 else "🔴做空"
            pnl_pct = (p["mark"] - p["entry"]) / p["entry"] * 100
            if p["amount"] < 0:
                pnl_pct = -pnl_pct
            lines.append(f"\n{direction} {p['symbol']}")
            lines.append(f"  数量：{abs(p['amount']):.4f}")
            lines.append(f"  开仓：${p['entry']:.1f} → 当前 ${p['mark']:.1f}")
            lines.append(f"  浮动盈亏：${p['unrealized']:+.2f}（{pnl_pct:+.2f}%）")
            lines.append(f"  杠杆：{p['leverage']}x")
            if p['liquidation']:
                lines.append(f"  强平价：${p['liquidation']:.1f}")
    return "\n".join(lines)

def format_orders(api_key: str, secret: str) -> str:
    """格式化挂单信息"""
    orders = get_open_orders("BTCUSDT", api_key, secret)
    if orders is None:
        return "❌ 未配置币安API密钥"
    if not orders:
        return "📭 *当前无挂单*"
    lines = [f"📋 *BTCUSDT 挂单 ({len(orders)}个)*"]
    for o in orders:
        side = "🟢买入" if o["side"] == "BUY" else "🔴卖出"
        lines.append(f"\n{side} {o['origQty']}张 @ ${float(o['price']):.1f}")
        lines.append(f"  类型：{o['type']} | 状态：{o['status']}")
    return "\n".join(lines)
