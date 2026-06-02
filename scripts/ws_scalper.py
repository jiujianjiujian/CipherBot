# -*- coding: utf-8 -*-
"""
AllTick WebSocket 实时报价 → OFI剥头皮触发器

需要 websocket-client 库: pip install websocket-client

用法:
  ALLTICK_TOKEN=your_token python3 scripts/ws_scalper.py

作用:
  监听BTC实时价格，每变动0.1%触发一次OFI剥头皮检查
  替代1分钟cron轮询（延迟从60秒→0.1秒）
"""
import os, json, sys, time, threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

TOKEN = os.environ.get("ALLTICK_TOKEN", "")

def on_price_change(symbol: str, price: float):
    """价格变动回调 — 触发OFI剥头皮检查"""
    from cipher_bot import analyze_order_flow, get_binance_price, get_klines
    from strategies import scalping_strategy
    from risk_control import RiskEngine
    from cornix_adapter import send_cornix
    from telegram_adapter import send_telegram, format_signal
    from safety import generate_signal_id, is_signal_executed, mark_signal_executed
    from config import TELEGRAM

    # 检查OFI
    ofi = analyze_order_flow(symbol)
    if abs(ofi.get("ofi", 0)) < 0.5:
        return

    k15 = get_klines(symbol, "15m", 14)
    if not k15:
        return

    sig = scalping_strategy(price, ofi, k15, "ws")
    if not sig:
        return

    sig["signal_id"] = generate_signal_id()
    sig["pair"] = symbol.replace("USDT", "")
    sig["symbol"] = symbol

    if is_signal_executed(sig["signal_id"]):
        return
    mark_signal_executed(sig["signal_id"])

    from cipher_bot import log_trade
    log_trade(sig, "sent")
    send_cornix(sig, TELEGRAM)
    print(f"[WS] {datetime.now().strftime('%H:%M:%S')} {sig['direction']} {symbol} @ {price}")


def connect_websocket():
    """连接AllTick WebSocket"""
    if not TOKEN:
        print("请设置 ALLTICK_TOKEN 环境变量")
        return

    try:
        import websocket
    except ImportError:
        print("需要安装: pip install websocket-client")
        return

    url = f"wss://ws.alltick.co?token={TOKEN}"
    last_price = {}

    def on_message(ws, message):
        nonlocal last_price
        try:
            data = json.loads(message)
            if "data" in data:
                for tick in data["data"]:
                    symbol = tick.get("symbol", "")
                    price = float(tick.get("price", 0))
                    if symbol and price > 0:
                        prev = last_price.get(symbol, price)
                        change = abs(price - prev) / prev * 100
                        if change >= 0.1:  # 价格变动0.1%触发
                            last_price[symbol] = price
                            on_price_change(symbol, price)
        except:
            pass

    def on_error(ws, error):
        print(f"[WS] 错误: {error}")

    def on_close(ws, status, msg):
        print(f"[WS] 断开, 5秒后重连...")
        time.sleep(5)
        connect_websocket()

    def on_open(ws):
        print("[WS] 已连接")
        # 订阅BTC和ETH实时报价
        ws.send(json.dumps({
            "cmd": "subscribe",
            "args": ["ticker:BTCUSDT", "ticker:ETHUSDT"]
        }))

    ws = websocket.WebSocketApp(url, on_message=on_message,
                                 on_error=on_error,
                                 on_close=on_close,
                                 on_open=on_open)
    ws.run_forever()


if __name__ == "__main__":
    print("AllTick WebSocket 剥头皮触发器")
    print(f"Token: {TOKEN[:8]}...{TOKEN[-4:]}" if TOKEN else "未设置")
    connect_websocket()
