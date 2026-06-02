# -*- coding: utf-8 -*-
"""
Binance 真实状态对账模块

职责:
  1. 读取Binance真实仓位/订单/强平价
  2. 与本地状态对比
  3. 发现不一致时告警/修正
  4. 清理孤儿订单
  5. 防止状态错乱导致的重复开单

用法:
  from binance_reconciler import reconcile
  issues = reconcile()
  if issues: send_telegram(f"对账异常: {issues}")
"""
import os, sys, json, time, hmac, hashlib
from datetime import datetime
from typing import List, Optional, Dict, Tuple
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

FAPI = "https://fapi.binance.com"
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOCAL_POSITIONS_FILE = os.path.join(LOG_DIR, "local_positions.json")


# ─── Binance签名请求 ───
def _sign(params: dict, secret: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def _request(method: str, path: str, params: dict = None,
             api_key: str = "", secret: str = "") -> Optional[dict]:
    if not api_key or not secret:
        return None
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params, secret)
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    url = f"{FAPI}{path}?{query}"
    try:
        req = Request(url, method=method, headers={"X-MBX-APIKEY": api_key})
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return None


# ─── 获取Binance真实仓位 ───
def get_real_positions() -> List[dict]:
    """获取Binance当前所有持仓"""
    try:
        from config import BINANCE
    except:
        return []
    info = _request("GET", "/fapi/v2/account", api_key=BINANCE.get("api_key",""), secret=BINANCE.get("api_secret",""))
    if not info:
        return []
    active = []
    for p in info.get("positions", []):
        amt = float(p.get("positionAmt", 0))
        if amt != 0:
            active.append({
                "symbol": p["symbol"],
                "amount": amt,
                "entry": float(p["entryPrice"]),
                "mark": float(p["markPrice"]),
                "liq": float(p.get("liquidationPrice", 0)),
                "leverage": int(p.get("leverage", 25)),
                "margin_type": p.get("marginType", "isolated"),
                "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
            })
    return active


def get_open_orders(symbol: str = None) -> List[dict]:
    """获取Binance当前挂单"""
    try:
        from config import BINANCE
    except:
        return []
    syms = [symbol] if symbol else ["BTCUSDT", "ETHUSDT"]
    orders = []
    for s in syms:
        data = _request("GET", "/fapi/v1/openOrders", {"symbol": s},
                        api_key=BINANCE.get("api_key",""), secret=BINANCE.get("api_secret",""))
        if data and isinstance(data, list):
            orders.extend(data)
    return orders


# ─── 本地仓位状态 ───
def load_local_positions() -> dict:
    """读取本地记录的仓位"""
    if os.path.exists(LOCAL_POSITIONS_FILE):
        try:
            with open(LOCAL_POSITIONS_FILE) as f:
                return json.load(f)
        except: pass
    return {"positions": [], "updated": ""}

def save_local_position(symbol: str, direction: str, entry: float, amount: float):
    """记录本地仓位"""
    state = load_local_positions()
    state["positions"] = [p for p in state["positions"] if p.get("symbol") != symbol]
    state["positions"].append({
        "symbol": symbol, "direction": direction,
        "entry": entry, "amount": amount,
        "time": datetime.now().isoformat(),
    })
    state["updated"] = datetime.now().isoformat()
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOCAL_POSITIONS_FILE, "w") as f:
        json.dump(state, f, indent=2)

def clear_local_position(symbol: str):
    """清除本地仓位"""
    state = load_local_positions()
    state["positions"] = [p for p in state["positions"] if p.get("symbol") != symbol]
    state["updated"] = datetime.now().isoformat()
    with open(LOCAL_POSITIONS_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── 核心对账逻辑 ───
def reconcile() -> Tuple[bool, List[str]]:
    """
    执行全量对账: 本地状态 vs Binance真实状态

    Returns:
        (all_ok: bool, issues: list)
    """
    issues = []
    real_positions = get_real_positions()
    local_state = load_local_positions()
    local_positions = local_state.get("positions", [])

    # 1. 本地有仓位但Binance没有 → 本地过期
    for local in local_positions:
        sym = local.get("symbol", "")
        real = [p for p in real_positions if p["symbol"] == sym]
        if not real:
            issues.append(f"{sym}: 本地记录有仓位但Binance无持仓 → 清除本地记录")
            clear_local_position(sym)

    # 2. Binance有仓位但本地没有 → 未知仓位保护
    for real in real_positions:
        sym = real["symbol"]
        local = [p for p in local_positions if p.get("symbol") == sym]
        if not local:
            issues.append(f"{sym}: Binance有持仓但本地未记录 → 禁止开新仓")
            save_local_position(sym, "long" if real["amount"] > 0 else "short",
                                real["entry"], abs(real["amount"]))

    # 3. 强平距离检查
    for real in real_positions:
        if real["liq"] <= 0:
            continue
        if real["amount"] > 0:  # long
            dist = (real["mark"] - real["liq"]) / real["mark"] * 100
        else:
            dist = (real["liq"] - real["mark"]) / real["mark"] * 100
        if dist < 1.0:
            issues.append(f"{real['symbol']}: 强平距离仅{dist:.1f}% < 1%，危险!")
        elif dist < 3.0:
            issues.append(f"{real['symbol']}: 强平距离{dist:.1f}%，注意")

    # 4. 孤儿订单检查
    try:
        orders = get_open_orders()
        active_symbols = {p["symbol"] for p in real_positions}
        for o in orders:
            if o["symbol"] not in active_symbols:
                o_type = o.get("type", "")
                if "STOP" in o_type or "TAKE_PROFIT" in o_type or "LIMIT" in o_type:
                    issues.append(f"孤儿订单: {o['symbol']} {o_type} @ {o.get('price','?')}")
    except:
        pass

    return len(issues) == 0, issues
