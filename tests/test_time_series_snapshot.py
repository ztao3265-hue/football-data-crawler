"""
时间序列快照系统测试
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import pytest

from backend.data.time_series_snapshot import TimeSeriesSnapshot


class TestTimeSeriesSnapshot:
    """TimeSeriesSnapshot 测试类"""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """创建临时数据库"""
        db_path = tmp_path / "test_snapshot.db"
        return TimeSeriesSnapshot(str(db_path))

    def test_init_db(self, temp_db):
        """测试数据库初始化"""
        # 验证数据库文件创建
        assert Path(temp_db.db_path).exists()

    def test_save_and_get_snapshot(self, temp_db):
        """测试保存和获取快照"""
        entity_type = "match"
        entity_id = "match_001"
        data = {
            "home_team": " Arsenal",
            "away_team": "Chelsea",
            "score": {"home": 2, "away": 1}
        }

        # 保存快照
        snapshot_id = temp_db.save_snapshot(entity_type, entity_id, data)
        assert snapshot_id > 0

        # 获取最新快照
        result = temp_db.get_snapshot(entity_type, entity_id)
        assert result is not None
        assert result["home_team"] == " Arsenal"
        assert result["score"]["home"] == 2

    def test_get_snapshot_at_time(self, temp_db):
        """测试获取指定时间的快照"""
        entity_type = "odds"
        entity_id = "odds_001"

        now = datetime.now()
        time1 = now - timedelta(hours=2)
        time2 = now - timedelta(hours=1)

        # 保存两个时间点的快照
        temp_db.save_snapshot(entity_type, entity_id, {"value": 1.5}, time1)
        temp_db.save_snapshot(entity_type, entity_id, {"value": 2.0}, time2)

        # 查询时间点1的数据
        result = temp_db.get_snapshot(entity_type, entity_id, time1)
        assert result["value"] == 1.5

        # 查询时间点2的数据
        result = temp_db.get_snapshot(entity_type, entity_id, time2)
        assert result["value"] == 2.0

        # 查询最新数据
        result = temp_db.get_snapshot(entity_type, entity_id)
        assert result["value"] == 2.0

    def test_get_time_series(self, temp_db):
        """测试获取时间序列"""
        entity_type = "price"
        entity_id = "price_001"

        base_time = datetime.now()

        # 保存多个时间点的数据
        for i in range(5):
            temp_db.save_snapshot(
                entity_type,
                entity_id,
                {"price": 100 + i * 10},
                base_time + timedelta(hours=i)
            )

        # 获取时间序列
        df = temp_db.get_time_series(entity_type, entity_id, "price")

        assert len(df) == 5
        assert list(df["value"]) == [100, 110, 120, 130, 140]

    def test_get_time_series_with_time_range(self, temp_db):
        """测试带时间范围的时间序列查询"""
        entity_type = "stock"
        entity_id = "stock_001"

        base_time = datetime.now()

        # 保存10个时间点的数据
        for i in range(10):
            temp_db.save_snapshot(
                entity_type,
                entity_id,
                {"price": i * 5},
                base_time + timedelta(hours=i)
            )

        # 查询时间范围
        start = base_time + timedelta(hours=2)
        end = base_time + timedelta(hours=6)

        df = temp_db.get_time_series(
            entity_type,
            entity_id,
            "price",
            start_time=start,
            end_time=end
        )

        assert len(df) == 5  # 索引 2,3,4,5,6
        assert list(df["value"]) == [10, 15, 20, 25, 30]

    def test_compare_snapshots(self, temp_db):
        """测试快照对比"""
        entity_type = "team"
        entity_id = "team_001"

        now = datetime.now()
        time1 = now - timedelta(hours=1)
        time2 = now

        # 保存两个不同时间点的快照
        temp_db.save_snapshot(
            entity_type,
            entity_id,
            {
                "name": "Arsenal",
                "rank": 3,
                "stats": {"wins": 15, "draws": 5}
            },
            time1
        )

        temp_db.save_snapshot(
            entity_type,
            entity_id,
            {
                "name": "Arsenal",
                "rank": 2,
                "stats": {"wins": 17, "draws": 5, "losses": 2}
            },
            time2
        )

        # 比较差异
        diff = temp_db.compare_snapshots(entity_type, entity_id, time1, time2)

        assert "rank" in diff["changed"]
        assert diff["changed"]["rank"]["old"] == 3
        assert diff["changed"]["rank"]["new"] == 2

        # 检查嵌套字段变化
        assert "stats.wins" in diff["changed"]
        assert "stats.losses" in diff["added"]

    def test_list_snapshots(self, temp_db):
        """测试列出快照"""
        # 保存多个不同实体的快照
        temp_db.save_snapshot("match", "m1", {"data": 1})
        temp_db.save_snapshot("match", "m2", {"data": 2})
        temp_db.save_snapshot("team", "t1", {"data": 3})

        # 列出所有快照
        all_snapshots = temp_db.list_snapshots()
        assert len(all_snapshots) == 3

        # 按类型过滤
        match_snapshots = temp_db.list_snapshots(entity_type="match")
        assert len(match_snapshots) == 2

        # 按ID过滤
        specific = temp_db.list_snapshots(entity_id="t1")
        assert len(specific) == 1
        assert specific[0]["entity_type"] == "team"

    def test_delete_old_snapshots(self, temp_db):
        """测试删除旧快照"""
        entity_type = "temp"

        now = datetime.now()
        old_time = now - timedelta(days=30)
        new_time = now - timedelta(days=1)

        # 保存旧快照和新快照
        temp_db.save_snapshot(entity_type, "old", {"data": 1}, old_time)
        temp_db.save_snapshot(entity_type, "new", {"data": 2}, new_time)

        # 删除7天前的快照
        cutoff = now - timedelta(days=7)
        deleted = temp_db.delete_old_snapshots(cutoff)

        assert deleted == 1

        # 验证旧快照被删除
        old_snapshot = temp_db.get_snapshot(entity_type, "old")
        assert old_snapshot is None

        # 验证新快照保留
        new_snapshot = temp_db.get_snapshot(entity_type, "new")
        assert new_snapshot is not None

    def test_nonexistent_snapshot(self, temp_db):
        """测试获取不存在的快照"""
        result = temp_db.get_snapshot("nonexistent", "no_id")
        assert result is None

    def test_empty_time_series(self, temp_db):
        """测试空时间序列"""
        import pandas as pd

        df = temp_db.get_time_series("empty", "empty_id")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_nested_field_extraction(self, temp_db):
        """测试嵌套字段提取"""
        entity_type = "complex"
        entity_id = "complex_001"

        data = {
            "level1": {
                "level2": {
                    "level3": "deep_value"
                },
                "simple": "value"
            }
        }

        temp_db.save_snapshot(entity_type, entity_id, data)

        # 提取深层嵌套字段
        df = temp_db.get_time_series(entity_type, entity_id, "level1.level2.level3")
        assert df.iloc[0]["value"] == "deep_value"

        # 提取浅层字段
        df = temp_db.get_time_series(entity_type, entity_id, "level1.simple")
        assert df.iloc[0]["value"] == "value"

    def test_unicode_data(self, temp_db):
        """测试中文等Unicode数据"""
        entity_type = "chinese"
        entity_id = "中文ID"

        data = {
            "球队": "阿森纳",
            "联赛": "英超",
            "数据": {"进球": 50, "失球": 20}
        }

        temp_db.save_snapshot(entity_type, entity_id, data)

        result = temp_db.get_snapshot(entity_type, entity_id)
        assert result["球队"] == "阿森纳"
        assert result["数据"]["进球"] == 50

    def test_snapshot_with_none_values(self, temp_db):
        """测试包含None值的快照"""
        entity_type = "nullable"
        entity_id = "nullable_001"

        data = {
            "field1": None,
            "field2": "value",
            "field3": {"nested": None}
        }

        temp_db.save_snapshot(entity_type, entity_id, data)

        result = temp_db.get_snapshot(entity_type, entity_id)
        assert result["field1"] is None
        assert result["field3"]["nested"] is None
