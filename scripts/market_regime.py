# -*- coding: utf-8 -*-
"""
Cipher 市场行情模式分类器
区分不同市场状态，各模式下策略参数自适应调整

模式:
  - TRENDING_BULL:  上升趋势 — 顺势做多，放宽止损
  - TRENDING_BEAR:  下降趋势 — 顺势做空，收紧止损
  - RANGING:        横盘震荡 — 高空低多，严格止损
  - VOLATILE:       高波动 — 减仓+扩大止损范围
"""
from enum import Enum
from typing import Dict, List, Optional, Tuple


class Regime(Enum):
    TRENDING_BULL = "trending_bull"
    TRENDING_BEAR = "trending_bear"
    SLOW_BEAR = "slow_bear"
    BEAR_CASCADE = "bear_cascade"
    BULL_CASCADE = "bull_cascade"  # 阶梯下跌
    SLOW_BULL = "slow_bull"
    FAST_PUMP_CONTINUATION = "fast_pump_continuation"
    FAST_PUMP_EXHAUSTION = "fast_pump_exhaustion"
    FAST_DUMP_CONTINUATION = "fast_dump_continuation"
    FAST_DUMP_EXHAUSTION = "fast_dump_exhaustion"
    RANGE_WIDE = "range_wide"       # 宽震荡 可交易
    RANGE_NARROW = "range_narrow"   # 窄震荡 少做
    CHOPPY = "choppy"               # 乱震荡 禁止交易
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


# 各行情模式下的策略参数
REGIME_PARAMS: Dict[Regime, dict] = {
    Regime.SLOW_BULL: {
        "label": "🐢 慢涨行情",
        "size_multiplier": 1.1, "min_rr": 2.0, "max_stop_pct": 0.50,
        "prefer_long": True, "score_bonus_long": 5, "score_penalty_short": 10,
        "trailing_pct": 0.35, "max_leverage": 22,
    },
    Regime.SLOW_BEAR: {
        "label": "🐌 阴跌行情",
        "size_multiplier": 1.0, "min_rr": 2.5, "max_stop_pct": 0.55,
        "prefer_long": False, "score_bonus_short": 8, "score_penalty_long": 12,
        "trailing_pct": 0.35, "max_leverage": 20,
    },
    Regime.BEAR_CASCADE: {
        "label": "💧 阶梯下跌",
        "size_multiplier": 1.2, "min_rr": 2.5, "max_stop_pct": 0.60,
        "prefer_long": False, "score_bonus_short": 10, "score_penalty_long": 15,
        "trailing_pct": 0.40, "max_leverage": 25,
    },
    Regime.BULL_CASCADE: {
        "label": "🔥 阶梯上涨",
        "size_multiplier": 1.2, "min_rr": 2.0, "max_stop_pct": 0.60,
        "prefer_long": True, "score_bonus_long": 10, "score_penalty_short": 15,
        "trailing_pct": 0.35, "max_leverage": 25,
    },
    Regime.TRENDING_BULL: {
        "label": "📈 上升趋势",
        "size_multiplier": 1.3,        # 加仓30%（原20%）
        "min_rr": 2.0,                 # 最低盈亏比
        "max_stop_pct": 1.2,           # 最大止损
        "prefer_long": True,
        "score_bonus_long": 8,         # 加成8分（原5分）— 顺势更积极
        "score_penalty_short": 8,
    },
    Regime.TRENDING_BEAR: {
        "label": "📉 下降趋势",
        "size_multiplier": 1.0,        # 正常仓位（原减仓20%）
        "min_rr": 2.0,                 # 原2.5→2.0，扩大机会
        "max_stop_pct": 0.8,           # 紧止损不变
        "prefer_long": False,
        "score_bonus_short": 7,        # 做空加成7分（原5分）
        "score_penalty_long": 3,       # 做多仅扣3分（原8分）— 允许优质超卖反弹
    },
    Regime.RANGE_WIDE: {
        "label": "↔️ 宽震荡",
        "size_multiplier": 0.8, "min_rr": 2.0, "max_stop_pct": 0.45,
        "prefer_long": None,
        "score_bonus_long": 0, "score_penalty_short": 0,
    },
    Regime.RANGE_NARROW: {
        "label": "🔹 窄震荡",
        "size_multiplier": 0.5, "min_rr": 2.5, "max_stop_pct": 0.35,
        "prefer_long": None,
        "score_bonus_long": 0, "score_penalty_short": 0,
    },
    Regime.CHOPPY: {
        "label": "❌ 乱震荡",
        "size_multiplier": 0.0, "min_rr": 99, "max_stop_pct": 0.1,
        "prefer_long": None,
        "score_bonus_long": 0, "score_penalty_short": 0,
    },
    Regime.FAST_PUMP_CONTINUATION: {
        "label": "🚀 暴涨延续",
        "size_multiplier": 0.7, "min_rr": 2.5, "max_stop_pct": 0.60,
        "prefer_long": True, "score_bonus_long": 3, "score_penalty_long": 8,
        "trailing_pct": 0.40, "max_leverage": 18,
    },
    Regime.FAST_PUMP_EXHAUSTION: {
        "label": "📉 暴涨衰竭",
        "size_multiplier": 0.5, "min_rr": 3.0, "max_stop_pct": 0.50,
        "prefer_long": False, "score_bonus_short": 5, "score_penalty_long": 10,
        "trailing_pct": 0.35, "max_leverage": 15,
    },
    Regime.FAST_DUMP_CONTINUATION: {
        "label": "💥 暴跌延续",
        "size_multiplier": 0.7, "min_rr": 2.5, "max_stop_pct": 0.60,
        "prefer_long": False, "score_bonus_short": 3, "score_penalty_long": 8,
        "trailing_pct": 0.45, "max_leverage": 18,
    },
    Regime.FAST_DUMP_EXHAUSTION: {
        "label": "📈 暴跌衰竭",
        "size_multiplier": 0.5, "min_rr": 3.0, "max_stop_pct": 0.50,
        "prefer_long": True, "score_bonus_long": 5, "score_penalty_short": 10,
        "trailing_pct": 0.40, "max_leverage": 15,
    },
    Regime.VOLATILE: {
        "label": "🌊 高波动",
        "size_multiplier": 0.7, "min_rr": 2.5, "max_stop_pct": 1.2,
        "prefer_long": None, "allow_add_position": False,
        "score_bonus_long": 0, "score_penalty_short": 0,
    },
    Regime.UNKNOWN: {
        "label": "❓ 未知",
        "size_multiplier": 0.0,  # 不明模式不交易
        "min_rr": 2.0,
        "max_stop_pct": 1.0,
        "prefer_long": None,
        "score_bonus_long": 0,
        "score_penalty_short": 0,
    },
}


