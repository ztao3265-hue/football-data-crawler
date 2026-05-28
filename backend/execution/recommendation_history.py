"""
推荐历史 — 保存每日推荐、盘口变化、推荐等级变化、CLV结果
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


class RecommendationHistory:
    """
    推荐历史系统

    功能：
    - 保存每日推荐历史
    - 追踪盘口变化 (open → close)
    - 追踪推荐等级变化
    - 追踪 CLV 结果
    - 历史查询与统计
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from config.paths import DATABASE_DIR
            db_path = str(DATABASE_DIR / "recommendation_history.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS recommendation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    league TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    kickoff_time TEXT,
                    bet_type TEXT NOT NULL,
                    pick TEXT NOT NULL,
                    recommendation_level TEXT NOT NULL,
                    ev REAL NOT NULL,
                    confidence REAL NOT NULL,
                    risk_level TEXT DEFAULT 'medium',
                    open_odds REAL,
                    close_odds REAL,
                    odds_movement REAL,
                    level_changes TEXT,
                    clv_result TEXT,
                    match_result TEXT,
                    actual_pnl REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS odds_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    bet_type TEXT NOT NULL,
                    odds REAL NOT NULL,
                    source TEXT DEFAULT 'system',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS level_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    change_time TEXT NOT NULL,
                    old_level TEXT,
                    new_level TEXT NOT NULL,
                    old_ev REAL,
                    new_ev REAL,
                    old_confidence REAL,
                    new_confidence REAL,
                    reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_history_date ON recommendation_history(date);
                CREATE INDEX IF NOT EXISTS idx_history_match ON recommendation_history(match_id);
                CREATE INDEX IF NOT EXISTS idx_history_level ON recommendation_history(recommendation_level);
                CREATE INDEX IF NOT EXISTS idx_snapshots_match ON odds_snapshots(match_id);
                CREATE INDEX IF NOT EXISTS idx_changes_match ON level_changes(match_id);
            """)

    # ── 推荐记录 ─────────────────────────────────────────────────

    def save_recommendation(
        self,
        match_id: str,
        bet_type: str,
        pick: str,
        ev: float,
        confidence: float,
        recommendation_level: str,
        risk_level: str = "medium",
        league: str = "",
        home_team: str = "",
        away_team: str = "",
        kickoff_time: str = "",
        open_odds: float = 0.0,
        date: Optional[str] = None,
    ) -> int:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO recommendation_history
                   (date, match_id, league, home_team, away_team, kickoff_time,
                    bet_type, pick, recommendation_level, ev, confidence,
                    risk_level, open_odds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, match_id, league, home_team, away_team, kickoff_time,
                 bet_type, pick, recommendation_level, ev, confidence,
                 risk_level, open_odds)
            )
            return cursor.lastrowid

    # ── 盘口快照 ─────────────────────────────────────────────────

    def record_odds_snapshot(
        self,
        match_id: str,
        bet_type: str,
        odds: float,
        snapshot_time: Optional[datetime] = None,
    ) -> int:
        if snapshot_time is None:
            snapshot_time = datetime.now()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO odds_snapshots
                   (match_id, snapshot_time, bet_type, odds)
                   VALUES (?, ?, ?, ?)""",
                (match_id, snapshot_time.isoformat(), bet_type, odds)
            )
            return cursor.lastrowid

    def update_close_odds(
        self,
        match_id: str,
        bet_type: str,
        close_odds: float,
    ) -> bool:
        """更新封盘赔率并计算盘口变化"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT id, open_odds FROM recommendation_history
                   WHERE match_id = ? AND bet_type = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (match_id, bet_type)
            ).fetchone()

            if not row:
                return False

            rec_id, open_odds = row
            movement = close_odds - open_odds if open_odds else 0

            conn.execute(
                """UPDATE recommendation_history
                   SET close_odds = ?, odds_movement = ?
                   WHERE id = ?""",
                (close_odds, round(movement, 4), rec_id)
            )
            return True

    def get_odds_history(
        self, match_id: str, bet_type: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """获取盘口变化历史"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if bet_type:
                rows = conn.execute(
                    """SELECT * FROM odds_snapshots
                       WHERE match_id = ? AND bet_type = ?
                       ORDER BY snapshot_time""",
                    (match_id, bet_type)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM odds_snapshots
                       WHERE match_id = ? ORDER BY snapshot_time""",
                    (match_id,)
                ).fetchall()
            return [dict(r) for r in rows]

    # ── 推荐等级变化 ─────────────────────────────────────────────

    def record_level_change(
        self,
        match_id: str,
        new_level: str,
        new_ev: float,
        new_confidence: float,
        old_level: Optional[str] = None,
        old_ev: Optional[float] = None,
        old_confidence: Optional[float] = None,
        reason: str = "",
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            # 获取上一次等级
            if old_level is None:
                prev = conn.execute(
                    """SELECT new_level, new_ev, new_confidence FROM level_changes
                       WHERE match_id = ? ORDER BY change_time DESC LIMIT 1""",
                    (match_id,)
                ).fetchone()
                if prev:
                    old_level, old_ev, old_confidence = prev

            cursor = conn.execute(
                """INSERT INTO level_changes
                   (match_id, change_time, old_level, new_level,
                    old_ev, new_ev, old_confidence, new_confidence, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (match_id, datetime.now().isoformat(), old_level, new_level,
                 old_ev, new_ev, old_confidence, new_confidence, reason)
            )
            return cursor.lastrowid

    def get_level_changes(
        self, match_id: str
    ) -> list[dict[str, Any]]:
        """获取推荐等级变化历史"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM level_changes
                   WHERE match_id = ? ORDER BY change_time""",
                (match_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── CLV 结果关联 ─────────────────────────────────────────────

    def record_clv_result(
        self,
        match_id: str,
        bet_type: str,
        clv_value: float,
        clv_status: str,
    ) -> bool:
        """记录 CLV 结果到推荐历史"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM recommendation_history WHERE match_id = ? AND bet_type = ? ORDER BY created_at DESC LIMIT 1",
                (match_id, bet_type)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE recommendation_history SET clv_result = ? WHERE id = ?",
                    (json.dumps({"clv_value": clv_value, "clv_status": clv_status}), row[0])
                )
            return True

    # ── 比赛结果记录 ─────────────────────────────────────────────

    def record_match_result(
        self,
        match_id: str,
        match_result: str,
        actual_pnl: float,
    ) -> bool:
        """记录比赛结果和实际盈亏"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE recommendation_history
                   SET match_result = ?, actual_pnl = ?
                   WHERE match_id = ? AND match_result IS NULL""",
                (match_result, round(actual_pnl, 2), match_id)
            )
            return True

    # ── 历史查询 ─────────────────────────────────────────────────

    def get_history(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recommendation_history WHERE 1=1"
        params: list = []

        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        if level:
            sql += " AND recommendation_level = ?"
            params.append(level)

        sql += " ORDER BY date DESC, ev DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_match_history(self, match_id: str) -> dict[str, Any]:
        """获取单场比赛完整历史 (推荐+盘口+等级变化)"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            recs = conn.execute(
                "SELECT * FROM recommendation_history WHERE match_id = ?",
                (match_id,)
            ).fetchall()

            odds = conn.execute(
                "SELECT * FROM odds_snapshots WHERE match_id = ? ORDER BY snapshot_time",
                (match_id,)
            ).fetchall()

            changes = conn.execute(
                "SELECT * FROM level_changes WHERE match_id = ? ORDER BY change_time",
                (match_id,)
            ).fetchall()

        return {
            "match_id": match_id,
            "recommendations": [dict(r) for r in recs],
            "odds_history": [dict(o) for o in odds],
            "level_changes": [dict(c) for c in changes],
        }

    # ── 统计 ─────────────────────────────────────────────────────

    def get_stats(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> dict[str, Any]:
        sql = "SELECT * FROM recommendation_history WHERE 1=1"
        params: list = []

        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            total = len(rows)

            if total == 0:
                return {"total": 0}

            by_level = {}
            by_risk = {}
            settled = []
            for r in rows:
                level = r[9]
                risk = r[12]
                by_level[level] = by_level.get(level, 0) + 1
                by_risk[risk] = by_risk.get(risk, 0) + 1
                if r[17]:
                    settled.append(r[17])

            avg_ev = sum(r[10] for r in rows) / total
            avg_conf = sum(r[11] for r in rows) / total
            total_pnl = sum(settled)

            return {
                "total_recommendations": total,
                "average_ev": round(avg_ev, 4),
                "average_confidence": round(avg_conf, 3),
                "by_recommendation_level": by_level,
                "by_risk_level": by_risk,
                "settled_count": len(settled),
                "total_pnl": round(total_pnl, 2),
                "average_pnl_per_rec": round(total_pnl / len(settled), 2) if settled else 0,
            }
