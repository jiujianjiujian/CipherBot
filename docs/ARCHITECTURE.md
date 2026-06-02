# CipherBot v5.0.0 架构文档

## 文件结构

```
scripts/
├── cipher_bot.py      # 主入口：信号引擎 + Telegram + Cornix
├── config.py           # 配置（密钥优先环境变量）
├── config.example.py   # 配置示例

├── indicators.py       # 技术指标（RSI/ATR/EMA/VWAP/布林带）
├── smc.py              # Smart Money Concepts（FVG检测）
├── vrvp.py             # 成交量分布分析（POC/价值区）
├── order_flow.py       # 订单流分析（OFI/吃单方向/CVD）

├── market_context.py   # 市场背景过滤（波动率/趋势/RSI/流动性）
├── market_regime.py    # 行情模式分类（4种模式+参数表）
├── signal_voter.py     # 多信号投票引擎（6源加权）

├── risk_control.py     # 风控引擎（日亏损/连亏/杠杆/强平/价差）
├── safety.py           # 安全模块（signal_id/禁交易/健康检查/日报）
├── macro.py            # 宏观数据（恐慌指数/DXY/标普/新闻）

├── validator.py        # 验证器（防幻觉/数值修正）
├── ml_collector.py     # XGBoost训练数据采集
├── bot_listener.py     # Telegram命令监听器
├── binance_account.py  # 币安账户查询
├── backtest.py         # 回测引擎
├── trailing_manager.py # 趋势延续加仓
├── walk_forward.py     # Walk-Forward稳定性验证
├── check_integrity.py  # 完整性检查

logs/
├── cipher.log          # 主日志
├── cron_scan.log       # cron扫描日志
├── trades.jsonl        # 交易记录
├── risk_state.json     # 风控状态
├── executed_signal_ids.json  # 已执行信号ID
└── ml_training_data.csv      # ML训练数据

docs/
└── ARCHITECTURE.md     # 本文件
```

## 数据流

```
Binance API → run_scan()
                │
                ├── 市场背景分析（波动率/趋势/RSI/流动性/宏观）
                ├── 风控检查（日亏损/连亏熔断）
                ├── 禁交易时段检查（CPI/FOMC/非农）
                ├── 系统健康检查（Binance/Telegram）
                │
                ├── 并发获取各币种K线
                │
                └── 逐个币种分析
                     │
                     ├── find_trading_signal()
                     │   ├── 计算指标（RSI/ATR/EMA/VWAP/布林带）
                     │   ├── 检测FVG
                     │   ├── 做多/做空候选评估
                     │   ├── 方向过滤（下降趋势不做多）
                     │   └── 评分+仓位计算
                     │
                     ├── 多信号投票（6源加权）
                     ├── 风控终审（杠杆/强平/真实R）
                     ├── 防重复执行（signal_id）
                     └── 发送（Telegram通知 + Cornix执行）
```

## 关键配置

### 环境变量（优先于硬编码）
```bash
export BINANCE_API_KEY=xxx
export BINANCE_API_SECRET=xxx
export CIPHER_TG_TOKEN=xxx
```

### 风控参数（config.py）
```
max_daily_loss_pct: 3.0     # 单日亏损3%熔断
max_consecutive_losses: 3   # 连亏3单停机4小时
leverage_cap_normal: 15x    # 普通信号≤15x
leverage_cap_strong: 25x    # A+级信号≤25x
real_rr_min: 2.0            # 扣除费用后R/R≥2
```

## 修改注意事项

1. 改风控参数 → `config.py` 或 `risk_control.py` DEFAULT
2. 改信号逻辑 → `cipher_bot.py` 的 `find_trading_signal()`
3. 改评分权重 → `config.py` SCORING
4. 改行情模式 → `market_regime.py` REGIME_PARAMS
5. 改投票权重 → `signal_voter.py` evaluate_vote()
6. 新增数据源 → `macro.py` + 在 `MacroContext.evaluate()` 中调用
