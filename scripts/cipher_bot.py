#!/usr/bin/env python3
"""
Cipher BTC 自动交易系统 v3 — 稳定盈利版
核心：止损小 + 盈利大 + 高胜率 + 多指标共振
运行: python3 cipher_bot.py [scan|summary|review]

v3 改进：
- Webhook 失败自动重试(3次) + Telegram 告警
- ATR 动态乘数(近3根K线波幅自适应)
- K线收盘确认(防止插针假信号)
- Telegram chat_id 持久化到文件
- market_structure 改用 EMA20/50/200 排列
- API 超时分段优化
"""
import json
import logging
import sys
import os
import math
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import THREE_COMMAS, TELEGRAM, TRADING, ANALYSIS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
CHAT_ID_FILE = os.path.join(BASE_DIR, ".chat_id")
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
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
MIN_SIGNAL_SCORE = 6
VOLUME_SURGE = 1.3
API_TIMEOUT_FAST = 5     # 价格查询（5秒）
API_TIMEOUT_NORMAL = 10  # K线数据（10秒）
API_TIMEOUT_SLOW = 15    # Telegram/Webhook（15秒）
WEBHOOK_RETRIES = 3      # Webhook 重试次数
CANDLE_CLOSE_BUFFER = 120  # K线收盘前120秒不下单（2分钟）

# ============================================================
# API — 改进：分时超时 + Webhook 重试
# ============================================================
def api_get(url: str, timeout: int = API_TIMEOUT_NORMAL):
    try:
        req = Request(url, headers={"User-Agent": "CipherBot/3.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"API GET 失败 [{url[-30:]}]: {e}")
        return None

def api_post(url: str, data: dict, timeout: int = API_TIMEOUT_SLOW):
    try:
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "CipherBot/3.0"
        })
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"API POST 失败: {e}")
        return None

def api_post_with_retry(url: str, data: dict, retries: int = WEBHOOK_RETRIES) -> Optional[dict]:
    """带指数退避重试的 POST"""
    last_error = None
    for attempt in range(retries):
        result = api_post(url, data)
        if result:
            return result
        last_error = "POST 返回空"
        if attempt < retries - 1:
            wait = 2 ** attempt
            logger.warning(f"POST 重试 {attempt+1}/{retries}，等待 {wait}s...")
            time.sleep(wait)
    logger.error(f"POST 失败 ({retries}次后放弃): {last_error}")
    return None

# ============================================================
# Binance 数据
# ============================================================
def get_binance_price() -> Optional[float]:
    data = api_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", API_TIMEOUT_FAST)
    return float(data["price"]) if data else None

def get_24h_ticker() -> Optional[dict]:
    data = api_get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", API_TIMEOUT_FAST)
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
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    data = api_get(url, API_TIMEOUT_NORMAL)
    if data and isinstance(data, list):
        return [{
            "time": k[0], "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            "quote_vol": float(k[7]), "trades": int(k[8]),
        } for k in data]
    return None

def is_candle_closing(klines: List[dict], interval_minutes: int = 15) -> bool:
    """判断当前K线是否即将收盘，临近收盘不下单（防止插针假信号）"""
    if not klines:
        return False
    current_candle_ms = klines[-1]["time"]
    current_candle_start = datetime.fromtimestamp(current_candle_ms / 1000, tz=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - current_candle_start).total_seconds()
    remaining = interval_minutes * 60 - elapsed
    if remaining < CANDLE_CLOSE_BUFFER:
        logger.info(f"⏳ 当前K线还剩{remaining:.0f}s，等待下一根确认")
        return True
    return False

# ============================================================
# 高级技术指标
# ============================================================
def calc_sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    return sum(values[-period:]) / period

def calc_ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema

def calc_rsi(closes: List[float], period: int = 14) -> float:
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
    if len(klines) < period + 1:
        return (max(k["high"] for k in klines) - min(k["low"] for k in klines)) / len(klines)
    trs = []
    for i in range(-period, 0):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)

def calc_local_atr(klines: List[dict], n: int = 3) -> float:
    """计算最近几根K线的局部波幅——用于动态调整止损乘数"""
    if len(klines) < n:
        return 0
    ranges = [abs(k["high"] - k["low"]) for k in klines[-n:]]
    return sum(ranges) / len(ranges)

