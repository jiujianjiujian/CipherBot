"""
Cipher Walk-Forward 稳定性验证
把历史数据切成多段分别回测，看策略在不同市场下的表现是否一致。

如果各段结果差异很大 → 策略不稳定/过拟合
如果各段结果接近     → 策略可靠

用法:
    python3 scripts/walk_forward.py --pair BTCUSDT --days 90 --windows 3
    python3 scripts/walk_forward.py --pair ETHUSDT --days 60 --windows 6
"""
import json
import sys
import os
import math
from datetime import datetime, timezone
from typing import List, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import BacktestEngine, fetch_klines_range


def analyze_consistency(results: List[dict]) -> dict:
    """分析多段回测结果的一致性"""
    if not results:
        return {}

    metrics = {
        "win_rate": [r.get("win_rate", 0) for r in results],
        "total_return": [r.get("total_return", 0) for r in results],
        "max_drawdown": [r.get("max_drawdown", 0) for r in results],
        "profit_factor": [r.get("profit_factor", 0) for r in results],
        "sharpe": [r.get("sharpe", 0) for r in results],
        "signals_per_day": [
            r.get("total_signals", 0) / max(r.get("days", 1), 1) for r in results
        ],
    }

    consistency = {}
    for name, values in metrics.items():
        if not values:
            continue
        avg = sum(values) / len(values)
        std = math.sqrt(sum((v - avg) ** 2 for v in values) / len(values)) if len(values) > 1 else 0
        cv = std / avg * 100 if avg != 0 else 0  # 变异系数：越小越稳定
        consistency[name] = {
            "avg": round(avg, 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "std": round(std, 2),
            "cv_pct": round(cv, 1),  # 变异系数百分比
            "values": [round(v, 2) for v in values],
        }

    return consistency


def print_report(symbol: str, results: List[dict], consistency: dict, labels: List[str]):
    """打印可读报告"""
    print(f"\n{'='*65}")
    print(f"  Walk-Forward 验证: {symbol}")
    print(f"  窗口数: {len(results)}")
    print(f"{'='*65}")

    # 各窗口结果对比表
    print(f"\n  {'指标':<16}", end="")
    for l in labels:
        print(f"{l:>12}", end="")
    print(f"{'平均':>10}{'变异系数':>10}")

    row_templates = {
        "胜率%": "win_rate",
        "总收益%": "total_return",
        "最大回撤%": "max_drawdown",
        "盈亏比": "profit_factor",
        "Sharpe": "sharpe",
        "信号/天": "signals_per_day",
    }

    for label, key in row_templates.items():
        if key not in consistency:
            continue
        c = consistency[key]
        print(f"  {label:<16}", end="")
        for v in c["values"]:
            print(f"{v:>12}", end="")
        print(f"{c['avg']:>10.1f}{c['cv_pct']:>10.1f}%")

    # 稳定性评级
    print(f"\n  {'='*65}")
    print(f"  稳定性评估:")

    max_cv = max(c.get("cv_pct", 0) for c in consistency.values())
    if max_cv < 20:
        grade = "🟢 优秀"
        desc = "各窗口表现高度一致，策略稳健"
    elif max_cv < 50:
        grade = "🟡 一般"
        desc = "有一定波动但可接受，注意市场环境变化"
    else:
        grade = "🔴 不稳定"
        desc = "策略在不同市场下表现差异大，需谨慎"

    print(f"    等级: {grade}")
    print(f"    说明: {desc}")
    print(f"    最大变异系数: {max_cv:.1f}%")

    # 趋势判断
    returns = [r.get("total_return", 0) for r in results]
    if len(returns) >= 2:
        trend = "随时间下降" if returns[-1] < returns[0] * 0.7 else (
                 "随时间上升" if returns[-1] > returns[0] * 1.3 else "基本稳定")
        print(f"    收益趋势: {trend}")

    print(f"{'='*65}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cipher Walk-Forward 验证")
    parser.add_argument("--pair", default="BTCUSDT", help="交易对")
    parser.add_argument("--days", type=int, default=90, help="总回测天数")
    parser.add_argument("--windows", type=int, default=3, help="窗口数（默认3段）")
    args = parser.parse_args()

    symbol = args.pair
    total_days = args.days
    n_windows = args.windows
    window_days = total_days // n_windows
    pair_name = "BTC" if "BTC" in symbol else "ETH"

    print(f"\n  Walk-Forward 分析: {symbol}")
    print(f"  总周期: {total_days}天 | {n_windows}个窗口 | 每窗口约{window_days}天")
    print(f"  检验策略在不同市场环境下的稳定性")

    # 窗口偏移：分段1=最新30天，分段2=30天前，分段3=60天前
    results = []
    labels = []
    for w in range(n_windows):
        label = f"分段{w+1}"
        labels.append(label)
        offset = w * window_days  # 依次偏移30天
        print(f"   偏移: 从{offset}天前开始")
        engine = BacktestEngine(symbol=symbol, initial_balance=1000)
        # pass offset through a wrapper since run_window doesn't support it
        result = engine.run(days=window_days, days_offset=offset)
        result["days"] = window_days
        result["label"] = label
        result["offset_days"] = offset
        results.append(result)

    if not results:
        print("❌ 所有窗口回测失败")
        return

    # 分析一致性
    consistency = analyze_consistency(results)
    print_report(symbol, results, consistency, labels)

    # 保存结果
    report = {
        "symbol": symbol,
        "total_days": total_days,
        "windows": n_windows,
        "window_days": window_days,
        "results": results,
        "consistency": consistency,
    }
    report_file = f"walkforward_{symbol}_{total_days}d.json"
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, report_file), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"报告已保存: logs/{report_file}")


if __name__ == "__main__":
    main()
