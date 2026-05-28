"""
统一推荐引擎 (Unified Recommendation Engine)

整合:
- 赔率采集 (RealtimeOddsCollector)
- ML模型预测 (MLPredictor)
- 市场信号分析 (MarketAnalyzer)
- 推荐生成 (DailyRecommendationGenerator)
- 去重/风控 (DedupAndRiskFilter)
- 历史追踪 (RecommendationHistory)
"""
import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Optional

from backend.engine.ml_predictor import MLPredictor
from backend.engine.market_analyzer import MarketAnalyzer
from backend.engine.dedup import DedupAndRiskFilter
from backend.execution.daily_recommendation import DailyRecommendationGenerator
from backend.live.live_prediction_engine import LivePredictionEngine, RecommendationLevel
from backend.live.recommendation_filter import RecommendationFilter


class UnifiedRecommendationEngine:
    """
    统一推荐引擎

    提供一站式接口:
    - run_daily_pipeline() → 完整的每日推荐工作流
    - get_today_picks() → 今日精选
    - get_top5() → Top5推荐
    - get_low_risk() → 低风险推荐
    - get_high_ev() → 高EV推荐
    - get_history() → 历史追踪
    """

    def __init__(
        self,
        models_dir: str = None,
        main_db_path: str = None,
        odds_collector=None,
    ):
        # 子引擎
        self.ml_predictor = MLPredictor(models_dir)
        self.market_analyzer = MarketAnalyzer()
        self.prediction_engine = LivePredictionEngine()
        self.recommendation_generator = DailyRecommendationGenerator(
            prediction_engine=self.prediction_engine
        )
        self.filter = RecommendationFilter()
        self.dedup = DedupAndRiskFilter()

        # 注册 ML 模型到预测引擎
        if self.ml_predictor.available:
            self._register_ml_models()

        # 主数据库
        if main_db_path is None:
            from config.paths import DATABASE_DIR
            main_db_path = str(DATABASE_DIR / "unified_recommendations.db")
        self.db_path = Path(main_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_main_db()

        # 外部采集器引用
        self.odds_collector = odds_collector

        # 今日缓存
        self._today_cache: Optional[dict[str, Any]] = None
        self._cache_date: Optional[str] = None

    def _init_main_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS unified_recommendations (
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
                    is_top5 INTEGER DEFAULT 0,
                    is_low_risk INTEGER DEFAULT 0,
                    is_high_ev INTEGER DEFAULT 0,
                    suggested_stake REAL,
                    ml_source TEXT,
                    steam_move_detected INTEGER DEFAULT 0,
                    sharp_money_detected INTEGER DEFAULT 0,
                    market_score INTEGER DEFAULT 0,
                    market_risk TEXT,
                    reason TEXT,
                    odds_snapshot TEXT,
                    ml_predictions TEXT,
                    market_signals TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, match_id, bet_type)
                );

                CREATE INDEX IF NOT EXISTS idx_ur_date ON unified_recommendations(date);
                CREATE INDEX IF NOT EXISTS idx_ur_level ON unified_recommendations(recommendation_level);
                CREATE INDEX IF NOT EXISTS idx_ur_risk ON unified_recommendations(risk_level);
                CREATE INDEX IF NOT EXISTS idx_ur_top5 ON unified_recommendations(date, is_top5);
                CREATE INDEX IF NOT EXISTS idx_ur_ev ON unified_recommendations(date, ev DESC);
            """)

    def _register_ml_models(self):
        """将ML模型注册到预测引擎"""

        def make_ml_predict_fn(ml_predictor):
            def predict_fn(match_data: dict) -> dict:
                odds = match_data.get("odds", {})
                result = ml_predictor.get_ensemble_prediction(odds)
                return result
            return predict_fn

        self.prediction_engine.register_prediction_model(
            "ml_ensemble", make_ml_predict_fn(self.ml_predictor)
        )

    # ── 核心: 每日流水线 ──────────────────────────────────────────

    def run_daily_pipeline(
        self,
        matches: list[dict[str, Any]],
        target_date: Optional[str] = None,
        bankroll: float = 10000.0,
    ) -> dict[str, Any]:
        """
        执行每日完整流水线:

        1. 对每场比赛采集/接收赔率
        2. 运行 ML 模型预测
        3. 执行市场分析 (Steam Move / Sharp Money / CLV)
        4. 生成推荐列表
        5. 去重 + 风险过滤
        6. 分类: 今日精选 / Top5 / 低风险 / 高EV
        7. 保存到数据库
        8. 返回完整报告
        """
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        pipeline_start = datetime.now()
        pipeline_log = []

        # ── Step 1-3: 评估每场比赛 ──
        evaluated = []
        for m in matches:
            match_id = m.get("match_id", "")
            odds_data = m.get("odds", {})

            if not odds_data:
                continue

            # Market analysis
            self.market_analyzer.feed_odds_snapshot(match_id, odds_data)
            market = self.market_analyzer.full_analysis(match_id, odds_data)

            # ML prediction
            ml_result = None
            if self.ml_predictor.available:
                try:
                    ml_result = self.ml_predictor.get_ensemble_prediction(odds_data)
                except Exception:
                    ml_result = None

            # 基础评估
            if self.prediction_engine:
                try:
                    prediction = self.prediction_engine.predict(
                        match_id, odds_data, model_name="ml_ensemble" if self.ml_predictor.available else "default"
                    )
                    ev_dict = prediction.expected_value
                    confidence = prediction.confidence
                    level = prediction.recommendation_level
                except Exception:
                    ev_dict, confidence, level = self._basic_evaluate(odds_data)
            else:
                ev_dict, confidence, level = self._basic_evaluate(odds_data)

            best_bet_type = max(ev_dict, key=ev_dict.get)
            best_ev = ev_dict[best_bet_type]
            best_odds = odds_data.get(best_bet_type, odds_data.get("home_win", 2.0))

            if isinstance(best_odds, dict):
                best_odds = 2.0

            risk_level = self.recommendation_generator.assess_risk_level(
                best_ev, confidence, best_odds, m.get("league", "")
            )

            evaluated.append({
                "match_id": match_id,
                "league": m.get("league", ""),
                "home_team": m.get("home_team", ""),
                "away_team": m.get("away_team", ""),
                "kickoff_time": m.get("kickoff_time", ""),
                "bet_type": best_bet_type,
                "pick": best_bet_type,
                "odds": round(float(best_odds), 3),
                "ev": round(best_ev, 4),
                "confidence": round(confidence, 3),
                "recommendation_level": level,
                "risk_level": risk_level,
                "odds_snapshot": json.dumps(odds_data, ensure_ascii=False),
                "ml_source": ml_result.get("source", "rule_based") if ml_result else "rule_based",
                "steam_move": market["steam_move"]["detected"],
                "sharp_money": market["sharp_money"]["detected"],
                "market_score": market["market_score"],
                "market_risk": market["market_risk"],
                "market_signals": json.dumps(market, ensure_ascii=False),
                "ml_predictions": json.dumps(ml_result, ensure_ascii=False) if ml_result else "{}",
            })

        pipeline_log.append(f"Step 1-3: 评估了 {len(evaluated)} 场比赛")

        # ── Step 4: 生成推荐 ──
        recommendations = self.recommendation_generator.generate_daily_recommendations(
            evaluated, date=target_date, bankroll=bankroll
        )
        pipeline_log.append(f"Step 4: 生成 {len(recommendations)} 条推荐")

        # ── Step 5: 去重 + 风险过滤 ──
        clean_recs, filter_report = self.dedup.filter(recommendations)
        pipeline_log.append(
            f"Step 5: 去重/风控 → {filter_report['passed']} 条通过 "
            f"(移除 {filter_report['duplicates_removed']} 重复 + {filter_report['anomaly_rejected']} 异常)"
        )

        # ── Step 6: 分类 ──
        self._classify_recommendations(clean_recs, target_date)

        # ── Step 7: 保存 ──
        self._save_to_db(clean_recs, target_date)
        pipeline_log.append(f"Step 7: 保存 {len(clean_recs)} 条到数据库")

        # ── Step 8: 构建报告 ──
        pipeline_elapsed = (datetime.now() - pipeline_start).total_seconds()
        report = self._build_daily_report(clean_recs, filter_report, pipeline_log, pipeline_elapsed, target_date)

        # 更新缓存
        self._today_cache = report
        self._cache_date = target_date

        return report

    def _basic_evaluate(self, odds_data: dict) -> tuple[dict, float, str]:
        """基础评估 fallback"""
        home = float(odds_data.get("home_win", 2.5))
        draw = float(odds_data.get("draw", 3.5))
        away = float(odds_data.get("away_win", 3.0))

        if home <= 0 or draw <= 0 or away <= 0:
            return {"home_win": 0.0}, 0.5, RecommendationLevel.PASS

        total = 1 / home + 1 / draw + 1 / away
        probs = {"home_win": (1 / home) / total, "draw": (1 / draw) / total, "away_win": (1 / away) / total}
        ev = {k: round(probs[k] * v - 1, 4) for k, v in [("home_win", home), ("draw", draw), ("away_win", away)]}
        confidence = min(0.85, max(0.35, 1.0 - (total - 1.0) * 3))

        best_ev = max(ev.values())
        if best_ev >= 0.05 and confidence >= 0.75:
            level = RecommendationLevel.STRONG_BUY
        elif best_ev >= 0.02 and confidence >= 0.60:
            level = RecommendationLevel.NORMAL
        else:
            level = RecommendationLevel.PASS

        return ev, confidence, level

    def _classify_recommendations(self, recs: list[dict], target_date: str):
        """分类标记: 今日精选 / Top5 / 低风险 / 高EV"""
        # Top5: EV最高的5个
        sorted_by_ev = sorted(recs, key=lambda r: r.get("ev", 0), reverse=True)
        top5_ids = {r["match_id"] for r in sorted_by_ev[:5]}

        for r in recs:
            r["is_top5"] = r["match_id"] in top5_ids
            r["is_low_risk"] = r.get("risk_level") == "low"
            r["is_high_ev"] = r.get("ev", 0) >= 0.05
            r["is_strongest_pick"] = (
                r.get("is_strongest_pick", False)
                or (r["is_top5"] and r["is_low_risk"])
            )

    def _save_to_db(self, recommendations: list[dict], target_date: str):
        """保存到统一推荐数据库"""
        with sqlite3.connect(str(self.db_path)) as conn:
            for r in recommendations:
                conn.execute(
                    """INSERT OR REPLACE INTO unified_recommendations
                       (date, match_id, league, home_team, away_team, kickoff_time,
                        bet_type, pick, odds, ev, confidence, recommendation_level,
                        risk_level, ev_rank, confidence_rank, is_strongest_pick,
                        is_top5, is_low_risk, is_high_ev, suggested_stake,
                        ml_source, steam_move_detected, sharp_money_detected,
                        market_score, market_risk, reason, odds_snapshot,
                        ml_predictions, market_signals)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        target_date, r["match_id"], r.get("league", ""),
                        r.get("home_team", ""), r.get("away_team", ""),
                        r.get("kickoff_time", ""),
                        r.get("bet_type", ""), r.get("pick", ""),
                        r.get("odds", 0), r.get("ev", 0), r.get("confidence", 0),
                        r.get("recommendation_level", ""), r.get("risk_level", ""),
                        r.get("ev_rank", 0), r.get("confidence_rank", 0),
                        1 if r.get("is_strongest_pick") else 0,
                        1 if r.get("is_top5") else 0,
                        1 if r.get("is_low_risk") else 0,
                        1 if r.get("is_high_ev") else 0,
                        r.get("suggested_stake", 0),
                        r.get("ml_source", "rule_based"),
                        1 if r.get("steam_move") else 0,
                        1 if r.get("sharp_money") else 0,
                        r.get("market_score", 0),
                        r.get("market_risk", "normal"),
                        r.get("reason", ""),
                        r.get("odds_snapshot", "{}"),
                        r.get("ml_predictions", "{}"),
                        r.get("market_signals", "{}"),
                    )
                )

    def _build_daily_report(
        self, recs: list[dict], filter_report: dict,
        pipeline_log: list, elapsed: float, target_date: str
    ) -> dict[str, Any]:
        """构建每日报告"""
        top5 = [r for r in recs if r.get("is_top5")]
        strongest = [r for r in recs if r.get("is_strongest_pick")]
        low_risk = [r for r in recs if r.get("is_low_risk")]
        high_ev = [r for r in recs if r.get("is_high_ev")]
        strong_buy = [r for r in recs if r.get("recommendation_level") == "strong_buy"]

        avg_ev = sum(r.get("ev", 0) for r in recs) / len(recs) if recs else 0
        avg_conf = sum(r.get("confidence", 0) for r in recs) / len(recs) if recs else 0

        ml_count = sum(1 for r in recs if r.get("ml_source") == "ml_ensemble")
        steam_count = sum(1 for r in recs if r.get("steam_move"))
        sharp_count = sum(1 for r in recs if r.get("sharp_money"))

        return {
            "date": target_date,
            "generated_at": datetime.now().isoformat(),
            "pipeline": {
                "elapsed_seconds": round(elapsed, 1),
                "log": pipeline_log,
            },
            "summary": {
                "total_recommendations": len(recs),
                "strong_buy": len(strong_buy),
                "strongest_picks": len(strongest),
                "top5": len(top5),
                "low_risk": len(low_risk),
                "high_ev": len(high_ev),
                "average_ev": round(avg_ev, 4),
                "average_confidence": round(avg_conf, 3),
                "ml_powered": ml_count,
                "steam_move_alerts": steam_count,
                "sharp_money_alerts": sharp_count,
            },
            "filter_stats": filter_report,
            "today_picks": recs,
            "top5": top5,
            "strongest_picks": strongest,
            "low_risk": low_risk,
            "high_ev": high_ev,
            "correlation_warnings": filter_report.get("correlation_risk", {}).get("warnings", []),
        }

    # ── 查询接口 ──────────────────────────────────────────────────

    def get_today_picks(self) -> list[dict[str, Any]]:
        return self._query("WHERE date = ? ORDER BY ev DESC", [self._today()])

    def get_top5(self, target_date: Optional[str] = None) -> list[dict[str, Any]]:
        d = target_date or self._today()
        return self._query("WHERE date = ? AND is_top5 = 1 ORDER BY ev DESC", [d])

    def get_low_risk(self, target_date: Optional[str] = None) -> list[dict[str, Any]]:
        d = target_date or self._today()
        return self._query("WHERE date = ? AND is_low_risk = 1 ORDER BY ev DESC", [d])

    def get_high_ev(self, target_date: Optional[str] = None) -> list[dict[str, Any]]:
        d = target_date or self._today()
        return self._query("WHERE date = ? AND is_high_ev = 1 ORDER BY ev DESC", [d])

    def get_strongest(self, target_date: Optional[str] = None) -> list[dict[str, Any]]:
        d = target_date or self._today()
        return self._query("WHERE date = ? AND is_strongest_pick = 1 ORDER BY ev DESC", [d])

    def get_by_risk(self, risk_level: str, target_date: Optional[str] = None) -> list[dict[str, Any]]:
        d = target_date or self._today()
        return self._query("WHERE date = ? AND risk_level = ? ORDER BY ev DESC", [d, risk_level])

    def get_history(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        conditions = []
        params = []
        if start_date:
            conditions.append("date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("date <= ?")
            params.append(end_date)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        where += " ORDER BY date DESC, ev DESC LIMIT ?"
        params.append(limit)
        return self._query(where, params)

    def get_daily_summary(self, target_date: Optional[str] = None) -> dict[str, Any]:
        d = target_date or self._today()
        recs = self._query("WHERE date = ?", [d])

        if not recs:
            return {"date": d, "total": 0, "message": "今天暂无推荐"}

        return {
            "date": d,
            "total": len(recs),
            "strong_buy": sum(1 for r in recs if r.get("recommendation_level") == "strong_buy"),
            "top5": sum(1 for r in recs if r.get("is_top5")),
            "low_risk": sum(1 for r in recs if r.get("is_low_risk")),
            "high_ev": sum(1 for r in recs if r.get("is_high_ev")),
            "strongest": sum(1 for r in recs if r.get("is_strongest_pick")),
            "average_ev": round(sum(r.get("ev", 0) for r in recs) / len(recs), 4),
            "average_confidence": round(sum(r.get("confidence", 0) for r in recs) / len(recs), 3),
            "steam_move_alerts": sum(1 for r in recs if r.get("steam_move_detected")),
            "sharp_money_alerts": sum(1 for r in recs if r.get("sharp_money_detected")),
        }

    def get_history_stats(self, days: int = 30) -> dict[str, Any]:
        """获取历史统计数据"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        recs = self._query("WHERE date >= ?", [cutoff])

        if not recs:
            return {"days": days, "total": 0}

        dates_set = sorted(set(r["date"] for r in recs))
        daily_counts = {}
        for r in recs:
            daily_counts[r["date"]] = daily_counts.get(r["date"], 0) + 1

        ev_values = [r.get("ev", 0) for r in recs if r.get("ev")]

        return {
            "days": days,
            "total_recommendations": len(recs),
            "active_days": len(dates_set),
            "avg_daily_count": round(len(recs) / max(len(dates_set), 1), 1),
            "avg_ev": round(sum(ev_values) / len(ev_values), 4) if ev_values else 0,
            "max_ev": round(max(ev_values), 4) if ev_values else 0,
            "date_range": {"start": dates_set[0] if dates_set else None, "end": dates_set[-1] if dates_set else None},
            "daily_distribution": daily_counts,
        }

    # ── 辅助 ──────────────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _query(self, where: str, params: list) -> list[dict[str, Any]]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM unified_recommendations {where}", params
            ).fetchall()
        return [dict(r) for r in rows]

    def record_push(self, match_id: str, bet_type: str, pick: str, odds: float):
        """记录推送"""
        self.dedup.record_push(match_id, bet_type, pick, odds)

    def get_engine_status(self) -> dict[str, Any]:
        return {
            "ml_models_available": self.ml_predictor.available,
            "ml_models": self.ml_predictor.get_available_models(),
            "filter_config": self.filter.get_filter_config(),
            "database": str(self.db_path),
        }
