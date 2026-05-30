#!/usr/bin/env python3
"""
Cipher BTC 自动交易系统 v2 — 稳定盈利版
核心：止损小 + 盈利大 + 高胜率 + 多指标共振
运行: python3 cipher_bot.py [scan|summary|review]
"""
import json
import logging
import sys
import os
import math
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import THREE_COMMAS, TELEGRAM, TRADING, ANALYSIS

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "cipher.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("Cipher")

# ============================================================
# 常量
# ============================================================
RSI_OVERSOLD = 35       # 超卖阈值（放宽到35，更易触发）
RSI_OVERBOUGHT = 65     # 超买阈值
MIN_SIGNAL_SCORE = 6    # 信号最低分（1-10）
COOLDOWN_MINUTES = 15   # 止损后冷却15分钟
VOLUME_SURGE = 1.3      # 放量阈值

# ============================================================
# API
# ============================================================
def api_get(url: str, timeout: int = 15):
    try:
        req = Request(url, headers={"User-Agent": "CipherBot/2.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"API GET 失败: {e}")
        return None

def api_post(url: str, data: dict, timeout: int = 10):
    try:
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "CipherBot/2.0"
        })
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"API POST 失败: {e}")
        return None

# ============================================================
# Binance 数据
# ============================================================
def get_binance_price() -> Optional[float]:
    data = api_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    return float(data["price"]) if data else None

def get_24h_ticker() -> Optional[dict]:
    data = api_get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT")
    if data:
        return {
            "high": float(data["highPrice"]),
            "low": float(data["lowPrice"]),
            "volume": float(data["volume"]),
            "change_pct": float(data["priceChangePercent"]),
            "last_price": float(data["lastPrice"]),
            "quote_volume": float(data["quoteVolume"]),
        }
    return None

def get_klines(interval: str, limit: int = 50) -> Optional[List[dict]]:
    """获取K线数据（含成交额）"""
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    data = api_get(url)
    if data and isinstance(data, list):
        return [{
            "time": k[0], "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            "quote_vol": float(k[7]), "trades": int(k[8]),
        } for k in data]
    return None

# ============================================================
# 高级技术指标
# ============================================================

def calc_sma(values: List[float], period: int) -> float:
    """简单移动平均"""
    if len(values) < period:
        return sum(values) / len(values)
    return sum(values[-period:]) / period

def calc_ema(values: List[float], period: int) -> float:
    """指数移动平均"""
    if len(values) < period:
        return sum(values) / len(values)
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema

