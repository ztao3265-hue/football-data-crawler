#!/usr/bin/env python3
"""
AI 特征工程系统 (Feature Engineering Engine)

从 SQLite 历史数据库生成 AI 可训练数据集。
- 欧赔特征: 隐含概率、概率变化、赔率方差、博彩公司分歧度
- 亚盘特征: 盘口、水位变化、升盘/降盘
- 大小球特征: 盘口、水位变化
- 比赛标签: 胜平负、让球胜负、大小球、爆冷
- 时间切分: 70% train / 15% val / 15% test
- 严格禁止未来数据泄露
"""

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "football_history.db"
DATASETS_DIR = PROJECT_ROOT / "datasets"
REPORTS_DIR = PROJECT_ROOT / "reports"

PRIMARY_BOOKMAKER = "Bet365"
BOOKMAKERS_EURO = ["Bet365", "Macau", "Betfair", "Crown", "Ladbrokes", "William Hill"]
BOOKMAKERS_ASIAN = ["Bet365", "Macau", "Crown", "Ladbrokes", "William Hill"]


# ── 亚盘盘口解析 ─────────────────────────────────────────────

def parse_asian_handicap(text: str) -> float | None:
    """将亚盘盘口中文字符串转为数值。

    平手=0, 平手/半球=0.25, 半球=0.5, ..., 受xxx = 负数
    """
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
        "平手": 0.0,
        "平手/半球": 0.25,
        "半球": 0.5,
        "半球/一球": 0.75,
        "一球": 1.0,
        "一球/球半": 1.25,
        "球半": 1.5,
        "球半/两球": 1.75,
        "两球": 2.0,
        "两球/两球半": 2.25,
        "两球半": 2.5,
        "两球半/三球": 2.75,
        "三球": 3.0,
        "三球/三球半": 3.25,
        "三球半": 3.5,
        "三球半/四球": 3.75,
        "四球": 4.0,
    }
    val = mapping.get(text)
    if val is not None:
        return sign * val
    return None


def parse_ou_handicap(text: str) -> float | None:
    """将大小球盘口字符串转为数值。

    "2.5球" → 2.5, "2/2.5球" → 2.25, "2.5/3球" → 2.75
    """
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


# ── 欧赔隐含概率计算 ─────────────────────────────────────────

def implied_probabilities(odds_home, odds_draw, odds_away):
    """计算去除博彩公司margin后的隐含概率。"""
    if not all([odds_home, odds_draw, odds_away]):
        return None, None, None
    if any(o <= 0 for o in [odds_home, odds_draw, odds_away]):
        return None, None, None

    inv_sum = 1 / odds_home + 1 / odds_draw + 1 / odds_away
    if inv_sum <= 0:
        return None, None, None
    return (1 / odds_home) / inv_sum, (1 / odds_draw) / inv_sum, (1 / odds_away) / inv_sum


# ── 主特征工程类 ─────────────────────────────────────────────

