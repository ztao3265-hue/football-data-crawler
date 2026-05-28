"""
每日自动流水线 (Daily Pipeline)

功能:
- 自动扫描今日即将开赛的比赛
- 自动采集赔率数据 (欧赔/亚盘/大小球)
- 自动运行ML模型 (XGBoost/LightGBM/CatBoost)
- 自动生成推荐 (今日精选/Top5/低风险/高EV)
- 去重 & 风险过滤
- 保存到数据库
- 生成每日报告

用法:
    python -m backend.engine.daily_pipeline --today
    python -m backend.engine.daily_pipeline --date 2026-05-28 --source sofascore
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class DailyPipeline:
    """每日自动推荐流水线"""

    def __init__(self):
        self.engine = None
        self._init_engine()

    def _init_engine(self):
        from backend.engine.recommendation_engine import UnifiedRecommendationEngine
        self.engine = UnifiedRecommendationEngine()

    # ── 比赛扫描 ──────────────────────────────────────────────────

    def scan_upcoming_matches(
        self,
        target_date: Optional[str] = None,
        source: str = "sofascore",
        hours_ahead: int = 48,
    ) -> list[dict[str, Any]]:
        """
        扫描即将开赛的比赛

        数据来源优先级:
        1. live_odds.db (RealtimeOddsCollector 已采集)
        2. PostgreSQL (crawler 已爬取)
        3. football_data.org API
        """
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        matches = []

        # 来源1: live_odds.db
        matches.extend(self._scan_live_odds_db(target_date))

        # 来源2: PostgreSQL
        if not matches:
            matches.extend(self._scan_postgresql(target_date))

        # 去重 (按 match_id)
        seen = set()
        unique = []
        for m in matches:
            mid = m.get("match_id", "")
            if mid and mid not in seen:
                seen.add(mid)
                unique.append(m)

        return unique

    def _scan_live_odds_db(self, target_date: str) -> list[dict[str, Any]]:
        """从 live_odds.db 扫描"""
        import sqlite3
        from config.paths import DB_LIVE_ODDS

        db_path = str(DB_LIVE_ODDS)
        if not Path(db_path).exists():
            return []

        matches = []
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT match_id, home_team, away_team, league, match_time, status
                       FROM matches
                       WHERE status = 'upcoming'
                       ORDER BY match_time ASC"""
                ).fetchall()

            for row in rows:
                match = dict(row)
                # 获取最新赔率
                odds = self._get_latest_odds_from_db(db_path, match["match_id"])
                match["odds"] = odds
                match["kickoff_time"] = match.pop("match_time", "")
                matches.append(match)
        except Exception as e:
            print(f"  [WARN] live_odds.db 扫描失败: {e}")

        return matches

    def _scan_postgresql(self, target_date: str) -> list[dict[str, Any]]:
        """从 PostgreSQL 扫描 (通过 crawler API)"""
        # PostgreSQL 可能未运行，返回空
        return []

    def _get_latest_odds_from_db(self, db_path: str, match_id: str) -> dict[str, Any]:
        """从数据库获取最新赔率"""
        import sqlite3

        odds = {}
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT odds_type, data FROM odds
                       WHERE match_id = ? ORDER BY collected_at DESC""",
                    (match_id,)
                ).fetchall()

            for row in rows:
                try:
                    data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                    if row["odds_type"] == "european":
                        odds.update({
                            "home_win": data.get("home_win", 2.5),
                            "draw": data.get("draw", 3.5),
                            "away_win": data.get("away_win", 3.0),
                        })
                    elif row["odds_type"] == "asian":
                        odds["asian"] = {
                            "handicap": data.get("handicap", 0),
                            "home_odds": data.get("home_odds", 0.9),
                            "away_odds": data.get("away_odds", 0.9),
                        }
                    elif row["odds_type"] == "over_under":
                        odds["over_under"] = {
                            "line": data.get("line", 2.5),
                            "over_odds": data.get("over_odds", 0.9),
                            "under_odds": data.get("under_odds", 0.9),
                        }
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass

        # 确保基础字段存在
        odds.setdefault("home_win", 2.5)
        odds.setdefault("draw", 3.5)
        odds.setdefault("away_win", 3.0)

        return odds

    # ── 手动添加比赛 ──────────────────────────────────────────────

    def add_manual_match(
        self,
        home_team: str,
        away_team: str,
        league: str,
        kickoff_time: str,
        odds_home: float = 2.5,
        odds_draw: float = 3.5,
        odds_away: float = 3.0,
        asian_handicap: float = 0.0,
        asian_home_odds: float = 0.9,
        asian_away_odds: float = 0.9,
        ou_line: float = 2.5,
        ou_over: float = 0.9,
        ou_under: float = 0.9,
    ) -> dict[str, Any]:
        """手动添加一场比赛到流水线"""
        import hashlib

        match_id = hashlib.sha256(
            f"{home_team}|{away_team}|{kickoff_time}".encode()
        ).hexdigest()[:16]

        return {
            "match_id": match_id,
            "home_team": home_team,
            "away_team": away_team,
            "league": league,
            "kickoff_time": kickoff_time,
            "odds": {
                "home_win": odds_home,
                "draw": odds_draw,
                "away_win": odds_away,
                "asian": {
                    "handicap": asian_handicap,
                    "home_odds": asian_home_odds,
                    "away_odds": asian_away_odds,
                },
                "over_under": {
                    "line": ou_line,
                    "over_odds": ou_over,
                    "under_odds": ou_under,
                },
            },
        }

    # ── 执行 ──────────────────────────────────────────────────────

    def run(
        self,
        target_date: Optional[str] = None,
        matches: Optional[list[dict[str, Any]]] = None,
        bankroll: float = 10000.0,
        output_json: bool = False,
    ) -> dict[str, Any]:
        """
        执行每日流水线

        Args:
            target_date: 目标日期 (默认今天)
            matches: 比赛列表 (不传则自动扫描)
            bankroll: 资金量
            output_json: 是否输出 JSON 到 stdout
        """
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        print(f"\n{'='*60}")
        print(f"  每日自动推荐系统 — {target_date}")
        print(f"{'='*60}")

        # 1. 扫描比赛
        if matches is None:
            print(f"\n[1/5] 扫描即将开赛比赛...")
            matches = self.scan_upcoming_matches(target_date)
        else:
            print(f"\n[1/5] 使用提供的 {len(matches)} 场比赛")

        print(f"  找到 {len(matches)} 场比赛")

        if not matches:
            print("  今天没有即将开赛的比赛。")
            return {"date": target_date, "total": 0, "message": "今天没有即将开赛的比赛"}

        for m in matches[:5]:
            print(f"    - {m.get('league', '?'):20s} {m.get('home_team', '?'):15s} vs {m.get('away_team', '?')}")

        if len(matches) > 5:
            print(f"    ... 还有 {len(matches) - 5} 场")

        # 2. ML模型状态
        print(f"\n[2/5] ML模型状态...")
        status = self.engine.get_engine_status()
        print(f"  ML模型可用: {status['ml_models_available']}")
        if status['ml_models_available']:
            for task, models in status['ml_models'].items():
                if models:
                    print(f"    {task}: {', '.join(models)}")

        # 3. 执行流水线
        print(f"\n[3/5] 执行推荐流水线...")
        report = self.engine.run_daily_pipeline(matches, target_date, bankroll)

        # 4. 输出结果
        print(f"\n[4/5] 推荐结果:")
        s = report["summary"]
        print(f"  总推荐: {s['total_recommendations']}")
        print(f"  强烈推荐: {s['strong_buy']} | 最强精选: {s['strongest_picks']}")
        print(f"  Top5: {s['top5']} | 低风险: {s['low_risk']} | 高EV: {s['high_ev']}")
        print(f"  平均EV: {s['average_ev']:.3%} | 平均置信度: {s['average_confidence']:.2%}")
        print(f"  ML驱动: {s['ml_powered']} | Steam Move: {s['steam_move_alerts']} | Sharp Money: {s['sharp_money_alerts']}")

        if report.get("correlation_warnings"):
            print(f"\n  [关联风险警告]")
            for w in report["correlation_warnings"]:
                print(f"    ! {w}")

        # 5. 输出 Top5
        print(f"\n[5/5] Top5 推荐:")
        top5 = report.get("top5", [])
        if top5:
            for i, r in enumerate(top5, 1):
                print(
                    f"  {i}. {r.get('league', '?'):20s} "
                    f"{r.get('home_team', '?'):12s} vs {r.get('away_team', '?'):12s} "
                    f"| {r.get('pick', '?'):10s} "
                    f"| EV={r.get('ev', 0):.3%} "
                    f"| 风险={r.get('risk_level', '?')}"
                )
        else:
            print("  (暂无)")

        print(f"\n  流水线耗时: {report['pipeline']['elapsed_seconds']:.1f}s")
        print(f"{'='*60}\n")

        if output_json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

        return report


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="每日自动推荐系统")
    parser.add_argument("--today", action="store_true", default=True, help="运行今日流水线")
    parser.add_argument("--date", type=str, default=None, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--bankroll", type=float, default=10000.0, help="资金量")
    parser.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    parser.add_argument("--scan-only", action="store_true", help="仅扫描比赛, 不生成推荐")
    parser.add_argument("--status", action="store_true", help="查看引擎状态")
    parser.add_argument("--summary", type=str, default=None, help="查看某日摘要 YYYY-MM-DD")

    args = parser.parse_args()

    pipeline = DailyPipeline()

    if args.status:
        status = pipeline.engine.get_engine_status()
        print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
        return

    if args.summary:
        summary = pipeline.engine.get_daily_summary(args.summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return

    if args.scan_only:
        target = args.date or datetime.now().strftime("%Y-%m-%d")
        matches = pipeline.scan_upcoming_matches(target)
        print(f"找到 {len(matches)} 场比赛:")
        for m in matches:
            print(f"  {m.get('match_id', '?')}: {m.get('league', '?')} | "
                  f"{m.get('home_team', '?')} vs {m.get('away_team', '?')} | "
                  f"{m.get('kickoff_time', '?')}")
        return

    target = args.date or datetime.now().strftime("%Y-%m-%d")
    pipeline.run(target_date=target, bankroll=args.bankroll, output_json=args.json)


if __name__ == "__main__":
    main()
