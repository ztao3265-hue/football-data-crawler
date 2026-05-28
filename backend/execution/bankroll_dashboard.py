"""
资金仪表盘 — 资金曲线、ROI、最大回撤、日/周/月统计
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


class BankrollDashboard:
    """
    资金仪表盘

    功能：
    - 系统推荐资金曲线
    - 用户实际资金曲线
    - ROI 计算
    - 最大回撤 (Max Drawdown)
    - 日/周/月收益统计
    - Sharpe Ratio / Calmar Ratio
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from config.paths import DATABASE_DIR
            db_path = str(DATABASE_DIR / "execution_tracking.db")
        self.db_path = Path(db_path)

    def _table_exists(self, table: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone()
            return row is not None

    def _get_settled_bets(self) -> list[dict]:
        if not self._table_exists("user_bets"):
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    """SELECT * FROM user_bets
                       WHERE status IN ('won','lost','half_won','half_lost','void')
                       ORDER BY settled_at ASC"""
                ).fetchall()
            ]

    def _get_recommendations_with_pnl(self) -> list[dict]:
        """获取带模拟盈亏的系统推荐"""
        if not self._table_exists("system_recommendations"):
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM system_recommendations ORDER BY created_at ASC"
                ).fetchall()
            ]

    # ── 资金曲线 ─────────────────────────────────────────────────

    def get_equity_curve(
        self,
        initial_capital: float = 10000.0,
        curve_type: str = "user"
    ) -> list[dict[str, Any]]:
        """
        获取资金曲线

        Args:
            initial_capital: 初始资金
            curve_type: "user" | "system"
        """
        if curve_type == "user":
            bets = self._get_settled_bets()
        else:
            bets = self._get_recommendations_with_pnl()

        if not bets:
            return []

        equity = initial_capital
        curve: list[dict[str, Any]] = [{
            "date": (bets[0].get("settled_at") or bets[0].get("created_at", ""))[:10],
            "equity": initial_capital,
            "pnl": 0.0,
            "cumulative_return": 0.0,
        }]

        for b in bets:
            pnl = b.get("pnl", 0) or 0
            if curve_type == "system" and b.get("stake") and b.get("odds"):
                pnl = b["stake"] * (b["odds"] - 1)
            equity += pnl
            date_str = (b.get("settled_at") or b.get("created_at", ""))[:10]
            curve.append({
                "date": date_str,
                "equity": round(equity, 2),
                "pnl": round(pnl, 2),
                "cumulative_return": round((equity / initial_capital - 1) * 100, 2),
            })

        return curve

    # ── ROI ──────────────────────────────────────────────────────

    def calculate_roi(self, period: str = "all") -> dict[str, Any]:
        """
        计算 ROI

        Args:
            period: "all" | "today" | "week" | "month"
        """
        bets = self._get_settled_bets()

        now = datetime.now()
        if period == "today":
            cutoff = now.strftime("%Y-%m-%d")
            bets = [b for b in bets if (b.get("settled_at") or "")[:10] == cutoff]
        elif period == "week":
            cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            bets = [b for b in bets if (b.get("settled_at") or "")[:10] >= cutoff]
        elif period == "month":
            cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
            bets = [b for b in bets if (b.get("settled_at") or "")[:10] >= cutoff]

        total_stake = sum(b.get("stake", 0) or 0 for b in bets)
        total_pnl = sum(b.get("pnl", 0) or 0 for b in bets)
        won = sum(1 for b in bets if b.get("status") in ("won", "half_won"))
        total = len(bets)

        return {
            "period": period,
            "total_bets": total,
            "won": won,
            "lost": total - won,
            "win_rate": round(won / total * 100, 1) if total > 0 else 0,
            "total_stake": round(total_stake, 2),
            "total_pnl": round(total_pnl, 2),
            "roi": round(total_pnl / total_stake * 100, 2) if total_stake > 0 else 0,
        }

    # ── 最大回撤 ─────────────────────────────────────────────────

    def calculate_max_drawdown(self, initial_capital: float = 10000.0) -> dict[str, Any]:
        curve = self.get_equity_curve(initial_capital, curve_type="user")
        if not curve:
            return {"max_drawdown": 0, "max_drawdown_pct": 0, "peak": initial_capital}

        peak = initial_capital
        max_dd = 0.0
        max_dd_pct = 0.0
        dd_start = ""
        dd_end = ""

        for point in curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            dd = peak - eq
            dd_pct = (dd / peak) * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
                dd_end = point["date"]

        return {
            "peak_equity": round(peak, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "drawdown_end_date": dd_end,
        }

    # ── 日/周/月 统计 ────────────────────────────────────────────

    def get_period_stats(self, period: str = "daily") -> list[dict[str, Any]]:
        """
        按时间粒度统计

        Args:
            period: "daily" | "weekly" | "monthly"
        """
        bets = self._get_settled_bets()
        if not bets:
            return []

        groups: dict[str, dict[str, Any]] = {}
        for b in bets:
            dt_str = b.get("settled_at") or b.get("placed_at") or ""
            if not dt_str:
                continue
            dt = datetime.fromisoformat(dt_str)

            if period == "daily":
                key = dt.strftime("%Y-%m-%d")
            elif period == "weekly":
                key = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
            else:
                key = dt.strftime("%Y-%m")

            if key not in groups:
                groups[key] = {"period": key, "bets": 0, "won": 0, "stake": 0.0, "pnl": 0.0}
            groups[key]["bets"] += 1
            if b.get("status") in ("won", "half_won"):
                groups[key]["won"] += 1
            groups[key]["stake"] += b.get("stake", 0) or 0
            groups[key]["pnl"] += b.get("pnl", 0) or 0

        result = []
        for key, g in sorted(groups.items()):
            g["win_rate"] = round(g["won"] / g["bets"] * 100, 1) if g["bets"] > 0 else 0
            g["roi"] = round(g["pnl"] / g["stake"] * 100, 2) if g["stake"] > 0 else 0
            g["stake"] = round(g["stake"], 2)
            g["pnl"] = round(g["pnl"], 2)
            result.append(g)

        return result

    # ── 高级指标 ─────────────────────────────────────────────────

    def calculate_sharpe_ratio(
        self,
        risk_free_rate: float = 0.02,
        initial_capital: float = 10000.0
    ) -> float:
        """计算 Sharpe Ratio (简易版)"""
        daily_stats = self.get_period_stats("daily")
        if len(daily_stats) < 2:
            return 0.0

        returns = [(s["pnl"] / max(s["stake"], initial_capital * 0.01)) for s in daily_stats]
        if not returns:
            return 0.0

        import statistics
        avg_return = statistics.mean(returns)
        std_return = statistics.stdev(returns) if len(returns) > 1 else 0.01

        if std_return == 0:
            return 0.0

        return round((avg_return - risk_free_rate / 252) / std_return, 2)

    def calculate_calmar_ratio(self, initial_capital: float = 10000.0) -> float:
        """计算 Calmar Ratio (年化收益 / 最大回撤)"""
        roi = self.calculate_roi("all")
        dd = self.calculate_max_drawdown(initial_capital)

        if dd["max_drawdown_pct"] < 0.1:
            return 0.0

        return round(roi["roi"] / dd["max_drawdown_pct"], 2)

    # ── 仪表盘总览 ──────────────────────────────────────────────

    def get_dashboard(self, initial_capital: float = 10000.0) -> dict[str, Any]:
        """获取完整仪表盘数据"""
        roi_all = self.calculate_roi("all")
        roi_month = self.calculate_roi("month")
        roi_week = self.calculate_roi("week")
        dd = self.calculate_max_drawdown(initial_capital)
        curve = self.get_equity_curve(initial_capital)
        daily = self.get_period_stats("daily")
        sharpe = self.calculate_sharpe_ratio(initial_capital=initial_capital)
        calmar = self.calculate_calmar_ratio(initial_capital)

        return {
            "initial_capital": initial_capital,
            "current_equity": round(curve[-1]["equity"], 2) if curve else initial_capital,
            "total_return_pct": round((curve[-1]["equity"] / initial_capital - 1) * 100, 2) if curve else 0,
            "roi": roi_all,
            "roi_this_month": roi_month,
            "roi_this_week": roi_week,
            "max_drawdown": dd,
            "sharpe_ratio": sharpe,
            "calmar_ratio": calmar,
            "equity_curve": curve[-90:] if len(curve) > 90 else curve,
            "daily_stats": daily[-30:],
            "generated_at": datetime.now().isoformat(),
        }
