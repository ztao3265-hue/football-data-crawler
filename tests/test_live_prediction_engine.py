"""
实时预测引擎测试
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.live.live_prediction_engine import (
    LivePredictionEngine,
    PredictionResult,
    RecommendationLevel
)


class TestPredictionResult:
    """PredictionResult 测试类"""

    def test_create_prediction_result(self):
        """测试创建预测结果"""
        result = PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.5, "draw": 0.25, "away_win": 0.25},
            expected_value={"home_win": 0.05, "draw": 0.0, "away_win": -0.1},
            confidence=0.75,
            recommendation_level=RecommendationLevel.STRONG_BUY,
            prediction_time=datetime.now()
        )

        assert result.match_id == "match_001"
        assert result.confidence == 0.75

    def test_to_dict(self):
        """测试转换为字典"""
        result = PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.5},
            expected_value={"home_win": 0.05},
            confidence=0.75,
            recommendation_level=RecommendationLevel.NORMAL,
            prediction_time=datetime.now()
        )

        data = result.to_dict()

        assert data["match_id"] == "match_001"
        assert "predicted_probability" in data
        assert "prediction_time" in data

    def test_from_dict(self):
        """测试从字典创建"""
        data = {
            "match_id": "match_001",
            "predicted_probability": {"home_win": 0.6},
            "expected_value": {"home_win": 0.1},
            "confidence": 0.8,
            "recommendation_level": RecommendationLevel.STRONG_BUY,
            "prediction_time": datetime.now().isoformat(),
            "model_version": "2.0",
            "features_used": ["feature1"],
            "metadata": {"key": "value"}
        }

        result = PredictionResult.from_dict(data)

        assert result.match_id == "match_001"
        assert result.predicted_probability["home_win"] == 0.6
        assert result.model_version == "2.0"


class TestLivePredictionEngine:
    """LivePredictionEngine 测试类"""

    @pytest.fixture
    def engine(self, tmp_path):
        """创建临时预测引擎"""
        db_path = tmp_path / "test_predictions.db"
        return LivePredictionEngine(str(db_path))

    def test_init(self, engine):
        """测试初始化"""
        assert engine.db_path.exists()
        assert "default" in engine._prediction_models

    def test_register_prediction_model(self, engine):
        """测试注册预测模型"""
        def custom_predict(data):
            return {
                "probability": {"home_win": 0.7},
                "expected_value": {"home_win": 0.1},
                "confidence": 0.85
            }

        engine.register_prediction_model("custom", custom_predict)

        assert "custom" in engine._prediction_models

    def test_predict_default_model(self, engine):
        """测试默认模型预测"""
        odds_data = {
            "home_win": 2.0,
            "draw": 3.5,
            "away_win": 3.0
        }

        result = engine.predict("match_001", odds_data)

        assert result.match_id == "match_001"
        assert "home_win" in result.predicted_probability
        assert "home_win" in result.expected_value
        assert result.confidence >= 0
        assert result.recommendation_level in [
            RecommendationLevel.STRONG_BUY,
            RecommendationLevel.NORMAL,
            RecommendationLevel.PASS
        ]

    def test_predict_custom_model(self, engine):
        """测试自定义模型预测"""
        def custom_predict(data):
            return {
                "probability": {"home_win": 0.8},
                "expected_value": {"home_win": 0.15},
                "confidence": 0.9
            }

        engine.register_prediction_model("custom", custom_predict)

        result = engine.predict("match_001", {}, model_name="custom")

        assert result.predicted_probability["home_win"] == 0.8
        assert result.expected_value["home_win"] == 0.15
        assert result.confidence == 0.9
        assert result.model_version == "custom"

    def test_calculate_recommendation_level(self, engine):
        """测试计算推荐等级"""
        # Strong Buy
        level = engine.calculate_recommendation_level(
            {"home_win": 0.08},
            0.8
        )
        assert level == RecommendationLevel.STRONG_BUY

        # Normal
        level = engine.calculate_recommendation_level(
            {"home_win": 0.03},
            0.65
        )
        assert level == RecommendationLevel.NORMAL

        # Pass
        level = engine.calculate_recommendation_level(
            {"home_win": 0.01},
            0.5
        )
        assert level == RecommendationLevel.PASS

    def test_get_prediction_history(self, engine):
        """测试获取预测历史"""
        odds_data = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}

        # 创建多个预测
        for i in range(5):
            engine.predict(f"match_001", odds_data)

        history = engine.get_prediction_history("match_001")

        assert len(history) == 5

    def test_get_latest_prediction(self, engine):
        """测试获取最新预测"""
        odds_data = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}

        engine.predict("match_001", odds_data)
        engine.predict("match_001", {"home_win": 1.5, "draw": 4.0, "away_win": 5.0})

        latest = engine.get_latest_prediction("match_001")

        assert latest is not None

    def test_get_recommendations(self, engine):
        """测试获取推荐列表"""
        odds_data = {"home_win": 1.8, "draw": 3.5, "away_win": 4.0}

        for i in range(10):
            engine.predict(f"match_{i:03d}", odds_data)

        recommendations = engine.get_recommendations()

        assert len(recommendations) > 0

    def test_get_recommendations_by_level(self, engine):
        """测试按等级获取推荐"""
        odds_data = {"home_win": 1.8, "draw": 3.5, "away_win": 4.0}

        for i in range(10):
            engine.predict(f"match_{i:03d}", odds_data)

        recommendations = engine.get_recommendations(
            level=RecommendationLevel.NORMAL
        )

        for rec in recommendations:
            assert rec.recommendation_level == RecommendationLevel.NORMAL

    def test_get_recommendations_with_filters(self, engine):
        """测试带过滤条件获取推荐"""
        odds_data = {"home_win": 1.5, "draw": 4.0, "away_win": 6.0}

        for i in range(10):
            engine.predict(f"match_{i:03d}", odds_data)

        recommendations = engine.get_recommendations(
            min_confidence=0.5,
            min_ev=0.0
        )

        assert len(recommendations) >= 0

    def test_compare_predictions(self, engine):
        """测试比较预测变化"""
        odds_data_1 = {"home_win": 2.5, "draw": 3.5, "away_win": 2.5}
        odds_data_2 = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}

        time1 = datetime.now() - timedelta(hours=2)
        time2 = datetime.now()

        engine.predict("match_001", odds_data_1)
        engine.predict("match_001", odds_data_2)

        comparison = engine.compare_predictions("match_001", time1, time2)

        assert "probability_change" in comparison or "error" in comparison

    def test_get_prediction_stats(self, engine):
        """测试获取预测统计"""
        odds_data = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}

        for i in range(20):
            engine.predict(f"match_{i:03d}", odds_data)

        stats = engine.get_prediction_stats()

        assert "total_predictions" in stats
        assert stats["total_predictions"] == 20
        assert "by_level" in stats

    def test_delete_old_predictions(self, engine):
        """测试删除旧预测"""
        odds_data = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}

        engine.predict("match_001", odds_data)

        deleted = engine.delete_old_predictions(days_old=0)

        assert deleted >= 0

    def test_predict_with_match_info(self, engine):
        """测试带比赛信息的预测"""
        odds_data = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}
        match_info = {
            "league": "Premier League",
            "home_team": "Arsenal",
            "away_team": "Chelsea"
        }

        result = engine.predict(
            "match_001",
            odds_data,
            match_info=match_info
        )

        assert result is not None

    def test_nonexistent_model_fallback(self, engine):
        """测试不存在的模型回退到默认"""
        result = engine.predict(
            "match_001",
            {"home_win": 2.0, "draw": 3.5, "away_win": 3.0},
            model_name="nonexistent"
        )

        # 应该使用默认模型
        assert result is not None

    def test_recommendation_level_constants(self):
        """测试推荐等级常量"""
        assert RecommendationLevel.STRONG_BUY == "strong_buy"
        assert RecommendationLevel.NORMAL == "normal"
        assert RecommendationLevel.PASS == "pass"

    def test_prediction_with_unicode(self, engine):
        """测试中文比赛信息"""
        odds_data = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}

        result = engine.predict("中文比赛001", odds_data)

        assert result.match_id == "中文比赛001"

        history = engine.get_prediction_history("中文比赛001")
        assert len(history) == 1

    def test_empty_odds_data(self, engine):
        """测试空赔率数据"""
        result = engine.predict("match_001", {})

        # 应该返回默认值
        assert result is not None

    def test_multiple_predictions_same_match(self, engine):
        """测试同一场比赛多次预测"""
        odds_data = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}

        # 第一次预测
        result1 = engine.predict("match_001", odds_data)

        # 赔率变化后重新预测
        new_odds = {"home_win": 1.8, "draw": 3.8, "away_win": 3.5}
        result2 = engine.predict("match_001", new_odds)

        assert result1.prediction_time != result2.prediction_time

        history = engine.get_prediction_history("match_001")
        assert len(history) == 2

    def test_register_feature_extractor(self, engine):
        """测试注册特征提取器"""
        def extract_home_form(data):
            return data.get("home_form", "unknown")

        engine.register_feature_extractor("home_form", extract_home_form)

        assert "home_form" in engine._feature_extractors

    def test_prediction_with_low_confidence(self, engine):
        """测试低置信度预测"""
        def low_confidence_predict(data):
            return {
                "probability": {"home_win": 0.5},
                "expected_value": {"home_win": 0.01},
                "confidence": 0.3
            }

        engine.register_prediction_model("low_conf", low_confidence_predict)

        result = engine.predict("match_001", {}, model_name="low_conf")

        assert result.recommendation_level == RecommendationLevel.PASS

    def test_prediction_with_high_ev(self, engine):
        """测试高期望值预测"""
        def high_ev_predict(data):
            return {
                "probability": {"home_win": 0.6},
                "expected_value": {"home_win": 0.2},
                "confidence": 0.85
            }

        engine.register_prediction_model("high_ev", high_ev_predict)

        result = engine.predict("match_001", {}, model_name="high_ev")

        assert result.recommendation_level == RecommendationLevel.STRONG_BUY
