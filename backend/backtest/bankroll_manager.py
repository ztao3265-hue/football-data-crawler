#!/usr/bin/env python3
"""资金管理器 — Flat Betting + Kelly Criterion"""

import numpy as np
import pandas as pd


class BankrollManager:
    """管理回测资金，支持固定投注和凯利投注。"""

    def __init__(self, initial_capital: float = 10000.0, mode: str = "flat",
                 flat_stake: float = 100.0, max_stake_pct: float = 0.01,
                 kelly_fraction: float = 0.25, min_edge: float = 0.02,
                 max_exposure: float = 0.50):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.peak_capital = initial_capital
        self.mode = mode
        self.flat_stake = flat_stake
        self.max_stake_pct = max_stake_pct
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.max_exposure = max_exposure

        self.history: list[dict] = []
        self.total_bets = 0
        self.total_won = 0
        self.total_return = 0.0

    def reset(self):
        self.capital = self.initial_capital
        self.peak_capital = self.initial_capital
        self.history = []
        self.total_bets = 0
        self.total_won = 0
        self.total_return = 0.0

    def calculate_stake(self, edge: float = 0.0, odds: float = 0.0) -> float:
        """计算单场投注金额。"""
        if self.mode == "flat":
            stake = min(self.flat_stake, self.capital * self.max_stake_pct)
        elif self.mode == "kelly":
            if edge <= self.min_edge or odds <= 0:
                return 0.0
            kelly_pct = self.kelly_fraction * edge / (odds - 1) if odds > 1 else 0
            kelly_pct = min(kelly_pct, self.max_stake_pct)
            stake = self.capital * kelly_pct
        else:
            stake = self.flat_stake
        return max(0.0, min(stake, self.capital * self.max_exposure))

    def place_bet(self, odds: float, result: int, edge: float = 0.0,
                  match_id: str = "", kickoff_time: str = "") -> dict:
        """执行一次投注。

        Args:
            odds: 下注赔率 (水位)
            result: 1=赢, 0=输, -1=无效/走水
            edge: 凯利模式下的预估优势
            match_id: 比赛ID
            kickoff_time: 开球时间

        Returns:
            投注记录 dict
        """
        if result == -1:
            return {
                "stake": 0.0, "return": 0.0, "profit": 0.0,
                "capital_after": self.capital, "result": "push",
                "match_id": match_id, "kickoff_time": kickoff_time,
            }

        stake = self.calculate_stake(edge, odds)
        if stake <= 0:
            return {
                "stake": 0.0, "return": 0.0, "profit": 0.0,
                "capital_after": self.capital, "result": "no_bet",
                "match_id": match_id, "kickoff_time": kickoff_time,
            }

        self.total_bets += 1
        if result == 1:
            profit = stake * (odds - 1) if odds > 0 else 0
            self.total_won += 1
            bet_result = "win"
        else:
            profit = -stake
            bet_result = "lose"

        self.capital += profit
        self.total_return += profit
        self.peak_capital = max(self.peak_capital, self.capital)

        record = {
            "stake": round(stake, 4),
            "return": round(profit, 4),
            "profit": round(profit, 4),
            "capital_after": round(self.capital, 4),
            "result": bet_result,
            "match_id": match_id,
            "kickoff_time": kickoff_time,
            "odds": odds,
            "edge": edge,
        }
        self.history.append(record)
        return record

    def get_stats(self) -> dict:
        """获取当前资金统计。"""
        n = self.total_bets
        win_rate = self.total_won / n if n > 0 else 0
        roi = self.total_return / (n * self.flat_stake) if n > 0 and self.mode == "flat" else (
            (self.capital - self.initial_capital) / self.initial_capital if self.initial_capital > 0 else 0
        )

        drawdown = (self.peak_capital - self.capital) / self.peak_capital if self.peak_capital > 0 else 0

        return {
            "initial_capital": self.initial_capital,
            "current_capital": round(self.capital, 2),
            "peak_capital": round(self.peak_capital, 2),
            "total_bets": n,
            "total_won": self.total_won,
            "win_rate": round(win_rate, 4),
            "total_return": round(self.total_return, 4),
            "roi": round(roi, 4),
            "drawdown_pct": round(drawdown, 4),
            "mode": self.mode,
        }

    def get_equity_curve(self) -> list[float]:
        """获取资金曲线。"""
        curve = [self.initial_capital]
        for h in self.history:
            curve.append(curve[-1] + h["profit"])
        return curve

    def get_drawdown_curve(self) -> list[float]:
        """获取回撤曲线 (百分比)。"""
        curve = self.get_equity_curve()
        peak = np.maximum.accumulate(curve)
        return ((peak - np.array(curve)) / peak).tolist()
