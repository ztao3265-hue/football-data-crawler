#!/usr/bin/env python3
"""
字段语义审计系统 (Field Semantic Audit)

彻底验证数据库中每个赔率/盘口字段的真实含义:
- high_water / low_water 是否对应 上盘/下盘?
- 投注代码是否用对了水位字段?
- 盘口方向跟随策略是否就是模型的全部?
- 水位分布是否符合真实博彩市场?
"""

import json
import os
import sqlite3
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "football_history.db"
DATASETS_DIR = PROJECT_ROOT / "datasets"
REPORTS_DIR = PROJECT_ROOT / "reports"
AUDIT_DIR = REPORTS_DIR / "semantic_audit"

warnings.filterwarnings("ignore")


def parse_asian_handicap(text: str) -> Optional[float]:
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
        "平手": 0.0, "平手/半球": 0.25, "半球": 0.5,
        "半球/一球": 0.75, "一球": 1.0, "一球/球半": 1.25,
        "球半": 1.5, "球半/两球": 1.75, "两球": 2.0,
        "两球/两球半": 2.25, "两球半": 2.5, "两球半/三球": 2.75,
        "三球": 3.0, "三球/三球半": 3.25, "三球半": 3.5,
        "三球半/四球": 3.75, "四球": 4.0,
    }
    val = mapping.get(text)
    return sign * val if val is not None else None


