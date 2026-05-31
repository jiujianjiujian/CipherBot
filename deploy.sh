#!/bin/bash
# CipherBot v4 VPS 部署脚本
# 从 GitHub 拉取后运行: bash deploy.sh

set -e

PROJECT_DIR="/root/CipherBot"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
LOGS_DIR="$PROJECT_DIR/logs"

echo "=========================================="
echo "  CipherBot v4 - VPS 部署"
echo "=========================================="

echo "[1/5] 创建目录..."
mkdir -p $SCRIPTS_DIR $LOGS_DIR

echo "[2/5] 安装 Python..."
pip3 install -q requests 2>/dev/null || apt-get install -y -qq python3-pip >/dev/null 2>&1

echo "[3/5] 设置定时任务..."
crontab -l 2>/dev/null | grep -v "CipherBot\|cipher_bot\|trailing_manager" | crontab -
cat >> /tmp/cipher_cron << 'CRON'
# CipherBot v4 定时任务
# 日间(7:00-23:59) 5分钟扫描 / 夜间(0:00-6:59) 30分钟省资源
*/5 7-23 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py scan >> logs/cron_scan.log 2>&1
*/30 0-6 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py scan >> logs/cron_scan.log 2>&1
0 */4 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py summary >> logs/cron_summary.log 2>&1
*/15 * * * * cd /root/CipherBot && python3 scripts/trailing_manager.py >> logs/cron_trailing.log 2>&1
0 23 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py review >> logs/cron_review.log 2>&1
CRON
crontab /tmp/cipher_cron 2>/dev/null; crontab -l 2>/dev/null | cat - /tmp/cipher_cron | crontab -
rm /tmp/cipher_cron

echo "[4/5] 安装 systemd 服务..."
cp -n scripts/cipherbot.service /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
systemctl enable cipherbot.service 2>/dev/null || true
systemctl restart cipherbot.service 2>/dev/null || echo "  (cipherbot.service需手动配置环境变量后启动)"

echo "[5/5] 验证..."
echo ""
echo "=== 项目文件 ==="
ls -la $SCRIPTS_DIR/*.py 2>/dev/null | awk '{print "  " $NF}'
echo ""
echo "=== 定时任务 ==="
crontab -l | grep -E "CipherBot|cipher_bot|trailing" || echo "  (无)"
echo ""
echo "=== 服务状态 ==="
systemctl is-active cipherbot.service 2>/dev/null || echo "  (未配置)"

echo ""
echo "=========================================="
echo "  ✅ CipherBot v4 部署完成！"
echo "  文件清单:"
echo "    cipher_bot.py      - 主分析引擎"
echo "    bot_listener.py    - TG命令监听器"
echo "    binance_account.py - 币安账户查询"
echo "    trailing_manager.py- 持仓监控告警"
echo "    validator.py       - 信号验证器"
echo "    backtest.py        - 回测模块"
echo "    walk_forward.py    - 前向优化"
echo "    check_integrity.py - 完整性检查"
echo "=========================================="