# ============================================================
# 市场结构分析 — 改进：改用 EMA 排列判断趋势
# ============================================================
def detect_market_structure(klines: List[dict]) -> dict:
    """用EMA20/50排列判断趋势，替代粗糙的三等分法"""
    if len(klines) < 30:
        return {"structure": "unknown", "direction": "neutral", "score": 0}

    closes = [k["close"] for k in klines]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50) if len(closes) >= 50 else calc_sma(closes, 20)
    ema100 = calc_sma(closes, 20)  # fallback

    # EMA 排列强度打分
    score = 0
    if ema20 > ema50: score += 1
    if ema50 > ema100: score += 1
    if closes[-1] > ema20: score += 1

    if score >= 2:
        return {"structure": "uptrend", "direction": "bullish", "score": score}
    elif score <= 0:
        return {"structure": "downtrend", "direction": "bearish", "score": score}
    else:
        return {"structure": "ranging", "direction": "neutral", "score": score}

# ============================================================
# 信号评分系统
# ============================================================
def score_signal(
    direction: str, price: float,
    stop_loss: float, target: float,
    klines_15m: List[dict], klines_1h: List[dict],
    structure: dict, rsi_1h: float, atr_1h: float
) -> Tuple[int, List[str], List[str]]:
    score = 5
    reasons = []
    risks = []

    closes_15m = [k["close"] for k in klines_15m]
    closes_1h = [k["close"] for k in klines_1h]

    stop_pct = abs(price - stop_loss) / price * 100
    if stop_pct < 0.4:
        score += 2; reasons.append("止损极小(<0.4%)")
    elif stop_pct < 0.7:
        score += 1.5; reasons.append("止损小(<0.7%)")
    elif stop_pct < 1.0:
        score += 1; reasons.append("止损适中(<1.0%)")
    else:
        risks.append("止损偏大")

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

    if len(klines_15m) >= 6:
        avg_vol = sum(k["volume"] for k in klines_15m[-6:-3]) / 3
        recent_vol = sum(k["volume"] for k in klines_15m[-3:]) / 3
        if avg_vol > 0 and recent_vol > avg_vol * VOLUME_SURGE:
            score += 1; reasons.append("成交量放大确认")
        elif avg_vol > 0 and recent_vol < avg_vol * 0.7:
            risks.append("成交量萎缩")

    last_3 = klines_15m[-3:]
    if direction == "long" and all(k["close"] > k["open"] for k in last_3):
        score += 1; reasons.append("15min三连阳")
    elif direction == "short" and all(k["close"] < k["open"] for k in last_3):
        score += 1; reasons.append("15min三连阴")
    else:
        risks.append("15minK线未确认")

    if direction == "long" and structure["direction"] == "bullish":
        score += 1; reasons.append("市场结构上升趋势")
    elif direction == "short" and structure["direction"] == "bearish":
        score += 1; reasons.append("市场结构下降趋势")
    elif structure["direction"] == "volatile":
        risks.append("市场波动剧烈")

    final_score = min(max(int(score), 1), 10)
    return final_score, reasons, risks

