# -*- coding: utf-8 -*-
"""
Cipher 7策略并行引擎 — 小龙虾多策略调度系统

架构:
  策略引擎(7路) → 策略路由(选主策略) → 信号合并(去冲突)
  → 策略锁(防自相残杀) → 仓位管理(统一TP/SL) → 风控 → 执行

策略:
  1. 趋势    2. 突破回踩   3. 震荡    4. 假突破反杀
  5. VWAP回归 6. 波动率扩张  7. OFI剥头皮
"""
from enum import Enum
from typing import Optional, List, Dict, Tuple
import uuid
import time


class MarketMode(Enum):
    TREND_UP = "上升趋势"
    TREND_DOWN = "下降趋势"
    SLOW_BEAR = "阴跌行情"
    BEAR_CASCADE = "阶梯下跌"
    SLOW_BULL = "慢涨行情"
    FAST_PUMP = "暴涨行情"
    FAST_DUMP = "暴跌行情"
    RANGE_WIDE = "宽震荡"
    RANGE_NARROW = "窄震荡"
    CHOPPY = "乱震荡"
    BREAKOUT = "突破"
    FAKEOUT = "假突破"
    SCALP_ONLY = "只适合剥头皮"
    NO_TRADE = "禁止交易"


MODE_STRATEGY_MAP = {
    MarketMode.TREND_UP: {
        "primary": ["trend_long", "breakout_retest_long"],
        "disabled": ["range_short", "vwap_reversion_short"],
    },
    MarketMode.TREND_DOWN: {
        "primary": ["trend_short", "breakout_retest_short"],
        "disabled": ["range_long", "vwap_reversion_long"],
    },
    MarketMode.SLOW_BULL: {
        "primary": ["slow_bull_long", "trend_long"],
        "disabled": ["range_short", "scalp_ofi_short"],
    },
    MarketMode.SLOW_BEAR: {
        "primary": ["trend_short", "slow_bear_short"],
        "disabled": ["range_long", "vwap_reversion_long", "scalp_ofi_long"],
    },
    MarketMode.BEAR_CASCADE: {
        "primary": ["trend_short", "slow_bear_short", "breakout_retest_short"],
        "disabled": ["range_long", "vwap_reversion_long", "scalp_ofi_long", "fakeout_reversal_long"],
    },
    MarketMode.FAST_PUMP: {
        "primary": ["fakeout_reversal_short", "breakout_retest_long"],
        "disabled": ["trend_long", "range_long", "scalp_ofi_long"],
    },
    MarketMode.FAST_DUMP: {
        "primary": ["fakeout_reversal_long", "breakout_retest_short"],
        "disabled": ["trend_short", "range_short", "scalp_ofi_short"],
    },
    MarketMode.RANGE_WIDE: {
        "primary": ["range", "vwap_reversion", "fakeout_reversal"],
        "disabled": ["trend", "breakout_retest"],
    },
    MarketMode.RANGE_NARROW: {
        "primary": ["range"],
        "disabled": ["trend", "breakout_retest", "scalp_ofi"],
    },
    MarketMode.CHOPPY: {
        "primary": [],
        "disabled": ["trend", "range", "breakout_retest", "scalp_ofi", "vwap_reversion"],
    },
    MarketMode.BREAKOUT: {
        "primary": ["breakout_retest", "volatility_expansion"],
        "disabled": ["range", "vwap_reversion"],
    },
    MarketMode.FAKEOUT: {
        "primary": ["fakeout_reversal"],
        "disabled": ["trend", "breakout_retest"],
    },
    MarketMode.SCALP_ONLY: {
        "primary": ["scalp_ofi"],
        "disabled": ["trend", "breakout_retest", "range"],
    },
    MarketMode.NO_TRADE: {
        "primary": [],
        "disabled": ["trend", "range", "scalp_ofi", "breakout_retest",
                       "fakeout_reversal", "vwap_reversion", "volatility_expansion"],
    },
}


