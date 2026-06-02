# -*- coding: utf-8 -*-
"""
Cipher XGBoost 训练数据采集器
每天运行一次，记录特征+结果，积累数据后用于训练模型

特征:
  - RSI(15m/1h/4h)
  - ATR%
  - EMA斜率
  - FVG存在
  - OI趋势
  - 资金费率
  - OFI
  - 行情模式

标签:
  - 1 = 后续4h价格上涨>0.5%
  - 0 = 后续4h价格横盘
  - -1 = 后续4h价格下跌>0.5%

用法:
  python3 scripts/ml_collector.py          # 采集当日数据
  python3 scripts/ml_collector.py --train   # 数据够1000条后训练模型
"""
import os, json, csv, sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, "logs", "ml_training_data.csv")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cipher_bot import (
    get_binance_price, get_klines, calc_rsi, calc_atr, calc_ema
)
from smc import detect_fvg
from order_flow import analyze as analyze_ofi


FEATURE_HEADERS = [
    "timestamp", "symbol",
    "rsi_15m", "rsi_1h", "rsi_4h",
    "atr_pct_1h",
    "ema_slope_1h",
    "fvg_bullish", "fvg_bearish",
    "ofi", "taker_buy_ratio",
    "vwap_distance", "bb_position", "bb_bandwidth",
    "regime", "quality_score",
    "label_30m",     # 30分钟后涨跌
    "label_60m",     # 60分钟后涨跌
    "label_120m",    # 120分钟后涨跌
    "mfe_pct",       # 最大顺向波动
    "mae_pct",       # 最大逆向波动
    "r_multiple",    # 最终R倍数
    "tp1_first",     # 是否先到TP1 1/0
    "sl_first",      # 是否先到SL 1/0
    "breakeven_hit", # 是否触发保本 1/0
    "net_r",         # 扣除费用后实际R
    "fee_pct",       # 手续费占比%
    "strategy_id",   # 策略ID
]


def collect(symbol: str = "BTCUSDT") -> dict:
    """采集一条特征数据（增强版：含VWAP/布林带/模式/评分）"""
    price = get_binance_price(symbol)
    k15 = get_klines(symbol, "15m", 50)
    k1h = get_klines(symbol, "1h", 50)
    k4h = get_klines(symbol, "4h", 30)

    if not all([price, k15, k1h, k4h]):
        return {}

    c15 = [k["close"] for k in k15]
    c1h = [k["close"] for k in k1h]
    c4h = [k["close"] for k in k4h]

    fvgs = detect_fvg(k15)
    ofi = analyze_ofi(symbol)

    # EMA斜率
    ema21_1h = calc_ema(c1h, 21)
    ema21_1h_prev = calc_ema(c1h[:-5], 21) if len(c1h) > 25 else ema21_1h
    ema_slope = (ema21_1h - ema21_1h_prev) / ema21_1h_prev * 100 if ema21_1h_prev else 0

    # VWAP和布林带
    from indicators import calc_vwap, calc_bollinger_bands
    vwap = calc_vwap(k1h) if len(k1h) >= 10 else calc_vwap(k15)
    bb = calc_bollinger_bands(k1h, 20, 2.0) if len(k1h) >= 20 else {}
    vwap_dist = (price - vwap) / vwap * 100 if vwap > 0 else 0
    bb_pos = bb.get("position", 50)
    bb_bw = bb.get("bandwidth", 0)

    # 行情模式评分
    try:
        from market_regime import classify_regime, get_regime_params
        reg = classify_regime(k4h)
        reg_label = get_regime_params(reg).get("label", "?")
    except:
        reg_label = "?"

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol": symbol,
        "rsi_15m": round(calc_rsi(c15, 14), 1),
        "rsi_1h": round(calc_rsi(c1h, 14), 1),
        "rsi_4h": round(calc_rsi(c4h, 14), 1),
        "atr_pct_1h": round(calc_atr(k1h, 14) / price * 100, 2),
        "ema_slope_1h": round(ema_slope, 4),
        "fvg_bullish": 1 if any(f["type"] == "bullish" for f in fvgs) else 0,
        "fvg_bearish": 1 if any(f["type"] == "bearish" for f in fvgs) else 0,
        "ofi": ofi.get("ofi", 0),
        "taker_buy_ratio": ofi.get("taker_buy_ratio", 50),
        "vwap_distance": round(vwap_dist, 2),
        "bb_position": bb_pos,
        "bb_bandwidth": bb_bw,
        "regime": reg_label,
        "quality_score": 5.0,
        "label_30m": "", "label_60m": "", "label_120m": "",
        "mfe_pct": "", "mae_pct": "", "r_multiple": "",
        "price": price,
    }


