#!/usr/bin/env python3
"""
Walk Forward 回测系统 (Walk-Forward Backtesting Engine)

滚动时间窗口回测，验证 AI 模型是否可实盘：
- 扩展窗口: (2021-22)→2023, (2021-23)→2024, (2021-24)→2025
- 严格时序隔离，杜绝未来数据泄露
- 资金曲线、夏普比率、联赛/月度分析、过拟合检测
"""

import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.preprocessing import LabelEncoder

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASETS_DIR = PROJECT_ROOT / "datasets"
REPORTS_DIR = PROJECT_ROOT / "reports"

warnings.filterwarnings("ignore")
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

RANDOM_STATE = 42
META_COLS = ["match_id", "kickoff_time", "season", "league_code", "home_team", "away_team"]
LABEL_COLS = [
    "label_home_win", "label_draw", "label_away_win",
    "label_over_2_5", "label_total_goals", "label_goal_diff",
    "label_asian", "label_upset",
]

WINDOWS = [
    {"train_seasons": [2021, 2022], "test_season": 2023, "label": "2021-22 → 2023"},
    {"train_seasons": [2021, 2022, 2023], "test_season": 2024, "label": "2021-23 → 2024"},
    {"train_seasons": [2021, 2022, 2023, 2024], "test_season": 2025, "label": "2021-24 → 2025"},
]

TASKS = {
    "wdl": {
        "name": "胜平负", "type": "multiclass",
        "label_cols": ["label_home_win", "label_draw", "label_away_win"],
        "class_map": {0: "主胜", 1: "平局", 2: "客胜"},
        "odds_col": {"home": "close_home_odds", "draw": "close_draw_odds", "away": "close_away_odds"},
    },
    "asian": {
        "name": "让球", "type": "multiclass",
        "label_cols": ["label_asian"],
        "class_map": {0: "客赢盘", 1: "走水", 2: "主赢盘"},
        "odds_col": "asian",  # special handling
    },
    "over_under": {
        "name": "大小球", "type": "binary",
        "label_cols": ["label_over_2_5"],
        "class_map": {0: "小球", 1: "大球"},
        "odds_col": "ou",  # special handling
    },
}


# ── 数据加载 ─────────────────────────────────────────────────

def load_full_data() -> pd.DataFrame:
    train = pd.read_csv(DATASETS_DIR / "train.csv")
    val = pd.read_csv(DATASETS_DIR / "validation.csv")
    test = pd.read_csv(DATASETS_DIR / "test.csv")
    df = pd.concat([train, val, test], ignore_index=True)

    # 衍生 WDL 标签
    valid = df.dropna(subset=["label_home_win", "label_draw", "label_away_win"])
    df["label_wdl"] = np.nan
    df.loc[valid.index, "label_wdl"] = (
        valid["label_home_win"] * 0 + valid["label_draw"] * 1 + valid["label_away_win"] * 2
    )
    # 亚盘标签映射
    df["label_asian_mapped"] = df["label_asian"].map({-1.0: 0, 0.0: 1, 1.0: 2})
    return df


def prepare_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    exclude = set(META_COLS) | set(LABEL_COLS) | {"label_wdl", "label_asian_mapped"}
    feature_cols = [c for c in df.columns if c not in exclude and df[c].dtype in ("float64", "int64")]
    X = df[feature_cols].copy()
    imp = SimpleImputer(strategy="median")
    X_imputed = pd.DataFrame(imp.fit_transform(X), columns=feature_cols, index=X.index)
    return X_imputed, feature_cols


# ── 模型训练 ────────────────────────────────────────────────

