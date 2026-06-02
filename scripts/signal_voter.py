# -*- coding: utf-8 -*-
"""
Cipher 多信号投票引擎
多个独立信号源加权投票，至少2个信号源同意才交易

信号源:
  1. 技术面得分 (from score_signal)
  2. FVG检测 (from smc.py)
  3. OI/资金费率 (from market_context)
  4. 订单流 (from order_flow)
  5. 行情模式 (from market_regime)
"""
from typing import Optional, Dict, List, Tuple


class VoteResult:
    """投票结果"""

    def __init__(self):
        self.total_long = 0.0    # 加权做多总分
        self.total_short = 0.0   # 加权做空总分
        self.long_sources = 0    # 支持做多的信号源数
        self.short_sources = 0   # 支持做空的信号源数
        self.total_weight = 0.0  # 总权重
        self.consensus = "neutral"  # 共识方向
        self.confidence = 0      # 共识置信度 0-100
        self.details = []        # 各信号源投票详情

    def add_source(self, name: str, direction: str, confidence: int, weight: float, reason: str = ""):
        """
        添加一个信号源投票

        Args:
            name: 信号源名称
            direction: "long"/"short"/"neutral"
            confidence: 置信度 0-100
            weight: 权重 0-1
            reason: 投票理由
        """
        self.total_weight += weight
        if direction == "long":
            self.total_long += weight * (confidence / 100)
            self.long_sources += 1
        elif direction == "short":
            self.total_short += weight * (confidence / 100)
            self.short_sources += 1

        self.details.append({
            "name": name, "direction": direction,
            "confidence": confidence, "weight": weight, "reason": reason,
        })

    def decide(self, min_sources: int = 2, min_confidence: int = 50) -> Tuple[Optional[str], int, List[str]]:
        """
        投票决策

        Args:
            min_sources: 最少需要的信号源数
            min_confidence: 最低置信度

        Returns:
            (direction, confidence, reasons)
            direction: "long"/"short"/None
            confidence: 0-100
            reasons: 投票理由列表
        """
        if self.total_weight == 0:
            return None, 0, ["无有效信号源"]

        # 归一化
        long_score = self.total_long / self.total_weight * 100 if self.total_weight > 0 else 0
        short_score = self.total_short / self.total_weight * 100 if self.total_weight > 0 else 0

        reasons = []

        # 至少 min_sources 个信号源支持同一方向
        if long_score > short_score and self.long_sources >= min_sources:
            confidence = int(long_score)
            if confidence >= min_confidence:
                reasons.append(f"技术面{self._fmt_src('long')}")
                reasons.append(f"投票结果: 做多 {confidence}% 置信")
                return "long", confidence, reasons

        elif short_score > long_score and self.short_sources >= min_sources:
            confidence = int(short_score)
            if confidence >= min_confidence:
                reasons.append(f"技术面{self._fmt_src('short')}")
                reasons.append(f"投票结果: 做空 {confidence}% 置信")
                return "short", confidence, reasons

        # 未达到阈值，但仍有倾向
        if long_score > short_score:
            reasons.append(f"微偏多 ({int(long_score)}% vs {int(short_score)}%)")
            reasons.append("置信度不足，不交易")
        elif short_score > long_score:
            reasons.append(f"微偏空 ({int(short_score)}% vs {int(long_score)}%)")
            reasons.append("置信度不足，不交易")
        else:
            reasons.append("多空均衡，观望")

        return None, 0, reasons

    def _fmt_src(self, direction: str) -> str:
        """格式化信号源支持详情"""
        names = [d["name"] for d in self.details if d["direction"] == direction and d["confidence"] >= 50]
        return f"[{','.join(names)}]" if names else ""