def append_to_csv(row: dict):
    """追加特征数据到CSV"""
    file_exists = os.path.exists(DATA_FILE)
    with open(DATA_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
    print(f"✅ 数据已保存 ({os.path.getsize(DATA_FILE)//1024}KB)")


def label_data():
    """
    对已有数据打标签：用后续4h价格变化标记
    需要采集时记录了price字段
    """
    if not os.path.exists(DATA_FILE):
        print("❌ 无数据文件")
        return

    rows = []
    with open(DATA_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if len(rows) < 2:
        print(f"⏸️ 仅{len(rows)}条，需要至少2条")
        return

    # 多时间维度标签：30min / 60min / 120min
    labeled = 0
    for i in range(len(rows)):
        if rows[i].get("label_30m") and rows[i]["label_30m"] != "":
            continue
        curr_price = float(rows[i].get("price", 0))
        if curr_price <= 0: continue
        curr_time = datetime.fromisoformat(rows[i]["timestamp"])
        for target_min, col in [(30, "label_30m"), (60, "label_60m"), (120, "label_120m")]:
            for j in range(i + 1, min(i + 50, len(rows))):
                nxt = rows[j]
                if not nxt.get("timestamp"): continue
                try:
                    nxt_time = datetime.fromisoformat(nxt["timestamp"])
                except: continue
                if (nxt_time - curr_time).total_seconds() >= target_min * 60:
                    nxt_price = float(nxt.get("price", 0))
                    if nxt_price > 0:
                        change = (nxt_price - curr_price) / curr_price * 100
                        rows[i][col] = "1" if change > 0.3 else ("-1" if change < -0.3 else "0")
                        break
        labeled += 1

    if labeled > 0:
        with open(DATA_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FEATURE_HEADERS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"✅ 已标记 {labeled} 条数据")
    else:
        print("⏸️ 无需标记")


def train():
    """训练XGBoost模型（数据够1000条时调用）"""
    try:
        import xgboost as xgb
        import pandas as pd
        from sklearn.model_selection import train_test_split
    except ImportError:
        print("❌ 需要安装: pip install xgboost pandas scikit-learn")
        return

    if not os.path.exists(DATA_FILE):
        print("❌ 无数据文件，请先采集")
        return

    df = pd.read_csv(DATA_FILE)
    df = df.dropna(subset=["label"])

    if len(df) < 100:
        print(f"⏸️ 数据不足 ({len(df)}条)，需要至少100条")
        return

    features = ["rsi_15m", "rsi_1h", "rsi_4h", "atr_pct_1h",
                "ema_slope_1h", "fvg_bullish", "fvg_bearish", "ofi", "taker_buy_ratio"]
    X = df[features]
    y = df["label"].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)

    model = xgb.XGBClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42,
    )
    model.fit(X_train, y_train)

    acc = model.score(X_test, y_test)
    print(f"✅ 模型训练完成! 准确率: {acc:.1%}")

    # 保存模型
    model_path = os.path.join(BASE_DIR, "scripts", "ml_model.json")
    model.save_model(model_path)
    print(f"✅ 模型已保存: {model_path}")

    # 特征重要性
    importances = sorted(zip(features, model.feature_importances_), key=lambda x: -x[1])
    print("\n📊 特征重要性:")
    for name, imp in importances:
        print(f"  {name}: {imp:.1%}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if mode == "collect":
        row = collect()
        if row:
            append_to_csv(row)
            label_data()
    elif mode == "train":
        train()
    elif mode == "label":
        label_data()
    else:
        print(f"用法: {sys.argv[0]} [collect|train|label]")
