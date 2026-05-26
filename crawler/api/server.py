"""FastAPI 数据接口 — 为 football-betting-analysis 主系统提供 REST API"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text, select, func

from crawler.database.connection import get_db
from crawler.database.schema import Match, Odds, OddsHistory, Team, League


def _get_session():
    db = get_db()
    return db.session_factory()


def start_api(port: int = 8000):
    """启动 FastAPI 服务"""
    import uvicorn
    from fastapi import FastAPI, Query, HTTPException
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(
        title="Football Data Crawler API",
        description="足球数据采集系统数据接口",
        version="2.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------- 比赛 ----------

    @app.get("/api/v1/matches")
    def list_matches(
        date: Optional[str] = Query(None, description="日期 YYYY-MM-DD"),
        league: Optional[str] = Query(None, description="联赛名称（模糊匹配）"),
        status: Optional[str] = Query(None, description="状态: scheduled/live/finished"),
        source: Optional[str] = Query(None, description="数据源: sofascore/fotmob"),
        limit: int = Query(100, ge=1, le=1000, description="返回条数上限"),
        offset: int = Query(0, ge=0, description="偏移量"),
    ):
        session = _get_session()
        try:
            q = select(Match)
            if date:
                q = q.where(func.date(Match.kickoff_time) == date)
            if league:
                q = q.where(Match.league_name.ilike(f"%{league}%"))
            if status:
                q = q.where(Match.status == status)
            if source:
                q = q.where(Match.source == source)
            q = q.order_by(Match.kickoff_time.asc()).offset(offset).limit(limit)
            rows = session.execute(q).scalars().all()
            return {
                "total": len(rows),
                "offset": offset,
                "data": [_match_to_dict(r) for r in rows],
            }
        finally:
            session.close()

    @app.get("/api/v1/matches/{match_id}")
    def get_match(match_id: str):
        session = _get_session()
        try:
            match = session.execute(select(Match).where(Match.match_id == match_id)).scalar_one_or_none()
            if not match:
                raise HTTPException(status_code=404, detail="比赛不存在")
            data = _match_to_dict(match)
            # 附带赔率
            odds_q = select(Odds).where(Odds.match_id == match_id)
            odds_rows = session.execute(odds_q).scalars().all()
            data["odds"] = [_odds_to_dict(o) for o in odds_rows]
            return data
        finally:
            session.close()

    @app.get("/api/v1/matches/{match_id}/odds-history")
    def get_odds_history(match_id: str, limit: int = Query(50, ge=1, le=500)):
        session = _get_session()
        try:
            q = (select(OddsHistory)
                 .where(OddsHistory.match_id == match_id)
                 .order_by(OddsHistory.snapshot_at.desc())
                 .limit(limit))
            rows = session.execute(q).scalars().all()
            return {
                "match_id": match_id,
                "total": len(rows),
                "data": [_history_to_dict(r) for r in rows],
            }
        finally:
            session.close()

    # ---------- 赔率 ----------

    @app.get("/api/v1/odds/latest")
    def latest_odds(
        source: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        session = _get_session()
        try:
            q = select(Odds).order_by(Odds.collected_at.desc())
            if source:
                q = q.where(Odds.source == source)
            q = q.limit(limit)
            rows = session.execute(q).scalars().all()
            return {"total": len(rows), "data": [_odds_to_dict(r) for r in rows]}
        finally:
            session.close()

    # ---------- 联赛 ----------

    @app.get("/api/v1/leagues")
    def list_leagues():
        session = _get_session()
        try:
            rows = session.execute(select(League).order_by(League.name)).scalars().all()
            return {"total": len(rows), "data": [{"id": r.id, "name": r.name, "country": r.country, "source": r.source} for r in rows]}
        finally:
            session.close()

    # ---------- 球队 ----------

    @app.get("/api/v1/teams")
    def list_teams(
        league_id: Optional[str] = Query(None, description="联赛 ID"),
        limit: int = Query(200, ge=1, le=1000),
    ):
        session = _get_session()
        try:
            q = select(Team)
            if league_id:
                q = q.where(Team.league_id == league_id)
            q = q.order_by(Team.name).limit(limit)
            rows = session.execute(q).scalars().all()
            return {"total": len(rows), "data": [{"id": r.id, "name": r.name, "league_id": r.league_id} for r in rows]}
        finally:
            session.close()

    # ---------- 统计 ----------

    @app.get("/api/v1/stats")
    def get_stats():
        session = _get_session()
        try:
            tables = ["matches", "odds", "odds_history", "teams", "leagues"]
            counts = {}
            for t in tables:
                r = session.execute(text(f"SELECT COUNT(*) FROM {t}"))
                counts[t] = r.scalar() or 0

            status_dist = {}
            r = session.execute(text("SELECT status, COUNT(*) FROM matches GROUP BY status"))
            for row in r:
                status_dist[row[0]] = row[1]

            source_dist = {}
            r = session.execute(text("SELECT source, COUNT(*) FROM matches GROUP BY source"))
            for row in r:
                source_dist[row[0]] = row[1]

            return {
                "counts": counts,
                "status_distribution": status_dist,
                "source_distribution": source_dist,
            }
        finally:
            session.close()

    # ---------- 启动 ----------

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


# ---------- 序列化辅助 ----------

def _match_to_dict(m: Match) -> dict:
    return {
        "match_id": m.match_id,
        "source": m.source,
        "league_id": m.league_id,
        "league_name": m.league_name,
        "home_team": m.home_team,
        "away_team": m.away_team,
        "home_team_id": m.home_team_id,
        "away_team_id": m.away_team_id,
        "kickoff_time": m.kickoff_time.isoformat() if m.kickoff_time else None,
        "home_score": m.home_score,
        "away_score": m.away_score,
        "score_display": m.score_display,
        "status": m.status,
        "collected_at": m.collected_at.isoformat() if m.collected_at else None,
    }


def _odds_to_dict(o: Odds) -> dict:
    return {
        "match_id": o.match_id,
        "source": o.source,
        "bookmaker": o.bookmaker,
        "odds_home": o.odds_home,
        "odds_draw": o.odds_draw,
        "odds_away": o.odds_away,
        "asian_handicap": o.asian_handicap,
        "over_under": o.over_under,
        "collected_at": o.collected_at.isoformat() if o.collected_at else None,
    }


def _history_to_dict(h: OddsHistory) -> dict:
    return {
        "match_id": h.match_id,
        "source": h.source,
        "bookmaker": h.bookmaker,
        "odds_home": h.odds_home,
        "odds_draw": h.odds_draw,
        "odds_away": h.odds_away,
        "asian_handicap": h.asian_handicap,
        "over_under": h.over_under,
        "snapshot_at": h.snapshot_at.isoformat() if h.snapshot_at else None,
    }
