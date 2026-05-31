"""
持仓监控器 — 只读模式
查询币安合约持仓状态，推送异常告警到Telegram。
不执行任何交易操作—所有交易走3Commas。

用法:
  python3 scripts/trailing_manager.py

安全原则:
  - 币安API仅用于读取持仓和盈亏数据（只读权限即可）
  - 所有交易操作由3Commas Signal Bot执行
  - Telegram仅用于查询，不执行交易
"""
import sys
import os
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BINANCE
from binance_account import get_account_info, format_positions
from cipher_bot import calc_atr, get_klines, logger, send_telegram


def check_positions():
    """读取持仓状态并返回摘要"""
    info = get_account_info(BINANCE.get("api_key", ""), BINANCE.get("api_secret", ""))
    if not info:
        logger.warning("持仓查询失败（API无权限或未配置）")
        return

    positions = info.get("positions", [])
    if not positions:
        logger.info("当前无持仓")
        return

    # 逐仓检查盈亏状态
    alerts = []
    for pos in positions:
        symbol = pos["symbol"]
        entry = pos["entry"]
        mark = pos["mark"]
        side = "多头" if pos["amount"] > 0 else "空头"
        pnl_pct = (mark - entry) / entry * 100 if pos["amount"] > 0 else (entry - mark) / entry * 100

        # 获取当前ATR用于报告
        k15 = get_klines(symbol, "15m", 20)
        atr = calc_atr(k15, 14) if k15 else 0

        logger.info(f"  {symbol} {side} | 入场${entry:.0f} 当前${mark:.0f} | 盈亏{pnl_pct:+.2f}% | ATR=${atr:.1f}")

        # 当盈亏超过一定比例时发送告警（不执行任何交易操作）
        if pnl_pct > 3.0:
            alerts.append(f"⚠️ {symbol} {side} 浮盈+{pnl_pct:.1f}%")
        elif pnl_pct < -1.0:
            alerts.append(f"⚠️ {symbol} {side} 浮亏{pnl_pct:.1f}%")

    if alerts:
        send_telegram("📊 *持仓告警*\n" + "\n".join(alerts))


def main():
    logger.info("=" * 40)
    logger.info("持仓监控检查")
    check_positions()


if __name__ == "__main__":
    main()