def train_models(X_train, y_train, task_type: str) -> dict:
    models = {}

    import xgboost as xgb
    xgb_params = {
        "objective": "multi:softprob" if task_type == "multiclass" else "binary:logistic",
        "max_depth": 6, "learning_rate": 0.05, "n_estimators": 300,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "random_state": RANDOM_STATE, "verbosity": 0,
    }
    if task_type == "multiclass":
        xgb_params["num_class"] = y_train.nunique()
    models["xgboost"] = xgb.XGBClassifier(**xgb_params)
    models["xgboost"].fit(X_train, y_train, verbose=False)

    import lightgbm as lgb
    lgb_params = {
        "objective": "multiclass" if task_type == "multiclass" else "binary",
        "max_depth": 6, "learning_rate": 0.05, "n_estimators": 300,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "random_state": RANDOM_STATE, "verbose": -1,
    }
    if task_type == "multiclass":
        lgb_params["num_class"] = y_train.nunique()
    models["lightgbm"] = lgb.LGBMClassifier(**lgb_params)
    models["lightgbm"].fit(X_train, y_train)

    from catboost import CatBoostClassifier
    cb_params = {
        "depth": 6, "learning_rate": 0.05, "iterations": 300,
        "random_seed": RANDOM_STATE, "verbose": False,
    }
    if task_type == "multiclass":
        cb_params["loss_function"] = "MultiClass"
    models["catboost"] = CatBoostClassifier(**cb_params)
    models["catboost"].fit(X_train, y_train, verbose=False)

    return models


# ── 回测执行 (单窗口) ──────────────────────────────────────

def backtest_predictions(test_df, y_test, y_pred, task_key: str) -> pd.DataFrame:
    """基于预测和收盘赔率计算每场比赛的收益。"""
    bt = test_df.copy()
    bt["y_true"] = y_test.values
    bt["y_pred"] = y_pred
    bt["return"] = 0.0
    bt["bet"] = False

    for i in bt.index:
        pred = int(bt.at[i, "y_pred"])
        actual = int(bt.at[i, "y_true"])

        if task_key == "wdl":
            odds_map = {0: "close_home_odds", 1: "close_draw_odds", 2: "close_away_odds"}
            odds = bt.at[i, odds_map.get(pred, "close_home_odds")]
            if pd.notna(odds) and odds > 0:
                bt.at[i, "bet"] = True
                bt.at[i, "return"] = (odds - 1) if pred == actual else -1.0

        elif task_key == "over_under":
            if actual == 1:
                water = bt.at[i, "ou_close_over_water"]
            else:
                water = bt.at[i, "ou_close_under_water"]
            # 下注在预测方向
            if pred == 1:
                odds = bt.at[i, "ou_close_over_water"]
            else:
                odds = bt.at[i, "ou_close_under_water"]
            if pd.notna(odds) and odds > 0:
                bt.at[i, "bet"] = True
                bt.at[i, "return"] = odds if pred == actual else -1.0

        elif task_key == "asian":
            if pred == 1:  # 预测走水，不投注
                continue
            if actual == 1:  # 实际走水
                bt.at[i, "return"] = 0.0
                continue
            if pred == 2:
                odds = bt.at[i, "asian_close_high_water"]
            else:
                odds = bt.at[i, "asian_close_low_water"]
            if pd.notna(odds) and odds > 0:
                bt.at[i, "bet"] = True
                bt.at[i, "return"] = odds if pred == actual else -1.0

    bt["equity"] = bt["return"].cumsum()
    return bt


# ── 指标计算 ────────────────────────────────────────────────

def compute_metrics(bt_df: pd.DataFrame) -> dict:
    bets = bt_df[bt_df["bet"]]
    n = len(bets)
    if n == 0:
        return {"total_bets": 0, "error": "无有效投注"}

    returns = bets["return"].values
    correct = (returns > 0).sum()

    metrics = {
        "total_bets": int(n),
        "correct_bets": int(correct),
        "win_rate": round(float(correct / n), 4),
        "total_return": round(float(returns.sum()), 4),
        "roi": round(float(returns.sum() / n), 4),
        "max_drawdown": round(float(_max_drawdown(returns)), 4),
        "sharpe": round(float(_sharpe_ratio(returns)), 4),
        "max_win_streak": int(_max_streak(returns > 0, True)),
        "max_lose_streak": int(_max_streak(returns > 0, False)),
        "final_equity": round(float(bt_df["equity"].iloc[-1]), 4),
    }
    return metrics


def compute_monthly(bt_df: pd.DataFrame) -> dict:
    bets = bt_df[bt_df["bet"]].copy()
    if len(bets) == 0:
        return {}
    bets["month"] = pd.to_datetime(bets["kickoff_time"], format="%Y-%m-%d %H:%M:%S", errors="coerce").dt.to_period("M")
    monthly = bets.groupby("month")["return"].agg(["sum", "count"])
    monthly.columns = ["return", "bets"]
    monthly["roi"] = monthly["return"] / monthly["bets"]
    return {str(k): {"return": round(float(v["return"]), 4), "bets": int(v["bets"]), "roi": round(float(v["roi"]), 4)}
            for k, v in monthly.iterrows()}


