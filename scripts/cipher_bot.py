#!/usr/bin/env python3
"""
Cipher BTC 自动交易系统 v4 — 稳定盈利版
核心：6维度评分(0-100) + 自适应仓位 + 交易日志 + 全维度分析
运行: python3 cipher_bot.py [scan|summary|review|log]

v4 改进：
- 6维度评分系统（0-100分）：多框架对齐+价格结构+成交量+K线形态+R/R+动能
- 自适应下单：评分映射仓位15%-60%
- 交易日志：每次信号记录到 trades.jsonl
- 形态识别：Pin Bar、吞没、连续K线
- 多时间框架方向对齐检测
- 复盘增强：含历史信号统计
"""
import json
import logging
import sys
import os
import uuid
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import THREE_COMMAS, TELEGRAM, TRADING, ANALYSIS, SCORING, TRADE_LOG_FILE, PAIRS
from validator import validate_analysis

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
API_TIMEOUT_FAST = 5     # 价格查询（5秒）
API_TIMEOUT_NORMAL = 10  # K线数据（10秒）
API_TIMEOUT_SLOW = 15    # Telegram/Webhook（15秒）
WEBHOOK_RETRIES = 3      # Webhook 重试次数
CANDLE_CLOSE_BUFFER = 120  # K线收盘前120秒不下单（2分钟）

# v4 新增常量（从配置读取，避免双源）
MIN_SCORE_STRONG = TRADING.get("score_strong", 80)
MIN_SCORE_GOOD = TRADING.get("score_good", 60)
MIN_SCORE_DECENT = TRADING.get("score_decent", 40)
VOL_SURGE_STRONG = 2.0    # 强放量阈值
VOL_SURGE_NORMAL = 1.5    # 正常放量阈值

# ============================================================
# 交易日志（v4）
# ============================================================
def log_trade(signal: dict, status: str = "sent"):
    """记录信号到 trades.jsonl"""
    record = {
        "id": str(uuid.uuid4())[:8],
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pair": signal.get("pair", "?"),
        "symbol": signal.get("symbol", "?"),
        "direction": signal.get("direction"),
        "entry": signal.get("entry"),
        "stop_loss": signal.get("stop_loss"),
        "target": signal.get("target"),
        "stop_pct": signal.get("stop_pct"),
        "rr": signal.get("rr"),
        "score": signal.get("score"),
        "score_detail": signal.get("score_detail", {}),
        "amount_pct": signal.get("amount_pct"),
        "pattern": signal.get("pattern"),
        "status": status,  # sent / filled / stopped / took_profit / cancelled
        "pnl": None,       # 后续更新
        "close_price": None,
        "close_reason": None,
    }
    try:
        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
        with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(f"交易日志已记录 [{record['id']}]")
    except Exception as e:
        logger.error(f"交易日志写入失败: {e}")

def load_trade_history(days: int = 7) -> List[dict]:
    """加载最近N天的交易日志"""
    if not os.path.exists(TRADE_LOG_FILE):
        return []
    records = []
    cutoff = time.time() - days * 86400
    try:
        with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    t = datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp()
                    if t >= cutoff:
                        records.append(r)
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"读取交易日志失败: {e}")
    return records

# ============================================================
# 形态识别（v4）
# ============================================================
def detect_pin_bar(candle: dict) -> Optional[str]:
    """检测Pin Bar/锤子线/射击之星，返回形态名或None"""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    if body <= 0:
        return None
    lower = min(o, c) - l
    upper = h - max(o, c)
    # 锤子线/Pin Bar：下影线 >= 2倍实体
    if lower >= 2.5 * body and upper <= body * 0.3:
        return "锤子线/PinBar" if c > o else "吊颈线"
    # 射击之星：上影线 >= 2倍实体
    if upper >= 2.5 * body and lower <= body * 0.3:
        return "射击之星" if c < o else "倒锤子线"
    return None

def detect_engulfing(candle_now: dict, candle_prev: dict) -> Optional[str]:
    """检测吞没形态"""
    if candle_prev["close"] > candle_prev["open"] and candle_now["close"] < candle_now["open"]:
        if candle_now["close"] < candle_prev["open"] and candle_now["open"] > candle_prev["close"]:
            return "看跌吞没"
    if candle_prev["close"] < candle_prev["open"] and candle_now["close"] > candle_now["open"]:
        if candle_now["close"] > candle_prev["open"] and candle_now["open"] < candle_prev["close"]:
            return "看涨吞没"
    return None

