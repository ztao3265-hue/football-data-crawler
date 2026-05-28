"""
预测快照存储 — 记录预测结果、盘口、EV、推荐等级的历史变化
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from enum import Enum

from backend.live.live_prediction_engine import PredictionResult, RecommendationLevel


class ChangeType(Enum):
    """变化类型"""
    PREDICTION = "prediction"
    ODDS = "odds"
    EV = "ev"
    RECOMMENDATION = "recommendation"


class PredictionSnapshotStorage:
    """
    预测快照存储

    功能：
    - 保存每次预测结果
    - 记录盘口变化
    - 记录EV变化
    - 记录推荐等级变化
    - 支持历史回溯
    """

    def __init__(self, db_path: str = None):
        """
        初始化存储

        Args:
            db_path: 数据库路径
        """
        if db_path is None:
            from config.paths import DB_PREDICTION_SNAPSHOTS
            db_path = str(DB_PREDICTION_SNAPSHOTS)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.db_path) as conn:
            # 预测快照表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prediction_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    prediction_data TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 变化记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    change_time TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    change_magnitude REAL,
                    metadata TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_snapshot_match_time
                ON prediction_snapshots(match_id, snapshot_time)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_changes_match_type
                ON changes(match_id, change_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_changes_time
                ON changes(change_time)
            """)

    def save_prediction_snapshot(
        self,
        prediction: PredictionResult,
        odds_snapshot: Optional[dict[str, Any]] = None
    ) -> int:
        """
        保存预测快照

        Args:
            prediction: 预测结果
            odds_snapshot: 赔率快照

        Returns:
            快照ID
        """
        snapshot_data = prediction.to_dict()
        if odds_snapshot:
            snapshot_data["odds_snapshot"] = odds_snapshot

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO prediction_snapshots (match_id, snapshot_time, prediction_data)
                VALUES (?, ?, ?)
                """,
                (
                    prediction.match_id,
                    prediction.prediction_time.isoformat(),
                    json.dumps(snapshot_data, ensure_ascii=False)
                )
            )
            return cursor.lastrowid

    def record_odds_change(
        self,
        match_id: str,
        old_odds: dict[str, Any],
        new_odds: dict[str, Any],
        change_time: Optional[datetime] = None
    ) -> int:
        """
        记录盘口变化

        Args:
            match_id: 比赛ID
            old_odds: 旧盘口
            new_odds: 新盘口
            change_time: 变化时间

        Returns:
            记录ID
        """
        if change_time is None:
            change_time = datetime.now()

        # 计算变化幅度
        change_magnitude = self._calculate_odds_change_magnitude(old_odds, new_odds)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO changes
                (match_id, change_type, change_time, old_value, new_value, change_magnitude)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    ChangeType.ODDS.value,
                    change_time.isoformat(),
                    json.dumps(old_odds, ensure_ascii=False),
                    json.dumps(new_odds, ensure_ascii=False),
                    change_magnitude
                )
            )
            return cursor.lastrowid

    def record_ev_change(
        self,
        match_id: str,
        old_ev: dict[str, float],
        new_ev: dict[str, float],
        change_time: Optional[datetime] = None
    ) -> int:
        """
        记录EV变化

        Args:
            match_id: 比赛ID
            old_ev: 旧EV
            new_ev: 新EV
            change_time: 变化时间

        Returns:
            记录ID
        """
        if change_time is None:
            change_time = datetime.now()

        # 计算变化幅度
        change_magnitude = self._calculate_ev_change_magnitude(old_ev, new_ev)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO changes
                (match_id, change_type, change_time, old_value, new_value, change_magnitude)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    ChangeType.EV.value,
                    change_time.isoformat(),
                    json.dumps(old_ev, ensure_ascii=False),
                    json.dumps(new_ev, ensure_ascii=False),
                    change_magnitude
                )
            )
            return cursor.lastrowid

    def record_recommendation_change(
        self,
        match_id: str,
        old_level: str,
        new_level: str,
        change_time: Optional[datetime] = None,
        metadata: Optional[dict[str, Any]] = None
    ) -> int:
        """
        记录推荐等级变化

        Args:
            match_id: 比赛ID
            old_level: 旧等级
            new_level: 新等级
            change_time: 变化时间
            metadata: 附加信息

        Returns:
            记录ID
        """
        if change_time is None:
            change_time = datetime.now()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO changes
                (match_id, change_type, change_time, old_value, new_value, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    ChangeType.RECOMMENDATION.value,
                    change_time.isoformat(),
                    old_level,
                    new_level,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None
                )
            )
            return cursor.lastrowid

    def _calculate_odds_change_magnitude(
        self,
        old_odds: dict[str, Any],
        new_odds: dict[str, Any]
    ) -> float:
        """计算盘口变化幅度"""
        total_change = 0.0
        count = 0

        for key in ["home_win", "draw", "away_win"]:
            if key in old_odds and key in new_odds:
                old_val = old_odds[key]
                new_val = new_odds[key]
                if old_val > 0:
                    total_change += abs(new_val - old_val) / old_val
                    count += 1

        return total_change / count if count > 0 else 0.0

    def _calculate_ev_change_magnitude(
        self,
        old_ev: dict[str, float],
        new_ev: dict[str, float]
    ) -> float:
        """计算EV变化幅度"""
        total_change = 0.0
        count = 0

        for key in old_ev:
            if key in new_ev:
                total_change += abs(new_ev[key] - old_ev[key])
                count += 1

        return total_change / count if count > 0 else 0.0

    def get_prediction_snapshot(
        self,
        match_id: str,
        at_time: Optional[datetime] = None
    ) -> Optional[dict[str, Any]]:
        """
        获取预测快照

        Args:
            match_id: 比赛ID
            at_time: 时间点（默认最新）

        Returns:
            快照数据
        """
        with sqlite3.connect(self.db_path) as conn:
            if at_time is None:
                cursor = conn.execute(
                    """
                    SELECT prediction_data FROM prediction_snapshots
                    WHERE match_id = ?
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                    """,
                    (match_id,)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT prediction_data FROM prediction_snapshots
                    WHERE match_id = ? AND snapshot_time <= ?
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                    """,
                    (match_id, at_time.isoformat())
                )

            row = cursor.fetchone()
            return json.loads(row[0]) if row else None

    def get_change_history(
        self,
        match_id: str,
        change_type: Optional[str] = None,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        获取变化历史

        Args:
            match_id: 比赛ID
            change_type: 变化类型过滤
            limit: 返回数量限制

        Returns:
            变化记录列表
        """
        with sqlite3.connect(self.db_path) as conn:
            if change_type:
                cursor = conn.execute(
                    """
                    SELECT change_type, change_time, old_value, new_value,
                           change_magnitude, metadata
                    FROM changes
                    WHERE match_id = ? AND change_type = ?
                    ORDER BY change_time DESC
                    LIMIT ?
                    """,
                    (match_id, change_type, limit)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT change_type, change_time, old_value, new_value,
                           change_magnitude, metadata
                    FROM changes
                    WHERE match_id = ?
                    ORDER BY change_time DESC
                    LIMIT ?
                    """,
                    (match_id, limit)
                )

            rows = cursor.fetchall()

        results = []
        for row in rows:
            change_type = row[0]
            old_value = row[2]
            new_value = row[3]

            # 根据变化类型决定如何解析值
            if change_type == ChangeType.RECOMMENDATION.value:
                # 推荐等级是字符串，不需要 JSON 解析
                parsed_old = old_value
                parsed_new = new_value
            else:
                # 其他类型是 JSON 格式
                try:
                    parsed_old = json.loads(old_value) if old_value else None
                except json.JSONDecodeError:
                    parsed_old = old_value
                try:
                    parsed_new = json.loads(new_value) if new_value else None
                except json.JSONDecodeError:
                    parsed_new = new_value

            results.append({
                "change_type": change_type,
                "change_time": row[1],
                "old_value": parsed_old,
                "new_value": parsed_new,
                "change_magnitude": row[4],
                "metadata": json.loads(row[5]) if row[5] else None
            })

        return results

    def get_all_changes_for_match(self, match_id: str) -> dict[str, list]:
        """
        获取比赛的所有变化记录

        Args:
            match_id: 比赛ID

        Returns:
            按类型分组的变化记录
        """
        changes = self.get_change_history(match_id, limit=1000)

        grouped = {
            ChangeType.ODDS.value: [],
            ChangeType.EV.value: [],
            ChangeType.RECOMMENDATION.value: [],
            ChangeType.PREDICTION.value: []
        }

        for change in changes:
            change_type = change["change_type"]
            if change_type in grouped:
                grouped[change_type].append(change)

        return grouped

    def detect_significant_changes(
        self,
        match_id: str,
        odds_threshold: float = 0.05,
        ev_threshold: float = 0.02
    ) -> list[dict[str, Any]]:
        """
        检测显著变化

        Args:
            match_id: 比赛ID
            odds_threshold: 盘口变化阈值
            ev_threshold: EV变化阈值

        Returns:
            显著变化列表
        """
        significant = []
        changes = self.get_change_history(match_id)

        for change in changes:
            magnitude = change.get("change_magnitude", 0)
            change_type = change["change_type"]

            if change_type == ChangeType.ODDS.value and magnitude >= odds_threshold:
                significant.append(change)
            elif change_type == ChangeType.EV.value and magnitude >= ev_threshold:
                significant.append(change)
            elif change_type == ChangeType.RECOMMENDATION.value:
                significant.append(change)

        return significant

    def get_change_summary(self, match_id: str) -> dict[str, Any]:
        """
        获取变化摘要

        Args:
            match_id: 比赛ID

        Returns:
            变化摘要统计
        """
        changes = self.get_change_history(match_id)

        summary = {
            "total_changes": len(changes),
            "by_type": {},
            "max_odds_change": 0.0,
            "max_ev_change": 0.0,
            "recommendation_changes": 0
        }

        for change in changes:
            change_type = change["change_type"]
            if change_type not in summary["by_type"]:
                summary["by_type"][change_type] = 0
            summary["by_type"][change_type] += 1

            magnitude = change.get("change_magnitude", 0)

            if change_type == ChangeType.ODDS.value:
                summary["max_odds_change"] = max(summary["max_odds_change"], magnitude)
            elif change_type == ChangeType.EV.value:
                summary["max_ev_change"] = max(summary["max_ev_change"], magnitude)
            elif change_type == ChangeType.RECOMMENDATION.value:
                summary["recommendation_changes"] += 1

        return summary

    def compare_two_snapshots(
        self,
        match_id: str,
        time1: datetime,
        time2: datetime
    ) -> dict[str, Any]:
        """
        比较两个时间点的快照

        Args:
            match_id: 比赛ID
            time1: 第一个时间点
            time2: 第二个时间点

        Returns:
            对比结果
        """
        snapshot1 = self.get_prediction_snapshot(match_id, time1)
        snapshot2 = self.get_prediction_snapshot(match_id, time2)

        if snapshot1 is None or snapshot2 is None:
            return {"error": "快照不存在"}

        return {
            "probability_change": self._diff_dict(
                snapshot1.get("predicted_probability", {}),
                snapshot2.get("predicted_probability", {})
            ),
            "ev_change": self._diff_dict(
                snapshot1.get("expected_value", {}),
                snapshot2.get("expected_value", {})
            ),
            "confidence_change": (
                snapshot2.get("confidence", 0) - snapshot1.get("confidence", 0)
            ),
            "recommendation_change": {
                "old": snapshot1.get("recommendation_level"),
                "new": snapshot2.get("recommendation_level")
            }
        }

    def _diff_dict(self, dict1: dict, dict2: dict) -> dict[str, Any]:
        """计算字典差异"""
        diff = {}
        all_keys = set(dict1.keys()) | set(dict2.keys())

        for key in all_keys:
            old_val = dict1.get(key, 0)
            new_val = dict2.get(key, 0)
            diff[key] = {
                "old": old_val,
                "new": new_val,
                "change": new_val - old_val
            }

        return diff

    def cleanup_old_snapshots(self, days_old: int = 30) -> int:
        """
        清理旧快照

        Args:
            days_old: 保留最近多少天

        Returns:
            删除的记录数
        """
        cutoff = datetime.now() - timedelta(days=days_old)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM prediction_snapshots WHERE snapshot_time < ?",
                (cutoff.isoformat(),)
            )
            snapshots_deleted = cursor.rowcount

            cursor = conn.execute(
                "DELETE FROM changes WHERE change_time < ?",
                (cutoff.isoformat(),)
            )
            changes_deleted = cursor.rowcount

            return snapshots_deleted + changes_deleted

    def export_match_history(self, match_id: str) -> dict[str, Any]:
        """
        导出比赛完整历史

        Args:
            match_id: 比赛ID

        Returns:
            完整历史数据
        """
        snapshots = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT snapshot_time, prediction_data
                FROM prediction_snapshots
                WHERE match_id = ?
                ORDER BY snapshot_time ASC
                """,
                (match_id,)
            )
            for row in cursor.fetchall():
                snapshots.append({
                    "time": row[0],
                    "data": json.loads(row[1])
                })

        changes = self.get_all_changes_for_match(match_id)

        return {
            "match_id": match_id,
            "snapshots": snapshots,
            "changes": changes,
            "summary": self.get_change_summary(match_id)
        }
