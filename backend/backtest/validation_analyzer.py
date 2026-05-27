#!/usr/bin/env python3
"""
回测结果真实性验证模块 (Backtest Validation Analyzer)

验证 Walk Forward 回测结果的真实性、稳健性:
- 信号覆盖率: 总比赛 vs 实际下注
- 联赛拆分: 英超/西甲/意甲/德甲/法甲/欧冠/欧联
- 赔率区间: 1.50-1.70 / 1.70-1.90 / 1.90-2.10 / 2.10-2.50 / 2.50+
- 水位区间: 低水/中水/高水 (亚盘 & 大小球)
- 月度/季度/年度频率
- 模型对比
- 样本量预警 (LOW SAMPLE WARNING)
"""

import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASETS_DIR = PROJECT_ROOT / "datasets"
REPORTS_DIR = PROJECT_ROOT / "reports"
BETS_DIR = REPORTS_DIR / "backtest"
OUTPUT_DIR = BETS_DIR / "validation"

warnings.filterwarnings("ignore")

# 联赛名称映射
LEAGUE_MAP = {
    "EPL": "英超",
    "LLG": "西甲",
    "ISA": "意甲",
    "BUN": "德甲",
    "FL1": "法甲",
    "UCL": "欧冠",
    "UEL": "欧联",
}

# 赔率区间定义
ODDS_BINS = [
    (1.50, 1.70, "1.50-1.70"),
    (1.70, 1.90, "1.70-1.90"),
    (1.90, 2.10, "1.90-2.10"),
    (2.10, 2.50, "2.10-2.50"),
    (2.50, float("inf"), "2.50+"),
]

# 水位区间定义 (香港盘水位)
WATER_BINS = [
    (0.00, 0.85, "低水 (<0.85)"),
    (0.85, 1.00, "中水 (0.85-1.00)"),
    (1.00, float("inf"), "高水 (>1.00)"),
]

# 样本量阈值
MIN_SAMPLE_STRONG = 100   # 低于此值标记 "LOW SAMPLE WARNING"
MIN_SAMPLE_WEAK = 30      # 低于此值标记 "样本不足，不可作为稳定结论"


