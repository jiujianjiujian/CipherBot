# -*- coding: utf-8 -*-
"""
Binance 手续费/滑点模型 — 动态计算真实交易成本

手续费公式: Fee = Notional × FeeRate
  Notional = 保证金 × 杠杆
  Maker: 0.02% (挂单成交)
  Taker: 0.05% (吃单成交)
  BNB抵扣: 可享75折 (需开通)

用法:
  from fee_model import estimate_cost
  cost = estimate_cost(entry=70000, stop=69500, target=71000,
                        leverage=25, margin=200, is_maker=True)
"""
from typing import Tuple


# Binance U本位永续默认费率（LV1）
MAKER_RATE = 0.0002  # 0.02%
TAKER_RATE = 0.0005  # 0.05%
BNB_DISCOUNT = 0.75  # BNB抵扣后为原费率75%


def get_rates(vip_level: int = 0, use_bnb: bool = False) -> Tuple[float, float]:
    """
    获取实际手续费率

    Args:
        vip_level: Binance VIP等级 (0-9)
        use_bnb: 是否开启BNB抵扣

    Returns:
        (maker_rate, taker_rate)
    """
    # VIP等级费率表 (maker/taker)
    vip_rates = [
        (0.0002, 0.0005),   # LV0
        (0.00018, 0.00045), # LV1
        (0.00016, 0.00040), # LV2
        (0.00014, 0.00035), # LV3
        (0.00012, 0.00032), # LV4
        (0.00010, 0.00030), # LV5
        (0.00008, 0.00027), # LV6
        (0.00006, 0.00025), # LV7
        (0.00004, 0.00022), # LV8
        (0.00002, 0.00020), # LV9
    ]
    level = min(vip_level, 9)
    maker, taker = vip_rates[level]
    if use_bnb:
        maker *= BNB_DISCOUNT
        taker *= BNB_DISCOUNT
    return maker, taker


def estimate_cost(entry: float, stop: float, target: float,
                  leverage: int, margin: float,
                  is_maker_entry: bool = True,
                  is_maker_exit: bool = True,
                  vip_level: int = 0,
                  use_bnb: bool = False) -> dict:
    """
    预估一笔交易的真实成本

    Args:
        entry: 入场价
        stop: 止损价
        target: 止盈价
        leverage: 杠杆倍数
        margin: 使用保证金
        is_maker_entry: 入场是否maker
        is_maker_exit: 出场是否maker
        vip_level: VIP等级
        use_bnb: 是否BNB抵扣

    Returns:
        {
            notional: 名义仓位
            entry_fee: 入场手续费
            exit_fee: 出场手续费 (按target算)
            total_fee: 总手续费
            slippage: 预估滑点
            total_cost: 总成本(手续费+滑点)
            gross_pnl: 毛利润
            net_pnl: 净利润
            net_pnl_pct: 净利润占保证金%
            fee_pct_of_notional: 手续费占名义仓位%
            maker_rate: 实际maker费率
            taker_rate: 实际taker费率
        }
    """
    notional = margin * leverage
    maker, taker = get_rates(vip_level, use_bnb)

    entry_rate = maker if is_maker_entry else taker
    exit_rate = maker if is_maker_exit else taker

    entry_fee = notional * entry_rate
    exit_fee = notional * exit_rate
    total_fee = entry_fee + exit_fee

    # 滑点估算: 基于成交方式和策略类型
    slippage_rate = 0.0003 if (is_maker_entry and is_maker_exit) else 0.0005
    slippage = notional * slippage_rate

    total_cost = total_fee + slippage

    # 价格波动损益
    if entry > 0 and stop > 0 and target > 0:
        if target > entry:  # long
            gross_pnl = (target - entry) / entry * notional
        else:  # short
            gross_pnl = (entry - target) / entry * notional
    else:
        gross_pnl = 0

    net_pnl = gross_pnl - total_cost

    return {
        "notional": round(notional, 2),
        "entry_fee": round(entry_fee, 4),
        "exit_fee": round(exit_fee, 4),
        "total_fee": round(total_fee, 4),
        "slippage": round(slippage, 4),
        "total_cost": round(total_cost, 4),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "net_pnl_pct": round(net_pnl / margin * 100, 2) if margin > 0 else 0,
        "maker_rate": maker,
        "taker_rate": taker,
    }
