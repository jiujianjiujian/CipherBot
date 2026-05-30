# CipherBot — BTC 超短线自动交易系统

> 基于技术指标共振 + 严格风控的比特币自动交易机器人。
> 由 AI 分析引擎驱动，实现「止损小、盈利大、稳定盈利」的交易策略。

## 功能

- ⏱ **每5分钟扫描** — 自动获取 BTC 实时行情，多周期分析
- 📊 **多指标共振** — RSI、ATR、EMA、成交量、市场结构综合判断
- 🎯 **信号评分系统** — 1-10分评分，低于6分不交易
- 🛡️ **ATR 动态止损** — 根据波动率自适应止损宽度
- 📲 **Telegram 通知** — 开单信号实时推送到手机
- 🤖 **3Commas 自动下单** — 发现机会自动执行 25x 合约交易
- 📈 **4小时总结** — 技术指标汇总推送
- 📉 **每日复盘** — 日终回顾，持续优化策略

## 策略核心

```
入场条件（全部满足才开单）：
✅ 价格在关键支撑/阻力位附近（距日高低点 <1.5%）
✅ ATR 动态止损 < 1.2%（根据波动率自适应）
✅ R/R 盈亏比 ≥ 2:1
✅ RSI 不处于极端区（不追涨杀跌）
✅ 多周期趋势一致
✅ 信号评分 ≥ 6/10

不交易的情况：
❌ 价格在中间位置震荡
❌ RSI 超买/超卖极端
❌ 信号评分不足
❌ 形态不清晰
❌ 逆大趋势
```

## 安装部署

```bash
# 1. SSH 到服务器
ssh root@YOUR_SERVER_IP

# 2. 克隆项目
git clone https://github.com/YOUR_GITHUB/cipher-bot.git /root/CipherBot
cd /root/CipherBot

# 3. 运行测试
python3 scripts/cipher_bot.py scan

# 4. 设置定时任务（自动）
# */5 * * * * cd /root/CipherBot && python3 scripts/cipher_bot.py scan >> logs/cron.log
# 0 */4 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py summary >> logs/cron.log
# 0 23 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py review >> logs/cron.log
```

## 文件结构

```
CipherBot/
├── README.md              # 项目说明
├── .gitignore             # Git 忽略规则
├── requirements.txt       # Python 依赖
├── deploy.sh              # 一键部署脚本
├── scripts/
│   ├── config.py          # 配置文件（API Key等）
│   └── cipher_bot.py      # 主程序（分析+交易引擎）
└── logs/                  # 运行日志
```

## 风险提示

⚠️ **合约交易有极高风险，可能导致全部本金损失。**
- 本系统仅供学习和研究参考
- 不构成任何投资建议
- 使用前请充分了解合约交易风险
- DYOR（自行研究）

## License

MIT