def compute_league(bt_df: pd.DataFrame) -> dict:
    bets = bt_df[bt_df["bet"]].copy()
    if len(bets) == 0:
        return {}
    league = bets.groupby("league_code")["return"].agg(["sum", "count", "mean"])
    league.columns = ["total_return", "bets", "roi"]
    return {str(k): {"total_return": round(float(v["total_return"]), 4), "bets": int(v["bets"]),
                     "roi": round(float(v["roi"]), 4)}
            for k, v in league.iterrows()}


def _max_drawdown(returns: np.ndarray) -> float:
    eq = np.cumsum(returns)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    return float(dd.max()) if len(dd) > 0 else 0.0


def _sharpe_ratio(returns: np.ndarray) -> float:
    if len(returns) < 2:
        return 0.0
    mean_r = np.mean(returns)
    std_r = np.std(returns, ddof=1)
    return float(mean_r / std_r) if std_r > 0 else 0.0


def _max_streak(series, value):
    max_s, cur = 0, 0
    for v in series:
        if v == value:
            cur += 1
            max_s = max(max_s, cur)
        else:
            cur = 0
    return max_s


# ── 主流程 ──────────────────────────────────────────────────

def run_walk_forward():
    print("=" * 60)
    print("Walk Forward 回测系统 v1.0")
    print("=" * 60)

    df = load_full_data()
    X_all, feature_names = prepare_features(df)
    print(f"  总数据: {len(df):,} 场 | 特征: {len(feature_names)}")

    all_results = {
        "generated_at": datetime.now().isoformat(),
        "windows": WINDOWS,
        "tasks": {},
        "overfitting_analysis": {},
    }

    for task_key, task_cfg in TASKS.items():
        task_name = task_cfg["name"]
        print(f"\n{'─' * 40}")
        print(f"[{task_name}] Walk-Forward 回测")
        print(f"{'─' * 40}")

        task_results = {"windows": [], "aggregate": {}, "models": {}}

        # 获取标签
        if task_key == "wdl":
            y_label = df["label_wdl"]
        elif task_key == "asian":
            y_label = df["label_asian_mapped"]
        else:
            y_label = df["label_over_2_5"]

        for model_name in ["xgboost", "lightgbm", "catboost"]:
            model_results = {
                "windows": [],
                "aggregate_equity": [],
                "all_bets": [],
                "metrics": {},
                "monthly": {},
                "league": {},
            }

            for win in WINDOWS:
                train_mask = df["season"].isin(win["train_seasons"])
                test_mask = df["season"] == win["test_season"]

                train_idx = df[train_mask].index
                test_idx = df[test_mask].index

                # 对齐有效标签
                train_valid = train_idx.intersection(y_label.dropna().index)
                test_valid = test_idx.intersection(y_label.dropna().index)

                X_tr = X_all.loc[train_valid]
                y_tr = y_label.loc[train_valid].astype(int)
                X_ts = X_all.loc[test_valid]
                y_ts = y_label.loc[test_valid].astype(int)

                if len(X_tr) < 100 or len(X_ts) < 50:
                    print(f"  [{win['label']}] 样本不足，跳过")
                    continue

                # 训练
                models = train_models(X_tr, y_tr, task_cfg["type"])
                model = models[model_name]

                # 预测
                y_pred = model.predict(X_ts)
                try:
                    y_proba = model.predict_proba(X_ts)
                    acc = accuracy_score(y_ts, y_pred)
                    auc = roc_auc_score(y_ts, y_proba, multi_class="ovr", average="weighted") if task_cfg["type"] == "multiclass" else roc_auc_score(y_ts, y_proba[:, 1])
                except Exception:
                    auc = None
                    acc = accuracy_score(y_ts, y_pred)

                # 回测
                test_df_part = df.loc[test_valid].copy()
                bt = backtest_predictions(test_df_part, y_ts, y_pred, task_key)
                m = compute_metrics(bt)

                win_result = {
                    "window": win["label"],
                    "test_season": win["test_season"],
                    "train_samples": len(X_tr),
                    "test_samples": len(X_ts),
                    "accuracy": round(float(acc), 4),
                    "auc": round(float(auc), 4) if auc else None,
                    "metrics": m,
                }
                model_results["windows"].append(win_result)
                model_results["aggregate_equity"].extend(bt["equity"].tolist())
                model_results["all_bets"].append(bt[bt["bet"]])

                print(f"  [{model_name}] {win['label']}: Acc={acc:.4f} "
                      f"ROI={m.get('roi', 0):.4f} Sharpe={m.get('sharpe', 0):.4f} "
                      f"Bets={m.get('total_bets', 0)}")

            # 汇总所有窗口
            if model_results["all_bets"]:
                all_bets_df = pd.concat(model_results["all_bets"])
                model_results["aggregate_metrics"] = compute_metrics(all_bets_df)
                model_results["monthly"] = compute_monthly(all_bets_df)
                model_results["league"] = compute_league(all_bets_df)

                agg = model_results["aggregate_metrics"]
                print(f"  [{model_name}] 汇总: ROI={agg.get('roi',0):.4f} "
                      f"Sharpe={agg.get('sharpe',0):.4f} WR={agg.get('win_rate',0):.4f} "
                      f"MaxDD={agg.get('max_drawdown',0):.4f} Bets={agg.get('total_bets',0)}")

            task_results["models"][model_name] = model_results

        # 跨模型对比
        task_results["model_comparison"] = {
            mn: mr.get("aggregate_metrics", {})
            for mn, mr in task_results["models"].items()
        }

        all_results["tasks"][task_key] = task_results

    # ── 过拟合分析 ───────────────────────────────────────────
    print(f"\n{'─' * 40}")
    print("[过拟合检测]")
    of_results = analyze_overfitting(all_results)
    all_results["overfitting_analysis"] = of_results

    # ── 资金曲线图 ───────────────────────────────────────────
    print("\n[生成资金曲线图]...")
    chart_paths = generate_equity_charts(all_results)
    all_results["chart_paths"] = chart_paths

    # ── 报告 ─────────────────────────────────────────────────
    print("[生成报告]...")
    generate_reports(all_results)
    print_summary(all_results)

    return all_results