class FieldSemanticAuditor:
    """字段语义审计 — 逐字段验证数据含义与博彩逻辑一致性。"""

    def __init__(self):
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH))
        self.conn.row_factory = sqlite3.Row
        self.issues: List[dict] = []
        self.findings: dict = {}

    def run(self):
        print("=" * 60)
        print("字段语义审计系统 (Field Semantic Audit) v1.0")
        print("=" * 60)

        self.audit_asian_water()
        self.audit_handicap_direction()
        self.audit_water_distribution()
        self.audit_odds_parsing()
        self.audit_raw_data_sample()
        self.audit_betting_logic()
        self.detect_anomalies()
        self.generate_reports()

        print(f"\n[OK] 字段语义审计完成 → {AUDIT_DIR}")
        return self.findings

    # ═══════════════════════════════════════════════════════════════
    # 1. 亚盘水位字段语义审计
    # ═══════════════════════════════════════════════════════════════

    def audit_asian_water(self):
        """验证 high_water / low_water 字段的真实含义。"""
        print("\n[1/7] 亚盘水位字段语义审计...")

        # 加载所有亚盘赔率数据
        df = pd.read_sql("""
            SELECT oa.*, m.home_score, m.away_score, m.home_team, m.away_team
            FROM odds_asian oa
            JOIN matches m ON oa.match_id = m.match_id
            WHERE oa.odds_type = 'closing'
              AND oa.high_water IS NOT NULL
              AND oa.low_water IS NOT NULL
              AND oa.handicap IS NOT NULL
        """, self.conn)

        df["asian_line"] = df["handicap"].apply(parse_asian_handicap)
        df = df.dropna(subset=["asian_line"])
        df["goal_diff"] = df["home_score"] - df["away_score"]

        # 计算盘口结果
        df["effective"] = df["goal_diff"] + df["asian_line"]
        df["home_covers"] = (df["effective"] > 0).astype(int)
        df["away_covers"] = (df["effective"] < 0).astype(int)
        df["push"] = (abs(df["effective"]) < 0.01).astype(int)

        n = len(df)
        print(f"  有效样本: {n:,} 条 (含比分)")

        # ── 关键验证 1: high_water 是否始终 >= low_water? ──
        hw_ge_lw = (df["high_water"] >= df["low_water"]).sum()
        hw_gt_lw = (df["high_water"] > df["low_water"]).sum()
        hw_eq_lw = (abs(df["high_water"] - df["low_water"]) < 0.01).sum()
        hw_lt_lw = (df["high_water"] < df["low_water"]).sum()

        print(f"\n  [验证1] high_water vs low_water 大小关系:")
        print(f"    high >= low: {hw_ge_lw:,} ({hw_ge_lw/n:.1%})")
        print(f"    high >  low: {hw_gt_lw:,} ({hw_gt_lw/n:.1%})")
        print(f"    high == low: {hw_eq_lw:,} ({hw_eq_lw/n:.1%})")
        print(f"    high <  low: {hw_lt_lw:,} ({hw_lt_lw/n:.1%})")

        if hw_lt_lw / n > 0.5:
            self.issues.append({
                "severity": "CRITICAL",
                "field": "asian_water",
                "detail": f"high_water < low_water 占比 {hw_lt_lw/n:.1%} > 50% — 字段含义可能相反!",
            })

        # ── 关键验证 2: high_water 对应下盘(underdog), low_water 对应上盘(favorite) ──
        # 对于 asian_line > 0 (主队让球, 主队=上盘=favorite):
        #   - 上盘(主队)水位 = low_water
        #   - 下盘(客队)水位 = high_water
        # 对于 asian_line < 0 (主队受让, 主队=下盘=underdog):
        #   - 上盘(客队)水位 = low_water
        #   - 下盘(主队)水位 = high_water

        home_fav = df[df["asian_line"] > 0]   # 主队让球
        home_dog = df[df["asian_line"] < 0]   # 主队受让
        neutral = df[df["asian_line"] == 0]   # 平手盘

        print(f"\n  [验证2] 水位与盘口方向的关系:")
        print(f"    主队让球(fav): {len(home_fav):,} 场")
        print(f"    主队受让(dog):  {len(home_dog):,} 场")
        print(f"    平手盘:         {len(neutral):,} 场")

        # 主队让球时：high_water 应该是客队(下盘)水位, low_water 是主队(上盘)水位
        if len(home_fav) > 0:
            # 主队赢盘时: asian_line>0 且 effective>0
            home_cover_fav = home_fav[home_fav["home_covers"] == 1]
            away_cover_fav = home_fav[home_fav["away_covers"] == 1]

            print(f"    主队让球时:")
            print(f"      主队赢盘: {len(home_cover_fav):,} 场, 低水均值={home_cover_fav['low_water'].mean():.3f}, 高水均值={home_cover_fav['high_water'].mean():.3f}")
            print(f"      客队赢盘: {len(away_cover_fav):,} 场, 低水均值={away_cover_fav['low_water'].mean():.3f}, 高水均值={away_cover_fav['high_water'].mean():.3f}")

        if len(home_dog) > 0:
            home_cover_dog = home_dog[home_dog["home_covers"] == 1]
            away_cover_dog = home_dog[home_dog["away_covers"] == 1]
            print(f"    主队受让时:")
            print(f"      主队赢盘: {len(home_cover_dog):,} 场, 低水均值={home_cover_dog['low_water'].mean():.3f}, 高水均值={home_cover_dog['high_water'].mean():.3f}")
            print(f"      客队赢盘: {len(away_cover_dog):,} 场, 低水均值={away_cover_dog['low_water'].mean():.3f}, 高水均值={away_cover_dog['high_water'].mean():.3f}")

        # ── 关键验证 3: 如果始终用 high_water 投主赢盘, 用 low_water 投客赢盘 ──
        print(f"\n  [验证3] 当前投注逻辑的水位使用:")
        print(f"    当前: pred=主赢盘 → 使用 high_water")
        print(f"    当前: pred=客赢盘 → 使用 low_water")

        # 模拟当前投注逻辑 (可能是错误的)
        curr_profit = 0
        for _, row in df.iterrows():
            line = row["asian_line"]
            hw = row["high_water"]
            lw = row["low_water"]
            eff = row["effective"]

            if abs(eff) < 0.01:  # push
                continue

            if line > 0:
                # 主队让球: 主=上盘, 客=下盘
                # 投主赢盘 → 应该用 low_water (上盘水位)
                # 但当前代码用 high_water...
                if eff > 0:  # 主赢盘
                    curr_profit += 100 * hw  # 当前: 用了 high_water (下盘水位)
                else:
                    curr_profit -= 100
            else:
                # 主队受让: 主=下盘, 客=上盘
                # 投客赢盘 → 应该用 low_water (上盘水位)
                # 但当前代码用 low_water... 这个可能正确?
                if eff < 0:  # 客赢盘
                    curr_profit += 100 * lw  # 当前: 用了 low_water
                else:
                    curr_profit -= 100

        # 纠正后的投注逻辑
        corr_profit = 0
        for _, row in df.iterrows():
            line = row["asian_line"]
            hw = row["high_water"]
            lw = row["low_water"]
            eff = row["effective"]

            if abs(eff) < 0.01:
                continue

            if line > 0:
                # 主队让球: 主=上盘=low, 客=下盘=high
                if eff > 0:  # 主赢盘, 投上盘
                    corr_profit += 100 * lw  # 改正: 用 low_water
                else:  # 客赢盘, 投下盘
                    corr_profit += 100 * hw  # 改正: 用 high_water
            else:
                # 主队受让: 主=下盘=high, 客=上盘=low
                if eff > 0:  # 主赢盘, 投下盘
                    corr_profit += 100 * hw  # 改正: 用 high_water
                else:  # 客赢盘, 投上盘
                    corr_profit += 100 * lw  # 改正: 用 low_water

        n_bets = len(df[abs(df["effective"]) >= 0.01])
        curr_roi = curr_profit / (n_bets * 100) if n_bets > 0 else 0
        corr_roi = corr_profit / (n_bets * 100) if n_bets > 0 else 0

        print(f"    当前逻辑ROI: {curr_roi:+.2%}")
        print(f"    纠正逻辑ROI: {corr_roi:+.2%}")

        self.findings["asian_water"] = {
            "n_samples": n,
            "high_ge_low_pct": round(hw_ge_lw / n, 4),
            "high_eq_low_pct": round(hw_eq_lw / n, 4),
            "current_logic_roi": round(curr_roi, 4),
            "corrected_logic_roi": round(corr_roi, 4),
            "water_fix_impact": round(corr_roi - curr_roi, 4),
        }

    # ═══════════════════════════════════════════════════════════════
    # 2. 盘口方向审计
    # ═══════════════════════════════════════════════════════════════

    def audit_handicap_direction(self):
        """验证盘口方向策略与模型预测的关系。"""
        print("\n[2/7] 盘口方向审计...")

        # 加载投注数据 + 原始盘口
        bets_dir = PROJECT_ROOT / "reports" / "backtest"
        bet_files = sorted(bets_dir.glob("bets_asian_*.csv"))
        if not bet_files:
            print("  未找到投注CSV文件")
            return

        # 加载含盘口的原始数据
        full = pd.concat([
            pd.read_csv(DATASETS_DIR / "train.csv"),
            pd.read_csv(DATASETS_DIR / "validation.csv"),
            pd.read_csv(DATASETS_DIR / "test.csv"),
        ], ignore_index=True)

        results = {}
        for bf in bet_files:
            model_name = bf.stem.replace("bets_asian_", "")
            bets = pd.read_csv(bf)
            if bets.empty:
                continue

            # merge with asian_close_line
            if "asian_close_line" in full.columns:
                orig = full[["match_id", "asian_close_line"]].drop_duplicates("match_id")
                bets = bets.merge(orig, on="match_id", how="left")

            if "asian_close_line" not in bets.columns:
                continue

            bets["line_sign"] = np.sign(bets["asian_close_line"].fillna(0))

            # 统计预测与盘口方向的一致性
            # 盘口>0: "正确"方向=主赢盘(2), "错误"方向=客赢盘(0)
            # 盘口<0: "正确"方向=客赢盘(0), "错误"方向=主赢盘(2)
            n_follow = 0
            n_contrarian = 0
            n_skip = 0
            follow_correct = 0
            contrarian_correct = 0

            for _, row in bets.iterrows():
                line = row.get("asian_close_line", 0)
                if pd.isna(line):
                    n_skip += 1
                    continue
                pred = int(row["y_pred"])
                actual = int(row["y_true"])

                if line > 0:
                    follow_pred = 2  # 跟随: 主赢盘
                    contrarian_pred = 0  # 逆势: 客赢盘
                elif line < 0:
                    follow_pred = 0  # 跟随: 客赢盘
                    contrarian_pred = 2  # 逆势: 主赢盘
                else:
                    n_skip += 1
                    continue

                # 模型是否跟随盘口?
                if pred == follow_pred:
                    n_follow += 1
                    if pred == actual:
                        follow_correct += 1
                elif pred == contrarian_pred:
                    n_contrarian += 1
                    if pred == actual:
                        contrarian_correct += 1
                else:
                    n_skip += 1

            n_valid = n_follow + n_contrarian
            follow_rate = n_follow / n_valid if n_valid > 0 else 0
            follow_wr = follow_correct / n_follow if n_follow > 0 else 0
            contrarian_wr = contrarian_correct / n_contrarian if n_contrarian > 0 else 0

            print(f"  [{model_name}]")
            print(f"    跟随盘口: {n_follow}/{n_valid} ({follow_rate:.1%}) 胜率={follow_wr:.1%}")
            print(f"    逆盘口:   {n_contrarian}/{n_valid} ({1-follow_rate:.1%}) 胜率={contrarian_wr:.1%}")

            if follow_rate > 0.95:
                self.issues.append({
                    "severity": "CRITICAL",
                    "field": "model_strategy",
                    "detail": f"[{model_name}] 模型预测与盘口方向一致率={follow_rate:.1%} > 95% — 模型无独立判断能力!",
                })

            results[model_name] = {
                "follow_rate": round(follow_rate, 4),
                "follow_winrate": round(follow_wr, 4),
                "contrarian_winrate": round(contrarian_wr, 4),
                "n_follow": n_follow,
                "n_contrarian": n_contrarian,
            }

        self.findings["handicap_direction"] = results

    # ═══════════════════════════════════════════════════════════════
    # 3. 水位分布审计
    # ═══════════════════════════════════════════════════════════════

    def audit_water_distribution(self):
        """验证水位分布是否符合真实博彩市场。"""
        print("\n[3/7] 水位分布审计...")

        # 加载亚盘数据
        asian_df = pd.read_sql("""
            SELECT high_water, low_water, handicap, bookmaker, odds_type
            FROM odds_asian
            WHERE high_water IS NOT NULL AND low_water IS NOT NULL
        """, self.conn)

        ou_df = pd.read_sql("""
            SELECT over_water, under_water, handicap, bookmaker, odds_type
            FROM odds_over_under
            WHERE over_water IS NOT NULL AND under_water IS NOT NULL
        """, self.conn)

        # 亚盘水位分布
        asian_stats = {}
        for col in ["high_water", "low_water"]:
            s = asian_df[col].dropna()
            asian_stats[col] = {
                "count": len(s),
                "min": round(float(s.min()), 4),
                "max": round(float(s.max()), 4),
                "mean": round(float(s.mean()), 4),
                "std": round(float(s.std()), 4),
                "median": round(float(s.median()), 4),
                "p5": round(float(s.quantile(0.05)), 4),
                "p95": round(float(s.quantile(0.95)), 4),
                "unique_values": int(s.nunique()),
            }

        # 水位异常检测
        hw_narrow = (asian_df["high_water"].between(0.90, 1.00)).mean()
        lw_narrow = (asian_df["low_water"].between(0.90, 1.00)).mean()
        hw_fixed = (asian_df["high_water"].nunique() / len(asian_df))

        print(f"  [亚盘水位]")
        for col, stats in asian_stats.items():
            print(f"    {col}: range=[{stats['min']:.4f}, {stats['max']:.4f}] "
                  f"mean={stats['mean']:.4f} std={stats['std']:.4f} "
                  f"unique={stats['unique_values']}")
        print(f"    high_water 在 0.90-1.00: {hw_narrow:.1%}")
        print(f"    low_water  在 0.90-1.00: {lw_narrow:.1%}")
        print(f"    水位唯一值占比: {hw_fixed:.3f}")

        if hw_narrow > 0.80:
            self.issues.append({
                "severity": "WARNING",
                "field": "high_water",
                "detail": f"high_water 80%+ 集中在 0.90-1.00 — 水位缺乏区分度",
            })

        # O/U 水位分布
        ou_stats = {}
        for col in ["over_water", "under_water"]:
            s = ou_df[col].dropna()
            ou_stats[col] = {
                "count": len(s),
                "min": round(float(s.min()), 4),
                "max": round(float(s.max()), 4),
                "mean": round(float(s.mean()), 4),
                "std": round(float(s.std()), 4),
            }

        print(f"\n  [大小球水位]")
        for col, stats in ou_stats.items():
            print(f"    {col}: range=[{stats['min']:.4f}, {stats['max']:.4f}] "
                  f"mean={stats['mean']:.4f} std={stats['std']:.4f}")

        # 按博彩公司分解水位
        print(f"\n  [按博彩公司分解水位]")
        for bk in asian_df["bookmaker"].dropna().unique():
            bk_data = asian_df[asian_df["bookmaker"] == bk]
            if len(bk_data) < 10:
                continue
            print(f"    {bk}: n={len(bk_data):,} "
                  f"high_water_mean={bk_data['high_water'].mean():.3f} "
                  f"low_water_mean={bk_data['low_water'].mean():.3f}")

        # 按联赛分解 (需要 join)
        league_df = pd.read_sql("""
            SELECT oa.high_water, oa.low_water, l.code as league_code
            FROM odds_asian oa
            JOIN matches m ON oa.match_id = m.match_id
            JOIN leagues l ON m.league_id = l.id
            WHERE oa.high_water IS NOT NULL AND oa.low_water IS NOT NULL
        """, self.conn)

        if len(league_df) > 0:
            print(f"\n  [按联赛分解水位]")
            for lg, grp in league_df.groupby("league_code"):
                if len(grp) < 30:
                    continue
                print(f"    {lg}: n={len(grp):,} "
                      f"high_mean={grp['high_water'].mean():.3f} "
                      f"low_mean={grp['low_water'].mean():.3f}")

        self.findings["water_distribution"] = {
            "asian": asian_stats,
            "over_under": ou_stats,
            "high_water_narrow_pct": round(hw_narrow, 4),
            "low_water_narrow_pct": round(lw_narrow, 4),
        }

    # ═══════════════════════════════════════════════════════════════
    # 4. 赔率解析逻辑审计
    # ═══════════════════════════════════════════════════════════════

    def audit_odds_parsing(self):
        """验证港赔→欧赔转换、隐含概率等逻辑。"""
        print("\n[4/7] 赔率解析逻辑审计...")

        # 1. 港赔→欧赔转换: decimal = water_hk + 1.0
        asian_df = pd.read_sql("""
            SELECT high_water, low_water, handicap FROM odds_asian
            WHERE high_water IS NOT NULL AND low_water IS NOT NULL
        """, self.conn)

        # 港赔特征: 水位water = 利润倍数, 通常在 0.6-1.5
        # 欧赔特征: odds > 1.0, 通常在 1.5-2.5
        # 如果 water + 1.0 ≈ 正常的欧赔范围(1.6-2.5), 说明转换正确

        hw_plus1 = (asian_df["high_water"] + 1.0).dropna()
        lw_plus1 = (asian_df["low_water"] + 1.0).dropna()

        print("  [港赔→欧赔转换验证]")
        print(f"    high_water + 1.0: range=[{hw_plus1.min():.2f}, {hw_plus1.max():.2f}] mean={hw_plus1.mean():.2f}")
        print(f"    low_water + 1.0:  range=[{lw_plus1.min():.2f}, {lw_plus1.max():.2f}] mean={lw_plus1.mean():.2f}")
        print(f"    如果 water+1.0 在 1.6-2.5 范围 → 港赔转换正确")
        print(f"    如果 water+1.0 接近 2.0 → 这就是正常的欧赔")

        # 2. 检查欧赔数据 (用于对比验证)
        euro_df = pd.read_sql("""
            SELECT odds_home, odds_draw, odds_away, bookmaker, odds_type
            FROM odds_europe
            WHERE odds_home IS NOT NULL
        """, self.conn)

        if len(euro_df) > 0:
            print(f"\n  [欧赔数据对照]")
            for col in ["odds_home", "odds_draw", "odds_away"]:
                s = euro_df[col].dropna()
                print(f"    {col}: range=[{s.min():.2f}, {s.max():.2f}] mean={s.mean():.2f}")

        # 3. 隐含概率验证
        euro_closing = euro_df[euro_df["odds_type"] == "closing"].dropna(
            subset=["odds_home", "odds_draw", "odds_away"]
        )
        if len(euro_closing) > 0:
            inv_sum = 1/euro_closing["odds_home"] + 1/euro_closing["odds_draw"] + 1/euro_closing["odds_away"]
            margin = inv_sum - 1.0
            print(f"\n  [博彩公司Margin验证]")
            print(f"    平均 margin: {margin.mean():.2%}")
            print(f"    正常范围: 2-8%")
            if margin.mean() < 0.01:
                self.issues.append({
                    "severity": "WARNING",
                    "field": "europe_odds",
                    "detail": f"欧赔margin={margin.mean():.2%} 异常低, 赔率可能存在精度问题",
                })

        self.findings["odds_parsing"] = {
            "water_plus_1_range": f"[{hw_plus1.min():.2f}, {hw_plus1.max():.2f}]",
            "water_plus_1_mean": round(float(hw_plus1.mean()), 2),
            "euro_margin_mean": round(float(margin.mean()), 4) if len(euro_closing) > 0 else None,
        }

    # ═══════════════════════════════════════════════════════════════
    # 5. 原始数据抽样对照
    # ═══════════════════════════════════════════════════════════════

    def audit_raw_data_sample(self):
        """随机抽取比赛, 对照数据库记录与原始爬虫数据。"""
        print("\n[5/7] 原始数据抽样对照...")

        # 从数据库随机抽样100场
        sample = pd.read_sql("""
            SELECT m.match_id, m.home_team, m.away_team, m.home_score, m.away_score,
                   m.kickoff_time, l.name_cn as league_name,
                   oa.high_water, oa.handicap as asian_handicap, oa.low_water,
                   oe.odds_home, oe.odds_draw, oe.odds_away,
                   ou.over_water, ou.handicap as ou_handicap, ou.under_water
            FROM matches m
            JOIN leagues l ON m.league_id = l.id
            LEFT JOIN odds_asian oa ON m.match_id = oa.match_id AND oa.odds_type = 'closing' AND oa.bookmaker = 'Bet365'
            LEFT JOIN odds_europe oe ON m.match_id = oe.match_id AND oe.odds_type = 'closing' AND oe.bookmaker = 'Bet365'
            LEFT JOIN odds_over_under ou ON m.match_id = ou.match_id AND ou.odds_type = 'closing' AND ou.bookmaker = 'Bet365'
            WHERE oa.high_water IS NOT NULL
            ORDER BY RANDOM()
            LIMIT 100
        """, self.conn)

        # 尝试从原始 JSON 文件中匹配 (如果存在)
        raw_records = []
        for raw_file in (PROJECT_ROOT / "data" / "raw").glob("sofascore_*.json"):
            try:
                with open(raw_file, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                if isinstance(raw_data, list):
                    raw_records.extend(raw_data)
            except Exception:
                pass

        n_matched = 0
        sample_rows = []
        for _, row in sample.iterrows():
            mid = row["match_id"]
            sr = {
                "match_id": mid,
                "league": row["league_name"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "score": f"{int(row['home_score'])}-{int(row['away_score'])}" if pd.notna(row["home_score"]) else "N/A",
                "asian_handicap_db": row["asian_handicap"],
                "high_water_db": row["high_water"],
                "low_water_db": row["low_water"],
                "odds_home_db": row["odds_home"],
                "odds_draw_db": row["odds_draw"],
                "odds_away_db": row["odds_away"],
                "ou_handicap_db": row["ou_handicap"],
                "over_water_db": row["over_water"],
                "under_water_db": row["under_water"],
            }

            # 尝试匹配原始JSON
            for rec in raw_records:
                if str(rec.get("match_id", "")) == mid:
                    sr["raw_source"] = rec.get("source", "")
                    sr["raw_odds_home"] = rec.get("odds_home", "")
                    sr["raw_asian"] = rec.get("asian_handicap", "")
                    n_matched += 1
                    break
            sample_rows.append(sr)

        pd.DataFrame(sample_rows).to_csv(
            AUDIT_DIR / "raw_data_comparison.csv", index=False, encoding="utf-8-sig"
        )
        print(f"  抽样: {len(sample)} 场 → raw_data_comparison.csv")
        print(f"  成功匹配原始JSON: {n_matched}/{len(sample)}")

        self.findings["raw_sample"] = {
            "n_sampled": len(sample),
            "n_matched_raw": n_matched,
        }

    # ═══════════════════════════════════════════════════════════════
    # 6. 投注逻辑完整审计
    # ═══════════════════════════════════════════════════════════════

    def audit_betting_logic(self):
        """完整审计投注逻辑: 水位选择是否正确。"""
        print("\n[6/7] 投注逻辑完整审计...")

        # 加载所有完整比赛数据
        full = pd.concat([
            pd.read_csv(DATASETS_DIR / "train.csv"),
            pd.read_csv(DATASETS_DIR / "validation.csv"),
            pd.read_csv(DATASETS_DIR / "test.csv"),
        ], ignore_index=True)
        full = full.dropna(subset=["label_asian", "asian_close_line",
                                    "asian_close_high_water", "asian_close_low_water"])
        full["label_mapped"] = full["label_asian"].map({-1.0: 0, 0.0: 1, 1.0: 2})

        # 策略1: "跟随盘口" (简单规则)
        # 盘口>0 → 预测主赢盘(2), 盘口<0 → 预测客赢盘(0)
        def follow_handicap(row):
            line = row["asian_close_line"]
            if line > 0:
                return 2
            elif line < 0:
                return 0
            return -1

        full["follow_pred"] = full.apply(follow_handicap, axis=1)

        # 策略2: "逆盘口"
        def contrarian(row):
            line = row["asian_close_line"]
            if line > 0:
                return 0
            elif line < 0:
                return 2
            return -1

        full["contrarian_pred"] = full.apply(contrarian, axis=1)

        # 分别用两种水位选择逻辑计算 ROI
        results = {}

        for strat_name, pred_col in [("跟随盘口", "follow_pred"), ("逆盘口", "contrarian_pred")]:
            for water_logic in ["current", "corrected"]:
                label = f"{strat_name}_{water_logic}"
                profit = 0
                n_bets = 0
                wins = 0
                losses = 0
                pushes = 0

                for _, row in full.iterrows():
                    pred = int(row[pred_col])
                    actual = int(row["label_mapped"])
                    line = row["asian_close_line"]
                    hw = row["asian_close_high_water"]
                    lw = row["asian_close_low_water"]

                    if pred == -1 or actual == 1:  # skip neutral predictions or push results
                        continue
                    if pd.isna(hw) or pd.isna(lw) or hw <= 0 or lw <= 0:
                        continue

                    n_bets += 1

                    # 选择水位
                    if water_logic == "current":
                        # 当前逻辑: 主赢盘→high_water, 客赢盘→low_water
                        water = hw if pred == 2 else lw
                    else:
                        # 纠正逻辑: 根据盘口方向选择
                        if line > 0:  # 主队让球: 上盘=主队, 下盘=客队
                            water = lw if pred == 2 else hw
                        else:  # 主队受让: 上盘=客队, 下盘=主队
                            water = hw if pred == 2 else lw

                    if pred == actual:
                        profit += 100 * water
                        wins += 1
                    else:
                        profit += -100
                        losses += 1

                roi = profit / (n_bets * 100) if n_bets > 0 else 0
                wr = wins / (wins + losses) if (wins + losses) > 0 else 0

                results[label] = {
                    "roi": round(roi, 4),
                    "win_rate": round(wr, 4),
                    "n_bets": n_bets,
                    "wins": wins,
                    "losses": losses,
                    "total_profit": round(profit, 2),
                }

        print("  策略ROI对比:")
        print(f"    {'策略':20s} {'水位逻辑':12s} {'ROI':>10s} {'胜率':>8s} {'投注数':>8s}")
        print(f"    {'-'*60}")
        for label, r in results.items():
            strat, logic = label.rsplit("_", 1)
            print(f"    {strat:20s} {logic:12s} {r['roi']:>9.2%} {r['win_rate']:>7.1%} {r['n_bets']:>8d}")

        self.findings["betting_logic"] = results

        # 如果当前水位逻辑ROI接近"纠正后"的ROI → 水位逻辑可能没问题
        # 如果跟随盘口+当前逻辑ROI极高 → 确认是盘口方向偏差
        follow_curr = results.get("跟随盘口_current", {}).get("roi", 0)
        follow_corr = results.get("跟随盘口_corrected", {}).get("roi", 0)

        if follow_curr > 0.50:
            self.issues.append({
                "severity": "CRITICAL",
                "field": "betting_strategy",
                "detail": f"跟随盘口+当前水位逻辑 ROI={follow_curr:.1%} — 盘口方向本身就构成了套利策略",
            })

        if abs(follow_curr - follow_corr) < 0.02:
            self.issues.append({
                "severity": "INFO",
                "field": "water_assignment",
                "detail": f"当前水位逻辑 vs 纠正逻辑 ROI差={abs(follow_curr-follow_corr):.1%} — 水位字段语义可能正确但影响不大",
            })

    # ═══════════════════════════════════════════════════════════════
    # 7. 异常检测
    # ═══════════════════════════════════════════════════════════════

    def detect_anomalies(self):
        """汇总所有异常。"""
        print("\n[7/7] 异常检测汇总...")

        if not self.issues:
            print("  [OK] 未检测到异常")
            return

        for issue in sorted(self.issues, key=lambda x: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}.get(x["severity"], 3)):
            tag = f"[{issue['severity']}]"
            print(f"  {tag:12s} [{issue['field']}] {issue['detail']}")

        self.findings["anomalies"] = self.issues

    # ═══════════════════════════════════════════════════════════════
    # 报告生成
    # ═══════════════════════════════════════════════════════════════

    def generate_reports(self):
        """生成所有审计报告。"""
        print("\n生成审计报告...")

        # CSV: 完整审计结果
        summary = {
            "generated_at": datetime.now().isoformat(),
            **self.findings,
        }
        with open(AUDIT_DIR / "semantic_audit_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        print(f"  JSON: semantic_audit_summary.json")

        # HTML 报告
        html = self._build_html()
        with open(AUDIT_DIR / "semantic_audit_summary.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  HTML: semantic_audit_summary.html")

        # 水位分析 HTML
        water_html = self._build_water_html()
        with open(AUDIT_DIR / "water_analysis.html", "w", encoding="utf-8") as f:
            f.write(water_html)
        print(f"  HTML: water_analysis.html")

        # 字段分布 HTML
        field_html = self._build_field_html()
        with open(AUDIT_DIR / "field_distribution.html", "w", encoding="utf-8") as f:
            f.write(field_html)
        print(f"  HTML: field_distribution.html")

        # 市场真实性 HTML
        reality_html = self._build_reality_html()
        with open(AUDIT_DIR / "market_reality_check.html", "w", encoding="utf-8") as f:
            f.write(reality_html)
        print(f"  HTML: market_reality_check.html")

    def _build_html(self) -> str:
        ts = datetime.now().isoformat()

        # 异常表格
        anomaly_html = ""
        if self.issues:
            anomaly_html += "<table><tr><th>级别</th><th>字段</th><th>详情</th></tr>"
            for a in sorted(self.issues, key=lambda x: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}.get(x["severity"], 3)):
                color = {"CRITICAL": "#ef4444", "WARNING": "#f59e0b", "INFO": "#3b82f6"}.get(a["severity"], "#666")
                anomaly_html += (
                    f"<tr><td style='color:{color};font-weight:bold'>{a['severity']}</td>"
                    f"<td>{a['field']}</td><td>{a['detail']}</td></tr>"
                )
            anomaly_html += "</table>"
        else:
            anomaly_html = "<p style='color:#10b981'>[OK] 未检测到数据语义异常</p>"

        # 投注逻辑对比
        bl = self.findings.get("betting_logic", {})
        bl_html = "<table><tr><th>策略</th><th>水位逻辑</th><th>ROI</th><th>胜率</th><th>投注数</th></tr>"
        for label, r in bl.items():
            strat, logic = label.rsplit("_", 1)
            bl_html += (
                f"<tr><td>{strat}</td><td>{logic}</td>"
                f"<td>{r['roi']:.2%}</td><td>{r['win_rate']:.1%}</td><td>{r['n_bets']}</td></tr>"
            )
        bl_html += "</table>"

        # 盘口方向
        hd = self.findings.get("handicap_direction", {})
        hd_html = "<table><tr><th>模型</th><th>跟随盘口率</th><th>跟随胜率</th><th>逆势胜率</th></tr>"
        for model, r in hd.items():
            hd_html += (
                f"<tr><td>{model}</td><td>{r['follow_rate']:.1%}</td>"
                f"<td>{r['follow_winrate']:.1%}</td><td>{r['contrarian_winrate']:.1%}</td></tr>"
            )
        hd_html += "</table>"

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>字段语义审计报告 — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#dc2626 0%,#991b1b 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; }}
.header p {{ opacity:0.8; font-size:14px; margin-top:8px; }}
.container {{ max-width:1400px; margin:0 auto; padding:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:24px; }}
.card h2 {{ font-size:16px; color:#16213e; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #e8e8e8; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; font-weight:600; color:#555; }}
tr:hover {{ background:#f8f9fa; }}
h3 {{ font-size:14px; color:#333; margin:16px 0 8px 0; }}
pre {{ background:#1a1a2e; color:#10b981; padding:16px; border-radius:8px; font-size:13px; overflow-x:auto; }}
</style>
</head>
<body>
<div class="header">
<h1>字段语义审计报告 (Field Semantic Audit)</h1>
<p>生成: {ts[:19]} | 验证所有赔率/盘口字段的真实含义</p>
</div>
<div class="container">
<div class="card"><h2>异常检测</h2>{anomaly_html}</div>
<div class="card"><h2>投注逻辑对比 (水位选择 × 策略方向)</h2>{bl_html}</div>
<div class="card"><h2>模型 vs 盘口方向一致性</h2>{hd_html}</div>
<div class="card"><h2>审计结论</h2>
<pre>
核心问题: 亚洲盘口的盘口线方向本身就构成了一个高ROI策略。
即使不使用任何ML模型，"跟随盘口方向"的简单规则策略也能获得显著正收益。

原因分析:
1. 盘口线（如 +1.5 代表主队让1.5球）使得"受让方"赢盘概率极高
2. 水位（high_water/low_water）集中在 0.93-0.97，未按概率调整
3. 模型95%+的预测与盘口方向一致，无独立判断能力

待确认:
- high_water 是否对应下盘(受让方)水位?
- 当前投注代码是否选错了水位字段?
- 水位数据是否存在系统性问题?
</pre>
</div>
</div>
</body>
</html>"""

    def _build_water_html(self) -> str:
        ts = datetime.now().isoformat()
        wd = self.findings.get("water_distribution", {}).get("asian", {})

        rows = ""
        for col, stats in wd.items():
            rows += (
                f"<tr><td><strong>{col}</strong></td>"
                f"<td>{stats['count']:,}</td>"
                f"<td>[{stats['min']:.4f}, {stats['max']:.4f}]</td>"
                f"<td>{stats['mean']:.4f}</td>"
                f"<td>{stats['std']:.4f}</td>"
                f"<td>{stats['median']:.4f}</td>"
                f"<td>{stats['p5']:.4f} ~ {stats['p95']:.4f}</td>"
                f"<td>{stats['unique_values']}</td></tr>"
            )

        return self._wrap_html("水位分布分析报告", ts, f"""
<h3>亚盘水位统计</h3>
<table>
<tr><th>字段</th><th>样本数</th><th>范围</th><th>均值</th><th>标准差</th><th>中位数</th><th>P5-P95</th><th>唯一值数</th></tr>
{rows}
</table>

<h3>水位分布解读</h3>
<pre>
真实博彩市场的水位特征:
- 港赔水位通常在 0.65-1.50 范围
- 高概率方水位较低 (如 0.70-0.85)
- 低概率方水位较高 (如 1.00-1.20)
- 水位差异反映概率差异

当前数据特征:
- 水位集中在 0.93-0.97 窄幅区间
- 高低水位几乎无差异
- 这说明数据源的水位可能:
  a) 不是真实博彩公司的水位数据
  b) 是经过某种处理/归一化的水位
  c) 是另一种计量单位
</pre>
""")

    def _build_field_html(self) -> str:
        ts = datetime.now().isoformat()

        aw = self.findings.get("asian_water", {})
        op = self.findings.get("odds_parsing", {})

        return self._wrap_html("字段分布报告", ts, f"""
<h3>亚盘水位字段关系</h3>
<table>
<tr><th>指标</th><th>值</th></tr>
<tr><td>high >= low 占比</td><td>{aw.get('high_ge_low_pct', '-')}</td></tr>
<tr><td>high == low 占比</td><td>{aw.get('high_eq_low_pct', '-')}</td></tr>
<tr><td>当前逻辑ROI</td><td>{aw.get('current_logic_roi', '-')}</td></tr>
<tr><td>纠正逻辑ROI</td><td>{aw.get('corrected_logic_roi', '-')}</td></tr>
<tr><td>水位修正影响</td><td>{aw.get('water_fix_impact', '-')}</td></tr>
</table>

<h3>赔率解析验证</h3>
<table>
<tr><th>检查项</th><th>结果</th></tr>
<tr><td>water + 1.0 范围</td><td>{op.get('water_plus_1_range', '-')}</td></tr>
<tr><td>water + 1.0 均值</td><td>{op.get('water_plus_1_mean', '-')}</td></tr>
<tr><td>欧赔margin均值</td><td>{op.get('euro_margin_mean', '-')}</td></tr>
</table>
""")

    def _build_reality_html(self) -> str:
        ts = datetime.now().isoformat()
        bl = self.findings.get("betting_logic", {})

        rows = ""
        for label, r in bl.items():
            strat, logic = label.rsplit("_", 1)
            rows += (
                f"<tr><td>{strat}</td><td>{logic}</td>"
                f"<td style='font-weight:bold'>{r['roi']:.2%}</td>"
                f"<td>{r['win_rate']:.1%}</td>"
                f"<td>{r['n_bets']}</td>"
                f"<td>{r['total_profit']:.0f}</td></tr>"
            )

        return self._wrap_html("市场真实性验证", ts, f"""
<h3>核心验证: 策略ROI矩阵</h3>
<p>如果"跟随盘口+任何水位逻辑"的ROI都显著为正 → 盘口线本身具有预测力</p>
<p>如果"跟随盘口"ROI远高于"逆盘口" → 市场盘口定价方向正确但水位对价不足</p>
<table>
<tr><th>策略方向</th><th>水位逻辑</th><th>ROI</th><th>胜率</th><th>投注数</th><th>总利润</th></tr>
{rows}
</table>

<h3>真实性判断标准</h3>
<pre>
真实市场的特征:
  - "跟随盘口"策略长期ROI应 ≈ 0% (扣除margin后为负)
  - 水位差异应反映概率差异: 高胜率 → 低水位, 低胜率 → 高水位
  - 盘口线两侧水位之和应 > 2.0 (体现margin)

如果当前数据不满足以上条件:
  → 水位数据不是"可执行的投注赔率"
  → 当前的ROI计算基于"不可交易的价格"
  → 实际交易中无法获得这个ROI
</pre>
""")

    @staticmethod
    def _wrap_html(title: str, ts: str, content: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>{title} — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#dc2626 0%,#991b1b 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; }}
.container {{ max-width:1200px; margin:0 auto; padding:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:24px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; }}
h3 {{ font-size:14px; color:#333; margin:16px 0 8px 0; }}
pre {{ background:#1a1a2e; color:#10b981; padding:16px; border-radius:8px; font-size:13px; overflow-x:auto; }}
</style>
</head>
<body>
<div class="header"><h1>{title}</h1><p>{ts[:19]}</p></div>
<div class="container"><div class="card">{content}</div></div>
</body>
</html>"""


def main():
    try:
        FieldSemanticAuditor().run()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
