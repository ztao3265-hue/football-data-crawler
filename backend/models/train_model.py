#!/usr/bin/env python3
"""
AI 模型训练系统 (Model Training Pipeline)

使用 XGBoost / LightGBM / CatBoost 训练足球比赛预测模型：
A. 胜平负 (WDL) — 3-class
B. 大小球 (O/U) — binary
C. 让球 (Asian) — 3-class

含自动回测、特征重要性、HTML+JSON 报告。
"""

import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
    confusion_matrix,
)
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import LabelEncoder, StandardScaler

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASETS_DIR = PROJECT_ROOT / "datasets"
MODELS_DIR = PROJECT_ROOT / "models"
REPORTS_DIR = PROJECT_ROOT / "reports"

warnings.filterwarnings("ignore")

# ── 配置 ────────────────────────────────────────────────────

RANDOM_STATE = 42
META_COLS = ["match_id", "kickoff_time", "season", "league_code", "home_team", "away_team"]
LABEL_COLS = [
    "label_home_win", "label_draw", "label_away_win",
    "label_over_2_5", "label_total_goals", "label_goal_diff",
    "label_asian", "label_upset",
]

TASKS = {
    "wdl": {
        "name": "胜平负",
        "label": None,  # computed: 0=home, 1=draw, 2=away
        "type": "multiclass",
        "classes": ["主胜", "平局", "客胜"],
    },
    "over_under": {
        "name": "大小球",
        "label": "label_over_2_5",
        "type": "binary",
        "classes": ["小球", "大球"],
    },
    "asian": {
        "name": "让球",
        "label": "label_asian",
        "type": "multiclass",
        "classes": ["客赢盘", "走水", "主赢盘"],
        "class_map": {-1.0: 0, 0.0: 1, 1.0: 2},
    },
}


# ── 数据加载 & 预处理 ───────────────────────────────────────

def load_and_preprocess() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """加载 train/val/test 并预处理。"""
    train = pd.read_csv(DATASETS_DIR / "train.csv")
    val = pd.read_csv(DATASETS_DIR / "validation.csv")
    test = pd.read_csv(DATASETS_DIR / "test.csv")

    for df in [train, val, test]:
        # 生成 WDL 标签
        valid = df.dropna(subset=["label_home_win", "label_draw", "label_away_win"])
        wdl = valid["label_home_win"] * 0 + valid["label_draw"] * 1 + valid["label_away_win"] * 2
        df["label_wdl"] = wdl.astype(int)

        # 重映射 asian label: -1→0, 0→1, 1→2
        df["label_asian_mapped"] = df["label_asian"].map({-1.0: 0, 0.0: 1, 1.0: 2})

    return train, val, test


def prepare_features(
    train: pd.DataFrame, val: pd.DataFrame, test: pd.DataFrame
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, List[str]]:
    """提取特征矩阵，处理缺失值和编码。"""
    # 确定特征列
    exclude = set(META_COLS) | set(LABEL_COLS) | {"label_wdl", "label_asian_mapped"}
    feature_cols = [c for c in train.columns if c not in exclude]
    numeric_cols = [c for c in feature_cols if train[c].dtype in ("float64", "int64")]
    cat_cols = [c for c in feature_cols if c not in numeric_cols]

    def transform(df: pd.DataFrame, imputer=None, encoders=None) -> pd.DataFrame:
        X = df[feature_cols].copy()

        # 缺失值填充
        if imputer is None:
            imputer = SimpleImputer(strategy="median")
            X_num = pd.DataFrame(imputer.fit_transform(X[numeric_cols]), columns=numeric_cols)
        else:
            X_num = pd.DataFrame(imputer.transform(X[numeric_cols]), columns=numeric_cols)

        # 类别编码
        X_cat_parts = []
        for col in cat_cols:
            le = LabelEncoder()
            if encoders is None:
                X_cat_parts.append(pd.Series(le.fit_transform(X[col].astype(str)), name=col))
                if encoders is None:
                    encoders = {}
                encoders[col] = le
            else:
                known = set(encoders[col].classes_)
                mapped = X[col].astype(str).apply(lambda x: x if x in known else "UNKNOWN")
                X_cat_parts.append(pd.Series(
                    [encoders[col].transform([v])[0] if v != "UNKNOWN" else -1 for v in mapped],
                    name=col,
                ))

        result = pd.concat([X_num] + X_cat_parts, axis=1)
        return result, imputer, encoders

    X_train, imp, enc = transform(train)
    X_val, _, _ = transform(val, imp, enc)
    X_test, _, _ = transform(test, imp, enc)

    all_feature_cols = list(X_train.columns)
    return X_train, X_val, X_test, all_feature_cols


