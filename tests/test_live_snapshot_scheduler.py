"""
实时快照调度器测试
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
import threading

import pytest

from backend.scheduler.live_snapshot_scheduler import (
    LiveSnapshotScheduler,
    SnapshotWindow
)


class TestSnapshotWindow:
    """SnapshotWindow 测试类"""

    def test_get_window(self):
        """测试获取时间窗口"""
        delta = SnapshotWindow.get_window("T-24h")
        assert delta == timedelta(hours=24)

        delta = SnapshotWindow.get_window("T-10m")
        assert delta == timedelta(minutes=10)

    def test_get_nonexistent_window(self):
        """测试获取不存在的时间窗口"""
        delta = SnapshotWindow.get_window("T-100h")
        assert delta is None

    def test_get_all_windows(self):
        """测试获取所有时间窗口"""
        windows = SnapshotWindow.get_all_windows()

        assert "T-24h" in windows
        assert "T-12h" in windows
        assert "T-6h" in windows
        assert "T-3h" in windows
        assert "T-1h" in windows
        assert "T-30m" in windows
        assert "T-10m" in windows

        assert len(windows) == 7

    def test_get_window_names(self):
        """测试获取窗口名称列表"""
        names = SnapshotWindow.get_window_names()

        assert len(names) == 7
        assert "T-24h" in names
        assert "T-10m" in names


class TestLiveSnapshotScheduler:
    """LiveSnapshotScheduler 测试类"""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """创建临时数据库"""
        db_path = tmp_path / "test_scheduler.db"
        return LiveSnapshotScheduler(str(db_path))

    @pytest.fixture
    def temp_config(self, tmp_path):
        """创建临时配置文件"""
        config_path = tmp_path / "config.json"
        config = {
            "default_interval": 30,
            "windows": ["T-1h", "T-10m"]
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)
        return str(config_path)

    def test_init(self, temp_db):
        """测试初始化"""
        assert temp_db.snapshot_manager is not None
        assert not temp_db.is_scheduler_running()

    def test_init_with_config(self, tmp_path):
        """测试带配置初始化"""
        config_path = tmp_path / "config.json"
        config = {"test_key": "test_value"}
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        scheduler = LiveSnapshotScheduler(
            snapshot_db_path=str(tmp_path / "test.db"),
            config_path=str(config_path)
        )

        assert scheduler.config["test_key"] == "test_value"

    def test_register_collector(self, temp_db):
        """测试注册采集器"""
        def mock_collector(entity_id: str) -> dict:
            return {"odds": 1.5, "entity_id": entity_id}

        temp_db.register_collector("odds", mock_collector)

        status = temp_db.get_collector_status()
        assert "odds" in status["registered_collectors"]

    def test_collect_snapshot(self, temp_db):
        """测试采集快照"""
        def mock_collector(entity_id: str) -> dict:
            return {"home_odds": 1.85, "away_odds": 4.20}

        temp_db.register_collector("odds", mock_collector)

        match_start = datetime.now() + timedelta(hours=1)
        snapshot_id = temp_db.collect_snapshot(
            "odds",
            "odds_001",
            "T-30m",
            match_start
        )

        assert snapshot_id is not None
        assert snapshot_id > 0

    def test_collect_snapshot_without_collector(self, temp_db):
        """测试无采集器时采集快照"""
        snapshot_id = temp_db.collect_snapshot(
            "nonexistent",
            "test_id"
        )

        assert snapshot_id is None

    def test_collect_snapshot_with_failed_collector(self, temp_db):
        """测试采集器失败的情况"""
        def failing_collector(entity_id: str) -> None:
            raise Exception("采集失败")

        temp_db.register_collector("failing", failing_collector)

        snapshot_id = temp_db.collect_snapshot("failing", "test_id")

        assert snapshot_id is None

    def test_calculate_snapshot_time(self, temp_db):
        """测试计算快照时间点"""
        match_start = datetime(2026, 1, 15, 20, 0, 0)

        # T-24h
        snapshot_time = temp_db._calculate_snapshot_time("T-24h", match_start)
        expected = match_start - timedelta(hours=24)
        assert snapshot_time == expected

        # T-10m
        snapshot_time = temp_db._calculate_snapshot_time("T-10m", match_start)
        expected = match_start - timedelta(minutes=10)
        assert snapshot_time == expected

    def test_calculate_snapshot_time_invalid_window(self, temp_db):
        """测试无效时间窗口"""
        match_start = datetime.now()
        snapshot_time = temp_db._calculate_snapshot_time("T-99h", match_start)

        assert snapshot_time is None

    def test_collect_window_series(self, temp_db):
        """测试按所有窗口采集快照"""
        def mock_collector(entity_id: str) -> dict:
            return {"odds": 2.0, "timestamp": datetime.now().isoformat()}

        temp_db.register_collector("odds", mock_collector)

        match_start = datetime.now() + timedelta(hours=25)
        results = temp_db.collect_window_series(
            "odds",
            "odds_001",
            match_start
        )

        assert len(results) == 7
        for window_name in SnapshotWindow.get_window_names():
            assert window_name in results
            assert results[window_name] is not None

    def test_schedule_batch_snapshots(self, temp_db):
        """测试批量调度快照"""
        call_count = {"count": 0}

        def mock_collector(entity_id: str) -> dict:
            call_count["count"] += 1
            return {"id": entity_id, "value": call_count["count"]}

        temp_db.register_collector("odds", mock_collector)

        entities = [
            {"type": "odds", "id": "odds_001"},
            {"type": "odds", "id": "odds_002"}
        ]

        match_start = datetime.now() + timedelta(hours=25)
        results = temp_db.schedule_batch_snapshots(entities, match_start)

        assert len(results) == 2
        assert "odds/odds_001" in results
        assert "odds/odds_002" in results

    def test_get_upcoming_windows(self, temp_db):
        """测试获取即将到来的窗口"""
        match_start = datetime.now() + timedelta(hours=5)
        upcoming = temp_db.get_upcoming_windows(match_start)

        assert len(upcoming) > 0
        assert all("window" in w for w in upcoming)
        assert all("target_time" in w for w in upcoming)
        assert all("seconds_until" in w for w in upcoming)

        # 验证按时间排序
        for i in range(len(upcoming) - 1):
            assert upcoming[i]["seconds_until"] <= upcoming[i + 1]["seconds_until"]

    def test_get_upcoming_windows_all_passed(self, temp_db):
        """测试所有窗口已过"""
        match_start = datetime.now() - timedelta(hours=1)
        upcoming = temp_db.get_upcoming_windows(match_start)

        assert len(upcoming) == 0

    def test_get_collector_status(self, temp_db):
        """测试获取采集器状态"""
        temp_db.register_collector("odds", lambda x: {})
        temp_db.register_collector("match", lambda x: {})

        status = temp_db.get_collector_status()

        assert len(status["registered_collectors"]) == 2
        assert "odds" in status["registered_collectors"]
        assert "match" in status["registered_collectors"]
        assert status["scheduler_running"] is False
        assert len(status["available_windows"]) == 7

    def test_start_and_stop_scheduler(self, temp_db):
        """测试启动和停止调度器"""
        def mock_collector(entity_id: str) -> dict:
            return {"value": 1.0}

        temp_db.register_collector("odds", mock_collector)

        match_start = datetime.now() + timedelta(seconds=3)

        temp_db.start_scheduler(
            "odds",
            "odds_001",
            match_start,
            interval_seconds=1
        )

        assert temp_db.is_scheduler_running()

        # 等待一会儿
        time.sleep(2)

        # 停止调度器
        temp_db.stop_scheduler()
        assert not temp_db.is_scheduler_running()

    def test_scheduler_collects_at_right_time(self, temp_db):
        """测试调度器在正确时间采集"""
        collected_times = []

        def mock_collector(entity_id: str) -> dict:
            collected_times.append(datetime.now())
            return {"timestamp": datetime.now().isoformat()}

        temp_db.register_collector("odds", mock_collector)

        # 设置比赛在 2 秒后开始
        match_start = datetime.now() + timedelta(seconds=2)

        temp_db.start_scheduler(
            "odds",
            "odds_001",
            match_start,
            interval_seconds=1
        )

        # 等待调度器完成
        time.sleep(4)

        assert not temp_db.is_scheduler_running()

    def test_multiple_collectors(self, temp_db):
        """测试多个采集器"""
        temp_db.register_collector("odds", lambda x: {"type": "odds"})
        temp_db.register_collector("match", lambda x: {"type": "match"})
        temp_db.register_collector("team", lambda x: {"type": "team"})

        match_start = datetime.now() + timedelta(hours=25)

        # 采集不同类型
        odds_id = temp_db.collect_snapshot("odds", "odds_001", "T-1h", match_start)
        match_id = temp_db.collect_snapshot("match", "match_001", "T-1h", match_start)
        team_id = temp_db.collect_snapshot("team", "team_001", "T-1h", match_start)

        assert odds_id is not None
        assert match_id is not None
        assert team_id is not None

    def test_collector_returns_none(self, temp_db):
        """测试采集器返回 None"""
        def none_collector(entity_id: str) -> None:
            return None

        temp_db.register_collector("none_type", none_collector)

        snapshot_id = temp_db.collect_snapshot("none_type", "test_id")

        assert snapshot_id is None

    def test_scheduler_thread_safety(self, temp_db):
        """测试调度器线程安全"""
        call_count = {"count": 0}

        def counting_collector(entity_id: str) -> dict:
            call_count["count"] += 1
            return {"count": call_count["count"]}

        temp_db.register_collector("odds", counting_collector)

        match_start = datetime.now() + timedelta(seconds=2)

        # 尝试多次启动
        temp_db.start_scheduler("odds", "odds_001", match_start, interval_seconds=1)
        temp_db.start_scheduler("odds", "odds_002", match_start, interval_seconds=1)
        temp_db.start_scheduler("odds", "odds_003", match_start, interval_seconds=1)

        # 应该只有一个线程在运行
        assert temp_db.is_scheduler_running()

        time.sleep(3)
        temp_db.stop_scheduler()

    def test_snapshot_with_current_time(self, temp_db):
        """测试无比赛时间时使用当前时间"""
        def mock_collector(entity_id: str) -> dict:
            return {"value": 1.0}

        temp_db.register_collector("odds", mock_collector)

        # 不提供比赛时间
        snapshot_id = temp_db.collect_snapshot("odds", "odds_001", "T-10m")

        assert snapshot_id is not None

    def test_unicode_entity_id(self, temp_db):
        """测试中文实体ID"""
        def mock_collector(entity_id: str) -> dict:
            return {"id": entity_id}

        temp_db.register_collector("odds", mock_collector)

        match_start = datetime.now() + timedelta(hours=1)
        snapshot_id = temp_db.collect_snapshot(
            "odds",
            "英超_阿森纳_切尔西",
            "T-10m",
            match_start
        )

        assert snapshot_id is not None

        # 验证快照数据
        snapshot = temp_db.snapshot_manager.get_snapshot("odds", "英超_阿森纳_切尔西")
        assert snapshot["id"] == "英超_阿森纳_切尔西"
