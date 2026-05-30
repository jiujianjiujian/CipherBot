"""
CipherBot 配置示例
复制为 config.py 并填入真实值
"""
THREE_COMMAS = {
    "webhook_url": "https://api.3commas.io/signal_bots/webhooks",
    "secret": "你的3Commas Secret",
    "bot_uuid": "你的Bot UUID",
    "leverage": 25,
    "amount_percent": 25,
}
TELEGRAM = {
    "bot_token": "你的Telegram Bot Token",
    "chat_id": None,
}
TRADING = {
    "symbol": "BTCUSDT",
    "max_stop_loss_pct": 1.2,
    "min_rr_ratio": 2.0,
    "max_leverage": 25,
    "amount_percent": 25,
    "order_type": "limit",
    "trailing_stop": False,
    "partial_exit_50pct": False,
}
ANALYSIS = {
    "short_term_candles": 30,
    "short_term_interval": "15m",
    "medium_candles": 30,
    "medium_interval": "1h",
    "long_term_candles": 24,
    "long_term_interval": "4h",
    "daily_candles": 7,
    "daily_interval": "1d",
}