class FeatureEngineer:
    """AI 特征工程引擎"""

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.conn.row_factory = sqlite3.Row
        self.features = []
        self.labels = []
        self.meta = []
        self.report_data = {
            "generated_at": datetime.now().isoformat(),
            "database": str(DB_PATH),
            "features": {},
            "warnings": [],
            "stats": {},
        }

    def run_all(self):
        print("=" * 60)
        print("AI 特征工程系统 v1.0")
        print("=" * 60)

        print("\n[1/7] 加载比赛数据...")
        matches = self._load_matches()
        print(f"  加载 {len(matches)} 场比赛")

        print("[2/7] 生成欧赔特征...")
        self._build_europe_features(matches)

        print("[3/7] 生成亚盘特征...")
        self._build_asian_features(matches)

        print("[4/7] 生成大小球特征...")
        self._build_ou_features(matches)

        print("[5/7] 生成比赛标签...")
        self._build_labels(matches)

        print("[6/7] 组装数据集并时间切分...")
        df = self._assemble_dataset()
        train, val, test = self._temporal_split(df)
        print(f"  Train: {len(train):,} | Val: {len(val):,} | Test: {len(test):,}")

        print("[7/7] 质量检查 + 导出 + 报告...")
        self._identify_sparse_features(train, val, test)
        self._export_datasets(train, val, test)
        self._generate_reports(train, val, test)
        self._run_tests(train, val, test, df)

        self.conn.close()
        print("\n特征工程完成。")
        return df, train, val, test

    # ── 1. 加载比赛 ──────────────────────────────────────────

    def _load_matches(self):
        sql = """
            SELECT m.*, l.name_cn as league_name, l.code as league_code
            FROM matches m
            JOIN leagues l ON m.league_id = l.id
            WHERE l.code != 'SUM'
            ORDER BY m.kickoff_time
        """
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    # ── 2. 欧赔特征 ──────────────────────────────────────────

    def _build_europe_features(self, matches):
        """为每场比赛生成欧赔特征。"""
        # 预加载所有欧赔
        odds_by_match = defaultdict(lambda: {"opening": {}, "closing": {}})
        rows = self.conn.execute("""
            SELECT match_id, bookmaker, odds_type, odds_home, odds_draw, odds_away
            FROM odds_europe WHERE odds_home IS NOT NULL
        """).fetchall()
        for r in rows:
            odds_by_match[r["match_id"]][r["odds_type"]][r["bookmaker"]] = {
                "h": r["odds_home"], "d": r["odds_draw"], "a": r["odds_away"]
            }

        self.euro_features = {}
        for m in matches:
            mid = m["match_id"]
            feats = {}
            odm = odds_by_match.get(mid, {"opening": {}, "closing": {}})

            # ── Bet365 初盘/终盘 ──
            for otype in ["opening", "closing"]:
                bk_data = odm.get(otype, {}).get(PRIMARY_BOOKMAKER)
                prefix = "open" if otype == "opening" else "close"
                if bk_data:
                    ph, pd, pa = implied_probabilities(bk_data["h"], bk_data["d"], bk_data["a"])
                    feats[f"{prefix}_home_prob"] = ph
                    feats[f"{prefix}_draw_prob"] = pd
                    feats[f"{prefix}_away_prob"] = pa
                    feats[f"{prefix}_home_odds"] = bk_data["h"]
                    feats[f"{prefix}_draw_odds"] = bk_data["d"]
                    feats[f"{prefix}_away_odds"] = bk_data["a"]
                else:
                    for k in ["home_prob", "draw_prob", "away_prob", "home_odds", "draw_odds", "away_odds"]:
                        feats[f"{prefix}_{k}"] = None

            # ── 概率变化 ──
            for outcome in ["home", "draw", "away"]:
                op = feats.get(f"open_{outcome}_prob")
                cp = feats.get(f"close_{outcome}_prob")
                feats[f"prob_change_{outcome}"] = round(cp - op, 6) if (op is not None and cp is not None) else None

            # ── 赔率变化率: (close - open) / open ──
            for outcome in ["home", "draw", "away"]:
                oo = feats.get(f"open_{outcome}_odds")
                co = feats.get(f"close_{outcome}_odds")
                feats[f"odds_change_pct_{outcome}"] = (
                    round((co - oo) / oo, 6) if (oo and co and oo > 0) else None
                )

            # ── 跨公司方差 (终盘) ──
            closing_odds = odm.get("closing", {})
            home_odds_list = [v["h"] for v in closing_odds.values() if v["h"]]
            draw_odds_list = [v["d"] for v in closing_odds.values() if v["d"]]
            away_odds_list = [v["a"] for v in closing_odds.values() if v["a"]]

            feats["odds_var_home"] = round(np.var(home_odds_list), 6) if len(home_odds_list) >= 3 else None
            feats["odds_var_draw"] = round(np.var(draw_odds_list), 6) if len(draw_odds_list) >= 3 else None
            feats["odds_var_away"] = round(np.var(away_odds_list), 6) if len(away_odds_list) >= 3 else None

            # ── 博彩公司分歧度: 隐含概率的标准差 ──
            probs_list = []
            for bk_data in closing_odds.values():
                ph, pd_, pa = implied_probabilities(bk_data["h"], bk_data["d"], bk_data["a"])
                if ph is not None:
                    probs_list.append((ph, pd_, pa))
            if len(probs_list) >= 3:
                feats["bookmaker_divergence"] = round(
                    float(np.mean([np.std([p[i] for p in probs_list]) for i in range(3)])), 6
                )
            else:
                feats["bookmaker_divergence"] = None

            # ── 欧赔离散度 (CV across bookmakers, closing) ──
            if len(home_odds_list) >= 3:
                mean_h = np.mean(home_odds_list)
                feats["odds_dispersion"] = round(float(np.std(home_odds_list) / mean_h), 6) if mean_h > 0 else None
            else:
                feats["odds_dispersion"] = None

            # ── 热门方向 ──
            if feats.get("close_home_prob") is not None:
                probs = {
                    "home": feats["close_home_prob"],
                    "draw": feats["close_draw_prob"],
                    "away": feats["close_away_prob"],
                }
                favorite = max(probs, key=probs.get)
                feats["favorite_direction"] = {"home": 0, "draw": 1, "away": 2}[favorite]
                feats["favorite_prob"] = probs[favorite]
            else:
                feats["favorite_direction"] = None
                feats["favorite_prob"] = None

            self.euro_features[mid] = feats

    # ── 3. 亚盘特征 ──────────────────────────────────────────

    def _build_asian_features(self, matches):
        odds_by_match = defaultdict(lambda: {"opening": {}, "closing": {}})
        rows = self.conn.execute("""
            SELECT match_id, bookmaker, odds_type, high_water, handicap, low_water
            FROM odds_asian
        """).fetchall()
        for r in rows:
            odds_by_match[r["match_id"]][r["odds_type"]][r["bookmaker"]] = {
                "high_water": r["high_water"],
                "handicap": r["handicap"],
                "low_water": r["low_water"],
            }

        self.asian_features = {}
        for m in matches:
            mid = m["match_id"]
            feats = {}
            odm = odds_by_match.get(mid, {"opening": {}, "closing": {}})

            for otype in ["opening", "closing"]:
                prefix = "asian_open" if otype == "opening" else "asian_close"
                bk_data = odm.get(otype, {}).get(PRIMARY_BOOKMAKER)
                if bk_data:
                    line = parse_asian_handicap(bk_data["handicap"])
                    feats[f"{prefix}_line"] = line
                    feats[f"{prefix}_high_water"] = bk_data["high_water"]
                    feats[f"{prefix}_low_water"] = bk_data["low_water"]
                else:
                    feats[f"{prefix}_line"] = None
                    feats[f"{prefix}_high_water"] = None
                    feats[f"{prefix}_low_water"] = None

            # ── 升盘/降盘 ──
            open_line = feats.get("asian_open_line")
            close_line = feats.get("asian_close_line")
            if open_line is not None and close_line is not None:
                feats["asian_line_change"] = round(close_line - open_line, 2)
                if feats["asian_line_change"] > 0:
                    feats["asian_line_direction"] = 1  # 升盘
                elif feats["asian_line_change"] < 0:
                    feats["asian_line_direction"] = -1  # 降盘
                else:
                    feats["asian_line_direction"] = 0
            else:
                feats["asian_line_change"] = None
                feats["asian_line_direction"] = None

            # ── 水位变化 ──
            for side in ["high_water", "low_water"]:
                ow = feats.get(f"asian_open_{side}")
                cw = feats.get(f"asian_close_{side}")
                feats[f"asian_{side}_change"] = round(cw - ow, 4) if (ow is not None and cw is not None) else None

            # ── 盘口方向: 正值=主队让球, 负值=主队受让 ──
            feats["asian_direction"] = 1 if (close_line is not None and close_line > 0) else (
                -1 if (close_line is not None and close_line < 0) else 0
            )

            # ── 亚盘热度: 基于水位变化推断资金流向 ──
            hw_change = feats.get("asian_high_water_change")
            lw_change = feats.get("asian_low_water_change")
            if hw_change is not None and lw_change is not None:
                feats["asian_heat"] = round(hw_change - lw_change, 4)
            else:
                feats["asian_heat"] = None

            self.asian_features[mid] = feats

    # ── 4. 大小球特征 ────────────────────────────────────────

    def _build_ou_features(self, matches):
        odds_by_match = defaultdict(lambda: {"opening": {}, "closing": {}})
        rows = self.conn.execute("""
            SELECT match_id, bookmaker, odds_type, over_water, handicap, under_water
            FROM odds_over_under
        """).fetchall()
        for r in rows:
            odds_by_match[r["match_id"]][r["odds_type"]][r["bookmaker"]] = {
                "over_water": r["over_water"],
                "handicap": r["handicap"],
                "under_water": r["under_water"],
            }

        self.ou_features = {}
        for m in matches:
            mid = m["match_id"]
            feats = {}
            odm = odds_by_match.get(mid, {"opening": {}, "closing": {}})

            for otype in ["opening", "closing"]:
                prefix = "ou_open" if otype == "opening" else "ou_close"
                bk_data = odm.get(otype, {}).get(PRIMARY_BOOKMAKER)
                if bk_data:
                    line = parse_ou_handicap(bk_data["handicap"])
                    feats[f"{prefix}_line"] = line
                    feats[f"{prefix}_over_water"] = bk_data["over_water"]
                    feats[f"{prefix}_under_water"] = bk_data["under_water"]
                else:
                    feats[f"{prefix}_line"] = None
                    feats[f"{prefix}_over_water"] = None
                    feats[f"{prefix}_under_water"] = None

            # ── 大小球升降盘 ──
            open_line = feats.get("ou_open_line")
            close_line = feats.get("ou_close_line")
            if open_line is not None and close_line is not None:
                feats["ou_line_change"] = round(close_line - open_line, 2)
                if feats["ou_line_change"] > 0.1:
                    feats["ou_line_direction"] = 1  # 升盘
                elif feats["ou_line_change"] < -0.1:
                    feats["ou_line_direction"] = -1  # 降盘
                else:
                    feats["ou_line_direction"] = 0
            else:
                feats["ou_line_change"] = None
                feats["ou_line_direction"] = None

            # ── 水位变化 ──
            for side in ["over_water", "under_water"]:
                ow = feats.get(f"ou_open_{side}")
                cw = feats.get(f"ou_close_{side}")
                feats[f"ou_{side}_change"] = round(cw - ow, 4) if (ow is not None and cw is not None) else None

            self.ou_features[mid] = feats

    # ── 5. 比赛标签 ──────────────────────────────────────────

    def _build_labels(self, matches):
        self.label_data = {}
        for m in matches:
            mid = m["match_id"]
            hs = m["home_score"]
            aws = m["away_score"]
            labels = {}

            if hs is not None and aws is not None:
                total = hs + aws
                labels["label_home_win"] = 1 if hs > aws else 0
                labels["label_draw"] = 1 if hs == aws else 0
                labels["label_away_win"] = 1 if hs < aws else 0
                labels["label_over_2_5"] = 1 if total > 2.5 else 0
                labels["label_total_goals"] = total
                labels["label_goal_diff"] = hs - aws

                # 让球胜负 (基于终盘盘口)
                asian = self.asian_features.get(mid, {})
                close_line = asian.get("asian_close_line")
                if close_line is not None:
                    adjusted_diff = hs - aws + close_line  # 主队 + 盘口 vs 客队
                    if adjusted_diff > 0:
                        labels["label_asian"] = 1  # 主队赢盘
                    elif adjusted_diff < 0:
                        labels["label_asian"] = -1  # 客队赢盘
                    else:
                        labels["label_asian"] = 0  # 走水
                else:
                    labels["label_asian"] = None

                # 爆冷: 终盘隐含概率最低的结果反而发生
                euro = self.euro_features.get(mid, {})
                cp = euro.get("close_home_prob")
                dp = euro.get("close_draw_prob")
                ap = euro.get("close_away_prob")
                if cp and dp and ap:
                    probs = {"home": cp, "draw": dp, "away": ap}
                    min_prob_outcome = min(probs, key=probs.get)
                    if labels["label_home_win"] == 1 and min_prob_outcome == "home":
                        labels["label_upset"] = 1
                    elif labels["label_draw"] == 1 and min_prob_outcome == "draw":
                        labels["label_upset"] = 1
                    elif labels["label_away_win"] == 1 and min_prob_outcome == "away":
                        labels["label_upset"] = 1
                    else:
                        labels["label_upset"] = 0
                else:
                    labels["label_upset"] = None
            else:
                for k in ["label_home_win", "label_draw", "label_away_win",
                          "label_over_2_5", "label_total_goals", "label_goal_diff",
                          "label_asian", "label_upset"]:
                    labels[k] = None

            self.label_data[mid] = labels

    # ── 6. 组装数据集 ────────────────────────────────────────

    def _assemble_dataset(self):
        rows = []
        for m in self._load_matches():
            mid = m["match_id"]
            row = {
                "match_id": mid,
                "kickoff_time": m["kickoff_time"],
                "season": m["season"],
                "league_code": m.get("league_code", ""),
                "home_team": m["home_team"],
                "away_team": m["away_team"],
            }
            row.update(self.euro_features.get(mid, {}))
            row.update(self.asian_features.get(mid, {}))
            row.update(self.ou_features.get(mid, {}))
            row.update(self.label_data.get(mid, {}))
            rows.append(row)

        df = pd.DataFrame(rows)
        df = df.sort_values("kickoff_time").reset_index(drop=True)

        # 统计特征数量
        exclude_cols = {"match_id", "kickoff_time", "season", "league_code", "home_team", "away_team"}
        label_cols = {c for c in df.columns if c.startswith("label_")}
        feature_cols = [c for c in df.columns if c not in exclude_cols and c not in label_cols]
        self.report_data["features"] = {
            "total_features": len(feature_cols),
            "feature_names": feature_cols,
            "total_labels": len(label_cols),
            "label_names": sorted(label_cols),
            "total_rows": len(df),
        }
        self.df = df
        return df

    # ── 7. 时间切分 ──────────────────────────────────────────

    def _temporal_split(self, df):
        n = len(df)
        train_end = int(n * 0.70)
        val_end = int(n * 0.85)

        train = df.iloc[:train_end].copy()
        val = df.iloc[train_end:val_end].copy()
        test = df.iloc[val_end:].copy()

        # 验证时间不交叉 (errors='coerce' 处理 2025-02-29 等无效日期)
        dt_fmt = "%Y-%m-%d %H:%M:%S"
        t_max = pd.to_datetime(train["kickoff_time"], format=dt_fmt, errors="coerce").max()
        v_min = pd.to_datetime(val["kickoff_time"], format=dt_fmt, errors="coerce").min()
        v_max = pd.to_datetime(val["kickoff_time"], format=dt_fmt, errors="coerce").max()
        ts_min = pd.to_datetime(test["kickoff_time"], format=dt_fmt, errors="coerce").min()

        self.split_info = {
            "train_end": str(t_max),
            "val_start": str(v_min),
            "val_end": str(v_max),
            "test_start": str(ts_min),
            "train_count": len(train),
            "val_count": len(val),
            "test_count": len(test),
            "time_separation_ok": t_max < v_min and v_max < ts_min,
        }

        return train, val, test

    # ── 8. 识别稀疏特征 ──────────────────────────────────────

    def _identify_sparse_features(self, train, val, test):
        feats = self.report_data["features"]["feature_names"]
        dropped = []
        keep = []
        for col in feats:
            worst_miss = max(
                train[col].isnull().mean(),
                val[col].isnull().mean(),
                test[col].isnull().mean(),
            )
            if worst_miss > 0.9:
                dropped.append(col)
            else:
                keep.append(col)

        self.report_data["features"]["feature_names"] = keep
        self.report_data["features"]["dropped_features"] = dropped
        self.report_data["features"]["dropped_count"] = len(dropped)
        if dropped:
            print(f"  移除 {len(dropped)} 个高缺失特征 (>90%): {dropped}")

    # ── 9. 导出 ──────────────────────────────────────────────

    def _export_datasets(self, train, val, test):
        DATASETS_DIR.mkdir(parents=True, exist_ok=True)

        # 移除高缺失特征列
        dropped = self.report_data["features"].get("dropped_features", [])
        for name, df in [("train", train), ("validation", val), ("test", test)]:
            cols_to_drop = [c for c in dropped if c in df.columns]
            df.drop(columns=cols_to_drop, inplace=True, errors="ignore")
            path = DATASETS_DIR / f"{name}.csv"
            df.to_csv(path, index=False, encoding="utf-8-sig")
            size_kb = os.path.getsize(path) / 1024
            print(f"  {name}.csv: {len(df):,} rows, {len(df.columns)} cols, {size_kb:.0f} KB")

    # ── 9. 报告 ──────────────────────────────────────────────

    def _generate_reports(self, train, val, test):
        self._generate_json_report(train, val, test)
        self._generate_html_report(train, val, test)

    def _generate_json_report(self, train, val, test):
        report = self.report_data.copy()
        report["split"] = self.split_info

        # 特征统计
        feats = self.report_data["features"]["feature_names"]
        stats = {}
        full = pd.concat([train, val, test])
        for col in feats:
            series = full[col].dropna()
            if len(series) > 0:
                stats[col] = {
                    "count": int(series.count()),
                    "missing_rate": round((len(full) - series.count()) / len(full) * 100, 2),
                    "mean": round(float(series.mean()), 4) if series.dtype in ("float64", "int64") else None,
                    "std": round(float(series.std()), 4) if series.dtype in ("float64", "int64") else None,
                }
        report["feature_stats"] = stats

        # 标签分布
        label_stats = {}
        for col in sorted(self.report_data["features"]["label_names"]):
            series = full[col].dropna()
            if len(series) > 0:
                vc = series.value_counts().to_dict()
                label_stats[col] = {
                    "count": int(series.count()),
                    "missing_rate": round((len(full) - series.count()) / len(full) * 100, 2),
                    "distribution": {str(k): int(v) for k, v in vc.items()},
                }
        report["label_stats"] = label_stats

        # 完整性
        completeness = {}
        for name, df in [("train", train), ("validation", val), ("test", test)]:
            non_null = df[feats].notnull().mean()
            completeness[name] = {
                "avg_completeness": round(float(non_null.mean()) * 100, 1),
                "min_completeness": round(float(non_null.min()) * 100, 1),
            }
        report["completeness"] = completeness

        # 警告
        for col in feats:
            missing = stats.get(col, {}).get("missing_rate", 0)
            if missing > 20:
                report["warnings"].append(f"特征 '{col}' 缺失率 {missing}% > 20%")
        if not report["warnings"]:
            report["warnings"].append("所有特征缺失率可接受")

        report_path = REPORTS_DIR / "feature_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"  JSON: {report_path}")

    def _generate_html_report(self, train, val, test):
        s = self.report_data["features"]
        sp = self.split_info
        r = self.report_data

        # 特征表格
        feat_rows = ""
        for i, col in enumerate(s["feature_names"]):
            st = r.get("feature_stats", {}).get(col, {})
            miss = st.get("missing_rate", 0)
            badge = "badge-ok" if miss < 5 else ("badge-warn" if miss < 20 else "badge-err")
            feat_rows += (
                f"<tr><td>{col}</td>"
                f"<td><span class='badge {badge}'>{miss}%</span></td>"
                f"<td>{st.get('mean', '-')}</td><td>{st.get('std', '-')}</td></tr>"
            )

        # 标签分布
        label_rows = ""
        for col in sorted(s["label_names"]):
            ls = r.get("label_stats", {}).get(col, {})
            dist = ls.get("distribution", {})
            dist_str = ", ".join(f"{k}: {v}" for k, v in dist.items())
            miss = ls.get("missing_rate", 0)
            label_rows += (
                f"<tr><td>{col}</td><td>{ls.get('count', '-')}</td>"
                f"<td><span class='badge badge-{'ok' if miss < 5 else 'warn'}'>{miss}%</span></td>"
                f"<td style='font-size:11px'>{dist_str}</td></tr>"
            )

        no_leak = sp.get("time_separation_ok", False)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>AI 特征工程报告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#0f3460 0%,#16213e 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; margin-bottom:8px; }}
