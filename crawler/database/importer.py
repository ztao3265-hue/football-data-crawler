"""数据导入器 — clean_matches 自动入库 + 重复检测"""

import json
from pathlib import Path
from datetime import datetime
from typing import List

from sqlalchemy import select, exists
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from crawler.database.connection import get_db
from crawler.database.schema import (
    Match, Odds, Team, League,
    generate_match_id, generate_team_id, generate_league_id, Base
)
from crawler.core.logger import get_logger

logger = get_logger(__name__)


class MatchImporter:
    """比赛数据导入器"""

    def __init__(self):
        self.db = get_db()
        self.stats = {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    def import_matches(self, matches: list[dict]) -> dict:
        """批量导入 clean matches 到数据库"""
        if not matches:
            logger.warning("没有数据可导入")
            return self.stats

        with self.db.session() as session:
            for match_data in matches:
                try:
                    self._import_one(session, match_data)
                except Exception as e:
                    self.stats["errors"] += 1
                    logger.error(f"导入失败: {match_data.get('home_team','?')} vs {match_data.get('away_team','?')}: {e}")

        logger.info(
            f"导入完成: 新增 {self.stats['inserted']}, "
            f"更新 {self.stats['updated']}, "
            f"跳过 {self.stats['skipped']}, "
            f"错误 {self.stats['errors']}"
        )
        return self.stats

    def _import_one(self, session: Session, data: dict):
        """导入单条比赛数据"""
        home_name = data.get("home_team", "").strip()
        away_name = data.get("away_team", "").strip()
        source = data.get("source", "")
        kickoff_str = data.get("kickoff_time", "")

        if not home_name or not away_name:
            self.stats["skipped"] += 1
            return

        # 生成唯一 match_id
        match_id = generate_match_id(source, home_name, away_name, kickoff_str)

        # 检查重复
        existing = session.execute(
            select(Match).where(Match.match_id == match_id)
        ).scalar_one_or_none()

        if existing:
            # 更新已有记录（合并赔率等）
            self._update_match(session, existing, data)
            self.stats["updated"] += 1
            return

        # 处理联赛
        league_name = data.get("league", "").strip()
        league_id = None
        if league_name:
            league_id = generate_league_id(league_name)
            self._ensure_league(session, league_id, league_name, source)

        # 处理球队
        home_team_id = generate_team_id(home_name)
        away_team_id = generate_team_id(away_name)
        self._ensure_team(session, home_team_id, home_name, league_id)
        self._ensure_team(session, away_team_id, away_name, league_id)

        # 解析开球时间
        kickoff_dt = None
        if kickoff_str:
            try:
                kickoff_dt = datetime.fromisoformat(kickoff_str.replace("Z", ""))
            except (ValueError, TypeError):
                pass

        # 解析比分
        home_score, away_score = None, None
        score_str = data.get("score", "")
        if score_str and score_str not in ("未开始", "?", "?-?"):
            parts = score_str.split("-")
            if len(parts) == 2:
                try:
                    home_score = int(parts[0])
                    away_score = int(parts[1])
                except ValueError:
                    pass

        # 状态推导
        if home_score is not None and away_score is not None:
            status = "finished"
        elif kickoff_dt and kickoff_dt > datetime.utcnow():
            status = "scheduled"
        else:
            status = "live" if score_str and score_str != "未开始" else "scheduled"

        match = Match(
            match_id=match_id,
            source=source,
            league_id=league_id,
            league_name=league_name,
            home_team_id=home_team_id,
            home_team=home_name,
            away_team_id=away_team_id,
            away_team=away_name,
            kickoff_time=kickoff_dt,
            home_score=home_score,
            away_score=away_score,
            score_display=score_str,
            status=status,
            collected_at=datetime.utcnow(),
        )
        session.add(match)

        # 导入赔率
        self._import_odds(session, match_id, source, data)

        self.stats["inserted"] += 1

    def _update_match(self, session: Session, match: Match, data: dict):
        """更新已有比赛（补全数据）"""
        updated = False

        # 补全缺失字段
        if not match.league_name and data.get("league"):
            match.league_name = data["league"].strip()
            updated = True

        if not match.league_id and data.get("league"):
            match.league_id = generate_league_id(data["league"].strip())
            updated = True

        if match.home_score is None or match.away_score is None:
            score_str = data.get("score", "")
            if score_str and score_str not in ("未开始", "?", "?-?"):
                parts = score_str.split("-")
                if len(parts) == 2:
                    try:
                        match.home_score = int(parts[0])
                        match.away_score = int(parts[1])
                        match.status = "finished"
                        updated = True
                    except ValueError:
                        pass

        if updated:
            match.updated_at = datetime.utcnow()

        # 添加新来源的赔率
        source = data.get("source", "")
        self._import_odds(session, match.match_id, source, data)

    def _import_odds(self, session: Session, match_id: str, source: str, data: dict):
        """导入赔率数据（一个来源一条记录）"""
        odds_home = data.get("odds_home", "")
        odds_draw = data.get("odds_draw", "")
        odds_away = data.get("odds_away", "")

        # 跳过无赔率的数据
        if not odds_home and not odds_draw and not odds_away:
            return

        # 检查该来源的赔率是否已存在
        existing_odds = session.execute(
            select(Odds).where(
                Odds.match_id == match_id,
                Odds.source == source,
            )
        ).scalar_one_or_none()

        if not existing_odds:
            try:
                odds = Odds(
                    match_id=match_id,
                    source=source,
                    odds_home=float(odds_home) if odds_home else None,
                    odds_draw=float(odds_draw) if odds_draw else None,
                    odds_away=float(odds_away) if odds_away else None,
                    asian_handicap=str(data.get("asian_handicap", "")),
                    over_under=str(data.get("over_under", "")),
                    collected_at=datetime.utcnow(),
                )
                session.add(odds)
            except (ValueError, TypeError):
                pass

    def _ensure_league(self, session: Session, league_id: str, name: str, source: str):
        """确保联赛记录存在"""
        existing = session.get(League, league_id)
        if not existing:
            session.add(League(
                id=league_id,
                name=name,
                source=source,
            ))

    def _ensure_team(self, session: Session, team_id: str, name: str, league_id: str = None):
        """确保球队记录存在"""
        existing = session.get(Team, team_id)
        if not existing:
            session.add(Team(
                id=team_id,
                name=name,
                league_id=league_id,
            ))
        elif league_id and not existing.league_id:
            existing.league_id = league_id


def import_clean_file(filepath: str = "exports/clean_matches.json") -> dict:
    """从 clean_matches.json 文件导入数据库"""
    path = Path(filepath)
    if not path.exists():
        logger.warning(f"文件不存在: {filepath}")
        return {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    with open(path, "r", encoding="utf-8") as f:
        matches = json.load(f)

    logger.info(f"从 {filepath} 读取 {len(matches)} 条数据，开始导入...")

    importer = MatchImporter()
    return importer.import_matches(matches)
