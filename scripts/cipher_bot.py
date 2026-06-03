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
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import TELEGRAM, TRADING, ANALYSIS, SCORING, TRADE_LOG_FILE, PAIRS, VERSION
from config import SMC_CONFIG, CONTEXT_CONFIG, FETCH_LIMITS
from validator import validate_analysis
from smc import detect_fvg, is_price_in_fvg
from market_context import evaluate_market_context
from market_regime import classify_regime, get_regime_params, get_score_adjustment
from indicators import calc_sma, calc_ema, calc_rsi, calc_atr, calc_local_atr
from indicators import calc_vwap, calc_bollinger_bands
from order_flow import analyze as analyze_order_flow
from signal_voter import evaluate_vote
from cornix_adapter import send_cornix
from telegram_adapter import send_telegram, format_signal
from vrvp import calculate_vrvp, describe as describe_vrvp
from risk_control import RiskEngine
from macro import MacroContext
from strategies import route_strategy, breakout_retest_strategy, fakeout_reversal_strategy, make_signal
from strategy_router import StrategyRouter
from safety import generate_signal_id, is_signal_executed, mark_signal_executed
from safety import check_event_blackout, check_health, generate_daily_report
from binance_reconciler import reconcile, load_local_positions
from position_state_machine import PositionStateMachine
from regime_transition_manager import RegimeTransitionManager
from safety import log_rejected_signal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
CHAT_ID_FILE = os.path.join(BASE_DIR, ".chat_id")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "cipher.log"), encoding="utf-8"),
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
CANDLE_CLOSE_BUFFER = 120  # K线收盘前120秒不下单（2分钟）
MAX_TRADE_LOG_SIZE = 10 * 1024 * 1024  # 日志10MB轮转
LAST_SIGNAL_FILE = os.path.join(LOG_DIR, "last_signal.json")
SIGNAL_COOLDOWN_SEC = 1800  # 同币种同向信号冷却30分钟（动态调整: 趋势强时自动缩短）

# v4 新增常量（从配置读取，避免双源）
MIN_SCORE_STRONG = TRADING.get("score_strong", 80)
MIN_SCORE_GOOD = TRADING.get("score_good", 60)
MIN_SCORE_DECENT = TRADING.get("score_decent", 40)
VOL_SURGE_STRONG = 2.0    # 强放量阈值
VOL_SURGE_NORMAL = 1.5    # 正常放量阈值

# v5 SMC 常量
FVG_TOLERANCE = SMC_CONFIG.get("fvg_tolerance_pct", 0.15)
FVG_SCORE_BONUS = SMC_CONFIG.get("fvg_score_bonus", 5)

# 活跃币种列表（从配置读取）
ACTIVE_PAIRS = [k for k, v in PAIRS.items() if v.get("enabled", False)]

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
        # 日志超10MB自动轮转
        if os.path.exists(TRADE_LOG_FILE) and os.path.getsize(TRADE_LOG_FILE) > MAX_TRADE_LOG_SIZE:
            os.rename(TRADE_LOG_FILE, TRADE_LOG_FILE + ".old")
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

# ============================================================
# 合约市场数据：OI + 资金费率（v5）
# ============================================================
def get_open_interest(symbol: str = "BTCUSDT") -> Optional[float]:
    """获取当前未平仓合约量"""
    data = api_get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}", API_TIMEOUT_FAST)
    return float(data["openInterest"]) if data else None

def get_oi_history(symbol: str = "BTCUSDT", period: str = "15m", limit: int = 96) -> Optional[List[dict]]:
    """获取历史OI数据（按period间隔，用于分析OI趋势）"""
    url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}"
    return api_get(url, API_TIMEOUT_NORMAL)

def get_funding_rates(symbol: str = "BTCUSDT", limit: int = 50) -> Optional[List[dict]]:
    """获取历史资金费率"""
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit={limit}"
    return api_get(url, API_TIMEOUT_FAST)

