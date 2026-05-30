#!/usr/bin/env python3
"""
Telegram Bot 命令监听器 — 常驻服务
让 @LobsterSignalBot 支持命令交互
运行方式: python3 bot_listener.py
"""
import json
import logging
import sys
import os
import time
from datetime import datetime
from urllib.request import Request, urlopen

# 添加脚本目录到路径并加载模块
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from config import TELEGRAM, TRADING, BINANCE

# 预加载 cipher_bot 模块
from cipher_bot import get_binance_price, get_24h_ticker, get_klines
from cipher_bot import calc_rsi, calc_atr, calc_ema, detect_market_structure

from binance_account import format_positions, format_orders

BASE_DIR = os.path.dirname(SCRIPTS_DIR)
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "bot_listener.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BotListener")

TOKEN = TELEGRAM["bot_token"]
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
LAST_UPDATE_ID = 0
CHAT_ID_FILE = os.path.join(BASE_DIR, ".chat_id")

# ============================================================
def api_get(url: str, timeout: int = 10):
    try:
        req = Request(url, headers={"User-Agent": "CipherBot/3.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"GET失败: {e}")
        return None

def api_post(url: str, data: dict, timeout: int = 10):
    try:
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "CipherBot/3.0"
        })
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"POST失败: {e}")
        return None

