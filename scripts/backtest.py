"""
Cipher 回测引擎
在历史K线上回放交易策略，评估真实表现。

用法:
    python3 scripts/backtest.py --pair BTCUSDT --days 90

输出:
    胜率 / 盈亏比 / 最大回撤 / Sharpe / 交易明细
"""
import json
import sys
import os
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import THREE_COMMAS, TELEGRAM, TRADING, ANALYSIS, SCORING

# 复用主程序的指标函数
from cipher_bot import (
    calc_rsi, calc_ema, calc_sma, calc_atr, calc_local_atr,
    detect_market_structure, score_signal, check_timeframe_alignment,
    analyze_candles, calc_position_size,
)


def fetch_klines(symbol: str, interval: str, limit: int = 500, end_time: int = None, start_time: int = None) -> Optional[List[dict]]:
    """获取历史K线数据（支持分页和时间偏移）"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if start_time:
        url += f"&startTime={start_time}"
    if end_time:
        url += f"&endTime={end_time}"
    try:
        req = Request(url, headers={"User-Agent": "CipherBot/4.0"})
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        return [{
            "time": k[0], "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
        } for k in data]
    except Exception as e:
        print(f"  ❌ 获取数据失败: {e}")
        return None


def fetch_klines_range(symbol: str, interval: str, total_needed: int, days_ago: int = 0) -> List[dict]:
    """分批拉取历史K线，支持时间偏移（days_ago=30表示30天前的数据）"""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_time = now_ms - days_ago * 86400 * 1000
    page_size = 500
    pages_needed = (total_needed + page_size - 1) // page_size
    all_data = []
    current_end = end_time
    for page in range(pages_needed):
        batch = fetch_klines(symbol, interval, min(500, total_needed), end_time=current_end)
        if not batch or len(batch) == 0:
            break
        all_data = batch + all_data
        current_end = batch[0]["time"] - 1
        if page == 0 and len(batch) < page_size:
            break
    return all_data[:total_needed]


class BacktestEngine:
    def __init__(self, symbol: str = "BTCUSDT", initial_balance: float = 1000):
        self.symbol = symbol
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.trades = []
        self.equity_curve = []
        self.max_balance = initial_balance
        self.max_drawdown = 0

    def run(self, days: int = 90, days_offset: int = 0) -> dict:
        """运行回测（days_offset: 从N天前的数据开始，用于walk-forward）"""
        pair_name = "BTC" if "BTC" in self.symbol else "ETH"
        offset_str = f" | 偏移{days_offset}天" if days_offset else ""
        print(f"\n{'='*60}")
        print(f"回测: {self.symbol} | 周期: {days}天{offset_str}")
        print(f"{'='*60}")

        # 拉数据（15m底层  + 1h + 4h）
        print("拉取历史数据（分页加载）...")
        limit_15m = days * 96 + 100  # 15m = 96根/天+缓冲区
        limit_1h = days * 24 + 50
        limit_4h = days * 6 + 30

        k15_all = fetch_klines_range(self.symbol, "15m", limit_15m, days_offset)
        k1h_all = fetch_klines_range(self.symbol, "1h", limit_1h, days_offset)
        k4h_all = fetch_klines_range(self.symbol, "4h", limit_4h, days_offset)

        if not all([k15_all, k1h_all, k4h_all]):
            return {"error": "数据获取失败"}

        print(f"  15m: {len(k15_all)}根 | 1h: {len(k1h_all)}根 | 4h: {len(k4h_all)}根")

        # 使用1h时间戳作为同步锚点
        # 对每根1h K线，取前面足够多的15m/4h数据来跑信号
        min_context_15m = 30  # 至少需要30根15m做分析
        min_context_4h = 24

        signal_count = 0
        win_count = 0
        loss_count = 0
        total_profit = 0
        cooldown_until = 0  # 信号冷却：防止连续开仓（3Commas最多1个持仓）

        print("\n逐根回放1H K线...")

        # 从有足够context的地方开始
        start_idx = max(min_context_15m // 4, 5)

        for i in range(start_idx, len(k1h_all)):
            # 冷却中，跳过
            if i < cooldown_until:
                continue

            # 当前1h时刻
            current_1h = k1h_all[i]
            current_time = datetime.fromtimestamp(current_1h["time"] / 1000, tz=timezone.utc)
            current_price = current_1h["close"]

            # 对齐的15m数据（取到当前时间为止）——注意：只用当前K线之前的数据
            cutoff_ms = current_1h["time"]
            k15_window = [k for k in k15_all if k["time"] <= cutoff_ms][-50:]
            k1h_window = [k for k in k1h_all if k["time"] <= cutoff_ms][-30:]
            k4h_window = [k for k in k4h_all if k["time"] <= cutoff_ms][-30:]

            if len(k15_window) < min_context_15m or len(k1h_window) < 12:
                continue

            # 构造24h ticker
            recent_24h = k15_window[-96:] if len(k15_window) >= 96 else k15_window
            ticker_24h = {
                "high": max(k["high"] for k in recent_24h),
                "low": min(k["low"] for k in recent_24h),
                "volume": sum(k["volume"] for k in recent_24h),
                "last_price": current_price,
            }

            # 跑信号引擎
            signal = self._evaluate_signal(current_price, ticker_24h, k15_window, k1h_window, k4h_window)

            if signal:
                signal_count += 1
                # 模拟交易：传入当前时间之后的数据（避免前瞻偏差）
                future_k15 = [k for k in k15_all if k["time"] > cutoff_ms]
                result = self._simulate_trade(signal, future_k15)
                if result["pnl_pct"] > 0:
                    win_count += 1
                else:
                    loss_count += 1
                total_profit += result["pnl_pct"]
                self.balance *= (1 + result["pnl_pct"] / 100)
                self.equity_curve.append((current_time, self.balance))

                # 更新最大回撤
                if self.balance > self.max_balance:
                    self.max_balance = self.balance
                dd = (self.max_balance - self.balance) / self.max_balance * 100
                if dd > self.max_drawdown:
                    self.max_drawdown = dd

                # 冷却8根1H（8小时），模拟持仓周期
                cooldown_until = i + 8
                # 打印交易
                action = "做多" if signal["direction"] == "long" else "做空"
                print(f"  {current_time.strftime('%m-%d %H:%M')} | {action} | "
                      f"评分{signal['score']} | R/R={signal['rr']:.1f} | "
                      f"实际{result['pnl_pct']:+.2f}%")

        # 计算最终指标
        total_trades = signal_count
        win_rate = win_count / total_trades * 100 if total_trades > 0 else 0
        profit_factor = total_profit / abs(sum(
            t["pnl_pct"] for t in self.trades if t["pnl_pct"] < 0
        )) if any(t["pnl_pct"] < 0 for t in self.trades) else float('inf')

        # Sharpe Ratio (简化: 平均收益/标准差*√242 按1h)
        if self.trades:
            returns = [t["pnl_pct"] for t in self.trades]
            avg_r = sum(returns) / len(returns)
            std_r = math.sqrt(sum((r - avg_r)**2 for r in returns) / len(returns))
            sharpe = avg_r / std_r * math.sqrt(242) if std_r > 0 else 0
        else:
            sharpe = 0

        print(f"\n{'='*60}")
        print(f"📊 回测结果")
        print(f"{'='*60}")
        print(f"总信号: {total_trades}次")
        print(f"胜率: {win_rate:.1f}% ({win_count}胜/{loss_count}负)")
        print(f"总收益: {total_profit:+.2f}%")
        print(f"最终余额: ${self.balance:.2f} (起始${self.initial_balance})")
        print(f"最大回撤: {self.max_drawdown:.2f}%")
        print(f"盈亏比(Profit Factor): {profit_factor:.2f}")
        print(f"Sharpe Ratio: {sharpe:.2f}")
        print(f"平均每笔: {total_profit/total_trades:+.2f}%" if total_trades > 0 else "")

        return {
            "symbol": self.symbol,
            "days": days,
            "total_signals": total_trades,
            "win_rate": round(win_rate, 1),
            "wins": win_count,
            "losses": loss_count,
            "total_return": round(total_profit, 2),
            "final_balance": round(self.balance, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "profit_factor": round(profit_factor, 2),
            "sharpe": round(sharpe, 2),
            "avg_trade": round(total_profit/total_trades, 2) if total_trades > 0 else 0,
        }

    def _evaluate_signal(self, price: float, ticker: dict,
                          k15: List[dict], k1h: List[dict], k4h: List[dict]) -> Optional[dict]:
        """在给定时刻评估信号"""
        closes_15m = [k["close"] for k in k15]
        closes_1h = [k["close"] for k in k1h]

        rsi_1h = calc_rsi(closes_1h, 14)
        atr_1h = calc_atr(k1h, 14)
        structure_1h = detect_market_structure(k1h)
        ema21_4h = calc_ema([k["close"] for k in k4h], 21)

        high_24h = ticker["high"]
        low_24h = ticker["low"]
        range_24h = high_24h - low_24h
        position_pct = (price - low_24h) / range_24h * 100 if range_24h > 0 else 50

        sr_15m_high = max(k["high"] for k in k15[-10:])
        sr_15m_low = min(k["low"] for k in k15[-10:])

        local_vol = calc_local_atr(k15, 3)
        vol_ratio = local_vol / atr_1h if atr_1h > 0 else 1.0
        atr_multiplier = max(0.3, min(0.8, vol_ratio * 0.5))
        base_stop_atr = atr_1h * atr_multiplier

        # 做多
        near_support = price < low_24h * 1.015 or price < sr_15m_low * 1.01
        support_level = min(low_24h, sr_15m_low)

        candidates = []

        if near_support and position_pct < 40:
            stop_loss = support_level - base_stop_atr * 0.5
            stop_pct = (price - stop_loss) / price * 100 if price > stop_loss else 0.5
            stop_pct = max(stop_pct, 0.3)
            target = min(ema21_4h if price < ema21_4h else high_24h, high_24h)
            target = max(target, price * 1.008)
            rr = abs(target - price) / abs(price - stop_loss) if abs(price - stop_loss) > 0 else 0

            if stop_pct < 1.2 and rr >= 2.0:
                alignment = check_timeframe_alignment(k15, k1h, "long")
                candle_analysis = analyze_candles(k15, "long")
                sig_score, sd, reasons, risks = score_signal(
                    "long", price, stop_loss, target, k15, k1h,
                    structure_1h, rsi_1h, atr_1h,
                    alignment=alignment, candle_analysis=candle_analysis,
                    near_support=near_support, support_level=support_level,
                )
                if sig_score >= 60:
                    amount = calc_position_size(sig_score, atr_1h, price)
                    candidates.append({
                        "direction": "long", "entry": price,
                        "stop_loss": round(stop_loss, 1), "target": round(target, 1),
                        "stop_pct": round(stop_pct, 2), "rr": round(rr, 2),
                        "score": sig_score, "score_detail": sd,
                        "amount_pct": amount,
                    })

        # 做空
        near_resistance = price > high_24h * 0.985 or price > sr_15m_high * 0.99
        resistance_level = max(high_24h, sr_15m_high)

        if near_resistance and position_pct > 60:
            stop_loss = resistance_level + base_stop_atr * 0.5
            stop_pct = (stop_loss - price) / price * 100 if stop_loss > price else 0.5
            stop_pct = max(stop_pct, 0.3)
            target = max(ema21_4h if price > ema21_4h else low_24h, low_24h)
            target = min(target, price * 0.992)
            rr = abs(target - price) / abs(price - stop_loss) if abs(price - stop_loss) > 0 else 0

            if stop_pct < 1.2 and rr >= 2.0:
                alignment = check_timeframe_alignment(k15, k1h, "short")
                candle_analysis = analyze_candles(k15, "short")
                sig_score, sd, reasons, risks = score_signal(
                    "short", price, stop_loss, target, k15, k1h,
                    structure_1h, rsi_1h, atr_1h,
                    alignment=alignment, candle_analysis=candle_analysis,
                    near_resistance=near_resistance, resistance_level=resistance_level,
                )
                if sig_score >= 60:
                    amount = calc_position_size(sig_score, atr_1h, price)
                    candidates.append({
                        "direction": "short", "entry": price,
                        "stop_loss": round(stop_loss, 1), "target": round(target, 1),
                        "stop_pct": round(stop_pct, 2), "rr": round(rr, 2),
                        "score": sig_score, "score_detail": sd,
                        "amount_pct": amount,
                    })

        if candidates:
            candidates.sort(key=lambda s: s["score"] * s["rr"], reverse=True)
            return candidates[0]
        return None

    def _simulate_trade(self, signal: dict, k15_next: List[dict]) -> dict:
        """模拟交易结果：用后续K线判断是否触发止损/止盈"""
        entry = signal["entry"]
        stop = signal["stop_loss"]
        target = signal["target"]
        direction = signal["direction"]

        # 找后续最多48根15m（12小时）内的最高最低
        future = k15_next[-48:] if len(k15_next) >= 48 else k15_next
        if direction == "long":
            hit_low = min(k["low"] for k in future)
            hit_high = max(k["high"] for k in future)
            if hit_low <= stop:
                pnl = (stop - entry) / entry * 100
                reason = "止损"
            elif hit_high >= target:
                pnl = (target - entry) / entry * 100
                reason = "止盈"
            else:
                exit_price = future[-1]["close"]
                pnl = (exit_price - entry) / entry * 100
                reason = "时间到期"
        else:
            hit_low = min(k["low"] for k in future)
            hit_high = max(k["high"] for k in future)
            if hit_high >= stop:
                pnl = (entry - stop) / entry * 100
                reason = "止损"
            elif hit_low <= target:
                pnl = (entry - target) / entry * 100
                reason = "止盈"
            else:
                exit_price = future[-1]["close"]
                pnl = (entry - exit_price) / entry * 100
                reason = "时间到期"

        trade = {
            "time": datetime.now().isoformat(),
            "direction": direction, "entry": entry,
            "stop": stop, "target": target,
            "pnl_pct": round(pnl, 2), "reason": reason,
        }
        self.trades.append(trade)
        return trade


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cipher 回测引擎")
    parser.add_argument("--pair", default="BTCUSDT", help="交易对")
    parser.add_argument("--days", type=int, default=90, help="回测天数")
    parser.add_argument("--balance", type=float, default=1000, help="起始资金")
    args = parser.parse_args()

    engine = BacktestEngine(symbol=args.pair, initial_balance=args.balance)
    result = engine.run(days=args.days)

    # 保存结果
    report_file = f"backtest_{args.pair}_{args.days}d.json"
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", report_file), "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n报告已保存: logs/{report_file}")


if __name__ == "__main__":
    main()
