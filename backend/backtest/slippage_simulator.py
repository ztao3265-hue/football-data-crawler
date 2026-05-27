#!/usr/bin/env python3
"""滑点模拟器 — 模拟真实赔率损耗"""

import numpy as np
import pandas as pd


class SlippageSimulator:
    """模拟交易滑点对收益的影响。

    在实际投注中，我们拿到的赔率通常略低于最优赔率。
    滑点模拟器在不同赔率折扣下评估策略稳健性。
    """

    def __init__(self, levels: list[float] = None, default_level: float = 0.02):
        self.levels = levels or [0.0, 0.01, 0.02, 0.03]
        self.default_level = default_level

    def apply_slippage(self, odds: float, level: float = None) -> float:
        """对赔率施加滑点。

        Args:
            odds: 原始赔率 (水位)
            level: 滑点级别 (0.02 = 2% 折扣)

        Returns:
            调整后的赔率
        """
        if level is None:
            level = self.default_level
        if pd.isna(odds) or odds <= 0:
            return odds
        return max(0.01, odds * (1 - level))

    def simulate(self, bets_df: pd.DataFrame, level: float = None) -> pd.DataFrame:
        """对一组投注记录批量施加滑点。

        Args:
            bets_df: 含 'odds' 和 'result' 列的 DataFrame
            level: 滑点级别

        Returns:
            添加了 'odds_slipped' 和 'profit_slipped' 列的 DataFrame
        """
        if level is None:
            level = self.default_level

        result = bets_df.copy()
        result["odds_slipped"] = result["odds"].apply(lambda o: self.apply_slippage(o, level))
        result["profit_slipped"] = result.apply(
            lambda r: (r["odds_slipped"] - 1) if r["result"] == 1
            else (-1.0 if r["result"] == 0 else 0.0),
            axis=1,
        )
        return result

    def run_multi_level(self, bets_df: pd.DataFrame) -> dict:
        """在所有滑点级别下运行模拟，返回各级别指标。

        Returns:
            {level: {"roi": float, "total_profit": float, ...}}
        """
        results = {}
        for level in self.levels:
            simulated = self.simulate(bets_df, level)
            total_profit = simulated["profit_slipped"].sum()
            n = len(simulated)
            results[str(level)] = {
                "level": level,
                "level_pct": f"{level*100:.0f}%",
                "total_profit": round(float(total_profit), 4),
                "roi": round(float(total_profit / n), 4) if n > 0 else 0,
                "avg_odds_original": round(float(bets_df["odds"].mean()), 4),
                "avg_odds_slipped": round(float(simulated["odds_slipped"].mean()), 4),
                "n_bets": n,
            }
        return results

    def find_breakeven_slippage(self, bets_df: pd.DataFrame, precision: float = 0.001) -> float:
        """二分搜索找到 ROI 归零的滑点级别 (盈亏平衡点)。"""
        lo, hi = 0.0, 0.50
        for _ in range(30):
            mid = (lo + hi) / 2
            sim = self.simulate(bets_df, mid)
            roi = sim["profit_slipped"].sum() / len(sim) if len(sim) > 0 else 0
            if roi > 0:
                lo = mid
            else:
                hi = mid
        return round(lo, 4)
