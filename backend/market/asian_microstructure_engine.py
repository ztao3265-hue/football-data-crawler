#!/usr/bin/env python3
"""亚洲盘市场微结构引擎 (Asian Market Microstructure Engine)

从"预测比赛结果"转向"发现亚洲盘市场错误定价"。
"""

import sqlite3
import json
import os
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import numpy as np
import pandas as pd

# ── 亚盘盘口解析 (自包含, 不依赖 feature_engineering) ──

def parse_asian_handicap(text: str) -> float | None:
    """将亚盘盘口中文字符串转为数值。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip().replace(" ", "")
    if not text:
        return None
    sign = 1
    if text.startswith("受"):
        sign = -1
        text = text[1:]
    mapping = {
        "平手": 0.0, "平手/半球": 0.25, "半球": 0.5, "半球/一球": 0.75,
        "一球": 1.0, "一球/球半": 1.25, "球半": 1.5, "球半/两球": 1.75,
        "两球": 2.0, "两球/两球半": 2.25, "两球半": 2.5, "两球半/三球": 2.75,
        "三球": 3.0, "三球/三球半": 3.25, "三球半": 3.5, "三球半/四球": 3.75,
        "四球": 4.0,
    }
    val = mapping.get(text)
    return sign * val if val is not None else None


def parse_ou_handicap(text: str) -> float | None:
    """将大小球盘口字符串转为数值。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip().replace(" ", "").replace("球", "")
    if not text:
        return None
    if "/" in text:
        parts = text.split("/")
        try:
            return (float(parts[0]) + float(parts[1])) / 2
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


# ── 主引擎 ──

