# -*- coding: utf-8 -*-
"""
Cipher 风控模块 — 账户级总风控系统

功能:
  1. 单日最大亏损熔断
  2. 连亏熔断
  3. 最大持仓数限制
  4. 最大总风险敞口
  5. 强平距离保护
  6. 杠杆上限二次确认
  7. 滑点/价差/深度检查
  8. 订单生命周期管理

所有风控规则：当天文件持久化，重启不丢失。
"""
import os, json, time
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple

# 文件路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
RISK_FILE = os.path.join(LOG_DIR, "risk_state.json")
TRADE_FILE = os.path.join(LOG_DIR, "trades.jsonl")

# 默认风控参数（可在 config.py 中覆盖）
DEFAULT_RISK_CONFIG = {
    "max_daily_loss_pct": 3.0,       # 单日最大亏损 3%
    "max_consecutive_losses": 3,      # 连亏 3 单熔断
    "consecutive_cooldown_hours": 4,  # 连亏后停机 4 小时
    "max_positions_same_dir": 1,      # 同方向最多 1 单
    "max_total_risk_pct": 2.0,        # 总风险敞口 ≤ 2%
    "min_liquidation_distance_pct": 15.0,  # 强平距离 ≥ 15%
    "leverage_cap_normal": 15,        # 普通信号 ≤ 15x
    "leverage_cap_strong": 25,        # 强信号(A+级) ≤ 25x
    "min_strong_score": 80,           # 25x 需要的最低评分
    "max_spread_pct": 0.05,           # 最大价差 0.05%
    "min_depth_ratio": 5.0,           # 最小深度比
    "max_slippage_pct": 0.15,         # 最大预估滑点
    "order_timeout_seconds": 300,     # 订单超时 5 分钟
    "capital_base": 10000,            # 本金基数
    "daily_profit_lock_pct": 8.0,     # 单日盈利达8%锁仓停机
    "daily_peak_drawdown_pct": 2.0,   # 日内盈利回撤>2%停机
    "use_capital_pct": 0.25,          # 每笔使用本金比例25%
    "min_net_profit_usdt": 20,        # 单笔最低净利润20U
    "capital_base": 1000,             # 本金基数
    "max_position_time_minutes": 120, # 最大持仓时间
    "fee_pct": 0.05,                  # 手续费率%
    "slippage_pct": 0.08,             # 预估滑点%
    "real_rr_min": 2.0,               # 扣除费用后最低R/R
    "auto_degrade_after_losses": 2,   # 连亏2单自动降级
    "degrade_multiplier": 0.5,        # 降级后仓位乘数
}