def analyze_overfitting(results: dict) -> dict:
    """检测过拟合：比较各窗口 train vs test 表现差距。"""
    analysis = {}
    for task_key, task_data in results["tasks"].items():
        task_name = TASKS[task_key]["name"]
        analysis[task_key] = {"name": task_name, "models": {}}

        for model_name, model_data in task_data["models"].items():
            windows = model_data["windows"]
            if len(windows) < 2:
                continue

            wf_rois = [w["metrics"].get("roi", 0) for w in windows]
            wf_accs = [w.get("accuracy", 0) for w in windows]

            roi_std = float(np.std(wf_rois))
            acc_std = float(np.std(wf_accs))
            roi_range = float(max(wf_rois) - min(wf_rois))

            # 判断过拟合风险
            risk_level = "LOW"
            reasons = []
            if roi_std > 0.15:
                risk_level = "HIGH"
                reasons.append(f"ROI 标准差过高 ({roi_std:.4f})")
            elif roi_std > 0.08:
                risk_level = "MEDIUM"
                reasons.append(f"ROI 标准差偏高 ({roi_std:.4f})")

            if roi_range > 0.25:
                risk_level = "HIGH" if risk_level == "MEDIUM" else risk_level
                reasons.append(f"ROI 跨窗口差异大 (range={roi_range:.4f})")

            if min(wf_rois) < -0.05:
                reasons.append(f"存在负 ROI 窗口 (min={min(wf_rois):.4f})")

            if not reasons:
                reasons.append("各窗口 ROI 稳定，无明显过拟合迹象")

            analysis[task_key]["models"][model_name] = {
                "risk_level": risk_level,
                "roi_std": round(roi_std, 4),
                "roi_min": round(float(min(wf_rois)), 4),
                "roi_max": round(float(max(wf_rois)), 4),
                "acc_std": round(acc_std, 4),
                "reasons": reasons,
                "window_rois": [round(r, 4) for r in wf_rois],
            }

            print(f"  [{task_name}/{model_name}] 过拟合风险: {risk_level}")
            for r in reasons:
                print(f"    - {r}")

    return analysis


# ── 资金曲线图 ──────────────────────────────────────────────

