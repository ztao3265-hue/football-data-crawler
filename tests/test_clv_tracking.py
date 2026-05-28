"""
CLV 追踪测试
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.live.clv_tracking import (
    CLVTracking,
    CLVStatus
)


class TestCLVTracking:
    """CLVTracking 测试类"""

    @pytest.fixture
    def clv(self, tmp_path):
        """创建临时 CLV 追踪器"""
        db_path = tmp_path / "test_clv.db"
        return CLVTracking(str(db_path))

    def test_init(self, clv):
        """测试初始化"""
        assert clv.db_path.exists()

    def test_record_recommendation(self, clv):
        """测试记录推荐"""
        record_id = clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0
        )

        assert record_id > 0

    def test_record_recommendation_with_details(self, clv):
        """测试记录带详细信息的推荐"""
        record_id = clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0,
            bookmaker="William Hill",
            metadata={"confidence": 0.8}
        )

        assert record_id > 0

        record = clv.get_clv_record("match_001", "home_win")
        assert record is not None
        assert record["bookmaker"] == "William Hill"

    def test_update_closing_odds(self, clv):
        """测试更新封盘赔率"""
        clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0
        )

        result = clv.update_closing_odds(
            match_id="match_001",
            bet_type="home_win",
            closing_odds=1.8
        )

        assert result is True

        record = clv.get_clv_record("match_001", "home_win")
        assert record["closing_odds"] == 1.8
        assert record["clv_status"] != CLVStatus.PENDING.value

    def test_update_nonexistent_record(self, clv):
        """测试更新不存在的记录"""
        result = clv.update_closing_odds(
            match_id="nonexistent",
            bet_type="home_win",
            closing_odds=1.8
        )

        assert result is False

    def test_calculate_positive_clv(self, clv):
        """测试正向 CLV 计算"""
        clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0
        )

        clv.update_closing_odds(
            match_id="match_001",
            bet_type="home_win",
            closing_odds=1.8
        )

        record = clv.get_clv_record("match_001", "home_win")

        # CLV = (2.0 / 1.8 - 1) * 100 = 11.11%
        assert record["clv_value"] > 0
        assert record["clv_status"] == CLVStatus.POSITIVE.value
        assert abs(record["line_movement"] + 0.2) < 0.01

    def test_calculate_negative_clv(self, clv):
        """测试负向 CLV 计算"""
        clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0
        )

        clv.update_closing_odds(
            match_id="match_001",
            bet_type="home_win",
            closing_odds=2.5
        )

        record = clv.get_clv_record("match_001", "home_win")

        # CLV = (2.0 / 2.5 - 1) * 100 = -20%
        assert record["clv_value"] < 0
        assert record["clv_status"] == CLVStatus.NEGATIVE.value

    def test_calculate_neutral_clv(self, clv):
        """测试中性 CLV 计算"""
        clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0
        )

        clv.update_closing_odds(
            match_id="match_001",
            bet_type="home_win",
            closing_odds=2.01
        )

        record = clv.get_clv_record("match_001", "home_win")

        assert record["clv_status"] == CLVStatus.NEUTRAL.value

    def test_get_clv_record(self, clv):
        """测试获取 CLV 记录"""
        clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0
        )

        record = clv.get_clv_record("match_001", "home_win")

        assert record is not None
        assert record["match_id"] == "match_001"
        assert record["bet_type"] == "home_win"

    def test_get_all_clv_for_match(self, clv):
        """测试获取比赛所有 CLV 记录"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.record_recommendation("match_001", "draw", 3.5)
        clv.record_recommendation("match_001", "away_win", 3.0)

        records = clv.get_all_clv_for_match("match_001")

        assert len(records) == 3

    def test_get_pending_records(self, clv):
        """测试获取待处理记录"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.record_recommendation("match_002", "home_win", 2.5)

        # 更新一个
        clv.update_closing_odds("match_001", "home_win", 1.8)

        pending = clv.get_pending_records()

        assert len(pending) == 1
        assert pending[0]["match_id"] == "match_002"

    def test_get_clv_stats(self, clv):
        """测试获取 CLV 统计"""
        # 创建多条记录
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.update_closing_odds("match_001", "home_win", 1.8)

        clv.record_recommendation("match_002", "home_win", 2.5)
        clv.update_closing_odds("match_002", "home_win", 2.0)

        clv.record_recommendation("match_003", "home_win", 2.0)
        clv.update_closing_odds("match_003", "home_win", 2.5)

        stats = clv.get_clv_stats()

        assert stats["total_records"] == 3
        assert stats["positive_count"] == 2
        assert stats["negative_count"] == 1

    def test_get_best_clv_records(self, clv):
        """测试获取最佳 CLV 记录"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.update_closing_odds("match_001", "home_win", 1.5)

        clv.record_recommendation("match_002", "home_win", 2.0)
        clv.update_closing_odds("match_002", "home_win", 1.8)

        best = clv.get_best_clv_records()

        assert len(best) == 2
        assert best[0]["clv_value"] > best[1]["clv_value"]

    def test_get_clv_by_bet_type(self, clv):
        """测试按投注类型获取统计"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.update_closing_odds("match_001", "home_win", 1.8)

        clv.record_recommendation("match_002", "draw", 3.5)
        clv.update_closing_odds("match_002", "draw", 3.0)

        by_type = clv.get_clv_by_bet_type()

        assert "home_win" in by_type
        assert "draw" in by_type

    def test_calculate_clv_summary(self, clv):
        """测试计算 CLV 汇总"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.update_closing_odds("match_001", "home_win", 1.8)

        clv.record_recommendation("match_001", "draw", 3.5)
        clv.update_closing_odds("match_001", "draw", 3.0)

        summary = clv.calculate_clv_summary("match_001")

        assert summary["completed_count"] == 2
        assert summary["average_clv"] > 0

    def test_calculate_clv_summary_pending(self, clv):
        """测试计算待处理的 CLV 汇总"""
        clv.record_recommendation("match_001", "home_win", 2.0)

        summary = clv.calculate_clv_summary("match_001")

        assert summary["status"] == "pending"

    def test_delete_old_records(self, clv):
        """测试删除旧记录"""
        clv.record_recommendation("match_001", "home_win", 2.0)

        deleted = clv.delete_old_records(days_old=0)

        assert deleted >= 1

    def test_export_clv_report(self, clv):
        """测试导出 CLV 报告"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.update_closing_odds("match_001", "home_win", 1.8)

        report = clv.export_clv_report()

        assert "statistics" in report
        assert "by_bet_type" in report
        assert "best_records" in report

    def test_export_clv_report_with_date_range(self, clv):
        """测试带日期范围的 CLV 报告"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.update_closing_odds("match_001", "home_win", 1.8)

        start_date = datetime.now() - timedelta(days=1)
        end_date = datetime.now() + timedelta(days=1)

        report = clv.export_clv_report(start_date, end_date)

        assert "period" in report
        assert report["period"]["start"] is not None

    def test_nonexistent_clv_record(self, clv):
        """测试获取不存在的 CLV 记录"""
        record = clv.get_clv_record("nonexistent", "home_win")
        assert record is None

    def test_multiple_bet_types_same_match(self, clv):
        """测试同场比赛多种投注类型"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.record_recommendation("match_001", "draw", 3.5)
        clv.record_recommendation("match_001", "away_win", 4.0)

        clv.update_closing_odds("match_001", "home_win", 1.8)
        clv.update_closing_odds("match_001", "draw", 3.2)
        clv.update_closing_odds("match_001", "away_win", 3.5)

        all_records = clv.get_all_clv_for_match("match_001")

        assert len(all_records) == 3

        summary = clv.calculate_clv_summary("match_001")
        assert summary["positive_count"] == 3

    def test_unicode_match_id(self, clv):
        """测试中文比赛ID"""
        clv.record_recommendation(
            match_id="英超_阿森纳_切尔西",
            bet_type="home_win",
            odds=2.0
        )

        record = clv.get_clv_record("英超_阿森纳_切尔西", "home_win")

        assert record is not None

    def test_clv_status_enum(self):
        """测试 CLV 状态枚举"""
        assert CLVStatus.POSITIVE.value == "positive"
        assert CLVStatus.NEGATIVE.value == "negative"
        assert CLVStatus.NEUTRAL.value == "neutral"
        assert CLVStatus.PENDING.value == "pending"

    def test_line_movement_calculation(self, clv):
        """测试盘口变化计算"""
        clv.record_recommendation("match_001", "home_win", 2.0)
        clv.update_closing_odds("match_001", "home_win", 1.8)

        record = clv.get_clv_record("match_001", "home_win")

        assert abs(record["line_movement"] + 0.2) < 0.01

    def test_clv_with_metadata(self, clv):
        """测试带元数据的 CLV"""
        metadata = {
            "confidence": 0.85,
            "ev": 0.08,
            "source": "model_v1"
        }

        clv.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            odds=2.0,
            metadata=metadata
        )

        record = clv.get_clv_record("match_001", "home_win")

        assert record["metadata"] is not None
