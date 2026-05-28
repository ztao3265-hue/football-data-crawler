"""
推荐过滤器测试
"""

import json
from datetime import datetime, date, timedelta
from pathlib import Path

import pytest

from backend.live.recommendation_filter import (
    RecommendationFilter,
    LiquidityLevel
)
from backend.live.live_prediction_engine import PredictionResult, RecommendationLevel


class TestRecommendationFilter:
    """RecommendationFilter 测试类"""

    @pytest.fixture
    def filter_instance(self, tmp_path):
        """创建临时过滤器"""
        db_path = tmp_path / "test_filter.db"
        return RecommendationFilter(str(db_path))

    @pytest.fixture
    def sample_prediction(self):
        """创建示例预测结果"""
        return PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.5},
            expected_value={"home_win": 0.05},
            confidence=0.75,
            recommendation_level=RecommendationLevel.NORMAL,
            prediction_time=datetime.now()
        )

    def test_init(self, filter_instance):
        """测试初始化"""
        assert filter_instance.db_path.exists()
        assert filter_instance.min_ev > 0
        assert filter_instance.min_confidence > 0

    def test_set_min_ev(self, filter_instance):
        """测试设置最低EV"""
        filter_instance.set_min_ev(0.1)
        assert filter_instance.min_ev == 0.1

    def test_set_min_confidence(self, filter_instance):
        """测试设置最低置信度"""
        filter_instance.set_min_confidence(0.8)
        assert filter_instance.min_confidence == 0.8

    def test_set_min_liquidity(self, filter_instance):
        """测试设置最低流动性"""
        filter_instance.set_min_liquidity(LiquidityLevel.HIGH)
        assert filter_instance.min_liquidity == LiquidityLevel.HIGH

    def test_set_max_per_day(self, filter_instance):
        """测试设置每日最大推荐数"""
        filter_instance.set_max_per_day(5)
        assert filter_instance.max_per_day == 5

    def test_check_ev_pass(self, filter_instance, sample_prediction):
        """测试EV检查通过"""
        filter_instance.set_min_ev(0.03)
        passed, reason = filter_instance.check_ev(sample_prediction)

        assert passed is True
        assert "EV" in reason

    def test_check_ev_fail(self, filter_instance, sample_prediction):
        """测试EV检查失败"""
        filter_instance.set_min_ev(0.1)
        passed, reason = filter_instance.check_ev(sample_prediction)

        assert passed is False

    def test_check_confidence_pass(self, filter_instance, sample_prediction):
        """测试置信度检查通过"""
        filter_instance.set_min_confidence(0.7)
        passed, reason = filter_instance.check_confidence(sample_prediction)

        assert passed is True

    def test_check_confidence_fail(self, filter_instance, sample_prediction):
        """测试置信度检查失败"""
        filter_instance.set_min_confidence(0.9)
        passed, reason = filter_instance.check_confidence(sample_prediction)

        assert passed is False

    def test_check_league_pass(self, filter_instance):
        """测试联赛检查通过"""
        passed, reason = filter_instance.check_league("Premier League")

        assert passed is True

    def test_check_league_excluded(self, filter_instance):
        """测试排除联赛"""
        filter_instance.exclude_league("Serie B")
        passed, reason = filter_instance.check_league("Serie B")

        assert passed is False
        assert "excluded" in reason

    def test_check_league_included_only(self, filter_instance):
        """测试仅包含指定联赛"""
        filter_instance.include_only_leagues(["Premier League", "La Liga"])

        passed, _ = filter_instance.check_league("Premier League")
        assert passed is True

        passed, _ = filter_instance.check_league("Bundesliga")
        assert passed is False

    def test_check_liquidity_high(self, filter_instance):
        """测试高流动性联赛"""
        filter_instance.set_min_liquidity(LiquidityLevel.HIGH)

        passed, _ = filter_instance.check_liquidity("Premier League")
        assert passed is True

    def test_check_liquidity_low(self, filter_instance):
        """测试低流动性"""
        filter_instance.set_min_liquidity(LiquidityLevel.HIGH)

        # 非高流动性联赛
        passed, _ = filter_instance.check_liquidity("Unknown League")
        assert passed is False

    def test_check_daily_limit(self, filter_instance):
        """测试每日限制检查"""
        passed, reason = filter_instance.check_daily_limit()

        assert passed is True

    def test_check_daily_limit_reached(self, filter_instance, sample_prediction):
        """测试达到每日限制"""
        filter_instance.set_max_per_day(1)

        # 第一次通过
        filter_instance.filter(sample_prediction, "Premier League")

        # 第二次应该失败
        passed, reason = filter_instance.check_daily_limit()

        assert passed is False

    def test_filter_pass(self, filter_instance, sample_prediction):
        """测试完整过滤通过"""
        result = filter_instance.filter(sample_prediction, "Premier League")

        assert result["passed"] is True
        assert len(result["reasons"]) == 0

    def test_filter_fail_ev(self, filter_instance, sample_prediction):
        """测试过滤失败 - EV"""
        filter_instance.set_min_ev(0.1)
        result = filter_instance.filter(sample_prediction, "Premier League")

        assert result["passed"] is False
        assert any("EV" in r for r in result["reasons"])

    def test_filter_fail_confidence(self, filter_instance, sample_prediction):
        """测试过滤失败 - 置信度"""
        filter_instance.set_min_confidence(0.9)
        result = filter_instance.filter(sample_prediction, "Premier League")

        assert result["passed"] is False
        assert any("Confidence" in r for r in result["reasons"])

    def test_filter_fail_league(self, filter_instance, sample_prediction):
        """测试过滤失败 - 联赛"""
        filter_instance.exclude_league("Test League")
        result = filter_instance.filter(sample_prediction, "Test League")

        assert result["passed"] is False

    def test_filter_batch(self, filter_instance):
        """测试批量过滤"""
        predictions = [
            (
                PredictionResult(
                    match_id=f"match_{i:03d}",
                    predicted_probability={"home_win": 0.5},
                    expected_value={"home_win": 0.05},
                    confidence=0.75,
                    recommendation_level=RecommendationLevel.NORMAL,
                    prediction_time=datetime.now()
                ),
                "Premier League"
            )
            for i in range(5)
        ]

        results = filter_instance.filter_batch(predictions)

        assert len(results) == 5

    def test_filter_batch_with_limit(self, filter_instance):
        """测试批量过滤 - 达到每日限制"""
        filter_instance.set_max_per_day(2)

        predictions = [
            (
                PredictionResult(
                    match_id=f"match_{i:03d}",
                    predicted_probability={"home_win": 0.5},
                    expected_value={"home_win": 0.05},
                    confidence=0.75,
                    recommendation_level=RecommendationLevel.NORMAL,
                    prediction_time=datetime.now()
                ),
                "Premier League"
            )
            for i in range(10)
        ]

        results = filter_instance.filter_batch(predictions)

        # 只有前 2 个通过
        passed_count = sum(1 for r in results if r["passed"])
        assert passed_count <= 2

    def test_get_filtered_recommendations(self, filter_instance, sample_prediction):
        """测试获取过滤后的推荐"""
        filter_instance.filter(sample_prediction, "Premier League")

        recommendations = filter_instance.get_filtered_recommendations()

        assert len(recommendations) >= 1

    def test_get_filter_stats(self, filter_instance, sample_prediction):
        """测试获取过滤统计"""
        filter_instance.filter(sample_prediction, "Premier League")

        stats = filter_instance.get_filter_stats()

        assert "total" in stats
        assert "passed" in stats
        assert "failed" in stats

    def test_reset_daily_count(self, filter_instance, sample_prediction):
        """测试重置每日计数"""
        filter_instance.filter(sample_prediction, "Premier League")

        filter_instance.reset_daily_count()

        count = filter_instance._get_daily_recommendation_count(date.today())
        assert count == 0

    def test_get_filter_config(self, filter_instance):
        """测试获取过滤配置"""
        config = filter_instance.get_filter_config()

        assert "min_ev" in config
        assert "min_confidence" in config
        assert "min_liquidity" in config
        assert "max_per_day" in config

    def test_update_config(self, filter_instance):
        """测试更新配置"""
        new_config = {
            "min_ev": 0.08,
            "min_confidence": 0.85,
            "min_liquidity": "high",
            "max_per_day": 5
        }

        filter_instance.update_config(new_config)

        assert filter_instance.min_ev == 0.08
        assert filter_instance.min_confidence == 0.85
        assert filter_instance.min_liquidity == LiquidityLevel.HIGH
        assert filter_instance.max_per_day == 5

    def test_clear_league_filters(self, filter_instance):
        """测试清除联赛过滤"""
        filter_instance.exclude_league("League A")
        filter_instance.include_only_leagues(["League B"])

        filter_instance.clear_league_filters()

        assert len(filter_instance._excluded_leagues) == 0
        assert filter_instance._included_leagues is None

    def test_liquidity_level_enum(self):
        """测试流动性等级枚举"""
        assert LiquidityLevel.HIGH.value == "high"
        assert LiquidityLevel.MEDIUM.value == "medium"
        assert LiquidityLevel.LOW.value == "low"

    def test_high_liquidity_leagues(self, filter_instance):
        """测试高流动性联赛列表"""
        assert "Premier League" in filter_instance.HIGH_LIQUIDITY_LEAGUES
        assert "La Liga" in filter_instance.HIGH_LIQUIDITY_LEAGUES
        assert "英超" in filter_instance.HIGH_LIQUIDITY_LEAGUES

    def test_unicode_league(self, filter_instance, sample_prediction):
        """测试中文联赛名"""
        result = filter_instance.filter(sample_prediction, "英超")

        assert "match_id" in result

    def test_filter_without_daily_limit_check(self, filter_instance, sample_prediction):
        """测试不检查每日限制"""
        filter_instance.set_max_per_day(0)

        result = filter_instance.filter(
            sample_prediction,
            "Premier League",
            check_daily_limit=False
        )

        # 不检查每日限制时，应该只看其他条件
        assert "checks" in result

    def test_filter_multiple_reasons(self, filter_instance, sample_prediction):
        """测试多个过滤原因"""
        filter_instance.set_min_ev(0.1)
        filter_instance.set_min_confidence(0.9)
        filter_instance.exclude_league("Test League")

        result = filter_instance.filter(sample_prediction, "Test League")

        assert len(result["reasons"]) >= 2

    def test_filter_by_date(self, filter_instance, sample_prediction):
        """测试按日期获取推荐"""
        filter_instance.filter(sample_prediction, "Premier League")

        yesterday = date.today() - timedelta(days=1)
        recommendations = filter_instance.get_filtered_recommendations(yesterday)

        assert len(recommendations) == 0

        today_recommendations = filter_instance.get_filtered_recommendations(date.today())
        assert len(today_recommendations) >= 1
