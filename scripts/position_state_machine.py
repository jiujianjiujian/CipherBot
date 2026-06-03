# -*- coding: utf-8 -*-
"""
仓位状态机 — 每笔交易的完整生命周期管理

状态流转:
  SIGNAL_CREATED → SENT_TO_CORNIX → ENTRY_PENDING → OPEN → TP1_HIT → BREAKEVEN_SET
  → TRAILING_ACTIVE / ADD_POSITION_ALLOWED → CLOSED / FAILED / MANUAL_REQUIRED

核心规则:
  - 同一 signal_id 不得重复执行
  - TP1 未触发不得加仓
  - 未推保本不得加仓
  - 已关闭订单不得再次操作
  - 异常状态禁止新开仓
"""
import os, json, time
from datetime import datetime
from typing import Optional, List, Tuple

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "trade_state.json"
)

VALID_TRANSITIONS = {
    "SIGNAL_CREATED": ["SENT_TO_CORNIX", "FAILED"],
    "SENT_TO_CORNIX": ["ENTRY_PENDING", "PARTIALLY_FILLED", "FAILED"],
    "ENTRY_PENDING": ["OPEN", "PARTIALLY_FILLED", "FAILED"],
    "PARTIALLY_FILLED": ["OPEN", "FAILED"],
    "OPEN": ["TP1_HIT", "REDUCING", "CLOSED", "FAILED"],
    "TP1_HIT": ["BREAKEVEN_SET", "ADD_POSITION_ALLOWED", "TRAILING_ACTIVE", "CLOSED"],
    "BREAKEVEN_SET": ["TRAILING_ACTIVE", "ADD_POSITION_ALLOWED", "CLOSED"],
    "TRAILING_ACTIVE": ["ADD_POSITION_ALLOWED", "CLOSED", "REDUCING"],
    "ADD_POSITION_ALLOWED": ["ADD_POSITION_SENT", "CLOSED"],
    "ADD_POSITION_SENT": ["OPEN", "FAILED"],
    "REDUCING": ["CLOSED", "FAILED"],
    "FAILED": [],
    "MANUAL_REQUIRED": ["CLOSED"],
    "CLOSED": [],
}

class PositionStateMachine:
    def __init__(self):
        self._load()

    def _load(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    self.positions = json.load(f)
            except:
                self.positions = {}
        else:
            self.positions = {}

    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.positions, f, indent=2)

    def create(self, signal_id: str, symbol: str, direction: str, entry: float,
               stop: float, target: float, leverage: int, strategy: str) -> bool:
        if signal_id in self.positions:
            return False
        self.positions[signal_id] = {
            "signal_id": signal_id, "symbol": symbol, "direction": direction,
            "entry": entry, "stop_loss": stop, "target": target,
            "leverage": leverage, "strategy": strategy,
            "state": "SIGNAL_CREATED", "history": [],
            "created_at": datetime.now().isoformat(),
            "updates": 0, "add_count": 0,
        }
        self._add_history(signal_id, "SIGNAL_CREATED")
        self._save()
        return True

    def transition(self, signal_id: str, to_state: str) -> Tuple[bool, str]:
        pos = self.positions.get(signal_id)
        if not pos:
            return False, "signal_id不存在"
        if to_state not in VALID_TRANSITIONS.get(pos["state"], []):
            return False, f"不允许从{pos['state']}转换到{to_state}"
        pos["state"] = to_state
        pos["updates"] = pos.get("updates", 0) + 1
        self._add_history(signal_id, to_state)
        self._save()
        return True, ""

    def _add_history(self, signal_id: str, state: str):
        if signal_id in self.positions:
            self.positions[signal_id]["history"].append({
                "state": state, "time": datetime.now().isoformat(),
            })

    def get(self, signal_id: str) -> Optional[dict]:
        return self.positions.get(signal_id)

    def get_active(self, symbol: str = None) -> List[dict]:
        active = []
        for p in self.positions.values():
            if p["state"] not in ("CLOSED", "FAILED"):
                if symbol is None or p["symbol"] == symbol:
                    active.append(p)
        return active

    def can_add_position(self, signal_id: str) -> Tuple[bool, str]:
        pos = self.positions.get(signal_id)
        if not pos:
            return False, "signal_id不存在"
        if pos["state"] not in ("TP1_HIT", "BREAKEVEN_SET", "TRAILING_ACTIVE", "ADD_POSITION_ALLOWED"):
            return False, f"当前状态{pos['state']}不允许加仓"
        if pos.get("add_count", 0) >= 2:
            return False, "已达最大加仓次数(2次)"
        return True, ""

    def mark_added(self, signal_id: str) -> bool:
        pos = self.positions.get(signal_id)
        if not pos:
            return False
        pos["add_count"] = pos.get("add_count", 0) + 1
        self._add_history(signal_id, f"ADD_{pos['add_count']}")
        self._save()
        return True

    def can_open_new(self, symbol: str, direction: str) -> Tuple[bool, str]:
        active = self.get_active(symbol)
        for p in active:
            if p["direction"] == direction:
                return False, f"已有同向活跃仓位({p['state']})"
            if p["state"] not in ("CLOSED", "FAILED"):
                return False, f"已有{ p['direction'] }仓位，不能反向开"
        return True, ""

    def has_unknown_positions(self) -> List[dict]:
        """检查Binance有但本地无记录的仓位"""
        try:
            from binance_reconciler import get_real_positions
            real = get_real_positions()
            unknown = []
            for r in real:
                found = False
                for p in self.positions.values():
                    if p["symbol"] == r["symbol"] and p["state"] not in ("CLOSED", "FAILED"):
                        found = True
                        break
                if not found:
                    unknown.append(r)
            return unknown
        except:
            return []