# ═══════════════════════════════════════════════
# 策略锁
# ═══════════════════════════════════════════════

class StrategyLocks:
    """策略锁 — 防多策略自相残杀"""

    def __init__(self):
        self.symbol_locks = {}    # symbol -> direction
        self.direction_locks = {} # symbol -> last_direction
        self.cooldowns = {}       # symbol -> timestamp
        self.cooldown_minutes = 15

    def can_open(self, symbol: str, direction: str) -> Tuple[bool, str]:
        """检查是否允许开仓"""
        # 已有同币种仓位
        if symbol in self.symbol_locks:
            existing = self.symbol_locks[symbol]
            if existing == direction:
                return False, f"已有{direction}仓位，不重复开"
            else:
                return False, f"已有{existing}仓位，不能反方向开"
        # 冷却中
        if symbol in self.cooldowns:
            remaining = (self.cooldowns[symbol] + self.cooldown_minutes * 60) - time.time()
            if remaining > 0:
                return False, f"冷却中，剩余{int(remaining//60)}分钟"
        return True, ""

    def open_position(self, symbol: str, direction: str):
        """记录开仓"""
        self.symbol_locks[symbol] = direction
        self.direction_locks[symbol] = direction

    def close_position(self, symbol: str):
        """平仓后设置冷却"""
        if symbol in self.symbol_locks:
            del self.symbol_locks[symbol]
        self.cooldowns[symbol] = time.time()

    def is_locked(self, symbol: str, strategy_name: str) -> bool:
        """是否有其他策略占用了该币种"""
        return symbol in self.symbol_locks


# ═══════════════════════════════════════════════
# 策略统一输出格式
# ═══════════════════════════════════════════════

def make_signal(strategy: str, symbol: str, direction: str,
                entry: float, stop: float, target: float,
                confidence: int, leverage: int = 15,
                risk_pct: float = 0.003, ttl_minutes: int = 60,
                tp2: float = None, tp3: float = None,
                reason: str = "") -> dict:
    """统一策略输出格式"""
    risk = abs(entry - stop)
    rr = abs(target - entry) / risk if risk > 0 else 0
    return {
        "strategy": strategy, "symbol": symbol,
        "direction": direction, "entry": entry,
        "stop_loss": round(stop, 1), "target": round(target, 1),
        "rr": round(rr, 2), "score": confidence,
        "confidence": confidence, "leverage": leverage,
        "risk_pct": risk_pct, "ttl_minutes": ttl_minutes,
        "signal_id": uuid.uuid4().hex[:12],
        "tp2": round(tp2, 1) if tp2 else None,
        "tp3": round(tp3, 1) if tp3 else None,
        "reason": reason, "pattern": strategy,
        "fvg_info": {"in_fvg": False},
        "key_level": round(entry, 1),
    }


# ═══════════════════════════════════════════════
# 信号合并器（同向合并/反向过滤）
# ═══════════════════════════════════════════════

class SignalMerger:
    """信号合并 — 多策略信号去重、合并、过滤冲突"""

    def merge(self, candidates: List[dict], locks: StrategyLocks) -> List[dict]:
        """合并候选信号，返回最终可执行信号列表"""
        if not candidates:
            return []

        # 按方向分组
        longs = [c for c in candidates if c["direction"] == "long"]
        shorts = [c for c in candidates if c["direction"] == "short"]

        result = []

        # 如果多空都有 → 冲突，选置信度高的那个
        if longs and shorts:
            best_long = max(longs, key=lambda x: x.get("confidence", 0))
            best_short = max(shorts, key=lambda x: x.get("confidence", 0))
            if best_long["confidence"] > best_short["confidence"]:
                result.append(best_long)
            else:
                result.append(best_short)
        elif longs:
            result.append(max(longs, key=lambda x: x.get("confidence", 0)))
        elif shorts:
            result.append(max(shorts, key=lambda x: x.get("confidence", 0)))

        # 策略锁检查
        final = []
        for s in result:
            symbol = s.get("symbol", "BTCUSDT")
            direction = s["direction"]
            ok, reason = locks.can_open(symbol, direction)
            if ok:
                final.append(s)
            else:
                s["skip_reason"] = reason

        return final