def analyze_candles(klines: List[dict], direction: str, n: int = 6) -> dict:
    """分析最近N根K线，返回形态信息和连续K线计数"""
    result = {"patterns": [], "consecutive": 0, "confirmation": False}
    if len(klines) < 2:
        return result
    # 连续同向K线
    count = 0
    for v in reversed(klines):
        if direction == "long" and v["close"] > v["open"]:
            count += 1
        elif direction == "short" and v["close"] < v["open"]:
            count += 1
        else:
            break
    result["consecutive"] = count
    # 确认K线：最近两根K线方向与信号方向一致
    if len(klines) >= 2:
        recent_bull = klines[-1]["close"] > klines[-1]["open"]
        prev_bull = klines[-2]["close"] > klines[-2]["open"]
        if direction == "long" and recent_bull and prev_bull:
            result["confirmation"] = True
        elif direction == "short" and not recent_bull and not prev_bull:
            result["confirmation"] = True
    # 形态检测——扫描最近N根
    for i in range(max(0, len(klines)-n), len(klines)):
        pb = detect_pin_bar(klines[i])
        if pb:
            result["patterns"].append(f"#{i+1} {pb}")
        if i > 0:
            eg = detect_engulfing(klines[i], klines[i-1])
            if eg:
                result["patterns"].append(f"#{i+1} {eg}")
    return result

# ============================================================
# 多时间框架对齐（v4）
# ============================================================
def check_timeframe_alignment(klines_15m: List[dict], klines_1h: List[dict], direction: str) -> dict:
    """检查1H和15m趋势方向是否一致——用最近5根收盘价趋势和EMA相对位置判断"""
    c15 = [k["close"] for k in klines_15m]
    c1h = [k["close"] for k in klines_1h]

    # 近期趋势：最近5根收盘价的线性方向
    # 简单方法：最近5根均线方向 vs 更早5根均线方向
    sma15_recent = sum(c15[-5:])/5 if len(c15)>=5 else sum(c15)/len(c15)
    sma15_prior = sum(c15[-10:-5])/5 if len(c15)>=10 else sma15_recent
    sma1h_recent = sum(c1h[-5:])/5 if len(c1h)>=5 else sum(c1h)/len(c1h)
    sma1h_prior = sum(c1h[-10:-5])/5 if len(c1h)>=10 else sma1h_recent

    trend_15m = "up" if sma15_recent > sma15_prior else ("dn" if sma15_recent < sma15_prior else "flat")
    trend_1h = "up" if sma1h_recent > sma1h_prior else ("dn" if sma1h_recent < sma1h_prior else "flat")

    aligned = (trend_15m == trend_1h)
    both_align_with_dir = (
        (direction == "long" and trend_15m == "up" and trend_1h == "up") or
        (direction == "short" and trend_15m == "dn" and trend_1h == "dn")
    )
    divergent = (trend_15m != trend_1h and trend_15m != "flat" and trend_1h != "flat")
    return {
        "trend_15m": trend_15m, "trend_1h": trend_1h,
        "aligned": aligned, "both_aligned": both_align_with_dir, "divergent": divergent,
    }

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
def get_binance_price(symbol: str = "BTCUSDT") -> Optional[float]:
    data = api_get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", API_TIMEOUT_FAST)
    return float(data["price"]) if data else None

def get_24h_ticker(symbol: str = "BTCUSDT") -> Optional[dict]:
    data = api_get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}", API_TIMEOUT_FAST)
    if data:
        return {
            "symbol": symbol,
            "high": float(data["highPrice"]),
            "low": float(data["lowPrice"]),
            "volume": float(data["volume"]),
            "change_pct": float(data["priceChangePercent"]),
            "last_price": float(data["lastPrice"]),
            "quote_volume": float(data["quoteVolume"]),
        }
    return None

def get_klines(symbol: str, interval: str, limit: int = 50) -> Optional[List[dict]]:
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
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
    """用EMA排列判断趋势——EMA20/50/100 三层验证"""
    if len(klines) < 20:
        return {"structure": "unknown", "direction": "neutral", "score": 0}

    closes = [k["close"] for k in klines]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, min(50, len(closes)))  # 数据不够50根就取最大
    slow_ma = calc_ema(closes, min(100, len(closes)))  # 用真实EMA100或最大可用

    score = 0
    if ema20 > ema50: score += 1
    if ema50 > slow_ma: score += 1
    if closes[-1] > ema20: score += 1

    if score >= 2:
        return {"structure": "uptrend", "direction": "bullish", "score": score}
    elif score <= 0:
        return {"structure": "downtrend", "direction": "bearish", "score": score}
    else:
        return {"structure": "ranging", "direction": "neutral", "score": score}