class ValidationAnalyzer:
    """回测结果真实性验证分析器。"""

    def __init__(self, bets_dir: str = None, datasets_dir: str = None,
                 output_dir: str = None):
        self.bets_dir = Path(bets_dir) if bets_dir else BETS_DIR
        self.datasets_dir = Path(datasets_dir) if datasets_dir else DATASETS_DIR
        self.output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.full_df: Optional[pd.DataFrame] = None
        self.all_bets: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.bets_with_meta: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.results: dict = {}

    # ── 数据加载 ────────────────────────────────────────────────

    def load_data(self):
        """加载全部比赛数据和回测投注记录。"""
        print("[1/6] 加载数据...")

        # 加载全量比赛数据
        df_parts = []
        for name in ["train.csv", "validation.csv", "test.csv"]:
            path = self.datasets_dir / name
            if path.exists():
                part = pd.read_csv(path)
                df_parts.append(part)
                print(f"  {name}: {len(part):,} 场")

        self.full_df = pd.concat(df_parts, ignore_index=True)
        self.full_df["kickoff_time"] = pd.to_datetime(
            self.full_df["kickoff_time"], errors="coerce"
        )
        print(f"  全量比赛: {len(self.full_df):,} 场")

        # 加载投注 CSV
        csv_files = sorted(self.bets_dir.glob("bets_*.csv"))
        known_tasks = ["asian", "over_under", "wdl"]
        for csv_path in csv_files:
            fname = csv_path.stem  # e.g., "bets_asian_xgboost" or "bets_over_under_catboost"
            stem = fname[len("bets_"):]  # "asian_xgboost" or "over_under_catboost"

            task_key = None
            model_name = None
            for t in known_tasks:
                if stem.startswith(t + "_"):
                    task_key = t
                    model_name = stem[len(t) + 1:]
                    break

            if task_key is None or model_name is None:
                print(f"  [WARN] 无法解析文件名: {fname}")
                continue

            bd = pd.read_csv(csv_path)
            if task_key not in self.all_bets:
                self.all_bets[task_key] = {}
            self.all_bets[task_key][model_name] = bd
            print(f"  bets_{task_key}_{model_name}: {len(bd):,} 笔投注")

    # ── 数据合并 ────────────────────────────────────────────────

    def merge_metadata(self):
        """将投注记录与原始比赛数据合并，附加联赛/赔率/水位等信息。"""
        print("[2/6] 合并元数据...")

        df = self.full_df
        for task_key, models in self.all_bets.items():
            self.bets_with_meta[task_key] = {}
            for model_name, bd in models.items():
                if bd.empty or "match_id" not in bd.columns:
                    continue

                # 选择要合并的原始列
                merge_cols = ["match_id", "league_code", "season", "kickoff_time"]
                extra_cols = []

                if task_key == "wdl":
                    extra_cols = ["close_home_odds", "close_draw_odds", "close_away_odds"]
                elif task_key == "asian":
                    extra_cols = ["asian_close_high_water", "asian_close_low_water",
                                  "asian_close_line"]
                elif task_key == "over_under":
                    extra_cols = ["ou_close_over_water", "ou_close_under_water",
                                  "ou_close_line"]

                available = [c for c in merge_cols + extra_cols if c in df.columns]
                orig_subset = df[available].drop_duplicates("match_id")

                merged = bd.merge(orig_subset, on="match_id", how="left",
                                  suffixes=("", "_orig"))
                # 用原始 kickoff_time 覆盖 (更可靠)
                if "kickoff_time_orig" in merged.columns:
                    merged["kickoff_time"] = merged["kickoff_time_orig"].fillna(
                        merged["kickoff_time"]
                    )
                    merged = merged.drop(columns=["kickoff_time_orig"])

                # 衍生字段
                merged["month"] = pd.to_datetime(merged["kickoff_time"], errors="coerce").dt.to_period("M")
                merged["quarter"] = pd.to_datetime(merged["kickoff_time"], errors="coerce").dt.to_period("Q")
                merged["year"] = pd.to_datetime(merged["kickoff_time"], errors="coerce").dt.year

                # 赔率区间 (用 decimal odds)
                if "odds" in merged.columns:
                    merged["odds_range"] = merged["odds"].apply(_classify_odds)

                # 水位区间 (用原始香港水位: odds - 1.0)
                merged["water"] = merged["odds"] - 1.0
                merged["water_level"] = merged["water"].apply(_classify_water)

                # 联赛中文名
                if "league_code" in merged.columns:
                    merged["league_name"] = merged["league_code"].map(LEAGUE_MAP).fillna(merged["league_code"])

                self.bets_with_meta[task_key][model_name] = merged
                print(f"  [{task_key}/{model_name}] 合并完成: {len(merged):,} 条")

    # ── 信号覆盖率 ──────────────────────────────────────────────

    def analyze_coverage(self) -> dict:
        """分析信号覆盖率: 总比赛 vs 下注数 vs 覆盖率。"""
        print("[3/6] 信号覆盖率分析...")

        total_matches = len(self.full_df)
        coverage = {"total_matches": total_matches, "tasks": {}}

        for task_key, models in self.bets_with_meta.items():
            task_cov = {"models": {}}
            for model_name, bd in models.items():
                if bd.empty:
                    continue

                n_bets = len(bd)
                n_matches_bet = bd["match_id"].nunique() if "match_id" in bd.columns else n_bets
                cov_pct = n_matches_bet / total_matches if total_matches > 0 else 0

                # 月度频率
                monthly = _safe_groupby(bd, "month", "profit", ["sum", "count"])
                monthly.columns = ["profit", "bets"]

                # 季度频率
                quarterly = _safe_groupby(bd, "quarter", "profit", ["sum", "count"])
                quarterly.columns = ["profit", "bets"]

                model_cov = {
                    "total_bets": n_bets,
                    "unique_matches": n_matches_bet,
                    "coverage_pct": round(cov_pct, 4),
                    "avg_bets_per_month": round(float(monthly["bets"].mean()), 1) if len(monthly) > 0 else 0,
                    "max_bets_per_month": int(monthly["bets"].max()) if len(monthly) > 0 else 0,
                    "min_bets_per_month": int(monthly["bets"].min()) if len(monthly) > 0 else 0,
                    "months_active": len(monthly),
                    "quarters_active": len(quarterly),
                    "monthly_detail": {
                        str(k): {"bets": int(v["bets"]), "profit": round(float(v["profit"]), 2)}
                        for k, v in monthly.iterrows()
                    },
                    "quarterly_detail": {
                        str(k): {"bets": int(v["bets"]), "profit": round(float(v["profit"]), 2)}
                        for k, v in quarterly.iterrows()
                    },
                }

                # 覆盖率评估
                if cov_pct < 0.10:
                    model_cov["coverage_warning"] = "覆盖率极低 (<10%), 可能存在严重选择性偏差"
                elif cov_pct < 0.30:
                    model_cov["coverage_warning"] = "覆盖率偏低 (<30%), 策略较保守"
                elif cov_pct > 0.80:
                    model_cov["coverage_warning"] = "覆盖率很高 (>80%), 接近全量投注"
                else:
                    model_cov["coverage_warning"] = None

                task_cov["models"][model_name] = model_cov
                print(f"  [{task_key}/{model_name}] 覆盖率: {cov_pct:.1%} "
                      f"({n_matches_bet}/{total_matches}) "
                      f"月均 {model_cov['avg_bets_per_month']:.0f} 注")

            coverage["tasks"][task_key] = task_cov

        return coverage

    # ── 联赛分析 ────────────────────────────────────────────────

    def analyze_by_league(self) -> dict:
        """按联赛拆分分析。"""
        print("[4/6] 联赛拆分分析...")

        league_results = {}
        for task_key, models in self.bets_with_meta.items():
            league_results[task_key] = {}
            for model_name, bd in models.items():
                if bd.empty or "league_code" not in bd.columns:
                    continue

                league_stats = {}
                for code in LEAGUE_MAP:
                    subset = bd[bd["league_code"] == code]
                    if len(subset) == 0:
                        continue
                    stats = _compute_segment_metrics(subset)
                    stats["league_name"] = LEAGUE_MAP[code]
                    stats["league_code"] = code
                    league_stats[code] = stats

                # 汇总 (所有联赛)
                all_stats = _compute_segment_metrics(bd)
                all_stats["league_name"] = "全部联赛"
                league_stats["ALL"] = all_stats

                league_results[task_key][model_name] = league_stats

                # 打印
                print(f"  [{task_key}/{model_name}]")
                for code, s in league_stats.items():
                    if code == "ALL":
                        continue
                    warn = _sample_warning(s["bets"])
                    warn_str = f" [!!! {warn}]" if warn else ""
                    print(f"    {LEAGUE_MAP.get(code, code):6s}: "
                          f"ROI={s['roi']:.2%} WR={s['win_rate']:.1%} "
                          f"Bets={s['bets']} MaxDD={s['max_drawdown_pct']:.1%}"
                          f"{warn_str}")

        return league_results

    # ── 赔率区间分析 ────────────────────────────────────────────

    def analyze_by_odds_range(self) -> dict:
        """按赔率区间分析 (仅对有 decimal odds 的任务有效)。"""
        print("[5/6] 赔率区间分析...")

        odds_results = {}
        for task_key, models in self.bets_with_meta.items():
            odds_results[task_key] = {}
            for model_name, bd in models.items():
                if bd.empty or "odds_range" not in bd.columns:
                    continue

                range_stats = {}
                for _, _, label in ODDS_BINS:
                    subset = bd[bd["odds_range"] == label]
                    stats = _compute_segment_metrics(subset) if len(subset) > 0 else {"bets": 0, "error": "无数据"}
                    stats["label"] = label
                    range_stats[label] = stats

                odds_results[task_key][model_name] = range_stats

                print(f"  [{task_key}/{model_name}]")
                for label, s in range_stats.items():
                    if s["bets"] == 0:
                        continue
                    warn = _sample_warning(s["bets"])
                    warn_str = f" [!!! {warn}]" if warn else ""
                    print(f"    {label:12s}: ROI={s['roi']:.2%} WR={s['win_rate']:.1%} "
                          f"Bets={s['bets']}{warn_str}")

        return odds_results

    # ── 水位区间分析 ────────────────────────────────────────────

    def analyze_by_water_level(self) -> dict:
        """按水位区间分析 (仅对亚盘/大小球有效, WDL 用赔率区间)。"""
        print("[5b/6] 水位区间分析...")

        water_results = {}
        for task_key, models in self.bets_with_meta.items():
            if task_key == "wdl":
                continue  # WDL 不适用水位区间

            water_results[task_key] = {}
            for model_name, bd in models.items():
                if bd.empty or "water_level" not in bd.columns:
                    continue

                level_stats = {}
                for _, _, label in WATER_BINS:
                    subset = bd[bd["water_level"] == label]
                    stats = _compute_segment_metrics(subset) if len(subset) > 0 else {"bets": 0, "error": "无数据"}
                    stats["label"] = label
                    level_stats[label] = stats

                water_results[task_key][model_name] = level_stats

                print(f"  [{task_key}/{model_name}]")
                for label, s in level_stats.items():
                    if s["bets"] == 0:
                        continue
                    warn = _sample_warning(s["bets"])
                    print(f"    {label:20s}: ROI={s['roi']:.2%} WR={s['win_rate']:.1%} "
                          f"Bets={s['bets']}{' [!!! ' + warn + ']' if warn else ''}")

        return water_results

    # ── 年度分析 ────────────────────────────────────────────────

    def analyze_by_year(self) -> dict:
        """按年度分析。"""
        yearly = {}
        for task_key, models in self.bets_with_meta.items():
            yearly[task_key] = {}
            for model_name, bd in models.items():
                if bd.empty or "year" not in bd.columns:
                    continue
                y_agg = _safe_groupby(bd, "year", "profit", ["sum", "count", "mean"])
                y_agg.columns = ["total_profit", "bets", "avg_profit"]

                win_rate_map = _safe_groupby(bd, "year", "profit",
                                             lambda x: (x > 0).sum() / max((x != 0).sum(), 1))
                y_agg["win_rate"] = win_rate_map

                detail = {}
                for yr, row in y_agg.iterrows():
                    detail[str(yr)] = {
                        "bets": int(row["bets"]),
                        "total_profit": round(float(row["total_profit"]), 2),
                        "roi": round(float(row["total_profit"] / row["bets"]) if row["bets"] > 0 else 0, 4),
                        "win_rate": round(float(row["win_rate"]), 4),
                    }
                yearly[task_key][model_name] = detail

        return yearly

    # ── 模型对比 ────────────────────────────────────────────────

    def compare_models(self) -> dict:
        """多模型核心指标对比。"""
        comparison = {}
        for task_key, models in self.bets_with_meta.items():
            rows = []
            for model_name, bd in models.items():
                if bd.empty:
                    continue
                s = _compute_segment_metrics(bd)
                rows.append({
                    "model": model_name,
                    "bets": s["bets"],
                    "roi": s["roi"],
                    "win_rate": s["win_rate"],
                    "sharpe": s["sharpe_ratio"],
                    "max_dd_pct": s["max_drawdown_pct"],
                    "profit_factor": s["profit_factor"],
                    "total_profit": s["total_profit"],
                })
            comparison[task_key] = pd.DataFrame(rows).set_index("model") if rows else pd.DataFrame()

        return comparison

    # ── 主流程 ──────────────────────────────────────────────────

    def run(self) -> dict:
        """执行完整验证分析。"""
        print("=" * 60)
        print("回测真实性验证分析器 v1.0")
        print("=" * 60)

        self.load_data()
        self.merge_metadata()

        results = {
            "generated_at": datetime.now().isoformat(),
            "coverage": self.analyze_coverage(),
            "by_league": self.analyze_by_league(),
            "by_odds_range": self.analyze_by_odds_range(),
            "by_water_level": self.analyze_by_water_level(),
            "by_year": self.analyze_by_year(),
            "model_comparison": self.compare_models(),
        }

        # 样本量预警汇总
        results["sample_warnings"] = self._collect_warnings(results)

        self.results = results

        # 输出文件
        print("\n[6/6] 生成验证报告...")
        self._export_csv(results)
        self._build_html(results)

        # 打印预警
        self._print_warnings(results["sample_warnings"])

        print(f"\n  验证报告目录: {self.output_dir}")
        print(f"  HTML: {self.output_dir / 'validation_summary.html'}")
        return results

    # ── 样本预警 ────────────────────────────────────────────────

    def _collect_warnings(self, results: dict) -> list:
        """收集所有样本量不足的警告。"""
        warnings_list = []

        def check_segment(name: str, stats: dict):
            bets = stats.get("bets", 0)
            if bets == 0:
                return
            if bets < MIN_SAMPLE_WEAK:
                warnings_list.append({
                    "segment": name,
                    "bets": bets,
                    "level": "CRITICAL",
                    "message": f"{name}: 仅 {bets} 注 — 样本不足，不可作为稳定结论",
                })
            elif bets < MIN_SAMPLE_STRONG:
                warnings_list.append({
                    "segment": name,
                    "bets": bets,
                    "level": "WARNING",
                    "message": f"{name}: 仅 {bets} 注 — LOW SAMPLE WARNING",
                })

        # 检查联赛
        for task_key, models in results.get("by_league", {}).items():
            for model_name, leagues in models.items():
                for code, stats in leagues.items():
                    if code == "ALL":
                        continue
                    check_segment(f"[{task_key}/{model_name}] 联赛={LEAGUE_MAP.get(code, code)}", stats)

        # 检查赔率区间
        for task_key, models in results.get("by_odds_range", {}).items():
            for model_name, ranges in models.items():
                for label, stats in ranges.items():
                    check_segment(f"[{task_key}/{model_name}] 赔率={label}", stats)

        # 检查水位区间
        for task_key, models in results.get("by_water_level", {}).items():
            for model_name, levels in models.items():
                for label, stats in levels.items():
                    check_segment(f"[{task_key}/{model_name}] 水位={label}", stats)

        return warnings_list

    def _print_warnings(self, warnings_list: list):
        """打印样本量预警。"""
        criticals = [w for w in warnings_list if w["level"] == "CRITICAL"]
        warns = [w for w in warnings_list if w["level"] == "WARNING"]

        if criticals:
            print(f"\n  === {len(criticals)} 个严重样本量问题 ===")
            for w in criticals:
                print(f"    [CRITICAL] {w['message']}")

        if warns:
            print(f"\n  === {len(warns)} 个低样本警告 ===")
            for w in warns:
                print(f"    [WARNING] {w['message']}")

        if not criticals and not warns:
            print(f"\n  [OK] 所有分析段样本量充足 (>={MIN_SAMPLE_STRONG})")

    # ── CSV 导出 ────────────────────────────────────────────────

    def _export_csv(self, results: dict):
        """导出 CSV 文件。"""
        out = self.output_dir

        # 1. 按联赛
        league_rows = []
        for task_key, models in results.get("by_league", {}).items():
            for model_name, leagues in models.items():
                for code, s in leagues.items():
                    if code == "ALL":
                        continue
                    league_rows.append({
                        "task": task_key,
                        "model": model_name,
                        "league_code": code,
                        "league_name": LEAGUE_MAP.get(code, code),
                        **{k: v for k, v in s.items() if k != "league_name"},
                    })
        if league_rows:
            pd.DataFrame(league_rows).to_csv(out / "validation_by_league.csv", index=False, encoding="utf-8-sig")
            print(f"  CSV: validation_by_league.csv ({len(league_rows)} 行)")

        # 2. 按赔率区间
        odds_rows = []
        for task_key, models in results.get("by_odds_range", {}).items():
            for model_name, ranges in models.items():
                for label, s in ranges.items():
                    if s.get("bets", 0) == 0:
                        continue
                    odds_rows.append({
                        "task": task_key,
                        "model": model_name,
                        "odds_range": label,
                        **{k: v for k, v in s.items() if k != "label"},
                    })
        if odds_rows:
            pd.DataFrame(odds_rows).to_csv(out / "validation_by_odds_range.csv", index=False, encoding="utf-8-sig")
            print(f"  CSV: validation_by_odds_range.csv ({len(odds_rows)} 行)")

        # 3. 按月
        month_rows = []
        for task_key, models in results.get("coverage", {}).get("tasks", {}).items():
            for model_name, cov in models.get("models", {}).items():
                for period, detail in cov.get("monthly_detail", {}).items():
                    month_rows.append({
                        "task": task_key,
                        "model": model_name,
                        "month": period,
                        "bets": detail["bets"],
                        "profit": detail["profit"],
                        "roi": round(detail["profit"] / detail["bets"], 4) if detail["bets"] > 0 else 0,
                    })
        if month_rows:
            pd.DataFrame(month_rows).to_csv(out / "validation_by_month.csv", index=False, encoding="utf-8-sig")
            print(f"  CSV: validation_by_month.csv ({len(month_rows)} 行)")

        # 4. 模型对比
        model_rows = []
        for task_key, models in results.get("by_league", {}).items():
            for model_name, leagues in models.items():
                all_s = leagues.get("ALL", {})
                model_rows.append({
                    "task": task_key,
                    "model": model_name,
                    **{k: v for k, v in all_s.items() if k != "league_name"},
                })
        if model_rows:
            pd.DataFrame(model_rows).to_csv(out / "validation_by_model.csv", index=False, encoding="utf-8-sig")
            print(f"  CSV: validation_by_model.csv ({len(model_rows)} 行)")

    # ── HTML 报告 ───────────────────────────────────────────────

    def _build_html(self, results: dict):
        ts = results.get("generated_at", datetime.now().isoformat())

        coverage_html = self._html_coverage(results)
        league_html = self._html_league(results)
        odds_html = self._html_odds(results)
        water_html = self._html_water(results)
        warnings_html = self._html_warnings(results.get("sample_warnings", []))

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>回测真实性验证报告 — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#b91c1c 0%,#991b1b 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; margin-bottom:8px; }}
.header p {{ opacity:0.8; font-size:14px; }}
.container {{ max-width:1400px; margin:0 auto; padding:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:24px; }}
.card h2 {{ font-size:16px; color:#16213e; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #e8e8e8; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; font-weight:600; color:#555; white-space:nowrap; }}
tr:hover {{ background:#f8f9fa; }}
h3 {{ font-size:14px; color:#333; margin:16px 0 8px 0; }}
.roi-pos {{ color:#10b981; font-weight:600; }}
.roi-neg {{ color:#ef4444; font-weight:600; }}
.warn-critical {{ background:#fee2e2; color:#991b1b; padding:4px 10px; border-radius:4px; font-weight:600; font-size:12px; }}
.warn-low {{ background:#fef3c7; color:#92400e; padding:4px 10px; border-radius:4px; font-weight:600; font-size:12px; }}
.warn-ok {{ background:#d1fae5; color:#065f46; padding:4px 10px; border-radius:4px; font-size:12px; }}
.stat-grid {{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:16px; }}
.stat-item {{ background:linear-gradient(135deg,#f8f9fa,#fff); border-radius:8px; padding:16px; text-align:center; border:1px solid #e8e8e8; }}
.stat-value {{ font-size:24px; font-weight:700; color:#0f3460; }}
.stat-label {{ font-size:12px; color:#666; margin-top:4px; }}
pre {{ background:#1a1a2e; color:#10b981; padding:16px; border-radius:8px; font-size:13px; overflow-x:auto; }}
</style>
</head>
<body>
<div class="header">
<h1>回测真实性验证报告</h1>
<p>生成时间: {ts[:19]} | 验证模块 v1.0</p>
</div>
<div class="container">

{warnings_html}

<div class="card"><h2>信号覆盖率分析</h2>{coverage_html}</div>

<div class="card"><h2>联赛拆分分析</h2>{league_html}</div>

<div class="card"><h2>赔率区间分析</h2>{odds_html}</div>

<div class="card"><h2>水位区间分析</h2>{water_html}</div>

<div class="card"><h2>方法论说明</h2>
<pre>
样本量阈值:
  - 低于 {MIN_SAMPLE_STRONG} 注: LOW SAMPLE WARNING — 标注为低样本
  - 低于 {MIN_SAMPLE_WEAK} 注: CRITICAL — 样本不足，不可作为稳定结论

赔率区间: 基于 European Decimal Odds
水位区间: 基于 Hong Kong Water (odds - 1.0)
  - 低水: water &lt; 0.85
  - 中水: 0.85 ≤ water ≤ 1.00
  - 高水: water &gt; 1.00

联赛覆盖: 英超/西甲/意甲/德甲/法甲 + 欧冠/欧联
</pre>
</div>
</div>
</body>
</html>"""

        html_path = self.output_dir / "validation_summary.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  HTML: {html_path}")

    def _html_coverage(self, results: dict) -> str:
        cov = results.get("coverage", {})
        total = cov.get("total_matches", 0)
        html = f'<div class="stat-grid"><div class="stat-item"><div class="stat-value">{total:,}</div><div class="stat-label">总比赛数</div></div>'

        tasks = cov.get("tasks", {})
        for task_key, task_cov in tasks.items():
            for model_name, m in task_cov.get("models", {}).items():
                html += f'<div class="stat-item"><div class="stat-value">{m.get("coverage_pct", 0):.1%}</div><div class="stat-label">{task_key}/{model_name} 覆盖率</div></div>'
                html += f'<div class="stat-item"><div class="stat-value">{m.get("total_bets", 0):,}</div><div class="stat-label">{task_key}/{model_name} 下注数</div></div>'
                html += f'<div class="stat-item"><div class="stat-value">{m.get("avg_bets_per_month", 0):.0f}</div><div class="stat-label">{task_key}/{model_name} 月均注数</div></div>'
        html += '</div>'

        # 月度/季度明细表
        for task_key, task_cov in tasks.items():
            for model_name, m in task_cov.get("models", {}).items():
                warn = m.get("coverage_warning")
                if warn:
                    html += f'<p style="color:#b91c1c;font-weight:600;margin-top:8px">⚠ {warn}</p>'

                monthly = m.get("monthly_detail", {})
                if monthly:
                    html += f'<h3>{task_key} / {model_name} — 月度下注频率</h3>'
                    html += '<table><tr><th>月份</th><th>下注数</th><th>收益</th><th>ROI</th></tr>'
                    for period, d in sorted(monthly.items()):
                        roi = d["profit"] / d["bets"] if d["bets"] > 0 else 0
                        roi_cls = "roi-pos" if roi > 0 else "roi-neg"
                        html += (f'<tr><td>{period}</td><td>{d["bets"]}</td>'
                                 f'<td>{d["profit"]:+.1f}</td>'
                                 f'<td class="{roi_cls}">{roi:+.2%}</td></tr>')
                    html += '</table>'

        return html

    def _html_league(self, results: dict) -> str:
        html = ""
        for task_key, models in results.get("by_league", {}).items():
            html += f"<h3>{task_key}</h3>"
            for model_name, leagues in models.items():
                html += f"<h4>{model_name}</h4><table>"
                html += "<tr><th>联赛</th><th>下注数</th><th>ROI</th><th>胜率</th><th>Sharpe</th><th>MaxDD</th><th>盈亏比</th><th>总收益</th><th>预警</th></tr>"
                for code, s in leagues.items():
                    roi = s.get("roi", 0)
                    roi_cls = "roi-pos" if roi > 0 else "roi-neg"
                    bets = s.get("bets", 0)
                    warn = _sample_warning(bets)
                    warn_html = ""
                    if warn:
                        cls = "warn-critical" if bets < MIN_SAMPLE_WEAK else "warn-low"
                        warn_html = f'<span class="{cls}">{warn}</span>'
                    html += (f'<tr><td><strong>{s.get("league_name", code)}</strong></td>'
                             f'<td>{bets}</td>'
                             f'<td class="{roi_cls}">{roi:+.2%}</td>'
                             f'<td>{s.get("win_rate", 0):.1%}</td>'
                             f'<td>{s.get("sharpe_ratio", 0):.2f}</td>'
                             f'<td>{s.get("max_drawdown_pct", 0):.1%}</td>'
                             f'<td>{s.get("profit_factor", 0):.2f}</td>'
                             f'<td>{s.get("total_profit", 0):+.1f}</td>'
                             f'<td>{warn_html}</td></tr>')
                html += "</table>"
        return html

    def _html_odds(self, results: dict) -> str:
        html = ""
        for task_key, models in results.get("by_odds_range", {}).items():
            html += f"<h3>{task_key}</h3>"
            for model_name, ranges in models.items():
                html += f"<h4>{model_name}</h4><table>"
                html += "<tr><th>赔率区间</th><th>下注数</th><th>ROI</th><th>胜率</th><th>Sharpe</th><th>MaxDD</th><th>盈亏比</th><th>预警</th></tr>"
                for label, s in ranges.items():
                    bets = s.get("bets", 0)
                    if bets == 0:
                        continue
                    roi = s.get("roi", 0)
                    roi_cls = "roi-pos" if roi > 0 else "roi-neg"
                    warn = _sample_warning(bets)
                    warn_html = ""
                    if warn:
                        cls = "warn-critical" if bets < MIN_SAMPLE_WEAK else "warn-low"
                        warn_html = f'<span class="{cls}">{warn}</span>'
                    html += (f'<tr><td><strong>{label}</strong></td>'
                             f'<td>{bets}</td>'
                             f'<td class="{roi_cls}">{roi:+.2%}</td>'
                             f'<td>{s.get("win_rate", 0):.1%}</td>'
                             f'<td>{s.get("sharpe_ratio", 0):.2f}</td>'
                             f'<td>{s.get("max_drawdown_pct", 0):.1%}</td>'
                             f'<td>{s.get("profit_factor", 0):.2f}</td>'
                             f'<td>{warn_html}</td></tr>')
                html += "</table>"
        return html

    def _html_water(self, results: dict) -> str:
        html = ""
        for task_key, models in results.get("by_water_level", {}).items():
            html += f"<h3>{task_key}</h3>"
            for model_name, levels in models.items():
                html += f"<h4>{model_name}</h4><table>"
                html += "<tr><th>水位区间</th><th>下注数</th><th>ROI</th><th>胜率</th><th>Sharpe</th><th>MaxDD</th><th>盈亏比</th><th>预警</th></tr>"
                for label, s in levels.items():
                    bets = s.get("bets", 0)
                    if bets == 0:
                        continue
                    roi = s.get("roi", 0)
                    roi_cls = "roi-pos" if roi > 0 else "roi-neg"
                    warn = _sample_warning(bets)
                    warn_html = ""
                    if warn:
                        cls = "warn-critical" if bets < MIN_SAMPLE_WEAK else "warn-low"
                        warn_html = f'<span class="{cls}">{warn}</span>'
                    html += (f'<tr><td><strong>{label}</strong></td>'
                             f'<td>{bets}</td>'
                             f'<td class="{roi_cls}">{roi:+.2%}</td>'
                             f'<td>{s.get("win_rate", 0):.1%}</td>'
                             f'<td>{s.get("sharpe_ratio", 0):.2f}</td>'
                             f'<td>{s.get("max_drawdown_pct", 0):.1%}</td>'
                             f'<td>{s.get("profit_factor", 0):.2f}</td>'
                             f'<td>{warn_html}</td></tr>')
                html += "</table>"
        return html

    def _html_warnings(self, warnings_list: list) -> str:
        if not warnings_list:
            return ""

        criticals = [w for w in warnings_list if w["level"] == "CRITICAL"]
        warns = [w for w in warnings_list if w["level"] == "WARNING"]

        html = '<div class="card" style="border-left:4px solid #ef4444"><h2>样本量预警</h2>'
        if criticals:
            html += f'<p style="color:#991b1b;font-weight:600">{len(criticals)} 个严重样本量问题:</p>'
            html += '<ul style="margin:8px 0;padding-left:20px">'
            for w in criticals:
                html += f'<li style="color:#991b1b;margin:4px 0"><strong>CRITICAL</strong> — {w["message"]}</li>'
            html += '</ul>'
        if warns:
            html += f'<p style="color:#92400e;font-weight:600">{len(warns)} 个低样本警告:</p>'
            html += '<ul style="margin:8px 0;padding-left:20px">'
            for w in warns:
                html += f'<li style="color:#92400e;margin:4px 0"><strong>WARNING</strong> — {w["message"]}</li>'
            html += '</ul>'
        html += '</div>'
        return html


# ── 辅助函数 ────────────────────────────────────────────────────

def _compute_segment_metrics(bd: pd.DataFrame) -> dict:
    """计算单个分段的全部指标。"""
    n = len(bd)
    if n == 0:
        return {"bets": 0, "error": "无数据"}

    profit_col = "profit" if "profit" in bd.columns else "return"
    profits = bd[profit_col].values
    wins = (profits > 0).sum()
    losses = (profits < 0).sum()
    resolved = wins + losses

    total_profit = float(profits.sum())
    roi = total_profit / (n * 100.0) if n > 0 else 0  # flat staking at 100/bet
    win_rate = wins / resolved if resolved > 0 else 0

    # 最大回撤
    equity = np.cumsum(profits)
    peak = np.maximum.accumulate(equity)
    dd_pct = float((peak - equity).max() / peak.max()) if len(peak) > 0 and peak.max() > 0 else 0

    # Sharpe
    sharpe = float(np.mean(profits) / np.std(profits, ddof=1)) if len(profits) > 1 and np.std(profits, ddof=1) > 0 else 0

    # 盈亏比
    gross_profit = profits[profits > 0].sum() if wins > 0 else 0
    gross_loss = abs(profits[profits < 0].sum()) if losses > 0 else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # 最长连胜/连黑
    max_win_streak = _max_streak(profits > 0, True)
    max_lose_streak = _max_streak(profits < 0, True)

    return {
        "bets": n,
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": round(win_rate, 4),
        "roi": round(roi, 4),
        "total_profit": round(total_profit, 2),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown_pct": round(dd_pct, 4),
        "max_drawdown_abs": round(float((np.maximum.accumulate(equity) - equity).max()), 2),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "max_win_streak": int(max_win_streak),
        "max_lose_streak": int(max_lose_streak),
        "avg_profit": round(float(np.mean(profits)), 4),
        "volatility": round(float(np.std(profits, ddof=1)), 4) if len(profits) > 1 else 0,
    }


def _classify_odds(decimal_odds: float) -> str:
    """将 decimal odds 分类到区间。"""
    if pd.isna(decimal_odds) or decimal_odds <= 0:
        return "未知"
    for lo, hi, label in ODDS_BINS:
        if lo <= decimal_odds < hi:
            return label
    return "未知"


def _classify_water(water: float) -> str:
    """将香港盘水位分类。"""
    if pd.isna(water) or water <= 0:
        return "未知"
    for lo, hi, label in WATER_BINS:
        if lo <= water < hi:
            return label
    return "未知"


def _sample_warning(bets: int) -> str:
    """返回样本量警告文本 (空字符串表示无警告)。"""
    if bets < MIN_SAMPLE_WEAK:
        return "LOW SAMPLE WARNING — 样本不足，不可作为稳定结论"
    elif bets < MIN_SAMPLE_STRONG:
        return "LOW SAMPLE WARNING"
    return ""


def _max_streak(condition: np.ndarray, value: bool) -> int:
    max_s, cur = 0, 0
    for v in condition:
        if v == value:
            cur += 1
            max_s = max(max_s, cur)
        else:
            cur = 0
    return max_s


def _safe_groupby(df: pd.DataFrame, group_col: str, agg_col: str,
                   agg_funcs) -> pd.DataFrame:
    """安全的 groupby 操作。"""
    valid = df[group_col].notna()
    if not valid.any():
        return pd.DataFrame()
    sub = df.loc[valid, [group_col, agg_col]]
    return sub.groupby(group_col)[agg_col].agg(agg_funcs)


# ── CLI ────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("回测真实性验证分析器 v1.0")
    print("=" * 60)

    analyzer = ValidationAnalyzer()
    try:
        results = analyzer.run()
        print("\n[OK] 验证完成")
    except Exception as e:
        print(f"\n[ERROR] 验证失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
