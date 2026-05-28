"""
资金仪表盘测试
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from backend.execution.bankroll_dashboard import BankrollDashboard
from backend.execution.execution_tracker import ExecutionTracker, BetStatus


class TestBankrollDashboard:
    """BankrollDashboard 测试"""

    @pytest.fixture
    def dashboard(self, tmp_path):
        db_path = tmp_path / "test_dashboard.db"
        self._seed_data(db_path)
        return BankrollDashboard(str(db_path))

    def _seed_data(self, db_path):
        """填充测试数据"""
        tracker = ExecutionTracker(str(db_path))
        for i in range(1, 6):
            rec_id = tracker.record_recommendation(
                f"match_{i:03d}", "home_win", "home_win", 2.0,
                ev=0.05, confidence=0.70, stake=100.0,
                league="英超", home_team=f"Team_{i}", away_team=f"Visitor_{i}",
            )
            bid = tracker.record_bet(
                f"match_{i:03d}", "home_win", "home_win", 2.0, 100.0, rec_id=rec_id
            )
            # win 3, lose 2
            status = BetStatus.WON.value if i <= 3 else BetStatus.LOST.value
            tracker.settle_bet(bid, status)

    def test_init(self, dashboard):
        assert dashboard.db_path.exists()

    def test_get_settled_bets(self, dashboard):
        bets = dashboard._get_settled_bets()
        assert len(bets) == 5

    def test_equity_curve_user(self, dashboard):
        curve = dashboard.get_equity_curve(initial_capital=10000.0, curve_type="user")
        assert len(curve) >= 1
        assert "equity" in curve[0]
        assert "date" in curve[0]
        assert curve[0]["equity"] == 10000.0

    def test_equity_curve_system(self, dashboard):
        curve = dashboard.get_equity_curve(initial_capital=10000.0, curve_type="system")
        assert len(curve) >= 1

    def test_equity_curve_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        dashboard = BankrollDashboard(str(db_path))
        curve = dashboard.get_equity_curve()
        assert curve == []

    def test_calculate_roi_all(self, dashboard):
        roi = dashboard.calculate_roi("all")
        assert roi["total_bets"] == 5
        assert roi["won"] == 3
        assert roi["period"] == "all"
        assert "roi" in roi
        assert "win_rate" in roi

    def test_calculate_roi_today(self, dashboard):
        roi = dashboard.calculate_roi("today")
        assert roi["period"] == "today"

    def test_calculate_roi_week(self, dashboard):
        roi = dashboard.calculate_roi("week")
        assert roi["period"] == "week"

    def test_calculate_roi_month(self, dashboard):
        roi = dashboard.calculate_roi("month")
        assert roi["period"] == "month"

    def test_calculate_roi_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        dashboard = BankrollDashboard(str(db_path))
        roi = dashboard.calculate_roi()
        assert roi["total_bets"] == 0
        assert roi["roi"] == 0

    def test_max_drawdown(self, dashboard):
        dd = dashboard.calculate_max_drawdown(initial_capital=10000.0)
        assert "max_drawdown" in dd
        assert "max_drawdown_pct" in dd
        assert "peak_equity" in dd
        assert dd["peak_equity"] > 0

    def test_max_drawdown_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        dashboard = BankrollDashboard(str(db_path))
        dd = dashboard.calculate_max_drawdown()
        assert dd["max_drawdown"] == 0

    def test_period_stats_daily(self, dashboard):
        stats = dashboard.get_period_stats("daily")
        assert len(stats) >= 1
        for s in stats:
            assert "period" in s
            assert "pnl" in s
            assert "roi" in s

    def test_period_stats_weekly(self, dashboard):
        stats = dashboard.get_period_stats("weekly")
        assert len(stats) >= 1

    def test_period_stats_monthly(self, dashboard):
        stats = dashboard.get_period_stats("monthly")
        assert len(stats) >= 1

    def test_period_stats_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        dashboard = BankrollDashboard(str(db_path))
        stats = dashboard.get_period_stats()
        assert stats == []

    def test_sharpe_ratio(self, dashboard):
        sharpe = dashboard.calculate_sharpe_ratio()
        assert isinstance(sharpe, (int, float))

    def test_sharpe_ratio_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        dashboard = BankrollDashboard(str(db_path))
        sharpe = dashboard.calculate_sharpe_ratio()
        assert sharpe == 0.0

    def test_calmar_ratio(self, dashboard):
        calmar = dashboard.calculate_calmar_ratio()
        assert isinstance(calmar, (int, float))

    def test_get_dashboard(self, dashboard):
        db = dashboard.get_dashboard(initial_capital=10000.0)
        assert "initial_capital" in db
        assert "current_equity" in db
        assert "roi" in db
        assert "max_drawdown" in db
        assert "sharpe_ratio" in db
        assert "equity_curve" in db
        assert "daily_stats" in db
        assert "generated_at" in db

    def test_get_dashboard_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        dashboard = BankrollDashboard(str(db_path))
        db = dashboard.get_dashboard()
        assert db["current_equity"] == 10000.0
        assert db["total_return_pct"] == 0
