"""
ML模型预测器 — 加载训练好的XGBoost/LightGBM/CatBoost模型进行实时预测
"""
import json
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")


class MLPredictor:
    """加载训练好的ML模型，对实时比赛进行预测"""

    TASKS = ["wdl", "over_under", "asian"]
    MODELS = ["xgboost", "lightgbm", "catboost"]

    # 训练时使用的特征名（从 feature_engineering.py 的 80+ 特征中选取核心可实时计算的特征）
    CORE_FEATURES = [
        # 欧赔隐含概率
        "home_implied_prob", "draw_implied_prob", "away_implied_prob",
        # 欧赔水位
        "home_odds", "draw_odds", "away_odds",
        # 亚盘特征
        "asian_handicap_line", "asian_home_water", "asian_away_water",
        # 大小球特征
        "ou_line", "ou_over_water", "ou_under_water",
        # 市场一致性
        "market_overround",
        # 开赔变化
        "home_odds_change", "draw_odds_change", "away_odds_change",
        "asian_line_change",
        "ou_line_change",
    ]

    def __init__(self, models_dir: str = None):
        if models_dir is None:
            from config.paths import MODELS_DIR
            models_dir = str(MODELS_DIR)
        self.models_dir = Path(models_dir)
        self._models: dict[str, dict[str, Any]] = {}
        self._available = False
        self._load_models()

    def _load_models(self):
        """加载所有已训练的模型"""
        import joblib

        loaded_count = 0
        for task in self.TASKS:
            self._models[task] = {}
            for model_name in self.MODELS:
                path = self.models_dir / f"{model_name}_{task}.pkl"
                if path.exists():
                    try:
                        self._models[task][model_name] = joblib.load(path)
                        loaded_count += 1
                    except Exception:
                        self._models[task][model_name] = None
                else:
                    self._models[task][model_name] = None

        self._available = loaded_count > 0

    @property
    def available(self) -> bool:
        return self._available

    def get_available_models(self) -> dict[str, list[str]]:
        return {
            task: [m for m in self.MODELS if self._models[task].get(m) is not None]
            for task in self.TASKS
        }

    def extract_live_features(self, odds_data: dict[str, Any]) -> pd.DataFrame:
        """从实时赔率数据提取特征向量"""
        features = {}

        # 欧赔
        home_odds = float(odds_data.get("home_win", 2.5))
        draw_odds = float(odds_data.get("draw", 3.5))
        away_odds = float(odds_data.get("away_win", 3.0))
        features["home_odds"] = home_odds
        features["draw_odds"] = draw_odds
        features["away_odds"] = away_odds

        # 隐含概率
        total = 1 / home_odds + 1 / draw_odds + 1 / away_odds
        features["home_implied_prob"] = (1 / home_odds) / total if total > 0 else 0.33
        features["draw_implied_prob"] = (1 / draw_odds) / total if total > 0 else 0.33
        features["away_implied_prob"] = (1 / away_odds) / total if total > 0 else 0.33
        features["market_overround"] = total

        # 开赔变化
        open_home = float(odds_data.get("open_home_win", home_odds))
        open_draw = float(odds_data.get("open_draw", draw_odds))
        open_away = float(odds_data.get("open_away_win", away_odds))
        features["home_odds_change"] = home_odds - open_home
        features["draw_odds_change"] = draw_odds - open_draw
        features["away_odds_change"] = away_odds - open_away

        # 亚盘
        asian = odds_data.get("asian", {})
        features["asian_handicap_line"] = float(asian.get("handicap", 0))
        features["asian_home_water"] = float(asian.get("home_odds", 0.9))
        features["asian_away_water"] = float(asian.get("away_odds", 0.9))

        open_asian = odds_data.get("open_asian", {})
        features["asian_line_change"] = (
            features["asian_handicap_line"] - float(open_asian.get("handicap", features["asian_handicap_line"]))
        )

        # 大小球
        ou = odds_data.get("over_under", {})
        features["ou_line"] = float(ou.get("line", 2.5))
        features["ou_over_water"] = float(ou.get("over_odds", 0.9))
        features["ou_under_water"] = float(ou.get("under_odds", 0.9))

        open_ou = odds_data.get("open_over_under", {})
        features["ou_line_change"] = (
            features["ou_line"] - float(open_ou.get("line", features["ou_line"]))
        )

        # 补充占位特征（训练时存在但实时不可得的特征用均值填充）
        placeholder_defaults = {
            "home_prob_change_15m": 0.0, "draw_prob_change_15m": 0.0, "away_prob_change_15m": 0.0,
            "odds_variance": 0.01, "bookmaker_divergence": 0.02,
            "favorite_direction": 1.0 if home_odds < away_odds else -1.0,
            "home_water_change": 0.0, "away_water_change": 0.0,
            "ou_water_change": 0.0,
            "asian_heat": 0.0, "ou_heat": 0.0,
        }
        features.update(placeholder_defaults)

        return pd.DataFrame([features])

    def predict(
        self, odds_data: dict[str, Any], task: str = "wdl"
    ) -> Optional[dict[str, Any]]:
        """
        对单场比赛运行所有可用模型的集成预测

        Returns:
            {
                "probabilities": {"home_win": 0.45, "draw": 0.25, "away_win": 0.30},
                "expected_value": {...},
                "confidence": 0.72,
                "model_votes": {"xgboost": "home_win", "lightgbm": "home_win", ...},
                "model_probas": {...},
                "best_pick": "home_win",
                "best_ev": 0.05,
            }
        """
        if not self._available:
            return None

        try:
            X = self.extract_live_features(odds_data)
        except Exception:
            return None

        models = self._models.get(task, {})
        active = {k: v for k, v in models.items() if v is not None}
        if not active:
            return None

        all_probas = {}
        votes = []
        proba_accum = None
        count = 0

        for name, model in active.items():
            try:
                proba = model.predict_proba(X)
                all_probas[name] = proba[0].tolist()
                pred_class = int(model.predict(X)[0])
                votes.append(pred_class)

                if proba_accum is None:
                    proba_accum = proba[0].copy()
                else:
                    proba_accum += proba[0]
                count += 1
            except Exception:
                continue

        if count == 0:
            return None

        ensemble_proba = proba_accum / count

        # 构建结果
        odds_map = {
            "home_win": float(odds_data.get("home_win", 2.5)),
            "draw": float(odds_data.get("draw", 3.5)),
            "away_win": float(odds_data.get("away_win", 3.0)),
        }

        if task == "wdl":
            prob_labels = ["home_win", "draw", "away_win"]
        elif task == "over_under":
            prob_labels = ["under", "over"]
            odds_map = {
                "under": float(odds_data.get("over_under", {}).get("under_odds", 1.9)),
                "over": float(odds_data.get("over_under", {}).get("over_odds", 1.9)),
            }
        elif task == "asian":
            prob_labels = ["away_cover", "push", "home_cover"]
            odds_map = {
                "away_cover": float(odds_data.get("asian", {}).get("away_odds", 1.9)),
                "push": 1.0,
                "home_cover": float(odds_data.get("asian", {}).get("home_odds", 1.9)),
            }
        else:
            prob_labels = ["home_win", "draw", "away_win"]

        probabilities = {}
        expected_value = {}
        for i, label in enumerate(prob_labels):
            p = float(ensemble_proba[i]) if i < len(ensemble_proba) else 0.0
            probabilities[label] = round(p, 4)
            odds_val = odds_map.get(label, 2.0)
            expected_value[label] = round(p * odds_val - 1, 4)

        best_pick = max(expected_value, key=expected_value.get)
        best_ev = expected_value[best_pick]

        # 置信度 = 1 - 模型预测的标准差
        if count >= 2:
            proba_std = np.std([np.array(p) for p in all_probas.values()], axis=0).mean()
            confidence = round(float(1.0 - proba_std * 2), 3)
            confidence = max(0.3, min(0.95, confidence))
        else:
            confidence = 0.6

        # 多数投票
        if votes:
            from collections import Counter
            vote_counter = Counter(votes)
            most_common = vote_counter.most_common(1)[0][0]
            vote_labels = {0: "home_win", 1: "draw", 2: "away_win"}
            model_consensus = vote_labels.get(most_common, best_pick)
            agreement = vote_counter[most_common] / len(votes)
        else:
            model_consensus = best_pick
            agreement = 0.5

        return {
            "probabilities": probabilities,
            "expected_value": expected_value,
            "confidence": confidence,
            "model_count": count,
            "model_probas": all_probas,
            "model_consensus": model_consensus,
            "model_agreement": round(agreement, 2),
            "best_pick": best_pick,
            "best_ev": best_ev,
        }

    def predict_all_tasks(self, odds_data: dict[str, Any]) -> dict[str, Any]:
        """对所有任务运行预测，返回集成结果"""
        results = {}
        for task in self.TASKS:
            result = self.predict(odds_data, task)
            if result:
                results[task] = result
        return results

    def get_ensemble_prediction(self, odds_data: dict[str, Any]) -> dict[str, Any]:
        """
        获取最终集成预测 — 优先使用ML模型，fallback到规则引擎
        """
        ml_result = self.predict(odds_data, "wdl")

        if ml_result and ml_result["model_count"] >= 1:
            return {
                **ml_result,
                "source": "ml_ensemble",
                "model_count": ml_result["model_count"],
            }

        # Fallback: 规则引擎
        return self._rule_based_predict(odds_data)

    def _rule_based_predict(self, odds_data: dict[str, Any]) -> dict[str, Any]:
        """规则引擎 fallback"""
        home = float(odds_data.get("home_win", 2.5))
        draw = float(odds_data.get("draw", 3.5))
        away = float(odds_data.get("away_win", 3.0))

        total = 1 / home + 1 / draw + 1 / away
        probs = {
            "home_win": round((1 / home) / total, 4),
            "draw": round((1 / draw) / total, 4),
            "away_win": round((1 / away) / total, 4),
        }

        ev = {
            k: round(probs[k] * odds - 1, 4)
            for k, odds in [("home_win", home), ("draw", draw), ("away_win", away)]
        }

        best_pick = max(ev, key=ev.get)
        confidence = round(1.0 - (total - 1.0), 3)
        confidence = max(0.3, min(0.85, confidence))

        return {
            "probabilities": probs,
            "expected_value": ev,
            "confidence": confidence,
            "model_count": 0,
            "model_probas": {},
            "model_consensus": best_pick,
            "model_agreement": 0.0,
            "best_pick": best_pick,
            "best_ev": ev[best_pick],
            "source": "rule_based",
        }
