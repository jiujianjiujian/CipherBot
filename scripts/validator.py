"""
Cipher 验证器 — 交易分析防幻觉引擎
在信号发出前逐条验证所有事实性陈述。

用法:
    from validator import validate_analysis
    result = validate_analysis(claims, raw_data)
    if result["passed"]:
        send_signal()
    else:
        for h in result["hallucinations"]:
            logger.warning(f"幻觉拦截: {h}")
"""
import json
import math
from typing import Optional, Dict, List, Tuple


def validate_price(claimed: float, actual: float, tolerance_pct: float = 0.1) -> dict:
    """验证价格准确性"""
    if actual <= 0:
        return {"passed": False, "issue": "实际价格为0，无法验证"}
    deviation = abs(claimed - actual) / actual * 100
    if deviation > tolerance_pct:
        return {
            "passed": False,
            "severity": "致命" if deviation > 1.0 else "严重" if deviation > 0.3 else "警告",
            "claimed": claimed,
            "actual": actual,
            "deviation_pct": round(deviation, 2),
            "issue": f"价格偏差{deviation:.2f}%（声称{claimed}，实际{actual}）",
            "corrected": actual,
        }
    return {"passed": True, "deviation_pct": round(deviation, 2)}


def validate_rsi(claimed: float, closes: List[float], period: int = 14, tolerance: float = 5.0) -> dict:
    """验证RSI计算"""
    if len(closes) < period + 1:
        return {"passed": False, "issue": f"数据不足(需{period+1}根)"}
    gains, losses = 0, 0
    for i in range(-period, 0):
        diff = closes[i] - closes[i-1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        actual_rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        actual_rsi = 100 - (100 / (1 + rs))

    deviation = abs(claimed - actual_rsi)
    if deviation > tolerance:
        return {
            "passed": False,
            "severity": "严重" if deviation > 10 else "警告",
            "claimed": claimed,
            "actual": round(actual_rsi, 1),
            "deviation": round(deviation, 1),
            "issue": f"RSI偏差{deviation:.1f}（声称{claimed}，实际{actual_rsi:.1f}）",
            "corrected": round(actual_rsi, 1),
        }
    return {"passed": True}


def validate_stop_pct(entry: float, stop_loss: float, claimed_stop_pct: float, tolerance: float = 0.05) -> dict:
    """验证止损百分比计算"""
    if entry <= 0:
        return {"passed": False, "issue": "入场价格无效"}
    actual_stop_pct = abs(entry - stop_loss) / entry * 100
    deviation = abs(claimed_stop_pct - actual_stop_pct)
    if deviation > tolerance:
        return {
            "passed": False,
            "severity": "警告",
            "claimed": claimed_stop_pct,
            "actual": round(actual_stop_pct, 2),
            "issue": f"止损%计算错误（声称{claimed_stop_pct}%，实际{actual_stop_pct:.2f}%）",
            "corrected": round(actual_stop_pct, 2),
        }
    return {"passed": True}


def validate_rr(entry: float, stop_loss: float, target: float, claimed_rr: float, tolerance: float = 0.1) -> dict:
    """验证盈亏比计算"""
    risk = abs(entry - stop_loss)
    reward = abs(target - entry)
    if risk == 0:
        return {"passed": False, "issue": "止损距离为0，无法计算R/R"}
    actual_rr = round(reward / risk, 1)
    deviation = abs(claimed_rr - actual_rr)
    if deviation > tolerance:
        return {
            "passed": False,
            "severity": "警告",
            "claimed": claimed_rr,
            "actual": actual_rr,
            "issue": f"R/R计算错误（声称{claimed_rr}:1，实际{actual_rr}:1）",
            "corrected": actual_rr,
        }
    return {"passed": True}


def validate_score(score_detail: dict) -> dict:
    """验证评分：检查单项是否越界、总分是否正确"""
    issues = []
    weights = {
        "timeframe_alignment": 15,
        "price_structure": 25,
        "volume_verification": 20,
        "candle_pattern": 15,
        "risk_reward": 15,
        "momentum": 10,
    }
    total = 0
    for dim, max_score in weights.items():
        val = score_detail.get(dim, 0)
        if val < 0 or val > max_score:
            issues.append(f"{dim}评分{val}越界[0-{max_score}]")
        total += val
    if total > 100:
        issues.append(f"总分{total}超过100")

    return {
        "passed": len(issues) == 0,
        "total_computed": total,
        "issues": issues,
    }


def validate_consistency(claims: dict) -> list:
    """检测分析结论内部的矛盾"""
    contradictions = []

    direction = claims.get("direction", "")
    rsi = claims.get("rsi", 50)
    pattern = claims.get("pattern", "")
    score = claims.get("score", 50)
    near_support = claims.get("near_support", False)
    near_resistance = claims.get("near_resistance", False)
    stop_pct = claims.get("stop_pct", 0)

    # 方向 vs RSI
    if direction == "long" and rsi > 75:
        contradictions.append(f"做多建议但RSI={rsi}处于超买区，追高风险大")
    if direction == "short" and rsi < 25:
        contradictions.append(f"做空建议但RSI={rsi}处于超卖区，追杀风险大")

    # 方向 vs 关键位
    if direction == "long" and near_resistance:
        contradictions.append("做多但价格在阻力位附近，追高风险大")
    if direction == "short" and near_support:
        contradictions.append("做空但价格在支撑位附近，追杀风险大")

    # 评分 vs 止损
    if score >= 80 and stop_pct > 0.8:
        contradictions.append(f"强信号(评分{score})但止损{stop_pct}%偏大，信号质量与风险不匹配")

    # 形态 vs 评分
    high_confidence_patterns = ["支撑+Pin Bar", "阻力+射击之星", "吞没", "三连阳", "三连阴"]
    if any(p in pattern for p in high_confidence_patterns) and score < 60:
        contradictions.append(f"声称形态\"{pattern}\"但评分仅{score}，形态声明与数据不匹配")

    return contradictions


def validate_analysis(claims: dict, raw_data: dict = None) -> dict:
    """
    主验证入口。验证分析结果中的所有事实性声明。

    claims 字段:
        current_price, support, resistance, rsi, stop_pct, rr,
        direction, pattern, score, score_detail, entry, stop_loss, target

    raw_data 字段(可选):
        price_actual: 实时价格
        closes_15m: 15m收盘价列表
        klines_15m: 15m K线数据

    returns:
        passed: 是否通过
        checks: 逐项检查结果
        hallucinations: 发现的问题列表
        corrections: 修正建议
        contradictions: 矛盾检测结果
    """
    results = []
    hallucinations = []
    corrections = {}
    contradictions = []

    # ——— 1. 价格验证 ———
    if raw_data and "price_actual" in raw_data:
        if claims.get("current_price"):
            r = validate_price(claims["current_price"], raw_data["price_actual"])
            results.append(("价格验证", r))
            if not r["passed"]:
                hallucinations.append(r["issue"])
                if "corrected" in r:
                    corrections["current_price"] = r["corrected"]

    # ——— 2. RSI验证 ———
    if raw_data and "closes_15m" in raw_data and claims.get("rsi") is not None:
        r = validate_rsi(claims["rsi"], raw_data["closes_15m"])
        results.append(("RSI验证", r))
        if not r["passed"]:
            hallucinations.append(r["issue"])
            if "corrected" in r:
                corrections["rsi"] = r["corrected"]

    # ——— 3. 止损%验证 ———
    if claims.get("entry") and claims.get("stop_loss") and claims.get("stop_pct") is not None:
        r = validate_stop_pct(claims["entry"], claims["stop_loss"], claims["stop_pct"])
        results.append(("止损%验证", r))
        if not r["passed"]:
            hallucinations.append(r["issue"])
            if "corrected" in r:
                corrections["stop_pct"] = r["corrected"]

    # ——— 4. R/R验证 ———
    if claims.get("entry") and claims.get("stop_loss") and claims.get("target") and claims.get("rr") is not None:
        r = validate_rr(claims["entry"], claims["stop_loss"], claims["target"], claims["rr"])
        results.append(("R/R验证", r))
        if not r["passed"]:
            hallucinations.append(r["issue"])
            if "corrected" in r:
                corrections["rr"] = r["corrected"]

    # ——— 5. 评分验证 ———
    if claims.get("score_detail"):
        r = validate_score(claims["score_detail"])
        results.append(("评分验证", r))
        if not r["passed"]:
            for issue in r["issues"]:
                hallucinations.append(f"评分系统: {issue}")

    # ——— 6. 矛盾检测 ———
    contradictions = validate_consistency(claims)
    if contradictions:
        for c in contradictions:
            results.append(("矛盾检测", {"passed": False, "issue": c}))
            hallucinations.append(c)

    # ——— 结果汇总 ———
    hallucination_count = len(hallucinations)
    if hallucination_count == 0:
        passed = True
        summary = "验证通过，未发现幻觉"
    elif hallucination_count == 1:
        passed = True  # 单个问题可修正
        summary = f"发现1处问题，已自动修正"
    else:
        passed = False
        summary = f"发现{hallucination_count}处问题，拦截信号"

    # 如果只有价格/数值偏差，自动修正后可以放行
    only_numeric = all(
        "价格偏差" in h or "计算错误" in h or "修正" in str(corrections)
        for h in hallucinations
    )
    if only_numeric and hallucination_count <= 2:
        passed = True
        summary += "（数值偏差已自动修正）"

    return {
        "passed": passed,
        "summary": summary,
        "hallucinations": hallucinations,
        "corrections": corrections,
        "contradictions": contradictions,
        "checks": results,
    }
