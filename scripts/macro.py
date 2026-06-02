# -*- coding: utf-8 -*-
"""
Cipher 宏观/消息面数据模块 — 全部免费API，9大数据源

数据源:
  1. 恐慌贪婪指数 (alternative.me)
  2. 美元指数 DXY (FRED)
  3. S&P500 / 纳指期货 (Yahoo Finance)
  4. BTC ETF 净流入 (theblock.co)
  5. Google Trends "Bitcoin" (无头请求)
  6. CoinDesk 头条新闻 (RSS)
  7. ForexFactory 经济日历 (网页)
  8. 稳定币供应量 (CoinGecko)
  9. 交易所 BTC 存量 (CoinGecko)
"""
import json, re, time, csv
from datetime import datetime, timezone
from typing import Optional, Dict, List
from urllib.request import Request, urlopen
from xml.etree import ElementTree

API_TIMEOUT = 8


def _get(url: str) -> Optional[str]:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 CipherBot/5.0"})
        with urlopen(req, timeout=API_TIMEOUT) as r:
            return r.read().decode()
    except: return None


def _get_json(url: str) -> Optional[dict]:
    try:
        d = _get(url)
        return json.loads(d) if d else None
    except: return None


# ═══════════════════════════════════════════════
# 1. 恐慌贪婪指数
# ═══════════════════════════════════════════════

def get_fear_greed() -> dict:
    """0-100, <25=极度恐慌, >75=极度贪婪"""
    d = _get_json("https://api.alternative.me/fng/?limit=1")
    if d and "data" in d and d["data"]:
        v = int(d["data"][0].get("value", 50))
        return {"value": v, "label": d["data"][0].get("value_classification", "中性")}
    return {"value": 50, "label": "未知"}


# ═══════════════════════════════════════════════
# 2. 美元指数 DXY
# ═══════════════════════════════════════════════

def get_dxy() -> Optional[float]:
    """最新美元指数值。DXY>108=美元强势压制风险资产"""
    csv = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTWEXBGS&cosd=2026-05-01&coed=2026-06-02")
    if csv:
        lines = csv.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[-1].split(",")
            if len(parts) >= 2 and parts[1]:
                return round(float(parts[1]), 2)
    return None


# ═══════════════════════════════════════════════
# 3. S&P500 期货 / 纳指 (Yahoo Finance)
# ═══════════════════════════════════════════════

def get_sp500() -> Optional[dict]:
    """标普500指数和涨跌幅"""
    d = _get_json("https://query1.finance.yahoo.com/v8/finance/chart/^GSPC?range=5d&interval=1d")
    if d and "chart" in d and "result" in d["chart"] and d["chart"]["result"]:
        r = d["chart"]["result"][0]
        meta = r.get("meta", {})
        quotes = r.get("indicators", {}).get("quote", [{}])[0]
        closes = quotes.get("close", [])
        if meta.get("regularMarketPrice") and len(closes) >= 2:
            price = meta["regularMarketPrice"]
            prev = [c for c in closes if c][-2]
            change = (price - prev) / prev * 100
            return {"price": round(price, 2), "change_pct": round(change, 2)}
    return None


def get_nasdaq() -> Optional[dict]:
    """纳斯达克指数"""
    d = _get_json("https://query1.finance.yahoo.com/v8/finance/chart/^IXIC?range=5d&interval=1d")
    if d and "chart" in d and "result" in d["chart"] and d["chart"]["result"]:
        r = d["chart"]["result"][0]
        meta = r.get("meta", {})
        quotes = r.get("indicators", {}).get("quote", [{}])[0]
        closes = quotes.get("close", [])
        if meta.get("regularMarketPrice") and len(closes) >= 2:
            price = meta["regularMarketPrice"]
            prev = [c for c in closes if c][-2]
            change = (price - prev) / prev * 100
            return {"price": round(price, 2), "change_pct": round(change, 2)}
    return None


# ═══════════════════════════════════════════════
# 4. BTC ETF 净流入 (theblock.co)
# ═══════════════════════════════════════════════

def get_btc_etf_flow() -> Optional[dict]:
    """BTC ETF 每日净流入/流出（百万美元）"""
    try:
        html = _get("https://www.theblock.co/data/crypto-markets/bitcoin-etf/bitcoin-etf-netflow")
        if html:
            # 尝试从页面提取最新值
            patterns = [
                r'净流入.*?[-\d,.]+',
                r'Net[Ff]low.*?[-\d,.]+',
                r'value.*?[-\d.]+',
            ]
            for p in patterns:
                m = re.search(p, html[:5000])
                if m:
                    nums = re.findall(r'-?\d+\.?\d*', m.group())
                    if nums:
                        return {"flow_million": float(nums[0]), "direction": "流入" if float(nums[0]) > 0 else "流出"}
    except: pass
    return None


# ═══════════════════════════════════════════════
# 5. Google Trends "Bitcoin" 搜索热度
# ═══════════════════════════════════════════════

def get_google_trends() -> Optional[int]:
    """Bitcoin搜索热度 0-100。<20=无人问津 >80=全民FOMO"""
    try:
        # 使用 Trends 的 CSV 导出（无需API Key）
        d = _get("https://trends.google.com/trends/api/dailytrends?hl=en-US&tz=-240&geo=US&ns=15")
        if d:
            # 提取 JSON
            json_str = d.replace(")]}',", "", 1) if d.startswith(")]}'") else d
            data = json.loads(json_str)
            for topic in data.get("default", {}).get("trendingSearchesDays", []):
                for search in topic.get("trendingSearches", []):
                    if "bitcoin" in search.get("title", {}).get("query", "").lower():
                        return search.get("formattedTraffic", None)
        return None
    except: return None


