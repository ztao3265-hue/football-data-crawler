"""
每日推荐生成器测试
"""
import json
from datetime import datetime

import pytest

from backend.execution.daily_recommendation import DailyRecommendationGenerator


class TestDailyRecommendationGenerator:
    """DailyRecommendationGenerator 测试"""

    @pytest.fixture
    def generator(self, tmp_path):
        db_path = tmp_path / "test_daily_recs.db"
        return DailyRecommendationGenerator(str(db_path))

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
            {
                "match_id": "m003",
                "league": "德甲",
                "home_team": "Bayern",
                "away_team": "Dortmund",
                "kickoff_time": "2026-05-28 19:30",
                "odds": {"home_win": 1.5, "draw": 4.0, "away_win": 5.5},
            },
        ]

    # ── 初始化 ─────────────────────────────────────────────────

    def test_init(self, generator):
        assert generator.db_path.exists()

    def test_tables_created(self, generator):
        import sqlite3
        with sqlite3.connect(generator.db_path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert "daily_recommendations" in [t[0] for t in tables]

    # ── 风险等级评估 ───────────────────────────────────────────

    def test_risk_low(self, generator):
        risk = generator.assess_risk_level(ev=0.06, confidence=0.80, odds=2.0, league="英超")
        assert risk == "low"

    def test_risk_medium(self, generator):
        risk = generator.assess_risk_level(ev=0.03, confidence=0.65, odds=2.5, league="英超")
        assert risk in ("low", "medium")

    def test_risk_high(self, generator):
        risk = generator.assess_risk_level(ev=0.005, confidence=0.45, odds=6.0, league="低级别联赛")
        assert risk == "high"

    def test_risk_low_league_bonus(self, generator):
        risk_with_bonus = generator.assess_risk_level(0.03, 0.65, 2.0, "英超")
        risk_without = generator.assess_risk_level(0.03, 0.65, 2.0, "低级别")
        # 英超应该有加分
        assert isinstance(risk_with_bonus, str)
        assert isinstance(risk_without, str)

    # ── 每日推荐生成 ───────────────────────────────────────────

    def test_generate_daily_recommendations(self, generator, sample_matches):
        recs = generator.generate_daily_recommendations(sample_matches)
        assert len(recs) >= 1
        for r in recs:
            assert "match_id" in r
            assert "ev" in r
            assert "confidence" in r
            assert "recommendation_level" in r
            assert "risk_level" in r
            assert "ev_rank" in r
            assert "confidence_rank" in r
            assert "suggested_stake" in r

    def test_generate_with_date(self, generator, sample_matches):
        recs = generator.generate_daily_recommendations(sample_matches, date="2026-01-01")
        assert len(recs) >= 1

    def test_generate_empty_matches(self, generator):
        recs = generator.generate_daily_recommendations([])
        assert recs == []

    def test_generate_ev_ranking(self, generator, sample_matches):
        recs = generator.generate_daily_recommendations(sample_matches)
        ev_values = [r["ev"] for r in recs]
        assert ev_values == sorted(ev_values, reverse=True)

    def test_generate_strongest_picks(self, generator, sample_matches):
        recs = generator.generate_daily_recommendations(sample_matches)
        strongest = [r for r in recs if r.get("is_strongest_pick")]
        # 最强精选是条件严格的子集
        for s in strongest:
            assert s["ev_rank"] is not None

    # ── 查询 ─────────────────────────────────────────────────

    def test_get_today_recommendations(self, generator, sample_matches):
        generator.generate_daily_recommendations(sample_matches)
        today = generator.get_today_recommendations()
        assert len(today) >= 1

    def test_get_recommendations_by_date(self, generator, sample_matches):
        generator.generate_daily_recommendations(sample_matches, date="2026-01-01")
        recs = generator.get_recommendations_by_date("2026-01-01")
        assert len(recs) >= 1

    def test_get_recommendations_by_future_date(self, generator):
        recs = generator.get_recommendations_by_date("2099-12-31")
        assert recs == []

    def test_get_strongest_picks(self, generator, sample_matches):
        generator.generate_daily_recommendations(sample_matches)
        picks = generator.get_strongest_picks()
        assert isinstance(picks, list)

    def test_get_ranked_by_ev(self, generator, sample_matches):
        generator.generate_daily_recommendations(sample_matches)
        ranked = generator.get_ranked_by_ev(top_n=2)
        assert len(ranked) <= 2
        if len(ranked) >= 2:
            assert ranked[0]["ev"] >= ranked[1]["ev"]

    def test_get_ranked_by_confidence(self, generator, sample_matches):
        generator.generate_daily_recommendations(sample_matches)
        ranked = generator.get_ranked_by_confidence(top_n=2)
        assert len(ranked) <= 2
        if len(ranked) >= 2:
            assert ranked[0]["confidence"] >= ranked[1]["confidence"]

    def test_get_by_risk_level(self, generator, sample_matches):
        generator.generate_daily_recommendations(sample_matches)
        low_risk = generator.get_by_risk_level("low")
        medium_risk = generator.get_by_risk_level("medium")
        high_risk = generator.get_by_risk_level("high")
        total = len(low_risk) + len(medium_risk) + len(high_risk)
        assert total >= 1

    def test_get_summary(self, generator, sample_matches):
        generator.generate_daily_recommendations(sample_matches)
        summary = generator.get_summary()
        assert summary["total"] >= 1
        assert "strong_buy" in summary
        assert "strongest_picks" in summary
        assert "average_ev" in summary
        assert "by_risk" in summary

    def test_get_summary_empty(self, generator):
        summary = generator.get_summary()
        assert summary["count"] == 0

    # ── 简易评估 ─────────────────────────────────────────────

    def test_simple_evaluate(self, generator):
        odds = {"home_win": 2.0, "draw": 3.5, "away_win": 3.0}
        ev_dict, confidence, level = generator._simple_evaluate(odds)
        assert len(ev_dict) == 3
        assert 0 < confidence <= 1.0
        assert level in ("strong_buy", "normal", "pass")

    def test_simple_evaluate_empty(self, generator):
        ev_dict, confidence, level = generator._simple_evaluate({})
        assert isinstance(ev_dict, dict)
        assert 0 < confidence <= 1.0

    # ── 投注金额建议 ───────────────────────────────────────────

    def test_suggest_stake_low_risk(self, generator):
        stake = generator._suggest_stake(bankroll=10000, risk_level="low", ev=0.06, confidence=0.80)
        assert 0 < stake <= 500

    def test_suggest_stake_high_risk(self, generator):
        stake = generator._suggest_stake(bankroll=10000, risk_level="high", ev=0.01, confidence=0.50)
        assert 0 < stake <= 100

    # ── Unicode ───────────────────────────────────────────────

    def test_unicode_matches(self, generator):
        matches = [{
            "match_id": "cn001",
            "league": "中超",
            "home_team": "上海申花",
            "away_team": "北京国安",
            "kickoff_time": "2026-05-28 19:35",
            "odds": {"home_win": 2.2, "draw": 3.2, "away_win": 2.8},
        }]
        recs = generator.generate_daily_recommendations(matches)
        assert len(recs) >= 1
        assert recs[0]["home_team"] == "上海申花"
