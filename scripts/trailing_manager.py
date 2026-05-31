#!/usr/bin/env python3
"""
CipherBot 仓位管理器 — 持仓查询 + 趋势延续加仓 + 追踪止损建议

职责:
  1. 查询币安持仓状态（只读）
  2. 判断是否满足加仓条件
  3. 推送持仓告警到Telegram
  4. 趋势延续时自动加仓

用法:
  python3 trailing_manager.py           # 检查持仓并推送报告
  python3 trailing_manager.py check_add # 检查所有币种是否可加仓
"""
import sys, os, json, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BINANCE, PAIRS
from binance_account import get_account_info
from cipher_bot import calc_atr, get_klines, calc_ema, detect_market_structure, logger, send_telegram, send_cornix

ADD_PCT = 15  # 加仓仓位(%)

def get_position(symbol="BTCUSDT"):
    """查询指定币种的当前持仓"""
    info = get_account_info(BINANCE.get("api_key",""), BINANCE.get("api_secret",""))
    if not info:
        return {"has_position": False}
    for p in info.get("positions", []):
        if p["symbol"] == symbol:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                side = "long" if amt > 0 else "short"
                pnl_pct = (float(p["markPrice"])-float(p["entryPrice"]))/float(p["entryPrice"])*100
                if side == "short":
                    pnl_pct = -pnl_pct
                return {"has_position":True, "direction":side, "amount":abs(amt),
                        "entry":float(p["entryPrice"]), "mark":float(p["markPrice"]),
                        "pnl_pct": round(pnl_pct,2), "leverage":int(p.get("leverage",25))}
    return {"has_position": False}

def check_can_add(symbol="BTCUSDT"):
    """趋势延续 → 可加仓？"""
    pos = get_position(symbol)
    if not pos["has_position"] or pos["pnl_pct"] < 0:
        return False
    k1h = get_klines(symbol, "1h", 30)
    if not k1h:
        return False
    c = [k["close"] for k in k1h]
    struct = detect_market_structure(k1h)
    ema21 = calc_ema(c, 21)
    price = pos["mark"]
    cond = 0
    if pos["direction"]=="long" and struct["direction"]=="bullish": cond+=1
    if pos["direction"]=="short" and struct["direction"]=="bearish": cond+=1
    if pos["direction"]=="long" and price>ema21: cond+=1
    if pos["direction"]=="short" and price<ema21: cond+=1
    if pos["pnl_pct"] > 0.5: cond+=1
    return cond >= 2

def check():
    """检查持仓并推送"""
    info = get_account_info(BINANCE.get("api_key",""), BINANCE.get("api_secret",""))
    if not info:
        return
    active = [p for p in info.get("positions",[]) if float(p.get("positionAmt",0))!=0]
    if not active:
        return
    for p in active:
        sym = p["symbol"]
        entry = float(p["entryPrice"])
        mark = float(p["markPrice"])
        amt = float(p["positionAmt"])
        pnl = float(p["unRealizedProfit"])
        side = "多头🟢" if amt>0 else "空头🔴"
        pnl_pct = (mark-entry)/entry*100 if amt>0 else (entry-mark)/entry*100
        k15 = get_klines(sym,"15m",20)
        atr = calc_atr(k15,14) if k15 else 0
        trailing_activation = max(0.5, pnl_pct*0.5)
        send_telegram(
            f"📊 *持仓状态*\n{side} {sym} | {abs(amt):.4f}张\n"
            f"入场 ${entry:.0f} → 当前 ${mark:.0f}\n"
            f"盈亏 {pnl_pct:+.2f}% (${pnl:+.2f})\nATR ${atr:.1f}\n\n"
            f"追踪止损建议: 激活 +{trailing_activation:.1f}% / 深度 0.4%"
        )

def check_add():
    """检查所有币种是否可加仓"""
    for sym, pc in PAIRS.items():
        if not pc.get("enabled"):
            continue
        if check_can_add(sym):
            pos = get_position(sym)
            pn = pc.get("name", sym)
            logger.info(f"✅ {pn} 趋势延续→加仓")
            sig = {"direction":pos["direction"], "entry":pos["mark"], "stop_loss":0,
                   "target":0, "stop_pct":0.5, "target_pct":0, "rr":0, "score":80,
                   "amount_pct":ADD_PCT, "pattern":f"趋势延续加仓({pn})",
                   "pair":pn, "symbol":sym, "leverage":pc.get("leverage",25),
                   "reasons":[f"趋势延续确认"], "risks":[]}
            send_telegram(
                f"📈 *{pn} 趋势延续加仓*\n"
                f"{'做多🟢' if pos['direction']=='long' else '做空🔴'}\n"
                f"当前 ${pos['mark']:.0f} | 浮盈 +{pos['pnl_pct']:.1f}%\n"
                f"加仓 {ADD_PCT}% | 追踪止损保护全部仓位"
            )
            send_cornix(sig)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv)>1 else "check"
    {"check": check, "check_add": check_add}.get(mode, check)()
