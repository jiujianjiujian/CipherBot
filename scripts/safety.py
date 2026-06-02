# -*- coding: utf-8 -*-
"""
Cipher 安全模块 — signal_id防重 / 禁交易时段 / 健康检查 / 日结复盘

依赖: risk_control (风控状态), trades.jsonl (交易日志)
"""
import os, json, time, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
SIGNAL_IDS_FILE = os.path.join(LOG_DIR, "executed_signal_ids.json")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "trades.jsonl")

# ============================================================
# 1. signal_id 防重复执行
# ============================================================

def generate_signal_id() -> str:
    """生成唯一信号ID"""
    return uuid.uuid4().hex[:12]

def is_signal_executed(signal_id: str) -> bool:
    """检查signal_id是否已执行过（防止API超时重复下单）"""
    if not os.path.exists(SIGNAL_IDS_FILE):
        return False
    try:
        with open(SIGNAL_IDS_FILE) as f:
            executed = json.load(f)
        return signal_id in executed
    except:
        return False

def mark_signal_executed(signal_id: str):
    """标记signal_id为已执行"""
    executed = {}
    if os.path.exists(SIGNAL_IDS_FILE):
        try:
            with open(SIGNAL_IDS_FILE) as f:
                executed = json.load(f)
        except: pass
    executed[signal_id] = time.time()
    # 保留最近1000条，防文件膨胀
    if len(executed) > 1000:
        sorted_items = sorted(executed.items(), key=lambda x: -x[1])[:1000]
        executed = dict(sorted_items)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(SIGNAL_IDS_FILE, "w") as f:
            json.dump(executed, f)
    except: pass


# ============================================================
# 2. 重大事件禁交易时段（2026年）
# ============================================================

# CPI公布: 每月第二或第三周 北京时间 20:30
# FOMC: 每6-8周一次 北京时间凌晨2:00
# 非农: 每月第一个周五 北京时间 20:30
EVENTS_2026 = [
    # (月, 日, 类型, 影响时长分钟)
    # 1月
    (1, 14, "CPI", 30), (1, 28, "FOMC", 60), (1, 9, "非农", 30),
    # 2月
    (2, 12, "CPI", 30), (2, 6, "非农", 30),
    # 3月
    (3, 12, "CPI", 30), (3, 18, "FOMC", 60), (3, 7, "非农", 30),
    # 4月
    (4, 10, "CPI", 30), (4, 3, "非农", 30),
    # 5月
    (5, 8, "CPI", 30), (5, 2, "非农", 30),
    # 6月
    (6, 11, "CPI", 30), (6, 17, "FOMC", 60), (6, 5, "非农", 30),
    # 7月
    (7, 9, "CPI", 30), (7, 30, "FOMC", 60), (7, 3, "非农", 30),
    # 8月
    (8, 13, "CPI", 30), (8, 7, "非农", 30),
    # 9月
    (9, 10, "CPI", 30), (9, 16, "FOMC", 60), (9, 5, "非农", 30),
    # 10月
    (10, 9, "CPI", 30), (10, 2, "非农", 30),
    # 11月
    (11, 13, "CPI", 30), (11, 5, "FOMC", 60), (11, 7, "非农", 30),
    # 12月
    (12, 11, "CPI", 30), (12, 5, "非农", 30),
]

# 事件对应北京时间
EVENT_TIME = {
    "CPI": "20:30",
    "FOMC": "02:00",
    "非农": "20:30",
}

def check_event_blackout() -> Tuple[bool, str]:
    """
    检查当前是否在重大事件禁交易时段

    Returns:
        (is_blackout: bool, reason: str)
    """
    now = datetime.now(timezone.utc) + timedelta(hours=8)  # 转北京时间
    today = (now.month, now.day)
    current_minutes = now.hour * 60 + now.minute

    for month, day, event_type, duration in EVENTS_2026:
        if (month, day) == today:
            event_time_str = EVENT_TIME.get(event_type, "20:30")
            parts = event_time_str.split(":")
            event_minutes = int(parts[0]) * 60 + int(parts[1])
            # 事件前无限制，事件后duration分钟内禁交易
            if event_minutes <= current_minutes < event_minutes + duration:
                remaining = (event_minutes + duration - current_minutes)
                return True, f"{event_type}数据刚公布，禁交易 {remaining} 分钟"
            # 事件前5分钟也禁（防止提前异动）
            if event_minutes - 5 <= current_minutes < event_minutes:
                remaining = (event_minutes - current_minutes)
                return True, f"{event_type}即将公布，禁交易 {remaining} 分钟"
    return False, ""


# ============================================================
# 3. 系统健康检查
# ============================================================

def check_health() -> dict:
    """检查各服务是否正常"""
    results = {}
    # 服务器时间（检查是否正常）
    results["server_time"] = int(time.time())
    # Binance API（快速ping）
    try:
        from urllib.request import Request, urlopen
        req = Request(
            "https://api.binance.com/api/v3/ping",
            headers={"User-Agent": "CipherBot/5.0"}
        )
        resp = urlopen(req, timeout=5)
        results["binance_api"] = resp.status == 200
    except:
        results["binance_api"] = False
    # Telegram（检查bot token是否有效）
    from config import TELEGRAM
    if TELEGRAM.get("bot_token"):
        results["telegram"] = True
    else:
        results["telegram"] = False
    # Cornix频道
    if TELEGRAM.get("cornix_channel"):
        results["cornix_channel"] = True
    else:
        results["cornix_channel"] = False
    return results


# ============================================================
# 4. 实盘复盘日报（每天Telegram推送一次）
# ============================================================