def get_labels(df: pd.DataFrame, task: str) -> pd.Series:
    """获取某任务的标签（仅有效样本）。"""
    if task == "wdl":
        return df["label_wdl"].dropna().astype(int)
    elif task == "asian":
        return df["label_asian_mapped"].dropna().astype(int)
    else:
        return df[TASKS[task]["label"]].dropna().astype(int)


# ── 模型训练 ────────────────────────────────────────────────

def train_xgboost(X, y, task_type: str):
    import xgboost as xgb
    params = {
        "objective": "multi:softprob" if task_type == "multiclass" else "binary:logistic",
        "eval_metric": "mlogloss" if task_type == "multiclass" else "logloss",
        "max_depth": 6,
        "learning_rate": 0.05,
        "n_estimators": 500,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "random_state": RANDOM_STATE,
        "verbosity": 0,
    }
    if task_type == "multiclass":
        params["num_class"] = y.nunique()
    model = xgb.XGBClassifier(**params)
    model.fit(X, y, verbose=False)
    return model


def train_lightgbm(X, y, task_type: str):
    import lightgbm as lgb
    params = {
        "objective": "multiclass" if task_type == "multiclass" else "binary",
        "metric": "multi_logloss" if task_type == "multiclass" else "binary_logloss",
        "max_depth": 6,
        "learning_rate": 0.05,
        "n_estimators": 500,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "random_state": RANDOM_STATE,
        "verbose": -1,
    }
    if task_type == "multiclass":
        params["num_class"] = y.nunique()
    model = lgb.LGBMClassifier(**params)
    model.fit(X, y)
    return model


def train_catboost(X, y, task_type: str):
    from catboost import CatBoostClassifier
    params = {
        "depth": 6,
        "learning_rate": 0.05,
        "iterations": 500,
        "random_seed": RANDOM_STATE,
        "verbose": False,
    }
    if task_type == "multiclass":
        params["loss_function"] = "MultiClass"
        params["classes_count"] = y.nunique()
    else:
        params["loss_function"] = "Logloss"
    model = CatBoostClassifier(**params)
    model.fit(X, y, verbose=False)
    return model


TRAINERS = {
    "xgboost": train_xgboost,
    "lightgbm": train_lightgbm,
    "catboost": train_catboost,
}


# ── 评估指标 ────────────────────────────────────────────────

def evaluate(y_true, y_pred, y_proba, task_type: str, task_name: str) -> dict:
    """计算完整评估指标。"""
    n_classes = len(np.unique(y_true))

    metrics = {}
    metrics["accuracy"] = round(float(accuracy_score(y_true, y_pred)), 4)
    metrics["precision"] = round(float(precision_score(y_true, y_pred, average="weighted", zero_division=0)), 4)
    metrics["recall"] = round(float(recall_score(y_true, y_pred, average="weighted", zero_division=0)), 4)
    metrics["f1"] = round(float(f1_score(y_true, y_pred, average="weighted", zero_division=0)), 4)

    # LogLoss
    if y_proba is not None:
        try:
            if task_type == "binary":
                metrics["logloss"] = round(float(log_loss(y_true, y_proba[:, 1])), 4)
            else:
                metrics["logloss"] = round(float(log_loss(y_true, y_proba)), 4)
        except Exception:
            metrics["logloss"] = None
    else:
        metrics["logloss"] = None

    # AUC
    if y_proba is not None:
        try:
            if task_type == "binary":
                metrics["auc"] = round(float(roc_auc_score(y_true, y_proba[:, 1])), 4)
            else:
                metrics["auc"] = round(float(roc_auc_score(y_true, y_proba, multi_class="ovr", average="weighted")), 4)
        except Exception:
            metrics["auc"] = None
    else:
        metrics["auc"] = None

    # 混淆矩阵
    cm = confusion_matrix(y_true, y_pred)
    metrics["confusion_matrix"] = cm.tolist()

    # 各类别指标
    if task_type == "multiclass":
        for i in range(n_classes):
            tp = cm[i, i]
            fp = cm[:, i].sum() - tp
            fn = cm[i, :].sum() - tp
            prec = tp / (tp + fp) if (tp + fp) > 0 else 0
            rec = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
            metrics[f"class_{i}_precision"] = round(float(prec), 4)
            metrics[f"class_{i}_recall"] = round(float(rec), 4)
            metrics[f"class_{i}_f1"] = round(float(f1), 4)

    return metrics


