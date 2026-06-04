# -*- coding: utf-8 -*-
"""
Cornix 信号适配器 — 策略自适应格式转换 + 推送
"""
import logging
import json
import os, sys
from urllib.request import Request, urlopen

# 读取交易所配置（config.py中的EXCHANGE变量）
_EXCHANGE = "Binance Futures"
try:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
    from config import EXCHANGE
    _EXCHANGE = EXCHANGE
except:
    pass

logger = logging.getLogger("Cipher")
_EXCHANGE = "Binance Futures"


def _fmt_price_cornix(p: float) -> str:
    """智能格式化价格"""
    if p >= 1000: return f"{p:.0f}"
    elif p >= 10: return f"{p:.3f}"
    elif p >= 1: return f"{p:.4f}"
    else: return f"{p:.5f}"


def send_cornix(signal: dict, telegram_config: dict) -> bool:
    """Cornix 标准信号 — 策略自适应TP/SL/杠杆/追踪"""
    channel = telegram_config.get("cornix_channel", "")
    if not channel:
        return False
    direction = "Long" if signal["direction"] == "long" else "Short"
    pair = "#" + signal.get("symbol", "BTCUSDT").replace("USDT", "/USDT")
    entry = signal.get("entry", 0)
    stop = signal.get("stop_loss", 0)
    pattern = signal.get("pattern", "")
    score = signal.get("score", 60)
    is_eth = "ETH" in pair

    # 策略参数表
    if "剥头皮" in pattern or "scalp" in pattern.lower():
        lev = 30 if score >= 75 else (25 if score >= 60 else 20)
        tp1_pct = 0.60 if is_eth else 0.55
        sl_pct = 0.30 if is_eth else 0.25
        tp_count = 1; trail_type = "Breakeven"
    elif "震荡" in pattern or "range" in pattern.lower():
        lev = 25 if is_eth else 30
        tp1_pct = 0.65 if is_eth else 0.55
        sl_pct = 0.40 if is_eth else 0.35
        tp_count = 2; trail_type = "Breakeven"
    elif "趋势" in pattern or "trend" in pattern.lower():
        lev = 25 if score >= 80 else 20
        tp1_pct = 0.85 if is_eth else 0.75
        sl_pct = 0.50 if is_eth else 0.40
        tp_count = 1; trail_type = "Percent Below Highest"
    elif "突破" in pattern or "breakout" in pattern.lower():
        lev = 25
        tp1_pct = 0.75 if is_eth else 0.70
        sl_pct = 0.45 if is_eth else 0.35
        tp_count = 1; trail_type = "Percent Below Highest"
    else:
        lev = 28; tp1_pct = 0.60; sl_pct = 0.30; tp_count = 1; trail_type = "Breakeven"

    lev = min(lev, signal.get("leverage", 25))
    fmt = _fmt_price_cornix
    ep = entry * 0.0008
    entry_min, entry_max = entry - ep, entry + ep

    if signal["direction"] == "long":
        tp1 = entry + entry * tp1_pct / 100
        tp2 = entry + entry * tp1_pct * 2 / 100 if tp_count >= 2 else 0
        sl_price = min(stop, entry - entry * sl_pct / 100)
    else:
        tp1 = entry - entry * tp1_pct / 100
        tp2 = entry - entry * tp1_pct * 2 / 100 if tp_count >= 2 else 0
        sl_price = max(stop, entry + entry * sl_pct / 100)

    sig_type = "Breakout" if "突破" in pattern else "Regular"
    trail_dist = "0.35" if is_eth else "0.30"

    lines = [f"{pair}", "", f"Exchanges: {_EXCHANGE}",
             f"Signal Type: {sig_type} ({direction})",
             f"Leverage: Isolated ({lev}X)", "",
             "Entry Zone:", f"{fmt(entry_min)} - {fmt(entry_max)}", "",
             "Take-Profit Targets:", f"1) {fmt(tp1)}"]
    if tp_count >= 2:
        lines.append(f"2) {fmt(tp2)}")
    lines += ["", "Stop Targets:", f"1) {fmt(sl_price)}", "",
              "Trailing Configuration:", f"Stop: {trail_type}", "Trigger: Target (1)"]
    if trail_type == "Percent Below Highest":
        lines.append(f"Trailing Distance: {trail_dist}%")

    try:
        body = json.dumps({"chat_id": channel, "text": chr(10).join(lines)}).encode()
        req = Request(f"https://api.telegram.org/bot{telegram_config['bot_token']}/sendMessage",
                      data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=15):
            logger.info(f"✅ Cornix: {direction} {pair} @ {fmt(entry)} ({lev}X)")
            return True
    except Exception as e:
        logger.warning(f"Cornix发送失败: {e}")
        return False
