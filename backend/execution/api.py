"""
执行层 API 接口 — 为 football-betting-analysis 主系统调用准备
"""
import json
from datetime import datetime
from typing import Any, Optional

from backend.execution.execution_tracker import ExecutionTracker, BetStatus, BetResult
from backend.execution.bankroll_dashboard import BankrollDashboard
from backend.execution.daily_recommendation import DailyRecommendationGenerator
from backend.execution.recommendation_history import RecommendationHistory


class ExecutionAPI:
    """
    执行层统一 API 接口

    为 football-betting-analysis 主系统提供：
    - 执行追踪数据
    - 资金曲线 & 仪表盘
    - 每日推荐
    - 推荐历史
    """

    def __init__(
        self,
        execution_tracker: Optional[ExecutionTracker] = None,
        bankroll_dashboard: Optional[BankrollDashboard] = None,
        daily_generator: Optional[DailyRecommendationGenerator] = None,
        history: Optional[RecommendationHistory] = None,
    ):
        self.tracker = execution_tracker or ExecutionTracker()
        self.dashboard = bankroll_dashboard or BankrollDashboard()
        self.generator = daily_generator or DailyRecommendationGenerator()
        self.history = history or RecommendationHistory()

    # ── 执行追踪 API ─────────────────────────────────────────────

    def record_system_recommendation(
        self,
        match_id: str,
        bet_type: str,
        pick: str,
        odds: float,
        **kwargs
    ) -> str:
        """记录系统推荐 → 返回 rec_id"""
        return self.tracker.record_recommendation(
            match_id=match_id, bet_type=bet_type, pick=pick, odds=odds, **kwargs
        )

    def record_user_bet(
        self,
        match_id: str,
        bet_type: str,
        pick: str,
        odds: float,
        stake: float,
        rec_id: str = "",
        **kwargs
    ) -> int:
        """记录用户投注 → 返回 bet_id"""
        return self.tracker.record_bet(
            match_id=match_id, bet_type=bet_type, pick=pick,
            odds=odds, stake=stake, rec_id=rec_id, **kwargs
        )

    def settle_bet(self, bet_id: int, status: str, pnl: Optional[float] = None) -> bool:
        """结算投注"""
        return self.tracker.settle_bet(bet_id, status, pnl)

    def get_execution_comparison(self, date: Optional[str] = None) -> dict[str, Any]:
        """获取系统 vs 用户执行对比"""
        return self.tracker.compare_execution(date)

    def get_execution_score(self, date: Optional[str] = None) -> float:
        """获取执行质量评分"""
        return self.tracker.calculate_execution_score(date)

    # ── 资金仪表盘 API ───────────────────────────────────────────

    def get_dashboard(self, initial_capital: float = 10000.0) -> dict[str, Any]:
        """获取完整仪表盘"""
        return self.dashboard.get_dashboard(initial_capital)

    def get_equity_curve(
        self, initial_capital: float = 10000.0, curve_type: str = "user"
    ) -> list[dict]:
        """获取资金曲线"""
        return self.dashboard.get_equity_curve(initial_capital, curve_type)

    def get_roi(self, period: str = "all") -> dict[str, Any]:
        """获取 ROI"""
        return self.dashboard.calculate_roi(period)

    def get_max_drawdown(self, initial_capital: float = 10000.0) -> dict[str, Any]:
        """获取最大回撤"""
        return self.dashboard.calculate_max_drawdown(initial_capital)

    def get_period_stats(self, period: str = "daily") -> list[dict]:
        """获取日/周/月统计"""
        return self.dashboard.get_period_stats(period)

    # ── 每日推荐 API ─────────────────────────────────────────────

    def generate_today_recommendations(
        self, matches: list[dict], bankroll: float = 10000.0
    ) -> list[dict]:
        """生成今日推荐"""
        return self.generator.generate_daily_recommendations(matches, bankroll=bankroll)

    def get_today_picks(self) -> list[dict]:
        """获取今日推荐"""
        return self.generator.get_today_recommendations()

    def get_strongest_picks(self, date: Optional[str] = None) -> list[dict]:
        """获取最强精选"""
        return self.generator.get_strongest_picks(date)

    def get_ev_ranking(self, date: Optional[str] = None, top_n: int = 10) -> list[dict]:
        """按 EV 排名"""
        return self.generator.get_ranked_by_ev(date, top_n)

    def get_confidence_ranking(self, date: Optional[str] = None, top_n: int = 10) -> list[dict]:
        """按 Confidence 排名"""
        return self.generator.get_ranked_by_confidence(date, top_n)

    def get_low_risk_picks(self, date: Optional[str] = None) -> list[dict]:
        """获取低风险推荐"""
        return self.generator.get_by_risk_level("low", date)

    # ── 推荐历史 API ─────────────────────────────────────────────

    def save_to_history(
        self,
        match_id: str,
        bet_type: str,
        pick: str,
        ev: float,
        confidence: float,
        level: str,
        **kwargs
    ) -> int:
        """保存到推荐历史"""
        return self.history.save_recommendation(
            match_id=match_id, bet_type=bet_type, pick=pick,
            ev=ev, confidence=confidence, recommendation_level=level, **kwargs
        )

    def record_odds_change(
        self, match_id: str, bet_type: str, odds: float
    ) -> int:
        """记录盘口变化"""
        return self.history.record_odds_snapshot(match_id, bet_type, odds)

    def record_level_change(
        self,
        match_id: str,
        new_level: str,
        new_ev: float,
        new_confidence: float,
        **kwargs
    ) -> int:
        """记录推荐等级变化"""
        return self.history.record_level_change(
            match_id, new_level, new_ev, new_confidence, **kwargs
        )

    def get_match_full_history(self, match_id: str) -> dict[str, Any]:
        """获取比赛完整历史"""
        return self.history.get_match_history(match_id)

    def get_history_stats(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> dict[str, Any]:
        """获取历史统计"""
        return self.history.get_stats(start_date, end_date)

    # ── 综合报告 API ─────────────────────────────────────────────

    def get_full_report(self, date: Optional[str] = None) -> dict[str, Any]:
        """生成完整执行报告 (供主系统消费)"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        execution = self.get_execution_comparison(date)
        dashboard = self.get_dashboard()
        picks = self.generator.get_recommendations_by_date(date)
        strongest = self.generator.get_strongest_picks(date)
        hist_stats = self.get_history_stats()

        return {
            "report_date": date,
            "generated_at": datetime.now().isoformat(),
            "execution": execution,
            "bankroll": dashboard,
            "today_picks": {
                "total": len(picks),
                "strongest": strongest,
                "by_ev": self.get_ev_ranking(date, 5),
                "by_confidence": self.get_confidence_ranking(date, 5),
                "low_risk": self.get_low_risk_picks(date),
            },
            "history_stats": hist_stats,
        }

    def export_json(self, date: Optional[str] = None) -> str:
        """导出 JSON 格式报告"""
        return json.dumps(
            self.get_full_report(date), ensure_ascii=False, indent=2, default=str
        )
