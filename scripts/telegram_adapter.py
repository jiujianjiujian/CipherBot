# -*- coding: utf-8 -*-
"""
Telegram 消息适配器 — 信号格式化 + 消息推送 + Cornix频道
"""
import os, json, logging
from datetime import datetime, timezone
from urllib.request import Request, urlopen

logger = logging.getLogger("Cipher")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT_ID_FILE = os.path.join(BASE_DIR, ".chat_id")

API_TIMEOUT_SLOW = 15


def _api_get(url: str):
    try:
        req = Request(url, headers={"User-Agent": "CipherBot/5.0"})
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except: return None


def _api_post(url: str, data: dict):
    try:
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "CipherBot/5.0"})
        with urlopen(req, timeout=API_TIMEOUT_SLOW) as r:
            return json.loads(r.read().decode())
    except: return None


def _get_chat_id(telegram_config: dict) -> int:
    """获取持久化的chat_id"""
    if telegram_config.get("chat_id"):
        return telegram_config["chat_id"]
    fpath = CHAT_ID_FILE
    if os.path.exists(fpath):
        try: return int(open(fpath).read().strip())
        except: pass
    # 首次获取
    updates = _api_get(f"https://api.telegram.org/bot{telegram_config['bot_token']}/getUpdates")
    if updates and updates.get("result"):
        for u in updates["result"]:
            cid = u.get("message", {}).get("chat", {}).get("id")
            if cid:
                telegram_config["chat_id"] = cid
                with open(fpath, "w") as f: f.write(str(cid))
                return cid
    return 0


def send_telegram(message: str, telegram_config: dict) -> bool:
    """发送消息到Telegram"""
    token = telegram_config["bot_token"]
    chat_id = _get_chat_id(telegram_config)
    if not chat_id:
        return False
    result = _api_post(f"https://api.telegram.org/bot{token}/sendMessage",
                        {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"})
    return result is not None


def format_signal(signal: dict) -> str:
    """格式化信号为Telegram消息"""
    pair = signal.get("pair", "BTC")
    entry = signal.get("entry", 0)
    stop = signal.get("stop_loss", 0)
    target = signal.get("target", 0)
    stop_pct = signal.get("stop_pct", 0)
    rr = signal.get("rr", 0)
    score = signal.get("score", 0)
    quality = signal.get("quality_score", 5.5)

    dir_emoji = "🟢" if signal["direction"] == "long" else "🔴"
    dir_cn = "做多" if signal["direction"] == "long" else "做空"

    if score >= 80: level = "🔥 强信号"
    elif score >= 60: level = "✅ 好信号"
    elif score >= 40: level = "⚠️ 一般信号"
    else: level = "❌ 弱信号"

    if quality >= 7: rec_note = "行情优质"
    elif quality >= 5: rec_note = "行情中性"
    else: rec_note = "行情偏差，轻仓"

    key_level = signal.get("key_level", entry)
    entry_min = signal.get("entry_min", entry * 0.997)
    entry_max = signal.get("entry_max", entry * 1.003)
    reg = signal.get("regime", "?")

    if signal["direction"] == "long":
        condition = (
            f"✅ 入场：${entry_min:.0f} - ${entry_max:.0f}\n"
            f"🛑 止损：${stop:.0f}（{stop_pct:.2f}%）\n"
            f"🎯 目标：${target:.0f}（+{signal.get('target_pct',0):.2f}%）\n"
            f"📊 R/R：{rr}:1\n"
            f"📈 模式：{reg}"
        )
        plan = (f"方案A：回踩${entry_min:.0f}附近轻仓试多，止损${stop:.0f}，目标${target:.0f}\n"
                f"方案B：15m收盘站上${entry_max:.0f}确认后入场\n⏱️ 持有1-4h")
    else:
        condition = (
            f"✅ 入场：${entry_min:.0f} - ${entry_max:.0f}\n"
            f"🛑 止损：${stop:.0f}（{stop_pct:.2f}%）\n"
            f"🎯 目标：${target:.0f}（-{signal.get('target_pct',0):.2f}%）\n"
            f"📊 R/R：{rr}:1\n"
            f"📉 模式：{reg}"
        )
        plan = (f"方案A：反弹${entry_min:.0f}附近轻仓试空，止损${stop:.0f}，目标${target:.0f}\n"
                f"方案B：15m收盘跌破${entry_max:.0f}确认后入场\n⏱️ 持有1-4h")

    reasons = signal.get("reasons", [])
    dr = [r for r in reasons if not r.startswith("6维度评分")]
    reason_str = "\n".join(f"• {r}" for r in dr[:4])
    risk_str = "\n".join(f"⚠️ {r}" for r in signal.get("risks", [])[:3]) if signal.get("risks") else ""

    return (
        f"📊 *Cipher {pair}* {dir_emoji}\n"
        f"> {dir_cn} | {level} | 行情感知 {quality}/10\n\n"
        f"━━━━━ 入场方案 ━━━━━\n{condition}\n\n"
        f"━━━━━ 计划 ━━━━━\n{plan}\n\n"
        f"━━━━━ 关键位 ━━━━━\n"
        f"📌 压力区 ${target:.0f}\n📌 关键位 ${key_level:.0f}\n📌 支撑区 ${stop:.0f}\n\n"
        f"{reason_str}\n{('风险：'+risk_str) if risk_str else ''}"
    )
