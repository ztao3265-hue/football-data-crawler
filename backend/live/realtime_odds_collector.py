"""
实时赔率采集器 — 自动采集即将开赛比赛的赔率数据
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
import threading
import time

from backend.data.time_series_snapshot import TimeSeriesSnapshot
from backend.scheduler.live_snapshot_scheduler import (
    LiveSnapshotScheduler,
    SnapshotWindow
)


class OddsType:
    """赔率类型定义"""
    EUROPEAN = "european"      # 欧赔
    ASIAN = "asian"            # 亚盘
    OVER_UNDER = "over_under"  # 大小球


class RealtimeOddsCollector:
    """
    实时赔率采集器

    功能：
    - 自动采集即将开赛比赛
    - 采集欧赔/亚盘/大小球
    - 保存时间序列快照
    - 支持多数据源
    """

    def __init__(
        self,
        db_path: str = "data/live_odds.db",
        snapshot_db_path: str = "data/time_series.db"
    ):
        """
        初始化采集器

        Args:
            db_path: 赔率数据库路径
            snapshot_db_path: 快照数据库路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.snapshot_manager = TimeSeriesSnapshot(snapshot_db_path)
        self.scheduler = LiveSnapshotScheduler(snapshot_db_path)

        self._init_db()
        self._register_collectors()

        self._running = False
        self._collector_thread: Optional[threading.Thread] = None

    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            # 比赛表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT PRIMARY KEY,
                    home_team TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    league TEXT NOT NULL,
                    match_time TEXT NOT NULL,
                    status TEXT DEFAULT 'upcoming',
                    home_score INTEGER DEFAULT 0,
                    away_score INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 赔率表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS odds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    odds_type TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    data TEXT NOT NULL,
                    collected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (match_id) REFERENCES matches(match_id)
                )
            """)

            # 索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_match_time
                ON matches(match_time)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_odds_match
                ON odds(match_id, odds_type)
            """)

    def _register_collectors(self):
        """注册内部采集器"""
        self.scheduler.register_collector(
            OddsType.EUROPEAN,
            self._collect_european_odds
        )
        self.scheduler.register_collector(
            OddsType.ASIAN,
            self._collect_asian_odds
        )
        self.scheduler.register_collector(
            OddsType.OVER_UNDER,
            self._collect_over_under_odds
        )

    def _collect_european_odds(self, match_id: str) -> dict[str, Any]:
        """采集欧赔数据"""
        # 实际实现需要对接真实数据源
        # 这里返回模拟数据结构
        return {
            "match_id": match_id,
            "odds_type": OddsType.EUROPEAN,
            "bookmaker": "default",
            "home_win": 0.0,
            "draw": 0.0,
            "away_win": 0.0,
            "collected_at": datetime.now().isoformat()
        }

    def _collect_asian_odds(self, match_id: str) -> dict[str, Any]:
        """采集亚盘数据"""
        return {
            "match_id": match_id,
            "odds_type": OddsType.ASIAN,
            "bookmaker": "default",
            "handicap": 0.0,
            "home_odds": 0.0,
            "away_odds": 0.0,
            "collected_at": datetime.now().isoformat()
        }

    def _collect_over_under_odds(self, match_id: str) -> dict[str, Any]:
        """采集大小球数据"""
        return {
            "match_id": match_id,
            "odds_type": OddsType.OVER_UNDER,
            "bookmaker": "default",
            "line": 2.5,
            "over_odds": 0.0,
            "under_odds": 0.0,
            "collected_at": datetime.now().isoformat()
        }

    def register_match(
        self,
        match_id: str,
        home_team: str,
        away_team: str,
        league: str,
        match_time: datetime
    ) -> bool:
        """
        注册待采集比赛

        Args:
            match_id: 比赛唯一ID
            home_team: 主队名称
            away_team: 客队名称
            league: 联赛名称
            match_time: 比赛时间

        Returns:
            是否成功
        """
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO matches
                    (match_id, home_team, away_team, league, match_time)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (match_id, home_team, away_team, league, match_time.isoformat())
                )
                return True
            except Exception as e:
                print(f"注册比赛失败: {e}")
                return False

    def get_upcoming_matches(
        self,
        hours_ahead: int = 24,
        league: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """
        获取即将开赛的比赛

        Args:
            hours_ahead: 未来多少小时内的比赛
            league: 按联赛过滤

        Returns:
            比赛列表
        """
        now = datetime.now()
        end_time = now + timedelta(hours=hours_ahead)

        with sqlite3.connect(self.db_path) as conn:
            sql = """
                SELECT match_id, home_team, away_team, league, match_time, status
                FROM matches
                WHERE match_time >= ? AND match_time <= ? AND status = 'upcoming'
            """
            params = [now.isoformat(), end_time.isoformat()]

            if league:
                sql += " AND league = ?"
                params.append(league)

            sql += " ORDER BY match_time ASC"

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

        return [
            {
                "match_id": row[0],
                "home_team": row[1],
                "away_team": row[2],
                "league": row[3],
                "match_time": row[4],
                "status": row[5]
            }
            for row in rows
        ]

    def collect_match_odds(
        self,
        match_id: str,
        odds_types: Optional[list[str]] = None,
        match_start_time: Optional[datetime] = None
    ) -> dict[str, Optional[int]]:
        """
        采集比赛赔率快照

        Args:
            match_id: 比赛ID
            odds_types: 赔率类型列表（默认全部）
            match_start_time: 比赛开始时间

        Returns:
            各赔率类型的快照ID
        """
        if odds_types is None:
            odds_types = [OddsType.EUROPEAN, OddsType.ASIAN, OddsType.OVER_UNDER]

        if match_start_time is None:
            match_start_time = self._get_match_time(match_id)

        results = {}

        for odds_type in odds_types:
            # 按所有时间窗口采集
            for window_name in SnapshotWindow.get_window_names():
                snapshot_id = self.scheduler.collect_snapshot(
                    odds_type,
                    f"{match_id}_{odds_type}",
                    window_name,
                    match_start_time
                )

                key = f"{odds_type}_{window_name}"
                results[key] = snapshot_id

                # 同时保存到赔率表
                if snapshot_id:
                    self._save_odds_record(match_id, odds_type, snapshot_id)

        return results

    def _get_match_time(self, match_id: str) -> Optional[datetime]:
        """获取比赛时间"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT match_time FROM matches WHERE match_id = ?",
                (match_id,)
            )
            row = cursor.fetchone()
            if row:
                return datetime.fromisoformat(row[0])
        return None

    def _save_odds_record(self, match_id: str, odds_type: str, snapshot_id: int):
        """保存赔率记录"""
        snapshot = self.snapshot_manager.get_snapshot(
            odds_type,
            f"{match_id}_{odds_type}"
        )

        if snapshot:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO odds (match_id, odds_type, bookmaker, data)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        match_id,
                        odds_type,
                        snapshot.get("bookmaker", "unknown"),
                        json.dumps(snapshot, ensure_ascii=False)
                    )
                )

    def collect_all_window_snapshots(
        self,
        match_id: str,
        match_start_time: datetime
    ) -> dict[str, dict[str, Optional[int]]]:
        """
        按所有时间窗口采集完整快照系列

        Args:
            match_id: 比赛ID
            match_start_time: 比赛开始时间

        Returns:
            各赔率类型各窗口的快照ID
        """
        results = {}

        for odds_type in [OddsType.EUROPEAN, OddsType.ASIAN, OddsType.OVER_UNDER]:
            window_results = {}

            for window_name in SnapshotWindow.get_window_names():
                snapshot_id = self.scheduler.collect_snapshot(
                    odds_type,
                    f"{match_id}_{odds_type}",
                    window_name,
                    match_start_time
                )
                window_results[window_name] = snapshot_id

                if snapshot_id:
                    self._save_odds_record(match_id, odds_type, snapshot_id)

            results[odds_type] = window_results

        return results

    def get_odds_time_series(
        self,
        match_id: str,
        odds_type: str,
        field_path: Optional[str] = None
    ) -> Any:
        """
        获取赔率时间序列

        Args:
            match_id: 比赛ID
            odds_type: 赔率类型
            field_path: 字段路径

        Returns:
            时间序列 DataFrame
        """
        return self.snapshot_manager.get_time_series(
            odds_type,
            f"{match_id}_{odds_type}",
            field_path
        )

    def get_latest_odds(
        self,
        match_id: str,
        odds_type: str
    ) -> Optional[dict[str, Any]]:
        """
        获取最新赔率

        Args:
            match_id: 比赛ID
            odds_type: 赔率类型

        Returns:
            最新赔率数据
        """
        return self.snapshot_manager.get_snapshot(
            odds_type,
            f"{match_id}_{odds_type}"
        )

    def start_collection(
        self,
        interval_seconds: int = 60,
        hours_ahead: int = 24
    ):
        """
        启动自动采集

        Args:
            interval_seconds: 采集间隔
            hours_ahead: 采集未来多少小时内的比赛
        """
        if self._running:
            return

        self._running = True
        self._collector_thread = threading.Thread(
            target=self._collection_loop,
            args=(interval_seconds, hours_ahead),
            daemon=True
        )
        self._collector_thread.start()

    def _collection_loop(self, interval_seconds: int, hours_ahead: int):
        """采集循环"""
        while self._running:
            try:
                # 获取即将开赛的比赛
                matches = self.get_upcoming_matches(hours_ahead)

                for match in matches:
                    match_time = datetime.fromisoformat(match["match_time"])
                    now = datetime.now()

                    # 检查是否需要采集
                    upcoming_windows = self.scheduler.get_upcoming_windows(match_time, now)

                    for window_info in upcoming_windows:
                        if window_info["seconds_until"] <= interval_seconds:
                            # 触发采集
                            self.collect_match_odds(
                                match["match_id"],
                                match_start_time=match_time
                            )
                            break

            except Exception as e:
                print(f"采集循环错误: {e}")

            time.sleep(interval_seconds)

    def stop_collection(self):
        """停止自动采集"""
        self._running = False
        if self._collector_thread:
            self._collector_thread.join(timeout=5)
            self._collector_thread = None

    def update_match_status(
        self,
        match_id: str,
        status: str,
        home_score: Optional[int] = None,
        away_score: Optional[int] = None
    ):
        """
        更新比赛状态

        Args:
            match_id: 比赛ID
            status: 状态 (upcoming, live, finished)
            home_score: 主队比分
            away_score: 客队比分
        """
        with sqlite3.connect(self.db_path) as conn:
            if home_score is not None and away_score is not None:
                conn.execute(
                    """
                    UPDATE matches
                    SET status = ?, home_score = ?, away_score = ?, updated_at = ?
                    WHERE match_id = ?
                    """,
                    (status, home_score, away_score, datetime.now().isoformat(), match_id)
                )
            else:
                conn.execute(
                    """
                    UPDATE matches SET status = ?, updated_at = ? WHERE match_id = ?
                    """,
                    (status, datetime.now().isoformat(), match_id)
                )

    def get_collection_status(self) -> dict[str, Any]:
        """获取采集器状态"""
        return {
            "running": self._running,
            "registered_collectors": list(self.scheduler._collectors.keys()),
            "available_windows": SnapshotWindow.get_window_names()
        }

    def cleanup_old_matches(self, days_old: int = 7) -> int:
        """
        清理旧比赛记录

        Args:
            days_old: 保留最近多少天

        Returns:
            删除的记录数
        """
        cutoff = datetime.now() - timedelta(days=days_old)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM matches WHERE match_time < ?",
                (cutoff.isoformat(),)
            )
            return cursor.rowcount