class AsianMicrostructureEngine:
    """亚洲盘市场微结构引擎。

    整合 8 大分析模块:
    1. 盘口行为分析 (开盘→收盘变化路径)
    2. 盘口强度评分 (多维度综合评分)
    3. 博彩公司分歧 (跨公司盘口/水位对比)
    4. 盘口行为标签 (8种行为模式自动识别)
    5. 时间序列盘口代理分析 (open→close 时间维度)
    6. Asian Value Engine (错误定价识别)
    7. Asian Trade Score (A/B/C/D 交易评分)
    8. 风险控制 (5种过滤器)
    """

    def __init__(self, data_dir: str = "datasets", db_path: str = None,
                 output_dir: str = "reports/asian_microstructure"):
        if db_path is None:
            from config.paths import DB_FOOTBALL_HISTORY
            db_path = str(DB_FOOTBALL_HISTORY)
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.df = None           # 合并后的完整数据
        self.asian_raw = None    # SQLite 原始亚盘数据 (5家公司)
        self.euro_raw = None     # SQLite 原始欧赔数据
        self.report = {}         # 汇总报告数据

    # ═══════════════════════════════════════════════════════════════
    # 数据加载
    # ═══════════════════════════════════════════════════════════════

    def _load_sqlite_data(self):
        """从 SQLite 加载所有博彩公司的原始亚盘和欧赔数据。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row

        # 亚盘: 5家公司 × opening/closing
        asian_rows = conn.execute("""
            SELECT match_id, bookmaker, odds_type, high_water, handicap, low_water
            FROM odds_asian
            WHERE high_water IS NOT NULL AND handicap IS NOT NULL AND low_water IS NOT NULL
        """).fetchall()

        asian_data = []
        for r in asian_rows:
            line = parse_asian_handicap(r["handicap"])
            if line is None:
                continue
            asian_data.append({
                "match_id": r["match_id"],
                "bookmaker": r["bookmaker"],
                "odds_type": r["odds_type"],
                "handicap_line": line,
                "high_water": r["high_water"],
                "low_water": r["low_water"],
            })
        self.asian_raw = pd.DataFrame(asian_data)

        # 欧赔: 6家公司 × opening/closing
        euro_rows = conn.execute("""
            SELECT match_id, bookmaker, odds_type, odds_home, odds_draw, odds_away
            FROM odds_europe
            WHERE odds_home IS NOT NULL AND odds_draw IS NOT NULL AND odds_away IS NOT NULL
        """).fetchall()

        euro_data = []
        for r in euro_rows:
            euro_data.append({
                "match_id": r["match_id"],
                "bookmaker": r["bookmaker"],
                "odds_type": r["odds_type"],
                "odds_home": r["odds_home"],
                "odds_draw": r["odds_draw"],
                "odds_away": r["odds_away"],
            })
        self.euro_raw = pd.DataFrame(euro_data)

        conn.close()

        n_asian = len(self.asian_raw)
        n_euro = len(self.euro_raw)
        print(f"  SQLite 亚盘: {n_asian} 条 ({self.asian_raw['bookmaker'].nunique()}家公司)")
        print(f"  SQLite 欧赔: {n_euro} 条 ({self.euro_raw['bookmaker'].nunique()}家公司)")

    def _load_datasets(self):
        """加载并合并 train/val/test 数据集。"""
        frames = []
        for split in ["train", "validation", "test"]:
            fp = self.data_dir / f"{split}.csv"
            if fp.exists():
                df = pd.read_csv(fp)
                df["_split"] = split
                frames.append(df)

        self.df = pd.concat(frames, ignore_index=True)
        self.df["kickoff_dt"] = pd.to_datetime(self.df["kickoff_time"], errors="coerce")
        print(f"  数据集: {len(self.df)} 场比赛 ({len(frames)}个分片)")

    def load_data(self):
        """加载所有数据源并合并。"""
        print("\n[数据加载]")
        self._load_sqlite_data()
        self._load_datasets()
        self._build_bookmaker_features()

    def _build_bookmaker_features(self):
        """从 SQLite 原始数据构建多公司亚盘特征。"""
        if self.asian_raw is None or self.df is None:
            return

        print("  构建多公司亚盘特征...")
        match_ids = set(self.df["match_id"].values)

        # pivot: 每个 match × bookmaker × odds_type 的 handicap_line
        asian = self.asian_raw[self.asian_raw["match_id"].isin(match_ids)].copy()

        bookmaker_stats = {}
        for bk in asian["bookmaker"].unique():
            bk_data = asian[asian["bookmaker"] == bk]
            close_data = bk_data[bk_data["odds_type"] == "closing"]
            open_data = bk_data[bk_data["odds_type"] == "opening"]

            close_agg = close_data.groupby("match_id").agg(
                **{f"{bk}_close_line": ("handicap_line", "first"),
                   f"{bk}_close_hw": ("high_water", "first"),
                   f"{bk}_close_lw": ("low_water", "first")}
            )
            open_agg = open_data.groupby("match_id").agg(
                **{f"{bk}_open_line": ("handicap_line", "first"),
                   f"{bk}_open_hw": ("high_water", "first"),
                   f"{bk}_open_lw": ("low_water", "first")}
            )

            merged = close_agg.join(open_agg, how="outer")
            bookmaker_stats[bk] = {
                "n_close": len(close_data),
                "n_open": len(open_data),
            }

            # 合并到主 DataFrame
            for col in merged.columns:
                self.df[col] = self.df["match_id"].map(merged[col])

        self.report["bookmaker_stats"] = bookmaker_stats

        # 多公司终盘盘口统计
        close_cols = [f"{bk}_close_line" for bk in asian["bookmaker"].unique()]
        available_cols = [c for c in close_cols if c in self.df.columns]
        if available_cols:
            close_lines = self.df[available_cols]
            self.df["bookmaker_line_std"] = close_lines.std(axis=1)
            self.df["bookmaker_line_range"] = close_lines.max(axis=1) - close_lines.min(axis=1)
            self.df["bookmaker_line_mean"] = close_lines.mean(axis=1)
            self.df["bookmaker_count"] = close_lines.notna().sum(axis=1)

        print(f"    已构建 {len(available_cols)} 列多公司特征")

    # ═══════════════════════════════════════════════════════════════
    # Module 1: 盘口行为分析
    # ═══════════════════════════════════════════════════════════════

    def analyze_line_movement(self):
        """分析开盘→收盘盘口变化路径。

        覆盖: 升盘/降盘/跳盘/升水/降水/临场异动
        """
        print("\n[Module 1] 盘口行为分析...")
        df = self.df

        # ── 1a. 盘口变化分类 (基于 Bet365) ──
        line_change = df["asian_line_change"]

        conditions = [
            (line_change >= 0.5),
            (line_change >= 0.25),
            (line_change > 0),
            (line_change <= -0.5),
            (line_change <= -0.25),
            (line_change < 0),
        ]
        choices = ["跳升盘", "升盘", "微升盘", "跳降盘", "降盘", "微降盘"]
        df["line_movement_type"] = np.select(conditions, choices, default="盘口稳定")

        # ── 1b. 水位变化分类 (需要 open water, 从 SQLite 已加载) ──
        if "Bet365_open_hw" in df.columns and "Bet365_close_hw" in df.columns:
            df["_hw_change"] = df["Bet365_close_hw"] - df["Bet365_open_hw"]
            df["_lw_change"] = df["Bet365_close_lw"] - df["Bet365_open_lw"]

            hw_cond = [
                (df["_hw_change"] >= 0.10), (df["_hw_change"] >= 0.05),
                (df["_hw_change"] <= -0.10), (df["_hw_change"] <= -0.05),
            ]
            hw_choices = ["急升水", "升水", "急降水", "降水"]
            df["hw_movement_type"] = np.select(hw_cond, hw_choices, default="水位稳定")

            lw_cond = [
                (df["_lw_change"] >= 0.10), (df["_lw_change"] >= 0.05),
                (df["_lw_change"] <= -0.10), (df["_lw_change"] <= -0.05),
            ]
            lw_choices = ["急升水", "升水", "急降水", "降水"]
            df["lw_movement_type"] = np.select(lw_cond, lw_choices, default="水位稳定")
        else:
            df["_hw_change"] = np.nan
            df["_lw_change"] = np.nan
            df["hw_movement_type"] = "无数据"
            df["lw_movement_type"] = "无数据"

        # ── 1c. 盘口+水位组合信号 ──
        def _combined_signal(row):
            lm = row["line_movement_type"]
            hwm = row.get("hw_movement_type", "")
            lwm = row.get("lw_movement_type", "")
            direction = row.get("asian_direction", 0)

            # 强势信号: 升盘 + 降水
            if "升盘" in lm and "降水" in str(hwm):
                return "强势升盘信号"
            if "降盘" in lm and "降水" in str(lwm):
                return "强势降盘信号"
            # 弱势信号: 升盘 + 升水 (市场分歧)
            if "升盘" in lm and "升水" in str(hwm):
                return "弱势升盘信号(分歧)"
            if "降盘" in lm and "升水" in str(lwm):
                return "弱势降盘信号(分歧)"
            # 纯水位信号
            if "盘口稳定" in lm and ("急升水" in str(hwm) or "急降水" in str(hwm)):
                return "临场资金冲击"
            if "盘口稳定" in lm and "降水" in str(hwm):
                return "真实降水信号"
            if "盘口稳定" in lm and "升水" in str(hwm):
                return "真实升水信号"
            # 跳盘信号
            if "跳升盘" in lm:
                return "强势跳升盘"
            if "跳降盘" in lm:
                return "强势跳降盘"
            return "正常波动"

        df["combined_signal"] = df.apply(_combined_signal, axis=1)

        # ── 统计 ──
        stats = {
            "line_movement": df["line_movement_type"].value_counts().to_dict(),
            "combined_signal": df["combined_signal"].value_counts().to_dict(),
        }
        self.report["line_movement"] = stats

        print(f"    盘口变化类型: {stats['line_movement']}")
        print(f"    组合信号分布: {stats['combined_signal']}")

    # ═══════════════════════════════════════════════════════════════
    # Module 2: 盘口强度评分
    # ═══════════════════════════════════════════════════════════════

    def score_market_strength(self):
        """多维度盘口强度评分 (0-100)。

        维度:
        - 盘口深度 (0-20): 让球越深, 市场信心越强
        - 盘口变化幅度 (0-20): 变化越大, 信号越强
        - 水位压力 (0-20): 水位变化方向和幅度
        - 多公司共识 (0-20): 多家公司盘口一致性
        - 赔率稳定性 (0-20): 欧赔离散度低则稳定
        """
        print("\n[Module 2] 盘口强度评分...")
        df = self.df

        # 维度1: 盘口深度
        abs_line = df["asian_close_line"].abs()
        df["strength_depth"] = np.select(
            [abs_line >= 2.0, abs_line >= 1.5, abs_line >= 1.0, abs_line >= 0.5, abs_line >= 0.25],
            [20, 16, 12, 8, 4], default=2
        )

        # 维度2: 盘口变化幅度 (信念强度)
        abs_change = df["asian_line_change"].abs()
        df["strength_movement"] = np.select(
            [abs_change >= 0.75, abs_change >= 0.5, abs_change >= 0.25, abs_change > 0],
            [20, 15, 10, 6], default=4
        )

        # 维度3: 水位压力 (水位朝有利方向变化)
        def _water_pressure(row):
            score = 10  # 中性基线
            hw_chg = row.get("_hw_change", 0)
            lw_chg = row.get("_lw_change", 0)
            line_dir = row.get("asian_line_direction", 0)
            if pd.isna(hw_chg) or pd.isna(lw_chg):
                return 10
            # 升盘 + 高水降水 = 强压力
            if line_dir > 0 and hw_chg < -0.03:
                score = 18
            elif line_dir > 0 and hw_chg < -0.01:
                score = 14
            # 降盘 + 低水降水 = 强压力
            elif line_dir < 0 and lw_chg < -0.03:
                score = 18
            elif line_dir < 0 and lw_chg < -0.01:
                score = 14
            # 水位急变
            elif abs(hw_chg) >= 0.08 or abs(lw_chg) >= 0.08:
                score = 16
            elif abs(hw_chg) >= 0.04 or abs(lw_chg) >= 0.04:
                score = 13
            return score

        df["strength_water"] = df.apply(_water_pressure, axis=1)

        # 维度4: 多公司共识
        if "bookmaker_line_std" in df.columns:
            line_std = df["bookmaker_line_std"].fillna(0.5)
            df["strength_consensus"] = np.select(
                [line_std < 0.05, line_std < 0.10, line_std < 0.15, line_std < 0.25],
                [20, 15, 10, 6], default=3
            )
        else:
            df["strength_consensus"] = 10

        # 维度5: 赔率稳定性 (odds_dispersion 低 = 稳定)
        if "odds_dispersion" in df.columns:
            disp = df["odds_dispersion"].fillna(0.1)
            df["strength_stability"] = np.select(
                [disp < 0.02, disp < 0.05, disp < 0.08, disp < 0.12],
                [20, 15, 10, 6], default=3
            )
        else:
            df["strength_stability"] = 10

        # ── 综合评分 ──
        strength_cols = ["strength_depth", "strength_movement", "strength_water",
                         "strength_consensus", "strength_stability"]
        df["market_strength_score"] = df[strength_cols].sum(axis=1)

        # 评分分布
        score_bins = [0, 30, 50, 65, 80, 100]
        score_labels = ["极弱", "弱", "中等", "强", "极强"]
        df["strength_level"] = pd.cut(df["market_strength_score"], bins=score_bins,
                                       labels=score_labels, include_lowest=True)

        stats = {
            "mean_score": round(df["market_strength_score"].mean(), 1),
            "median_score": round(df["market_strength_score"].median(), 1),
            "std_score": round(df["market_strength_score"].std(), 1),
            "level_distribution": df["strength_level"].value_counts().to_dict(),
            "component_means": {c: round(df[c].mean(), 1) for c in strength_cols},
        }
        self.report["market_strength"] = stats
        print(f"    综合评分均值: {stats['mean_score']}, 分布: {stats['level_distribution']}")

    # ═══════════════════════════════════════════════════════════════
    # Module 3: 博彩公司分歧分析
    # ═══════════════════════════════════════════════════════════════

    def analyze_disagreement(self):
        """分析不同博彩公司之间的盘口/水位分歧。"""
        print("\n[Module 3] 博彩公司分歧分析...")
        df = self.df

        bookmakers = ["Bet365", "Macau", "Crown", "William Hill", "Ladbrokes"]
        available_bks = [bk for bk in bookmakers if f"{bk}_close_line" in df.columns]

        if len(available_bks) < 2:
            print("    [警告] 多公司数据不足, 跳过分歧分析")
            self.report["disagreement"] = {"error": "insufficient bookmaker data"}
            return

        # ── 每场比赛的盘口分歧度 ──
        close_cols = [f"{bk}_close_line" for bk in available_bks]
        hw_cols = [f"{bk}_close_hw" for bk in available_bks]

        lines_df = df[close_cols]
        df["disagreement_line_std"] = lines_df.std(axis=1, skipna=True)
        df["disagreement_line_maxmin"] = lines_df.max(axis=1) - lines_df.min(axis=1)
        df["disagreement_n_bookmakers"] = lines_df.notna().sum(axis=1)

        hw_df = df[hw_cols]
        df["disagreement_water_std"] = hw_df.std(axis=1, skipna=True)

        # ── 分歧等级 ──
        line_std = df["disagreement_line_std"].fillna(0)
        df["disagreement_level"] = np.select(
            [line_std >= 0.25, line_std >= 0.15, line_std >= 0.05],
            ["高度分歧", "中度分歧", "轻微分歧"], default="一致"
        )

        # ── 找出"异类"博彩公司 ──
        def _find_outlier(row):
            vals = {}
            for bk in available_bks:
                v = row.get(f"{bk}_close_line")
                if pd.notna(v):
                    vals[bk] = v
            if len(vals) < 3:
                return "数据不足"
            median = np.median(list(vals.values()))
            for bk, v in vals.items():
                if abs(v - median) >= 0.25:
                    return f"{bk}异类({v:+.2f} vs 中位数{median:+.2f})"
            return "无异常"

        df["disagreement_outlier"] = df.apply(_find_outlier, axis=1)

        # ── 博彩公司统计对比 ──
        bk_comparison = {}
        for bk in available_bks:
            close_line = df[f"{bk}_close_line"]
            close_hw = df[f"{bk}_close_hw"]
            close_lw = df[f"{bk}_close_lw"]

            bk_comparison[bk] = {
                "n": int(close_line.notna().sum()),
                "line_mean": round(close_line.mean(), 3),
                "line_std": round(close_line.std(), 3),
                "hw_mean": round(close_hw.mean(), 3),
                "hw_std": round(close_hw.std(), 3),
                "lw_mean": round(close_lw.mean(), 3),
                "lw_std": round(close_lw.std(), 3),
                "hw_lw_corr": round(close_hw.corr(close_lw), 4) if close_hw.notna().sum() > 10 else None,
            }

        stats = {
            "bookmakers_available": available_bks,
            "disagreement_distribution": df["disagreement_level"].value_counts().to_dict(),
            "outlier_matches": df[df["disagreement_outlier"] != "无异常"]["disagreement_outlier"].value_counts().to_dict(),
            "bookmaker_comparison": bk_comparison,
        }
        self.report["disagreement"] = stats
        print(f"    分歧分布: {stats['disagreement_distribution']}")
        if stats["outlier_matches"]:
            print(f"    异类博彩公司: {stats['outlier_matches']}")

    # ═══════════════════════════════════════════════════════════════
    # Module 4: 盘口行为标签系统
    # ═══════════════════════════════════════════════════════════════

    def label_behaviors(self):
        """自动识别 8 种盘口行为模式。"""
        print("\n[Module 4] 盘口行为标签...")
        df = self.df

        # ── 定义 8 种行为模式 ──
        has_water = "_hw_change" in df.columns

        # 1. 强势升盘: 升盘 ≥ 0.25 + 高水降水
        cond1 = (df["asian_line_change"] >= 0.25)
        if has_water:
            cond1 = cond1 & (df["_hw_change"] < -0.02)

        # 2. 弱势升盘: 升盘 + 高水升水 (诱多)
        cond2 = (df["asian_line_change"] >= 0.25)
        if has_water:
            cond2 = cond2 & (df["_hw_change"] > 0.02)

        # 3. 诱盘: 盘口变化方向与欧赔方向相反
        if "prob_change_home" in df.columns:
            euro_up = df["prob_change_home"] > 0.02
            asian_down = df["asian_line_direction"] < 0
            euro_down = df["prob_change_home"] < -0.02
            asian_up = df["asian_line_direction"] > 0
            cond3 = (euro_up & asian_down) | (euro_down & asian_up)
        else:
            cond3 = pd.Series(False, index=df.index)

        # 4. 真实降水: 盘口稳定 + 高水降水 > 0.05
        if has_water:
            cond4 = (df["asian_line_change"] == 0) & (df["_hw_change"] < -0.05)
        else:
            cond4 = pd.Series(False, index=df.index)

        # 5. 热门过热: 升盘 + 升水 (市场追涨)
        if has_water:
            cond5 = (df["asian_line_change"] > 0) & (df["_hw_change"] > 0.03) & (df["_lw_change"] < -0.03)
        else:
            cond5 = pd.Series(False, index=df.index)

        # 6. 冷门防范: 降盘 + 低水降水 (市场防范冷门)
        if has_water:
            cond6 = (df["asian_line_change"] < -0.25) & (df["_lw_change"] < -0.03)
        else:
            cond6 = (df["asian_line_change"] < -0.25)

        # 7. 临场资金冲击: 盘口大幅变化 ≥ 0.5
        cond7 = df["asian_line_change"].abs() >= 0.5

        # 8. 假动作盘口: 初盘 vs 终盘差异大, 但方向与欧赔相反
        if "prob_change_home" in df.columns and "asian_line_change" in df.columns:
            cond8 = (df["asian_line_change"].abs() >= 0.25) & (df["prob_change_home"].abs() < 0.01)
        else:
            cond8 = pd.Series(False, index=df.index)

        # ── 应用标签 (优先级: 1>2>3>4>5>6>7>8) ──
        df["behavior_label"] = "正常盘口"
        for cond, label in [
            (cond7, "临场资金冲击"),
            (cond3, "诱盘信号"),
            (cond1, "强势升盘"),
            (cond2, "弱势升盘"),
            (cond6, "冷门防范"),
            (cond4, "真实降水"),
            (cond5, "热门过热"),
            (cond8, "假动作盘口"),
        ]:
            df.loc[cond & (df["behavior_label"] == "正常盘口"), "behavior_label"] = label

        # ── 统计 ──
        label_counts = df["behavior_label"].value_counts().to_dict()
        stats = {
            "label_distribution": label_counts,
            "total_labeled": sum(v for k, v in label_counts.items() if k != "正常盘口"),
            "label_rate": round(
                sum(v for k, v in label_counts.items() if k != "正常盘口") / len(df) * 100, 1
            ),
        }
        self.report["behavior_labels"] = stats
        print(f"    行为标签分布: {label_counts}")
        print(f"    有标签比例: {stats['label_rate']}%")

    # ═══════════════════════════════════════════════════════════════
    # Module 5: 时间序列盘口代理分析
    # ═══════════════════════════════════════════════════════════════

    def analyze_movement_timing(self):
        """使用 open→close 变化幅度作为时间维度的代理分析。

        注意: 当前数据仅有开盘/收盘两个快照, 无法进行真实的 24h/6h/1h/30min
        时间序列分析。以下分析使用变化幅度和联赛特征作为代理指标。

        如需真实时间序列分析, 需要使用 odds_history 表中的时间戳数据。
        """
        print("\n[Module 5] 时间序列代理分析...")
        df = self.df

        # ── 按联赛统计盘口变化特征 ──
        league_stats = df.groupby("league_code").agg(
            n_matches=("match_id", "count"),
            avg_line_change=("asian_line_change", "mean"),
            abs_line_change=("asian_line_change", lambda x: x.abs().mean()),
            line_change_std=("asian_line_change", "std"),
            jump_rate=("asian_line_change", lambda x: (x.abs() >= 0.5).mean() * 100),
            move_rate=("asian_line_change", lambda x: (x.abs() >= 0.25).mean() * 100),
            stable_rate=("asian_line_change", lambda x: (x == 0).mean() * 100),
        ).round(3)

        league_stats = league_stats.sort_values("abs_line_change", ascending=False)

        # ── 按赛季统计 ──
        season_stats = df.groupby("season").agg(
            n_matches=("match_id", "count"),
            avg_line_change=("asian_line_change", "mean"),
            abs_line_change=("asian_line_change", lambda x: x.abs().mean()),
            jump_rate=("asian_line_change", lambda x: (x.abs() >= 0.5).mean() * 100),
        ).round(3)

        # ── 盘口变化幅度分桶 ──
        abs_chg = df["asian_line_change"].abs()
        df["movement_magnitude"] = pd.cut(
            abs_chg,
            bins=[-0.01, 0, 0.25, 0.5, 0.75, 5.0],
            labels=["零变化", "微调(<0.25)", "中等(0.25-0.5)", "大幅(0.5-0.75)", "跳盘(>0.75)"]
        )

        stats = {
            "by_league": league_stats.to_dict(),
            "by_season": season_stats.to_dict(),
            "magnitude_distribution": df["movement_magnitude"].value_counts().to_dict(),
            "data_limitation_note": "当前仅有开盘/收盘两个快照, 无中间时刻数据。以下分析为代理指标。",
        }
        self.report["movement_timing"] = stats

        print(f"    联赛盘口变化率排名 (前5):")
        for league, row in league_stats.head(5).iterrows():
            print(f"      {league}: 平均变化={row['abs_line_change']:.3f}, 跳盘率={row['jump_rate']:.1f}%")

    # ═══════════════════════════════════════════════════════════════
    # Module 6: Asian Value Engine
    # ═══════════════════════════════════════════════════════════════

    def _euro_prob_to_fair_handicap(self, home_prob: float, draw_prob: float, away_prob: float) -> float:
        """从欧赔隐含概率推导公允亚盘盘口。

        方法: 计算预期净胜球 → 映射到盘口线。
        简化模型: fair_line ≈ (home_prob - away_prob) * K
        其中 K ≈ 2.0-2.5 (经验系数)
        """
        prob_diff = home_prob - away_prob
        # 经验映射 (基于大量比赛数据)
        if prob_diff > 0.55:
            return 2.0 + (prob_diff - 0.55) * 4
        elif prob_diff > 0.40:
            return 1.5 + (prob_diff - 0.40) * 3.33
        elif prob_diff > 0.25:
            return 1.0 + (prob_diff - 0.25) * 3.33
        elif prob_diff > 0.12:
            return 0.5 + (prob_diff - 0.12) * 3.85
        elif prob_diff > 0.03:
            return 0.25 + (prob_diff - 0.03) * 2.78
        elif prob_diff > -0.03:
            return 0.0
        elif prob_diff > -0.12:
            return -0.25 + (prob_diff + 0.12) * 2.78
        elif prob_diff > -0.25:
            return -0.5 + (prob_diff + 0.25) * 3.85
        elif prob_diff > -0.40:
            return -1.0 + (prob_diff + 0.40) * 3.33
        else:
            return -1.5 + (prob_diff + 0.55) * 3.33

    def compute_value_edge(self):
        """计算 Asian Value Edge。

        核心: 欧赔隐含公允盘口 vs 实际亚盘盘口 = Edge
        """
        print("\n[Module 6] Asian Value Engine...")
        df = self.df

        # ── 公允盘口 (从 Bet365 收盘欧赔推导) ──
        if all(c in df.columns for c in ["close_home_prob", "close_draw_prob", "close_away_prob"]):
            df["fair_asian_line"] = df.apply(
                lambda r: self._euro_prob_to_fair_handicap(
                    r["close_home_prob"], r["close_draw_prob"], r["close_away_prob"]
                ), axis=1
            )
        else:
            # 从原始赔率计算隐含概率 (去除margin)
            if all(c in df.columns for c in ["close_home_odds", "close_draw_odds", "close_away_odds"]):
                inv = 1/df["close_home_odds"] + 1/df["close_draw_odds"] + 1/df["close_away_odds"]
                df["_hp_raw"] = 1/df["close_home_odds"] / inv
                df["_dp_raw"] = 1/df["close_draw_odds"] / inv
                df["_ap_raw"] = 1/df["close_away_odds"] / inv
                df["fair_asian_line"] = df.apply(
                    lambda r: self._euro_prob_to_fair_handicap(r["_hp_raw"], r["_dp_raw"], r["_ap_raw"]),
                    axis=1
                )
            else:
                print("    [错误] 缺少欧赔数据, 无法计算公允盘口")
                self.report["value_engine"] = {"error": "missing european odds"}
                return

        # ── Edge 计算 ──
        df["asian_line_mispricing"] = df["asian_close_line"] - df["fair_asian_line"]
        df["asian_edge_pct"] = (df["asian_line_mispricing"].abs() / 0.25) * 2.5  # 每个0.25约2.5%的edge

        # ── Edge 方向 ──
        # mispricing > 0: 实际盘口比公允更深 → 下盘有价值
        # mispricing < 0: 实际盘口比公允更浅 → 上盘有价值
        df["value_direction"] = np.select(
            [df["asian_line_mispricing"] > 0.15, df["asian_line_mispricing"] < -0.15],
            ["下盘价值(盘口过深)", "上盘价值(盘口过浅)"], default="公允定价"
        )

        # ── Edge 等级 ──
        edge = df["asian_edge_pct"]
        df["value_edge_level"] = np.select(
            [edge >= 8, edge >= 5, edge >= 3, edge >= 1],
            ["超强Edge(≥8%)", "强Edge(5-8%)", "中等Edge(3-5%)", "弱Edge(1-3%)"],
            default="无Edge(<1%)"
        )

        stats = {
            "mean_mispricing": round(df["asian_line_mispricing"].mean(), 4),
            "std_mispricing": round(df["asian_line_mispricing"].std(), 4),
            "mean_edge_pct": round(df["asian_edge_pct"].mean(), 2),
            "edge_distribution": df["value_edge_level"].value_counts().to_dict(),
            "direction_distribution": df["value_direction"].value_counts().to_dict(),
            "fair_vs_actual_corr": round(df["fair_asian_line"].corr(df["asian_close_line"]), 4),
        }
        self.report["value_engine"] = stats
        print(f"    公允vs实际盘口相关性: {stats['fair_vs_actual_corr']}")
        print(f"    平均错误定价: {stats['mean_mispricing']:.3f} 球")
        print(f"    Edge分布: {stats['edge_distribution']}")

    # ═══════════════════════════════════════════════════════════════
    # Module 7: Asian Trade Score
    # ═══════════════════════════════════════════════════════════════

    def score_trades(self):
        """生成 Asian Trade Score (A/B/C/D)。

        A级: edge > 5% + strength > 65 + 低分歧 + 通过风控
        B级: edge > 3% + strength > 50 + 低分歧 + 通过风控
        C级: edge > 1% + strength > 35
        D级: 其余全部
        """
        print("\n[Module 7] Asian Trade Score...")
        df = self.df

        has_edge = "asian_edge_pct" in df.columns
        has_strength = "market_strength_score" in df.columns
        has_disagreement = "disagreement_level" in df.columns
        has_risk = "risk_flag" in df.columns

        if not has_edge or not has_strength:
            print("    [警告] 缺少 Edge 或 Strength 数据, 使用简化评分")
            df["trade_score"] = "C"
            df["trade_score_detail"] = "数据不足"
            self.report["trade_scoring"] = {"error": "insufficient data"}
            return

        edge = df["asian_edge_pct"].fillna(0)
        strength = df["market_strength_score"].fillna(50)
        low_disagreement = df.get("disagreement_level", pd.Series("一致", index=df.index)).isin(["一致", "轻微分歧"])
        risk_clean = ~df.get("risk_flag", pd.Series(False, index=df.index))

        df["trade_score"] = "D"
        df["trade_score_detail"] = ""

        # A级
        a_mask = (edge >= 5) & (strength >= 65) & low_disagreement & risk_clean
        df.loc[a_mask, "trade_score"] = "A"
        df.loc[a_mask, "trade_score_detail"] = "强价值+强信号+低分歧+风控通过"

        # B级
        b_mask = (edge >= 3) & (strength >= 50) & low_disagreement & risk_clean & (df["trade_score"] == "D")
        df.loc[b_mask, "trade_score"] = "B"
        df.loc[b_mask, "trade_score_detail"] = "中等价值+信号确认+风控通过"

        # C级
        c_mask = (edge >= 1) & (strength >= 35) & (df["trade_score"] == "D")
        df.loc[c_mask, "trade_score"] = "C"
        df.loc[c_mask, "trade_score_detail"] = "弱价值信号, 需观察"

        stats = {
            "score_distribution": df["trade_score"].value_counts().to_dict(),
            "A_rate": round((df["trade_score"] == "A").mean() * 100, 1),
            "B_rate": round((df["trade_score"] == "B").mean() * 100, 1),
            "C_rate": round((df["trade_score"] == "C").mean() * 100, 1),
            "D_rate": round((df["trade_score"] == "D").mean() * 100, 1),
            "A_details": df[df["trade_score"] == "A"]["trade_score_detail"].value_counts().to_dict(),
        }
        self.report["trade_scoring"] = stats
        print(f"    A级: {stats['A_rate']}% | B级: {stats['B_rate']}% | C级: {stats['C_rate']}% | D级: {stats['D_rate']}%")

    # ═══════════════════════════════════════════════════════════════
    # Module 8: 风险控制
    # ═══════════════════════════════════════════════════════════════

    def apply_risk_controls(self):
        """应用 5 种风险过滤器。

        1. 热门过热: 升盘+升水 (追涨陷阱)
        2. 临场异常: 盘口/水位极端变化
        3. 深盘风险: 盘口过深 (>2.5球) 的不确定性
        4. 高频跳盘: 盘口跳跃 ≥ 0.75
        5. 水位失真: 水位异常 (<0.70 或 >1.20)
        """
        print("\n[Module 8] 风险控制...")
        df = self.df

        has_water = "_hw_change" in df.columns

        # 风险1: 热门过热
        if has_water:
            risk1 = (df["asian_line_change"] > 0) & (df["_hw_change"] > 0.05)
        else:
            risk1 = pd.Series(False, index=df.index)

        # 风险2: 临场异常 (盘口跳升+水位急变)
        if has_water:
            risk2 = (df["asian_line_change"].abs() >= 0.5) & (df["_hw_change"].abs() >= 0.08)
        else:
            risk2 = df["asian_line_change"].abs() >= 0.75

        # 风险3: 深盘风险
        risk3 = df["asian_close_line"].abs() >= 2.5

        # 风险4: 高频跳盘
        risk4 = df["asian_line_change"].abs() >= 0.75

        # 风险5: 水位失真
        if "asian_close_high_water" in df.columns:
            risk5 = (df["asian_close_high_water"] < 0.70) | (df["asian_close_high_water"] > 1.20)
            risk5 = risk5 | ((df["asian_close_low_water"] < 0.70) | (df["asian_close_low_water"] > 1.20))
        else:
            risk5 = pd.Series(False, index=df.index)

        df["risk_1_overheat"] = risk1
        df["risk_2_late_anomaly"] = risk2
        df["risk_3_deep_handicap"] = risk3
        df["risk_4_jump"] = risk4
        df["risk_5_water_distortion"] = risk5

        df["risk_flag"] = risk1 | risk2 | risk3 | risk4 | risk5
        df["risk_count"] = risk1.astype(int) + risk2.astype(int) + risk3.astype(int) + risk4.astype(int) + risk5.astype(int)

        risk_reasons = []
        if risk1.any():
            risk_reasons.append(f"热门过热: {risk1.sum()}")
        if risk2.any():
            risk_reasons.append(f"临场异常: {risk2.sum()}")
        if risk3.any():
            risk_reasons.append(f"深盘风险: {risk3.sum()}")
        if risk4.any():
            risk_reasons.append(f"高频跳盘: {risk4.sum()}")
        if risk5.any():
            risk_reasons.append(f"水位失真: {risk5.sum()}")

        stats = {
            "total_flagged": int(df["risk_flag"].sum()),
            "flag_rate": round(df["risk_flag"].mean() * 100, 1),
            "risk_breakdown": risk_reasons,
            "risk_count_distribution": df["risk_count"].value_counts().to_dict(),
        }
        self.report["risk_control"] = stats
        print(f"    风险标记: {stats['total_flagged']} 场 ({stats['flag_rate']}%)")
        print(f"    风险明细: {stats['risk_breakdown']}")

    # ═══════════════════════════════════════════════════════════════
    # Module 9: 回测验证
    # ═══════════════════════════════════════════════════════════════

    def validate_with_backtest(self):
        """使用实际赛果验证 Edge 和 Trade Score 的有效性。

        预期: A级信号的 ROI 应显著高于 D级信号。
        """
        print("\n[Module 9] 回测验证...")
        df = self.df

        if "trade_score" not in df.columns or "label_asian" not in df.columns:
            print("    [警告] 缺少交易评分或标签, 跳过验证")
            self.report["validation"] = {"error": "missing trade_score or label_asian"}
            return

        # ── 按 Trade Score 分组计算亚盘胜率 ──
        # label_asian: 1=主赢盘, 0=走水, -1=客赢盘
        # 需要结合 value_direction 来判断投注方向

        results = {}
        for score in ["A", "B", "C", "D"]:
            subset = df[df["trade_score"] == score]
            if len(subset) == 0:
                results[score] = {"n": 0, "note": "无样本"}
                continue

            total = len(subset)
            # 上盘价值信号: 投主赢盘
            upper_value = subset[subset["value_direction"] == "上盘价值(盘口过浅)"]
            # 下盘价值信号: 投客赢盘
            lower_value = subset[subset["value_direction"] == "下盘价值(盘口过深)"]

            upper_wins = (upper_value["label_asian"] == 1).sum() if len(upper_value) > 0 else 0
            lower_wins = (lower_value["label_asian"] == -1).sum() if len(lower_value) > 0 else 0
            total_signals = len(upper_value) + len(lower_value)
            total_wins = upper_wins + lower_wins

            results[score] = {
                "n": total,
                "n_signals": total_signals,
                "n_wins": int(total_wins),
                "win_rate": round(total_wins / total_signals * 100, 1) if total_signals > 0 else None,
                "upper_signals": len(upper_value),
                "upper_wins": int(upper_wins),
                "lower_signals": len(lower_value),
                "lower_wins": int(lower_wins),
            }

        self.report["validation"] = results
        print("    Trade Score 亚盘胜率验证:")
        for score, r in results.items():
            if r.get("win_rate") is not None:
                print(f"      {score}级: 胜率={r['win_rate']}% ({r['n_wins']}/{r['n_signals']})")
            else:
                print(f"      {score}级: {r.get('note', 'N/A')}")

    # ═══════════════════════════════════════════════════════════════
    # Module 10: 报告生成
    # ═══════════════════════════════════════════════════════════════

    def generate_reports(self):
        """生成 6 种输出报告。"""
        print("\n[Module 10] 生成报告...")
        df = self.df
        out = self.output_dir

        # ── 1. market_behavior.html ──
        self._write_html_report(df, out)

        # ── 2. line_movement_analysis.csv ──
        movement_cols = ["match_id", "league_code", "home_team", "away_team",
                         "asian_open_line", "asian_close_line", "asian_line_change",
                         "line_movement_type", "hw_movement_type", "lw_movement_type",
                         "combined_signal", "behavior_label"]
        available = [c for c in movement_cols if c in df.columns]
        df[available].to_csv(out / "line_movement_analysis.csv", index=False)
        print(f"    line_movement_analysis.csv: {len(available)} 列")

        # ── 3. bookmaker_disagreement.csv ──
        dis_cols = ["match_id", "league_code", "home_team", "away_team",
                    "disagreement_line_std", "disagreement_water_std",
                    "disagreement_level", "disagreement_outlier",
                    "bookmaker_line_mean", "bookmaker_line_std"]
        available = [c for c in dis_cols if c in df.columns]
        df[available].to_csv(out / "bookmaker_disagreement.csv", index=False)
        print(f"    bookmaker_disagreement.csv: {len(available)} 列")

        # ── 4. asian_trade_signals.csv ──
        signal_cols = ["match_id", "kickoff_time", "league_code", "home_team", "away_team",
                       "asian_close_line", "fair_asian_line", "asian_line_mispricing",
                       "asian_edge_pct", "value_direction", "value_edge_level",
                       "market_strength_score", "strength_level",
                       "trade_score", "trade_score_detail",
                       "behavior_label", "risk_flag", "risk_count"]
        available = [c for c in signal_cols if c in df.columns]
        df[available].to_csv(out / "asian_trade_signals.csv", index=False)
        print(f"    asian_trade_signals.csv: {len(available)} 列")

        # ── 5. value_edge_analysis.html ──
        self._write_value_html(df, out)

        # ── 6. market_microstructure_summary.html ──
        self._write_summary_html(out)

        # ── 7. 完整 JSON 报告 ──
        self.report["generated_at"] = datetime.now().isoformat()
        self.report["total_matches"] = len(df)
        self.report["data_columns"] = list(df.columns)
        with open(out / "microstructure_summary.json", "w", encoding="utf-8") as f:
            json.dump(self.report, f, ensure_ascii=False, indent=2, default=str)
        print(f"    microstructure_summary.json")

    def _write_html_report(self, df, out):
        """生成市场行为分析 HTML 报告。"""
        # 关键统计
        total = len(df)
        n_risk = int(df["risk_flag"].sum()) if "risk_flag" in df.columns else 0
        n_edge = int((df.get("asian_edge_pct", 0) >= 3).sum()) if "asian_edge_pct" in df.columns else 0
        n_A = int((df.get("trade_score") == "A").sum()) if "trade_score" in df.columns else 0

        # 联赛Edge汇总
        league_edge_html = ""
        if "asian_edge_pct" in df.columns:
            league_edge = df.groupby("league_code")["asian_edge_pct"].mean().sort_values(ascending=False).head(10)
            league_edge_html = "<table><tr><th>联赛</th><th>平均Edge%</th></tr>"
            for lg, val in league_edge.items():
                league_edge_html += f"<tr><td>{lg}</td><td>{val:.2f}%</td></tr>"
            league_edge_html += "</table>"

        # 行为标签分布
        behavior_html = ""
        if "behavior_label" in df.columns:
            behavior_counts = df["behavior_label"].value_counts()
            behavior_html = "<table><tr><th>行为标签</th><th>数量</th><th>占比</th></tr>"
            for label, count in behavior_counts.items():
                behavior_html += f"<tr><td>{label}</td><td>{count}</td><td>{count/total*100:.1f}%</td></tr>"
            behavior_html += "</table>"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>亚洲盘市场行为分析</title>
<style>
body{{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5}}
h1{{color:#1a1a2e}}h2{{color:#16213e;border-bottom:2px solid #0f3460;padding-bottom:5px}}
.card{{background:white;border-radius:8px;padding:15px;margin:10px 0;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
.stats{{display:flex;gap:15px;flex-wrap:wrap}}.stat{{background:#0f3460;color:white;padding:15px;border-radius:8px;min-width:120px;text-align:center}}
.stat .value{{font-size:28px;font-weight:bold}}.stat .label{{font-size:12px;opacity:0.8;margin-top:5px}}
table{{border-collapse:collapse;width:100%;margin:10px 0}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#0f3460;color:white}}tr:nth-child(even){{background:#f2f2f2}}
.warning{{background:#fff3cd;border:1px solid #ffc107;padding:10px;border-radius:4px;margin:10px 0}}
</style></head>
<body>
<h1>亚洲盘市场微结构分析报告</h1>
<div class="card">
<h2>概览</h2>
<div class="stats">
<div class="stat"><div class="value">{total:,}</div><div class="label">总场次</div></div>
<div class="stat"><div class="value">{n_risk:,}</div><div class="label">风险标记</div></div>
<div class="stat"><div class="value">{n_edge:,}</div><div class="label">Edge≥3%</div></div>
<div class="stat"><div class="value">{n_A}</div><div class="label">A级信号</div></div>
</div>
</div>
<div class="card">
<h2>联赛 Edge 排名 (前10)</h2>
{league_edge_html}
</div>
<div class="card">
<h2>盘口行为标签分布</h2>
{behavior_html}
</div>
<div class="card">
<h2>博彩公司分歧分析</h2>
<p>分歧数据已输出到 bookmaker_disagreement.csv</p>
</div>
<div class="card">
<h2>数据说明</h2>
<div class="warning">
<strong>当前数据局限:</strong> 仅有开盘/收盘两个快照, 无法进行 24h/6h/1h/30min 的真实时间序列分析。
完整的微观结构分析需要 odds_history 表中的带时间戳数据。
</div>
</div>
<p style="color:#999;font-size:12px">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</body></html>"""
        with open(out / "market_behavior.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"    market_behavior.html")

    def _write_value_html(self, df, out):
        """生成价值分析 HTML 报告。"""
        # Edge等级汇总
        edge_html = ""
        if "value_edge_level" in df.columns:
            edge_counts = df["value_edge_level"].value_counts()
            edge_html = "<table><tr><th>Edge等级</th><th>数量</th><th>占比</th></tr>"
            for level, count in edge_counts.items():
                edge_html += f"<tr><td>{level}</td><td>{count}</td><td>{count/len(df)*100:.1f}%</td></tr>"
            edge_html += "</table>"

        # Trade Score vs Actual Win Rate
        validation_html = ""
        val_data = self.report.get("validation", {})
        if val_data and "error" not in val_data:
            validation_html = "<table><tr><th>Trade Score</th><th>样本数</th><th>信号数</th><th>胜率</th></tr>"
            for score in ["A", "B", "C", "D"]:
                r = val_data.get(score, {})
                wr = r.get("win_rate", "N/A")
                validation_html += f"<tr><td>{score}级</td><td>{r.get('n', 0)}</td><td>{r.get('n_signals', 0)}</td><td>{wr}%</td></tr>"
            validation_html += "</table>"

        # 风险过滤统计
        risk_html = ""
        risk_data = self.report.get("risk_control", {})
        if risk_data and "risk_breakdown" in risk_data:
            risk_html = "<ul>"
            for r in risk_data["risk_breakdown"]:
                risk_html += f"<li>{r}</li>"
            risk_html += "</ul>"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>Asian Value Edge 分析</title>
<style>
body{{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5}}
h1{{color:#1a1a2e}}h2{{color:#16213e;border-bottom:2px solid #0f3460;padding-bottom:5px}}
.card{{background:white;border-radius:8px;padding:15px;margin:10px 0;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
table{{border-collapse:collapse;width:100%;margin:10px 0}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#0f3460;color:white}}tr:nth-child(even){{background:#f2f2f2}}
.good{{color:green;font-weight:bold}}.bad{{color:red}}.warn{{color:orange}}
</style></head>
<body>
<h1>Asian Value Edge 分析报告</h1>
<div class="card">
<h2>Edge等级分布</h2>
{edge_html}
</div>
<div class="card">
<h2>Trade Score 胜率验证</h2>
<p>使用实际亚盘赛果验证各等级信号的有效性。</p>
{validation_html}
</div>
<div class="card">
<h2>风险控制详情</h2>
{risk_html}
</div>
<div class="card">
<h2>方法论说明</h2>
<p><strong>Edge计算:</strong> 从Bet365收盘欧赔推导公允亚盘盘口 → 与实际亚盘盘口对比 → 差值 = Edge</p>
<p><strong>Trade Score:</strong> 综合 Edge + 市场强度 + 博彩公司共识 + 风险过滤 → A/B/C/D 等级</p>
<p><strong>核心逻辑:</strong> 不预测比赛结果, 而是识别市场定价错误。</p>
</div>
<p style="color:#999;font-size:12px">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</body></html>"""
        with open(out / "value_edge_analysis.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"    value_edge_analysis.html")

    def _write_summary_html(self, out):
        """生成综合摘要 HTML 报告。"""
        r = self.report
        total = r.get("total_matches", 0)
        strength = r.get("market_strength", {})
        value = r.get("value_engine", {})
        trade = r.get("trade_scoring", {})
        risk = r.get("risk_control", {})
        behavior = r.get("behavior_labels", {})
        disagreement = r.get("disagreement", {})
        validation = r.get("validation", {})
        movement = r.get("line_movement", {})

        def _kv_table(data, title):
            if not data or "error" in str(data):
                return f"<p>{data}</p>"
            rows = ""
            for k, v in data.items():
                if isinstance(v, dict):
                    continue
                rows += f"<tr><td>{k}</td><td>{v}</td></tr>"
            return f"<h3>{title}</h3><table>{rows}</table>"

        # 博彩公司对比表
        bk_table = ""
        bk_comp = disagreement.get("bookmaker_comparison", {})
        if bk_comp:
            bk_table = "<h3>博彩公司对比</h3><table><tr><th>公司</th><th>样本数</th><th>盘口均值</th><th>盘口Std</th><th>高水均值</th><th>低水均值</th></tr>"
            for bk, stats in bk_comp.items():
                bk_table += f"<tr><td>{bk}</td><td>{stats.get('n','')}</td><td>{stats.get('line_mean','')}</td><td>{stats.get('line_std','')}</td><td>{stats.get('hw_mean','')}</td><td>{stats.get('lw_mean','')}</td></tr>"
            bk_table += "</table>"

        # 回测验证表
        val_table = ""
        if validation and "error" not in validation:
            val_table = "<h3>Trade Score 胜率验证</h3><table><tr><th>等级</th><th>信号数</th><th>胜场</th><th>胜率</th></tr>"
            for score in ["A", "B", "C", "D"]:
                v = validation.get(score, {})
                val_table += f"<tr><td>{score}级</td><td>{v.get('n_signals',0)}</td><td>{v.get('n_wins',0)}</td><td>{v.get('win_rate','N/A')}%</td></tr>"
            val_table += "</table>"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>亚洲盘市场微结构综合报告</title>
<style>
body{{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5;max-width:1200px}}
h1{{color:#1a1a2e}}h2{{color:#16213e;border-bottom:2px solid #0f3460;padding-bottom:5px;margin-top:30px}}
h3{{color:#333;margin-top:15px}}
.card{{background:white;border-radius:8px;padding:15px;margin:10px 0;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
.stats{{display:flex;gap:15px;flex-wrap:wrap}}
.stat{{background:linear-gradient(135deg,#0f3460,#16213e);color:white;padding:15px;border-radius:8px;min-width:100px;text-align:center}}
.stat.green{{background:linear-gradient(135deg,#1a8a4a,#0d5e2e)}}
.stat.orange{{background:linear-gradient(135deg,#c75b1a,#8b3a0a)}}
.stat.red{{background:linear-gradient(135deg,#b53030,#7a1a1a)}}
.stat .value{{font-size:24px;font-weight:bold}}.stat .label{{font-size:11px;opacity:0.85;margin-top:3px}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:14px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left}}
th{{background:#0f3460;color:white}}tr:nth-child(even){{background:#f8f8f8}}
.key-finding{{background:#e8f5e9;border-left:4px solid #4caf50;padding:12px;margin:10px 0;border-radius:4px}}
.limitation{{background:#fff3cd;border-left:4px solid #ffc107;padding:12px;margin:10px 0;border-radius:4px}}
.recommendation{{background:#e3f2fd;border-left:4px solid #2196f3;padding:12px;margin:10px 0;border-radius:4px}}
</style></head>
<body>
<h1>亚洲盘市场微结构引擎 — 综合报告</h1>
<p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Total matches: {total:,}</p>

<div class="card">
<h2>核心指标</h2>
<div class="stats">
<div class="stat"><div class="value">{value.get('fair_vs_actual_corr', 'N/A')}</div><div class="label">公允vs实际盘口相关性</div></div>
<div class="stat green"><div class="value">{value.get('mean_edge_pct', 'N/A')}%</div><div class="label">平均Edge</div></div>
<div class="stat"><div class="value">{strength.get('mean_score', 'N/A')}</div><div class="label">平均强度评分</div></div>
<div class="stat orange"><div class="value">{risk.get('flag_rate', 'N/A')}%</div><div class="label">风险标记率</div></div>
<div class="stat green"><div class="value">{trade.get('A_rate', 'N/A')}%</div><div class="label">A级信号率</div></div>
</div>
</div>

<div class="card">
<h2>Trade Score 分布</h2>
<table><tr><th>等级</th><th>占比</th><th>含义</th></tr>
<tr><td>A级 (强价值)</td><td>{trade.get('A_rate', 'N/A')}%</td><td>Edge≥5% + 强信号 + 低分歧 + 风控通过</td></tr>
<tr><td>B级 (可下注)</td><td>{trade.get('B_rate', 'N/A')}%</td><td>Edge≥3% + 信号确认 + 风控通过</td></tr>
<tr><td>C级 (观望)</td><td>{trade.get('C_rate', 'N/A')}%</td><td>Edge≥1% + 基础强度</td></tr>
<tr><td>D级 (放弃)</td><td>{trade.get('D_rate', 'N/A')}%</td><td>无信号或风险过高</td></tr>
</table>
</div>

{val_table}

<div class="card">
{bk_table}
</div>

<div class="card">
<h2>盘口变化类型分布</h2>
<p>{movement}</p>
</div>

<div class="card">
<h2>行为标签分布</h2>
<p>{behavior.get('label_distribution', {})}</p>
<p>标签覆盖率: {behavior.get('label_rate', 'N/A')}%</p>
</div>

<div class="card">
<h2>风险过滤统计</h2>
<p>总标记: {risk.get('total_flagged', 'N/A')} ({risk.get('flag_rate', 'N/A')}%)</p>
<p>{risk.get('risk_breakdown', [])}</p>
</div>

<div class="key-finding">
<strong>核心发现:</strong> 通过欧赔推导公允盘口 vs 实际亚盘盘口的差异, 识别市场定价错误机会。
</div>

<div class="limitation">
<strong>数据局限:</strong> 当前仅有开盘/收盘两个快照。<br>
- 无法进行真实的 24h/6h/1h/30min 时间序列盘口分析<br>
- 缺少中间时刻的盘口变动数据 (odds_history 表中有存储但尚未提取)<br>
- Water 分析受限于仅有 Bet365 终盘水位
</div>

<div class="recommendation">
<strong>下一步建议:</strong><br>
1. 提取 odds_history 表的时间序列数据, 实现真实的多时间维度分析<br>
2. 使用 William Hill (水位范围最宽[0.50,1.50]) 的水位作为可执行价格<br>
3. 对 A/B 级信号进行 Walk Forward 回测验证 ROI<br>
4. 构建实时盘口监控, 在临场发现定价错误
</div>

</body></html>"""
        with open(out / "market_microstructure_summary.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"    market_microstructure_summary.html")

    # ═══════════════════════════════════════════════════════════════
    # 主流程
    # ═══════════════════════════════════════════════════════════════

    def run_all(self):
        """运行完整分析流程。"""
        print("=" * 60)
        print("亚洲盘市场微结构引擎 v1.0")
        print("Asian Market Microstructure Engine")
        print("=" * 60)

        self.load_data()

        if self.df is None or len(self.df) == 0:
            print("[错误] 无数据, 退出")
            return

        self.analyze_line_movement()
        self.score_market_strength()
        self.analyze_disagreement()
        self.label_behaviors()
        self.analyze_movement_timing()
        self.compute_value_edge()
        self.score_trades()
        self.apply_risk_controls()
        self.validate_with_backtest()
        self.generate_reports()

        print(f"\n[完成] 所有报告已输出到: {self.output_dir}")
        print("=" * 60)


# ── CLI 入口 ──

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="亚洲盘市场微结构引擎")
    parser.add_argument("--data-dir", default="datasets", help="数据集目录")
    from config.paths import DB_FOOTBALL_HISTORY
    parser.add_argument("--db", default=str(DB_FOOTBALL_HISTORY), help="SQLite 数据库路径")
    parser.add_argument("--output", default="reports/asian_microstructure", help="输出目录")
    args = parser.parse_args()

    # 处理运行目录: 可能从 backend/market/ 或项目根目录运行
    if not Path(args.data_dir).exists():
        args.data_dir = str(Path(__file__).parent.parent.parent / "datasets")
    if not Path(args.db).exists():
        args.db = str(DB_FOOTBALL_HISTORY)
    if not Path(args.output).exists():
        args.output = str(Path(__file__).parent.parent.parent / "reports" / "asian_microstructure")

    engine = AsianMicrostructureEngine(
        data_dir=args.data_dir,
        db_path=args.db,
        output_dir=args.output,
    )
    engine.run_all()
