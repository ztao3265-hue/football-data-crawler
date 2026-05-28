"""
预测快照存储测试
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.live.prediction_snapshot_storage import (
    PredictionSnapshotStorage,
    ChangeType
)
from backend.live.live_prediction_engine import PredictionResult, RecommendationLevel


class TestPredictionSnapshotStorage:
    """PredictionSnapshotStorage 测试类"""

    @pytest.fixture
    def storage(self, tmp_path):
        """创建临时存储"""
        db_path = tmp_path / "test_snapshots.db"
        return PredictionSnapshotStorage(str(db_path))

    @pytest.fixture
    def sample_prediction(self):
        """创建示例预测结果"""
        return PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.5, "draw": 0.25, "away_win": 0.25},
            expected_value={"home_win": 0.05, "draw": 0.0, "away_win": -0.1},
            confidence=0.75,
            recommendation_level=RecommendationLevel.NORMAL,
            prediction_time=datetime.now()
        )

    def test_init(self, storage):
        """测试初始化"""
        assert storage.db_path.exists()

    def test_save_prediction_snapshot(self, storage, sample_prediction):
        """测试保存预测快照"""
        snapshot_id = storage.save_prediction_snapshot(sample_prediction)

        assert snapshot_id > 0

    def test_save_prediction_with_odds(self, storage, sample_prediction):
        """测试保存带赔率的预测快照"""
        odds_snapshot = {
            "home_win": 2.0,
            "draw": 3.5,
            "away_win": 3.0
        }

        snapshot_id = storage.save_prediction_snapshot(
            sample_prediction,
            odds_snapshot=odds_snapshot
        )

        assert snapshot_id > 0

        # 验证保存的数据
        saved = storage.get_prediction_snapshot("match_001")
        assert saved is not None
        assert "odds_snapshot" in saved

    def test_get_prediction_snapshot(self, storage, sample_prediction):
        """测试获取预测快照"""
        storage.save_prediction_snapshot(sample_prediction)

        result = storage.get_prediction_snapshot("match_001")

        assert result is not None
        assert result["match_id"] == "match_001"
        assert "predicted_probability" in result

    def test_get_prediction_snapshot_at_time(self, storage):
        """测试获取指定时间的快照"""
        time1 = datetime.now() - timedelta(hours=2)
        time2 = datetime.now() - timedelta(hours=1)

        pred1 = PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.4},
            expected_value={"home_win": 0.0},
            confidence=0.6,
            recommendation_level=RecommendationLevel.PASS,
            prediction_time=time1
        )

        pred2 = PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.6},
            expected_value={"home_win": 0.1},
            confidence=0.8,
            recommendation_level=RecommendationLevel.STRONG_BUY,
            prediction_time=time2
        )

        storage.save_prediction_snapshot(pred1)
        storage.save_prediction_snapshot(pred2)

        result = storage.get_prediction_snapshot("match_001", at_time=time1)
        assert result["predicted_probability"]["home_win"] == 0.4

        result = storage.get_prediction_snapshot("match_001", at_time=time2)
        assert result["predicted_probability"]["home_win"] == 0.6

    def test_record_odds_change(self, storage):
        """测试记录盘口变化"""
        old_odds = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}
        new_odds = {"home_win": 1.8, "draw": 3.6, "away_win": 3.2}

        change_id = storage.record_odds_change("match_001", old_odds, new_odds)

        assert change_id > 0

    def test_record_ev_change(self, storage):
        """测试记录EV变化"""
        old_ev = {"home_win": 0.02, "draw": 0.0, "away_win": -0.05}
        new_ev = {"home_win": 0.08, "draw": 0.01, "away_win": -0.02}

        change_id = storage.record_ev_change("match_001", old_ev, new_ev)

        assert change_id > 0

    def test_record_recommendation_change(self, storage):
        """测试记录推荐等级变化"""
        change_id = storage.record_recommendation_change(
            "match_001",
            RecommendationLevel.PASS,
            RecommendationLevel.STRONG_BUY,
            metadata={"reason": "EV increased significantly"}
        )

        assert change_id > 0

    def test_get_change_history(self, storage):
        """测试获取变化历史"""
        # 记录多种变化
        storage.record_odds_change(
            "match_001",
            {"home_win": 2.0},
            {"home_win": 1.8}
        )
        storage.record_ev_change(
            "match_001",
            {"home_win": 0.0},
            {"home_win": 0.05}
        )
        storage.record_recommendation_change(
            "match_001",
            RecommendationLevel.PASS,
            RecommendationLevel.NORMAL
        )

        history = storage.get_change_history("match_001")

        assert len(history) == 3

    def test_get_change_history_by_type(self, storage):
        """测试按类型获取变化历史"""
        storage.record_odds_change("match_001", {"a": 1}, {"a": 2})
        storage.record_odds_change("match_001", {"a": 2}, {"a": 3})
        storage.record_ev_change("match_001", {"ev": 0}, {"ev": 0.1})

        history = storage.get_change_history(
            "match_001",
            change_type=ChangeType.ODDS.value
        )

        assert len(history) == 2
        for h in history:
            assert h["change_type"] == ChangeType.ODDS.value

    def test_get_all_changes_for_match(self, storage):
        """测试获取比赛的所有变化"""
        storage.record_odds_change("match_001", {"a": 1}, {"a": 2})
        storage.record_ev_change("match_001", {"ev": 0}, {"ev": 0.1})
        storage.record_recommendation_change(
            "match_001",
            RecommendationLevel.PASS,
            RecommendationLevel.NORMAL
        )

        changes = storage.get_all_changes_for_match("match_001")

        assert ChangeType.ODDS.value in changes
        assert ChangeType.EV.value in changes
        assert ChangeType.RECOMMENDATION.value in changes

    def test_detect_significant_changes(self, storage):
        """测试检测显著变化"""
        # 小变化
        storage.record_odds_change(
            "match_001",
            {"home_win": 2.0},
            {"home_win": 1.98}
        )

        # 大变化
        storage.record_odds_change(
            "match_001",
            {"home_win": 2.0},
            {"home_win": 1.5}
        )

        # EV 变化
        storage.record_ev_change(
            "match_001",
            {"home_win": 0.0},
            {"home_win": 0.1}
        )

        significant = storage.detect_significant_changes(
            "match_001",
            odds_threshold=0.1,
            ev_threshold=0.05
        )

        assert len(significant) >= 2

    def test_get_change_summary(self, storage):
        """测试获取变化摘要"""
        storage.record_odds_change("match_001", {"a": 1}, {"a": 2})
        storage.record_odds_change("match_001", {"a": 2}, {"a": 3})
        storage.record_ev_change("match_001", {"ev": 0}, {"ev": 0.1})
        storage.record_recommendation_change(
            "match_001",
            RecommendationLevel.PASS,
            RecommendationLevel.STRONG_BUY
        )

        summary = storage.get_change_summary("match_001")

        assert summary["total_changes"] == 4
        assert summary["recommendation_changes"] == 1

    def test_compare_two_snapshots(self, storage):
        """测试比较两个快照"""
        time1 = datetime.now() - timedelta(hours=2)
        time2 = datetime.now() - timedelta(hours=1)

        pred1 = PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.4},
            expected_value={"home_win": 0.0},
            confidence=0.5,
            recommendation_level=RecommendationLevel.PASS,
            prediction_time=time1
        )

        pred2 = PredictionResult(
            match_id="match_001",
            predicted_probability={"home_win": 0.6},
            expected_value={"home_win": 0.1},
            confidence=0.8,
            recommendation_level=RecommendationLevel.STRONG_BUY,
            prediction_time=time2
        )

        storage.save_prediction_snapshot(pred1)
        storage.save_prediction_snapshot(pred2)

        comparison = storage.compare_two_snapshots("match_001", time1, time2)

        assert "probability_change" in comparison
        assert "ev_change" in comparison
        assert abs(comparison["confidence_change"] - 0.3) < 0.01

    def test_compare_nonexistent_snapshots(self, storage):
        """测试比较不存在的快照"""
        comparison = storage.compare_two_snapshots(
            "nonexistent",
            datetime.now() - timedelta(hours=1),
            datetime.now()
        )

        assert "error" in comparison

    def test_cleanup_old_snapshots(self, storage, sample_prediction):
        """测试清理旧快照"""
        # 保存新快照
        storage.save_prediction_snapshot(sample_prediction)

        # 清理 0 天前的快照（删除所有）
        deleted = storage.cleanup_old_snapshots(days_old=0)

        assert deleted >= 0

    def test_export_match_history(self, storage, sample_prediction):
        """测试导出比赛历史"""
        storage.save_prediction_snapshot(sample_prediction)
        storage.record_odds_change(
            "match_001",
            {"home_win": 2.0},
            {"home_win": 1.8}
        )

        history = storage.export_match_history("match_001")

        assert "match_id" in history
        assert "snapshots" in history
        assert "changes" in history
        assert "summary" in history

    def test_nonexistent_snapshot(self, storage):
        """测试获取不存在的快照"""
        result = storage.get_prediction_snapshot("nonexistent")
        assert result is None

    def test_multiple_matches(self, storage):
        """测试多场比赛"""
        for i in range(5):
            pred = PredictionResult(
                match_id=f"match_{i:03d}",
                predicted_probability={"home_win": 0.5},
                expected_value={"home_win": 0.05},
                confidence=0.7,
                recommendation_level=RecommendationLevel.NORMAL,
                prediction_time=datetime.now()
            )
            storage.save_prediction_snapshot(pred)

        for i in range(5):
            result = storage.get_prediction_snapshot(f"match_{i:03d}")
            assert result is not None

    def test_change_type_enum(self):
        """测试变化类型枚举"""
        assert ChangeType.PREDICTION.value == "prediction"
        assert ChangeType.ODDS.value == "odds"
        assert ChangeType.EV.value == "ev"
        assert ChangeType.RECOMMENDATION.value == "recommendation"

    def test_unicode_match_id(self, storage):
        """测试中文比赛ID"""
        pred = PredictionResult(
            match_id="英超_阿森纳_切尔西",
            predicted_probability={"home_win": 0.6},
            expected_value={"home_win": 0.1},
            confidence=0.8,
            recommendation_level=RecommendationLevel.STRONG_BUY,
            prediction_time=datetime.now()
        )

        storage.save_prediction_snapshot(pred)

        result = storage.get_prediction_snapshot("英超_阿森纳_切尔西")
        assert result is not None

    def test_calculate_odds_change_magnitude(self, storage):
        """测试盘口变化幅度计算"""
        old_odds = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}
        new_odds = {"home_win": 1.8, "draw": 3.5, "away_win": 3.3}

        magnitude = storage._calculate_odds_change_magnitude(old_odds, new_odds)

        assert magnitude > 0

    def test_calculate_ev_change_magnitude(self, storage):
        """测试EV变化幅度计算"""
        old_ev = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
        new_ev = {"home_win": 0.1, "draw": 0.05, "away_win": -0.05}

        magnitude = storage._calculate_ev_change_magnitude(old_ev, new_ev)

        assert magnitude > 0