# ── 回测 ────────────────────────────────────────────────────

def backtest(df: pd.DataFrame, y_true, y_pred, y_proba, task: str) -> dict:
    """基于收盘赔率进行回测。"""
    valid_idx = y_true.index
    bt_df = df.loc[valid_idx].copy()
    bt_df["y_true"] = y_true.values
    bt_df["y_pred"] = y_pred

    result = {}
    n = len(bt_df)
    result["total_bets"] = n

    # 胜率
    correct = (bt_df["y_true"] == bt_df["y_pred"]).sum()
    result["win_rate"] = round(float(correct / n), 4) if n > 0 else 0

    # ROI 模拟: 下注1单位于预测结果, 用收盘赔率计算回报
    if task == "wdl":
        result["roi"] = _backtest_wdl(bt_df)
        result["roi_note"] = "基于收盘欧赔，仅对预测为热门方向时下注"
    elif task == "over_under":
        result["roi"] = _backtest_ou(bt_df)
        result["roi_note"] = "基于大小球收盘水位"
    elif task == "asian":
        result["roi"] = _backtest_asian(bt_df)
        result["roi_note"] = "基于亚盘收盘水位"

    # 累计收益曲线
    if result["roi"] is not None:
        cumulative = np.cumsum(bt_df["_returns"] if "_returns" in bt_df.columns else [0]*n)
        peak = np.maximum.accumulate(cumulative)
        dd = peak - cumulative
        result["max_drawdown"] = round(float(dd.max()), 4)
    else:
        result["max_drawdown"] = None

    # 连胜/连黑
    correct_series = (bt_df["y_true"] == bt_df["y_pred"]).astype(int)
    result["max_win_streak"] = int(_max_streak(correct_series, 1))
    result["max_lose_streak"] = int(_max_streak(correct_series, 0))

    return result


def _backtest_wdl(df: pd.DataFrame) -> float | None:
    """WDL 回测: 在预测结果上模拟下注1单位，用收盘赔率。"""
    col_map = {0: "close_home_odds", 1: "close_draw_odds", 2: "close_away_odds"}
    returns = []
    for _, row in df.iterrows():
        pred = int(row["y_pred"])
        actual = int(row["y_true"])
        odds_col = col_map.get(pred)
        odds = row.get(odds_col)
        if odds is None or pd.isna(odds) or odds <= 0:
            returns.append(0)
            continue
        if pred == actual:
            returns.append(odds - 1)  # profit
        else:
            returns.append(-1)  # lose stake
    arr = np.array(returns)
    df["_returns"] = arr
    total_return = arr.sum()
    roi = total_return / len(arr) if len(arr) > 0 else 0
    return round(float(roi), 4)


def _backtest_ou(df: pd.DataFrame) -> float | None:
    """O/U 回测。水位为香港盘风格, water 即 profit。"""
    returns = []
    for _, row in df.iterrows():
        pred = int(row["y_pred"])
        actual = int(row["y_true"])
        water_col = "ou_close_over_water" if pred == 1 else "ou_close_under_water"
        water = row.get(water_col)
        if water is None or pd.isna(water) or water <= 0:
            returns.append(0)
            continue
        if pred == actual:
            returns.append(water)  # 水位即利润
        else:
            returns.append(-1)
    arr = np.array(returns)
    df["_returns"] = arr
    roi = arr.sum() / len(arr) if len(arr) > 0 else 0
    return round(float(roi), 4)


def _backtest_asian(df: pd.DataFrame) -> float | None:
    """亚盘回测: 0=客赢盘, 1=走水, 2=主赢盘。水位为香港盘风格。"""
    returns = []
    for _, row in df.iterrows():
        pred = int(row["y_pred"])
        actual = int(row["y_true"])
        if pred == 1:  # 预测走水，跳过下注
            returns.append(0)
            continue
        if actual == 1:  # 实际走水，退款
            returns.append(0)
            continue

        if pred == 2:  # 预测主赢盘，下注 high_water
            water = row.get("asian_close_high_water")
        else:  # pred == 0, 预测客赢盘，下注 low_water
            water = row.get("asian_close_low_water")

        if water is None or pd.isna(water) or water <= 0:
            returns.append(0)
            continue
        if pred == actual:
            returns.append(water)  # 水位即利润
        else:
            returns.append(-1)
    arr = np.array(returns)
    df["_returns"] = arr
    roi = arr.sum() / len(arr) if len(arr) > 0 else 0
    return round(float(roi), 4)