# ============================================================
# 信号评分系统 v4 — 6维度 0-100分
# ============================================================
def score_signal(
    direction: str, price: float,
    stop_loss: float, target: float,
    klines_15m: List[dict], klines_1h: List[dict],
    structure: dict, rsi_1h: float, atr_1h: float,
    alignment: dict = None,         # v4: 多框架对齐
    candle_analysis: dict = None,   # v4: 形态分析
    near_support: bool = False,
    near_resistance: bool = False,
    support_level: float = 0,
    resistance_level: float = 0,
) -> Tuple[int, dict, List[str], List[str]]:
    """
    6维度评分系统（总分100）：
    1. 多时间框架对齐 15分
    2. 价格结构/关键位 25分
    3. 成交量验证 20分
    4. K线形态确认 15分
    5. 风险收益比 15分
    6. 动能与背离 10分
    """
    score_detail = {}
    reasons = []
    risks = []

    closes_15m = [k["close"] for k in klines_15m]
    closes_1h = [k["close"] for k in klines_1h]
    vols_15m = [k["volume"] for k in klines_15m]
    stop_pct = abs(price - stop_loss) / price * 100 if stop_loss > 0 else 99
    rr = abs(target - price) / abs(price - stop_loss) if abs(price - stop_loss) > 0 else 0

    # 各维度满分（从配置读取，验证一致性）
    MAX_TF = SCORING.get("timeframe_alignment", 15)
    MAX_PS = SCORING.get("price_structure", 25)
    MAX_VOL = SCORING.get("volume_verification", 20)
    MAX_CP = SCORING.get("candle_pattern", 15)
    MAX_RR = SCORING.get("risk_reward", 15)
    MAX_MM = SCORING.get("momentum", 10)
    assert MAX_TF + MAX_PS + MAX_VOL + MAX_CP + MAX_RR + MAX_MM == 100, \
        f"SCORING权重总分={MAX_TF+MAX_PS+MAX_VOL+MAX_CP+MAX_RR+MAX_MM}≠100"

    # ——— 维度1：多时间框架对齐（15分）———
    tf_score = 5  # 基础分
    if alignment:
        if alignment["both_aligned"]:
            tf_score = 15
            reasons.append("多框架同向，趋势共振")
        elif alignment["aligned"]:
            tf_score = 10
            reasons.append("多框架方向一致")
        elif alignment["divergent"]:
            tf_score = 3
            risks.append("多框架方向分歧/背离")
    score_detail["timeframe_alignment"] = tf_score

    # ——— 维度2：价格结构（25分）———
    ps_score = 5
    if near_support and direction == "long":
        ps_score = 20
        reasons.append("精准触发支撑位")
        if support_level > 0:
            dist_to_support = (price - support_level) / price * 100
            if dist_to_support < 0.3:
                ps_score = 25
                reasons.append("紧贴支撑位，极佳入场点")
            elif dist_to_support < 1.0:
                ps_score = 22
    elif near_resistance and direction == "short":
        ps_score = 20
        reasons.append("精准触发阻力位")
        if resistance_level > 0:
            dist_to_resistance = (resistance_level - price) / price * 100
            if dist_to_resistance < 0.3:
                ps_score = 25
                reasons.append("紧贴阻力位，极佳入场点")
            elif dist_to_resistance < 1.0:
                ps_score = 22
    elif direction == "long" and near_support:
        ps_score = 15
        reasons.append("接近支撑位")
    elif direction == "short" and near_resistance:
        ps_score = 15
        reasons.append("接近阻力位")
    else:
        risks.append("未在关键位触发")
    # 趋势加分（顺大势）
    if structure and structure["direction"] == ("bullish" if direction == "long" else "bearish"):
        ps_score = min(ps_score + 3, 25)
        reasons.append("市场结构支持方向")
    score_detail["price_structure"] = ps_score

    # ——— 维度3：成交量验证（20分）———
    vol_score = 5
    if len(vols_15m) >= 6:
        avg_vol = sum(vols_15m[-6:-3]) / 3 if len(vols_15m) >= 6 else 0
        recent_vol = sum(vols_15m[-3:]) / 3
        # 关键位放量
        if near_support or near_resistance:
            if avg_vol > 0 and vols_15m[-1] > avg_vol * VOL_SURGE_STRONG:
                vol_score = 18
                reasons.append("关键位放量验证(>2x)")
            elif avg_vol > 0 and vols_15m[-1] > avg_vol * VOL_SURGE_NORMAL:
                vol_score = 14
                reasons.append("关键位放量验证(>1.5x)")
        # 整体量能趋势
        if avg_vol > 0:
            vol_ratio = recent_vol / avg_vol
            if vol_ratio > VOL_SURGE_STRONG:
                vol_score = max(vol_score, 16)
                reasons.append("近3根均量放大(>2x)")
            elif vol_ratio > VOL_SURGE_NORMAL:
                vol_score = max(vol_score, 12)
                reasons.append("近3根均量放大(>1.5x)")
            elif vol_ratio < 0.7:
                if vol_score > 5:
                    risks.append("近期量能萎缩")
                else:
                    vol_score = 3
                    risks.append("成交量萎缩，信号可靠性降低")
    score_detail["volume_verification"] = vol_score

    # ——— 维度4：K线形态确认（15分）———
    cp_score = 5
    if candle_analysis:
        if candle_analysis["patterns"]:
            cp_score = 12
            reasons.append(f"形态确认: {', '.join(candle_analysis['patterns'][:2])}")
        if candle_analysis["consecutive"] >= 3:
            cp_score = min(cp_score + 2, 15)
            reasons.append(f"{candle_analysis['consecutive']}根同向K线，动能强")
        if candle_analysis["confirmation"]:
            cp_score = min(cp_score + 2, 15)
            reasons.append("确认K线方向一致")
    # 三连阳/阴
    if len(klines_15m) >= 3:
        last3 = klines_15m[-3:]
        if direction == "long" and all(k["close"] > k["open"] for k in last3):
            cp_score = max(cp_score, 13)
            reasons.append("15m三连阳")
        elif direction == "short" and all(k["close"] < k["open"] for k in last3):
            cp_score = max(cp_score, 13)
            reasons.append("15m三连阴")
    score_detail["candle_pattern"] = cp_score

    # ——— 维度5：风险收益比（15分）———
    rr_score = 0
    if rr >= 3.0:
        rr_score = 15
        reasons.append(f"高盈亏比({rr:.1f}:1)")
    elif rr >= 2.5:
        rr_score = 10
        reasons.append(f"良好盈亏比({rr:.1f}:1)")
    elif rr >= 2.0:
        rr_score = 5
        reasons.append(f"合格盈亏比({rr:.1f}:1)")
    else:
        risks.append(f"盈亏比不足({rr:.1f}:1)")
    score_detail["risk_reward"] = rr_score

    # ——— 维度6：动能与背离（10分）———
    mm_score = 5
    # RSI位置
    if direction == "long" and rsi_1h < 35:
        mm_score = 8
        reasons.append("1H RSI超卖区，反弹概率增加")
    elif direction == "short" and rsi_1h > 65:
        mm_score = 8
        reasons.append("1H RSI超买区，回调风险增加")
    elif direction == "long" and rsi_1h < 45:
        mm_score = 7
        reasons.append("1H RSI偏低")
    elif direction == "short" and rsi_1h > 55:
        mm_score = 7
        reasons.append("1H RSI偏高")
    # 止损质量
    if stop_pct <= 0.3:
        mm_score = min(mm_score + 2, 10)
        reasons.append("止损极小(≤0.3%)")
    elif stop_pct <= 0.6:
        mm_score = min(mm_score + 1, 10)
        reasons.append("止损小(≤0.6%)")
    score_detail["momentum"] = mm_score

    # ——— 总分 ———
    total = tf_score + ps_score + vol_score + cp_score + rr_score + mm_score
    reasons.append(f"6维度评分={total}")

    return total, score_detail, reasons, risks