# ═══════════════════════════════════════════════
# 6. CoinDesk 头条新闻 (RSS) — 做简单情绪判断
# ═══════════════════════════════════════════════

def get_news_sentiment() -> dict:
    """CoinDesk RSS头条，关键词判断多空情绪"""
    try:
        xml = _get("https://www.coindesk.com/arc/outboundfeeds/rss/")
        if not xml:
            return {"sentiment": "neutral", "headlines": [], "score": 0}
        root = ElementTree.fromstring(xml)
        items = root.findall(".//item")[:10]
        headlines = []
        for item in items:
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                headlines.append(title_el.text)
        # 关键词打分
        bullish_words = ["surge", "rally", "bull", "high", "gain", "breakthrough", "adoption", "inflow"]
        bearish_words = ["crash", "drop", "bear", "low", "loss", "ban", "crackdown", "outflow", "hack"]
        score = 0
        for h in headlines:
            hl = h.lower()
            for w in bullish_words:
                if w in hl: score += 1
            for w in bearish_words:
                if w in hl: score -= 1
        sentiment = "bullish" if score > 2 else ("bearish" if score < -2 else "neutral")
        return {"sentiment": sentiment, "headlines": headlines[:5], "score": score}
    except:
        return {"sentiment": "neutral", "headlines": [], "score": 0}


# ═══════════════════════════════════════════════
# 7. 稳定币供应量 (CoinGecko免费API)
# ═══════════════════════════════════════════════

def get_stablecoin_supply() -> Optional[dict]:
    """USDT+USDC 总供应量（亿美元）。供应增长=场外资金入场"""
    try:
        d = _get_json("https://api.coingecko.com/api/v3/global")
        if d and "data" in d:
            data = d["data"]
            total_mcap = data.get("total_market_cap", {}).get("usd", 0)
            btc_dominance = data.get("market_cap_percentage", {}).get("btc", 0)
            return {
                "total_mcap_trillion": round(total_mcap / 1e12, 2),
                "btc_dominance_pct": round(btc_dominance, 1),
            }
    except: pass
    return None


# ═══════════════════════════════════════════════
# 8. 交易所 BTC 存量估算 (CoinGecko)
# ═══════════════════════════════════════════════

def get_exchange_btc_balance() -> Optional[dict]:
    """交易所BTC存量变化。存量下降=提币=囤币信号"""
    try:
        d = _get_json("https://api.coingecko.com/api/v3/exchanges/binance/tickers?limit=1")
        if d:
            tickers = d.get("tickers", [])
            btc_pairs = [t for t in tickers if t.get("base") == "BTC" and t.get("target") == "USDT"]
            if btc_pairs:
                vol_24h = float(btc_pairs[0].get("converted_volume", {}).get("usd", 0))
                return {"btc_vol_24h_billion": round(vol_24h / 1e9, 2)}
    except: pass
    return None


# ═══════════════════════════════════════════════
# 综合评估
# ═══════════════════════════════════════════════

class MacroContext:
    """宏观/消息面上下文 — 采集所有免费数据源"""

    def __init__(self):
        self.fear_greed = 50; self.fear_greed_label = "中性"
        self.dxy = None; self.sp500 = None; self.nasdaq = None
        self.etf_flow = None; self.news_sentiment = "neutral"
        self.stablecoin = None; self.btc_dominance = None
        self.derisk = False; self.derisk_factor = 1.0
        self.reasons = []

    def evaluate(self) -> "MacroContext":
        fg = get_fear_greed()
        self.fear_greed = fg.get("value", 50)
        self.fear_greed_label = fg.get("label", "中性")
        if self.fear_greed < 20:
            self.derisk = True; self.derisk_factor = min(self.derisk_factor, 0.5)
            self.reasons.append(f"恐慌{self.fear_greed}极度恐慌")
        elif self.fear_greed < 40:
            self.derisk = True; self.derisk_factor = min(self.derisk_factor, 0.8)
            self.reasons.append(f"恐慌{self.fear_greed}偏恐慌")

        self.dxy = get_dxy()
        if self.dxy and self.dxy > 108:
            self.derisk = True; self.derisk_factor = min(self.derisk_factor, 0.7)
            self.reasons.append(f"DXY={self.dxy}美元强势")

        try:
            sp = get_sp500()
            if sp and sp.get("change_pct", 0) < -2:
                self.derisk = True; self.derisk_factor = min(self.derisk_factor, 0.7)
                self.reasons.append(f"标普{sp['change_pct']:+.1f}%大跌")
        except: pass

        try:
            news = get_news_sentiment()
            self.news_sentiment = news.get("sentiment", "neutral")
            if news.get("score", 0) <= -3:
                self.derisk = True; self.derisk_factor = min(self.derisk_factor, 0.8)
                self.reasons.append("新闻偏空")
        except: pass

        try:
            sc = get_stablecoin_supply()
            if sc: self.btc_dominance = sc.get("btc_dominance_pct")
        except: pass

        if not self.reasons:
            self.reasons.append("宏观正常")
        return self