def evaluate_vote(
    signal_score: int,
    signal_direction: str,
    signal_rr: float,
    fvg_info: dict,
    oi_info: dict,
    ofi_info: dict,
    regime_label: str,
    vrvp_info: dict = None,
) -> Tuple[Optional[str], int, List[str], int]:
    """
    综合评估所有信号源，返回投票结果

    Returns:
        (direction, confidence, reasons, score_boost)
        direction: "long"/"short"/None
        confidence: 0-100
        reasons: 理由列表
        score_boost: 评分加成（-20 ~ +15）
    """
    voter = VoteResult()

    # ─── 信号源1: 技术面得分（权重0.30）───
    tech_confidence = min(100, signal_score)
    tech_dir = signal_direction
    voter.add_source("技术面", tech_dir, tech_confidence, 0.25,
                      f"评分{signal_score} R/R={signal_rr}")

    # ─── 信号源2: FVG检测（权重0.20）───
    if fvg_info and fvg_info.get("in_fvg"):
        fvg_type = fvg_info.get("fvg", {}).get("type", "")
        if fvg_type == "bullish":
            voter.add_source("FVG检测", "long", 80, 0.18, "看涨FVG触发")
        elif fvg_type == "bearish":
            voter.add_source("FVG检测", "short", 80, 0.18, "看跌FVG触发")

    # ─── 信号源3: OI/资金费率（权重0.20）───
    if oi_info:
        oi_signal = oi_info.get("signal", "neutral")
        oi_reason = oi_info.get("reason", "")
        if oi_signal == "short_boost":
            voter.add_source("OI+费率", "short", 75, 0.18, oi_reason)
        elif oi_signal == "long_boost":
            voter.add_source("OI+费率", "long", 75, 0.18, oi_reason)
        elif oi_signal == "no_chase":
            voter.add_source("OI+费率", "neutral", 60, 0.18, oi_reason)

    # ─── 信号源4: 订单流（权重0.20）───
    if ofi_info:
        ofi_signal = ofi_info.get("signal", "neutral")
        ofi_strength = ofi_info.get("strength", 0)
        ofi_dir = "long" if ofi_signal in ("bullish", "mild_bullish") else (
            "short" if ofi_signal in ("bearish", "mild_bearish") else "neutral")
        if ofi_dir != "neutral":
            voter.add_source("订单流", ofi_dir, min(100, ofi_strength), 0.18,
                              f"OFI={ofi_info.get('ofi',0):.2f} 吃单买比={ofi_info.get('taker_buy_ratio',50):.0f}%")

    # ─── 信号源5: 行情模式（权重0.10）───
    if "上升" in regime_label:
        voter.add_source("行情模式", "long", 70, 0.09, regime_label)
    elif "下降" in regime_label:
        voter.add_source("行情模式", "short", 70, 0.09, regime_label)

    # ─── 信号源6: VRVP成交量分布（权重0.10）───
    if vrvp_info:
        vrvp_pos = vrvp_info.get("current_position", "")
        vrvp_signal = vrvp_info.get("signal", "neutral")
        vrvp_strength = vrvp_info.get("strength", 30)
        if vrvp_pos == "above_va" and "bull" in vrvp_signal:
            voter.add_source("VRVP", "long", min(80, vrvp_strength), 0.12,
                              f"价格在价值区上方 POC=${vrvp_info.get('poc',0)}")
        elif vrvp_pos == "below_va" and "bear" in vrvp_signal:
            voter.add_source("VRVP", "short", min(80, vrvp_strength), 0.12,
                              f"价格在价值区下方 POC=${vrvp_info.get('poc',0)}")
        elif vrvp_pos == "near_poc":
            # POC附近=关键位置，不投票但加分
            voter.add_source("VRVP", signal_direction, 50, 0.12,
                              f"POC附近关键位")

    # 投票决定
    direction, confidence, reasons = voter.decide()

    # 计算评分加成
    score_boost = 0
    if direction == signal_direction and confidence >= 50:
        # 投票支持当前方向 → 加分
        score_boost = min(15, confidence // 7)
        reasons.append(f"多信号投票支持，评分+{score_boost}")
    elif direction and direction != signal_direction:
        # 投票反对当前方向 → 扣分
        score_boost = -15
        reasons.append("多信号投票反对，信号否决")
    elif not direction and confidence > 0:
        # 投票无共识 → 轻微扣分
        score_boost = -5
        reasons.append("信号源分歧，减仓交易")

    return direction, confidence, reasons, score_boost
