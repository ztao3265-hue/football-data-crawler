"""
CLV (Closing Line Value) 追踪 — 记录推荐与封盘盘口对比
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from enum import Enum

from backend.live.live_prediction_engine import PredictionResult


class CLVStatus(Enum):
    """CLV 状态"""
    POSITIVE = "positive"   # 正向 CLV (推荐优于封盘)
    NEGATIVE = "negative"   # 负向 CLV (封盘优于推荐)
    NEUTRAL = "neutral"     # 中性 CLV
    PENDING = "pending"     # 等待封盘数据


class CLVTracking:
    """
    CLV 追踪系统

    功能：
    - 记录推荐时盘口
    - 记录最终封盘盘口
    - 计算 closing line movement
    - 计算 CLV value
    """

    def __init__(self, db_path: str = "data/clv_tracking.db"):
        """
        初始化 CLV 追踪

        Args:
            db_path: 数据库路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clv_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    bet_type TEXT NOT NULL,
                    recommendation_time TEXT NOT NULL,
                    recommendation_odds REAL NOT NULL,
                    closing_time TEXT,
                    closing_odds REAL,
                    line_movement REAL,
                    clv_value REAL,
                    clv_status TEXT DEFAULT 'pending',
                    bookmaker TEXT,
                    metadata TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_clv_match
                ON clv_records(match_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_clv_status
                ON clv_records(clv_status)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_clv_time
                ON clv_records(recommendation_time)
            """)

    def record_recommendation(
        self,
        match_id: str,
        bet_type: str,
        odds: float,
        recommendation_time: Optional[datetime] = None,
        bookmaker: str = "default",
        metadata: Optional[dict[str, Any]] = None
    ) -> int:
        """
        记录推荐时的盘口

        Args:
            match_id: 比赛ID
            bet_type: 投注类型 (home_win, draw, away_win, over, under 等)
            odds: 推荐时的赔率
            recommendation_time: 推荐时间
            bookmaker: 博彩公司
            metadata: 附加信息

        Returns:
            记录ID
        """
        if recommendation_time is None:
            recommendation_time = datetime.now()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO clv_records
                (match_id, bet_type, recommendation_time, recommendation_odds,
                 bookmaker, metadata, clv_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    bet_type,
                    recommendation_time.isoformat(),
                    odds,
                    bookmaker,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                    CLVStatus.PENDING.value
                )
            )
            return cursor.lastrowid

    def update_closing_odds(
        self,
        match_id: str,
        bet_type: str,
        closing_odds: float,
        closing_time: Optional[datetime] = None
    ) -> bool:
        """
        更新封盘赔率

        Args:
            match_id: 比赛ID
            bet_type: 投注类型
            closing_odds: 封盘赔率
            closing_time: 封盘时间

        Returns:
            是否成功
        """
        if closing_time is None:
            closing_time = datetime.now()

        # 获取推荐时的赔率
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT id, recommendation_odds FROM clv_records
                WHERE match_id = ? AND bet_type = ? AND clv_status = ?
                ORDER BY recommendation_time DESC
                LIMIT 1
                """,
                (match_id, bet_type, CLVStatus.PENDING.value)
            )
            row = cursor.fetchone()

            if row is None:
                return False

            record_id = row[0]
            recommendation_odds = row[1]

            # 计算 CLV
            clv_value, line_movement, status = self._calculate_clv(
                recommendation_odds,
                closing_odds
            )

            # 更新记录
            conn.execute(
                """
                UPDATE clv_records
                SET closing_time = ?, closing_odds = ?, line_movement = ?,
                    clv_value = ?, clv_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    closing_time.isoformat(),
                    closing_odds,
                    line_movement,
                    clv_value,
                    status.value,
                    datetime.now().isoformat(),
                    record_id
                )
            )

            return True

    def _calculate_clv(
        self,
        recommendation_odds: float,
        closing_odds: float
    ) -> tuple[float, float, CLVStatus]:
        """
        计算 CLV 值

        Args:
            recommendation_odds: 推荐时赔率
            closing_odds: 封盘赔率

        Returns:
            (clv_value, line_movement, status)
        """
        if recommendation_odds <= 0 or closing_odds <= 0:
            return 0.0, 0.0, CLVStatus.NEUTRAL

        # 赔率变化
        line_movement = closing_odds - recommendation_odds

        # CLV 值 (对于投注者)
        # 如果封盘赔率低于推荐赔率 (line_movement < 0)，说明我们获得了更好的价格
        # CLV = (recommendation_odds / closing_odds - 1) * 100%
        clv_value = (recommendation_odds / closing_odds - 1) * 100

        # 判断状态
        threshold = 1.0  # 1% 阈值

        if clv_value > threshold:
            status = CLVStatus.POSITIVE
        elif clv_value < -threshold:
            status = CLVStatus.NEGATIVE
        else:
            status = CLVStatus.NEUTRAL

        return clv_value, line_movement, status

    def get_clv_record(
        self,
        match_id: str,
        bet_type: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """
        获取 CLV 记录

        Args:
            match_id: 比赛ID
            bet_type: 投注类型

        Returns:
            CLV 记录
        """
        with sqlite3.connect(self.db_path) as conn:
            if bet_type:
                cursor = conn.execute(
                    """
                    SELECT * FROM clv_records
                    WHERE match_id = ? AND bet_type = ?
                    ORDER BY recommendation_time DESC
                    LIMIT 1
                    """,
                    (match_id, bet_type)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM clv_records
                    WHERE match_id = ?
                    ORDER BY recommendation_time DESC
                    LIMIT 1
                    """,
                    (match_id,)
                )

            row = cursor.fetchone()
            if row is None:
                return None

            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))

    def get_all_clv_for_match(self, match_id: str) -> list[dict[str, Any]]:
        """
        获取比赛的所有 CLV 记录

        Args:
            match_id: 比赛ID

        Returns:
            CLV 记录列表
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM clv_records
                WHERE match_id = ?
                ORDER BY recommendation_time DESC
                """,
                (match_id,)
            )

            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            return [dict(zip(columns, row)) for row in rows]

    def get_pending_records(self, limit: int = 100) -> list[dict[str, Any]]:
        """
        获取待更新封盘的记录

        Args:
            limit: 返回数量限制

        Returns:
            待处理记录列表
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM clv_records
                WHERE clv_status = ?
                ORDER BY recommendation_time ASC
                LIMIT ?
                """,
                (CLVStatus.PENDING.value, limit)
            )

            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            return [dict(zip(columns, row)) for row in rows]

    def get_clv_stats(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> dict[str, Any]:
        """
        获取 CLV 统计

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            统计信息
        """
        with sqlite3.connect(self.db_path) as conn:
            sql = """
                SELECT
                    COUNT(*) as total,
                    AVG(clv_value) as avg_clv,
                    SUM(CASE WHEN clv_status = 'positive' THEN 1 ELSE 0 END) as positive_count,
                    SUM(CASE WHEN clv_status = 'negative' THEN 1 ELSE 0 END) as negative_count,
                    SUM(CASE WHEN clv_status = 'neutral' THEN 1 ELSE 0 END) as neutral_count,
                    AVG(line_movement) as avg_movement
                FROM clv_records
                WHERE clv_status != 'pending'
            """
            params = []

            if start_date:
                sql += " AND recommendation_time >= ?"
                params.append(start_date.isoformat())
            if end_date:
                sql += " AND recommendation_time <= ?"
                params.append(end_date.isoformat())

            cursor = conn.execute(sql, params)
            row = cursor.fetchone()

            return {
                "total_records": row[0],
                "average_clv": row[1] if row[1] else 0,
                "positive_count": row[2],
                "negative_count": row[3],
                "neutral_count": row[4],
                "average_line_movement": row[5] if row[5] else 0,
                "positive_rate": row[2] / row[0] * 100 if row[0] > 0 else 0
            }

    def get_best_clv_records(
        self,
        limit: int = 10,
        start_date: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        """
        获取最佳 CLV 记录

        Args:
            limit: 返回数量
            start_date: 开始日期

        Returns:
            最佳 CLV 记录列表
        """
        with sqlite3.connect(self.db_path) as conn:
            sql = """
                SELECT * FROM clv_records
                WHERE clv_status = 'positive'
            """
            params = []

            if start_date:
                sql += " AND recommendation_time >= ?"
                params.append(start_date.isoformat())

            sql += " ORDER BY clv_value DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            return [dict(zip(columns, row)) for row in rows]

    def get_clv_by_bet_type(self) -> dict[str, dict[str, Any]]:
        """
        按投注类型获取 CLV 统计

        Returns:
            各投注类型的 CLV 统计
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    bet_type,
                    COUNT(*) as count,
                    AVG(clv_value) as avg_clv,
                    AVG(line_movement) as avg_movement
                FROM clv_records
                WHERE clv_status != 'pending'
                GROUP BY bet_type
                """
            )
            rows = cursor.fetchall()

        return {
            row[0]: {
                "count": row[1],
                "average_clv": row[2],
                "average_movement": row[3]
            }
            for row in rows
        }

    def calculate_clv_summary(
        self,
        match_id: str
    ) -> dict[str, Any]:
        """
        计算比赛的 CLV 汇总

        Args:
            match_id: 比赛ID

        Returns:
            CLV 汇总
        """
        records = self.get_all_clv_for_match(match_id)

        if not records:
            return {"error": "无 CLV 记录"}

        pending_count = sum(1 for r in records if r["clv_status"] == CLVStatus.PENDING.value)

        completed_records = [r for r in records if r["clv_status"] != CLVStatus.PENDING.value]

        if not completed_records:
            return {
                "match_id": match_id,
                "status": "pending",
                "pending_count": pending_count
            }

        total_clv = sum(r["clv_value"] or 0 for r in completed_records)
        avg_clv = total_clv / len(completed_records)

        positive_count = sum(1 for r in completed_records if r["clv_status"] == CLVStatus.POSITIVE.value)
        negative_count = sum(1 for r in completed_records if r["clv_status"] == CLVStatus.NEGATIVE.value)

        return {
            "match_id": match_id,
            "total_records": len(records),
            "pending_count": pending_count,
            "completed_count": len(completed_records),
            "average_clv": avg_clv,
            "total_clv": total_clv,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "win_rate": positive_count / len(completed_records) * 100 if completed_records else 0
        }

    def delete_old_records(self, days_old: int = 90) -> int:
        """
        删除旧记录

        Args:
            days_old: 保留最近多少天

        Returns:
            删除的记录数
        """
        cutoff = datetime.now() - timedelta(days=days_old)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM clv_records WHERE recommendation_time < ?",
                (cutoff.isoformat(),)
            )
            return cursor.rowcount

    def export_clv_report(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> dict[str, Any]:
        """
        导出 CLV 报告

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            CLV 报告
        """
        stats = self.get_clv_stats(start_date, end_date)
        by_bet_type = self.get_clv_by_bet_type()
        best_records = self.get_best_clv_records(limit=20, start_date=start_date)

        return {
            "period": {
                "start": start_date.isoformat() if start_date else None,
                "end": end_date.isoformat() if end_date else None
            },
            "statistics": stats,
            "by_bet_type": by_bet_type,
            "best_records": best_records
        }
