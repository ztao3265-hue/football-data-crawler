"""
去重 & 风险过滤器 — 推送前最后一道防线
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


class DedupAndRiskFilter:
    """
    去重 & 风险过滤

    推送前检查:
    - 重复推荐去重 (同场比赛同方向24h内不重复推)
    - 关联比赛检测 (同一联赛多场推荐风险)
    - 赔率异常检测
    - 水位合理性检查
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from config.paths import DATABASE_DIR
            db_path = str(DATABASE_DIR / "dedup_filter.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    bet_type TEXT NOT NULL,
                    pick TEXT NOT NULL,
                    odds REAL,
                    fingerprint TEXT NOT NULL,
                    pushed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_push_fingerprint
                ON push_history(fingerprint)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_push_match
                ON push_history(match_id, bet_type)
            """)

    def _fingerprint(self, match_id: str, bet_type: str, pick: str) -> str:
        raw = f"{match_id}|{bet_type}|{pick}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── 去重检查 ──────────────────────────────────────────────────

    def is_duplicate(self, match_id: str, bet_type: str, pick: str) -> tuple[bool, str]:
        """
        检查是否24h内已推送过相同推荐
        """
        fp = self._fingerprint(match_id, bet_type, pick)
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id, pushed_at FROM push_history WHERE fingerprint = ? AND pushed_at >= ?",
                (fp, cutoff)
            ).fetchone()

        if row:
            return True, f"24h内已推送 (id={row[0]}, at={row[1]})"
        return False, ""

    def record_push(self, match_id: str, bet_type: str, pick: str, odds: float):
        """记录一次推送"""
        fp = self._fingerprint(match_id, bet_type, pick)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO push_history (match_id, bet_type, pick, odds, fingerprint) VALUES (?, ?, ?, ?, ?)",
                (match_id, bet_type, pick, odds, fp)
            )

    # ── 关联风险检查 ──────────────────────────────────────────────

    def check_correlation_risk(
        self, recommendations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        检查关联风险: 同一联赛多场推荐 = 集中度风险
        """
        league_count: dict[str, int] = {}
        for r in recommendations:
            league = r.get("league", "Unknown")
            league_count[league] = league_count.get(league, 0) + 1

        warnings = []
        for league, count in league_count.items():
            if count >= 4:
                warnings.append(f"{league}: {count}场推荐, 集中度风险高")
            elif count >= 3:
                warnings.append(f"{league}: {count}场推荐, 注意集中度")

        return {
            "has_correlation_risk": len(warnings) > 0,
            "league_distribution": league_count,
            "warnings": warnings,
        }

    # ── 赔率异常检测 ──────────────────────────────────────────────

    def check_odds_anomaly(self, odds_data: dict[str, Any]) -> dict[str, Any]:
        """
        检测赔率异常: 异常高/低赔率, 不合理水位
        """
        issues = []

        home = float(odds_data.get("home_win", 0))
        draw = float(odds_data.get("draw", 0))
        away = float(odds_data.get("away_win", 0))

        # 赔率 > 10.0 或 < 1.05 视为异常
        for key, val in [("主胜", home), ("平局", draw), ("客胜", away)]:
            if val > 10.0:
                issues.append(f"{key}赔率异常高: {val}")
            elif 0 < val < 1.05:
                issues.append(f"{key}赔率异常低: {val}")

        # 市场溢价过高 (>15% 说明赔率质量差)
        if home > 1.0 and draw > 1.0 and away > 1.0:
            overround = 1 / home + 1 / draw + 1 / away - 1.0
            if overround > 0.15:
                issues.append(f"市场溢价过高: {overround:.1%}, 赔率质量差")
            elif overround < 0.01:
                issues.append(f"市场溢价过低: {overround:.1%}, 可能数据异常")

        return {
            "has_anomaly": len(issues) > 0,
            "issues": issues,
        }

    # ── 综合过滤 ──────────────────────────────────────────────────

    def filter(
        self, recommendations: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        综合过滤: 去重 + 风险检查
        返回 (通过过滤的推荐列表, 过滤报告)
        """
        passed = []
        rejected = []
        filter_report = {
            "total": len(recommendations),
            "duplicates_removed": 0,
            "anomaly_rejected": 0,
            "passed": 0,
            "details": [],
        }

        for r in recommendations:
            match_id = r.get("match_id", "")
            bet_type = r.get("bet_type", "")
            pick = r.get("pick", "")
            odds_data_raw = r.get("odds_snapshot", "{}")

            # 1. 去重检查
            is_dup, dup_reason = self.is_duplicate(match_id, bet_type, pick)
            if is_dup:
                rejected.append(r)
                filter_report["duplicates_removed"] += 1
                filter_report["details"].append({
                    "match_id": match_id, "pick": pick, "status": "rejected", "reason": dup_reason
                })
                continue

            # 2. 赔率异常检测
            try:
                odds_data = json.loads(odds_data_raw) if isinstance(odds_data_raw, str) else odds_data_raw
            except (json.JSONDecodeError, TypeError):
                odds_data = r.get("odds", {})

            anomaly = self.check_odds_anomaly(odds_data)
            if anomaly["has_anomaly"]:
                rejected.append(r)
                filter_report["anomaly_rejected"] += 1
                filter_report["details"].append({
                    "match_id": match_id, "pick": pick,
                    "status": "rejected", "reason": "; ".join(anomaly["issues"])
                })
                continue

            # 3. 通过
            passed.append(r)
            filter_report["details"].append({
                "match_id": match_id, "pick": pick, "status": "passed"
            })

        filter_report["passed"] = len(passed)

        # 关联风险
        corr = self.check_correlation_risk(passed)
        filter_report["correlation_risk"] = corr

        return passed, filter_report