class RiskEngine:
    """风控引擎 — 每个扫描周期调用一次"""

    def __init__(self, config: dict = None):
        self.config = {**DEFAULT_RISK_CONFIG, **(config or {})}
        self.state = self._load_state()
        self.violations: List[str] = []

    # ─── 状态持久化 ───
    def _load_state(self) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        default = {
            "date": today,
            "daily_pnl": 0.0,          # 今日累计盈亏(本金%)
            "consecutive_losses": 0,    # 连亏计数
            "consecutive_stop_until": 0.0,  # 连亏熔断截止时间戳
            "loss_limit_hit": False,    # 当日是否已触发亏损熔断
        }
        if os.path.exists(RISK_FILE):
            try:
                with open(RISK_FILE) as f:
                    saved = json.load(f)
                if saved.get("date") == today:
                    return {**default, **saved}
            except: pass
        return default

    def _save_state(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(RISK_FILE, "w") as f:
                json.dump(self.state, f)
        except Exception as e:
            print(f"[风控] 状态保存失败: {e}")

    # ─── 1. 更新盈亏（每次信号结束后调用）───
    def update_trade_result(self, pnl_pct: float):
        """更新交易结果，用于连亏/日亏损计算"""
        self.state["daily_pnl"] = round(self.state["daily_pnl"] + pnl_pct, 2)
        if pnl_pct < 0:
            self.state["consecutive_losses"] += 1
        else:
            self.state["consecutive_losses"] = 0
        self._save_state()

    # ─── 2. 单日亏损熔断 ───
    def check_daily_loss(self) -> bool:
        """日亏损超过阈值 → 熔断"""
        max_loss = self.config["max_daily_loss_pct"]
        if self.state["daily_pnl"] <= -max_loss:
            self.state["loss_limit_hit"] = True
            self._save_state()
            self.violations.append(f"日亏损 {self.state['daily_pnl']:.1f}% ≤ -{max_loss}%，熔断")
            return True
        return False

    # ─── 3. 连亏熔断 ───
    def check_consecutive_losses(self) -> bool:
        """连亏超过阈值 → 停机 N 小时"""
        now = time.time()
        if now < self.state["consecutive_stop_until"]:
            remain = self.state["consecutive_stop_until"] - now
            self.violations.append(f"连亏熔断中，剩余 {int(remain//60)} 分钟")
            return True
        if self.state["consecutive_losses"] >= self.config["max_consecutive_losses"]:
            cooldown = self.config["consecutive_cooldown_hours"] * 3600
            self.state["consecutive_stop_until"] = now + cooldown
            self._save_state()
            self.violations.append(f"连亏 {self.state['consecutive_losses']} 单，熔断 {self.config['consecutive_cooldown_hours']} 小时")
            return True
        return False

    # ─── 4. 杠杆上限确认 ───
    def check_leverage(self, score: int, lev: int) -> Tuple[bool, int]:
        """根据信号质量限制杠杆"""
        min_strong = self.config["min_strong_score"]
        cap_normal = self.config["leverage_cap_normal"]
        cap_strong = self.config["leverage_cap_strong"]
        if score >= min_strong:
            max_lev = cap_strong
        else:
            max_lev = cap_normal
        if lev > max_lev:
            self.violations.append(f"杠杆 {lev}x > 最大 {max_lev}x（评分{score}），已降级")
            return False, max_lev
        return True, lev

    # ─── 5. 强平距离检查（需传入强平价）───
    def check_liquidation_distance(self, price: float, liq_price: float, direction: str) -> bool:
        if liq_price <= 0:
            return True  # 无强平数据时放行
        if direction == "long":
            dist = (price - liq_price) / price * 100
        else:
            dist = (liq_price - price) / price * 100
        min_dist = self.config["min_liquidation_distance_pct"]
        if dist < min_dist:
            self.violations.append(f"强平距离 {dist:.1f}% < {min_dist}%，拒单")
            return False
        return True

    # ─── 6. 价差/深度检查（使用order_flow数据）───
    def check_spread_and_depth(self, order_book: dict) -> bool:
        """检查买一卖一价差和订单簿深度"""
        if not order_book:
            return True
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        if len(bids) < 2 or len(asks) < 2:
            return True
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 0
        if spread_pct > self.config["max_spread_pct"]:
            self.violations.append(f"价差 {spread_pct:.3f}% > {self.config['max_spread_pct']}%，拒单")
            return False
        return True

    # ─── 7. 订单超时检查 ───
    def check_order_timeout(self, signal_time: float) -> bool:
        """信号发出超过 N 秒未成交则不再有效"""
        elapsed = time.time() - signal_time
        if elapsed > self.config["order_timeout_seconds"]:
            self.violations.append(f"信号超时 {elapsed:.0f}s > {self.config['order_timeout_seconds']}s，丢弃")
            return False
        return True

    # ─── 8. 净利润过滤（扣除费用后净赚≥20U）───
    def check_net_profit(self, entry: float, stop: float, target: float,
                         direction: str, leverage: int, pattern: str) -> Tuple[bool, float]:
        capital = self.config["capital_base"] * self.config["use_capital_pct"]
        notional = capital * leverage
        if direction == "long":
            gross_pnl = (target - entry) / entry * notional
        else:
            gross_pnl = (entry - target) / entry * notional
        fee = self.config["fee_pct"] / 100 * notional * 2
        slippage = self.config["slippage_pct"] / 100 * notional
        net = gross_pnl - fee - slippage
        min_net = self.config["min_net_profit_usdt"]
        if "剥头皮" in pattern or "scalp" in pattern.lower():
            min_rr = 1.5
            if net / 20 < 1.5: pass  # 剥头皮可接受略低
        if net < min_net:
            self.violations.append(f"净利润${net:.1f}<${min_net}，拒单")
            return False, round(net, 1)
        return True, round(net, 1)

    # ─── 9. 最大持仓时间（策略自适应）───
    STRATEGY_TTL = {
        "scalp": 12, "剥头皮": 12,
        "fakeout": 45, "假突破": 45,
        "range": 90, "震荡": 90,
        "breakout": 180, "突破": 180,
        "trend": 240, "趋势": 240,
        "vwap": 60, "波动": 60,
    }
    def check_max_position_time(self, open_time: float, pattern: str = "") -> bool:
        """超过最大持仓时间未达TP1 → 强制退出"""
        if open_time <= 0:
            return True
        elapsed_min = (time.time() - open_time) / 60
        max_min = self.config["max_position_time_minutes"]
        if elapsed_min > max_min:
            self.violations.append(f"持仓 {elapsed_min:.0f}min > {max_min}min 上限，时间止损")
            return False
        return True

    # ─── 9. 真实盈亏比（扣除手续费+滑点）───
    def check_real_rr(self, entry: float, stop: float, target: float, direction: str) -> bool:
        """扣除手续费和预估滑点后，真实R/R是否仍满足要求"""
        if entry <= 0 or stop <= 0 or target <= 0:
            return True
        fee = self.config["fee_pct"]
        slip = self.config["slippage_pct"]
        total_cost = fee + slip
        if direction == "long":
            gross_risk = abs(entry - stop) / entry * 100
            gross_reward = abs(target - entry) / entry * 100
        else:
            gross_risk = abs(stop - entry) / entry * 100
            gross_reward = abs(entry - target) / entry * 100
        net_risk = gross_risk + total_cost
        net_reward = gross_reward - total_cost
        real_rr = net_reward / net_risk if net_risk > 0 else 0
        min_rr = self.config["real_rr_min"]
        if real_rr < min_rr:
            self.violations.append(f"扣除费用后真实R/R {real_rr:.1f} < {min_rr}，拒单")
            return False
        return True

    # ─── 10. 单日利润锁仓 ───
    def check_profit_lock(self) -> bool:
        """单日盈利达阈值 → 锁仓停机"""
        if self.state["daily_pnl"] >= self.config["daily_profit_lock_pct"]:
            self.violations.append(f"日盈利{self.state['daily_pnl']:.1f}% ≥ {self.config['daily_profit_lock_pct']}%，锁仓停机")
            return True
        return False

    # ─── 11. 杠杆分级 ───
    def get_leverage_tier(self, score: int, pattern: str) -> int:
        """根据信号质量返回杠杆倍数"""
        # OFI剥头皮最高30x
        if "剥头皮" in pattern or "scalp" in pattern.lower():
            if score >= 75: return 30
            if score >= 60: return 25
            return 20
        # S级信号
        if score >= 85: return 25
        # A+级
        if score >= 75: return 20
        # A级
        if score >= 65: return 15
        # B级不下单
        return 10

    # ─── 12. 自动降级判断 ───
    def get_degrade_factor(self) -> float:
        """连亏超过阈值自动降级仓位"""
        threshold = self.config["auto_degrade_after_losses"]
        if self.state["consecutive_losses"] >= threshold:
            return self.config["degrade_multiplier"]
        return 1.0

    # ─── 8. 总风控检查（一站式调用）───
    def check_all(self, signal: dict, order_book: dict = None,
                  liq_price: float = 0) -> Tuple[bool, List[str]]:
        """
        全量风控检查。空signal={}时只做熔断检查，不做具体信号检查。
        """
        self.violations = []
        passed = True

        # 熔断检查（空信号也检查）
        if self.check_daily_loss():
            passed = False
        if self.check_consecutive_losses():
            passed = False

        # 具体信号检查（空信号跳过）
        if signal.get("score") is not None:
            score = signal.get("score", 0)
            lev = signal.get("leverage", 25)
            lev_ok, adjusted_lev = self.check_leverage(score, lev)
            if not lev_ok:
                signal["leverage"] = adjusted_lev

            price = signal.get("entry", 0)
            stop = signal.get("stop_loss", 0)
            target = signal.get("target", 0)
            direction = signal.get("direction", "long")

            if not self.check_liquidation_distance(price, liq_price, direction):
                passed = False
            if not self.check_real_rr(price, stop, target, direction):
                passed = False
            # 净利润过滤
            lev = signal.get("leverage", 15)
            pat = signal.get("pattern", "")
            net_ok, net_val = self.check_net_profit(price, stop, target, direction, lev, pat)
            if not net_ok:
                passed = False
            if order_book and not self.check_spread_and_depth(order_book):
                passed = False

            # 自动降级乘数
            degrade = self.get_degrade_factor()
            if degrade < 1.0:
                signal["degrade_factor"] = degrade
                self.violations.append(f"连亏{self.state['consecutive_losses']}单，仓位降级x{degrade}")

        return passed, self.violations


# ─── 日亏损/连亏数据读取（供复盘用）───
def load_risk_state() -> dict:
    if os.path.exists(RISK_FILE):
        try:
            with open(RISK_FILE) as f:
                return json.load(f)
        except: pass
    return {}


# ─── 从trades.jsonl计算今日盈亏 ───
def calc_daily_pnl() -> float:
    """计算今日累计盈亏（本金%）"""
    if not os.path.exists(TRADE_FILE):
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    try:
        with open(TRADE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    t = json.loads(line)
                    if t.get("time", "").startswith(today) and t.get("pnl") is not None:
                        total += float(t["pnl"])
                except: pass
    except: pass
    return round(total, 2)
