"""
实时快照调度器 — 按时间窗口自动采集盘口快照
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Callable
import threading

from backend.data.time_series_snapshot import TimeSeriesSnapshot


class SnapshotWindow:
    """快照时间窗口定义"""

    WINDOWS = {
        "T-24h": timedelta(hours=24),
        "T-12h": timedelta(hours=12),
        "T-6h": timedelta(hours=6),
        "T-3h": timedelta(hours=3),
        "T-1h": timedelta(hours=1),
        "T-30m": timedelta(minutes=30),
        "T-10m": timedelta(minutes=10),
    }

    @classmethod
    def get_window(cls, name: str) -> Optional[timedelta]:
        """获取时间窗口"""
        return cls.WINDOWS.get(name)

    @classmethod
    def get_all_windows(cls) -> dict[str, timedelta]:
        """获取所有时间窗口"""
        return cls.WINDOWS.copy()

    @classmethod
    def get_window_names(cls) -> list[str]:
        """获取所有窗口名称"""
        return list(cls.WINDOWS.keys())


class LiveSnapshotScheduler:
    """
    实时快照调度器

    功能：
    - 按时间窗口自动采集盘口快照
    - 支持多种采集间隔（T-24h 到 T-10m）
    - 定时执行快照采集
    - 支持自定义数据采集器
    """

    def __init__(
        self,
        snapshot_db_path: str = "data/time_series.db",
        config_path: Optional[str] = None
    ):
        """
        初始化调度器

        Args:
            snapshot_db_path: 快照数据库路径
            config_path: 配置文件路径
        """
        self.snapshot_manager = TimeSeriesSnapshot(snapshot_db_path)
        self.config_path = config_path
        self.config = self._load_config() if config_path else {}

        self._running = False
        self._scheduler_thread: Optional[threading.Thread] = None
        self._collectors: dict[str, Callable] = {}
        self._snapshot_schedule: dict[str, datetime] = {}

    def _load_config(self) -> dict[str, Any]:
        """加载配置文件"""
        if not Path(self.config_path).exists():
            return {}
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def register_collector(
        self,
        entity_type: str,
        collector_func: Callable[[str], dict[str, Any]]
    ):
        """
        注册数据采集器

        Args:
            entity_type: 实体类型（如 "odds", "match"）
            collector_func: 采集函数，接收 entity_id，返回数据字典
        """
        self._collectors[entity_type] = collector_func

    def collect_snapshot(
        self,
        entity_type: str,
        entity_id: str,
        window_name: str = "T-10m",
        match_start_time: Optional[datetime] = None
    ) -> Optional[int]:
        """
        采集单个快照

        Args:
            entity_type: 实体类型
            entity_id: 实体ID
            window_name: 时间窗口名称
            match_start_time: 比赛开始时间（用于计算窗口）

        Returns:
            快照ID，失败返回 None
        """
        # 获取采集器
        collector = self._collectors.get(entity_type)
        if collector is None:
            return None

        # 计算快照时间
        snapshot_time = self._calculate_snapshot_time(window_name, match_start_time)
        if snapshot_time is None:
            snapshot_time = datetime.now()

        # 采集数据
        try:
            data = collector(entity_id)
            if data is None:
                return None

            # 保存快照
            return self.snapshot_manager.save_snapshot(
                entity_type,
                entity_id,
                data,
                snapshot_time
            )
        except Exception as e:
            print(f"采集快照失败: {entity_type}/{entity_id} - {e}")
            return None

    def _calculate_snapshot_time(
        self,
        window_name: str,
        match_start_time: Optional[datetime] = None
    ) -> Optional[datetime]:
        """
        计算快照时间点

        Args:
            window_name: 时间窗口名称
            match_start_time: 比赛开始时间

        Returns:
            快照时间点
        """
        window_delta = SnapshotWindow.get_window(window_name)
        if window_delta is None:
            return None

        if match_start_time is None:
            return datetime.now()

        return match_start_time - window_delta

    def collect_window_series(
        self,
        entity_type: str,
        entity_id: str,
        match_start_time: datetime
    ) -> dict[str, Optional[int]]:
        """
        按所有时间窗口采集快照

        Args:
            entity_type: 实体类型
            entity_id: 实体ID
            match_start_time: 比赛开始时间

        Returns:
            各窗口的快照ID字典
        """
        results = {}

        for window_name in SnapshotWindow.get_window_names():
            snapshot_id = self.collect_snapshot(
                entity_type,
                entity_id,
                window_name,
                match_start_time
            )
            results[window_name] = snapshot_id

        return results

    def start_scheduler(
        self,
        entity_type: str,
        entity_id: str,
        match_start_time: datetime,
        interval_seconds: int = 60
    ):
        """
        启动定时快照采集

        Args:
            entity_type: 实体类型
            entity_id: 实体ID
            match_start_time: 比赛开始时间
            interval_seconds: 检查间隔（秒）
        """
        if self._running:
            return

        self._running = True
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            args=(entity_type, entity_id, match_start_time, interval_seconds),
            daemon=True
        )
        self._scheduler_thread.start()

    def _scheduler_loop(
        self,
        entity_type: str,
        entity_id: str,
        match_start_time: datetime,
        interval_seconds: int
    ):
        """调度循环"""
        collected_windows = set()

        while self._running:
            now = datetime.now()

            # 检查是否需要采集
            for window_name, window_delta in SnapshotWindow.get_all_windows().items():
                target_time = match_start_time - window_delta

                # 如果到达或超过目标时间，且尚未采集
                if now >= target_time and window_name not in collected_windows:
                    snapshot_id = self.collect_snapshot(
                        entity_type,
                        entity_id,
                        window_name,
                        match_start_time
                    )
                    if snapshot_id:
                        collected_windows.add(window_name)
                        print(f"[{now.isoformat()}] 采集快照: {window_name} -> {snapshot_id}")

            # 比赛开始后停止
            if now >= match_start_time:
                print("比赛已开始，停止采集")
                self._running = False
                break

            time.sleep(interval_seconds)

    def stop_scheduler(self):
        """停止调度器"""
        self._running = False
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
            self._scheduler_thread = None

    def schedule_batch_snapshots(
        self,
        entities: list[dict[str, str]],
        match_start_time: datetime
    ) -> dict[str, dict[str, Optional[int]]]:
        """
        批量调度快照采集

        Args:
            entities: 实体列表 [{"type": "odds", "id": "odds_001"}, ...]
            match_start_time: 比赛开始时间

        Returns:
            各实体的快照ID字典
        """
        results = {}

        for entity in entities:
            entity_type = entity.get("type")
            entity_id = entity.get("id")

            if entity_type and entity_id:
                snapshot_ids = self.collect_window_series(
                    entity_type,
                    entity_id,
                    match_start_time
                )
                results[f"{entity_type}/{entity_id}"] = snapshot_ids

        return results

    def get_upcoming_windows(
        self,
        match_start_time: datetime,
        current_time: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        """
        获取即将到来的采集窗口

        Args:
            match_start_time: 比赛开始时间
            current_time: 当前时间（默认当前时间）

        Returns:
            即将到来的窗口列表
        """
        if current_time is None:
            current_time = datetime.now()

        upcoming = []

        for window_name, window_delta in SnapshotWindow.get_all_windows().items():
            target_time = match_start_time - window_delta

            if target_time > current_time:
                time_until = target_time - current_time
                upcoming.append({
                    "window": window_name,
                    "target_time": target_time.isoformat(),
                    "seconds_until": time_until.total_seconds()
                })

        # 按时间排序
        upcoming.sort(key=lambda x: x["seconds_until"])
        return upcoming

    def get_collector_status(self) -> dict[str, Any]:
        """获取采集器状态"""
        return {
            "registered_collectors": list(self._collectors.keys()),
            "scheduler_running": self._running,
            "available_windows": SnapshotWindow.get_window_names()
        }

    def is_scheduler_running(self) -> bool:
        """检查调度器是否运行中"""
        return self._running
