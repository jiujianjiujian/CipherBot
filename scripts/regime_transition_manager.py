# -*- coding: utf-8 -*-
"""
行情模式切换锁 — 防止15种模式频繁跳变导致策略乱开

规则:
  - CHOPPY / FAST_PUMP / FAST_DUMP: 立即生效
  - BULL_CASCADE / BEAR_CASCADE: 连续2根15m确认
  - SLOW_BULL / SLOW_BEAR: 持续30分钟确认
  - RANGE_WIDE: POC稳定后确认
  - VOLATILE: 立即生效
"""
import os, json, time
from datetime import datetime
from typing import Optional, Tuple

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "current_regime.json"
)

IMMEDIATE_MODES = ["CHOPPY", "FAST_PUMP_CONTINUATION", "FAST_PUMP_EXHAUSTION",
                   "FAST_DUMP_CONTINUATION", "FAST_DUMP_EXHAUSTION", "VOLATILE"]
CONFIRM_2BAR_MODES = ["BULL_CASCADE", "BEAR_CASCADE"]
CONFIRM_30MIN_MODES = ["SLOW_BULL", "SLOW_BEAR"]
CONFIRM_POC_MODES = ["RANGE_WIDE", "RANGE_NARROW"]


class RegimeTransitionManager:
    def __init__(self):
        self._load()

    def _load(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                self.current = data.get("regime", "UNKNOWN")
                self.previous = data.get("previous", "UNKNOWN")
                self.confirmed_since = data.get("confirmed_since", 0)
                self.pending_regime = data.get("pending_regime", "")
                self.pending_since = data.get("pending_since", 0)
            except:
                self._reset()
        else:
            self._reset()

    def _reset(self):
        self.current = "UNKNOWN"
        self.previous = "UNKNOWN"
        self.confirmed_since = 0
        self.pending_regime = ""
        self.pending_since = 0

    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump({
                "regime": self.current, "previous": self.previous,
                "confirmed_since": self.confirmed_since,
                "pending_regime": self.pending_regime,
                "pending_since": self.pending_since,
            }, f, indent=2)

    def update(self, new_regime: str) -> Tuple[str, bool]:
        """
        尝试更新行情模式

        Args:
            new_regime: 检测到的新模式

        Returns:
            (actual_regime: str, changed: bool)
            实际的模式（可能未切换），是否刚完成切换
        """
        if new_regime == self.current:
            self.pending_regime = ""
            self.pending_since = 0
            self._save()
            return self.current, False

        now = time.time()

        # 立即生效模式
        if new_regime in IMMEDIATE_MODES:
            changed = new_regime != self.current
            self.previous = self.current
            self.current = new_regime
            self.confirmed_since = now
            self.pending_regime = ""
            self.pending_since = 0
            self._save()
            return new_regime, changed

        # 需要2根15m确认的模式
        if new_regime in CONFIRM_2BAR_MODES:
            if new_regime == self.pending_regime:
                if now - self.pending_since >= 120:  # 2根15m = 2分钟
                    changed = new_regime != self.current
                    self.previous = self.current
                    self.current = new_regime
                    self.confirmed_since = now
                    self.pending_regime = ""
                    self.pending_since = 0
                    self._save()
                    return new_regime, changed
            else:
                self.pending_regime = new_regime
                self.pending_since = now
                self._save()
            return self.current, False

        # 需要30分钟确认的模式
        if new_regime in CONFIRM_30MIN_MODES:
            if new_regime == self.pending_regime:
                if now - self.pending_since >= 1800:
                    changed = new_regime != self.current
                    self.previous = self.current
                    self.current = new_regime
                    self.confirmed_since = now
                    self.pending_regime = ""
                    self.pending_since = 0
                    self._save()
                    return new_regime, changed
            else:
                self.pending_regime = new_regime
                self.pending_since = now
                self._save()
            return self.current, False

        # 默认立即切换
        changed = new_regime != self.current
        self.previous = self.current
        self.current = new_regime
        self.confirmed_since = now
        self._save()
        return new_regime, changed