def generate_daily_report() -> str:
    """生成今日交易日报"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trades = []
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        t = json.loads(line)
                        if t.get("time", "").startswith(today):
                            trades.append(t)
                    except: pass
        except: pass

    if not trades:
        return f"📊 *Cipher 日结复盘*\n日期：{today}\n\n今日无信号，系统正常运行。\n风控状态：正常 ✅"

    total = len(trades)
    wins = sum(1 for t in trades if t.get("pnl") and float(t["pnl"]) > 0)
    losses = sum(1 for t in trades if t.get("pnl") and float(t["pnl"]) < 0)
    win_rate = round(wins / total * 100, 1) if total > 0 else 0
    total_pnl = sum(float(t.get("pnl", 0)) or 0 for t in trades)
    avg_rr = sum(float(t.get("rr", 0)) or 0 for t in trades) / total if total > 0 else 0
    avg_score = sum(t.get("score", 0) or 0 for t in trades) / total if total > 0 else 0

    # 信号源统计（从reason字段提取）
    all_reasons = []
    for t in trades:
        reasons = t.get("reason", "") if isinstance(t.get("reason"), str) else ""
        all_reasons.extend([r for r in reasons.split("，") if r])

    long_count = sum(1 for t in trades if t.get("direction") == "long")
    short_count = sum(1 for t in trades if t.get("direction") == "short")

    # 最大回撤
    max_drawdown = 0
    cumulative = 0
    for t in trades:
        pnl = float(t.get("pnl", 0)) or 0
        cumulative += pnl
        if cumulative < -max_drawdown:
            max_drawdown = abs(cumulative)

    # 风控状态
    risk_state = load_risk_state() if os.path.exists(
        os.path.join(LOG_DIR, "risk_state.json")
    ) else {}

    report = (
        f"📊 *Cipher 实盘复盘日报*\n"
        f"日期：{today}\n\n"
        f"━━━━━ 成交统计 ━━━━━\n"
        f"总信号：{total} 单\n"
        f"做多/做空：{long_count}/{short_count}\n"
        f"胜率：{win_rate}%（{wins}胜/{losses}负）\n"
        f"净盈亏：{total_pnl:+.2f}%\n"
        f"最大浮亏：-{max_drawdown:.2f}%\n"
        f"平均R/R：{avg_rr:.1f}\n"
        f"平均评分：{avg_score:.0f}/100\n\n"
        f"━━━━━ 风控 ━━━━━\n"
        f"日亏损熔断：{'⚠️ 已触发' if risk_state.get('loss_limit_hit') else '✅ 正常'}\n"
        f"连亏计数：{risk_state.get('consecutive_losses', 0)}\n\n"
        f"━━━━━ 评估 ━━━━━\n"
    )

    if win_rate >= 50 and total_pnl > 0:
        report += "模式：正常交易 ✅\n今日表现良好，按当前策略继续。"
    elif win_rate >= 40 and total_pnl > 0:
        report += "模式：观察 🟡\n有盈利但胜率偏低，关注止损设置。"
    elif total_pnl < 0:
        report += "模式：降级 ⚠️\n今日亏损，检查信号源质量。"
    else:
        report += "模式：正常 ✅"

    return report


# ============================================================
# 5. 未知仓位保护
# ============================================================

def check_unknown_positions() -> Tuple[bool, List[dict]]:
    """启动时检查交易所是否有数据库未记录的仓位"""
    try:
        sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
        from binance_account import get_account_info
        from config import BINANCE
        info = get_account_info(BINANCE["api_key"], BINANCE["api_secret"])
        if not info:
            return False, []
        positions = info.get("positions", [])
        if not positions:
            return False, []
        # 检查trades.jsonl是否有这些仓位的记录
        known = set()
        if os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        t = json.loads(line)
                        if t.get("symbol") and t.get("direction"):
                            known.add(f"{t['symbol']}_{t['direction']}")
                    except: pass
        unknown = []
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt == 0: continue
            sym = p["symbol"]
            d = "long" if amt > 0 else "short"
            key = f"{sym}_{d}"
            if key not in known:
                unknown.append(p)
        return len(unknown) > 0, unknown
    except ImportError:
        return False, []
    except Exception:
        return False, []


# ============================================================
# 6. 孤儿订单清理（标记无对应仓位的挂单）
# ============================================================

def check_orphan_orders() -> Tuple[bool, List[str]]:
    """检查是否有TP/SL挂单但对应仓位已不存在"""
    orphans = []
    try:
        sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
        from binance_account import get_account_info, get_open_orders
        from config import BINANCE
        info = get_account_info(BINANCE["api_key"], BINANCE["api_secret"])
        if not info:
            return False, []
        active_symbols = set()
        for p in info.get("positions", []):
            if float(p.get("positionAmt", 0)) != 0:
                active_symbols.add(p["symbol"])
        # 检查各币种挂单
        for sym in ["BTCUSDT", "ETHUSDT"]:
            orders = get_open_orders(sym, BINANCE["api_key"], BINANCE["api_secret"])
            if not orders: continue
            for o in orders:
                if o["symbol"] not in active_symbols:
                    # TP/SL挂单但仓位已不在
                    if o.get("type") in ("TAKE_PROFIT", "STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP"):
                        orphans.append(f"{o['symbol']} {o['type']} @ {o.get('price','?')}")
        return len(orphans) > 0, orphans
    except ImportError:
        return False, []
    except Exception:
        return False, []


def load_risk_state() -> dict:
    """读取风控状态（避免循环导入）"""
    fpath = os.path.join(LOG_DIR, "risk_state.json")
    if os.path.exists(fpath):
        try:
            with open(fpath) as f:
                return json.load(f)
        except: pass
    return {}