def get_contract_data(symbol: str = "BTCUSDT") -> dict:
    """一次性获取所有合约数据"""
    return {
        "oi": get_open_interest(symbol),
        "oi_history": get_oi_history(symbol),
        "funding": get_funding_rates(symbol),
    }

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
    fvg_info: dict = None,          # v5: FVG信息
    vwap: float = 0,                # v5: VWAP
    bb: dict = None,                # v5: 布林带
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
    # v5: VWAP方向确认
    if vwap > 0:
        if direction == "long" and price >= vwap:
            tf_score = min(15, tf_score + 3)
            reasons.append("价格在VWAP之上，偏强")
        elif direction == "short" and price <= vwap:
            tf_score = min(15, tf_score + 3)
            reasons.append("价格在VWAP之下，偏弱")
        elif direction == "long" and price < vwap * 0.99:
            risks.append("价格远低于VWAP，做多逆势")
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
    # v5: FVG 加成 — 机构入场印记
    if fvg_info and fvg_info.get("in_fvg"):
        ps_score = min(ps_score + FVG_SCORE_BONUS, MAX_PS)
        reasons.append(f"FVG区域触发(+{FVG_SCORE_BONUS}分)")
    score_detail["price_structure"] = ps_score

    # ——— 维度3：成交量验证（20分）———
    vol_score = 5
    if len(vols_15m) >= 6:
        # 改用20期均量做基准（中位数抗异常值）
        baseline = sorted(vols_15m[-max(20, len(vols_15m)):-3])
        avg_vol = baseline[len(baseline)//2] if baseline else 0
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
    # v5: 布林带位置
    if bb and bb.get("bandwidth", 0) > 0:
        bw = bb["bandwidth"]
        pos = bb["position"]
        if bw < 10:
            reasons.append("布林带收窄，变盘前兆")
        if direction == "long" and pos < 20:
            mm_score = min(10, mm_score + 2)
            reasons.append("触及下轨，反弹概率增加")
        elif direction == "short" and pos > 80:
            mm_score = min(10, mm_score + 2)
            reasons.append("触及上轨，回调风险增加")
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
                        max_stop_pct: float = 1.2,
                        market_context=None,        # v5: 市场背景
                        market_regime=None,         # v5: 行情模式
                        ofi_info: dict = None) -> Tuple[Optional[dict], dict]:  # v5: 订单流
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
    # v5: 用已收盘的4h K线计算EMA（跳过当前未收盘的）
    closed_4h = closes_4h[:-1] if len(closes_4h) > 21 else closes_4h
    ema21_4h = calc_ema(closed_4h, 21)

    # 组装 indicators 供日志使用，避免 run_scan 重复计算
    ema9_1h = calc_ema(closes_1h, 9)
    indicators = {
        "rsi_15m": rsi_15m, "rsi_1h": rsi_1h,
        "atr_1h": atr_1h, "atr_4h": atr_4h,
        "ema9_1h": ema9_1h, "ema21_1h": ema21_1h, "ema21_4h": ema21_4h,
        "structure_1h": structure_1h,
    }

    # v5: 增强指标（FVG/VRVP/VWAP/布林带）
    fvg_list_15m = detect_fvg(klines_15m)
    indicators["fvg_count"] = len(fvg_list_15m)
    vwap = calc_vwap(klines_1h) if len(klines_1h) >= 10 else calc_vwap(klines_15m)
    bb = calc_bollinger_bands(klines_1h, 20, 2.0) if len(klines_1h) >= 20 else {}
    indicators["vwap"] = vwap
    indicators["bb"] = bb

    # v5: 行情模式参数
    regime_params = get_regime_params(market_regime) if market_regime else {}
    _regime_min_rr = regime_params.get("min_rr", TRADING["min_rr_ratio"])
    _regime_max_stop = regime_params.get("max_stop_pct", max_stop_pct)

    high_24h = ticker_24h["high"]
    low_24h = ticker_24h["low"]
    range_24h = high_24h - low_24h
    position_pct = (price - low_24h) / range_24h * 100 if range_24h > 0 else 50

    sr_15m_high = max(k["high"] for k in klines_15m[-10:])
    sr_15m_low = min(k["low"] for k in klines_15m[-10:])

    # ——— ATR 动态乘数 ———
    local_vol = calc_local_atr(klines_15m, 3)
    vol_ratio = local_vol / atr_1h if atr_1h > 0 else 1.0
    # v5: 止损乘数放宽 — 原0.15-0.4倍ATR太紧，现0.4-1.0倍ATR
    atr_multiplier = max(0.4, min(1.0, vol_ratio * 0.6))
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
        # v5: 止损放宽 — 原0.5倍ATR太紧，改1.5倍
        stop_loss = support_level - base_stop_atr * 1.5
        stop_pct = (price - stop_loss) / price * 100 if price > stop_loss else 0.5
        # 最小止损: 不低于0.6%（原0.48%）
        min_stop = round(0.5 * max_stop_pct, 2)
        stop_pct = max(stop_pct, min_stop, 0.6)

        # v5: 目标按固定R/R=2.5计算，不再依赖24h高位
        target = price + stop_pct * 2.5 * price / 100
        target_pct = (target - price) / price * 100
        rr = target_pct / stop_pct if stop_pct > 0 else 0

        # v5: FVG检测 — 看涨FVG匹配
        in_fvg_long, matched_fvg_long = is_price_in_fvg(price, fvg_list_15m, "long", FVG_TOLERANCE)
        fvg_info_long = {"in_fvg": in_fvg_long, "fvg": matched_fvg_long}

        if stop_pct < _regime_max_stop and rr >= _regime_min_rr:
            # v4: 增强分析
            alignment = check_timeframe_alignment(klines_15m, klines_1h, "long")
            candle_analysis = analyze_candles(klines_15m, "long")

            sig_score, score_detail, reasons, risks = score_signal(
                "long", price, stop_loss, target,
                klines_15m, klines_1h, structure_1h, rsi_1h, atr_1h,
                alignment=alignment, candle_analysis=candle_analysis,
                near_support=near_support, support_level=support_level,
                fvg_info=fvg_info_long,
                vwap=vwap, bb=bb,
            )
            # v5: 行情模式评分调整
            if market_regime:
                adj, adj_reason = get_score_adjustment(market_regime, "long")
                if adj != 0:
                    sig_score = max(0, min(100, sig_score + adj))
                    reasons.append(adj_reason)
            if ofi_info and ofi_info.get("ofi", 0) > 0.3:
                sig_score = min(100, sig_score + 3)
                reasons.append("OFI做多确认+3")
            if sig_score >= MIN_SCORE_DECENT:
                candidates.append({
                    "direction": "long", "entry": price,
                    "entry_range": f"${max(support_level, price-10):.0f} - ${price+10:.0f}",
                    "stop_loss": round(stop_loss, 1),
                    "target": round(target, 1),
                    "stop_pct": round(stop_pct, 2),
                    "target_pct": round(target_pct, 2),
                    "rr": round(rr, 2),
                    "pattern": "支撑位做多",
                    "score": sig_score, "score_detail": score_detail,
                    "reasons": reasons, "risks": risks,
                    "fvg_info": fvg_info_long,  # v5
                    "key_level": round(support_level, 1),  # v5: 关键位
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
        min_stop = round(0.4 * max_stop_pct, 2)
        stop_pct = max(stop_pct, min_stop)

        target = max(ema21_4h if price > ema21_4h else low_24h, low_24h)
        target = min(target, price * 0.992)
        target_pct = (price - target) / price * 100
        rr = target_pct / stop_pct if stop_pct > 0 else 0

        # v5: FVG检测 — 看跌FVG匹配
        in_fvg_short, matched_fvg_short = is_price_in_fvg(price, fvg_list_15m, "short", FVG_TOLERANCE)
        fvg_info_short = {"in_fvg": in_fvg_short, "fvg": matched_fvg_short}

        if stop_pct < _regime_max_stop and rr >= _regime_min_rr:
            alignment = check_timeframe_alignment(klines_15m, klines_1h, "short")
            candle_analysis = analyze_candles(klines_15m, "short")

            sig_score, score_detail, reasons, risks = score_signal(
                "short", price, stop_loss, target,
                klines_15m, klines_1h, structure_1h, rsi_1h, atr_1h,
                alignment=alignment, candle_analysis=candle_analysis,
                near_resistance=near_resistance, resistance_level=resistance_level,
                fvg_info=fvg_info_short,
                vwap=vwap, bb=bb,
            )
            # v5: 行情模式评分调整
            if market_regime:
                adj, adj_reason = get_score_adjustment(market_regime, "short")
                if adj != 0:
                    sig_score = max(0, min(100, sig_score + adj))
                    reasons.append(adj_reason)
            if ofi_info and ofi_info.get("ofi", 0) < -0.3:
                sig_score = min(100, sig_score + 3)
                reasons.append("OFI做空确认+3")
            if sig_score >= MIN_SCORE_DECENT:
                candidates.append({
                    "direction": "short", "entry": price,
                    "entry_range": f"${price-10:.0f} - ${price+10:.0f}",
                    "stop_loss": round(stop_loss, 1),
                    "target": round(target, 1),
                    "stop_pct": round(stop_pct, 2),
                    "target_pct": round(target_pct, 2),
                    "rr": round(rr, 2),
                    "pattern": "阻力位做空",
                    "score": sig_score, "score_detail": score_detail,
                    "reasons": reasons, "risks": risks,
                    "fvg_info": fvg_info_short,  # v5
                    "key_level": round(resistance_level, 1),  # v5: 关键位
                })

    # ===== 破位做空（下降趋势中跌破支撑顺势追空）=====
    if regime_params.get("prefer_long") is False and position_pct < 30:
        near_breakdown = price < low_24h * 1.01 or price < sr_15m_low * 1.005
        if near_breakdown and rsi_1h < 50:
            sl = price + base_stop_atr * 0.5
            sp = max((sl - price) / price * 100, 0.6) if sl > price else 0.6
            tg = price - sp * 2.5 * price / 100
            r = (price - tg) / price / sp * 100 if sp > 0 else 0
            if sp < _regime_max_stop and r >= _regime_min_rr:
                candidates.append({
                    "direction": "short", "entry": price,
                    "entry_range": f"${int(price-10)} - ${int(price+10)}",
                    "stop_loss": round(sl, 1), "target": round(tg, 1),
                    "stop_pct": round(sp, 2), "target_pct": round((price-tg)/price*100, 2),
                    "rr": round(r, 2), "pattern": "破位做空",
                    "score": 65, "score_detail": {},
                    "reasons": ["破位下跌+卖压确认"], "risks": [],
                    "key_level": round(low_24h, 1),
                })

    # v5: 多策略路由（震荡策略+剥头皮策略）
    extra = route_strategy(
        regime_label=regime_params.get("label", "?"),
        price=price, ticker=ticker_24h,
        k15=klines_15m, k1h=klines_1h, k4h=klines_4h,
        vrvp=calculate_vrvp(klines_4h if klines_4h else klines_1h, 50),
        ofi_info=ofi_info if ofi_info else {"ofi": 0, "strength": 0},
        rsi_1h=rsi_1h, rsi_15m=rsi_15m,
        atr_1h=atr_1h, structure=structure_1h,
        base_stop_atr=base_stop_atr,
        regime_params=regime_params, market_regime=market_regime,
    )
    for s in extra:
        s["signal_id"] = generate_signal_id()
        s["fvg_info"] = {"in_fvg": False}
        candidates.append(s)

    # v5: 方向过滤 — 下降趋势不做多，上升趋势不做空
    if regime_params.get("prefer_long") is False:
        candidates = [c for c in candidates if c["direction"] != "long"]
    elif regime_params.get("prefer_long") is True:
        candidates = [c for c in candidates if c["direction"] != "short"]

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
        # v5: 应用去风险乘数 + 行情模式调整
        derisk_mult = market_context.derisk_factor if market_context else 1.0
        regime_size_mult = regime_params.get("size_multiplier", 1.0) if market_regime else 1.0
        base_amount = calc_position_size(best["score"], atr_1h, price=price, trend_factor=trend_factor)
        best["amount_pct"] = max(10, int(base_amount * derisk_mult * regime_size_mult))
        best["derisk_factor"] = round(derisk_mult, 2)
        best["regime"] = regime_params.get("label", "?") if market_regime else "?"
        best["signal_id"] = generate_signal_id()
        best["strategy_version"] = VERSION
        best["quality_score"] = market_context.quality_score if market_context else 5.5
        best["entry_min"] = round(support_level if best["direction"] == "long" else price, 1)
        best["entry_max"] = round(price if best["direction"] == "long" else resistance_level, 1)
        logger.info(f"候选:{len(candidates)} 最佳:{best['direction']} 评分{best['score']}/100 "
                    f"R/R={best['rr']} 仓位:{best['amount_pct']}% "
                    f"去风险x{derisk_mult:.1f} 模式{best['regime']}")
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




def fetch_pair(symbol: str, pconf: dict):
    """并发获取单个币种数据"""
    price = get_binance_price(symbol)
    ticker = get_24h_ticker(symbol)
    if not price or not ticker:
        return symbol, pconf, None, None
    k15 = get_klines(symbol, "15m", FETCH_LIMITS.get("15m", 100))
    k1h = get_klines(symbol, "1h", FETCH_LIMITS.get("1h", 96))
    k4h = get_klines(symbol, "4h", FETCH_LIMITS.get("4h", 100))
    return symbol, pconf, (price, ticker, k15, k1h, k4h), None if all([k15, k1h, k4h]) else "K线数据失败"

def run_scan():
    logger.info("=" * 60)
    logger.info("Cipher v5 多币种扫描 — SMC增强版")

    # ─── 1. 全局市场背景分析 ───
    btc_k1h = get_klines("BTCUSDT", "1h", FETCH_LIMITS.get("1h", 96))
    btc_k4h = get_klines("BTCUSDT", "4h", FETCH_LIMITS.get("4h", 100))
    # v5: OI/资金费率分析
    btc_contract = get_contract_data("BTCUSDT")
    market_ctx = evaluate_market_context(btc_k1h, btc_k4h, oi_data=btc_contract)
    regime = classify_regime(btc_k4h, btc_k1h)
    actual_regime, regime_changed = regime_mgr.update(regime.value)
    regime_label = get_regime_params(regime).get("label", "?")
    if regime_changed:
        logger.info(f"  行情模式切换: {regime_mgr.previous} -> {regime.value}")
    status_icon = "!!" if market_ctx.derisk else "OK"
    # OI信息
    oi_sig = market_ctx.oi_info.get("signal", "neutral")
    oi_str = f" | OI={market_ctx.oi_info.get('oi_trend','?')}"
    if market_ctx.oi_info.get("funding_rate"):
        oi_str += f" 费率={market_ctx.oi_info['funding_rate']:.6f}"
    # v5: 订单流分析
    btc_ofi = analyze_order_flow("BTCUSDT")
    ofi_str = f" | OFI={btc_ofi.get('ofi',0):+.2f}" if btc_ofi.get('ofi') else ""
    if btc_ofi.get('whale_alert'):
        ofi_str += " 🐋大单"
    # v5: VRVP成交量分布
    btc_vrvp = calculate_vrvp(btc_k4h if btc_k4h else btc_k1h, 50)
    vrvp_str = ""
    if btc_vrvp:
        v = btc_vrvp
        vrvp_str = f" | POC ${v['poc']}"
        if v['current_position'] == 'near_poc': vrvp_str += "🎯"
        elif v['current_position'] == 'above_va': vrvp_str += "📈"
        elif v['current_position'] == 'below_va': vrvp_str += "📉"
        logger.info(f"  VRVP: POC=${v['poc']} 价值区=${v['va_low']}-${v['va_high']} {describe_vrvp(btc_vrvp).split('|')[-1]}")

    # v5: 宏观/消息面数据
    # v5: 模式切换锁（必须在regime分类前初始化）
    regime_mgr = RegimeTransitionManager()

    macro_ctx = MacroContext().evaluate()
    macro_str = f" | 恐慌{macro_ctx.fear_greed}"
    if macro_ctx.dxy:
        macro_str += f" | DXY={macro_ctx.dxy}"
    if macro_ctx.derisk:
        market_ctx.derisk = True
        market_ctx.derisk_factor = min(market_ctx.derisk_factor, macro_ctx.derisk_factor)
    for r in macro_ctx.reasons:
        if r not in market_ctx.reasons:
            market_ctx.reasons.append(r)

    # v5: 初始化策略路由
    strategy_router = StrategyRouter()
    bb = calc_bollinger_bands(btc_k1h, 20, 2.0) if btc_k1h and len(btc_k1h) >= 20 else {}
    _, market_mode = strategy_router.route(regime_label, market_ctx.btc_rsi, bb, btc_ofi, [])

    logger.info(
        f"市场背景: [{status_icon}] | 乘数={market_ctx.derisk_factor:.1f} | "
        f"趋势={market_ctx.btc_trend} | "
        f"RSI={market_ctx.btc_rsi:.0f} | 行情感知 {market_ctx.quality_score}/10 | "
        f"模式={regime_label}{oi_str}{ofi_str}{vrvp_str}{macro_str}"
    )

    # 严重去风险 → 暂停交易
    if market_ctx.derisk and market_ctx.derisk_factor < CONTEXT_CONFIG.get("derisk_factor_min", 0.3):
        warn = (f"⚠️ *Cipher 暂停交易 — 市场风险过高*\n"
                f"{chr(10).join(f'• {r}' for r in market_ctx.reasons[:3])}")
        logger.warning("市场风险过高，暂停交易: %s", "; ".join(market_ctx.reasons))
        send_telegram(warn, TELEGRAM)
        return

    # v5: 初始化引擎
    risk_engine = RiskEngine()
    pos_machine = PositionStateMachine()
    risk_passed, risk_violations = risk_engine.check_all({})
    logger.info(f"风控状态: {'✅' if risk_passed else '⚠️'} 日盈亏={risk_engine.state['daily_pnl']:.1f}% | "
                f"连亏={risk_engine.state['consecutive_losses']} | "
                f"熔断={'⚠️' if risk_engine.state['loss_limit_hit'] else '✅'}")

    # v5: 禁交易时段检查
    in_blackout, blackout_reason = check_event_blackout()
    if in_blackout:
        logger.warning(f"⏸️ 禁交易: {blackout_reason}")
        send_telegram(f"⏸️ *禁交易时段*\n{blackout_reason}\n\n自动恢复交易后正常扫描")
        return

    # v5: 系统健康检查
    health = check_health()
    if not health.get("binance_api"):
        logger.error("❌ Binance API 异常，暂停交易")
        send_telegram("⚠️ *Binance API 异常*\n暂停交易，等待恢复", TELEGRAM)
        return

    # v5: Binance真实仓位对账
    all_ok, reconcile_issues = reconcile()
    if not all_ok:
        for issue in reconcile_issues[:3]:
            logger.warning(f"  对账: {issue}")
        if any("孤儿" in i or "危险" in i for i in reconcile_issues):
            send_telegram(f"⚠️ *仓位对账异常*\n" + "\n".join(reconcile_issues[:3]), TELEGRAM)

    # v5: 动态冷却 — 趋势强+OFI确认时10分钟，否则30分钟
    ofi_val = btc_ofi.get("ofi", 0)
    if (market_ctx.btc_trend == "downtrend" and ofi_val < -0.3) or \
       (market_ctx.btc_trend == "uptrend" and ofi_val > 0.3):
        _cooldown = 600  # 趋势确认→10分钟冷却
        logger.info(f"  趋势强劲，冷却缩短至10分钟 (OFI={ofi_val:+.2f})")
    else:
        _cooldown = SIGNAL_COOLDOWN_SEC

    signals_found = 0
    enabled_pairs = [(k, v) for k, v in PAIRS.items() if v.get("enabled", False)]

    # 并发获取所有币种数据
    with ThreadPoolExecutor(max_workers=min(len(enabled_pairs), 3)) as ex:
        futures = {ex.submit(fetch_pair, sym, pc): sym for sym, pc in enabled_pairs}
        results = {}
        for future in as_completed(futures):
            sym, pconf, data, err = future.result()
            ok = not err and data is not None
            if err or not data:
                logger.warning(f"{pconf.get('name', sym)}: {err or '数据获取失败'}")
            results[sym] = (pconf, data)
            if ok:
                pair_name = pconf.get("name", sym)
                price, ticker, k15, k1h, k4h = data
                logger.info(f"  {pair_name} ${price:,.2f} | 24h ${ticker['low']:,.0f}-${ticker['high']:,.0f} {ticker['change_pct']:+.2f}%")

    # 逐个币种分析信号
    for symbol, pconf in enabled_pairs:
        if symbol not in results:
            continue
        pconf, data = results[symbol]
        if not data:
            continue
        # 检查K线数据完整性
        _, _, k15, k1h, k4h = data
        if not all([k15, k1h, k4h]):
            logger.warning(f"  {pconf.get('name', symbol)}: K线数据不完整，跳过")
            continue

        pair_name = pconf.get("name", symbol)
        price, ticker, k15, k1h, k4h = data

        pair_max_stop = pconf.get("max_stop_pct", 1.2)
        signal, indicators = find_trading_signal(price, ticker, k15, k1h, k4h,
                                                   max_stop_pct=pair_max_stop,
                                                   market_context=market_ctx,
                                                   market_regime=regime,
                                                   ofi_info=btc_ofi)

        ind = indicators
        if ind:
            logger.info(f"  RSI 15m={ind['rsi_15m']:.0f} 1h={ind['rsi_1h']:.0f} | ATR 1h=${ind['atr_1h']:.1f}")
            logger.info(f"  结构: {ind['structure_1h']['structure']} | EMA9/21 1h: {'多头' if ind['ema9_1h']>ind['ema21_1h'] else '空头'}")

        if signal:
            try:
                score = signal.get("score", 0)
                min_score = pconf.get("min_score", MIN_SCORE_GOOD)
                if score < min_score:
                    logger.info(f"  ⏸️ 信号评分{score}<{min_score}({pair_name}最低要求)，跳过")
                    log_rejected_signal(signal, f"评分不足{score}<{min_score}", pair_name)
                    continue

                # 信号去重：同币种同方向30分钟内不重复发
                last_sig = {}
                if os.path.exists(LAST_SIGNAL_FILE):
                    try:
                        with open(LAST_SIGNAL_FILE) as f:
                            last_sig = json.load(f)
                    except Exception as e:
                        logger.warning(f"读取去重文件失败: {e}")
                if last_sig.get("pair") == pair_name and last_sig.get("direction") == signal["direction"]:
                    price_diff = abs(last_sig.get("entry", 0) - price) / price * 100
                    time_diff = datetime.now(timezone.utc).timestamp() - last_sig.get("ts", 0)
                    if price_diff < 0.3 and time_diff < _cooldown:
                        logger.info(f"  ⏸️ 去重: 同{pair_name}同方向价差{price_diff:.2f}% 距上次{int(time_diff//60)}分，跳过")
                        continue
                # v5: 多信号投票引擎
                vote_dir, vote_conf, vote_reasons, score_boost = evaluate_vote(
                    signal_score=score,
                    signal_direction=signal["direction"],
                    signal_rr=signal["rr"],
                    fvg_info=signal.get("fvg_info", {}),
                    oi_info=market_ctx.oi_info,
                    ofi_info=btc_ofi,
                    regime_label=regime_label,
                    vrvp_info=btc_vrvp,
                )
                # 投票明确反对 -> 跳过
                if vote_dir is None and score_boost <= -5:
                    logger.info(f"  ⏸️ 多信号否决: {', '.join(vote_reasons[:2])}")
                    continue
                # 应用评分加成
                orig_score = score
                score = max(10, min(100, score + score_boost))
                signal["score"] = score
                if score != orig_score:
                    signal["score_detail"]["vote_boost"] = score_boost

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
                    log_rejected_signal(signal, f"验证器拦截:{vresult['summary']}", pair_name)
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

                # v5: 风控最终审核
                risk_passed, risk_violations = risk_engine.check_all(signal, btc_ofi)
                if not risk_passed:
                    reason = "; ".join(risk_violations[:3])
                    logger.error(f"  ❌ 风控拦截: {reason}")
                    log_rejected_signal(signal, f"风控拦截:{reason}", pair_name)
                    send_telegram(f"⚠️ {pair_name} *风控拦截*\n{reason}")
                    continue

                # v5: 风控自动降级（在check_all之后，因为degrade_factor由check_all设置）
                degrade = signal.get("degrade_factor", 1.0)
                if degrade < 1.0:
                    old_amt = signal.get("amount_pct", 0)
                    signal["amount_pct"] = max(10, int(old_amt * degrade))
                    logger.info(f"  ⏸️ 自动降级: 仓位 {old_amt}% → {signal['amount_pct']}% (x{degrade})")

                # v5: signal_id 防重复执行
                sig_id = signal.get("signal_id", "")
                if sig_id and is_signal_executed(sig_id):
                    logger.warning(f"  ⏸️ signal_id {sig_id} 已执行过，跳过")
                    continue
                if sig_id:
                    mark_signal_executed(sig_id)

                log_trade(signal, "sent")
                try:
                    with open(LAST_SIGNAL_FILE, "w") as f:
                        json.dump({"pair": pair_name, "direction": signal["direction"],
                                   "entry": price, "ts": datetime.now(timezone.utc).timestamp()}, f)
                except Exception as e:
                    logger.error(f"写入去重文件失败: {e}")
                send_telegram(format_signal(signal))
                send_cornix(signal, TELEGRAM)  # Cornix 自动执行
                # 记录本地仓位（用于对账）
                try:
                    from binance_reconciler import save_local_position
                    save_local_position(signal.get("symbol", "BTCUSDT"),
                                        signal["direction"], signal.get("entry", 0),
                                        signal.get("amount_pct", 0))
                except: pass
                signals_found += 1
            except Exception as e:
                logger.error(f"  ❌ 信号处理异常: {e}", exc_info=True)

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
def run_fast_scan():
    """1分钟快扫 — 仅OFI剥头皮信号，跳过全量分析"""
    logger.info("=" * 40)
    logger.info("Cipher 快扫描 — OFI剥头皮")
    ofi = analyze_order_flow("BTCUSDT")
    if abs(ofi.get("ofi", 0)) < 0.5:
        return
    price = get_binance_price("BTCUSDT")
    k15 = get_klines("BTCUSDT", "15m", 14)
    if not price or not k15:
        return
    from strategies import scalping_strategy
    sig = scalping_strategy(price, ofi, k15, "fast")
    if not sig:
        return
    sig["signal_id"] = generate_signal_id()
    sig["pair"] = "BTC"
    sig["symbol"] = "BTCUSDT"
    sig["leverage"] = sig.get("leverage", 25)
    # 风控检查
    risk_engine = RiskEngine()
    risk_passed, _ = risk_engine.check_all({})
    if not risk_passed:
        return
    if is_signal_executed(sig["signal_id"]):
        return
    mark_signal_executed(sig["signal_id"])
    log_trade(sig, "sent")
    send_telegram(format_signal(sig))
    try:
        from binance_reconciler import save_local_position
        save_local_position(sig.get("symbol", "BTCUSDT"),
                            sig["direction"], sig.get("entry", 0),
                            sig.get("amount_pct", 0))
    except: pass
    send_cornix(sig, TELEGRAM)
    logger.info(f"✅ OFI剥头皮信号已发送")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan": run_scan()
    elif mode == "fastscan": run_fast_scan()
    elif mode == "summary": run_summary()
    elif mode == "review": run_review()
    elif mode == "log": run_log()
    elif mode == "history": run_log()
    elif mode == "report":
        report = generate_daily_report()
        print(report)
        send_telegram(report)
    elif mode == "report-only":
        print(generate_daily_report())
    elif mode == "health":
        h = check_health()
        for k, v in h.items():
            print(f"{k}: {'✅' if v else '❌'}")
    else: print(f"未知: {mode}，可用: scan/summary/review/log/report/health")
