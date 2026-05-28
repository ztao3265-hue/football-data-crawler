"""
实时预测引擎 — 每次快照更新后自动重新预测比赛
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Callable
import threading

from backend.live.realtime_odds_collector import RealtimeOddsCollector, OddsType


class RecommendationLevel:
    """推荐等级"""
    STRONG_BUY = "strong_buy"
    NORMAL = "normal"
    PASS = "pass"


class PredictionResult:
    """预测结果"""

    def __init__(
        self,
        match_id: str,
        predicted_probability: dict[str, float],
        expected_value: dict[str, float],
        confidence: float,
        recommendation_level: str,
        prediction_time: datetime,
        model_version: str = "1.0",
        features_used: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None
    ):
        self.match_id = match_id
        self.predicted_probability = predicted_probability
        self.expected_value = expected_value
        self.confidence = confidence
        self.recommendation_level = recommendation_level
        self.prediction_time = prediction_time
        self.model_version = model_version
        self.features_used = features_used or []
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "match_id": self.match_id,
            "predicted_probability": self.predicted_probability,
            "expected_value": self.expected_value,
            "confidence": self.confidence,
            "recommendation_level": self.recommendation_level,
            "prediction_time": self.prediction_time.isoformat(),
            "model_version": self.model_version,
            "features_used": self.features_used,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PredictionResult":
        """从字典创建"""
        return cls(
            match_id=data["match_id"],
            predicted_probability=data["predicted_probability"],
            expected_value=data["expected_value"],
            confidence=data["confidence"],
            recommendation_level=data["recommendation_level"],
            prediction_time=datetime.fromisoformat(data["prediction_time"]),
            model_version=data.get("model_version", "1.0"),
            features_used=data.get("features_used", []),
            metadata=data.get("metadata", {})
        )


class LivePredictionEngine:
    """
    实时预测引擎

    功能：
    - 快照更新时自动重新预测
    - 计算概率、EV、置信度、推荐等级
    - 支持自定义预测模型
    - 保存预测历史
    """

    # EV 和 Confidence 阈值
    STRONG_BUY_EV_THRESHOLD = 0.05
    STRONG_BUY_CONFIDENCE_THRESHOLD = 0.75
    NORMAL_EV_THRESHOLD = 0.02
    NORMAL_CONFIDENCE_THRESHOLD = 0.60

    def __init__(
        self,
        db_path: str = "data/live_predictions.db",
        odds_collector: Optional[RealtimeOddsCollector] = None
    ):
        """
        初始化预测引擎

        Args:
            db_path: 预测数据库路径
            odds_collector: 赔率采集器实例
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.odds_collector = odds_collector
        self._prediction_models: dict[str, Callable] = {}
        self._feature_extractors: dict[str, Callable] = {}

        self._init_db()
        self._register_default_models()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    prediction_time TEXT NOT NULL,
                    predicted_probability TEXT NOT NULL,
                    expected_value TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    recommendation_level TEXT NOT NULL,
                    model_version TEXT DEFAULT '1.0',
                    features_used TEXT,
                    odds_snapshot TEXT,
                    metadata TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_match_pred_time
                ON predictions(match_id, prediction_time)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_recommendation
                ON predictions(recommendation_level)
            """)

    def _register_default_models(self):
        """注册默认预测模型"""
        # 默认模型返回基于赔率的简单预测
        self.register_prediction_model("default", self._default_predict)

    def _default_predict(self, match_data: dict[str, Any]) -> dict[str, Any]:
        """
        默认预测逻辑

        Args:
            match_data: 比赛数据（包含赔率等）

        Returns:
            预测结果
        """
        odds = match_data.get("odds", {})

        # 基于欧赔计算隐含概率
        home_odds = odds.get("home_win", 2.0)
        draw_odds = odds.get("draw", 3.5)
        away_odds = odds.get("away_win", 3.0)

        # 计算隐含概率
        total = 1/home_odds + 1/draw_odds + 1/away_odds
        implied_prob = {
            "home_win": (1/home_odds) / total,
            "draw": (1/draw_odds) / total,
            "away_win": (1/away_odds) / total
        }

        # 计算期望值 (简化版)
        ev = {
            "home_win": implied_prob["home_win"] * home_odds - 1,
            "draw": implied_prob["draw"] * draw_odds - 1,
            "away_win": implied_prob["away_win"] * away_odds - 1
        }

        # 置信度（基于市场一致性）
        confidence = 1 - (total - 1)  # 越接近1，置信度越高

        return {
            "probability": implied_prob,
            "expected_value": ev,
            "confidence": max(0.0, min(1.0, confidence))
        }

    def register_prediction_model(
        self,
        model_name: str,
        predict_func: Callable[[dict[str, Any]], dict[str, Any]]
    ):
        """
        注册预测模型

        Args:
            model_name: 模型名称
            predict_func: 预测函数，接收比赛数据，返回预测结果
        """
        self._prediction_models[model_name] = predict_func

    def register_feature_extractor(
        self,
        feature_name: str,
        extract_func: Callable[[dict[str, Any]], Any]
    ):
        """
        注册特征提取器

        Args:
            feature_name: 特征名称
            extract_func: 提取函数
        """
        self._feature_extractors[feature_name] = extract_func

    def calculate_recommendation_level(
        self,
        expected_value: dict[str, float],
        confidence: float
    ) -> str:
        """
        计算推荐等级

        Args:
            expected_value: 期望值字典
            confidence: 置信度

        Returns:
            推荐等级
        """
        max_ev = max(expected_value.values()) if expected_value else 0

        if max_ev >= self.STRONG_BUY_EV_THRESHOLD and confidence >= self.STRONG_BUY_CONFIDENCE_THRESHOLD:
            return RecommendationLevel.STRONG_BUY
        elif max_ev >= self.NORMAL_EV_THRESHOLD and confidence >= self.NORMAL_CONFIDENCE_THRESHOLD:
            return RecommendationLevel.NORMAL
        else:
            return RecommendationLevel.PASS

    def predict(
        self,
        match_id: str,
        odds_data: dict[str, Any],
        model_name: str = "default",
        match_info: Optional[dict[str, Any]] = None
    ) -> PredictionResult:
        """
        执行预测

        Args:
            match_id: 比赛ID
            odds_data: 赔率数据
            model_name: 使用的模型名称
            match_info: 比赛附加信息

        Returns:
            预测结果
        """
        # 获取预测模型
        predict_func = self._prediction_models.get(model_name)
        if predict_func is None:
            predict_func = self._default_predict

        # 准备输入数据
        match_data = {
            "match_id": match_id,
            "odds": odds_data,
            "match_info": match_info or {}
        }

        # 执行预测
        prediction = predict_func(match_data)

        # 构建结果
        predicted_prob = prediction.get("probability", {})
        expected_value = prediction.get("expected_value", {})
        confidence = prediction.get("confidence", 0.5)

        # 计算推荐等级
        recommendation = self.calculate_recommendation_level(expected_value, confidence)

        result = PredictionResult(
            match_id=match_id,
            predicted_probability=predicted_prob,
            expected_value=expected_value,
            confidence=confidence,
            recommendation_level=recommendation,
            prediction_time=datetime.now(),
            model_version=model_name,
            features_used=list(self._feature_extractors.keys()),
            metadata={"source": "live_prediction_engine"}
        )

        # 保存预测
        self._save_prediction(result, odds_data)

        return result

    def _save_prediction(self, result: PredictionResult, odds_snapshot: dict[str, Any]):
        """保存预测结果"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO predictions
                (match_id, prediction_time, predicted_probability, expected_value,
                 confidence, recommendation_level, model_version, features_used,
                 odds_snapshot, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.match_id,
                    result.prediction_time.isoformat(),
                    json.dumps(result.predicted_probability, ensure_ascii=False),
                    json.dumps(result.expected_value, ensure_ascii=False),
                    result.confidence,
                    result.recommendation_level,
                    result.model_version,
                    json.dumps(result.features_used, ensure_ascii=False),
                    json.dumps(odds_snapshot, ensure_ascii=False),
                    json.dumps(result.metadata, ensure_ascii=False)
                )
            )

    def predict_from_collector(
        self,
        match_id: str,
        model_name: str = "default"
    ) -> Optional[PredictionResult]:
        """
        从采集器获取赔率并预测

        Args:
            match_id: 比赛ID
            model_name: 模型名称

        Returns:
            预测结果
        """
        if self.odds_collector is None:
            return None

        # 获取最新欧赔
        odds_data = self.odds_collector.get_latest_odds(match_id, OddsType.EUROPEAN)

        if odds_data is None:
            return None

        return self.predict(match_id, odds_data, model_name)

    def get_prediction_history(
        self,
        match_id: str,
        limit: int = 100
    ) -> list[PredictionResult]:
        """
        获取预测历史

        Args:
            match_id: 比赛ID
            limit: 返回数量限制

        Returns:
            预测结果列表
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT predicted_probability, expected_value, confidence,
                       recommendation_level, prediction_time, model_version,
                       features_used, metadata
                FROM predictions
                WHERE match_id = ?
                ORDER BY prediction_time DESC
                LIMIT ?
                """,
                (match_id, limit)
            )
            rows = cursor.fetchall()

        results = []
        for row in rows:
            result = PredictionResult(
                match_id=match_id,
                predicted_probability=json.loads(row[0]),
                expected_value=json.loads(row[1]),
                confidence=row[2],
                recommendation_level=row[3],
                prediction_time=datetime.fromisoformat(row[4]),
                model_version=row[5],
                features_used=json.loads(row[6]),
                metadata=json.loads(row[7])
            )
            results.append(result)

        return results

    def get_latest_prediction(self, match_id: str) -> Optional[PredictionResult]:
        """
        获取最新预测

        Args:
            match_id: 比赛ID

        Returns:
            最新预测结果
        """
        history = self.get_prediction_history(match_id, limit=1)
        return history[0] if history else None

    def get_recommendations(
        self,
        level: Optional[str] = None,
        min_confidence: Optional[float] = None,
        min_ev: Optional[float] = None,
        limit: int = 50
    ) -> list[PredictionResult]:
        """
        获取推荐列表

        Args:
            level: 按推荐等级过滤
            min_confidence: 最低置信度
            min_ev: 最低期望值
            limit: 返回数量限制

        Returns:
            预测结果列表
        """
        with sqlite3.connect(self.db_path) as conn:
            sql = """
                SELECT match_id, predicted_probability, expected_value, confidence,
                       recommendation_level, prediction_time, model_version,
                       features_used, metadata
                FROM predictions
                WHERE 1=1
            """
            params = []

            if level:
                sql += " AND recommendation_level = ?"
                params.append(level)

            if min_confidence:
                sql += " AND confidence >= ?"
                params.append(min_confidence)

            sql += " ORDER BY prediction_time DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

        results = []
        for row in rows:
            result = PredictionResult(
                match_id=row[0],
                predicted_probability=json.loads(row[1]),
                expected_value=json.loads(row[2]),
                confidence=row[3],
                recommendation_level=row[4],
                prediction_time=datetime.fromisoformat(row[5]),
                model_version=row[6],
                features_used=json.loads(row[7]),
                metadata=json.loads(row[8])
            )

            # 额外的 EV 过滤
            if min_ev:
                max_ev = max(result.expected_value.values()) if result.expected_value else 0
                if max_ev < min_ev:
                    continue

            results.append(result)

        return results

    def compare_predictions(
        self,
        match_id: str,
        time1: datetime,
        time2: datetime
    ) -> dict[str, Any]:
        """
        比较两个时间点的预测变化

        Args:
            match_id: 比赛ID
            time1: 第一个时间点
            time2: 第二个时间点

        Returns:
            变化对比
        """
        history = self.get_prediction_history(match_id)

        pred1 = None
        pred2 = None

        for pred in history:
            if pred.prediction_time <= time1 and pred1 is None:
                pred1 = pred
            if pred.prediction_time <= time2 and pred2 is None:
                pred2 = pred

        if pred1 is None or pred2 is None:
            return {"error": "预测数据不足"}

        return {
            "probability_change": {
                k: pred2.predicted_probability.get(k, 0) - pred1.predicted_probability.get(k, 0)
                for k in pred1.predicted_probability.keys()
            },
            "ev_change": {
                k: pred2.expected_value.get(k, 0) - pred1.expected_value.get(k, 0)
                for k in pred1.expected_value.keys()
            },
            "confidence_change": pred2.confidence - pred1.confidence,
            "recommendation_change": {
                "old": pred1.recommendation_level,
                "new": pred2.recommendation_level
            }
        }

    def get_prediction_stats(self) -> dict[str, Any]:
        """获取预测统计"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    recommendation_level,
                    COUNT(*) as count,
                    AVG(confidence) as avg_confidence
                FROM predictions
                GROUP BY recommendation_level
                """
            )
            rows = cursor.fetchall()

            total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]

        stats = {
            "total_predictions": total,
            "by_level": {}
        }

        for row in rows:
            stats["by_level"][row[0]] = {
                "count": row[1],
                "avg_confidence": row[2]
            }

        return stats

    def delete_old_predictions(self, days_old: int = 30) -> int:
        """
        删除旧预测记录

        Args:
            days_old: 保留最近多少天

        Returns:
            删除的记录数
        """
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days_old)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM predictions WHERE prediction_time < ?",
                (cutoff.isoformat(),)
            )
            return cursor.rowcount
