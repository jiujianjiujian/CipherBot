#!/bin/bash
# CipherBot VPS 部署脚本
# 在 VPS 上运行: bash deploy.sh

set -e

PROJECT_DIR="/root/CipherBot"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
LOGS_DIR="$PROJECT_DIR/logs"

echo "=========================================="
echo "  Cipher BTC 自动交易系统 - VPS 部署"
echo "=========================================="

# 1. 创建目录
echo "[1/4] 创建项目目录..."
mkdir -p $SCRIPTS_DIR $LOGS_DIR

# 2. 安装 Python 依赖
echo "[2/4] 安装 Python 依赖..."
pip3 install -q requests 2>/dev/null || apt-get install -y python3-pip >/dev/null 2>&1

# 3. 设置定时任务
echo "[3/4] 设置定时任务..."

# 删除旧的 CipherBot 相关 cron
crontab -l 2>/dev/null | grep -v "CipherBot" | grep -v "cipher_bot" | crontab -

# 添加新 cron
cat >> /tmp/cipher_cron << 'CRON'
# CipherBot 定时任务
*/5 * * * * cd /root/CipherBot && python3 scripts/cipher_bot.py scan >> logs/cron_scan.log 2>&1
0 */4 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py summary >> logs/cron_summary.log 2>&1
0 23 * * * cd /root/CipherBot && python3 scripts/cipher_bot.py review >> logs/cron_review.log 2>&1
CRON

crontab /tmp/cipher_cron 2>/dev/null; crontab -l 2>/dev/null | cat - /tmp/cipher_cron | crontab -
rm /tmp/cipher_cron

# 4. 验证安装
echo "[4/4] 验证安装..."
echo "项目目录: $PROJECT_DIR"
echo "脚本目录: $SCRIPTS_DIR"
echo "日志目录: $LOGS_DIR"
echo ""
echo "定时任务:"
crontab -l | grep -E "CipherBot|cipher_bot" || echo "  (无)"

echo ""
echo "=========================================="
echo "  ✅ CipherBot 部署完成！"
echo "  日志: $LOGS_DIR/cipher.log"
echo "  查看日志: tail -f $LOGS_DIR/cipher.log"
echo "=========================================="
