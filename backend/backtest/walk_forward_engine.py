#!/usr/bin/env python3
"""
Walk Forward 回测引擎 — 滚动时间窗口回测完整系统

支持:
- 按月/季度/自定义窗口滚动
- Flat Betting / Kelly Criterion 资金管理
- CLV (Closing Line Value) 分析
- 滑点模拟 (多级别)
- 综合指标 (ROI, Sharpe, MaxDD, Sortino, Calmar 等)
- 图表 + HTML/CSV/JSON 报告
- YAML 配置文件驱动
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
import yaml
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.preprocessing import LabelEncoder

from .bankroll_manager import BankrollManager
from .clv_analyzer import CLVAnalyzer
from .performance_metrics import PerformanceMetrics
from .report_generator import ReportGenerator
from .slippage_simulator import SlippageSimulator

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASETS_DIR = PROJECT_ROOT / "datasets"
MODELS_DIR = PROJECT_ROOT / "models"
REPORTS_DIR = PROJECT_ROOT / "reports"
CONFIG_DIR = PROJECT_ROOT / "config"

warnings.filterwarnings("ignore")

RANDOM_STATE = 42
META_COLS = ["match_id", "kickoff_time", "season", "league_code", "home_team", "away_team"]
LABEL_COLS = [
    "label_home_win", "label_draw", "label_away_win",
    "label_over_2_5", "label_total_goals", "label_goal_diff",
    "label_asian", "label_upset",
]

TASK_CONFIGS = {
    "asian": {
        "name": "让球盘",
        "type": "multiclass",
        "label_col": "label_asian_mapped",
        "class_map": {0: "客赢盘", 1: "走水", 2: "主赢盘"},
    },
    "over_under": {
        "name": "大小球",
        "type": "binary",
        "label_col": "label_over_2_5",
        "class_map": {0: "小球", 1: "大球"},
    },
    "wdl": {
        "name": "胜平负",
        "type": "multiclass",
        "label_col": "label_wdl",
        "class_map": {0: "主胜", 1: "平局", 2: "客胜"},
    },
}


class WalkForwardEngine:
    """Walk Forward 回测主引擎。"""

    def __init__(self, config_path: str = None):
        """初始化引擎。

        Args:
            config_path: YAML 配置文件路径，默认 config/backtest.yaml
        """
        if config_path is None:
            config_path = CONFIG_DIR / "backtest.yaml"
        self.config_path = Path(config_path)
        self.config = self._load_config()

        # 窗口配置
        win_cfg = self.config.get("windows", {})
        self.window_mode = win_cfg.get("mode", "quarterly")
        self.min_train_months = win_cfg.get("min_train_months", 12)
        self.expanding = win_cfg.get("expanding", True)
        self.rolling_window_months = win_cfg.get("rolling_window_months", 24)

        # 资金管理
        br_cfg = self.config.get("bankroll", {})
        self.initial_capital = br_cfg.get("initial_capital", 10000.0)
        self.bankroll_mode = br_cfg.get("mode", "flat")
        self.flat_stake = br_cfg.get("flat", {}).get("stake_per_bet", 100.0)
        self.kelly_fraction = br_cfg.get("kelly", {}).get("fraction", 0.25)
        self.kelly_min_edge = br_cfg.get("kelly", {}).get("min_edge", 0.02)
        self.kelly_max_stake_pct = br_cfg.get("kelly", {}).get("max_stake_pct", 0.05)
        self.kelly_max_exposure = br_cfg.get("kelly", {}).get("max_exposure", 0.50)

        # 滑点
        slip_cfg = self.config.get("slippage", {})
        self.slippage_enabled = slip_cfg.get("enabled", True)
        self.slippage_levels = slip_cfg.get("levels", [0.0, 0.01, 0.02, 0.03])
        self.slippage_default = slip_cfg.get("default", 0.02)

        # 报告
        rpt_cfg = self.config.get("reports", {})
        self.output_dir = PROJECT_ROOT / rpt_cfg.get("output_dir", "reports/backtest")
        self.charts_dir = PROJECT_ROOT / rpt_cfg.get("charts_dir", "reports/charts")
        self.chart_dpi = rpt_cfg.get("chart_dpi", 150)

        # 模型配置
        self.model_configs = self.config.get("models", {})

        # 任务启用状态
        task_cfg = self.config.get("tasks", {})
        self.enabled_tasks = [t for t, c in task_cfg.items() if c.get("enabled", True)]

        # 初始化子模块
        self.metrics_calc = PerformanceMetrics()
        self.clv_analyzer = CLVAnalyzer()
        self.slippage_sim = SlippageSimulator(
            levels=self.slippage_levels,
            default_level=self.slippage_default,
        )
        self.report_gen = ReportGenerator(
            output_dir=str(self.output_dir),
            charts_dir=str(self.charts_dir),
            chart_dpi=self.chart_dpi,
        )

        # 运行时状态
        self.df: Optional[pd.DataFrame] = None
        self.X_raw: Optional[pd.DataFrame] = None
        self.feature_names: List[str] = []
        self.results: dict = {}
        self.bets_data: dict = {}

    def _load_config(self) -> dict:
        """加载 YAML 配置。"""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        print(f"[WARN] 配置文件不存在: {self.config_path}, 使用默认配置")
        return {}

    # ── 数据加载 ────────────────────────────────────────────────

    def load_data(self) -> pd.DataFrame:
        """加载全部数据 (train + val + test)。"""
        print("[1/8] 加载数据...")
        df_parts = []
        for name in ["train.csv", "validation.csv", "test.csv"]:
            path = DATASETS_DIR / name
            if path.exists():
                df_parts.append(pd.read_csv(path))
                print(f"  {name}: {len(df_parts[-1]):,} 条")

        if not df_parts:
            raise FileNotFoundError(f"未找到数据集文件于 {DATASETS_DIR}")

        df = pd.concat(df_parts, ignore_index=True)

        # 衍生 WDL 标签
        valid = df.dropna(subset=["label_home_win", "label_draw", "label_away_win"])
        df["label_wdl"] = np.nan
        df.loc[valid.index, "label_wdl"] = (
            valid["label_home_win"] * 0 + valid["label_draw"] * 1 + valid["label_away_win"] * 2
        ).astype(int)

        # 亚盘标签映射: -1→0, 0→1, 1→2
        df["label_asian_mapped"] = df["label_asian"].map({-1.0: 0, 0.0: 1, 1.0: 2})

        # 确保 kickoff_time 为 datetime
        df["kickoff_time"] = pd.to_datetime(df["kickoff_time"], errors="coerce")

        self.df = df
        print(f"  总计: {len(df):,} 场比赛")
        return df

    def prepare_features(self) -> Tuple[pd.DataFrame, List[str]]:
        """特征预处理 (不在全量数据上fit, 避免数据泄露)。"""
        print("[2/8] 特征预处理...")

        exclude = set(META_COLS) | set(LABEL_COLS) | {"label_wdl", "label_asian_mapped"}
        feature_cols = [c for c in self.df.columns
                        if c not in exclude and self.df[c].dtype in ("float64", "int64")]

        # 仅提取特征矩阵, 不做imputation — imputation在每个窗口内用训练集fit
        X_raw = self.df[feature_cols].copy()

        self.X_raw = X_raw
        self.feature_names = feature_cols
        print(f"  特征数: {len(feature_cols)}")
        return X_raw, feature_cols

    # ── 窗口生成 ────────────────────────────────────────────────

    def generate_windows(self) -> List[dict]:
        """根据配置生成滚动窗口。

        Returns:
            [{train_start, train_end, test_start, test_end, label}, ...]
        """
        df = self.df
        min_date = df["kickoff_time"].min()
        max_date = df["kickoff_time"].max()

        if self.window_mode == "monthly":
            return self._gen_monthly_windows(min_date, max_date)
        elif self.window_mode == "quarterly":
            return self._gen_quarterly_windows(min_date, max_date)
        elif self.window_mode == "yearly":
            return self._gen_yearly_windows(min_date, max_date)
        elif self.window_mode == "custom":
            return self._gen_custom_windows()
        else:
            raise ValueError(f"不支持的窗口模式: {self.window_mode}")

    def _gen_monthly_windows(self, min_date, max_date) -> List[dict]:
        """生成按月滚动窗口。"""
        windows = []
        all_months = pd.date_range(min_date, max_date, freq="MS")
        for i, test_start in enumerate(all_months):
            test_end = test_start + pd.DateOffset(months=1) - pd.Timedelta(days=1)
            if test_end > max_date:
                break

            if self.expanding:
                train_start = min_date
                train_end = test_start - pd.Timedelta(days=1)
            else:
                train_start = test_start - pd.DateOffset(months=self.rolling_window_months)
                train_end = test_start - pd.Timedelta(days=1)

            train_mask = (self.df["kickoff_time"] >= train_start) & (self.df["kickoff_time"] <= train_end)
            test_mask = (self.df["kickoff_time"] >= test_start) & (self.df["kickoff_time"] <= test_end)
            train_months = (test_start.year - train_start.year) * 12 + (test_start.month - train_start.month)

            if train_mask.sum() < 100 or test_mask.sum() < 20 or train_months < self.min_train_months:
                continue

            windows.append({
                "train_start": str(train_start.date()),
                "train_end": str(train_end.date()),
                "test_start": str(test_start.date()),
                "test_end": str(test_end.date()),
                "label": f"{test_start.year}-{test_start.month:02d}",
            })
        return windows

    def _gen_quarterly_windows(self, min_date, max_date) -> List[dict]:
        """生成按季度滚动窗口。"""
        windows = []
        all_quarters = pd.date_range(min_date, max_date, freq="QS")
        for i, test_start in enumerate(all_quarters):
            test_end = test_start + pd.DateOffset(months=3) - pd.Timedelta(days=1)
            if test_end > max_date:
                break

            if self.expanding:
                train_start = min_date
                train_end = test_start - pd.Timedelta(days=1)
            else:
                train_start = test_start - pd.DateOffset(months=self.rolling_window_months)
                train_end = test_start - pd.Timedelta(days=1)

            train_months = (test_start.year - train_start.year) * 12 + (test_start.month - train_start.month)
            if train_months < self.min_train_months:
                continue

            train_mask = (self.df["kickoff_time"] >= train_start) & (self.df["kickoff_time"] <= train_end)
            test_mask = (self.df["kickoff_time"] >= test_start) & (self.df["kickoff_time"] <= test_end)

            if train_mask.sum() < 200 or test_mask.sum() < 50:
                continue

            q = (test_start.month - 1) // 3 + 1
            windows.append({
                "train_start": str(train_start.date()),
                "train_end": str(train_end.date()),
                "test_start": str(test_start.date()),
                "test_end": str(test_end.date()),
                "label": f"{test_start.year}Q{q}",
            })
        return windows

    def _gen_yearly_windows(self, min_date, max_date) -> List[dict]:
        """生成按年滚动窗口。"""
        windows = []
        for year in range(min_date.year + 1, max_date.year + 1):
            test_start = pd.Timestamp(f"{year}-01-01")
            test_end = pd.Timestamp(f"{year}-12-31")

            if self.expanding:
                train_start = min_date
            else:
                train_start = test_start - pd.DateOffset(months=self.rolling_window_months)
            train_end = test_start - pd.Timedelta(days=1)

            train_mask = (self.df["kickoff_time"] >= train_start) & (self.df["kickoff_time"] <= train_end)
            test_mask = (self.df["kickoff_time"] >= test_start) & (self.df["kickoff_time"] <= test_end)

            if train_mask.sum() < 200 or test_mask.sum() < 50:
                continue

            windows.append({
                "train_start": str(train_start.date()),
                "train_end": str(train_end.date()),
                "test_start": str(test_start.date()),
                "test_end": str(test_end.date()),
                "label": f"{year}",
            })
        return windows

    def _gen_custom_windows(self) -> List[dict]:
        """从配置读取自定义窗口。"""
        win_cfg = self.config.get("windows", {})
        train_years = win_cfg.get("train_years", [2021, 2022])
        test_season = win_cfg.get("test_season", 2023)

        train_start = pd.Timestamp(f"{min(train_years)}-01-01")
        train_end = pd.Timestamp(f"{max(train_years)}-12-31")
        test_start = pd.Timestamp(f"{test_season}-01-01")
        test_end = pd.Timestamp(f"{test_season}-12-31")

        return [{
            "train_start": str(train_start.date()),
            "train_end": str(train_end.date()),
            "test_start": str(test_start.date()),
            "test_end": str(test_end.date()),
            "label": f"{'-'.join(str(y) for y in train_years)} -> {test_season}",
        }]

    # ── 模型训练 ────────────────────────────────────────────────

    def _train_models(self, X_train: pd.DataFrame, y_train: pd.Series,
                      task_type: str) -> dict:
        """训练所有启用的模型。"""
        models = {}
        model_cfgs = self.model_configs

        if model_cfgs.get("xgboost", {}).get("enabled", True):
            models["xgboost"] = self._train_xgboost(X_train, y_train, task_type)

        if model_cfgs.get("lightgbm", {}).get("enabled", True):
            models["lightgbm"] = self._train_lightgbm(X_train, y_train, task_type)

        if model_cfgs.get("catboost", {}).get("enabled", True):
            models["catboost"] = self._train_catboost(X_train, y_train, task_type)

        return models

    def _train_xgboost(self, X, y, task_type: str):
        import xgboost as xgb
        cfg = self.model_configs.get("xgboost", {}).get("params", {})
        params = {
            "objective": "multi:softprob" if task_type == "multiclass" else "binary:logistic",
            "eval_metric": "mlogloss" if task_type == "multiclass" else "logloss",
            "max_depth": cfg.get("max_depth", 6),
            "learning_rate": cfg.get("learning_rate", 0.05),
            "n_estimators": cfg.get("n_estimators", 300),
            "subsample": cfg.get("subsample", 0.8),
            "colsample_bytree": cfg.get("colsample_bytree", 0.8),
            "random_state": cfg.get("random_state", RANDOM_STATE),
            "verbosity": 0,
        }
        if task_type == "multiclass":
            params["num_class"] = y.nunique()
        model = xgb.XGBClassifier(**params)
        model.fit(X, y, verbose=False)
        return model

    def _train_lightgbm(self, X, y, task_type: str):
        import lightgbm as lgb
        cfg = self.model_configs.get("lightgbm", {}).get("params", {})
        params = {
            "objective": "multiclass" if task_type == "multiclass" else "binary",
            "metric": "multi_logloss" if task_type == "multiclass" else "binary_logloss",
            "max_depth": cfg.get("max_depth", 6),
            "learning_rate": cfg.get("learning_rate", 0.05),
            "n_estimators": cfg.get("n_estimators", 300),
            "subsample": cfg.get("subsample", 0.8),
            "colsample_bytree": cfg.get("colsample_bytree", 0.8),
            "random_state": cfg.get("random_state", RANDOM_STATE),
            "verbose": -1,
        }
        if task_type == "multiclass":
            params["num_class"] = y.nunique()
        model = lgb.LGBMClassifier(**params)
        model.fit(X, y)
        return model

    def _train_catboost(self, X, y, task_type: str):
        from catboost import CatBoostClassifier
        cfg = self.model_configs.get("catboost", {}).get("params", {})
        params = {
            "depth": cfg.get("depth", 6),
            "learning_rate": cfg.get("learning_rate", 0.05),
            "iterations": cfg.get("iterations", 300),
            "random_seed": cfg.get("random_seed", RANDOM_STATE),
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

    # ── 回测执行 ────────────────────────────────────────────────

    def _execute_bets(self, test_df: pd.DataFrame, y_true: pd.Series,
                      y_pred: np.ndarray, task_key: str,
                      bankroll: BankrollManager) -> pd.DataFrame:
        """执行投注并返回记录。"""
        records = []
        test_df = test_df.copy()
        test_df["y_true"] = y_true.values
        test_df["y_pred"] = y_pred

        for idx in test_df.index:
            row = test_df.loc[idx]
            pred = int(row["y_pred"])
            actual = int(row["y_true"])

            # 获取赔率
            if task_key == "wdl":
                odds_map = {0: "close_home_odds", 1: "close_draw_odds", 2: "close_away_odds"}
                odds = row.get(odds_map.get(pred))
                if pd.isna(odds) or odds <= 0:
                    continue
                result = 1 if pred == actual else 0
                edge = self._estimate_edge(row, pred, task_key)

            elif task_key == "over_under":
                if pred == 1:
                    water = row.get("ou_close_over_water")
                else:
                    water = row.get("ou_close_under_water")
                if pd.isna(water) or water <= 0:
                    continue
                result = 1 if pred == actual else 0
                edge = self._estimate_edge(row, pred, task_key)
                # 香港盘水位 → 欧赔: water IS profit, decimal_odds = water + 1
                odds = water + 1.0

            elif task_key == "asian":
                if pred == 1:  # 预测走水
                    continue
                if pred == 2:
                    water = row.get("asian_close_high_water")
                else:
                    water = row.get("asian_close_low_water")
                if pd.isna(water) or water <= 0:
                    continue
                if actual == 1:  # 实际走水
                    result = -1
                else:
                    result = 1 if pred == actual else 0
                edge = self._estimate_edge(row, pred, task_key)
                # 香港盘水位 → 欧赔
                odds = water + 1.0

            else:
                continue

            bt_record = bankroll.place_bet(
                odds=odds,
                result=result,
                edge=edge,
                match_id=str(row.get("match_id", "")),
                kickoff_time=str(row.get("kickoff_time", "")),
            )
            bt_record["y_true"] = actual
            bt_record["y_pred"] = pred
            bt_record["task"] = task_key
            bt_record["odds"] = odds
            records.append(bt_record)

        return pd.DataFrame(records)

    def _estimate_edge(self, row, pred: int, task_key: str) -> float:
        """估算凯利投注所需的 edge (基于预测概率 vs 隐含概率)。"""
        try:
            proba_cols = [c for c in row.index if c.startswith("y_proba_")]
            if not proba_cols:
                return 0.0

            # 预测概率
            model_prob = row.get(f"y_proba_{pred}", 0)
            if pd.isna(model_prob) or model_prob <= 0:
                return 0.0

            # 收盘赔率隐含概率
            if task_key == "wdl":
                odds_cols = ["close_home_odds", "close_draw_odds", "close_away_odds"]
                raw_odds = [row.get(c) for c in odds_cols]
            elif task_key == "over_under":
                raw_odds = [row.get("ou_close_under_water"), row.get("ou_close_over_water")]
            else:
                raw_odds = [row.get("asian_close_low_water"), row.get("asian_close_high_water")]

            if any(pd.isna(o) or o <= 0 for o in raw_odds):
                return 0.0

            # 归一化隐含概率
            implied = [1.0 / o for o in raw_odds]
            total = sum(implied)
            implied_probs = [p / total for p in implied]

            if task_key == "wdl":
                market_prob = implied_probs[pred]
            elif task_key == "over_under":
                market_prob = implied_probs[1] if pred == 1 else implied_probs[0]
            else:
                market_prob = implied_probs[1] if pred == 2 else implied_probs[0]

            return float(model_prob - market_prob)
        except Exception:
            return 0.0

    # ── 主流程 ──────────────────────────────────────────────────

    def run(self) -> dict:
        """执行完整 Walk Forward 回测。"""
        print("=" * 60)
        print("Walk Forward 回测引擎 v2.0")
        print(f"  窗口模式: {self.window_mode}")
        print(f"  窗口策略: {'扩展窗口' if self.expanding else '滚动窗口'}")
        print(f"  资金模式: {self.bankroll_mode}")
        print("=" * 60)

        # 1. 加载数据
        self.load_data()

        # 2. 特征预处理
        self.prepare_features()

        # 3. 生成窗口
        print("[3/8] 生成回测窗口...")
        windows = self.generate_windows()
        print(f"  共 {len(windows)} 个窗口")
        for w in windows:
            print(f"    {w['label']}: train={w['train_start']}~{w['train_end']}, test={w['test_start']}~{w['test_end']}")

        # 4-7. 对每个任务执行 Walk Forward
        all_results = {
            "generated_at": datetime.now().isoformat(),
            "config": self.config,
            "windows": windows,
            "tasks": {},
            "clv_analysis": {},
            "slippage_analysis": {},
        }
        all_bets_data = {}

        for task_key in self.enabled_tasks:
            task_cfg = TASK_CONFIGS[task_key]
            task_name = task_cfg["name"]
            task_type = task_cfg["type"]
            label_col = task_cfg["label_col"]

            print(f"\n{'─' * 50}")
            print(f"[4/8] [{task_name}] Walk Forward 回测")
            print(f"{'─' * 50}")

            # 获取标签
            y_all = self.df[label_col].dropna().astype(int)

            task_results = {"name": task_name, "models": {}, "windows_meta": []}
            task_bets = {}

            for model_name in ["xgboost", "lightgbm", "catboost"]:
                model_cfg = self.model_configs.get(model_name, {})
                if not model_cfg.get("enabled", True):
                    continue

                print(f"\n  [{model_name}]")

                # 创建资金管理器
                bankroll = BankrollManager(
                    initial_capital=self.initial_capital,
                    mode=self.bankroll_mode,
                    flat_stake=self.flat_stake,
                    kelly_fraction=self.kelly_fraction,
                    min_edge=self.kelly_min_edge,
                    max_stake_pct=self.kelly_max_stake_pct,
                    max_exposure=self.kelly_max_exposure,
                )

                model_results = {
                    "windows": [],
                    "all_bets": [],
                    "aggregate_metrics": {},
                }

                for win in windows:
                    train_mask = (
                        (self.df["kickoff_time"] >= win["train_start"]) &
                        (self.df["kickoff_time"] <= win["train_end"])
                    )
                    test_mask = (
                        (self.df["kickoff_time"] >= win["test_start"]) &
                        (self.df["kickoff_time"] <= win["test_end"])
                    )

                    # 对齐有效标签
                    train_idx = self.df[train_mask].index.intersection(y_all.index)
                    test_idx = self.df[test_mask].index.intersection(y_all.index)

                    if len(train_idx) < 100 or len(test_idx) < 20:
                        print(f"    [{win['label']}] 样本不足 (train={len(train_idx)}, test={len(test_idx)})，跳过")
                        continue

                    X_tr_raw = self.X_raw.loc[train_idx]
                    y_tr = y_all.loc[train_idx].astype(int)
                    X_ts_raw = self.X_raw.loc[test_idx]
                    y_ts = y_all.loc[test_idx].astype(int)

                    # 在每个窗口内单独fit imputer, 仅用训练集, 杜绝数据泄露
                    imputer = SimpleImputer(strategy="median")
                    X_tr = pd.DataFrame(
                        imputer.fit_transform(X_tr_raw),
                        columns=self.feature_names, index=X_tr_raw.index
                    )
                    X_ts = pd.DataFrame(
                        imputer.transform(X_ts_raw),
                        columns=self.feature_names, index=X_ts_raw.index
                    )

                    # 训练模型
                    models = self._train_models(X_tr, y_tr, task_type)
                    model = models[model_name]

                    # 预测
                    y_pred = model.predict(X_ts)
                    try:
                        y_proba = model.predict_proba(X_ts)
                        acc = accuracy_score(y_ts, y_pred)
                        if task_type == "multiclass":
                            auc = roc_auc_score(y_ts, y_proba, multi_class="ovr", average="weighted")
                        else:
                            auc = roc_auc_score(y_ts, y_proba[:, 1])
                    except Exception:
                        y_proba = None
                        acc = accuracy_score(y_ts, y_pred)
                        auc = None

                    # 回测
                    test_df_part = self.df.loc[test_idx].copy()
                    # 附加预测概率
                    if y_proba is not None:
                        for i in range(y_proba.shape[1]):
                            test_df_part[f"y_proba_{i}"] = y_proba[:, i]

                    bets_df = self._execute_bets(test_df_part, y_ts, y_pred, task_key, bankroll)
                    metrics = self.metrics_calc.compute_from_df(bets_df) if len(bets_df) > 0 else {}

                    win_result = {
                        "window": win["label"],
                        "test_period": f"{win['test_start']} ~ {win['test_end']}",
                        "train_samples": len(X_tr),
                        "test_samples": len(X_ts),
                        "accuracy": round(float(acc), 4),
                        "auc": round(float(auc), 4) if auc else None,
                        "metrics": metrics,
                    }
                    model_results["windows"].append(win_result)
                    model_results["all_bets"].append(bets_df)

                    roi = metrics.get("roi", 0)
                    sharpe = metrics.get("sharpe_ratio", 0)
                    bets_n = metrics.get("total_bets", 0)
                    print(f"    [{win['label']}] Acc={acc:.4f} ROI={roi:.4f} "
                          f"Sharpe={sharpe:.2f} Bets={bets_n}")

                # 汇总所有窗口
                if model_results["all_bets"]:
                    all_bets_df = pd.concat(model_results["all_bets"], ignore_index=True)
                    model_results["aggregate_metrics"] = self.metrics_calc.compute_from_df(all_bets_df)
                    model_results["aggregate_equity"] = (
                        np.cumsum(all_bets_df["profit"].values)
                        if "profit" in all_bets_df.columns else []
                    ).tolist()

                    agg = model_results["aggregate_metrics"]
                    print(f"  [{model_name}] 汇总: ROI={agg.get('roi',0):.4f} "
                          f"Sharpe={agg.get('sharpe_ratio',0):.2f} "
                          f"WR={agg.get('win_rate',0):.1%} "
                          f"MaxDD={agg.get('max_drawdown_pct',0):.1%} "
                          f"Bets={agg.get('total_bets',0)}")

                    task_bets[model_name] = all_bets_df
                else:
                    model_results["aggregate_equity"] = []

                task_results["models"][model_name] = model_results

            # ── CLV 分析 ────────────────────────────────────────
            print(f"\n[5/8] [{task_name}] CLV 分析...")
            clv_bets = self._prepare_clv_data(task_bets, task_key)
            if clv_bets is not None and len(clv_bets) > 0:
                clv_result = self.clv_analyzer.analyze(clv_bets, task_key)
                all_results["clv_analysis"][task_key] = clv_result
                if "mean_clv" in clv_result:
                    print(f"  CLV mean={clv_result['mean_clv']:.4f} "
                          f"positive_rate={clv_result['positive_clv_rate']:.1%} "
                          f"significant={clv_result.get('significant', False)}")
            else:
                all_results["clv_analysis"][task_key] = {"error": "CLV 数据不足"}

            # ── 滑点模拟 ────────────────────────────────────────
            print(f"[6/8] [{task_name}] 滑点模拟...")
            slip_results = {}
            for model_name, bd in task_bets.items():
                if "odds" not in bd.columns or len(bd) == 0:
                    continue
                slip_bets = bd[["odds", "profit", "result"]].copy()
                slip_bets["result"] = slip_bets["profit"].apply(
                    lambda x: 1 if x > 0 else (0 if x < 0 else -1)
                )
                # 映射 result
                slip_bets["result_num"] = slip_bets["result"].map({1: 1, 0: 0, -1: -1}).fillna(0).astype(int)

                # 用原始 result 列
                bets_for_slip = bd[["odds"]].copy()
                bets_for_slip["result"] = bd["profit"].apply(
                    lambda x: 1 if x > 0 else (0 if x < 0 else -1)
                )

                multi = self.slippage_sim.run_multi_level(bets_for_slip)
                slip_results[model_name] = multi

                be = self.slippage_sim.find_breakeven_slippage(bets_for_slip)
                slip_results[f"{model_name}_breakeven"] = be

                for lv_key, lv in multi.items():
                    print(f"  [{model_name}] 滑点{lv['level_pct']}: ROI={lv['roi']:.4f}")
                print(f"  [{model_name}] 盈亏平衡滑点: {be:.1%}")

            all_results["slippage_analysis"][task_key] = slip_results

            all_results["tasks"][task_key] = task_results
            all_bets_data[task_key] = task_bets

        # ── 过拟合检测 ──────────────────────────────────────────
        print(f"\n{'─' * 50}")
        print("[7/8] 过拟合分析...")
        of_analysis = self._analyze_overfitting(all_results)
        all_results["overfitting_analysis"] = of_analysis

        # ── 报告生成 ────────────────────────────────────────────
        print(f"\n[8/8] 生成报告...")
        report_paths = self.report_gen.generate_all(all_results, all_bets_data)
        all_results["report_paths"] = {k: str(v) for k, v in report_paths.items()}

        self.results = all_results
        self.bets_data = all_bets_data

        # 打印总结
        self._print_summary()

        return all_results

    def _prepare_clv_data(self, task_bets: dict, task_key: str) -> Optional[pd.DataFrame]:
        """准备 CLV 分析所需的数据 (需要开盘+收盘赔率)。

        仅 WDL 有完整的开盘/收盘赔率数据。
        亚盘和大小球只有收盘水位，无法计算 CLV。
        """
        if not task_bets:
            return None

        # 检查该任务可用的开盘/收盘列
        available_cols = set(self.df.columns)
        if task_key == "wdl":
            needed = ["open_home_odds", "close_home_odds",
                      "open_draw_odds", "close_draw_odds",
                      "open_away_odds", "close_away_odds"]
        elif task_key == "over_under":
            # OU 只有收盘水位，无开盘水位 → 无法计算 CLV
            return None
        elif task_key == "asian":
            # 亚盘只有收盘水位，无开盘水位 → 无法计算 CLV
            return None
        else:
            return None

        if not needed or not all(c in available_cols for c in needed):
            return None

        frames = []
        for model_name, bd in task_bets.items():
            if bd.empty:
                continue
            bd_copy = bd.copy()
            if "match_id" not in bd_copy.columns:
                continue

            match_ids = bd_copy["match_id"].unique()
            orig = self.df[self.df["match_id"].isin(match_ids)]

            merge_cols = [c for c in ["match_id", "y_pred", "y_true", "result"] if c in bd_copy.columns]
            ods_cols = [c for c in needed if c in orig.columns]

            merged = bd_copy[merge_cols].merge(
                orig[ods_cols + ["match_id"]].drop_duplicates("match_id"),
                on="match_id", how="left"
            )
            frames.append(merged)

        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)

    def _analyze_overfitting(self, results: dict) -> dict:
        """过拟合检测: 跨窗口 ROI 稳定性分析。"""
        analysis = {}
        for task_key, task_data in results.get("tasks", {}).items():
            task_name = task_data.get("name", task_key)
            analysis[task_key] = {"name": task_name, "models": {}}

            for model_name, model_data in task_data.get("models", {}).items():
                windows = model_data.get("windows", [])
                if len(windows) < 2:
                    continue

                rois = [w.get("metrics", {}).get("roi", 0) for w in windows]
                rois = [r for r in rois if r is not None]
                if len(rois) < 2:
                    continue

                roi_std = float(np.std(rois))
                roi_min = float(min(rois))
                roi_max = float(max(rois))
                roi_range = roi_max - roi_min

                if roi_std > 0.15 or roi_range > 0.25:
                    risk = "HIGH"
                    reasons = [f"ROI 跨窗口差异大 (std={roi_std:.4f}, range={roi_range:.4f})"]
                elif roi_std > 0.08 or roi_range > 0.15:
                    risk = "MEDIUM"
                    reasons = [f"ROI 标准差偏高 ({roi_std:.4f})"]
                else:
                    risk = "LOW"
                    reasons = ["各窗口 ROI 稳定"]

                if roi_min < -0.05:
                    reasons.append(f"存在负 ROI 窗口 (min={roi_min:.4f})")
                    if risk == "LOW":
                        risk = "MEDIUM"

                analysis[task_key]["models"][model_name] = {
                    "risk_level": risk,
                    "roi_std": round(roi_std, 4),
                    "roi_min": round(roi_min, 4),
                    "roi_max": round(roi_max, 4),
                    "roi_range": round(roi_range, 4),
                    "reasons": reasons,
                    "window_rois": [round(r, 4) for r in rois],
                }
                print(f"  [{task_name}/{model_name}] 过拟合风险: {risk}")

        return analysis

    def _print_summary(self):
        """打印最终总结。"""
        print(f"\n{'=' * 60}")
        print("Walk Forward 回测 — 最终总结")
        print(f"{'=' * 60}")

        for task_key in self.enabled_tasks:
            task_data = self.results["tasks"].get(task_key, {})
            task_name = task_data.get("name", task_key)
            print(f"\n  [{task_name}]")
            for model_name, model_data in task_data.get("models", {}).items():
                agg = model_data.get("aggregate_metrics", {})
                roi = agg.get("roi", 0)
                sharpe = agg.get("sharpe_ratio", 0)
                wr = agg.get("win_rate", 0)
                maxdd = agg.get("max_drawdown_pct", 0)
                bets = agg.get("total_bets", 0)
                print(f"    {model_name:10s} | ROI={roi:.4f} Sharpe={sharpe:.2f} "
                      f"WR={wr:.1%} MaxDD={maxdd:.1%} Bets={bets}")

        of = self.results.get("overfitting_analysis", {})
        print(f"\n  [过拟合风险]")
        for task_key, task_data in of.items():
            for model_name, model_data in task_data.get("models", {}).items():
                print(f"    {task_data['name']}/{model_name}: {model_data['risk_level']}")

        rp = self.results.get("report_paths", {})
        print(f"\n  报告目录: {self.output_dir}")
        print(f"  图表目录: {self.charts_dir}")
        if "html" in rp:
            print(f"  HTML 报告: {rp['html']}")
        print(f"{'=' * 60}")


# ── CLI ────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Walk Forward 回测引擎")
    parser.add_argument("--config", type=str, default=None, help="YAML 配置文件路径")
    parser.add_argument("--mode", type=str, default=None,
                        choices=["monthly", "quarterly", "yearly", "custom"],
                        help="窗口模式 (覆盖配置文件)")
    parser.add_argument("--bankroll", type=str, default=None,
                        choices=["flat", "kelly"],
                        help="资金管理模式 (覆盖配置文件)")
    parser.add_argument("--expanding", dest="expanding", action="store_true", default=None)
    parser.add_argument("--rolling", dest="expanding", action="store_false",
                        help="使用滚动窗口 (非扩展)")
    parser.add_argument("--tasks", type=str, default=None,
                        help="逗号分隔的任务列表, 如: asian,over_under")
    args = parser.parse_args()

    engine = WalkForwardEngine(config_path=args.config)

    # CLI 覆盖
    if args.mode:
        engine.window_mode = args.mode
    if args.bankroll:
        engine.bankroll_mode = args.bankroll
    if args.expanding is not None:
        engine.expanding = args.expanding
    if args.tasks:
        engine.enabled_tasks = [t.strip() for t in args.tasks.split(",")]

    try:
        engine.run()
    except Exception as e:
        print(f"\n[ERROR] 回测失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