def reply(chat_id: int, text: str):
    api_post(f"{BASE_URL}/sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

# ============================================================
# 命令处理
# ============================================================
def cmd_price():
    price = get_binance_price()
    ticker = get_24h_ticker()
    if not price:
        return "❌ 无法获取价格"
    if ticker:
        return (
            f"📊 *BTC 实时行情*\n"
            f"当前：${price:,.2f}\n"
            f"24h最高：${ticker['high']:,.0f}\n"
            f"24h最低：${ticker['low']:,.0f}\n"
            f"24h涨跌：{ticker['change_pct']:+.2f}%\n"
            f"更新：{datetime.now().strftime('%H:%M:%S')}"
        )
    return f"📊 BTC：${price:,.2f}"

def cmd_status():
    uptime = "N/A"
    try:
        with open("/proc/uptime") as f:
            s = float(f.read().split()[0])
            uptime = f"{int(s//3600)}h{int((s%3600)//60)}m"
    except Exception as _:
        logger.warning(f"读取失败: {_}")

    # 账户权益
    account_info = ""
    try:
        acc = format_positions(BINANCE["api_key"], BINANCE["api_secret"])
        for line in acc.split("\n")[:3]:
            account_info += line + "\n"
    except:
        account_info = "  余额：查询中...\n"

    return (
        f"🤖 *CipherBot 状态*\n"
        f"运行：{uptime}\n"
        f"交易对：{TRADING['symbol']} | 杠杆：{TRADING['max_leverage']}x\n"
        f"{account_info}\n"
        f"📌 /price /status /scan /analysis /positions /orders /help"
    )

def cmd_scan():
    try:
        import subprocess
        r = subprocess.run(
            ["python3", os.path.join(SCRIPTS_DIR, "cipher_bot.py"), "scan"],
            capture_output=True, text=True, timeout=30, cwd=BASE_DIR
        )
        lines = r.stdout.strip().split("\n")
        info = [l for l in lines if any(k in l for k in ["BTC", "RSI", "结构", "信号", "Cipher", "EMA"])]
        return "🔍 *扫描完成*\n" + "\n".join(info[-5:])[:2000] if info else "🔍 扫描完成，无信号"
    except Exception as e:
        return f"❌ 扫描失败: {e}"

def cmd_analysis():
    price = get_binance_price()
    ticker = get_24h_ticker()
    k4h = get_klines("4h", 24)
    if not price or not ticker:
        return "❌ 无法获取行情"
    if not k4h:
        return f"📊 BTC：${price:,.2f}（{ticker['change_pct']:+.2f}%）"

    closes = [k["close"] for k in k4h]
    rsi_4h = calc_rsi(closes, 14)
    atr_4h = calc_atr(k4h, 14)
    ema21 = calc_ema(closes, 21)
    struct = detect_market_structure(k4h)
    sr_high = max(k["high"] for k in k4h)
    sr_low = min(k["low"] for k in k4h)

    rsi_label = "🔥超买" if rsi_4h > 65 else "🧊超卖" if rsi_4h < 35 else "⚖️中性"
    trend_label = {"uptrend": "上行📈", "downtrend": "下行📉", "ranging": "震荡⏸️"}.get(struct["structure"], "❓")

    return (
        f"📊 *BTC行情简报*\n"
        f"━━━━━━━━━━━━━\n"
        f"价格：${price:,.0f}（{ticker['change_pct']:+.2f}%）\n"
        f"RSI(4H)：{rsi_4h:.0f} {rsi_label}\n"
        f"ATR：${atr_4h:.0f} | EMA21：${ema21:,.0f}\n"
        f"趋势：{trend_label}\n\n"
        f"阻力 ${sr_high:,.0f} | 支撑 ${sr_low:,.0f}\n"
        f"━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now().strftime('%m-%d %H:%M')}"
    )

def cmd_positions():
    if not BINANCE.get("api_key"):
        return "❌ 未配置币安API Key\n请在币安后台创建只读API Key后发给我配置"
    try:
        return format_positions(BINANCE["api_key"], BINANCE["api_secret"])
    except Exception as e:
        return f"❌ 查询失败: {e}"

def cmd_orders():
    if not BINANCE.get("api_key"):
        return "❌ 未配置币安API Key"
    try:
        return format_orders(BINANCE["api_key"], BINANCE["api_secret"])
    except Exception as e:
        return f"❌ 查询失败: {e}"

def cmd_help():
    return (
        f"📚 *CipherBot 命令*\n\n"
        f"`/price` — BTC实时价格\n"
        f"`/status` — 系统状态+账户权益\n"
        f"`/scan` — 手动扫描一次\n"
        f"`/analysis` — 行情简报\n"
        f"`/positions` — 查看持仓\n"
        f"`/orders` — 查看挂单\n"
        f"`/help` — 帮助\n\n"
        f"⚡ 交易信号推送：自动"
    )

# ============================================================
# 命令路由
# ============================================================
COMMANDS = {
    "/price": cmd_price, "/start": cmd_help,
    "/status": cmd_status, "/help": cmd_help,
    "/scan": cmd_scan,
    "/analysis": cmd_analysis,
    "/positions": cmd_positions, "/position": cmd_positions,
    "/orders": cmd_orders, "/order": cmd_orders,
}

def process_update(update: dict):
    global LAST_UPDATE_ID
    uid = update.get("update_id")
    if uid and uid <= LAST_UPDATE_ID:
        return
    LAST_UPDATE_ID = uid

    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip().lower()

    if not chat_id or not text:
        return

    # 持久化 chat_id
    with open(CHAT_ID_FILE, "w") as f:
        f.write(str(chat_id))

    logger.info(f"收到: {text}")

    try:
        # 精确匹配命令
        handler = COMMANDS.get(text) or COMMANDS.get(text.split()[0])
        if handler:
            result = handler()
            reply(chat_id, result)
        elif text.startswith("/"):
            reply(chat_id, f"❓ 未知命令 `{text}`\n/help 查看可用命令")
        else:
            reply(chat_id, "🤖 你好！我是 CipherBot\n/help 查看命令")
    except Exception as e:
        logger.error(f"处理失败: {e}")
        reply(chat_id, f"❌ 命令执行出错，已记录日志")

def main():
    global LAST_UPDATE_ID
    offset_file = os.path.join(BASE_DIR, ".update_offset")
    if os.path.exists(offset_file):
        try:
            LAST_UPDATE_ID = int(open(offset_file).read().strip())
        except Exception as _:
            logger.warning(f"读取失败: {_}")

    logger.info("🚀 CipherBot 命令监听器已启动 (进程PID: %d)", os.getpid())
    logger.info(f"Bot: @LobsterSignalBot")

    while True:
        try:
            url = f"{BASE_URL}/getUpdates?timeout=30&offset={LAST_UPDATE_ID + 1}"
            result = api_get(url, timeout=35)
            if result and result.get("ok"):
                for update in result.get("result", []):
                    process_update(update)
                with open(offset_file, "w") as f:
                    f.write(str(LAST_UPDATE_ID))
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