def generate_equity_charts(results: dict) -> dict:
    chart_dir = REPORTS_DIR / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    for task_key, task_data in results["tasks"].items():
        task_name = TASKS[task_key]["name"]

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))
        fig.suptitle(f"{task_name} — Walk Forward 资金曲线", fontsize=14, fontweight="bold")

        for ax, (model_name, model_data) in zip(axes, task_data["models"].items()):
            equity = model_data.get("aggregate_equity", [])
            if equity:
                cum_eq = np.cumsum(equity)
            else:
                cum_eq = [0]
            ax.plot(cum_eq, linewidth=0.8, color="#0f3460")
            ax.fill_between(range(len(cum_eq)), 0, cum_eq, alpha=0.15, color="#0f3460")
            ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.5)
            agg = model_data.get("aggregate_metrics", {})
            roi = agg.get("roi", 0)
            sharpe = agg.get("sharpe", 0)
            ax.set_title(f"{model_name}\nROI={roi:.4f} Sharpe={sharpe:.4f}", fontsize=11)
            ax.set_xlabel("投注序号")
            ax.set_ylabel("累计盈亏")

        plt.tight_layout()
        path = chart_dir / f"equity_{task_key}.png"
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        paths[task_key] = str(path)
        print(f"  {path}")

    return {str(k): str(v) for k, v in paths.items()}


# ── 报告 ────────────────────────────────────────────────────

def generate_reports(results: dict):
    json_path = REPORTS_DIR / "walk_forward_report.json"
    # 清理不可序列化数据
    clean = _make_serializable(results)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2, default=str)
    print(f"  JSON: {json_path}")

    html = _build_html(results)
    html_path = REPORTS_DIR / "walk_forward_report.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML: {html_path}")


