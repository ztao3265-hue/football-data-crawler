#!/usr/bin/env python3
"""CLV (Closing Line Value) 分析器 — 衡量能否 beat market"""

import numpy as np
import pandas as pd
from scipy import stats


class CLVAnalyzer:
    """分析投注赔率 vs 收盘赔率，评估是否具备长期 beat market 能力。

    CLV > 0 意味着投注赔率优于收盘赔率，是长期盈利的关键指标。
    """

    def __init__(self):
        self.results = {}

    def analyze(self, bets_df: pd.DataFrame, task: str) -> dict:
        """计算 CLV 指标。

        Args:
            bets_df: 含投注赔率和对应收盘赔率的 DataFrame
            task: 'asian' | 'over_under' | 'wdl'

        Returns:
            CLV 分析结果
        """
        df = bets_df.copy()
        n = len(df)

        if task == "asian":
            df["clv"] = df.apply(self._asian_clv, axis=1)
        elif task == "over_under":
            df["clv"] = df.apply(self._ou_clv, axis=1)
        else:
            df["clv"] = df.apply(self._wdl_clv, axis=1)

        clv_valid = df["clv"].dropna()
        if len(clv_valid) == 0:
            return {"error": "无有效 CLV 数据"}

        # CLV 统计
        mean_clv = float(clv_valid.mean())
        median_clv = float(clv_valid.median())
        pos_clv_rate = float((clv_valid > 0).mean())
        clv_std = float(clv_valid.std())

        # t-test: CLV 是否显著 > 0
        t_stat, p_value = stats.ttest_1samp(clv_valid, 0)
        p_value_one_sided = float(p_value / 2) if t_stat > 0 else 1.0

        # CLV 与单场收益的相关性
        if "return" in df.columns:
            valid_for_corr = df.dropna(subset=["clv", "return"])
            if len(valid_for_corr) > 10:
                corr = float(valid_for_corr["clv"].corr(valid_for_corr["return"]))
            else:
                corr = None
        else:
            corr = None

        # CLV 分桶分析
        bins = [-np.inf, -0.02, 0, 0.02, np.inf]
        labels = ["CLV < -2%", "-2% ~ 0", "0 ~ +2%", "CLV > +2%"]
        df["clv_bucket"] = pd.cut(df["clv"], bins=bins, labels=labels)
        bucket_stats = {}
        for label in labels:
            bucket = df[df["clv_bucket"] == label]
            if len(bucket) > 0:
                bucket_stats[label] = {
                    "count": int(len(bucket)),
                    "avg_clv": round(float(bucket["clv"].mean()), 4),
                    "win_rate": round(float((bucket.get("result") == "win").mean()), 4) if "result" in bucket.columns else None,
                    "avg_return": round(float(bucket["return"].mean()), 4) if "return" in bucket.columns else None,
                }

        return {
            "n_valid": int(len(clv_valid)),
            "n_total": n,
            "mean_clv": round(mean_clv, 6),
            "median_clv": round(median_clv, 6),
            "positive_clv_rate": round(pos_clv_rate, 4),
            "clv_std": round(clv_std, 6),
            "t_statistic": round(float(t_stat), 4),
            "p_value_one_sided": round(p_value_one_sided, 4),
            "significant": p_value_one_sided < 0.05,
            "clv_return_correlation": round(corr, 4) if corr is not None else None,
            "bucket_analysis": bucket_stats,
            "interpretation": self._interpret(mean_clv, p_value_one_sided, pos_clv_rate),
        }

    def _asian_clv(self, row) -> float | None:
        """亚盘 CLV: 投注方水位 vs 对应收盘水位。"""
        pred = row.get("y_pred")
        if pred is None or pd.isna(pred):
            return None
        pred = int(pred)
        if pred == 1:
            return None  # 走水
        if pred == 2:
            open_w = row.get("asian_open_high_water")
            close_w = row.get("asian_close_high_water")
        else:
            open_w = row.get("asian_open_low_water")
            close_w = row.get("asian_close_low_water")

        if pd.isna(open_w) or pd.isna(close_w) or close_w == 0:
            return None
        return float((open_w - close_w) / close_w)

    def _ou_clv(self, row) -> float | None:
        """大小球 CLV。"""
        pred = row.get("y_pred")
        if pred is None or pd.isna(pred):
            return None
        pred = int(pred)
        if pred == 1:
            open_w = row.get("ou_open_over_water")
            close_w = row.get("ou_close_over_water")
        else:
            open_w = row.get("ou_open_under_water")
            close_w = row.get("ou_close_under_water")

        if pd.isna(open_w) or pd.isna(close_w) or close_w == 0:
            return None
        return float((open_w - close_w) / close_w)

    def _wdl_clv(self, row) -> float | None:
        """欧赔 CLV。"""
        pred = row.get("y_pred")
        if pred is None or pd.isna(pred):
            return None
        pred = int(pred)
        col_map = {0: ("open_home_odds", "close_home_odds"),
                   1: ("open_draw_odds", "close_draw_odds"),
                   2: ("open_away_odds", "close_away_odds")}
        cols = col_map.get(pred, ("open_home_odds", "close_home_odds"))
        open_odds = row.get(cols[0])
        close_odds = row.get(cols[1])

        if pd.isna(open_odds) or pd.isna(close_odds) or close_odds == 0:
            return None
        # CLV = (开盘赔率 - 收盘赔率) / 收盘赔率
        # 正 CLV 表示开盘赔率高于收盘，意味着价值
        return float((open_odds - close_odds) / close_odds)

    def _interpret(self, mean_clv: float, p_value: float, pos_rate: float) -> str:
        if p_value < 0.01 and mean_clv > 0.005:
            return "强 CLV 信号 — 开盘赔率系统性优于收盘，具备 beat market 能力"
        elif p_value < 0.05 and mean_clv > 0:
            return "中等 CLV 信号 — 有证据表明能获取优于收盘赔率的价值"
        elif mean_clv > 0:
            return "弱 CLV 信号 — 方向正确但统计不显著"
        else:
            return "无 CLV 信号 — 投注赔率未能系统性优于收盘赔率"