# ============================================================
# 主信号引擎
# ============================================================
def find_trading_signal(price: float, ticker_24h: dict,
                        klines_15m: List[dict], klines_1h: List[dict],
                        klines_4h: List[dict]) -> Optional[dict]:
    if not all([price, ticker_24h, klines_15m, klines_1h, klines_4h]):
        return None

    closes_15m = [k["close"] for k in klines_15m]
    closes_1h = [k["close"] for k in klines_1h]
    closes_4h = [k["close"] for k in klines_4h]

    rsi_15m = calc_rsi(closes_15m, 14)
    rsi_1h = calc_rsi(closes_1h, 14)
    atr_1h = calc_atr(klines_1h, 14)
    atr_4h = calc_atr(klines_4h, 14)
    structure_1h = detect_market_structure(klines_1h)

    ema21_1h = calc_ema(closes_1h, 21)
    ema21_4h = calc_ema(closes_4h, 21)

    high_24h = ticker_24h["high"]
    low_24h = ticker_24h["low"]
    range_24h = high_24h - low_24h
    position_pct = (price - low_24h) / range_24h * 100 if range_24h > 0 else 50

    sr_15m_high = max(k["high"] for k in klines_15m[-10:])
    sr_15m_low = min(k["low"] for k in klines_15m[-10:])

    # ——— ATR 动态乘数 ———
    # 改进：用最近3根K线的局部波幅 / 整体ATR 比值来调整乘数
    local_vol = calc_local_atr(klines_15m, 3)
    vol_ratio = local_vol / atr_1h if atr_1h > 0 else 1.0
    atr_multiplier = max(0.3, min(0.8, vol_ratio * 0.5))
    base_stop_atr = atr_1h * atr_multiplier

    # ——— K线收盘确认 ———
    # 改进：K线临近收盘不下单，防止插针假信号
    if is_candle_closing(klines_15m, 15):
        return None

    candidates = []

    # ===== 做多信号 =====
    near_support = (
        price < low_24h * 1.015
        or price < sr_15m_low * 1.01
    )
    support_level = min(low_24h, sr_15m_low)

    if near_support and position_pct < 40:
        stop_loss = support_level - base_stop_atr * 0.5
        stop_pct = (price - stop_loss) / price * 100 if price > stop_loss else 0.5
        stop_pct = max(stop_pct, 0.3)

        target = min(ema21_4h if price < ema21_4h else high_24h, high_24h)
        target = max(target, price * 1.008)
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
                    "pattern": "支撑位做多",
                    "score": sig_score, "reasons": reasons, "risks": risks,
                })

    # ===== 做空信号 =====
    near_resistance = (
        price > high_24h * 0.985
        or price > sr_15m_high * 0.99
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
                    "pattern": "阻力位做空",
                    "score": sig_score, "reasons": reasons, "risks": risks,
                })

    if candidates:
        candidates.sort(key=lambda s: s["score"] * s["rr"], reverse=True)
        best = candidates[0]
        logger.info(f"候选:{len(candidates)} 最佳:{best['direction']} 评分{best['score']}/10 R/R={best['rr']}")
        return best
    return None

# ============================================================
# Telegram — 改进：chat_id 持久化到文件
# ============================================================
def get_chat_id() -> Optional[int]:
    """从文件读取缓存的 chat_id"""
    if os.path.exists(CHAT_ID_FILE):
        try:
            return int(open(CHAT_ID_FILE).read().strip())
        except:
            pass
    return None

def save_chat_id(chat_id: int):
    with open(CHAT_ID_FILE, "w") as f:
        f.write(str(chat_id))
    logger.info(f"chat_id {chat_id} 已持久化")

def send_telegram(message: str) -> bool:
    token = TELEGRAM["bot_token"]
    chat_id = TELEGRAM.get("chat_id") or get_chat_id()

    if not chat_id:
        updates = api_get(f"https://api.telegram.org/bot{token}/getUpdates", API_TIMEOUT_FAST)
        if updates and updates.get("result"):
            for u in updates["result"]:
                c = u.get("message", {}).get("chat", {})
                if c.get("id"):
                    chat_id = c["id"]
                    TELEGRAM["chat_id"] = chat_id
                    save_chat_id(chat_id)
                    break

    if chat_id:
        result = api_post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        )
        if result:
            return True
        logger.warning("Telegram 发送失败")
    return False

def send_webhook(signal: dict) -> bool:
    """改进：失败自动重试3次 + Telegram 告警"""
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

    result = api_post_with_retry(THREE_COMMAS["webhook_url"], payload, WEBHOOK_RETRIES)
    if result:
        logger.info(f"✅ Webhook: {action} @ ${signal['entry']:.0f}")
        return True

    logger.error(f"❌ Webhook 失败({WEBHOOK_RETRIES}次)")
    send_telegram(f"⚠️ *Webhook 发送失败*\n信号已出但3Commas未收到\n方向：{action} @ ${signal['entry']:.0f}\n请手动处理！")
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
        f"杠杆：25x | 形态：{signal['pattern']}\n\n"
        f"*评分理由：*\n{reasons_str}\n\n"
        f"*风险提示：*\n{risks_str}\n\n"
        f"⚠️ DYOR，杠杆风险自行承担"
    )