# ============================================================
# 主信号引擎
# ============================================================
def find_trading_signal(price: float, ticker_24h: dict,
                        klines_15m: List[dict], klines_1h: List[dict],
                        klines_4h: List[dict],
                        max_stop_pct: float = 1.2) -> Tuple[Optional[dict], dict]:
    """返回 (signal, indicators) — signal=None 表示无信号, indicators 含计算指标供日志使用"""
    if not all([price, ticker_24h, klines_15m, klines_1h, klines_4h]):
        return None, {}

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

    # 组装 indicators 供日志使用，避免 run_scan 重复计算
    ema9_1h = calc_ema(closes_1h, 9)
    indicators = {
        "rsi_15m": rsi_15m, "rsi_1h": rsi_1h,
        "atr_1h": atr_1h, "atr_4h": atr_4h,
        "ema9_1h": ema9_1h, "ema21_1h": ema21_1h, "ema21_4h": ema21_4h,
        "structure_1h": structure_1h,
    }

    high_24h = ticker_24h["high"]
    low_24h = ticker_24h["low"]
    range_24h = high_24h - low_24h
    position_pct = (price - low_24h) / range_24h * 100 if range_24h > 0 else 50

    sr_15m_high = max(k["high"] for k in klines_15m[-10:])
    sr_15m_low = min(k["low"] for k in klines_15m[-10:])

    # ——— ATR 动态乘数 ———
    local_vol = calc_local_atr(klines_15m, 3)
    vol_ratio = local_vol / atr_1h if atr_1h > 0 else 1.0
    atr_multiplier = max(0.3, min(0.8, vol_ratio * 0.5))
    base_stop_atr = atr_1h * atr_multiplier

    # ——— K线收盘确认 ———
    if is_candle_closing(klines_15m, 15):
        return None, indicators

    # ——— v4: 多框架对齐检测 ———
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

        if stop_pct < max_stop_pct and rr >= TRADING["min_rr_ratio"]:
            # v4: 增强分析
            alignment = check_timeframe_alignment(klines_15m, klines_1h, "long")
            candle_analysis = analyze_candles(klines_15m, "long")

            sig_score, score_detail, reasons, risks = score_signal(
                "long", price, stop_loss, target,
                klines_15m, klines_1h, structure_1h, rsi_1h, atr_1h,
                alignment=alignment, candle_analysis=candle_analysis,
                near_support=near_support, support_level=support_level,
            )
            if sig_score >= MIN_SCORE_DECENT:
                candidates.append({
                    "direction": "long", "entry": price,
                    "entry_range": f"${max(support_level, price-30):.0f} - ${price+30:.0f}",
                    "stop_loss": round(stop_loss, 1),
                    "target": round(target, 1),
                    "stop_pct": round(stop_pct, 2),
                    "target_pct": round(target_pct, 2),
                    "rr": round(rr, 2),
                    "pattern": "支撑位做多",
                    "score": sig_score, "score_detail": score_detail,
                    "reasons": reasons, "risks": risks,
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

        if stop_pct < max_stop_pct and rr >= TRADING["min_rr_ratio"]:
            alignment = check_timeframe_alignment(klines_15m, klines_1h, "short")
            candle_analysis = analyze_candles(klines_15m, "short")

            sig_score, score_detail, reasons, risks = score_signal(
                "short", price, stop_loss, target,
                klines_15m, klines_1h, structure_1h, rsi_1h, atr_1h,
                alignment=alignment, candle_analysis=candle_analysis,
                near_resistance=near_resistance, resistance_level=resistance_level,
            )
            if sig_score >= MIN_SCORE_DECENT:
                candidates.append({
                    "direction": "short", "entry": price,
                    "entry_range": f"${price-30:.0f} - ${min(resistance_level, price+30):.0f}",
                    "stop_loss": round(stop_loss, 1),
                    "target": round(target, 1),
                    "stop_pct": round(stop_pct, 2),
                    "target_pct": round(target_pct, 2),
                    "rr": round(rr, 2),
                    "pattern": "阻力位做空",
                    "score": sig_score, "score_detail": score_detail,
                    "reasons": reasons, "risks": risks,
                })

    if candidates:
        candidates.sort(key=lambda s: s["score"] * s["rr"], reverse=True)
        best = candidates[0]
        # 趋势乘数：顺趋势加仓，逆趋势减仓
        ema9 = ema9_1h
        ema21 = ema21_1h
        direction = best["direction"]
        trend_aligned = (direction == "long" and ema9 > ema21) or (direction == "short" and ema9 < ema21)
        trend_factor = TRADING.get("trend_aligned_mult", 1.3) if trend_aligned else TRADING.get("trend_against_mult", 0.7)
        best["trend_factor"] = trend_factor
        best["amount_pct"] = calc_position_size(best["score"], atr_1h, price=price, trend_factor=trend_factor)
        logger.info(f"候选:{len(candidates)} 最佳:{best['direction']} 评分{best['score']}/100 R/R={best['rr']} 仓位:{best['amount_pct']}%")
        return best, indicators
    return None, indicators

def calc_position_size(score: int, atr: float, price: float = None,
                       trend_factor: float = 1.0) -> int:
    """根据评分+波动率+价格自适应计算下单量百分比

    - score 越高 → 仓位越大（0-100映射15%-60%）
    - ATR% 越高 → 波动越大 → 仓位减少
    - base_amount 各币种独立配置
    """
    # 波动率调整：用 ATR% 替代绝对值（ETH的ATR=$11但BTC的ATR=$326）
    atr_pct = atr / price * 100 if price and price > 0 else 0.5
    if atr_pct > 1.0:      vol_factor = 0.6   # 极高波动 → 减仓
    elif atr_pct > 0.7:    vol_factor = 0.7   # 高波动
    elif atr_pct > 0.4:    vol_factor = 0.85  # 中波动
    else:                  vol_factor = 1.0   # 低波动

    # 评分→仓位映射（base_amount 仅作为最基础下限，不覆盖各等级）
    if score >= MIN_SCORE_STRONG:
        base = TRADING["size_strong_min"]
        size_range = TRADING["size_strong_max"] - TRADING["size_strong_min"]
        score_extra = min(score - MIN_SCORE_STRONG, 20) / 20.0
    elif score >= MIN_SCORE_GOOD:
        base = TRADING["size_good_min"]
        size_range = TRADING["size_good_max"] - TRADING["size_good_min"]
        score_extra = min(score - MIN_SCORE_GOOD, 20) / 20.0
    elif score >= MIN_SCORE_DECENT:
        base = TRADING["size_decent_min"]
        size_range = TRADING["size_decent_max"] - TRADING["size_decent_min"]
        score_extra = min(score - MIN_SCORE_DECENT, 20) / 20.0
    else:
        return 0

    raw = int((base + int(size_range * score_extra * vol_factor)) * trend_factor)
    return max(10, min(60, raw))

# ============================================================
# Telegram — 改进：chat_id 持久化到文件
# ============================================================
def get_chat_id() -> Optional[int]:
    """从文件读取缓存的 chat_id"""
    if os.path.exists(CHAT_ID_FILE):
        try:
            return int(open(CHAT_ID_FILE).read().strip())
        except Exception:
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

def send_webhook(signal: dict, bot_uuid: str = None, secret: str = None) -> bool:
    """改进：失败自动重试3次 + Telegram 告警"""
    secret = secret or THREE_COMMAS.get("secret", "")
    if not bot_uuid:
        logger.warning("Webhook未配置bot_uuid，跳过")
        return False
    if not secret:
        logger.warning("Webhook未配置secret，跳过")
        return False

    action = "enter_long" if signal["direction"] == "long" else "enter_short"
    stop_pct = signal.get("stop_pct", 1.0)
    target_pct = signal.get("target_pct", 2.0)

    payload = {
        "secret": secret,
        "max_lag": "300",
        "timestamp": "{{timenow}}",
        "trigger_price": "{{close}}",
        "tv_exchange": "{{exchange}}",
        "tv_instrument": "{{ticker}}",
        "action": action,
        "bot_uuid": bot_uuid,
        "order": {
            "amount": "25",  # 会被下面 amount_pct 覆盖
            "currency_type": "margin_percent",
            "order_type": TRADING["order_type"],
            "price": str(int(signal["entry"])),
        },
    }

    # 动态止损止盈（3Commas 收到后会去币安下对应的订单）
    payload["stop_loss"] = str(round(stop_pct, 1))
    payload["take_profit"] = str(round(target_pct, 1))

    amount_pct = signal.get("amount_pct", 25)
    payload["order"]["amount"] = str(amount_pct)

    result = api_post_with_retry(THREE_COMMAS["webhook_url"], payload, WEBHOOK_RETRIES)
    if result:
        pair_name = signal.get("pair", "?")
        logger.info(f"✅ {pair_name} Webhook: {action} @ ${signal['entry']:.0f} 仓位:{amount_pct}%")
        return True

    pair_name = signal.get("pair", "?")
    logger.error(f"❌ {pair_name} Webhook 失败({WEBHOOK_RETRIES}次)")
    send_telegram(f"⚠️ *Webhook 发送失败*\n{pair_name}信号已出但3Commas未收到\n方向：{action} @ ${signal['entry']:.0f}\n请手动处理！")
    return False

def format_signal(signal: dict) -> str:
    pair = signal.get("pair", "BTC")
    emoji = "🟢" if signal["direction"] == "long" else "🔴"
    dir_cn = "做多" if signal["direction"] == "long" else "做空"
    amt = signal.get("amount_pct", 25)

    # 评分等级
    score = signal.get("score", 0)
    if score >= MIN_SCORE_STRONG:
        level = "🔥 强信号"
    elif score >= MIN_SCORE_GOOD:
        level = "✅ 好信号"
    elif score >= MIN_SCORE_DECENT:
        level = "⚠️ 一般信号"
    else:
        level = "❌ 弱信号"

    # 评分明细
    sd = signal.get("score_detail", {})
    detail_str = (
        f"  ├ 多框架对齐 {sd.get('timeframe_alignment',0)}/15\n"
        f"  ├ 价格结构 {sd.get('price_structure',0)}/25\n"
        f"  ├ 成交量验证 {sd.get('volume_verification',0)}/20\n"
        f"  ├ K线形态 {sd.get('candle_pattern',0)}/15\n"
        f"  ├ 风险收益比 {sd.get('risk_reward',0)}/15\n"
        f"  └ 动能背离 {sd.get('momentum',0)}/10"
    )

    reasons = signal.get("reasons", [])
    # 去掉末尾的总分理由
    display_reasons = [r for r in reasons if not r.startswith("6维度评分")]
    reasons_str = "\n".join(f"  ✅ {r}" for r in display_reasons[:5])
    risks_str = "\n".join(f"  ⚠️ {r}" for r in signal.get("risks", [])[:5]) if signal.get("risks") else "  无明显风险"

    return (
        f"📊 *Cipher {pair} 超短线信号*\n"
        f"{level} | 方向：{dir_cn} {emoji}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"入场：{signal['entry_range']}\n"
        f"当前：${signal['entry']:.0f}\n"
        f"止损：${signal['stop_loss']:.0f}（{signal['stop_pct']:.2f}%）\n"
        f"目标：${signal['target']:.0f}（+{signal['target_pct']:.2f}%）\n"
        f"盈亏比：{signal['rr']}:1\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"评分：{score}/100 | 仓位：{amt}% | {signal['pattern']}\n\n"
        f"*评分明细：*\n{detail_str}\n\n"
        f"*关键理由：*\n{reasons_str}\n\n"
        f"*风险提示：*\n{risks_str}\n\n"
        f"杠杆{signal.get('leverage', 25)}x | 止损小，盈亏比高 ✅"
    )

# ============================================================
# 主函数
# ============================================================
def run_scan():
    logger.info("=" * 50)
    logger.info("Cipher v4 多币种扫描")

    signals_found = 0
    for symbol, pconf in PAIRS.items():
        if not pconf.get("enabled", False):
            continue

        pair_name = pconf.get("name", symbol)
        logger.info(f"--- {pair_name} ({symbol}) ---")

        price = get_binance_price(symbol)
        ticker = get_24h_ticker(symbol)
        if not price or not ticker:
            logger.warning(f"{pair_name}: 价格数据获取失败")
            continue

        logger.info(f"${price:,.2f} | 24h ${ticker['low']:,.0f}-${ticker['high']:,.0f} {ticker['change_pct']:+.2f}%")

        k15 = get_klines(symbol, "15m", 30)
        k1h = get_klines(symbol, "1h", 30)
        k4h = get_klines(symbol, "4h", 24)
        if not all([k15, k1h, k4h]):
            logger.warning(f"{pair_name}: K线数据获取失败")
            continue

        pair_max_stop = pconf.get("max_stop_pct", 1.2)
        signal, indicators = find_trading_signal(price, ticker, k15, k1h, k4h,
                                                   max_stop_pct=pair_max_stop)

        ind = indicators
        if ind:
            logger.info(f"  RSI 15m={ind['rsi_15m']:.0f} 1h={ind['rsi_1h']:.0f} | ATR 1h=${ind['atr_1h']:.1f}")
            logger.info(f"  结构: {ind['structure_1h']['structure']} | EMA9/21 1h: {'多头' if ind['ema9_1h']>ind['ema21_1h'] else '空头'}")

        if signal:
            score = signal.get("score", 0)
            min_score = pconf.get("min_score", MIN_SCORE_GOOD)
            if score < min_score:
                logger.info(f"  ⏸️ 信号评分{score}<{min_score}({pair_name}最低要求)，跳过")
                continue
            logger.info(f"  ✅ 信号! {signal['direction']} 评分{score}/100 R/R={signal['rr']}")

            # 给信号打上pair标记
            signal["pair"] = pair_name
            signal["symbol"] = symbol

            # 验证器
            claims_for_validation = {
                "current_price": price,
                "entry": signal.get("entry"),
                "stop_loss": signal.get("stop_loss"),
                "target": signal.get("target"),
                "stop_pct": signal.get("stop_pct"),
                "rr": signal.get("rr"),
                "rsi": ind.get("rsi_1h", 50) if ind else 50,
                "direction": signal.get("direction"),
                "pattern": signal.get("pattern", ""),
                "score": score,
                "score_detail": signal.get("score_detail", {}),
            }
            vresult = validate_analysis(claims_for_validation,
                                         {"price_actual": price, "closes_15m": [k["close"] for k in k15]})
            if not vresult["passed"]:
                logger.error(f"  ❌ 验证器拦截: {vresult['summary']}")
                send_telegram(f"⚠️ {pair_name} *验证器拦截信号*\n{vresult['summary']}\n\n信号未发送")
                continue

            for key, val in vresult.get("corrections", {}).items():
                if key == "rr":
                    signal["rr"] = val
                elif key == "stop_pct":
                    signal["stop_pct"] = val

            # 调整杠杆显示
            lev = pconf.get("leverage", 25)
            signal["leverage"] = lev

            log_trade(signal, "sent")
            send_telegram(format_signal(signal))
            # 发送webhook（使用币种专属bot_uuid）
            send_webhook(signal, bot_uuid=pconf.get("bot_uuid"), secret=THREE_COMMAS.get("secret"))
            signals_found += 1

    if signals_found == 0:
        logger.info("本次无信号")
    else:
        logger.info(f"本次共 {signals_found} 个信号")

def run_summary():
    """每4小时推送：所有币种4H级别技术指标"""
    parts = []
    for symbol, pconf in PAIRS.items():
        if not pconf.get("enabled", False):
            continue
        pair_name = pconf.get("name", symbol)
        ticker = get_24h_ticker(symbol)
        k4h = get_klines(symbol, "4h", 24)
        if not ticker or not k4h:
            continue

        price = ticker["last_price"]
        closes_4h = [k["close"] for k in k4h]
        rsi_4h = calc_rsi(closes_4h, 14)
        atr_4h = calc_atr(k4h, 14)
        ema21_4h = calc_ema(closes_4h, 21)
        struct = detect_market_structure(k4h)
        sr_high = max(k["high"] for k in k4h)
        sr_low = min(k["low"] for k in k4h)
        struct_cn = {"uptrend": "上升", "downtrend": "下降", "ranging": "震荡", "unknown": "不明"}
        rsi_label = "超买🔥" if rsi_4h > 65 else ("超卖🧊" if rsi_4h < 35 else "中性")

        parts.append(
            f"*{pair_name}*\n"
            f"当前：${price:,.2f}（{ticker['change_pct']:+.2f}%）\n"
            f"RSI(14)：{rsi_4h:.0f} {rsi_label}\n"
            f"ATR：${atr_4h:.1f} | EMA21：${ema21_4h:,.0f}\n"
            f"结构：{struct_cn.get(struct['structure'], '?')}\n"
            f"阻力：${sr_high:,.0f} | 支撑：${sr_low:,.0f}\n"
        )

    if not parts:
        return
    msg = "📊 *Cipher 短线分析 (4H)*\n━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(parts)
    msg += "\n☝️ 回踩做多  👇 反弹做空  ⏸️ 中间观望"
    send_telegram(msg)

def run_review():
    """每日复盘：所有币种日线+信号统计"""
    # ——— 行情部分 ———
    daily_parts = []
    for symbol, pconf in PAIRS.items():
        if not pconf.get("enabled", False):
            continue
        pair_name = pconf.get("name", symbol)
        ticker = get_24h_ticker(symbol)
        k1d = get_klines(symbol, "1d", 7)
        if not ticker or not k1d:
            continue

        open_p = k1d[-1]["open"]
        close_p = ticker["last_price"]
        change_pct = (close_p - open_p) / open_p * 100 if open_p else 0
        closes_d = [k["close"] for k in k1d]
        rsi_d = calc_rsi(closes_d, 14)
        ema7_d = calc_ema(closes_d, 7)
        ema21_d = calc_ema(closes_d, 21)
        trend_icon = "📈" if ema7_d > ema21_d else "📉"

        daily_parts.append(
            f"*{pair_name}*\n"
            f"开盘 ${open_p:,.0f} → 收盘 ${close_p:,.0f}（{change_pct:+.2f}%）\n"
            f"最高 ${ticker['high']:,.0f} / 最低 ${ticker['low']:,.0f}\n"
            f"RSI(14)：{rsi_d:.0f} | EMA7 ${ema7_d:,.0f}\n"
            f"趋势：{trend_icon}\n"
        )

    if not daily_parts:
        return

    # ——— 信号统计部分 ———
    history = load_trade_history(days=1)
    if history:
        # 按币种分组
        btc_signals = [h for h in history if h.get("symbol") == "BTCUSDT" or h.get("pair") == "BTC"]
        eth_signals = [h for h in history if h.get("symbol") == "ETHUSDT" or h.get("pair") == "ETH"]
        signal_lines = []
        for label, sigs in [("BTC", btc_signals), ("ETH", eth_signals)]:
            if sigs:
                n = len(sigs)
                avg_s = sum(h.get("score", 0) or 0 for h in sigs) / n
                avg_r = sum(h.get("rr", 0) or 0 for h in sigs) / n
                n_long = sum(1 for h in sigs if h.get("direction") == "long")
                n_short = sum(1 for h in sigs if h.get("direction") == "short")
                signal_lines.append(f"{label}: {n}次信号 | 做多{n_long}/做空{n_short} | 均分{avg_s:.0f} | 均R/R{avg_r:.1f}")
        signal_text = "\n".join(signal_lines) if signal_lines else "今日暂无信号"
    else:
        signal_text = "今日暂无信号"

    msg = (
        f"📈 *Cipher每日复盘*\n{datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"━━━━━ 行情 ━━━━━\n\n"
        f"{chr(10).join(daily_parts)}"
        f"━━━━━ 信号 ━━━━━\n\n"
        f"{signal_text}\n\n"
        f"*策略*\n严格按信号执行，无信号不做。宁可错过不乱做。\n\n⚠️ DYOR"
    )
    send_telegram(msg)

def run_log():
    """查看最近交易日志"""
    history = load_trade_history(days=7)
    if not history:
        print("暂无交易记录")
        return
    print(f"近7天共 {len(history)} 条信号记录:")
    print(f"{'ID':8} {'时间':20} {'方向':6} {'评分':6} {'R/R':6} {'仓位':6} {'形态':16} {'状态':10}")
    print("-" * 80)
    for h in history:
        t = h.get("time", "")[11:19]
        print(f"{h.get('id','?'):8} {t:20} {h.get('direction','?'):6} {h.get('score',0):6} {h.get('rr',0):6.1f} {h.get('amount_pct',0):6} {h.get('pattern','?'):16} {h.get('status','?'):10}")

# ============================================================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan": run_scan()
    elif mode == "summary": run_summary()
    elif mode == "review": run_review()
    elif mode == "log": run_log()
    elif mode == "history": run_log()
    else: print(f"未知: {mode}，可用: scan/summary/review/log")