def calc_rsi(closes: List[float], period: int = 14) -> float:
    """RSI 指标"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = 0, 0
    for i in range(-period, 0):
        diff = closes[i] - closes[i-1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_atr(klines: List[dict], period: int = 14) -> float:
    """ATR - 平均真实波幅"""
    if len(klines) < period + 1:
        return (max(k["high"] for k in klines) - min(k["low"] for k in klines)) / len(klines)
    trs = []
    for i in range(-period, 0):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)

def calc_macd(closes: List[float]) -> Tuple[float, float, float]:
    """MACD: (macd_line, signal_line, histogram)"""
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd = ema12 - ema26
    signal = calc_ema([macd] * 3 + [macd], 9)  # simplified
    return macd, signal, macd - signal

def find_volume_nodes(klines: List[dict], levels: int = 3) -> Tuple[List[float], List[float]]:
    """成交量加权支撑/阻力——找到成交量最大的几个价格区间"""
    if not klines:
        return [], []
    buckets = {}
    for k in klines:
        center = (k["high"] + k["low"]) / 2
        idx = int(center / 100) * 100  # 每100刀一个区间
        buckets[idx] = buckets.get(idx, 0) + k["volume"]
    sorted_buckets = sorted(buckets.items(), key=lambda x: x[1], reverse=True)
    volume_supports = []
    volume_resistances = []
    current_price = klines[-1]["close"]
    for price_lvl, vol in sorted_buckets[:levels]:
        if price_lvl < current_price:
            volume_supports.append(price_lvl)
        else:
            volume_resistances.append(price_lvl)
    return sorted(volume_supports, reverse=True)[:2], sorted(volume_resistances)[:2]

def detect_market_structure(klines: List[dict]) -> dict:
    """分析市场结构——找出更高的高点/低点"""
    if len(klines) < 12:
        return {"structure": "unknown", "direction": "neutral"}

    # 使用1/3分段的HH/HL
    seg = len(klines) // 3
    seg1_high = max(k["high"] for k in klines[:seg])
    seg1_low = min(k["low"] for k in klines[:seg])
    seg2_high = max(k["high"] for k in klines[seg:2*seg])
    seg2_low = min(k["low"] for k in klines[seg:2*seg])
    seg3_high = max(k["high"] for k in klines[2*seg:])
    seg3_low = min(k["low"] for k in klines[2*seg:])

    # 上升结构：高点越来越高，低点越来越高
    if seg3_high > seg2_high > seg1_high and seg3_low > seg2_low:
        return {"structure": "uptrend", "direction": "bullish"}
    # 下降结构：高点越来越低，低点越来越低
    if seg3_high < seg2_high and seg3_low < seg2_low < seg1_low:
        return {"structure": "downtrend", "direction": "bearish"}
    # 扩张/收敛
    if seg3_high > seg2_high and seg3_low < seg2_low:
        return {"structure": "expanding", "direction": "volatile"}
    return {"structure": "ranging", "direction": "neutral"}

# ============================================================
# 信号评分系统
# ============================================================

def score_signal(
    direction: str, price: float,
    stop_loss: float, target: float,
    klines_15m: List[dict], klines_1h: List[dict],
    structure: dict, rsi_1h: float, atr_1h: float
) -> Tuple[int, List[str]]:
    """
    给信号打分（1-10），并列出理由
    只返回 ≥ MIN_SIGNAL_SCORE 的优质信号
    """
    score = 5  # 基础分
    reasons = []
    risks = []

    closes_15m = [k["close"] for k in klines_15m]
    closes_1h = [k["close"] for k in klines_1h]

    # 1. 止损大小（止损越小分越高）
    stop_pct = abs(price - stop_loss) / price * 100
    if stop_pct < 0.4:
        score += 2; reasons.append("止损极小(<0.4%)")
    elif stop_pct < 0.7:
        score += 1.5; reasons.append("止损小(<0.7%)")
    elif stop_pct < 1.0:
        score += 1; reasons.append("止损适中(<1.0%)")
    else:
        risks.append("止损偏大")

    # 2. 盈亏比（R/R越高分越高）
    target_pct = abs(target - price) / price * 100
    rr = target_pct / stop_pct if stop_pct > 0 else 0
    if rr >= 4:
        score += 2; reasons.append(f"高盈亏比({rr:.1f}:1)")
    elif rr >= 3:
        score += 1.5; reasons.append(f"优秀盈亏比({rr:.1f}:1)")
    elif rr >= 2.5:
        score += 1; reasons.append(f"良好盈亏比({rr:.1f}:1)")
    elif rr >= 2.0:
        score += 0.5; reasons.append(f"合格盈亏比({rr:.1f}:1)")

    # 3. RSI 支持
    if direction == "long" and rsi_1h < RSI_OVERSOLD:
        score += 1.5; reasons.append("1H RSI超卖区")
    elif direction == "long" and rsi_1h < 45:
        score += 1; reasons.append("1H RSI偏低")
    elif direction == "short" and rsi_1h > RSI_OVERBOUGHT:
        score += 1.5; reasons.append("1H RSI超买区")
    elif direction == "short" and rsi_1h > 55:
        score += 1; reasons.append("1H RSI偏高")
    else:
        risks.append("RSI中性")

    # 4. 趋势一致（和1H趋势同向加分）
    ema9_1h = calc_ema(closes_1h, 9)
    ema21_1h = calc_ema(closes_1h, 21)
    if direction == "long" and ema9_1h > ema21_1h and price > ema9_1h:
        score += 1.5; reasons.append("1H趋势支持做多")
    elif direction == "short" and ema9_1h < ema21_1h and price < ema9_1h:
        score += 1.5; reasons.append("1H趋势支持做空")
    elif direction == "long" and price < ema21_1h:
        risks.append("逆1H大趋势做多")
    elif direction == "short" and price > ema21_1h:
        risks.append("逆1H大趋势做空")

    # 5. 放量确认
    if len(klines_15m) >= 6:
        avg_vol = sum(k["volume"] for k in klines_15m[-6:-3]) / 3
        recent_vol = sum(k["volume"] for k in klines_15m[-3:]) / 3
        if avg_vol > 0 and recent_vol > avg_vol * VOLUME_SURGE:
            score += 1; reasons.append("成交量放大确认")
        elif avg_vol > 0 and recent_vol < avg_vol * 0.7:
            risks.append("成交量萎缩")

    # 6. 连续K线确认（3根同向）
    last_3 = klines_15m[-3:]
    if direction == "long" and all(k["close"] > k["open"] for k in last_3):
        score += 1; reasons.append("15min三连阳")
    elif direction == "short" and all(k["close"] < k["open"] for k in last_3):
        score += 1; reasons.append("15min三连阴")
    else:
        risks.append("15minK线未确认")

    # 7. 市场结构支持
    if direction == "long" and structure["direction"] == "bullish":
        score += 1; reasons.append("市场结构上升趋势")
    elif direction == "short" and structure["direction"] == "bearish":
        score += 1; reasons.append("市场结构下降趋势")
    elif structure["direction"] == "volatile":
        risks.append("市场波动剧烈")

    # 最终分数
    final_score = min(max(int(score), 1), 10)
    return final_score, reasons, risks

# ============================================================
# 主信号引擎
# ============================================================

def find_trading_signal(price: float, ticker_24h: dict,
                        klines_15m: List[dict], klines_1h: List[dict],
                        klines_4h: List[dict]) -> Optional[dict]:
    """多指标共振信号引擎"""
    if not all([price, ticker_24h, klines_15m, klines_1h, klines_4h]):
        return None

    closes_15m = [k["close"] for k in klines_15m]
    closes_1h = [k["close"] for k in klines_1h]
    closes_4h = [k["close"] for k in klines_4h]

    # === 多周期指标计算 ===
    rsi_15m = calc_rsi(closes_15m, 14)
    rsi_1h = calc_rsi(closes_1h, 14)
    atr_1h = calc_atr(klines_1h, 14)
    atr_4h = calc_atr(klines_4h, 14)
    structure_1h = detect_market_structure(klines_1h)
    structure_4h = detect_market_structure(klines_4h)

    ema21_1h = calc_ema(closes_1h, 21)
    ema21_4h = calc_ema(closes_4h, 21)

    vol_supports, vol_resistances = find_volume_nodes(klines_4h)

    high_24h = ticker_24h["high"]
    low_24h = ticker_24h["low"]
    range_24h = high_24h - low_24h
    position_pct = (price - low_24h) / range_24h * 100 if range_24h > 0 else 50

    # 15分钟级别支撑/阻力
    sr_15m_high = max(k["high"] for k in klines_15m[-10:])
    sr_15m_low = min(k["low"] for k in klines_15m[-10:])

    # ---- 动态 ATR 止损 ----
    # 超短线用 1H ATR × 0.5 作为止损基准
    base_stop_atr = atr_1h * 0.5

    candidates = []

    # ===================== 做多信号 =====================
    near_support = (
        price < low_24h * 1.015  # 在24h低点1.5%以内
        or price < sr_15m_low * 1.01  # 在15min低点1%以内
        or any(abs(price - s) / price * 100 < 1.5 for s in vol_supports)  # 靠近成交量支撑
    )
    support_level = min(low_24h, sr_15m_low)

    if near_support and position_pct < 40:
        stop_loss = support_level - base_stop_atr * 0.5
        stop_pct = (price - stop_loss) / price * 100 if price > stop_loss else 0.5
        stop_pct = max(stop_pct, 0.3)  # 最低0.3%

        # 目标：4H EMA21 或 24h高点
        target = min(ema21_4h if price < ema21_4h else high_24h, high_24h)
        target = max(target, price * 1.008)  # 至少0.8%空间
        target_pct = (target - price) / price * 100
        rr = target_pct / stop_pct if stop_pct > 0 else 0

        if stop_pct < TRADING["max_stop_loss_pct"] and rr >= TRADING["min_rr_ratio"]:
            sig_score, reasons, risks = score_signal(
                "long", price, stop_loss, target,
                klines_15m, klines_1h, structure_1h, rsi_1h, atr_1h
            )
            if sig_score >= MIN_SIGNAL_SCORE:
                candidates.append({
                    "direction": "long", "entry": price,
                    "entry_range": f"${max(support_level, price-30):.0f} - ${price+30:.0f}",
                    "stop_loss": round(stop_loss, 1),
                    "target": round(target, 1),
                    "stop_pct": round(stop_pct, 2),
                    "target_pct": round(target_pct, 2),
                    "rr": round(rr, 2),
                    "pattern": f"支撑位做多",
                    "score": sig_score, "reasons": reasons, "risks": risks,
                })

    # ===================== 做空信号 =====================
    near_resistance = (
        price > high_24h * 0.985  # 在24h高点1.5%以内
        or price > sr_15m_high * 0.99  # 在15min高点1%以内
        or any(abs(price - r) / price * 100 < 1.5 for r in vol_resistances)
    )
    resistance_level = max(high_24h, sr_15m_high)

    if near_resistance and position_pct > 60:
        stop_loss = resistance_level + base_stop_atr * 0.5
        stop_pct = (stop_loss - price) / price * 100 if stop_loss > price else 0.5
        stop_pct = max(stop_pct, 0.3)

        target = max(ema21_4h if price > ema21_4h else low_24h, low_24h)
        target = min(target, price * 0.992)
        target_pct = (price - target) / price * 100
        rr = target_pct / stop_pct if stop_pct > 0 else 0

        if stop_pct < TRADING["max_stop_loss_pct"] and rr >= TRADING["min_rr_ratio"]:
            sig_score, reasons, risks = score_signal(
                "short", price, stop_loss, target,
                klines_15m, klines_1h, structure_1h, rsi_1h, atr_1h
            )
            if sig_score >= MIN_SIGNAL_SCORE:
                candidates.append({
                    "direction": "short", "entry": price,
                    "entry_range": f"${price-30:.0f} - ${min(resistance_level, price+30):.0f}",
                    "stop_loss": round(stop_loss, 1),
                    "target": round(target, 1),
                    "stop_pct": round(stop_pct, 2),
                    "target_pct": round(target_pct, 2),
                    "rr": round(rr, 2),
                    "pattern": f"阻力位做空",
                    "score": sig_score, "reasons": reasons, "risks": risks,
                })

    # ===================== 选择最佳信号 =====================
    if candidates:
        # 综合评分 × R/R 排序
        candidates.sort(key=lambda s: s["score"] * s["rr"], reverse=True)
        best = candidates[0]
        logger.info(f"候选信号: {len(candidates)}个, 最佳: {best['direction']} 评分{best['score']}/10 R/R={best['rr']}")
        return best
    return None

# ============================================================
# 通知 & Webhook
# ============================================================

def send_telegram(message: str) -> bool:
    token = TELEGRAM["bot_token"]
    if not TELEGRAM["chat_id"]:
        updates = api_get(f"https://api.telegram.org/bot{token}/getUpdates")
        if updates and updates.get("result"):
            for u in updates["result"]:
                chat = u.get("message", {}).get("chat", {})
                if chat.get("id"):
                    TELEGRAM["chat_id"] = chat["id"]
                    break
    if TELEGRAM["chat_id"]:
        return api_post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": TELEGRAM["chat_id"], "text": message, "parse_mode": "Markdown"}
        ) is not None
    return False

def send_webhook(signal: dict) -> bool:
    action = "enter_long" if signal["direction"] == "long" else "enter_short"
    payload = {
        "secret": THREE_COMMAS["secret"],
        "max_lag": "300",
        "timestamp": "{{timenow}}",
        "trigger_price": "{{close}}",
        "tv_exchange": "{{exchange}}",
        "tv_instrument": "{{ticker}}",
        "action": action,
        "bot_uuid": THREE_COMMAS["bot_uuid"],
        "order": {
            "amount": str(THREE_COMMAS["amount_percent"]),
            "currency_type": "margin_percent",
            "order_type": TRADING["order_type"],
            "price": str(int(signal["entry"])),
        }
    }
    result = api_post(THREE_COMMAS["webhook_url"], payload)
    if result:
        logger.info(f"✅ Webhook: {action} @ ${signal['entry']:.0f}")
        return True
    logger.error(f"❌ Webhook 失败")
    return False

def format_signal(signal: dict) -> str:
    emoji = "🟢" if signal["direction"] == "long" else "🔴"
    dir_cn = "做多" if signal["direction"] == "long" else "做空"
    reasons_str = "\n".join(f"  ✅ {r}" for r in signal.get("reasons", []))
    risks_str = "\n".join(f"  ⚠️ {r}" for r in signal.get("risks", [])) if signal.get("risks") else "  无明显风险"

    return (
        f"📊 *BTC超短线信号*\n"
        f"方向：{dir_cn} {emoji}\n"
        f"当前价格：${signal['entry']:.0f}\n"
        f"入场区间：{signal['entry_range']}\n"
        f"止损参考：${signal['stop_loss']:.0f}（{signal['stop_pct']:.2f}%）\n"
        f"目标参考：${signal['target']:.0f}（{signal['target_pct']:.2f}%）\n"
        f"预计盈亏比：{signal['rr']}:1\n"
        f"信号评分：{signal['score']}/10\n"
        f"杠杆：25x\n"
        f"形态：{signal['pattern']}\n\n"
        f"*评分理由：*\n{reasons_str}\n\n"
        f"*风险提示：*\n{risks_str}\n\n"
        f"⚠️ DYOR，杠杆风险自行承担"
    )

# ============================================================
# 主函数
# ============================================================

def run_scan():
    logger.info("=" * 50)
    logger.info("Cipher v2 超短线扫描")
    price = get_binance_price()
    ticker = get_24h_ticker()
    if not price or not ticker:
        return

    logger.info(f"BTC ${price:,.2f} | 24h ${ticker['low']:,.0f}-${ticker['high']:,.0f} {ticker['change_pct']:+.2f}%")

    # 获取多周期数据
    k15 = get_klines("15m", 30)
    k1h = get_klines("1h", 30)
    k4h = get_klines("4h", 24)
    if not all([k15, k1h, k4h]):
        return

    # 计算多周期指标
    rsi_15m = calc_rsi([k["close"] for k in k15], 14)
    rsi_1h = calc_rsi([k["close"] for k in k1h], 14)
    atr_1h = calc_atr(k1h, 14)
    ema9_1h = calc_ema([k["close"] for k in k1h], 9)
    ema21_1h = calc_ema([k["close"] for k in k1h], 21)
    ema21_4h = calc_ema([k["close"] for k in k4h], 21)
    struct_1h = detect_market_structure(k1h)

    logger.info(f"RSI 15m={rsi_15m:.0f} 1h={rsi_1h:.0f} | ATR 1h=${atr_1h:.1f}")
    logger.info(f"结构: {struct_1h['structure']} | EMA9/21 1h: {'多头' if ema9_1h>ema21_1h else '空头'}")
    logger.info(f"EMA21 4h=${ema21_4h:.0f} | 24h区间位置: {((price-ticker['low'])/(ticker['high']-ticker['low'])*100):.0f}%")

    signal = find_trading_signal(price, ticker, k15, k1h, k4h)
    if signal:
        logger.info(f"✅ 优质信号! {signal['direction']} 评分{signal['score']}/10 R/R={signal['rr']}")
        msg = format_signal(signal)
        send_telegram(msg)
        send_webhook(signal)
        logger.info("信号已推送+下单")
    else:
        logger.info("❌ 无优质信号，继续观察")

def run_summary():
    """4小时总结（含多周期指标报告）"""
    price = get_binance_price()
    ticker = get_24h_ticker()
    k4h = get_klines("4h", 24)
    k1d = get_klines("1d", 7)
    if not all([price, ticker, k4h]):
        return

    closes_4h = [k["close"] for k in k4h]
    rsi_4h = calc_rsi(closes_4h, 14)
    atr_4h = calc_atr(k4h, 14)
    ema21_4h = calc_ema(closes_4h, 21)
    struct = detect_market_structure(k4h)

    sr_high = max(k["high"] for k in k4h)
    sr_low = min(k["low"] for k in k4h)

    struct_cn = {"uptrend": "上升趋势 ✅", "downtrend": "下降趋势 ❌",
                 "ranging": "震荡 ⏸️", "expanding": "扩张波动 ⚡", "unknown": "不明"}

    msg = (
        f"📊 *BTC短线分析 (4H多指标)*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"当前价格：${price:,.0f}\n\n"
        f"*技术指标*\n"
        f"RSI(14)：{rsi_4h:.0f} {'超买🔥' if rsi_4h>65 else '超卖🧊' if rsi_4h<35 else '中性'}\n"
        f"ATR(14)：${atr_4h:.1f}\n"
        f"EMA21 4H：${ema21_4h:,.0f}\n"
        f"结构：{struct_cn.get(struct['structure'], struct['structure'])}\n\n"
        f"*关键位*\n"
        f"━━ 阻力：${sr_high:,.0f}\n"
        f"━━ 支撑：${sr_low:,.0f}\n\n"
        f"*做单参考*\n"
        f"☝️ 做多：回踩${sr_low:,.0f}附近企稳 + RSI不超卖\n"
        f"👇 做空：反弹${sr_high:,.0f}附近遇阻 + RSI不超买\n"
        f"⏸️ 观望：${sr_low:,.0f}-${sr_high:,.0f}中间震荡"
    )
    send_telegram(msg)
    logger.info("4小时总结已推送")

def run_review():
    """每日复盘"""
    ticker = get_24h_ticker()
    k1d = get_klines("1d", 7)
    if not ticker or not k1d:
        return
    open_p = k1d[-1]["open"]
    close_p = ticker["last_price"]
    change_pct = (close_p - open_p) / open_p * 100 if open_p else 0

    closes_d = [k["close"] for k in k1d]
    rsi_d = calc_rsi(closes_d, 14)
    ema7_d = calc_ema(closes_d, 7)
    ema21_d = calc_ema(closes_d, 21)

    msg = (
        f"📈 *Cipher每日复盘*\n"
        f"日期：{datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"*BTC今日*\n"
        f"开盘 ${open_p:,.0f} → 现价 ${close_p:,.0f}（{change_pct:+.2f}%）\n"
        f"最高 ${ticker['high']:,.0f} / 最低 ${ticker['low']:,.0f}\n"
        f"成交量 {ticker['volume']:,.0f} BTC\n\n"
        f"*日线指标*\n"
        f"RSI(14)：{rsi_d:.0f}\n"
        f"EMA7：${ema7_d:,.0f}\n"
        f"EMA21：${ema21_d:,.0f}\n"
        f"趋势：{'多头📈' if ema7_d>ema21_d else '空头📉'}\n\n"
        f"*策略*\n"
        f"今日严格按信号执行，无信号不做。\n"
        f"宁可错过，不乱做。\n\n"
        f"⚠️ DYOR"
    )
    send_telegram(msg)
    logger.info("每日复盘已推送")

# ============================================================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    {"scan": run_scan, "summary": run_summary, "review": run_review}.get(mode, lambda: print(f"未知: {mode}"))()