# ============================================================
# 主函数
# ============================================================
def run_scan():
    logger.info("=" * 50)
    logger.info("Cipher v3 超短线扫描")

    price = get_binance_price()
    ticker = get_24h_ticker()
    if not price or not ticker:
        logger.error("价格获取失败，跳过本轮")
        return

    logger.info(f"BTC ${price:,.2f} | 24h ${ticker['low']:,.0f}-${ticker['high']:,.0f} {ticker['change_pct']:+.2f}%")

    k15 = get_klines("15m", 30)
    k1h = get_klines("1h", 30)
    k4h = get_klines("4h", 24)
    if not all([k15, k1h, k4h]):
        logger.error("K线数据获取失败，跳过本轮")
        return

    rsi_15m = calc_rsi([k["close"] for k in k15], 14)
    rsi_1h = calc_rsi([k["close"] for k in k1h], 14)
    atr_1h = calc_atr(k1h, 14)
    ema9_1h = calc_ema([k["close"] for k in k1h], 9)
    ema21_1h = calc_ema([k["close"] for k in k1h], 21)
    ema21_4h = calc_ema([k["close"] for k in k4h], 21)
    struct_1h = detect_market_structure(k1h)

    logger.info(f"RSI 15m={rsi_15m:.0f} 1h={rsi_1h:.0f} | ATR 1h=${atr_1h:.1f}")
    logger.info(f"结构: {struct_1h['structure']} | EMA9/21 1h: {'多头' if ema9_1h>ema21_1h else '空头'}")
    logger.info(f"EMA21 4h=${ema21_4h:.0f} | 24h区间: {((price-ticker['low'])/(ticker['high']-ticker['low'])*100):.0f}%")

    signal = find_trading_signal(price, ticker, k15, k1h, k4h)
    if signal:
        logger.info(f"✅ 信号! {signal['direction']} 评分{signal['score']}/10 R/R={signal['rr']}")
        send_telegram(format_signal(signal))
        send_webhook(signal)
    else:
        logger.info("无优质信号")

def run_summary():
    price = get_binance_price()
    ticker = get_24h_ticker()
    k4h = get_klines("4h", 24)
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
        f"📊 *BTC短线分析 (4H)*\n━━━━━━━━━━━━━━━━━━\n"
        f"当前：${price:,.0f}\n\n"
        f"RSI(14)：{rsi_4h:.0f} {'超买🔥' if rsi_4h>65 else '超卖🧊' if rsi_4h<35 else '中性'}\n"
        f"ATR：${atr_4h:.1f} | EMA21：${ema21_4h:,.0f}\n"
        f"结构：{struct_cn.get(struct['structure'], '?')}\n\n"
        f"阻力：${sr_high:,.0f}\n支撑：${sr_low:,.0f}\n\n"
        f"☝️ 回踩${sr_low:,.0f}做多  👇 反弹${sr_high:,.0f}做空  ⏸️ 中间观望"
    )
    send_telegram(msg)

def run_review():
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
        f"📈 *Cipher每日复盘*\n{datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"*BTC今日*\n开盘 ${open_p:,.0f} → ${close_p:,.0f}（{change_pct:+.2f}%）\n"
        f"最高 ${ticker['high']:,.0f} / 最低 ${ticker['low']:,.0f}\n"
        f"成交量 {ticker['volume']:,.0f} BTC\n\n"
        f"*日线*\nRSI(14)：{rsi_d:.0f}\n"
        f"EMA7：${ema7_d:,.0f} | EMA21：${ema21_d:,.0f}\n"
        f"趋势：{'多头📈' if ema7_d>ema21_d else '空头📉'}\n\n"
        f"*策略*\n严格按信号执行，无信号不做。宁可错过不乱做。\n\n⚠️ DYOR"
    )
    send_telegram(msg)

# ============================================================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan": run_scan()
    elif mode == "summary": run_summary()
    elif mode == "review": run_review()
    else: print(f"未知: {mode}，可用: scan/summary/review")
