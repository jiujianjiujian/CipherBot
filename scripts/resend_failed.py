#!/usr/bin/env python3
"""
CipherBot Webhook 补发器
从 VPS 读取失败信号并重发
用法: python3 scripts/resend_failed.py
"""
import sys, os, json, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import THREE_COMMAS
from urllib.request import Request, urlopen

VPS_SSH = "root@43.108.48.96"
VPS_FAILED_DIR = "/root/CipherBot/logs/failed_webhooks/"
KEY = "C:\\Users\\Administrator\\.ssh\\vps_key"

def resend():
    # Get failed files from VPS
    import subprocess
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-i", KEY, VPS_SSH, f"ls -t {VPS_FAILED_DIR} 2>/dev/null || echo 'EMPTY'"],
        capture_output=True, text=True, timeout=15
    )
    files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip() and f != "EMPTY"]
    if not files:
        print("✅ 没有失败的 Webhook")
        return

    print(f"📋 发现 {len(files)} 个失败信号:")
    for fname in files:
        # Get file content
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-i", KEY, VPS_SSH, f"cat {VPS_FAILED_DIR}/{fname}"],
            capture_output=True, text=True, timeout=10
        )
        try:
            data = json.loads(r.stdout)
            payload = data.get("payload", {})
            sig = data.get("signal", {})
            print(f"\n  {fname}")
            print(f"  方向: {sig.get('direction','?')} 入场: ${sig.get('entry','?')} 仓位: {sig.get('amount_pct','?')}%")
            print(f"  止损: ${sig.get('stop_loss','?')} 目标: ${sig.get('target','?')} R/R: {sig.get('rr','?')}")
        except:
            print(f"  {fname}: 解析失败")

    # Ask to resend all
    confirm = input(f"\n🔄 补发全部 {len(files)} 个? (y/n): ")
    if confirm.lower() != "y":
        print("已取消")
        return

    for fname in files:
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-i", KEY, VPS_SSH, f"cat {VPS_FAILED_DIR}/{fname}"],
            capture_output=True, text=True, timeout=10
        )
        try:
            data = json.loads(r.stdout)
            payload = data.get("payload", {})
            body = json.dumps(payload).encode()
            req = Request(THREE_COMMAS["webhook_url"], data=body,
                         headers={"Content-Type": "application/json"})
            resp = urlopen(req, timeout=15)
            status = resp.status
            if status == 200:
                print(f"  ✅ {fname}: 补发成功")
                subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", "-i", KEY, VPS_SSH,
                              f"rm {VPS_FAILED_DIR}/{fname}"], capture_output=True, timeout=5)
            else:
                print(f"  ❌ {fname}: HTTP {status}")
        except Exception as e:
            print(f"  ❌ {fname}: {e}")

if __name__ == "__main__":
    resend()
