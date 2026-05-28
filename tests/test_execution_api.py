"""
执行层 API 接口测试
"""
import json
from datetime import datetime

import pytest

from backend.execution.api import ExecutionAPI
from backend.execution.execution_tracker import BetStatus


class TestExecutionAPI:
    """ExecutionAPI 测试"""

    @pytest.fixture
    def api(self, tmp_path):
        import sqlite3
        api = ExecutionAPI()
        # 重定向到临时数据库
        api.tracker.db_path = tmp_path / "test_api_tracker.db"
        api.tracker.db_path.parent.mkdir(parents=True, exist_ok=True)
        api.tracker._init_db()

        api.dashboard.db_path = api.tracker.db_path

        api.generator.db_path = tmp_path / "test_api_generator.db"
        api.generator.db_path.parent.mkdir(parents=True, exist_ok=True)
        api.generator._init_db()

        api.history.db_path = tmp_path / "test_api_history.db"
        api.history.db_path.parent.mkdir(parents=True, exist_ok=True)
        api.history._init_db()

        return api

    @pytest.fixture
    def sample_matches(self):
        return [
            {
                "match_id": "m001",
                "league": "英超",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "kickoff_time": "2026-05-28 20:00",
                "odds": {"home_win": 2.0, "draw": 3.5, "away_win": 3.0},
            },
            {
                "match_id": "m002",
                "league": "西甲",
                "home_team": "Barcelona",
                "away_team": "Real Madrid",
                "kickoff_time": "2026-05-28 22:00",
                "odds": {"home_win": 1.8, "draw": 3.6, "away_win": 4.0},
            },
        ]

    # ── 执行追踪 API ─────────────────────────────────────────

    def test_record_system_recommendation(self, api):
        rec_id = api.record_system_recommendation(
            match_id="m001", bet_type="home_win", pick="home_win", odds=2.0,
            league="英超", ev=0.05, confidence=0.75
        )
        assert rec_id.startswith("REC-")

    def test_record_user_bet(self, api):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        assert bet_id > 0

    def test_settle_bet(self, api):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        ok = api.settle_bet(bet_id, BetStatus.WON.value)
        assert ok

    def test_get_execution_comparison(self, api):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        api.settle_bet(bet_id, BetStatus.WON.value)

        comp = api.get_execution_comparison()
        assert comp["total_recommendations"] >= 1
        assert comp["user_pnl"] > 0

    def test_get_execution_score(self, api):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        api.settle_bet(bet_id, BetStatus.WON.value)

        score = api.get_execution_score()
        assert 0 <= score <= 100

    # ── 资金仪表盘 API ───────────────────────────────────────

    def test_get_dashboard(self, api):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        api.settle_bet(bet_id, BetStatus.WON.value)

        db = api.get_dashboard(initial_capital=10000.0)
        assert "current_equity" in db
        assert "roi" in db
        assert "max_drawdown" in db

    def test_get_equity_curve(self, api):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        api.settle_bet(bet_id, BetStatus.WON.value)

        curve = api.get_equity_curve(initial_capital=10000.0)
        assert len(curve) >= 1

    def test_get_roi(self, api):
        roi = api.get_roi("all")
        assert "period" in roi

    def test_get_max_drawdown(self, api):
        dd = api.get_max_drawdown()
        assert "max_drawdown" in dd

    def test_get_period_stats(self, api):
        stats = api.get_period_stats("daily")
        assert isinstance(stats, list)

    # ── 每日推荐 API ────────────────────────────────────────

    def test_generate_today_recommendations(self, api, sample_matches):
        recs = api.generate_today_recommendations(sample_matches)
        assert len(recs) >= 1

    def test_get_today_picks(self, api, sample_matches):
        api.generate_today_recommendations(sample_matches)
        picks = api.get_today_picks()
        assert len(picks) >= 1

    def test_get_strongest_picks(self, api, sample_matches):
        api.generate_today_recommendations(sample_matches)
        picks = api.get_strongest_picks()
        assert isinstance(picks, list)

    def test_get_ev_ranking(self, api, sample_matches):
        api.generate_today_recommendations(sample_matches)
        ranked = api.get_ev_ranking(top_n=1)
        assert len(ranked) <= 1

    def test_get_confidence_ranking(self, api, sample_matches):
        api.generate_today_recommendations(sample_matches)
        ranked = api.get_confidence_ranking(top_n=1)
        assert len(ranked) <= 1

    def test_get_low_risk_picks(self, api, sample_matches):
        api.generate_today_recommendations(sample_matches)
        picks = api.get_low_risk_picks()
        assert isinstance(picks, list)

    # ── 推荐历史 API ────────────────────────────────────────

    def test_save_to_history(self, api):
        rid = api.save_to_history(
            "m001", "home_win", "home_win",
            ev=0.05, confidence=0.72, level="strong_buy",
            league="英超",
        )
        assert rid > 0

    def test_record_odds_change(self, api):
        sid = api.record_odds_change("m001", "home_win", 2.0)
        assert sid > 0

    def test_record_level_change(self, api):
        cid = api.record_level_change("m001", "strong_buy", 0.06, 0.78)
        assert cid > 0

    def test_get_match_full_history(self, api):
        api.save_to_history("m001", "home_win", "home_win", 0.05, 0.72, "strong_buy")
        api.record_odds_change("m001", "home_win", 2.0)
        api.record_level_change("m001", "strong_buy", 0.06, 0.78)

        full = api.get_match_full_history("m001")
        assert full["match_id"] == "m001"
        assert len(full["recommendations"]) >= 1

    def test_get_history_stats(self, api):
        api.save_to_history("m001", "home_win", "home_win", 0.05, 0.72, "strong_buy")
        api.save_to_history("m002", "away_win", "away_win", 0.03, 0.60, "normal")

        stats = api.get_history_stats()
        assert stats["total_recommendations"] >= 2

    # ── 综合报告 ────────────────────────────────────────────

    def test_get_full_report(self, api, sample_matches):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        api.settle_bet(bet_id, BetStatus.WON.value)
        api.generate_today_recommendations(sample_matches)
        api.save_to_history("m001", "home_win", "home_win", 0.05, 0.72, "strong_buy")

        report = api.get_full_report()
        assert "report_date" in report
        assert "execution" in report
        assert "bankroll" in report
        assert "today_picks" in report
        assert "history_stats" in report
        assert "generated_at" in report

    def test_export_json(self, api, sample_matches):
        rec_id = api.record_system_recommendation("m001", "home_win", "home_win", 2.0)
        bet_id = api.record_user_bet("m001", "home_win", "home_win", 2.0, 100.0, rec_id)
        api.settle_bet(bet_id, BetStatus.WON.value)
        api.generate_today_recommendations(sample_matches)

        json_str = api.export_json()
        assert isinstance(json_str, str)
        data = json.loads(json_str)
        assert "report_date" in data
        assert "execution" in data

    def test_export_json_empty(self, api):
        json_str = api.export_json()
        data = json.loads(json_str)
        assert "report_date" in data