# ═══════════════════════════════════════════════
# 仓位管理器
# ═══════════════════════════════════════════════

class PositionManager:
    """统一仓位管理 — TP/SL/reduce-only"""

    def __init__(self):
        self.positions = {}  # symbol -> position_info

    def open(self, signal: dict):
        """开仓记录"""
        symbol = signal.get("symbol", "BTCUSDT")
        self.positions[symbol] = {
            "direction": signal["direction"],
            "entry": signal["entry"],
            "stop": signal["stop_loss"],
            "target": signal["target"],
            "strategy": signal.get("strategy", "?"),
            "open_time": time.time(),
            "signal_id": signal.get("signal_id", ""),
        }

    def close(self, symbol: str):
        """平仓记录"""
        if symbol in self.positions:
            del self.positions[symbol]

    def get(self, symbol: str) -> Optional[dict]:
        return self.positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions


# ═══════════════════════════════════════════════
# 策略路由 — 核心调度器
# ═══════════════════════════════════════════════

def detect_market_mode(regime_label: str, rsi_1h: float,
                        bb: dict, ofi: dict) -> MarketMode:
    """市场模式检测 — 从行情数据判断当前模式"""
    if not regime_label:
        return MarketMode.NO_TRADE

    # 高波动 + 趋势 = 突破模式
    bb_bw = bb.get("bandwidth", 100) if bb else 100
    ofi_val = abs(ofi.get("ofi", 0)) if ofi else 0

    if "宽震荡" in regime_label:
        return MarketMode.RANGE_WIDE
    if "窄震荡" in regime_label:
        return MarketMode.RANGE_NARROW
    if "乱震荡" in regime_label:
        return MarketMode.CHOPPY
    if "暴涨" in regime_label:
        return MarketMode.FAST_PUMP
    if "暴跌" in regime_label:
        return MarketMode.FAST_DUMP
    if "慢涨" in regime_label:
        return MarketMode.SLOW_BULL
    if "阴跌" in regime_label:
        return MarketMode.SLOW_BEAR
    if "阶梯下跌" in regime_label:
        return MarketMode.BEAR_CASCADE
    if "下降" in regime_label:
        if bb_bw > 30 and ofi_val > 0.5:
            return MarketMode.BREAKOUT
        return MarketMode.TREND_DOWN
    elif "上升" in regime_label:
        if bb_bw > 30 and ofi_val > 0.5:
            return MarketMode.BREAKOUT
        return MarketMode.TREND_UP
    elif "震荡" in regime_label or "横盘" in regime_label:
        if ofi_val > 0.7:
            return MarketMode.FAKEOUT
        return MarketMode.RANGE
    elif "高波动" in regime_label:
        return MarketMode.SCALP_ONLY
    return MarketMode.NO_TRADE


class StrategyRouter:
    """策略调度器 — 决定哪个策略有执行权"""

    def __init__(self):
        self.locks = StrategyLocks()
        self.merger = SignalMerger()
        self.position_manager = PositionManager()
        self.mode = MarketMode.NO_TRADE

    def route(self, regime_label: str, rsi_1h: float,
              bb: dict, ofi: dict,
              candidates: List[dict]) -> Tuple[List[dict], MarketMode]:
        """路由所有策略信号，返回最终可执行信号"""
        self.mode = detect_market_mode(regime_label, rsi_1h, bb, ofi)
        mode_config = MODE_STRATEGY_MAP.get(self.mode, {})

        # 过滤：禁用策略的信号不能执行
        disabled = mode_config.get("disabled", [])
        filtered = [c for c in candidates
                    if c.get("strategy") not in disabled]

        # 合并同方向信号
        merged = self.merger.merge(filtered, self.locks)

        return merged, self.mode