def _max_streak(series, value):
    max_s = 0
    cur = 0
    for v in series:
        if v == value:
            cur += 1
            max_s = max(max_s, cur)
        else:
            cur = 0
    return max_s


# ── 特征重要性 ──────────────────────────────────────────────

def get_feature_importance(model, feature_names: list, model_type: str) -> dict:
    """提取特征重要性排名。"""
    if model_type == "xgboost":
        imp = model.feature_importances_
    elif model_type == "lightgbm":
        imp = model.feature_importances_
    elif model_type == "catboost":
        imp = model.get_feature_importance()
    else:
        return {}

    ranked = sorted(zip(feature_names, imp), key=lambda x: x[1], reverse=True)
    return {name: round(float(score), 6) for name, score in ranked}


# ── 报告生成 ────────────────────────────────────────────────

def generate_reports(all_results: dict):
    """生成 HTML + JSON 报告。"""
    json_path = REPORTS_DIR / "model_report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"  JSON: {json_path}")

    html = _build_html(all_results)
    html_path = REPORTS_DIR / "model_report.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML: {html_path}")


def _build_html(results: dict) -> str:
    r = results
    ts = r.get("generated_at", datetime.now().isoformat())

    # 指标卡片
    metrics_html = ""
    for task_key, task_data in r["tasks"].items():
        task_name = task_data["name"]
        metrics_html += f"<h3 style='margin-top:24px'>{task_name} ({TASKS[task_key]['type']})</h3>"
        metrics_html += "<table><tr><th>模型</th><th>Accuracy</th><th>Precision</th><th>Recall</th><th>F1</th><th>AUC</th><th>LogLoss</th><th>ROI</th><th>胜率</th></tr>"
        for model_name, model_data in task_data["models"].items():
            m = model_data["metrics"]
            bt = model_data.get("backtest", {})
            roi_str = f"{bt.get('roi', '-'):.4f}" if isinstance(bt.get('roi'), (int, float)) else "-"
            wr_str = f"{bt.get('win_rate', '-'):.2%}" if isinstance(bt.get('win_rate'), (int, float)) else "-"
            metrics_html += (
                f"<tr><td><strong>{model_name}</strong></td>"
                f"<td>{m.get('accuracy', '-')}</td>"
                f"<td>{m.get('precision', '-')}</td>"
                f"<td>{m.get('recall', '-')}</td>"
                f"<td>{m.get('f1', '-')}</td>"
                f"<td>{m.get('auc', '-')}</td>"
                f"<td>{m.get('logloss', '-')}</td>"
                f"<td>{roi_str}</td>"
                f"<td>{wr_str}</td></tr>"
            )
        metrics_html += "</table>"

    # 回测详情
    backtest_html = ""
    for task_key, task_data in r["tasks"].items():
        task_name = task_data["name"]
        backtest_html += f"<h3 style='margin-top:24px'>{task_name} — 回测</h3>"
        backtest_html += "<table><tr><th>模型</th><th>ROI</th><th>胜率</th><th>最大回撤</th><th>最长连胜</th><th>最长连黑</th></tr>"
        for model_name, model_data in task_data["models"].items():
            bt = model_data.get("backtest", {})
            backtest_html += (
                f"<tr><td><strong>{model_name}</strong></td>"
                f"<td>{bt.get('roi', '-')}</td>"
                f"<td>{bt.get('win_rate', '-')}</td>"
                f"<td>{bt.get('max_drawdown', '-')}</td>"
                f"<td>{bt.get('max_win_streak', '-')}</td>"
                f"<td>{bt.get('max_lose_streak', '-')}</td></tr>"
            )
        backtest_html += "</table>"

    # 特征重要性
    feat_html = ""
    for task_key, task_data in r["tasks"].items():
        task_name = task_data["name"]
        for model_name, model_data in task_data["models"].items():
            fi = model_data.get("feature_importance", {})
            if fi:
                top10 = list(fi.items())[:10]
                rows = "".join(
                    f"<tr><td>{k}</td><td>{v:.6f}</td></tr>"
                    for k, v in top10
                )
                feat_html += (
                    f"<h3 style='margin-top:16px'>{task_name} — {model_name} TOP-10</h3>"
                    f"<table><tr><th>特征</th><th>重要性</th></tr>{rows}</table>"
                )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>AI 模型训练报告 — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#0f3460 0%,#16213e 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; margin-bottom:8px; }}
