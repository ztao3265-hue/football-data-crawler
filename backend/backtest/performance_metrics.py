#!/usr/bin/env python3
"""综合回测指标计算器 — ROI, Sharpe, MaxDD, Profit Factor, Streaks 等"""

import numpy as np
import pandas as pd
from scipy import stats


class PerformanceMetrics:
    """计算全套回测性能指标。

    支持单组或多组投注记录，输出 ROI、夏普比率、最大回撤、
    盈亏比、连胜/连败、月度/年度收益、波动率等。
    """

    def __init__(self, risk_free_rate: float = 0.0):
        self.risk_free_rate = risk_free_rate

    def compute(self, returns: np.ndarray, stakes: np.ndarray = None,
                dates: pd.Series = None, equity_curve: np.ndarray = None) -> dict:
        """计算所有指标。

        Args:
            returns: 每笔投注的收益数组 (profit)
            stakes: 每笔投注的金额数组 (默认全1)
            dates: 每笔投注的日期 (用于月度/年度统计)
            equity_curve: 资金曲线数组 (默认从 returns 计算)

        Returns:
            指标字典
        """
        n = len(returns)
        if n == 0:
            return {"error": "无投注记录", "total_bets": 0}

        if stakes is None:
            stakes = np.ones(n)
        if equity_curve is None:
            equity_curve = np.cumsum(returns)

        total_stake = stakes.sum()
        total_return = returns.sum()
        wins = returns > 0
        losses = returns < 0
        pushes = returns == 0

        n_wins = wins.sum()
        n_losses = losses.sum()
        n_pushes = pushes.sum()
        n_resolved = n_wins + n_losses

        # 基础指标
        win_rate = n_wins / n_resolved if n_resolved > 0 else 0.0
        roi = total_return / total_stake if total_stake > 0 else 0.0
        avg_win = returns[wins].mean() if n_wins > 0 else 0.0
        avg_loss = abs(returns[losses].mean()) if n_losses > 0 else 0.0

        # 盈亏比 (Profit Factor)
        gross_profit = returns[wins].sum() if n_wins > 0 else 0.0
        gross_loss = abs(returns[losses].sum()) if n_losses > 0 else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # 最大回撤
        peak = np.maximum.accumulate(equity_curve)
        drawdowns = peak - equity_curve
        max_dd = float(drawdowns.max())
        max_dd_pct = float((drawdowns / peak).max()) if len(peak) > 0 and peak[0] > 0 else 0.0

        # 夏普比率 (年化)
        sharpe = self._sharpe_ratio(returns)

        # Sortino 比率 (只用下行波动)
        sortino = self._sortino_ratio(returns)

        # Calmar 比率
        calmar = roi / max_dd_pct if max_dd_pct > 0 else 0.0

        # 波动率 (年化, 以 unit stake 为单位)
        volatility = float(np.std(returns / stakes, ddof=1)) if n > 1 else 0.0

        # 连胜/连败
        max_win_streak = int(self._max_streak(returns > 0, True))
        max_lose_streak = int(self._max_streak(returns < 0, True))
        avg_win_streak = float(self._avg_streak(returns > 0, True))
        avg_lose_streak = float(self._avg_streak(returns < 0, True))

        # 统计检验: returns 是否显著 > 0
        if n > 1:
            t_stat, p_value = stats.ttest_1samp(returns, 0)
            p_value_one_sided = float(p_value / 2) if t_stat > 0 else 1.0 - float(p_value / 2)
        else:
            t_stat, p_value_one_sided = 0.0, 1.0

        # 收益分布
        percentiles = [float(np.percentile(returns, p)) for p in [5, 25, 50, 75, 95]]

        metrics = {
            "total_bets": n,
            "resolved_bets": int(n_resolved),
            "wins": int(n_wins),
            "losses": int(n_losses),
            "pushes": int(n_pushes),
            "win_rate": round(win_rate, 4),
            "roi": round(roi, 4),
            "total_return": round(float(total_return), 4),
            "total_stake": round(float(total_stake), 4),
            "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "max_drawdown": round(max_dd, 4),
            "max_drawdown_pct": round(max_dd_pct, 4),
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
            "calmar_ratio": round(calmar, 4),
            "volatility": round(volatility, 4),
            "max_win_streak": max_win_streak,
            "max_lose_streak": max_lose_streak,
            "avg_win_streak": round(avg_win_streak, 2),
            "avg_lose_streak": round(avg_lose_streak, 2),
            "t_statistic": round(float(t_stat), 4),
            "p_value_one_sided": round(p_value_one_sided, 4),
            "significant": p_value_one_sided < 0.05,
            "return_percentiles": {
                "p5": percentiles[0], "p25": percentiles[1],
                "p50": percentiles[2], "p75": percentiles[3], "p95": percentiles[4],
            },
        }

        # 月度/年度统计
        if dates is not None:
            metrics["monthly"] = self._period_returns(returns, stakes, dates, "M")
            metrics["yearly"] = self._period_returns(returns, stakes, dates, "Y")

        return metrics

    def compute_from_df(self, bets_df: pd.DataFrame,
                         return_col: str = "profit",
                         stake_col: str = "stake",
                         date_col: str = "kickoff_time") -> dict:
        """从 DataFrame 计算指标。"""
        returns = bets_df[return_col].values
        stakes = bets_df[stake_col].values if stake_col in bets_df.columns else None
        dates = bets_df[date_col] if date_col in bets_df.columns else None

        if "capital_after" in bets_df.columns and len(bets_df) > 0:
            initial = bets_df.iloc[0].get("capital_before", 0)
            if initial == 0:
                initial = bets_df["capital_after"].iloc[0] - returns[0] if len(returns) > 0 else 10000
            equity = np.array([initial] + (initial + np.cumsum(returns)).tolist())
        else:
            equity = np.cumsum(returns)

        return self.compute(returns, stakes, dates, equity)

    def compare_models(self, model_results: dict) -> pd.DataFrame:
        """多模型指标对比表。

        Args:
            model_results: {model_name: metrics_dict}

        Returns:
            对比 DataFrame
        """
        rows = []
        for name, metrics in model_results.items():
            if "error" in metrics:
                continue
            rows.append({
                "model": name,
                "bets": metrics.get("total_bets", 0),
                "roi": metrics.get("roi", 0),
                "win_rate": metrics.get("win_rate", 0),
                "sharpe": metrics.get("sharpe_ratio", 0),
                "sortino": metrics.get("sortino_ratio", 0),
                "max_dd_pct": metrics.get("max_drawdown_pct", 0),
                "profit_factor": metrics.get("profit_factor") or 0,
                "calmar": metrics.get("calmar_ratio", 0),
                "volatility": metrics.get("volatility", 0),
                "max_win_streak": metrics.get("max_win_streak", 0),
                "max_lose_streak": metrics.get("max_lose_streak", 0),
            })
        return pd.DataFrame(rows).set_index("model")

    # ── 内部方法 ────────────────────────────────────────────────

    def _sharpe_ratio(self, returns: np.ndarray) -> float:
        if len(returns) < 2:
            return 0.0
        mean_r = np.mean(returns)
        std_r = np.std(returns, ddof=1)
        return float(mean_r / std_r) if std_r > 0 else 0.0

    def _sortino_ratio(self, returns: np.ndarray) -> float:
        if len(returns) < 2:
            return 0.0
        mean_r = np.mean(returns)
        downside = returns[returns < 0]
        if len(downside) < 2:
            return 0.0
        downside_std = np.std(downside, ddof=1)
        return float(mean_r / downside_std) if downside_std > 0 else 0.0

    def _max_streak(self, condition: np.ndarray, value: bool) -> int:
        max_s, cur = 0, 0
        for v in condition:
            if v == value:
                cur += 1
                max_s = max(max_s, cur)
            else:
                cur = 0
        return max_s

    def _avg_streak(self, condition: np.ndarray, value: bool) -> float:
        streaks = []
        cur = 0
        for v in condition:
            if v == value:
                cur += 1
            else:
                if cur > 0:
                    streaks.append(cur)
                cur = 0
        if cur > 0:
            streaks.append(cur)
        return float(np.mean(streaks)) if streaks else 0.0

    def _period_returns(self, returns: np.ndarray, stakes: np.ndarray,
                         dates: pd.Series, freq: str) -> dict:
        """按周期聚合收益。"""
        try:
            dt = pd.to_datetime(dates, format="%Y-%m-%d %H:%M:%S", errors="coerce")
        except Exception:
            dt = pd.to_datetime(dates, errors="coerce")

        valid = dt.notna()
        if not valid.any():
            return {}

        r = returns[valid]
        s = stakes[valid]
        d = dt[valid]

        period = d.dt.to_period(freq)
        df = pd.DataFrame({"return": r, "stake": s, "period": period.values})
        agg = df.groupby("period").agg(
            total_return=("return", "sum"),
            total_stake=("stake", "sum"),
            bets=("return", "count"),
        )
        agg["roi"] = agg["total_return"] / agg["total_stake"]
        agg["win_rate"] = df.groupby("period")["return"].apply(
            lambda x: (x > 0).sum() / max((x != 0).sum(), 1)
        )

        result = {}
        for idx, row in agg.iterrows():
            result[str(idx)] = {
                "return": round(float(row["total_return"]), 4),
                "stake": round(float(row["total_stake"]), 4),
                "bets": int(row["bets"]),
                "roi": round(float(row["roi"]), 4),
                "win_rate": round(float(row["win_rate"]), 4),
            }
        return result


def rolling_roi(returns: np.ndarray, window: int = 50) -> np.ndarray:
    """计算滚动 ROI 序列。"""
    if len(returns) < window:
        return np.full(len(returns), np.nan)
    result = np.full(len(returns), np.nan)
    cumsum = np.cumsum(returns)
    for i in range(window - 1, len(returns)):
        result[i] = (cumsum[i] - cumsum[i - window + 1] + returns[i - window + 1]) / window
    return result


def max_drawdown_series(returns: np.ndarray) -> np.ndarray:
    """计算回撤序列。"""
    eq = np.cumsum(returns)
    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 0, (peak - eq) / peak, 0)
    return dd
