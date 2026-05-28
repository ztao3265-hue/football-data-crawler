"""
推荐过滤器 — 按条件过滤推荐
"""

import json
import sqlite3
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Optional
from enum import Enum

from backend.live.live_prediction_engine import PredictionResult, RecommendationLevel


class LiquidityLevel(Enum):
    """流动性等级"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RecommendationFilter:
    """
    推荐过滤器

    功能：
    - 最低EV过滤
    - 最低confidence过滤
    - 联赛过滤
    - 排除低流动性比赛
    - 每日最大推荐场数
    """

    # 默认配置
    DEFAULT_MIN_EV = 0.02
    DEFAULT_MIN_CONFIDENCE = 0.60
    DEFAULT_MIN_LIQUIDITY = LiquidityLevel.MEDIUM
    DEFAULT_MAX_RECOMMENDATIONS_PER_DAY = 10

    # 高流动性联赛
    HIGH_LIQUIDITY_LEAGUES = [
        "Premier League",
        "La Liga",
        "Bundesliga",
        "Serie A",
        "Ligue 1",
        "Champions League",
        "Europa League",
        "英超",
        "西甲",
        "德甲",
        "意甲",
        "法甲",
        "欧冠",
        "欧联"
    ]

    def __init__(
        self,
        db_path: str = None,
        min_ev: float = DEFAULT_MIN_EV,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        min_liquidity: LiquidityLevel = DEFAULT_MIN_LIQUIDITY,
        max_per_day: int = DEFAULT_MAX_RECOMMENDATIONS_PER_DAY
    ):
        """
        初始化过滤器

        Args:
            db_path: 数据库路径
            min_ev: 最低EV阈值
            min_confidence: 最低置信度阈值
            min_liquidity: 最低流动性等级
            max_per_day: 每日最大推荐数
        """
        if db_path is None:
            from config.paths import DB_RECOMMENDATION
            db_path = str(DB_RECOMMENDATION)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.min_ev = min_ev
        self.min_confidence = min_confidence
        self.min_liquidity = min_liquidity
        self.max_per_day = max_per_day

        self._excluded_leagues: set[str] = set()
        self._included_leagues: Optional[set[str]] = None

        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS filtered_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    filter_date TEXT NOT NULL,
                    recommendation_level TEXT NOT NULL,
                    ev REAL,
                    confidence REAL,
                    league TEXT,
                    liquidity TEXT,
                    passed_filters INTEGER DEFAULT 1,
                    filter_reasons TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_filter_date
                ON filtered_recommendations(filter_date)
            """)

    def set_min_ev(self, min_ev: float):
        """设置最低EV"""
        self.min_ev = min_ev

    def set_min_confidence(self, min_confidence: float):
        """设置最低置信度"""
        self.min_confidence = min_confidence

    def set_min_liquidity(self, min_liquidity: LiquidityLevel):
        """设置最低流动性"""
        self.min_liquidity = min_liquidity

    def set_max_per_day(self, max_per_day: int):
        """设置每日最大推荐数"""
        self.max_per_day = max_per_day

    def exclude_league(self, league: str):
        """排除联赛"""
        self._excluded_leagues.add(league)

    def include_only_leagues(self, leagues: list[str]):
        """仅包含指定联赛"""
        self._included_leagues = set(leagues)

    def clear_league_filters(self):
        """清除联赛过滤"""
        self._excluded_leagues.clear()
        self._included_leagues = None

    def check_ev(self, prediction: PredictionResult) -> tuple[bool, str]:
        """
        检查EV是否达标

        Args:
            prediction: 预测结果

        Returns:
            (是否通过, 原因)
        """
        max_ev = max(prediction.expected_value.values()) if prediction.expected_value else 0

        if max_ev >= self.min_ev:
            return True, f"EV {max_ev:.3f} >= {self.min_ev:.3f}"
        else:
            return False, f"EV {max_ev:.3f} < {self.min_ev:.3f}"

    def check_confidence(self, prediction: PredictionResult) -> tuple[bool, str]:
        """
        检查置信度是否达标

        Args:
            prediction: 预测结果

        Returns:
            (是否通过, 原因)
        """
        if prediction.confidence >= self.min_confidence:
            return True, f"Confidence {prediction.confidence:.2f} >= {self.min_confidence:.2f}"
        else:
            return False, f"Confidence {prediction.confidence:.2f} < {self.min_confidence:.2f}"

    def check_league(self, league: str) -> tuple[bool, str]:
        """
        检查联赛是否允许

        Args:
            league: 联赛名称

        Returns:
            (是否通过, 原因)
        """
        # 检查排除列表
        if league in self._excluded_leagues:
            return False, f"League '{league}' is excluded"

        # 检查包含列表
        if self._included_leagues is not None:
            if league not in self._included_leagues:
                return False, f"League '{league}' not in included list"

        return True, "League allowed"

    def check_liquidity(self, league: str) -> tuple[bool, str]:
        """
        检查流动性是否达标

        Args:
            league: 联赛名称

        Returns:
            (是否通过, 原因)
        """
        liquidity = self._get_league_liquidity(league)
        liquidity_order = [LiquidityLevel.LOW, LiquidityLevel.MEDIUM, LiquidityLevel.HIGH]

        min_index = liquidity_order.index(self.min_liquidity)
        current_index = liquidity_order.index(liquidity)

        if current_index >= min_index:
            return True, f"Liquidity {liquidity.value} >= {self.min_liquidity.value}"
        else:
            return False, f"Liquidity {liquidity.value} < {self.min_liquidity.value}"

    def _get_league_liquidity(self, league: str) -> LiquidityLevel:
        """获取联赛流动性等级"""
        if league in self.HIGH_LIQUIDITY_LEAGUES:
            return LiquidityLevel.HIGH

        # 这里可以接入实际的流动性数据
        # 目前返回中等
        return LiquidityLevel.MEDIUM

    def check_daily_limit(self, target_date: Optional[date] = None) -> tuple[bool, str]:
        """
        检查每日推荐限制

        Args:
            target_date: 目标日期

        Returns:
            (是否通过, 原因)
        """
        if target_date is None:
            target_date = date.today()

        count = self._get_daily_recommendation_count(target_date)

        if count < self.max_per_day:
            return True, f"Daily count {count} < {self.max_per_day}"
        else:
            return False, f"Daily limit reached: {count}/{self.max_per_day}"

    def _get_daily_recommendation_count(self, target_date: date) -> int:
        """获取当日推荐数量"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM filtered_recommendations
                WHERE filter_date = ? AND passed_filters = 1
                """,
                (target_date.isoformat(),)
            )
            return cursor.fetchone()[0]

    def filter(
        self,
        prediction: PredictionResult,
        league: str = "",
        check_daily_limit: bool = True
    ) -> dict[str, Any]:
        """
        执行过滤

        Args:
            prediction: 预测结果
            league: 联赛名称
            check_daily_limit: 是否检查每日限制

        Returns:
            过滤结果
        """
        results = {
            "match_id": prediction.match_id,
            "passed": True,
            "checks": [],
            "reasons": []
        }

        # EV 检查
        passed, reason = self.check_ev(prediction)
        results["checks"].append({"type": "ev", "passed": passed, "reason": reason})
        if not passed:
            results["passed"] = False
            results["reasons"].append(reason)

        # 置信度检查
        passed, reason = self.check_confidence(prediction)
        results["checks"].append({"type": "confidence", "passed": passed, "reason": reason})
        if not passed:
            results["passed"] = False
            results["reasons"].append(reason)

        # 联赛检查
        if league:
            passed, reason = self.check_league(league)
            results["checks"].append({"type": "league", "passed": passed, "reason": reason})
            if not passed:
                results["passed"] = False
                results["reasons"].append(reason)

            # 流动性检查
            passed, reason = self.check_liquidity(league)
            results["checks"].append({"type": "liquidity", "passed": passed, "reason": reason})
            if not passed:
                results["passed"] = False
                results["reasons"].append(reason)

        # 每日限制检查
        if check_daily_limit:
            passed, reason = self.check_daily_limit()
            results["checks"].append({"type": "daily_limit", "passed": passed, "reason": reason})
            if not passed:
                results["passed"] = False
                results["reasons"].append(reason)

        # 保存过滤结果
        self._save_filter_result(
            prediction,
            league,
            results["passed"],
            results["reasons"]
        )

        return results

    def _save_filter_result(
        self,
        prediction: PredictionResult,
        league: str,
        passed: bool,
        reasons: list[str]
    ):
        """保存过滤结果"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO filtered_recommendations
                (match_id, filter_date, recommendation_level, ev, confidence,
                 league, liquidity, passed_filters, filter_reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction.match_id,
                    date.today().isoformat(),
                    prediction.recommendation_level,
                    max(prediction.expected_value.values()) if prediction.expected_value else 0,
                    prediction.confidence,
                    league,
                    self._get_league_liquidity(league).value,
                    1 if passed else 0,
                    json.dumps(reasons, ensure_ascii=False)
                )
            )

    def filter_batch(
        self,
        predictions: list[tuple[PredictionResult, str]]
    ) -> list[dict[str, Any]]:
        """
        批量过滤

        Args:
            predictions: 预测结果和联赛列表 [(prediction, league), ...]

        Returns:
            过滤结果列表
        """
        results = []

        for prediction, league in predictions:
            result = self.filter(prediction, league)
            results.append(result)

            # 如果达到每日限制，停止处理
            if not self.check_daily_limit()[0]:
                break

        return results

    def get_filtered_recommendations(
        self,
        target_date: Optional[date] = None,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        获取过滤后的推荐

        Args:
            target_date: 目标日期
            limit: 返回数量限制

        Returns:
            推荐列表
        """
        if target_date is None:
            target_date = date.today()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT match_id, recommendation_level, ev, confidence, league, liquidity
                FROM filtered_recommendations
                WHERE filter_date = ? AND passed_filters = 1
                ORDER BY ev DESC
                LIMIT ?
                """,
                (target_date.isoformat(), limit)
            )
            rows = cursor.fetchall()

        return [
            {
                "match_id": row[0],
                "recommendation_level": row[1],
                "ev": row[2],
                "confidence": row[3],
                "league": row[4],
                "liquidity": row[5]
            }
            for row in rows
        ]

    def get_filter_stats(self, target_date: Optional[date] = None) -> dict[str, Any]:
        """
        获取过滤统计

        Args:
            target_date: 目标日期

        Returns:
            统计信息
        """
        if target_date is None:
            target_date = date.today()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    passed_filters,
                    COUNT(*) as count
                FROM filtered_recommendations
                WHERE filter_date = ?
                GROUP BY passed_filters
                """,
                (target_date.isoformat(),)
            )
            rows = cursor.fetchall()

        stats = {
            "date": target_date.isoformat(),
            "total": 0,
            "passed": 0,
            "failed": 0
        }

        for row in rows:
            stats["total"] += row[1]
            if row[0] == 1:
                stats["passed"] = row[1]
            else:
                stats["failed"] = row[1]

        return stats

    def reset_daily_count(self, target_date: Optional[date] = None):
        """
        重置每日计数

        Args:
            target_date: 目标日期
        """
        if target_date is None:
            target_date = date.today()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM filtered_recommendations WHERE filter_date = ?",
                (target_date.isoformat(),)
            )

    def get_filter_config(self) -> dict[str, Any]:
        """获取当前过滤配置"""
        return {
            "min_ev": self.min_ev,
            "min_confidence": self.min_confidence,
            "min_liquidity": self.min_liquidity.value,
            "max_per_day": self.max_per_day,
            "excluded_leagues": list(self._excluded_leagues),
            "included_leagues": list(self._included_leagues) if self._included_leagues else None
        }

    def update_config(self, config: dict[str, Any]):
        """
        更新配置

        Args:
            config: 新配置
        """
        if "min_ev" in config:
            self.min_ev = config["min_ev"]
        if "min_confidence" in config:
            self.min_confidence = config["min_confidence"]
        if "min_liquidity" in config:
            self.min_liquidity = LiquidityLevel(config["min_liquidity"])
        if "max_per_day" in config:
            self.max_per_day = config["max_per_day"]
        if "excluded_leagues" in config:
            self._excluded_leagues = set(config["excluded_leagues"])
        if "included_leagues" in config:
            self._included_leagues = set(config["included_leagues"])
