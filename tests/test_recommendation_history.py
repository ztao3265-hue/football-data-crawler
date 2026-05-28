"""
推荐历史系统测试
"""
import json
from datetime import datetime, timedelta

import pytest

from backend.execution.recommendation_history import RecommendationHistory


class TestRecommendationHistory:
    """RecommendationHistory 测试"""

    @pytest.fixture
    def history(self, tmp_path):
        db_path = tmp_path / "test_history.db"
        return RecommendationHistory(str(db_path))

    # ── 初始化 ─────────────────────────────────────────────────

    def test_init(self, history):
        assert history.db_path.exists()

    def test_tables_created(self, history):
        import sqlite3
        with sqlite3.connect(history.db_path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]
            assert "recommendation_history" in table_names
            assert "odds_snapshots" in table_names
            assert "level_changes" in table_names

    # ── 推荐记录 ─────────────────────────────────────────────

    def test_save_recommendation(self, history):
        rec_id = history.save_recommendation(
            match_id="m001",
            bet_type="home_win",
            pick="home_win",
            ev=0.05,
            confidence=0.72,
            recommendation_level="strong_buy",
            league="英超",
            home_team="Arsenal",
            away_team="Chelsea",
        )
        assert rec_id > 0

    def test_save_recommendation_minimal(self, history):
        rec_id = history.save_recommendation(
            match_id="m002",
            bet_type="over_2_5",
            pick="over",
            ev=0.03,
            confidence=0.60,
            recommendation_level="normal",
        )
        assert rec_id > 0

    def test_save_recommendation_with_odds(self, history):
        rec_id = history.save_recommendation(
            match_id="m003",
            bet_type="home_win",
            pick="home_win",
            ev=0.04,
            confidence=0.68,
            recommendation_level="strong_buy",
            open_odds=2.10,
        )
        assert rec_id > 0

    # ── 盘口快照 ─────────────────────────────────────────────

    def test_record_odds_snapshot(self, history):
        sid = history.record_odds_snapshot("m001", "home_win", 2.0)
        assert sid > 0

    def test_record_odds_snapshot_with_time(self, history):
        t = datetime.now() - timedelta(hours=6)
        sid = history.record_odds_snapshot("m001", "home_win", 1.95, snapshot_time=t)
        assert sid > 0

    def test_update_close_odds(self, history):
        history.save_recommendation(
            "m001", "home_win", "home_win", 0.05, 0.72, "strong_buy", open_odds=2.0
        )
        ok = history.update_close_odds("m001", "home_win", 1.85)
        assert ok

    def test_update_close_odds_nonexistent(self, history):
        ok = history.update_close_odds("nonexistent", "home_win", 1.85)
        assert not ok

    def test_get_odds_history(self, history):
        history.record_odds_snapshot("m001", "home_win", 2.0)
        history.record_odds_snapshot("m001", "home_win", 1.95)
        history.record_odds_snapshot("m001", "home_win", 1.85)

        odds = history.get_odds_history("m001", "home_win")
        assert len(odds) == 3
        assert odds[0]["odds"] == 2.0
        assert odds[-1]["odds"] == 1.85

    def test_get_odds_history_all_bet_types(self, history):
        history.record_odds_snapshot("m001", "home_win", 2.0)
        history.record_odds_snapshot("m001", "draw", 3.5)

        odds = history.get_odds_history("m001")
        assert len(odds) == 2

    def test_get_odds_history_empty(self, history):
        odds = history.get_odds_history("nonexistent")
        assert odds == []

    # ── 推荐等级变化 ───────────────────────────────────────────

    def test_record_level_change(self, history):
        cid = history.record_level_change(
            match_id="m001",
            new_level="strong_buy",
            new_ev=0.06,
            new_confidence=0.78,
            old_level="normal",
            old_ev=0.03,
            old_confidence=0.65,
            reason="赔率上升，EV增加",
        )
        assert cid > 0

    def test_record_level_change_auto_old(self, history):
        history.record_level_change("m001", "normal", 0.03, 0.65)
        cid = history.record_level_change("m001", "strong_buy", 0.06, 0.78)
        assert cid > 0

    def test_record_level_change_downgrade(self, history):
        cid = history.record_level_change(
            "m001", "pass", 0.005, 0.45,
            old_level="normal", old_ev=0.03, old_confidence=0.62,
            reason="信心下降",
        )
        assert cid > 0

    def test_get_level_changes(self, history):
        history.record_level_change("m001", "normal", 0.03, 0.65)
        history.record_level_change("m001", "strong_buy", 0.06, 0.78)

        changes = history.get_level_changes("m001")
        assert len(changes) == 2

    def test_get_level_changes_empty(self, history):
        changes = history.get_level_changes("nonexistent")
        assert changes == []

    # ── CLV 结果 ─────────────────────────────────────────────

    def test_record_clv_result(self, history):
        history.save_recommendation(
            "m001", "home_win", "home_win", 0.05, 0.72, "strong_buy"
        )
        ok = history.record_clv_result("m001", "home_win", 2.5, "positive")
        assert ok

    # ── 比赛结果 ─────────────────────────────────────────────

    def test_record_match_result(self, history):
        history.save_recommendation(
            "m001", "home_win", "home_win", 0.05, 0.72, "strong_buy"
        )
        ok = history.record_match_result("m001", "won", 85.0)
        assert ok

    # ── 历史查询 ─────────────────────────────────────────────

    def test_get_history(self, history):
        history.save_recommendation("m001", "home_win", "home_win", 0.05, 0.72, "strong_buy")
        history.save_recommendation("m002", "away_win", "away_win", 0.03, 0.60, "normal")

        recs = history.get_history()
        assert len(recs) >= 2

    def test_get_history_by_date_range(self, history):
        today = datetime.now().strftime("%Y-%m-%d")
        history.save_recommendation("m001", "home_win", "home_win", 0.05, 0.72, "strong_buy")

        recs = history.get_history(start_date=today, end_date=today)
        assert len(recs) >= 1

    def test_get_history_by_level(self, history):
        history.save_recommendation("m001", "home_win", "home_win", 0.05, 0.72, "strong_buy")
        history.save_recommendation("m002", "away_win", "away_win", 0.03, 0.60, "normal")

        strong = history.get_history(level="strong_buy")
        assert all(r["recommendation_level"] == "strong_buy" for r in strong)

    def test_get_history_limit(self, history):
        for i in range(20):
            history.save_recommendation(
                f"m{i:03d}", "home_win", "home_win", 0.05, 0.72, "strong_buy"
            )
        recs = history.get_history(limit=10)
        assert len(recs) == 10

    def test_get_match_history(self, history):
        history.save_recommendation(
            "m001", "home_win", "home_win", 0.05, 0.72, "strong_buy"
        )
        history.record_odds_snapshot("m001", "home_win", 2.0)
        history.record_odds_snapshot("m001", "home_win", 1.85)
        history.record_level_change("m001", "strong_buy", 0.06, 0.78)

        full = history.get_match_history("m001")
        assert full["match_id"] == "m001"
        assert len(full["recommendations"]) >= 1
        assert len(full["odds_history"]) >= 2
        assert len(full["level_changes"]) >= 1

    def test_get_match_history_empty(self, history):
        full = history.get_match_history("nonexistent")
        assert full["match_id"] == "nonexistent"
        assert len(full["recommendations"]) == 0

    # ── 统计 ─────────────────────────────────────────────────

    def test_get_stats(self, history):
        history.save_recommendation("m001", "home_win", "home_win", 0.05, 0.72, "strong_buy")
        history.save_recommendation("m002", "away_win", "away_win", 0.03, 0.60, "normal")

        stats = history.get_stats()
        assert stats["total_recommendations"] >= 2
        assert "average_ev" in stats
        assert "average_confidence" in stats
        assert "by_recommendation_level" in stats
        assert "by_risk_level" in stats

    def test_get_stats_date_range(self, history):
        history.save_recommendation(
            "m001", "home_win", "home_win", 0.05, 0.72, "strong_buy", date="2026-01-01"
        )
        history.save_recommendation(
            "m002", "away_win", "away_win", 0.03, 0.60, "normal", date="2026-06-01"
        )

        stats = history.get_stats(start_date="2026-01-01", end_date="2026-03-01")
        assert stats["total_recommendations"] == 1

    def test_get_stats_empty(self, history):
        stats = history.get_stats()
        assert stats["total"] == 0

    # ── Unicode ───────────────────────────────────────────────

    def test_unicode_history(self, history):
        history.save_recommendation(
            match_id="cn001",
            bet_type="home_win",
            pick="主胜",
            ev=0.05,
            confidence=0.72,
            recommendation_level="strong_buy",
            league="中超",
            home_team="上海申花",
            away_team="北京国安",
        )
        history.record_level_change("cn001", "normal", 0.03, 0.62, reason="赔率变化")
        full = history.get_match_history("cn001")
        assert full["recommendations"][0]["home_team"] == "上海申花"
        assert full["level_changes"][0]["reason"] == "赔率变化"