from indicators import calc_ema, calc_atr


def classify_regime(klines_4h: Optional[List[dict]], klines_15m: Optional[List[dict]] = None) -> Regime:
    """
    分类当前市场行情模式

    需要至少 20 根 4h K线（~3天），推荐 100+ 根（~16天）
    """
    if not klines_4h or len(klines_4h) < 10:
        return Regime.UNKNOWN

    closes = [k["close"] for k in klines_4h]
    price = closes[-1]

    # 趋势判断 —— EMA20 vs EMA50
    ema20 = calc_ema(closes, min(20, len(closes)))
    ema50 = calc_ema(closes, min(50, len(closes)))

    # ─── Step 0: 暴涨暴跌检测（用15m数据）───
    if klines_15m and len(klines_15m) >= 20:
        c15 = [k["close"] for k in klines_15m]
        v15 = [k["volume"] for k in klines_15m]
        recent_candles = klines_15m[-3:]
        avg_vol = sum(v15) / len(v15)
        for k in recent_candles:
            body = abs(k["close"] - k["open"])
            range_pct = body / k["open"] * 100
            vol_ratio = k["volume"] / avg_vol if avg_vol > 0 else 1
            if range_pct > 2.0 and vol_ratio > 2.5:
                upper = k["high"] - max(k["close"], k["open"])
                lower = min(k["close"], k["open"]) - k["low"]
                has_wick = upper > body * 0.3 or lower > body * 0.3
                if k["close"] > k["open"]:
                    return Regime.FAST_PUMP_EXHAUSTION if has_wick else Regime.FAST_PUMP_CONTINUATION
                else:
                    return Regime.FAST_DUMP_EXHAUSTION if has_wick else Regime.FAST_DUMP_CONTINUATION

    # 波动率 —— ATR%
    atr_val = calc_atr(klines_4h, 14)
    atr_pct = atr_val / price * 100 if price > 0 else 0

    # 均值ATR (近20根)
    if len(klines_4h) >= 30:
        avg_atr = calc_atr(klines_4h[-20:], 7)
        avg_atr_pct = avg_atr / price * 100
    else:
        avg_atr_pct = atr_pct

    # ─── Step 0.5: 阶梯上涨检测（BULL_CASCADE）───
    # 特征: EMA20>EMA50 + 价格>EMA20 + ATR适中 + 连续上涨
    if ema20 > ema50 * 1.005 and price > ema20 and atr_pct >= 0.8 and atr_pct < 2.5:
        if len(closes) >= 4 and closes[-1] > closes[-2] >= closes[-3]:
            return Regime.BULL_CASCADE

    # ─── Step 1: 波动率过滤 ───
    if atr_pct > 2.0 or (avg_atr_pct > 0 and atr_pct > avg_atr_pct * 1.5):
        return Regime.VOLATILE

    # ─── Step 2: 阴跌识别（SLOW_BEAR）───
    # 条件: EMA20<EMA50 + 价格<EMA20 + 低波动 + 持续阴线
    if ema20 < ema50 * 0.995 and price < ema20:
        # 检查是否为阴跌（慢跌+弱反弹）
        closes_slice = closes[-6:] if len(closes) >= 6 else closes
        consecutive_below_ema20 = sum(1 for c in closes_slice if c < ema20)
        # 最近6根有5根在EMA20以下 = 持续弱势
        if consecutive_below_ema20 >= 5 and atr_pct < 1.5:
            return Regime.SLOW_BEAR
        return Regime.TRENDING_BEAR

    # ─── Step 3: 慢涨识别（SLOW_BULL）───
    if ema20 > ema50 * 1.005 and price > ema20:
        # 检查是否为慢涨（低波动+持续在EMA20上）
        closes_slice = closes[-6:] if len(closes) >= 6 else closes
        consecutive_above_ema20 = sum(1 for c in closes_slice if c > ema20)
        if consecutive_above_ema20 >= 5 and atr_pct < 1.2:
            return Regime.SLOW_BULL
        return Regime.TRENDING_BULL

    # ─── Step 4: 震荡分类（宽/窄/乱）───
    range_width = abs(ema20 - ema50) / price * 100
    if atr_pct > 1.0:
        return Regime.CHOPPY  # 高波动+无趋势=乱震荡
    if range_width < 0.3:
        return Regime.CHOPPY  # EMA20/50几乎重合=乱震荡
    if range_width < 0.8:
        return Regime.RANGE_NARROW  # 窄震荡
    return Regime.RANGE_WIDE  # 宽震荡可交易


def get_regime_params(regime: Optional[Regime]) -> dict:
    """获取当前行情模式的策略参数"""
    if regime is None:
        regime = Regime.UNKNOWN
    return dict(REGIME_PARAMS.get(regime, REGIME_PARAMS[Regime.UNKNOWN]))


def get_score_adjustment(regime: Optional[Regime], direction: str) -> Tuple[int, str]:
    """
    根据行情模式计算评分调整

    Returns:
        (adjustment: int, reason: str)
    """
    if regime is None:
        return 0, ""
    params = get_regime_params(regime)

    if direction == "long":
        bonus = params.get("score_bonus_long", 0)
        penalty = params.get("score_penalty_long", 0)
        if bonus > 0:
            return bonus, f"上升趋势做多加成+{bonus}"
        if penalty > 0:
            return -penalty, f"下降趋势做空惩罚-{penalty}"
    else:
        bonus = params.get("score_bonus_short", 0)
        penalty = params.get("score_penalty_short", 0)
        if bonus > 0:
            return bonus, f"下降趋势做空加成+{bonus}"
        if penalty > 0:
            return -penalty, f"上升趋势做多惩罚-{penalty}"

    return 0, ""
