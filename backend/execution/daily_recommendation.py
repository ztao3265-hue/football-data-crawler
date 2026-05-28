"""
每日推荐生成器 — 今日推荐、EV排名、confidence排名、风险等级、最强精选
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


class DailyRecommendationGenerator:
    """
    每日推荐生成器

    功能：
    - 今日推荐比赛列表
    - EV 排名 (Expected Value)
    - Confidence 排名
    - 风险等级评估
    - Strongest Picks (最强精选)
    - 推荐组合建议
    """

    RISK_LEVELS = {
        "low": {"max_stake_pct": 0.05, "min_confidence": 0.70, "min_ev": 0.04},
        "medium": {"max_stake_pct": 0.03, "min_confidence": 0.60, "min_ev": 0.02},
        "high": {"max_stake_pct": 0.01, "min_confidence": 0.50, "min_ev": 0.01},
    }

    def __init__(
        self,
        db_path: str = None,
        prediction_engine=None,
        odds_collector=None
    ):
        if db_path is None:
            from config.paths import DATABASE_DIR
            db_path = str(DATABASE_DIR / "daily_recommendations.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.prediction_engine = prediction_engine
        self.odds_collector = odds_collector
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS daily_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    league TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    kickoff_time TEXT,
                    bet_type TEXT NOT NULL,
                    pick TEXT NOT NULL,
                    odds REAL NOT NULL,
                    ev REAL NOT NULL,
                    confidence REAL NOT NULL,
                    recommendation_level TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    ev_rank INTEGER,
                    confidence_rank INTEGER,
                    is_strongest_pick INTEGER DEFAULT 0,
                    suggested_stake REAL,
                    reason TEXT,
                    odds_snapshot TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_recommendations(date);
                CREATE INDEX IF NOT EXISTS idx_daily_level ON daily_recommendations(recommendation_level);
                CREATE INDEX IF NOT EXISTS idx_daily_match ON daily_recommendations(match_id, date);
            """)

    # ── 风险等级评估 ─────────────────────────────────────────────

    def assess_risk_level(
        self,
        ev: float,
        confidence: float,
        odds: float,
        league: str = ""
    ) -> str:
        """
        评估风险等级

        考虑因素：EV值、置信度、赔率高低、联赛知名度
        """
        score = 0

        if ev >= 0.05:
            score += 3
        elif ev >= 0.02:
            score += 2
        else:
            score += 1

        if confidence >= 0.75:
            score += 3
        elif confidence >= 0.60:
            score += 2
        else:
            score += 1

        if 1.5 <= odds <= 2.5:
            score += 2
        else:
            score += 1

        high_quality_leagues = [
            "Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1",
            "英超", "西甲", "德甲", "意甲", "法甲"
        ]
        if any(l in league for l in high_quality_leagues):
            score += 1

        if score >= 7:
            return "low"
        elif score >= 5:
            return "medium"
        else:
            return "high"

    # ── 推荐生成 ─────────────────────────────────────────────────

    def generate_daily_recommendations(
        self,
        matches: list[dict[str, Any]],
        date: Optional[str] = None,
        bankroll: float = 10000.0
    ) -> list[dict[str, Any]]:
        """
        生成每日推荐

        Args:
            matches: 比赛数据列表，每个包含 match_id, odds, league 等
            date: 日期 (默认今天)
            bankroll: 当前资金

        Returns:
            推荐列表
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        recommendations = []

        for m in matches:
            rec = self._evaluate_match(m, bankroll)
            if rec and rec["recommendation_level"] != "pass":
                recommendations.append(rec)

        # EV 排名
        recommendations.sort(key=lambda r: r["ev"], reverse=True)
        for i, r in enumerate(recommendations):
            r["ev_rank"] = i + 1

        # Confidence 排名
        sorted_by_conf = sorted(recommendations, key=lambda r: r["confidence"], reverse=True)
        conf_ranks = {r["match_id"]: i + 1 for i, r in enumerate(sorted_by_conf)}

        for r in recommendations:
            r["confidence_rank"] = conf_ranks[r["match_id"]]

        # 标记最强精选 (同时满足: EV前20% + 低风险 + strong_buy)
        ev_threshold = sorted([r["ev"] for r in recommendations], reverse=True)[
            max(0, int(len(recommendations) * 0.2) - 1)
        ] if recommendations else 0

        for r in recommendations:
            if (
                r["ev"] >= ev_threshold
                and r["risk_level"] == "low"
                and r["recommendation_level"] == "strong_buy"
            ):
                r["is_strongest_pick"] = True
            else:
                r["is_strongest_pick"] = False

        # 保存到数据库
        self._save_recommendations(recommendations, date)

        return recommendations

    def _evaluate_match(
        self,
        match: dict[str, Any],
        bankroll: float
    ) -> Optional[dict[str, Any]]:
        """评估单场比赛"""
        match_id = match.get("match_id", "")
        odds = match.get("odds", {})
        league = match.get("league", "")

        if not odds:
            return None

        # 使用预测引擎计算 EV 和 confidence
        if self.prediction_engine:
            try:
                prediction = self.prediction_engine.predict(match_id, odds)
                ev_dict = prediction.expected_value
                confidence = prediction.confidence
                level = prediction.recommendation_level
            except Exception:
                ev_dict, confidence, level = self._simple_evaluate(odds)
        else:
            ev_dict, confidence, level = self._simple_evaluate(odds)

        best_bet_type = max(ev_dict, key=ev_dict.get)
        best_ev = ev_dict[best_bet_type]
        best_odds = odds.get(best_bet_type, 2.0)

        risk_level = self.assess_risk_level(best_ev, confidence, best_odds, league)

        suggested_stake = self._suggest_stake(bankroll, risk_level, best_ev, confidence)

        return {
            "match_id": match_id,
            "league": league,
            "home_team": match.get("home_team", ""),
            "away_team": match.get("away_team", ""),
            "kickoff_time": match.get("kickoff_time", ""),
            "bet_type": best_bet_type,
            "pick": best_bet_type,
            "odds": best_odds,
            "ev": round(best_ev, 4),
            "confidence": round(confidence, 3),
            "recommendation_level": level,
            "risk_level": risk_level,
            "suggested_stake": round(suggested_stake, 2),
            "reason": self._generate_reason(best_bet_type, best_ev, confidence, risk_level),
            "odds_snapshot": json.dumps(odds, ensure_ascii=False),
        }

    def _simple_evaluate(self, odds: dict) -> tuple[dict, float, str]:
        """简易评估 (无需预测引擎) — 对低赔方向给予小额价值加成"""
        from backend.live.live_prediction_engine import RecommendationLevel

        ev_dict = {}
        vals = [(k, v) for k, v in odds.items() if isinstance(v, (int, float)) and v > 1.0]
        if not vals:
            return {"home_win": 0.0}, 0.4, RecommendationLevel.PASS

        # 按赔率排序，低赔=更可能赢
        vals.sort(key=lambda x: x[1])
        n = len(vals)

        # 简易概率分配: 赔率越低，概率越高，并且叠加一点主场/低赔优势
        total_implied = sum(1.0 / v for _, v in vals)
        raw_probs = [(k, (1.0 / v) / total_implied) for k, v in vals]

        # 对前 N-1 个选项 (低赔方向) 给予概率加成
        for i, (k, prob) in enumerate(raw_probs):
            if i < n - 1:
                prob += 0.04  # 低赔方向加成 4%
            ev = prob * dict(vals)[k] - 1.0
            ev_dict[k] = round(ev, 4)

        best_ev = max(ev_dict.values())
        best_key = max(ev_dict, key=ev_dict.get)
        confidence = min(0.82, max(0.48, 0.55 + best_ev * 3))

        if best_ev >= 0.02 and confidence >= 0.65:
            level = RecommendationLevel.STRONG_BUY
        elif best_ev >= 0.0 and confidence >= 0.50:
            level = RecommendationLevel.NORMAL
        else:
            level = RecommendationLevel.PASS

        return ev_dict, confidence, level

    def _suggest_stake(
        self,
        bankroll: float,
        risk_level: str,
        ev: float,
        confidence: float
    ) -> float:
        """建议投注金额 (Kelly 分数法)"""
        risk_cfg = self.RISK_LEVELS.get(risk_level, self.RISK_LEVELS["medium"])
        base_pct = risk_cfg["max_stake_pct"]

        kelly_pct = max(0, ev) * confidence
        final_pct = min(base_pct, kelly_pct)

        return bankroll * final_pct

    def _generate_reason(
        self,
        bet_type: str,
        ev: float,
        confidence: float,
        risk_level: str
    ) -> str:
        """生成推荐理由"""
        reasons = []
        if ev >= 0.05:
            reasons.append(f"高期望值 EV={ev:.1%}")
        elif ev >= 0.02:
            reasons.append(f"正期望值 EV={ev:.1%}")
        else:
            reasons.append(f"边际期望值 EV={ev:.1%}")

        if confidence >= 0.75:
            reasons.append("高置信度")
        elif confidence >= 0.60:
            reasons.append("中等置信度")

        risk_labels = {"low": "低风险", "medium": "中风险", "high": "高风险"}
        reasons.append(risk_labels.get(risk_level, "中风险"))

        return "; ".join(reasons)

    def _save_recommendations(self, recommendations: list[dict], date: str):
        """保存推荐到数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM daily_recommendations WHERE date = ?", (date,))
            for r in recommendations:
                conn.execute(
                    """INSERT INTO daily_recommendations
                       (date, match_id, league, home_team, away_team, kickoff_time,
                        bet_type, pick, odds, ev, confidence, recommendation_level,
                        risk_level, ev_rank, confidence_rank, is_strongest_pick,
                        suggested_stake, reason, odds_snapshot)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        date, r["match_id"], r.get("league", ""),
                        r.get("home_team", ""), r.get("away_team", ""),
                        r.get("kickoff_time", ""),
                        r["bet_type"], r["pick"], r["odds"],
                        r["ev"], r["confidence"], r["recommendation_level"],
                        r["risk_level"], r["ev_rank"], r["confidence_rank"],
                        1 if r.get("is_strongest_pick") else 0,
                        r["suggested_stake"], r.get("reason", ""),
                        r.get("odds_snapshot", "{}")
                    )
                )

    # ── 查询接口 ─────────────────────────────────────────────────

    def get_today_recommendations(self) -> list[dict[str, Any]]:
        """获取今日推荐"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self._query_by_date(today)

    def get_recommendations_by_date(self, date: str) -> list[dict[str, Any]]:
        """按日期获取推荐"""
        return self._query_by_date(date)

    def _query_by_date(self, date: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    """SELECT * FROM daily_recommendations
                       WHERE date = ? ORDER BY ev DESC""",
                    (date,)
                ).fetchall()
            ]

    def get_strongest_picks(self, date: Optional[str] = None) -> list[dict[str, Any]]:
        """获取最强精选"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    """SELECT * FROM daily_recommendations
                       WHERE date = ? AND is_strongest_pick = 1
                       ORDER BY ev DESC""",
                    (date,)
                ).fetchall()
            ]

    def get_ranked_by_ev(
        self, date: Optional[str] = None, top_n: int = 10
    ) -> list[dict[str, Any]]:
        """按 EV 排名"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    """SELECT * FROM daily_recommendations
                       WHERE date = ? ORDER BY ev DESC LIMIT ?""",
                    (date, top_n)
                ).fetchall()
            ]

    def get_ranked_by_confidence(
        self, date: Optional[str] = None, top_n: int = 10
    ) -> list[dict[str, Any]]:
        """按 Confidence 排名"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    """SELECT * FROM daily_recommendations
                       WHERE date = ? ORDER BY confidence DESC LIMIT ?""",
                    (date, top_n)
                ).fetchall()
            ]

    def get_by_risk_level(
        self, risk_level: str, date: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """按风险等级过滤"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    """SELECT * FROM daily_recommendations
                       WHERE date = ? AND risk_level = ?
                       ORDER BY ev DESC""",
                    (date, risk_level)
                ).fetchall()
            ]

    def get_summary(self, date: Optional[str] = None) -> dict[str, Any]:
        """获取每日推荐摘要"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        recs = self._query_by_date(date)
        if not recs:
            return {"date": date, "count": 0}

        strong = [r for r in recs if r["recommendation_level"] == "strong_buy"]
        picks = [r for r in recs if r.get("is_strongest_pick")]

        avg_ev = sum(r["ev"] for r in recs) / len(recs)
        avg_conf = sum(r["confidence"] for r in recs) / len(recs)

        return {
            "date": date,
            "total": len(recs),
            "strong_buy": len(strong),
            "normal": len(recs) - len(strong),
            "strongest_picks": len(picks),
            "average_ev": round(avg_ev, 4),
            "average_confidence": round(avg_conf, 3),
            "total_suggested_stake": round(sum(r["suggested_stake"] for r in recs), 2),
            "by_risk": {
                level: len([r for r in recs if r["risk_level"] == level])
                for level in ["low", "medium", "high"]
            },
        }
