"""
实时赔率采集器测试
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.live.realtime_odds_collector import (
    RealtimeOddsCollector,
    OddsType
)


class TestRealtimeOddsCollector:
    """RealtimeOddsCollector 测试类"""

    @pytest.fixture
    def collector(self, tmp_path):
        """创建临时采集器"""
        db_path = tmp_path / "test_odds.db"
        snapshot_db = tmp_path / "test_snapshot.db"
        return RealtimeOddsCollector(str(db_path), str(snapshot_db))

    def test_init(self, collector):
        """测试初始化"""
        assert collector.db_path.exists()
        assert collector.snapshot_manager is not None
        assert collector.scheduler is not None

    def test_register_match(self, collector):
        """测试注册比赛"""
        match_time = datetime.now() + timedelta(hours=5)

        result = collector.register_match(
            match_id="match_001",
            home_team="Arsenal",
            away_team="Chelsea",
            league="Premier League",
            match_time=match_time
        )

        assert result is True

    def test_get_upcoming_matches(self, collector):
        """测试获取即将开赛比赛"""
        # 注册多场比赛
        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            datetime.now() + timedelta(hours=3)
        )
        collector.register_match(
            "match_002",
            "Liverpool",
            "Man City",
            "Premier League",
            datetime.now() + timedelta(hours=10)
        )
        collector.register_match(
            "match_003",
            "Barcelona",
            "Real Madrid",
            "La Liga",
            datetime.now() + timedelta(hours=30)
        )

        # 获取未来24小时内的比赛
        matches = collector.get_upcoming_matches(hours_ahead=24)

        assert len(matches) == 2

    def test_get_upcoming_matches_by_league(self, collector):
        """测试按联赛过滤比赛"""
        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            datetime.now() + timedelta(hours=3)
        )
        collector.register_match(
            "match_002",
            "Barcelona",
            "Real Madrid",
            "La Liga",
            datetime.now() + timedelta(hours=5)
        )

        matches = collector.get_upcoming_matches(
            hours_ahead=24,
            league="Premier League"
        )

        assert len(matches) == 1
        assert matches[0]["league"] == "Premier League"

    def test_collect_match_odds(self, collector):
        """测试采集比赛赔率"""
        match_time = datetime.now() + timedelta(hours=1)

        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            match_time
        )

        # 采集所有赔率类型
        results = collector.collect_match_odds(
            "match_001",
            match_start_time=match_time
        )

        # 应该有 3 个赔率类型 * 7 个时间窗口 = 21 个结果
        assert len(results) == 21

    def test_collect_specific_odds_types(self, collector):
        """测试采集特定赔率类型"""
        match_time = datetime.now() + timedelta(hours=1)

        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            match_time
        )

        # 只采集欧赔
        results = collector.collect_match_odds(
            "match_001",
            odds_types=[OddsType.EUROPEAN],
            match_start_time=match_time
        )

        # 1 个赔率类型 * 7 个时间窗口 = 7 个结果
        assert len(results) == 7

    def test_get_latest_odds(self, collector):
        """测试获取最新赔率"""
        match_time = datetime.now() + timedelta(hours=1)

        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            match_time
        )

        collector.collect_match_odds(
            "match_001",
            odds_types=[OddsType.EUROPEAN],
            match_start_time=match_time
        )

        latest = collector.get_latest_odds("match_001", OddsType.EUROPEAN)

        assert latest is not None
        assert "match_id" in latest

    def test_get_odds_time_series(self, collector):
        """测试获取赔率时间序列"""
        match_time = datetime.now() + timedelta(hours=25)

        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            match_time
        )

        # 采集所有窗口
        collector.collect_all_window_snapshots("match_001", match_time)

        # 获取时间序列
        import pandas as pd
        df = collector.get_odds_time_series("match_001", OddsType.EUROPEAN)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 7

    def test_collect_all_window_snapshots(self, collector):
        """测试按所有窗口采集完整快照"""
        match_time = datetime.now() + timedelta(hours=25)

        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            match_time
        )

        results = collector.collect_all_window_snapshots("match_001", match_time)

        # 3 个赔率类型
        assert len(results) == 3
        assert OddsType.EUROPEAN in results
        assert OddsType.ASIAN in results
        assert OddsType.OVER_UNDER in results

        # 每个类型有 7 个窗口
        for odds_type in results:
            assert len(results[odds_type]) == 7

    def test_update_match_status(self, collector):
        """测试更新比赛状态"""
        match_time = datetime.now() + timedelta(hours=1)

        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            match_time
        )

        # 更新为进行中
        collector.update_match_status("match_001", "live")

        matches = collector.get_upcoming_matches(hours_ahead=24)
        assert len(matches) == 0  # 状态变更后不再返回

    def test_update_match_score(self, collector):
        """测试更新比赛比分"""
        match_time = datetime.now() + timedelta(hours=1)

        collector.register_match(
            "match_001",
            "Arsenal",
            "Chelsea",
            "Premier League",
            match_time
        )

        # 更新比分
        collector.update_match_status("match_001", "finished", home_score=2, away_score=1)

        # 验证更新
        import sqlite3
        with sqlite3.connect(collector.db_path) as conn:
            cursor = conn.execute(
                "SELECT status, home_score, away_score FROM matches WHERE match_id = ?",
                ("match_001",)
            )
            row = cursor.fetchone()

        assert row[0] == "finished"
        assert row[1] == 2
        assert row[2] == 1

    def test_get_collection_status(self, collector):
        """测试获取采集器状态"""
        status = collector.get_collection_status()

        assert status["running"] is False
        assert OddsType.EUROPEAN in status["registered_collectors"]
        assert OddsType.ASIAN in status["registered_collectors"]
        assert OddsType.OVER_UNDER in status["registered_collectors"]
        assert len(status["available_windows"]) == 7

    def test_cleanup_old_matches(self, collector):
        """测试清理旧比赛"""
        # 注册一场过去的比赛
        collector.register_match(
            "match_old",
            "Team A",
            "Team B",
            "League",
            datetime.now() - timedelta(days=10)
        )

        # 注册一场未来的比赛
        collector.register_match(
            "match_new",
            "Team C",
            "Team D",
            "League",
            datetime.now() + timedelta(days=1)
        )

        # 清理 7 天前的比赛
        deleted = collector.cleanup_old_matches(days_old=7)

        assert deleted >= 1

    def test_nonexistent_match_time(self, collector):
        """测试不存在的比赛时间"""
        result = collector._get_match_time("nonexistent_match")
        assert result is None

    def test_multiple_matches_collection(self, collector):
        """测试多场比赛同时采集"""
        match_time = datetime.now() + timedelta(hours=25)

        for i in range(5):
            collector.register_match(
                f"match_{i:03d}",
                f"Home {i}",
                f"Away {i}",
                "League",
                match_time
            )

        # 批量采集
        for i in range(5):
            results = collector.collect_all_window_snapshots(
                f"match_{i:03d}",
                match_time
            )
            assert len(results) == 3

    def test_odds_type_constants(self):
        """测试赔率类型常量"""
        assert OddsType.EUROPEAN == "european"
        assert OddsType.ASIAN == "asian"
        assert OddsType.OVER_UNDER == "over_under"

    def test_unicode_team_names(self, collector):
        """测试中文队名"""
        match_time = datetime.now() + timedelta(hours=5)

        result = collector.register_match(
            match_id="中文比赛001",
            home_team="阿森纳",
            away_team="切尔西",
            league="英超",
            match_time=match_time
        )

        assert result is True

        matches = collector.get_upcoming_matches(hours_ahead=24)
        assert len(matches) == 1
        assert matches[0]["home_team"] == "阿森纳"
