"""
执行追踪系统 — 记录系统推荐、用户实盘投注、盈亏对比
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from enum import Enum


class BetStatus(Enum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    HALF_WON = "half_won"
    HALF_LOST = "half_lost"
    VOID = "void"
    CASHED_OUT = "cashed_out"


class BetResult(Enum):
    FOLLOWED = "followed"         # 完全跟随系统推荐
    OPPOSITE = "opposite"         # 反向操作
    SKIPPED = "skipped"           # 系统推荐但用户未投注
    MANUAL = "manual"             # 用户自行投注（无系统推荐）
    MODIFIED = "modified"         # 修改了金额/盘口


class ExecutionTracker:
    """
    执行追踪系统

    功能：
    - 记录系统推荐详情
    - 记录用户实际投注
    - 记录投注金额与盈亏
    - 对比系统推荐 vs 用户实际执行偏差
    - 执行质量评分
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from config.paths import DATABASE_DIR
            db_path = str(DATABASE_DIR / "execution_tracking.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS system_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rec_id TEXT UNIQUE NOT NULL,
                    match_id TEXT NOT NULL,
                    league TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    kickoff_time TEXT,
                    bet_type TEXT NOT NULL,
                    pick TEXT NOT NULL,
                    odds REAL NOT NULL,
                    stake REAL,
                    ev REAL,
                    confidence REAL,
                    recommendation_level TEXT,
                    risk_level TEXT DEFAULT 'medium',
                    reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS user_bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rec_id TEXT,
                    match_id TEXT NOT NULL,
                    bet_type TEXT NOT NULL,
                    pick TEXT NOT NULL,
                    odds REAL NOT NULL,
                    stake REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    result TEXT DEFAULT 'followed',
                    pnl REAL DEFAULT 0.0,
                    roi REAL DEFAULT 0.0,
                    placed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    settled_at TEXT,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS execution_quality (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    total_recommendations INTEGER DEFAULT 0,
                    followed_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    opposite_count INTEGER DEFAULT 0,
                    manual_count INTEGER DEFAULT 0,
                    system_pnl REAL DEFAULT 0.0,
                    user_pnl REAL DEFAULT 0.0,
                    pnl_deviation REAL DEFAULT 0.0,
                    follow_rate REAL DEFAULT 0.0,
                    execution_score REAL DEFAULT 0.0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_rec_match ON system_recommendations(match_id);
                CREATE INDEX IF NOT EXISTS idx_rec_level ON system_recommendations(recommendation_level);
                CREATE INDEX IF NOT EXISTS idx_rec_created ON system_recommendations(created_at);
                CREATE INDEX IF NOT EXISTS idx_bets_match ON user_bets(match_id);
                CREATE INDEX IF NOT EXISTS idx_bets_status ON user_bets(status);
                CREATE INDEX IF NOT EXISTS idx_bets_rec ON user_bets(rec_id);
                CREATE INDEX IF NOT EXISTS idx_quality_date ON execution_quality(date);
            """)

    # ── 系统推荐记录 ─────────────────────────────────────────────

    def record_recommendation(
        self,
        match_id: str,
        bet_type: str,
        pick: str,
        odds: float,
        league: str = "",
        home_team: str = "",
        away_team: str = "",
        kickoff_time: str = "",
        stake: float = 0.0,
        ev: float = 0.0,
        confidence: float = 0.0,
        recommendation_level: str = "normal",
        risk_level: str = "medium",
        reason: str = ""
    ) -> str:
        import uuid
        rec_id = f"REC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO system_recommendations
                   (rec_id, match_id, league, home_team, away_team, kickoff_time,
                    bet_type, pick, odds, stake, ev, confidence,
                    recommendation_level, risk_level, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec_id, match_id, league, home_team, away_team, kickoff_time,
                 bet_type, pick, odds, stake, ev, confidence,
                 recommendation_level, risk_level, reason)
            )
        return rec_id

    def get_recommendations(
        self,
        date: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 50
    ) -> list[dict]:
        sql = "SELECT * FROM system_recommendations WHERE 1=1"
        params: list = []

        if date:
            sql += " AND date(created_at) = ?"
            params.append(date)
        if level:
            sql += " AND recommendation_level = ?"
            params.append(level)

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── 用户投注记录 ─────────────────────────────────────────────

    def record_bet(
        self,
        match_id: str,
        bet_type: str,
        pick: str,
        odds: float,
        stake: float,
        rec_id: str = "",
        result: str = "followed",
        notes: str = ""
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO user_bets
                   (rec_id, match_id, bet_type, pick, odds, stake, result, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec_id if rec_id else None, match_id, bet_type, pick, odds, stake, result, notes)
            )
            return cursor.lastrowid

    def settle_bet(
        self,
        bet_id: int,
        status: str,
        actual_pnl: Optional[float] = None
    ) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT stake, odds FROM user_bets WHERE id = ?", (bet_id,)
            ).fetchone()
            if not row:
                return False

            stake, odds = row

            if actual_pnl is not None:
                pnl = actual_pnl
            elif status == BetStatus.WON.value:
                pnl = stake * (odds - 1)
            elif status == BetStatus.HALF_WON.value:
                pnl = stake * (odds - 1) / 2
            elif status == BetStatus.LOST.value:
                pnl = -stake
            elif status == BetStatus.HALF_LOST.value:
                pnl = -stake / 2
            else:
                pnl = 0.0

            roi = (pnl / stake * 100) if stake > 0 else 0.0

            conn.execute(
                """UPDATE user_bets
                   SET status = ?, pnl = ?, roi = ?, settled_at = ?
                   WHERE id = ?""",
                (status, round(pnl, 2), round(roi, 2), datetime.now().isoformat(), bet_id)
            )
            return True

    def get_user_bets(
        self,
        status: Optional[str] = None,
        result: Optional[str] = None,
        date: Optional[str] = None,
        limit: int = 100
    ) -> list[dict]:
        sql = "SELECT * FROM user_bets WHERE 1=1"
        params: list = []

        if status:
            sql += " AND status = ?"
            params.append(status)
        if result:
            sql += " AND result = ?"
            params.append(result)
        if date:
            sql += " AND date(placed_at) = ?"
            params.append(date)

        sql += " ORDER BY placed_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── 执行偏差分析 ─────────────────────────────────────────────

    def compare_execution(self, date: Optional[str] = None) -> dict[str, Any]:
        """对比系统推荐 vs 用户实际执行"""
        recs = self.get_recommendations(date=date)
        bets = self.get_user_bets(date=date)

        rec_ids = {r["rec_id"] for r in recs if r["rec_id"]}
        bet_rec_ids = {b["rec_id"] for b in bets if b["rec_id"]}

        followed_ids = rec_ids & bet_rec_ids
        skipped_ids = rec_ids - bet_rec_ids

        system_pnl = sum(
            (r["stake"] * (r["odds"] - 1)) if r.get("stake") and r["stake"] > 0 else 0
            for r in recs
        )
        user_pnl = sum(b.get("pnl", 0) or 0 for b in bets)

        total_recs = len(recs)
        follow_rate = (len(followed_ids) / total_recs * 100) if total_recs > 0 else 0

        return {
            "date": date or "all",
            "total_recommendations": total_recs,
            "total_bets": len(bets),
            "followed": len(followed_ids),
            "skipped": len(skipped_ids),
            "follow_rate": round(follow_rate, 1),
            "system_pnl": round(system_pnl, 2),
            "user_pnl": round(user_pnl, 2),
            "pnl_deviation": round(user_pnl - system_pnl, 2),
        }

    def calculate_execution_score(self, date: Optional[str] = None) -> float:
        """计算执行质量评分 (0-100)"""
        comp = self.compare_execution(date)
        total = comp["total_recommendations"]
        if total == 0:
            return 100.0

        follow_score = comp["follow_rate"]
        pnl_score = 50.0
        if comp["system_pnl"] > 0:
            pnl_score = min(50, max(0, (comp["user_pnl"] / comp["system_pnl"]) * 50))

        return round(follow_score * 0.5 + pnl_score, 1)

    def save_daily_quality(self, date: Optional[str] = None):
        """保存每日执行质量记录"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        comp = self.compare_execution(date)
        bets = self.get_user_bets(date=date)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO execution_quality
                   (date, total_recommendations, followed_count, skipped_count,
                    opposite_count, manual_count, system_pnl, user_pnl,
                    pnl_deviation, follow_rate, execution_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date,
                    comp["total_recommendations"],
                    comp["followed"],
                    comp["skipped"],
                    sum(1 for b in bets if b.get("result") == "opposite"),
                    sum(1 for b in bets if b.get("result") == "manual"),
                    comp["system_pnl"],
                    comp["user_pnl"],
                    comp["pnl_deviation"],
                    comp["follow_rate"],
                    self.calculate_execution_score(date),
                )
            )

    def get_quality_history(self, days: int = 30) -> list[dict]:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM execution_quality WHERE date >= ? ORDER BY date",
                    (cutoff,)
                ).fetchall()
            ]

    # ── 汇总统计 ─────────────────────────────────────────────────

    def get_summary(self) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            total_recs = conn.execute(
                "SELECT COUNT(*) FROM system_recommendations"
            ).fetchone()[0]
            total_bets = conn.execute(
                "SELECT COUNT(*) FROM user_bets"
            ).fetchone()[0]
            settled = conn.execute(
                "SELECT COUNT(*), SUM(pnl), AVG(roi) FROM user_bets WHERE status != 'pending'"
            ).fetchone()
            by_level = conn.execute(
                """SELECT recommendation_level, COUNT(*)
                   FROM system_recommendations GROUP BY recommendation_level"""
            ).fetchall()
            by_result = conn.execute(
                """SELECT result, COUNT(*)
                   FROM user_bets GROUP BY result"""
            ).fetchall()

        return {
            "total_recommendations": total_recs,
            "total_bets": total_bets,
            "settled_bets": settled[0],
            "total_pnl": round(settled[1] or 0, 2),
            "average_roi": round(settled[2] or 0, 2),
            "recommendations_by_level": dict(by_level),
            "bets_by_result": dict(by_result),
        }