.header p {{ opacity:0.8; font-size:14px; }}
.container {{ max-width:1300px; margin:0 auto; padding:24px; }}
.grid-3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:16px; }}
.card h2 {{ font-size:16px; color:#16213e; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #e8e8e8; }}
.stat-big {{ font-size:36px; font-weight:700; color:#0f3460; }}
.stat-label {{ font-size:13px; color:#666; margin-top:4px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; font-weight:600; color:#555; }}
tr:hover {{ background:#f8f9fa; }}
.badge-ok {{ background:#d1fae5; color:#065f46; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600; }}
h3 {{ font-size:14px; color:#333; margin-bottom:8px; }}
</style>
</head>
<body>
<div class="header">
<h1>AI 模型训练报告</h1>
<p>生成时间: {ts[:19]} | 模型: XGBoost + LightGBM + CatBoost</p>
</div>

<div class="container">

<div class="card"><h2>评估指标</h2>{metrics_html}</div>
<div class="card"><h2>回测结果</h2>{backtest_html}</div>
<div class="card"><h2>特征重要性 TOP-10</h2>{feat_html}</div>
{r.get("tests_html", "")}
</div>

</div>
</body>
</html>"""


# ── 主流程 ──────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("AI 模型训练系统 v1.0")
    print("=" * 60)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 加载数据
    print("\n[1/6] 加载数据...")
    train, val, test = load_and_preprocess()
    print(f"  Train: {len(train):,} | Val: {len(val):,} | Test: {len(test):,}")

    # 2. 预处理
    print("[2/6] 特征预处理...")
    X_train, X_val, X_test, feature_names = prepare_features(train, val, test)
    print(f"  特征数: {len(feature_names)}")

    # 3. 训练 + 评估
    print("[3/6] 训练模型...")
    all_results = {
        "generated_at": datetime.now().isoformat(),
        "tasks": {},
        "feature_count": len(feature_names),
        "feature_names": feature_names,
        "train_samples": len(train),
        "val_samples": len(val),
        "test_samples": len(test),
    }

    for task_key, task_config in TASKS.items():
        task_name = task_config["name"]
        task_type = task_config["type"]
        print(f"\n  ── {task_name} ({task_type}) ──")

        y_train = get_labels(train, task_key)
        y_val = get_labels(val, task_key)
        y_test = get_labels(test, task_key)

        # 对齐 X 和 y
        X_tr = X_train.loc[y_train.index]
        X_v = X_val.loc[y_val.index]
        X_ts = X_test.loc[y_test.index]

        # 合并 train + val 做最终训练
        X_full = pd.concat([X_tr, X_v])
        y_full = pd.concat([y_train, y_val])

        print(f"    Train+Val: {len(X_full):,} | Test: {len(X_ts):,}")

        task_results = {"name": task_name, "models": {}}

        for model_name, trainer in TRAINERS.items():
            print(f"    [{model_name}] 训练中...")
            try:
                model = trainer(X_full, y_full, task_type)
            except Exception as e:
                print(f"      训练失败: {e}")
                task_results["models"][model_name] = {"error": str(e)}
                continue

            # 预测
            y_pred = model.predict(X_ts)
            try:
                y_proba = model.predict_proba(X_ts)
            except Exception:
                y_proba = None

            # 评估
            metrics = evaluate(y_test, y_pred, y_proba, task_type, task_name)
            print(f"      Acc={metrics['accuracy']:.4f}  F1={metrics['f1']:.4f}  AUC={metrics.get('auc', '-')}")

            # 回测
            bt = backtest(test, y_test, y_pred, y_proba, task_key)
            if bt.get("roi") is not None:
                print(f"      ROI={bt['roi']:.4f}  WinRate={bt['win_rate']:.4f}  MaxDD={bt.get('max_drawdown', '-')}")

            # 特征重要性
            fi = get_feature_importance(model, feature_names, model_name)

            # 保存模型
            model_path = MODELS_DIR / f"{model_name}_{task_key}.pkl"
            joblib.dump(model, model_path)
            print(f"      已保存: {model_path}")

            task_results["models"][model_name] = {
                "metrics": metrics,
                "backtest": bt,
                "feature_importance": fi,
                "model_path": str(model_path),
            }

        all_results["tasks"][task_key] = task_results

    # 4. 报告 (先生成，测试中检查报告文件)
    print("\n[4/6] 生成报告...")
    generate_reports(all_results)

    # 5. 自动测试
    print("\n[5/6] 自动测试...")
    test_results = run_tests(all_results, test, X_test)
    all_results["tests"] = test_results
    all_results["tests_html"] = (
        f'<div class="card"><h2>自动测试: {test_results["passed"]}/{test_results["passed"]+test_results["failed"]} 通过</h2>'
        + "".join(f'<p style="color:red">FAIL: {e}</p>' for e in test_results.get("errors", []))
        + "".join(f'<p style="color:green">PASS: {p}</p>' for p in test_results.get("passed_msgs", []))
        + "</div>"
    )

    # 重新生成报告 (含测试结果)
    generate_reports(all_results)

    # 6. 最终总结
    print("\n[6/6] 最终总结")
    print_summary(all_results)

    if test_results["failed"] > 0:
        sys.exit(1)


def run_tests(results: dict, test_df: pd.DataFrame, X_test: pd.DataFrame) -> dict:
    passed = 0
    failed = 0
    errors = []
    passed_msgs = []

    def check(condition, msg):
        nonlocal passed, failed
        if condition:
            passed += 1
            passed_msgs.append(msg)
            print(f"  PASS: {msg}")
        else:
            failed += 1
            errors.append(msg)
            print(f"  FAIL: {msg}")

    # 1. 所有模型文件存在
    print("\n## 1. 模型持久化")
    for task_key in TASKS:
        for model_name in TRAINERS:
            path = MODELS_DIR / f"{model_name}_{task_key}.pkl"
            check(path.exists(), f"{model_name}_{task_key}.pkl 存在")

    # 2. 模型指标有效
    print("\n## 2. 模型指标")
    for task_key, task_data in results["tasks"].items():
        for model_name, model_data in task_data["models"].items():
            if "error" in model_data:
                check(False, f"[{task_key}/{model_name}] 训练失败: {model_data['error']}")
                continue
            m = model_data["metrics"]
            check(m.get("accuracy", 0) > 0.3,
                  f"[{task_key}/{model_name}] Accuracy={m.get('accuracy')} > 0.30 (random baseline)")
            check(m.get("auc", 0) is None or m.get("auc", 1) >= 0.45,
                  f"[{task_key}/{model_name}] AUC={m.get('auc')} >= 0.45")

    # 3. 报告文件
    print("\n## 3. 报告输出")
    check((REPORTS_DIR / "model_report.json").exists(), "model_report.json 存在")
    check((REPORTS_DIR / "model_report.html").exists(), "model_report.html 存在")

    # 4. 测试样本数
    print("\n## 4. 测试集完整性")
    for task_key in TASKS:
        y_test = get_labels(test_df, task_key)
        check(len(y_test) > 500, f"[{task_key}] 测试样本: {len(y_test)} > 500")

    # 5. 特征重要性
    print("\n## 5. 特征重要性")
    for task_key, task_data in results["tasks"].items():
        for model_name, model_data in task_data["models"].items():
            if "error" in model_data:
                continue
            fi = model_data.get("feature_importance", {})
            check(len(fi) > 0, f"[{task_key}/{model_name}] 特征重要性: {len(fi)} 个特征")
            if fi:
                top = list(fi.keys())[0]
                check(fi[top] > 0, f"[{task_key}/{model_name}] 最重要特征 '{top}' 重要性 > 0")

    return {"passed": passed, "failed": failed, "errors": errors, "passed_msgs": passed_msgs}


def print_summary(results: dict):
    print(f"\n{'=' * 60}")
    print("模型训练 — 最终总结")
    print(f"{'=' * 60}")

    for task_key, task_data in results["tasks"].items():
        task_name = task_data["name"]
        print(f"\n  [{task_name}]")
        for model_name, model_data in task_data["models"].items():
            if "error" in model_data:
                print(f"    {model_name}: ERROR — {model_data['error']}")
                continue
            m = model_data["metrics"]
            bt = model_data.get("backtest", {})
            print(f"    {model_name:10s} | Acc={m.get('accuracy',0):.4f} F1={m.get('f1',0):.4f} AUC={m.get('auc','-')} | ROI={bt.get('roi','-'):.4f}" if isinstance(bt.get('roi'), (int,float)) else f"    {model_name:10s} | Acc={m.get('accuracy',0):.4f} F1={m.get('f1',0):.4f} AUC={m.get('auc','-')} | ROI={bt.get('roi','-')}")

    t = results["tests"]
    print(f"\n  测试: {t['passed']}/{t['passed']+t['failed']} 通过")
    print(f"  模型目录: {MODELS_DIR}")
    print(f"  报告目录: {REPORTS_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
