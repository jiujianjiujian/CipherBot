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
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import TELEGRAM, TRADING

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
        logger.debug(f"API GET 失败: {e}")
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
        logger.debug(f"API POST 失败: {e}")
        return None

def reply(chat_id: int, text: str):
    api_post(f"{BASE_URL}/sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    })

# ============================================================
# 命令处理
# ============================================================
def get_price_text() -> str:
    from binance import get_binance_price, get_24h_ticker
    price = get_binance_price()
    ticker = get_24h_ticker()
    if not price:
        return "❌ 无法获取当前价格"
    if ticker:
        return (
            f"📊 *BTC 实时行情*\n"
            f"当前价格：${price:,.2f}\n"
            f"24h最高：${ticker['high']:,.0f}\n"
            f"24h最低：${ticker['low']:,.0f}\n"
            f"24h涨跌：{ticker['change_pct']:+.2f}%\n"
            f"更新时间：{datetime.now().strftime('%H:%M:%S')}"
        )
    return f"📊 BTC 当前价格：${price:,.2f}"

def get_status_text() -> str:
    uptime = "N/A"
    try:
        with open("/proc/uptime") as f:
            up_seconds = float(f.read().split()[0])
            hours = int(up_seconds // 3600)
            mins = int((up_seconds % 3600) // 60)
            uptime = f"{hours}h{mins}m"
    except:
        pass

    # 检查最近扫描状态
    last_log = "暂无记录"
    log_file = os.path.join(LOG_DIR, "cron.log")
    if os.path.exists(log_file):
        try:
            lines = open(log_file).read().strip().split("\n")
            last_lines = [l for l in lines if "无优质" in l or "信号" in l or "BTC" in l]
            if last_lines:
                last_log = last_lines[-1][:80]
        except:
            pass

    return (
        f"🤖 *CipherBot 系统状态*\n"
        f"运行时长：{uptime}\n"
        f"BTC 交易对：{TRADING['symbol']}\n"
        f"杠杆：{TRADING['max_leverage']}x\n"
        f"最近记录：{last_log}\n\n"
        f"📌 *命令列表*\n"
        f"/price — 查询BTC价格\n"
        f"/status — 系统状态\n"
        f"/scan — 手动扫描\n"
        f"/analysis — 最新分析\n"
        f"/help — 帮助"
    )

def get_scan_text() -> str:
    """手动触发扫描"""
    try:
        import subprocess
        result = subprocess.run(
            ["python3", os.path.join(BASE_DIR, "scripts", "cipher_bot.py"), "scan"],
            capture_output=True, text=True, timeout=30,
            cwd=BASE_DIR
        )
        # 提取关键信息
        lines = result.stdout.strip().split("\n")
        info_lines = [l for l in lines if "BTC" in l or "信号" in l or "RSI" in l or "结构" in l or "Cipher" in l]
        summary = "\n".join(info_lines[-5:]) if info_lines else "扫描完成，无信号"
        return f"🔍 *扫描结果*\n```\n{summary}\n```"
    except Exception as e:
        return f"❌ 扫描失败: {e}"

def get_help_text() -> str:
    return (
        f"📚 *CipherBot 命令帮助*\n\n"
        f"`/price` — 查询 BTC 实时价格和24h涨跌\n"
        f"`/status` — 系统运行状态和最近扫描记录\n"
        f"`/scan` — 立即执行一次超短线扫描分析\n"
        f"`/analysis` — 获取最新行情简报\n"
        f"`/help` — 显示此帮助\n\n"
        f"⚠️ 自动交易信号会主动推送到此聊天，无需手动查询"
    )

def get_analysis_text() -> str:
    """获取最新分析简报"""
    from binance import get_binance_price, get_24h_ticker, get_klines
    from cipher_bot import calc_rsi, calc_atr, calc_ema, detect_market_structure

    price = get_binance_price()
    ticker = get_24h_ticker()
    k4h = get_klines("4h", 24)

    if not price or not ticker:
        return "❌ 无法获取行情数据"

    if k4h:
        closes = [k["close"] for k in k4h]
        rsi_4h = calc_rsi(closes, 14)
        atr_4h = calc_atr(k4h, 14)
        ema21 = calc_ema(closes, 21)
        struct = detect_market_structure(k4h)
        sr_high = max(k["high"] for k in k4h)
        sr_low = min(k["low"] for k in k4h)

        return (
            f"📊 *BTC 行情简报*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"当前：${price:,.0f}（{ticker['change_pct']:+.2f}%）\n\n"
            f"技术指标：\n"
            f"RSI(4H)：{rsi_4h:.0f} {'🔥' if rsi_4h>65 else '🧊' if rsi_4h<35 else '⚖️'}\n"
            f"ATR：${atr_4h:.0f}\n"
            f"EMA21：${ema21:,.0f}\n"
            f"趋势：{struct['structure']}\n\n"
            f"关键位：\n"
            f"阻力 ${sr_high:,.0f} / 支撑 ${sr_low:,.0f}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%m-%d %H:%M')}"
        )

    return f"📊 BTC 当前价格：${price:,.2f}（{ticker['change_pct']:+.2f}%）"

# ============================================================
# 动态导入 binance 函数（避免循环导入）
# ============================================================
def lazy_import():
    global binance, cipher_bot
    import importlib
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    binance = importlib.import_module("cipher_bot")
    cipher_bot = binance
    from cipher_bot import get_binance_price, get_24h_ticker, get_klines
    from cipher_bot import calc_rsi, calc_atr, calc_ema, detect_market_structure
    globals().update(locals())

lazy_import()

# ============================================================
# 主循环
# ============================================================
def process_update(update: dict):
    """处理单条 Telegram 更新"""
    global LAST_UPDATE_ID

    update_id = update.get("update_id")
    if update_id and update_id <= LAST_UPDATE_ID:
        return
    LAST_UPDATE_ID = update_id

    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip().lower()

    if not chat_id or not text:
        return

    # 保存 chat_id
    with open(CHAT_ID_FILE, "w") as f:
        f.write(str(chat_id))

    logger.info(f"收到消息: {text} (chat_id={chat_id})")

    if text == "/price" or text == "price":
        reply(chat_id, get_price_text())
    elif text == "/status" or text == "status":
        reply(chat_id, get_status_text())
    elif text == "/scan" or text == "scan":
        reply(chat_id, "🔍 正在扫描，请稍候...")
        reply(chat_id, get_scan_text())
    elif text == "/analysis" or text == "analysis":
        reply(chat_id, get_analysis_text())
    elif text == "/help" or text == "help" or text == "/start":
        reply(chat_id, get_help_text())
    elif text.startswith("/"):
        reply(chat_id, f"❓ 未知命令 `{text}`\n输入 /help 查看可用命令")
    else:
        reply(chat_id, f"🤖 你好！我是 CipherBot\n可用命令：\n/price  /status  /scan  /analysis  /help")

def main():
    global LAST_UPDATE_ID

    # 恢复上次处理的 update_id
    offset_file = os.path.join(BASE_DIR, ".update_offset")
    if os.path.exists(offset_file):
        try:
            LAST_UPDATE_ID = int(open(offset_file).read().strip())
        except:
            pass

    logger.info("🚀 CipherBot 命令监听器已启动")
    logger.info(f"Bot: @LobsterSignalBot")

    while True:
        try:
            url = f"{BASE_URL}/getUpdates?timeout=30&offset={LAST_UPDATE_ID + 1}"
            result = api_get(url, timeout=35)

            if result and result.get("ok"):
                for update in result.get("result", []):
                    process_update(update)

                # 保存 offset
                with open(offset_file, "w") as f:
                    f.write(str(LAST_UPDATE_ID))

        except KeyboardInterrupt:
            logger.info("收到退出信号")
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
