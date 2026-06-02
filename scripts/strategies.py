# -*- coding: utf-8 -*-
"""
Cipher 多策略引擎 — 趋势/震荡/剥头皮 并行

策略选择规则（基于行情模式）:
  TRENDING_BULL → 趋势做多
  TRENDING_BEAR → 趋势做空 + 破位做空
  RANGING       → 震荡高抛低吸（均值回归）
  VOLATILE      → 剥头皮（OFI快速进出）
"""
from typing import List, Dict, Optional, Tuple
import logging
logger = logging.getLogger("Cipher")


# ═══════════════════════════════════════════════
# 1. 趋势策略（现有逻辑封装）
# ═══════════════════════════════════════════════

def trend_strategy(price, ticker_24h, klines_15m, klines_1h, klines_4h,
                   rsi_1h, atr_1h, structure, base_stop_atr,
                   regime_params, market_regime) -> Optional[dict]:
    """
    趋势策略 — 顺大势，逆小势
    在上升趋势做多，下降趋势做空
    """
    # 由 find_trading_signal 处理，此函数为空壳
    # 保持架构一致，实际逻辑在 cipher_bot.py 中
    return None


# ═══════════════════════════════════════════════
# 2. 震荡策略（均值回归）
# ═══════════════════════════════════════════════

def ranging_strategy(price: float, vrvp: dict, klines_15m: List[dict],
                     rsi_15m: float, atr_1h: float,
                     regime_label: str) -> Optional[dict]:
    """
    震荡策略 — 价值区上下沿高抛低吸

    条件:
      - 行情模式为震荡或未知
      - VRVP价值区可用
      - 价格在价值区上沿/下沿

    入场:
      价格≤VAL → 做多（价值区下沿买）
      价格≥VAH → 做空（价值区上沿卖）

    止损:
      做多: VAL下方半倍ATR
      做空: VAH上方半倍ATR

    目标:
      对侧价值区边界
    """
    if "震荡" not in regime_label and "未知" not in regime_label:
        return None
    if not vrvp or not vrvp.get("va_low") or not vrvp.get("va_high"):
        return None

    val = vrvp["va_low"]
    vah = vrvp["va_high"]
    poc = vrvp.get("poc", (val + vah) / 2)
    range_width = (vah - val) / price * 100

    # 区间太窄不做（<1%没空间）
    if range_width < 1.0:
        return None

    # 震荡策略参数
    min_rr = 2.0
    max_stop = 0.8
    lev = 10
    base_score = 65

    # 接近价值区下沿 → 做多
    if price <= val * 1.003 and rsi_15m < 50:
        stop = val - atr_1h * 0.5
        stop_pct = (price - stop) / price * 100 if price > stop else 0.6
        stop_pct = max(stop_pct, 0.4)
        target = vah
        target_pct = (target - price) / price * 100
        rr = target_pct / stop_pct if stop_pct > 0 else 0
        if stop_pct < max_stop and rr >= min_rr:
            return {
                "direction": "long", "entry": price,
                "stop_loss": round(stop, 1), "target": round(target, 1),
                "stop_pct": round(stop_pct, 2), "target_pct": round(target_pct, 2),
                "rr": round(rr, 2), "pattern": "震荡做多(价值区下沿)",
                "score": base_score, "leverage": lev,
                "reasons": [f"价值区下沿反弹 VAL=${val:.0f}"], "risks": [],
                "key_level": round(val, 1),
            }

    # 接近价值区上沿 → 做空
    if price >= vah * 0.997 and rsi_15m > 50:
        stop = vah + atr_1h * 0.5
        stop_pct = (stop - price) / price * 100 if stop > price else 0.6
        stop_pct = max(stop_pct, 0.4)
        target = val
        target_pct = (price - target) / price * 100
        rr = target_pct / stop_pct if stop_pct > 0 else 0
        if stop_pct < max_stop and rr >= min_rr:
            return {
                "direction": "short", "entry": price,
                "stop_loss": round(stop, 1), "target": round(target, 1),
                "stop_pct": round(stop_pct, 2), "target_pct": round(target_pct, 2),
                "rr": round(rr, 2), "pattern": "震荡做空(价值区上沿)",
                "score": base_score, "leverage": lev,
                "reasons": [f"价值区上沿回落 VAH=${vah:.0f}"], "risks": [],
                "key_level": round(vah, 1),
            }

    return None


