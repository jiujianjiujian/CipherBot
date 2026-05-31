"""
CipherBot 配置示例
复制为 config.py 并填入真实值

密钥写死本地，不对外暴露。
"""
import os

PAIRS = {
    "BTCUSDT": {"enabled": True, "name": "BTC", "leverage": 25, "max_stop_pct": 1.2, "order_type": "limit"},
    "ETHUSDT": {"enabled": True, "name": "ETH", "leverage": 15, "max_stop_pct": 1.5, "order_type": "limit", "min_score": 70},
}
TRADING = {"min_rr_ratio": 2.0, "order_type": "limit",
    "score_strong": 80, "score_good": 60, "score_decent": 40,
    "size_strong_max": 60, "size_strong_min": 50, "size_good_max": 40, "size_good_min": 30, "size_decent_max": 20, "size_decent_min": 15,
    "trend_aligned_mult": 1.3, "trend_against_mult": 0.7}
TELEGRAM = {"bot_token": "你的Telegram Bot Token", "chat_id": None, "cornix_channel": "你的频道ID"}
SCORING = {"timeframe_alignment": 15, "price_structure": 25, "volume_verification": 20, "candle_pattern": 15, "risk_reward": 15, "momentum": 10}
TRADE_LOG_FILE = ""
ANALYSIS = {"short_term_candles": 20, "short_term_interval": "15m", "medium_candles": 12, "medium_interval": "1h", "long_term_candles": 24, "long_term_interval": "4h", "daily_candles": 7, "daily_interval": "1d"}
BINANCE = {"api_key": "", "api_secret": ""}
