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
    SLOW_BEAR = "slow_bear"     # 阴跌: 反弹弱+逐级破位
    RANGING = "ranging"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


# 各行情模式下的策略参数
REGIME_PARAMS: Dict[Regime, dict] = {
    Regime.SLOW_BEAR: {
        "label": "🐌 阴跌行情",
        "size_multiplier": 1.0,
        "min_rr": 2.5,
        "max_stop_pct": 0.55,
        "prefer_long": False,
        "score_bonus_short": 8,
        "score_penalty_long": 12,
        "trailing_pct": 0.35,
        "max_leverage": 20,
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
    Regime.RANGING: {
        "label": "⏸️ 横盘震荡",
        "size_multiplier": 0.8,        # 减仓20%（原30%）
        "min_rr": 2.0,                 # 原2.5→2.0
        "max_stop_pct": 0.6,           # 紧止损不变
        "prefer_long": None,
        "score_bonus_long": 0,
        "score_penalty_short": 0,
    },
    Regime.VOLATILE: {
        "label": "🌊 高波动",
        "size_multiplier": 0.5,        # 减半仓
        "min_rr": 3.0,                 # 极高盈亏比要求
        "max_stop_pct": 1.5,           # 放宽止损（避免被波动扫掉）
        "prefer_long": None,
        "score_bonus_long": 0,
        "score_penalty_short": 0,
    },
    Regime.UNKNOWN: {
        "label": "❓ 未知",
        "size_multiplier": 0.5,
        "min_rr": 2.0,
        "max_stop_pct": 1.0,
        "prefer_long": None,
        "score_bonus_long": 0,
        "score_penalty_short": 0,
    },
}


from indicators import calc_ema, calc_atr


def classify_regime(klines_4h: Optional[List[dict]]) -> Regime:
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

    # 波动率 —— ATR%
    atr_val = calc_atr(klines_4h, 14)
    atr_pct = atr_val / price * 100 if price > 0 else 0

    # 均值ATR (近20根)
    if len(klines_4h) >= 30:
        avg_atr = calc_atr(klines_4h[-20:], 7)
        avg_atr_pct = avg_atr / price * 100
    else:
        avg_atr_pct = atr_pct

    # ─── Step 1: 波动率过滤 ───
    # 如果当前ATR比均值高50%以上 → 高波动
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

    # ─── Step 3: 趋势判断 ───
    if ema20 > ema50 * 1.005 and price > ema20:
        return Regime.TRENDING_BULL

    # ─── Step 4: 其余情况 → 震荡 ───
    return Regime.RANGING


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