# ═══════════════════════════════════════════════
# 3. 剥头皮策略（OFI订单流快速进出）
# ═══════════════════════════════════════════════

def scalping_strategy(price: float, ofi_info: dict, klines_15m: List[dict],
                      regime_label: str) -> Optional[dict]:
    """
    剥头皮策略 — OFI极值时快速进出

    条件:
      - OFI > 0.6 (强买压) 或 OFI < -0.6 (强卖压)
      - 价格不在价值区极端位置

    入场:
      OFI>0.6 → 做多（跟买）
      OFI<-0.6 → 做空（跟卖）

    止损:
      半倍15m ATR，约0.15%

    目标:
      0.25-0.4%，固定止盈

    杠杆:
      15x（剥头皮风险可控，杠杆可稍高）
    """
    if not ofi_info:
        return None

    ofi = ofi_info.get("ofi", 0)
    strength = ofi_info.get("strength", 0)

    # 需要强信号才剥头皮
    if abs(ofi) < 0.5 or strength < 50:
        return None

    # 计算15m ATR
    if len(klines_15m) < 14:
        return None
    trs = []
    for i in range(-14, 0):
        h, l, pc = klines_15m[i]["high"], klines_15m[i]["low"], klines_15m[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_15m = sum(trs) / len(trs)
    stop_dist = atr_15m * 0.5  # 半倍15m ATR

    # 做多
    if ofi > 0.5:
        stop = price - stop_dist
        stop_pct = (price - stop) / price * 100 if price > stop else 0.15
        tp_pct = stop_pct * 2.0  # 2:1 R/R
        target = price + tp_pct * price / 100
        return {
            "direction": "long", "entry": price,
            "stop_loss": round(stop, 1), "target": round(target, 1),
            "stop_pct": round(stop_pct, 2),
            "target_pct": round(tp_pct, 2),
            "rr": 2.0, "pattern": "剥头皮做多",
            "score": 60, "leverage": 15,
            "reasons": [f"OFI={ofi:+.2f}强买压，剥头皮跟进"], "risks": ["剥头皮单，严格止损"],
            "key_level": round(price, 1),
        }

    # 做空
    if ofi < -0.5:
        stop = price + stop_dist
        stop_pct = (stop - price) / price * 100 if stop > price else 0.15
        tp_pct = stop_pct * 2.0
        target = price - tp_pct * price / 100
        return {
            "direction": "short", "entry": price,
            "stop_loss": round(stop, 1), "target": round(target, 1),
            "stop_pct": round(stop_pct, 2),
            "target_pct": round(tp_pct, 2),
            "rr": 2.0, "pattern": "剥头皮做空",
            "score": 60, "leverage": 15,
            "reasons": [f"OFI={ofi:+.2f}强卖压，剥头皮跟进"], "risks": ["剥头皮单，严格止损"],
            "key_level": round(price, 1),
        }

    return None


# ═══════════════════════════════════════════════
# 策略路由器 — 根据市场模式选择策略
# ═══════════════════════════════════════════════

def route_strategy(regime_label: str, price: float, ticker: dict,
                   k15: list, k1h: list, k4h: list,
                   vrvp: dict, ofi_info: dict,
                   rsi_1h: float, rsi_15m: float,
                   atr_1h: float, structure: dict,
                   base_stop_atr: float,
                   regime_params: dict, market_regime) -> list:
    """
    多策略路由 — 当前行情适用哪些策略

    Returns:
        候选信号列表（可多个策略同时出信号）
    """
    candidates = []

    # 趋势策略（由主引擎处理，此处不重复）
    # 已在 find_trading_signal 中实现

    # 震荡策略（只在震荡/未知/高波动模式下启用）
    if any(k in regime_label for k in ["震荡", "未知", "高波动"]):
        sig = ranging_strategy(price, vrvp, k15, rsi_15m, atr_1h, regime_label)
        if sig:
            candidates.append(sig)

    # 剥头皮策略（全天候，依赖OFI）
    sig = scalping_strategy(price, ofi_info, k15, regime_label)
    if sig: candidates.append(sig)

    sig = breakout_retest_strategy(price, ticker, k15, k4h, vrvp, ofi_info, rsi_15m, rsi_1h, structure, regime_label)
    if sig: candidates.append(sig)

    if ofi_info:
        ofi_info['ofi_prev'] = ofi_info.get('ofi_prev', ofi_info.get('ofi', 0))
        sig = fakeout_reversal_strategy(price, ticker, k15, vrvp, ofi_info, rsi_15m, regime_label)
        if sig: candidates.append(sig)

    return candidates


def breakout_retest_strategy(price, ticker, k15, k4h, vrvp, ofi_info, rsi_15m, rsi_1h, structure, label):
    if not vrvp: return None
    vah=vrvp.get("va_high",0); val=vrvp.get("va_low",0); poc=vrvp.get("poc",(vah+val)/2)
    ofi=ofi_info.get("ofi",0) if ofi_info else 0
    if price>vah*1.005 and abs(price-poc)/price*100<0.3 and ofi>0.1:
        sp=max((price-(poc-(vah-poc)*0.3))/price*100,0.4)
        return {"direction":"long","entry":price,"stop_loss":round(poc-(vah-poc)*0.3,1),"target":round(price+sp*2.5*price/100,1),"stop_pct":round(sp,2),"rr":2.5,"score":70,"leverage":12,"pattern":"突破回踩做多","reasons":["突破VAH回踩POC"],"risks":[],"key_level":round(poc,1),"strategy":"breakout_retest","fvg_info":{"in_fvg":False}}
    if price<val*0.995 and abs(price-poc)/price*100<0.3 and ofi<-0.1:
        sp=max(((poc+(poc-val)*0.3)-price)/price*100,0.4)
        return {"direction":"short","entry":price,"stop_loss":round(poc+(poc-val)*0.3,1),"target":round(price-sp*2.5*price/100,1),"stop_pct":round(sp,2),"rr":2.5,"score":70,"leverage":12,"pattern":"突破回踩做空","reasons":["跌破VAL回踩POC"],"risks":[],"key_level":round(poc,1),"strategy":"breakout_retest","fvg_info":{"in_fvg":False}}
    return None

def fakeout_reversal_strategy(price, ticker, k15, vrvp, ofi_info, rsi_15m, label):
    if not vrvp or not ofi_info or len(k15)<3: return None
    vah=vrvp.get("va_high",0); val=vrvp.get("va_low",0)
    ofi=ofi_info.get("ofi",0); ofi_p=ofi_info.get("ofi_prev",ofi); last=k15[-1]
    if last["high"]>vah*1.01 and last["close"]<vah and ofi<ofi_p:
        sp=max((last["high"]+(last["high"]-vah)*0.3-price)/price*100,0.3)
        return {"direction":"short","entry":price,"stop_loss":round(last["high"]+(last["high"]-vah)*0.3,1),"target":round(price-sp*2.5*price/100,1),"stop_pct":round(sp,2),"rr":2.5,"score":68,"leverage":10,"pattern":"假突破反杀做空","reasons":["假突破VAH反手"],"risks":[],"key_level":round(vah,1),"strategy":"fakeout_reversal","fvg_info":{"in_fvg":False}}
    if last["low"]<val*0.99 and last["close"]>val and ofi>ofi_p:
        sp=max((price-(last["low"]-(val-last["low"])*0.3))/price*100,0.3)
        return {"direction":"long","entry":price,"stop_loss":round(last["low"]-(val-last["low"])*0.3,1),"target":round(price+sp*2.5*price/100,1),"stop_pct":round(sp,2),"rr":2.5,"score":68,"leverage":10,"pattern":"假突破反杀做多","reasons":["假跌破VAL反手"],"risks":[],"key_level":round(val,1),"strategy":"fakeout_reversal","fvg_info":{"in_fvg":False}}
    return None

def make_signal(strategy, symbol, direction, entry, stop, target, confidence, leverage=15, risk_pct=0.003, ttl=60, reason=""):
    risk=abs(entry-stop); rr=abs(target-entry)/risk if risk>0 else 0
    return {"strategy":strategy,"symbol":symbol,"direction":direction,"entry":entry,"stop_loss":round(stop,1),"target":round(target,1),"rr":round(rr,2),"score":confidence,"confidence":confidence,"leverage":leverage,"risk_pct":risk_pct,"ttl_minutes":ttl,"pattern":strategy,"fvg_info":{"in_fvg":False},"key_level":round(entry,1)}