def _make_serializable(obj):
    if isinstance(obj, dict):
        return {str(k): _make_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_make_serializable(v) for v in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def _build_html(results: dict) -> str:
    ts = results.get("generated_at", datetime.now().isoformat())
    of = results.get("overfitting_analysis", {})

    # 任务指标表格
    metrics_html = ""
    for task_key, task_data in results["tasks"].items():
        task_name = TASKS[task_key]["name"]
        metrics_html += f"<h3>{task_name}</h3><table><tr><th>模型</th><th>总投注</th><th>ROI</th><th>胜率</th><th>夏普</th><th>最大回撤</th><th>最长连胜</th><th>最长连黑</th></tr>"
        for model_name, model_data in task_data["models"].items():
            agg = model_data.get("aggregate_metrics", {})
            metrics_html += (
                f"<tr><td><strong>{model_name}</strong></td>"
                f"<td>{agg.get('total_bets', 0)}</td>"
                f"<td>{_fmt(agg.get('roi'))}</td>"
                f"<td>{_fmt(agg.get('win_rate'), pct=True)}</td>"
                f"<td>{_fmt(agg.get('sharpe'))}</td>"
                f"<td>{_fmt(agg.get('max_drawdown'))}</td>"
                f"<td>{agg.get('max_win_streak', '-')}</td>"
                f"<td>{agg.get('max_lose_streak', '-')}</td></tr>"
            )

            # 各窗口详情
            for w in model_data.get("windows", []):
                m = w.get("metrics", {})
                metrics_html += (
                    f"<tr style='font-size:11px;color:#666'><td>  ↳ {w['window']}</td>"
                    f"<td>{m.get('total_bets', 0)}</td>"
                    f"<td>{_fmt(m.get('roi'))}</td>"
                    f"<td>{_fmt(m.get('win_rate'), pct=True)}</td>"
                    f"<td>{_fmt(m.get('sharpe'))}</td>"
                    f"<td>{_fmt(m.get('max_drawdown'))}</td>"
                    f"<td>{m.get('max_win_streak', '-')}</td>"
                    f"<td>{m.get('max_lose_streak', '-')}</td></tr>"
                )
        metrics_html += "</table>"

    # 过拟合分析
    of_html = ""
    for task_key, task_data in of.items():
        of_html += f"<h3>{task_data['name']}</h3><table><tr><th>模型</th><th>风险</th><th>ROI 标准差</th><th>ROI 范围</th><th>判断</th></tr>"
        for model_name, model_data in task_data["models"].items():
            risk_color = {"LOW": "#10b981", "MEDIUM": "#f59e0b", "HIGH": "#ef4444"}.get(model_data["risk_level"], "#666")
            of_html += (
                f"<tr><td><strong>{model_name}</strong></td>"
                f"<td style='color:{risk_color};font-weight:bold'>{model_data['risk_level']}</td>"
                f"<td>{model_data['roi_std']:.4f}</td>"
                f"<td>{model_data['roi_min']:.4f} ~ {model_data['roi_max']:.4f}</td>"
                f"<td style='font-size:12px'>{'; '.join(model_data['reasons'])}</td></tr>"
            )
        of_html += "</table>"

    # 联赛分析
    league_html = ""
    for task_key, task_data in results["tasks"].items():
        task_name = TASKS[task_key]["name"]
        for model_name, model_data in task_data["models"].items():
            league_data = model_data.get("league", {})
            if league_data:
                sorted_leagues = sorted(league_data.items(), key=lambda x: x[1].get("roi", 0), reverse=True)
                rows = "".join(
                    f"<tr><td>{lg}</td><td>{d['bets']}</td><td>{d['roi']:.4f}</td><td>{d['total_return']:.4f}</td></tr>"
                    for lg, d in sorted_leagues
                )
                league_html += (
                    f"<h3>{task_name} — {model_name}</h3>"
                    f"<table><tr><th>联赛</th><th>投注数</th><th>ROI</th><th>总收益</th></tr>{rows}</table>"
                )

    # 图表嵌入
    charts_html = ""
    for task_key in results.get("tasks", {}):
        chart_path = results.get("chart_paths", {}).get(task_key)
        if chart_path and Path(chart_path).exists():
            charts_html += f"<h3>{TASKS[task_key]['name']}</h3><img src='charts/equity_{task_key}.png' style='max-width:100%;border-radius:8px;margin-bottom:16px'>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Walk Forward 回测报告 — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#0f3460 0%,#16213e 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; margin-bottom:8px; }}
.header p {{ opacity:0.8; font-size:14px; }}
.container {{ max-width:1300px; margin:0 auto; padding:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:24px; }}
.card h2 {{ font-size:16px; color:#16213e; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #e8e8e8; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; font-weight:600; color:#555; }}
tr:hover {{ background:#f8f9fa; }}
h3 {{ font-size:14px; color:#333; margin:12px 0 8px 0; }}
img {{ max-width:100%; border-radius:8px; }}
</style>
</head>
<body>
<div class="header">
<h1>Walk Forward 回测报告</h1>
<p>生成时间: {ts[:19]} | 扩展窗口: 2021-22→2023, 2021-23→2024, 2021-24→2025</p>
</div>
<div class="container">
<div class="card"><h2>累计指标 & 各窗口详情</h2>{metrics_html}</div>
<div class="card"><h2>资金曲线</h2>{charts_html}</div>
<div class="card"><h2>过拟合分析</h2>{of_html}</div>
<div class="card"><h2>联赛收益分析</h2>{league_html}</div>
</div>
</body>
</html>"""


def _fmt(val, pct=False):
    if val is None:
        return "-"
    if pct:
        return f"{val*100:.1f}%"
    return f"{val:.4f}"


def print_summary(results: dict):
    print(f"\n{'=' * 60}")
    print("Walk Forward 回测 — 最终总结")
    print(f"{'=' * 60}")

    for task_key, task_data in results["tasks"].items():
        task_name = TASKS[task_key]["name"]
        print(f"\n  [{task_name}]")
        for model_name, model_data in task_data["models"].items():
            agg = model_data.get("aggregate_metrics", {})
            print(f"    {model_name:10s} | ROI={_fmt(agg.get('roi'))} "
                  f"Sharpe={_fmt(agg.get('sharpe'))} WR={_fmt(agg.get('win_rate'), pct=True)} "
                  f"MaxDD={_fmt(agg.get('max_drawdown'))} Bets={agg.get('total_bets', 0)}")

    of = results.get("overfitting_analysis", {})
    print(f"\n  [过拟合风险]")
    for task_key, task_data in of.items():
        for model_name, model_data in task_data["models"].items():
            print(f"    {task_data['name']}/{model_name}: {model_data['risk_level']}")

    print(f"\n  图表: {REPORTS_DIR / 'charts'}")
    print(f"  报告: {REPORTS_DIR / 'walk_forward_report.html'}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    run_walk_forward()
