"""
时间序列快照系统 — 记录数据随时间变化的完整历史
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import pandas as pd


class TimeSeriesSnapshot:
    """
    时间序列快照管理器

    功能：
    - 记录数据快照（支持任意时间点恢复）
    - 时间序列查询（获取某字段的历史变化）
    - 快照差异对比（比较两个时间点的数据变化）
    """

    def __init__(self, db_path: str = "data/time_series.db"):
        """
        初始化快照管理器

        Args:
            db_path: SQLite 数据库路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_time TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_snapshot_time
                ON snapshots(snapshot_time)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_entity
                ON snapshots(entity_type, entity_id)
            """)

    def save_snapshot(
        self,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
        snapshot_time: Optional[datetime] = None
    ) -> int:
        """
        保存数据快照

        Args:
            entity_type: 实体类型（如 "match", "team", "odds"）
            entity_id: 实体唯一标识
            data: 快照数据
            snapshot_time: 快照时间（默认当前时间）

        Returns:
            快照记录ID
        """
        if snapshot_time is None:
            snapshot_time = datetime.now()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO snapshots (snapshot_time, entity_type, entity_id, data)
                VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot_time.isoformat(),
                    entity_type,
                    entity_id,
                    json.dumps(data, ensure_ascii=False)
                )
            )
            return cursor.lastrowid

    def get_snapshot(
        self,
        entity_type: str,
        entity_id: str,
        at_time: Optional[datetime] = None
    ) -> Optional[dict[str, Any]]:
        """
        获取指定时间点的快照

        Args:
            entity_type: 实体类型
            entity_id: 实体ID
            at_time: 目标时间（默认最新）

        Returns:
            快照数据，不存在则返回 None
        """
        with sqlite3.connect(self.db_path) as conn:
            if at_time is None:
                cursor = conn.execute(
                    """
                    SELECT data FROM snapshots
                    WHERE entity_type = ? AND entity_id = ?
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                    """,
                    (entity_type, entity_id)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT data FROM snapshots
                    WHERE entity_type = ? AND entity_id = ?
                      AND snapshot_time <= ?
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                    """,
                    (entity_type, entity_id, at_time.isoformat())
                )

            row = cursor.fetchone()
            return json.loads(row[0]) if row else None

    def get_time_series(
        self,
        entity_type: str,
        entity_id: str,
        field_path: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> pd.DataFrame:
        """
        获取时间序列数据

        Args:
            entity_type: 实体类型
            entity_id: 实体ID
            field_path: 字段路径（如 "odds.home"，为空则返回全部）
            start_time: 起始时间
            end_time: 结束时间

        Returns:
            时间序列 DataFrame（索引为时间）
        """
        with sqlite3.connect(self.db_path) as conn:
            sql = """
                SELECT snapshot_time, data FROM snapshots
                WHERE entity_type = ? AND entity_id = ?
            """
            params = [entity_type, entity_id]

            if start_time:
                sql += " AND snapshot_time >= ?"
                params.append(start_time.isoformat())
            if end_time:
                sql += " AND snapshot_time <= ?"
                params.append(end_time.isoformat())

            sql += " ORDER BY snapshot_time ASC"

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

        if not rows:
            return pd.DataFrame()

        records = []
        for time_str, data_json in rows:
            data = json.loads(data_json)
            if field_path:
                # 按路径提取字段值
                value = self._extract_field(data, field_path)
                records.append({"time": time_str, "value": value})
            else:
                data["time"] = time_str
                records.append(data)

        df = pd.DataFrame(records)
        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        return df

    def _extract_field(self, data: dict, field_path: str) -> Any:
        """从嵌套字典中提取字段值"""
        keys = field_path.split(".")
        value = data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return None
        return value

    def compare_snapshots(
        self,
        entity_type: str,
        entity_id: str,
        time1: datetime,
        time2: datetime
    ) -> dict[str, Any]:
        """
        比较两个时间点的快照差异

        Args:
            entity_type: 实体类型
            entity_id: 实体ID
            time1: 第一个时间点
            time2: 第二个时间点

        Returns:
            差异字典：{"added": {}, "removed": {}, "changed": {}}
        """
        snapshot1 = self.get_snapshot(entity_type, entity_id, time1)
        snapshot2 = self.get_snapshot(entity_type, entity_id, time2)

        if snapshot1 is None or snapshot2 is None:
            return {"error": "快照不存在"}

        return self._diff_dicts(snapshot1, snapshot2)

    def _diff_dicts(
        self,
        dict1: dict,
        dict2: dict,
        path: str = ""
    ) -> dict[str, Any]:
        """递归比较两个字典的差异"""
        diff = {"added": {}, "removed": {}, "changed": {}}

        all_keys = set(dict1.keys()) | set(dict2.keys())

        for key in all_keys:
            current_path = f"{path}.{key}" if path else key

            if key not in dict1:
                diff["added"][current_path] = dict2[key]
            elif key not in dict2:
                diff["removed"][current_path] = dict1[key]
            elif dict1[key] != dict2[key]:
                if isinstance(dict1[key], dict) and isinstance(dict2[key], dict):
                    nested_diff = self._diff_dicts(dict1[key], dict2[key], current_path)
                    diff["added"].update(nested_diff["added"])
                    diff["removed"].update(nested_diff["removed"])
                    diff["changed"].update(nested_diff["changed"])
                else:
                    diff["changed"][current_path] = {
                        "old": dict1[key],
                        "new": dict2[key]
                    }

        return diff

    def list_snapshots(
        self,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        列出快照记录

        Args:
            entity_type: 按实体类型过滤
            entity_id: 按实体ID过滤
            limit: 返回数量限制

        Returns:
            快照元数据列表
        """
        with sqlite3.connect(self.db_path) as conn:
            sql = "SELECT id, snapshot_time, entity_type, entity_id FROM snapshots"
            conditions = []
            params = []

            if entity_type:
                conditions.append("entity_type = ?")
                params.append(entity_type)
            if entity_id:
                conditions.append("entity_id = ?")
                params.append(entity_id)

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

            sql += " ORDER BY snapshot_time DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

        return [
            {
                "id": row[0],
                "snapshot_time": row[1],
                "entity_type": row[2],
                "entity_id": row[3]
            }
            for row in rows
        ]

    def delete_old_snapshots(
        self,
        before_time: datetime,
        entity_type: Optional[str] = None
    ) -> int:
        """
        删除旧快照

        Args:
            before_time: 删除此时间之前的快照
            entity_type: 仅删除指定类型（可选）

        Returns:
            删除的记录数
        """
        with sqlite3.connect(self.db_path) as conn:
            sql = "DELETE FROM snapshots WHERE snapshot_time < ?"
            params = [before_time.isoformat()]

            if entity_type:
                sql += " AND entity_type = ?"
                params.append(entity_type)

            cursor = conn.execute(sql, params)
            return cursor.rowcount