.header p {{ opacity:0.8; font-size:14px; }}
.container {{ max-width:1200px; margin:0 auto; padding:24px; }}
.grid-3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:24px; }}
.grid-2 {{ display:grid; grid-template-columns:repeat(2,1fr); gap:16px; margin-bottom:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
.card h2 {{ font-size:16px; color:#16213e; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #e8e8e8; }}
.stat-big {{ font-size:36px; font-weight:700; color:#0f3460; }}
.stat-label {{ font-size:13px; color:#666; margin-top:4px; }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600; }}
.badge-ok {{ background:#d1fae5; color:#065f46; }}
.badge-warn {{ background:#fef3c7; color:#92400e; }}
.badge-err {{ background:#fee2e2; color:#991b1b; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; font-weight:600; color:#555; }}
tr:hover {{ background:#f8f9fa; }}
.good {{ color:#10b981; }} .warn {{ color:#f59e0b; }} .bad {{ color:#ef4444; }}
</style>
</head>
<body>
<div class="header">
<h1>AI 特征工程报告</h1>
<p>生成时间: {r['generated_at']} | 数据库: football_history.db</p>
</div>

<div class="container">

<div class="grid-3">
<div class="card">
  <div class="stat-big">{s['total_features']}</div>
  <div class="stat-label">特征总数</div>
</div>
<div class="card">
  <div class="stat-big">{s['total_labels']}</div>
  <div class="stat-label">标签数</div>
</div>
<div class="card">
  <div class="stat-big">{s['total_rows']:,}</div>
  <div class="stat-label">总样本数</div>
</div>
</div>

<div class="grid-3">
<div class="card" style="text-align:center">
  <div class="stat-big" style="font-size:28px">{sp['train_count']:,}</div>
  <div class="stat-label">Train (70%) — 截止 {sp['train_end'][:10]}</div>
</div>
<div class="card" style="text-align:center">
  <div class="stat-big" style="font-size:28px">{sp['val_count']:,}</div>
  <div class="stat-label">Val (15%) — {sp['val_start'][:10]} ~ {sp['val_end'][:10]}</div>
</div>
<div class="card" style="text-align:center">
  <div class="stat-big" style="font-size:28px">{sp['test_count']:,}</div>
  <div class="stat-label">Test (15%) — 起始 {sp['test_start'][:10]}</div>
</div>
</div>

<div class="card" style="margin-bottom:24px">
<h2>时间切分验证</h2>
<table>
<tr><th>检查项</th><th>结果</th></tr>
<tr><td>Train max &lt; Val min</td>
  <td><span class='badge badge-{"ok" if no_leak else "err"}'>{"PASS — 无时间泄露" if no_leak else "FAIL — 时间交叉!"}</span></td></tr>
<tr><td>Val max &lt; Test min</td>
  <td><span class='badge badge-{"ok" if no_leak else "err"}'>{"PASS" if no_leak else "FAIL"}</span></td></tr>
<tr><td>Train 截止时间</td><td>{sp['train_end']}</td></tr>
<tr><td>Val 时间范围</td><td>{sp['val_start']} ~ {sp['val_end']}</td></tr>
<tr><td>Test 起始时间</td><td>{sp['test_start']}</td></tr>
</table>
</div>

<div class="grid-2">
<div class="card">
<h2>特征清单 & 缺失率</h2>
<div style="max-height:500px;overflow-y:auto">
<table><tr><th>特征</th><th>缺失率</th><th>均值</th><th>标准差</th></tr>
{feat_rows}
</table>
</div>
</div>

<div class="card">
<h2>标签分布</h2>
<table><tr><th>标签</th><th>数量</th><th>缺失率</th><th>分布</th></tr>
{label_rows}
</table>
</div>
</div>

<div class="card">
<h2>警告 ({len(r.get('warnings', []))})</h2>
{''.join(f'<div class="issue-info">{w}</div>' for w in r.get('warnings', []))}
</div>

</div>
</body>
</html>"""

        report_path = REPORTS_DIR / "feature_report.html"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  HTML: {report_path}")

    # ── 10. 自动测试 ─────────────────────────────────────────

    def _run_tests(self, train, val, test, full_df):
        print("\n" + "=" * 60)
        print("自动测试")
        print("=" * 60)

        passed = 0
        failed = 0
        errors = []

        def check(condition, msg):
            nonlocal passed, failed
            if condition:
                passed += 1
                print(f"  PASS: {msg}")
            else:
                failed += 1
                errors.append(msg)
                print(f"  FAIL: {msg}")

        feats = self.report_data["features"]["feature_names"]
        dropped_features = self.report_data["features"].get("dropped_features", [])

        # 1. 特征完整性检查
        print("\n## 1. 特征完整性")
        for col in dropped_features:
            print(f"  DROP: {col} (缺失率 >90%)")
        for col in feats:
            worst_miss = max(
                train[col].isnull().mean(),
                val[col].isnull().mean(),
                test[col].isnull().mean(),
            )
            if worst_miss > 0.5:
                print(f"  WARN: {col}: 缺失率 {worst_miss*100:.0f}% (50-90%, 保留)")
        check(len(dropped_features) < 20, f"移除 {len(dropped_features)} 个高缺失特征 < 20")

        # 2. 标签正确性
        print("\n## 2. 标签正确性")
        total = len(full_df.dropna(subset=["label_home_win", "label_draw", "label_away_win"]))
        check(total > 0, f"有标签样本: {total} > 0")

        labeled = full_df.dropna(subset=["label_home_win", "label_draw", "label_away_win"])
        if len(labeled) > 0:
            # 互斥性: home_win + draw + away_win = 1
            sums = labeled["label_home_win"] + labeled["label_draw"] + labeled["label_away_win"]
            check((sums == 1).all(), "标签互斥: home_win + draw + away_win = 1 (全部)")

            # 大小球标签: total_goals > 2.5 <=> over_2_5 == 1
            ou_labeled = labeled.dropna(subset=["label_total_goals", "label_over_2_5"])
            consistent = (ou_labeled["label_over_2_5"] == (ou_labeled["label_total_goals"] > 2.5).astype(int))
            check(consistent.mean() >= 0.95,
                  f"大小球标签一致性: {consistent.mean()*100:.1f}% >= 95%")

            # 爆冷标签: upset只在冷门结果实际发生时=1
            upset = labeled.dropna(subset=["label_upset"])
            check(upset["label_upset"].isin([0, 1]).all(), "爆冷标签值域: 仅 0 或 1")

        # 3. 无未来数据泄露
        print("\n## 3. 无未来数据泄露")
        dt_fmt = "%Y-%m-%d %H:%M:%S"
        t_max = pd.to_datetime(train["kickoff_time"], format=dt_fmt, errors="coerce").max()
        v_min = pd.to_datetime(val["kickoff_time"], format=dt_fmt, errors="coerce").min()
        v_max = pd.to_datetime(val["kickoff_time"], format=dt_fmt, errors="coerce").max()
        ts_min = pd.to_datetime(test["kickoff_time"], format=dt_fmt, errors="coerce").min()

        check(t_max < v_min,
              f"Train max ({str(t_max)[:19]}) < Val min ({str(v_min)[:19]})")
        check(v_max < ts_min,
              f"Val max ({str(v_max)[:19]}) < Test min ({str(ts_min)[:19]})")

        # 4. Train/Val/Test 时间不交叉
        print("\n## 4. 数据集不交叉")
        train_ids = set(train["match_id"])
        val_ids = set(val["match_id"])
        test_ids = set(test["match_id"])
        check(len(train_ids & val_ids) == 0, f"Train ∩ Val: 0 重复")
        check(len(val_ids & test_ids) == 0, f"Val ∩ Test: 0 重复")
        check(len(train_ids & test_ids) == 0, f"Train ∩ Test: 0 重复")

        # 5. 样本比例
        print("\n## 5. 切分比例")
        total = len(full_df)
        check(abs(len(train) / total - 0.70) < 0.02, f"Train: {len(train)/total*100:.1f}% ~ 70%")
        check(abs(len(val) / total - 0.15) < 0.02, f"Val: {len(val)/total*100:.1f}% ~ 15%")
        check(abs(len(test) / total - 0.15) < 0.02, f"Test: {len(test)/total*100:.1f}% ~ 15%")

        # 汇总
        print(f"\n{'=' * 60}")
        print(f"测试结果: {passed} 通过, {failed} 失败")
        if errors:
            print("失败项:")
            for e in errors:
                print(f"  - {e}")
        print(f"{'=' * 60}")

        self.test_results = {"passed": passed, "failed": failed, "errors": errors}
        return failed == 0


def main():
    print("AI 特征工程系统启动...")
    print(f"数据库: {DB_PATH}")
    print(f"输出目录: {DATASETS_DIR}")

    if not DB_PATH.exists():
        print(f"错误: 数据库不存在: {DB_PATH}")
        sys.exit(1)

    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    engineer = FeatureEngineer()
    df, train, val, test = engineer.run_all()

    # ── 最终总结 ──────────────────────────────────────────
    s = engineer.report_data["features"]
    sp = engineer.split_info
    tr = engineer.test_results

    print(f"\n{'=' * 60}")
    print("特征工程 — 最终总结")
    print(f"{'=' * 60}")
    print(f"  总样本:            {s['total_rows']:,}")
    print(f"  特征数:            {s['total_features']}")
    print(f"  标签数:            {s['total_labels']}")
    print(f"  Train:             {sp['train_count']:,} (70%)")
    print(f"  Validation:        {sp['val_count']:,} (15%)")
    print(f"  Test:              {sp['test_count']:,} (15%)")
    print(f"  时间泄露:          {'无' if sp['time_separation_ok'] else '有! 需修复'}")
    print(f"  测试:              {tr['passed']}/{tr['passed']+tr['failed']} 通过")
    print()
    print(f"数据集文件:")
    print(f"  {DATASETS_DIR / 'train.csv'}")
    print(f"  {DATASETS_DIR / 'validation.csv'}")
    print(f"  {DATASETS_DIR / 'test.csv'}")
    print(f"报告文件:")
    print(f"  {REPORTS_DIR / 'feature_report.html'}")
    print(f"  {REPORTS_DIR / 'feature_report.json'}")
    print(f"{'=' * 60}")

    if tr["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
