"""
执行追踪系统测试
"""
import json
from datetime import datetime

import pytest

from backend.execution.execution_tracker import (
    ExecutionTracker,
    BetStatus,
    BetResult,
)


class TestExecutionTracker:
    """ExecutionTracker 测试"""

    @pytest.fixture
    def tracker(self, tmp_path):
        db_path = tmp_path / "test_execution.db"
        return ExecutionTracker(str(db_path))

    # ── 初始化 ─────────────────────────────────────────────────

    def test_init(self, tracker):
        assert tracker.db_path.exists()

    def test_tables_created(self, tracker):
        import sqlite3
        with sqlite3.connect(tracker.db_path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]
            assert "system_recommendations" in table_names
            assert "user_bets" in table_names
            assert "execution_quality" in table_names

    # ── 系统推荐 ─────────────────────────────────────────────

    def test_record_recommendation(self, tracker):
        rec_id = tracker.record_recommendation(
            match_id="match_001",
            bet_type="home_win",
            pick="home_win",
            odds=2.0,
            league="英超",
            home_team="Arsenal",
            away_team="Chelsea",
            ev=0.05,
            confidence=0.75,
            recommendation_level="strong_buy",
        )
        assert rec_id.startswith("REC-")

    def test_record_recommendation_minimal(self, tracker):
        rec_id = tracker.record_recommendation(
            match_id="match_002",
            bet_type="over_2_5",
            pick="over",
            odds=1.85,
        )
        assert rec_id.startswith("REC-")

    def test_get_recommendations(self, tracker):
        tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        tracker.record_recommendation("m2", "away_win", "away_win", 3.5)
        recs = tracker.get_recommendations()
        assert len(recs) >= 2

    def test_get_recommendations_by_level(self, tracker):
        tracker.record_recommendation(
            "m1", "home_win", "home_win", 2.0, recommendation_level="strong_buy"
        )
        tracker.record_recommendation(
            "m2", "away_win", "away_win", 3.5, recommendation_level="normal"
        )
        strong = tracker.get_recommendations(level="strong_buy")
        assert all(r["recommendation_level"] == "strong_buy" for r in strong)

    def test_get_recommendations_by_date(self, tracker):
        today = datetime.now().strftime("%Y-%m-%d")
        tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        recs = tracker.get_recommendations(date=today)
        assert len(recs) >= 1

    # ── 用户投注 ─────────────────────────────────────────────

    def test_record_bet(self, tracker):
        bet_id = tracker.record_bet(
            match_id="match_001",
            bet_type="home_win",
            pick="home_win",
            odds=2.0,
            stake=100.0,
            rec_id="REC-test",
        )
        assert bet_id > 0

    def test_record_bet_without_rec(self, tracker):
        bet_id = tracker.record_bet(
            match_id="match_001",
            bet_type="home_win",
            pick="home_win",
            odds=2.0,
            stake=50.0,
            result="manual",
        )
        assert bet_id > 0

    def test_settle_bet_won(self, tracker):
        bet_id = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0)
        ok = tracker.settle_bet(bet_id, BetStatus.WON.value)
        assert ok

        bets = tracker.get_user_bets()
        won_bet = [b for b in bets if b["id"] == bet_id][0]
        assert won_bet["status"] == "won"
        assert won_bet["pnl"] == 100.0

    def test_settle_bet_lost(self, tracker):
        bet_id = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0)
        tracker.settle_bet(bet_id, BetStatus.LOST.value)

        bets = tracker.get_user_bets()
        lost_bet = [b for b in bets if b["id"] == bet_id][0]
        assert lost_bet["pnl"] == -100.0

    def test_settle_bet_half_won(self, tracker):
        bet_id = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0)
        tracker.settle_bet(bet_id, BetStatus.HALF_WON.value)

        bets = tracker.get_user_bets()
        hw_bet = [b for b in bets if b["id"] == bet_id][0]
        assert hw_bet["pnl"] == 50.0

    def test_settle_bet_half_lost(self, tracker):
        bet_id = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0)
        tracker.settle_bet(bet_id, BetStatus.HALF_LOST.value)

        bets = tracker.get_user_bets()
        hl_bet = [b for b in bets if b["id"] == bet_id][0]
        assert hl_bet["pnl"] == -50.0

    def test_settle_bet_custom_pnl(self, tracker):
        bet_id = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0)
        tracker.settle_bet(bet_id, BetStatus.WON.value, actual_pnl=85.0)

        bets = tracker.get_user_bets()
        b = [x for x in bets if x["id"] == bet_id][0]
        assert b["pnl"] == 85.0

    def test_settle_nonexistent_bet(self, tracker):
        ok = tracker.settle_bet(9999, BetStatus.WON.value)
        assert not ok

    def test_get_user_bets_by_status(self, tracker):
        tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0)
        bid = tracker.record_bet("m2", "away_win", "away_win", 3.0, 50.0)
        tracker.settle_bet(bid, BetStatus.WON.value)

        pending = tracker.get_user_bets(status="pending")
        won = tracker.get_user_bets(status="won")
        assert len(pending) >= 1
        assert len(won) >= 1

    def test_get_user_bets_by_result(self, tracker):
        tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0, result="manual")
        tracker.record_bet("m2", "home_win", "home_win", 2.0, 50.0, result="followed")

        manual = tracker.get_user_bets(result="manual")
        followed = tracker.get_user_bets(result="followed")
        assert len(manual) >= 1
        assert len(followed) >= 1

    # ── 执行偏差分析 ───────────────────────────────────────────

    def test_compare_execution(self, tracker):
        rec_id = tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        bid = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0, rec_id=rec_id)
        tracker.settle_bet(bid, BetStatus.WON.value)

        comp = tracker.compare_execution()
        assert comp["total_recommendations"] >= 1
        assert comp["total_bets"] >= 1
        assert comp["followed"] >= 1
        assert "follow_rate" in comp
        assert "user_pnl" in comp

    def test_compare_execution_with_skipped(self, tracker):
        tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        tracker.record_recommendation("m2", "away_win", "away_win", 3.0)
        # 只跟注 m1, 跳过 m2
        rec_id = tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0, rec_id=rec_id)

        comp = tracker.compare_execution()
        assert comp["skipped"] >= 1

    def test_execution_score(self, tracker):
        rec_id = tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        bid = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0, rec_id=rec_id)
        tracker.settle_bet(bid, BetStatus.WON.value)

        score = tracker.calculate_execution_score()
        assert 0 <= score <= 100

    def test_execution_score_empty(self, tracker):
        score = tracker.calculate_execution_score()
        assert score == 100.0

    def test_save_daily_quality(self, tracker):
        rec_id = tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        bid = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0, rec_id=rec_id)
        tracker.settle_bet(bid, BetStatus.WON.value)

        tracker.save_daily_quality()

        quality = tracker.get_quality_history()
        assert len(quality) >= 1

    # ── 汇总 ─────────────────────────────────────────────────

    def test_get_summary(self, tracker):
        tracker.record_recommendation("m1", "home_win", "home_win", 2.0)
        tracker.record_recommendation("m2", "away_win", "away_win", 3.0)
        bid = tracker.record_bet("m1", "home_win", "home_win", 2.0, 100.0)
        tracker.settle_bet(bid, BetStatus.WON.value)

        summary = tracker.get_summary()
        assert summary["total_recommendations"] >= 2
        assert summary["total_bets"] >= 1
        assert summary["settled_bets"] >= 1

    def test_get_summary_empty(self, tracker):
        summary = tracker.get_summary()
        assert summary["total_recommendations"] == 0
        assert summary["total_bets"] == 0

    # ── BetStatus / BetResult ──────────────────────────────────

    def test_bet_status_enum(self):
        assert BetStatus.WON.value == "won"
        assert BetStatus.LOST.value == "lost"
        assert BetStatus.PENDING.value == "pending"

    def test_bet_result_enum(self):
        assert BetResult.FOLLOWED.value == "followed"
        assert BetResult.MANUAL.value == "manual"
        assert BetResult.SKIPPED.value == "skipped"

    # ── Unicode ───────────────────────────────────────────────

    def test_unicode_recommendation(self, tracker):
        rec_id = tracker.record_recommendation(
            match_id="match_cn",
            bet_type="home_win",
            pick="主胜",
            odds=1.95,
            league="英超",
            home_team="阿森纳",
            away_team="切尔西",
            reason="强队主场优势",
        )
        recs = tracker.get_recommendations()
        cn = [r for r in recs if r["rec_id"] == rec_id][0]
        assert cn["home_team"] == "阿森纳"
        assert cn["reason"] == "强队主场优势"
