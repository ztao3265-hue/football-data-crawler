"""
完整 API 服务器 — 爬虫数据 + 推荐系统 统一接口

启动方式:
    python -m crawler.api.full_server --port 8000
    python -m crawler.api.full_server --port 8000 --reload
"""
import argparse
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text, select, func
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from crawler.database.connection import get_db
from crawler.database.schema import Match, Odds, OddsHistory, Team, League
from crawler.api.recommendation_routes import router as rec_router


def _get_session():
    db = get_db()
    return db.session_factory()


def create_app() -> FastAPI:
    """创建完整 FastAPI 应用"""
    app = FastAPI(
        title="Football Data & Recommendation API",
        description="足球数据采集 & AI 每日推荐系统",
        version="3.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 爬虫数据 API ──────────────────────────────────────────

    @app.get("/api/v1/matches")
    def list_matches(
        date: Optional[str] = Query(None, description="日期 YYYY-MM-DD"),
        league: Optional[str] = Query(None, description="联赛名称（模糊匹配）"),
        status: Optional[str] = Query(None, description="状态: scheduled/live/finished"),
        source: Optional[str] = Query(None, description="数据源: sofascore/fotmob"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
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
            return {"total": len(rows), "offset": offset, "data": [_match_to_dict(r) for r in rows]}
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
            return {"match_id": match_id, "total": len(rows), "data": [_history_to_dict(r) for r in rows]}
        finally:
            session.close()

    @app.get("/api/v1/odds/latest")
    def latest_odds(source: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500)):
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

    @app.get("/api/v1/leagues")
    def list_leagues():
        session = _get_session()
        try:
            rows = session.execute(select(League).order_by(League.name)).scalars().all()
            return {"total": len(rows), "data": [
                {"id": r.id, "name": r.name, "country": r.country, "source": r.source} for r in rows
            ]}
        finally:
            session.close()

    @app.get("/api/v1/teams")
    def list_teams(league_id: Optional[str] = Query(None), limit: int = Query(200, ge=1, le=1000)):
        session = _get_session()
        try:
            q = select(Team)
            if league_id:
                q = q.where(Team.league_id == league_id)
            q = q.order_by(Team.name).limit(limit)
            rows = session.execute(q).scalars().all()
            return {"total": len(rows), "data": [
                {"id": r.id, "name": r.name, "league_id": r.league_id} for r in rows
            ]}
        finally:
            session.close()

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
            return {"counts": counts, "status_distribution": status_dist, "source_distribution": source_dist}
        finally:
            session.close()

    # ── 仪表盘 HTML ───────────────────────────────────────────

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        from pathlib import Path
        dashboard_path = Path(__file__).resolve().parent.parent / "ui" / "templates" / "dashboard.html"
        if dashboard_path.exists():
            return dashboard_path.read_text(encoding="utf-8")
        return "<h1>Dashboard not found</h1>"

    # ── 健康检查 ───────────────────────────────────────────────

    @app.get("/api/v1/health")
    def health():
        return {
            "status": "ok",
            "version": "3.0.0",
            "timestamp": datetime.now().isoformat(),
        }

    # ── 注册推荐路由 ───────────────────────────────────────────

    app.include_router(rec_router)

    return app


# ── 序列化辅助 ───────────────────────────────────────────────────

def _match_to_dict(m: Match) -> dict:
    return {
        "match_id": m.match_id, "source": m.source,
        "league_id": m.league_id, "league_name": m.league_name,
        "home_team": m.home_team, "away_team": m.away_team,
        "home_team_id": m.home_team_id, "away_team_id": m.away_team_id,
        "kickoff_time": m.kickoff_time.isoformat() if m.kickoff_time else None,
        "home_score": m.home_score, "away_score": m.away_score,
        "score_display": m.score_display, "status": m.status,
        "collected_at": m.collected_at.isoformat() if m.collected_at else None,
    }


def _odds_to_dict(o: Odds) -> dict:
    return {
        "match_id": o.match_id, "source": o.source, "bookmaker": o.bookmaker,
        "odds_home": o.odds_home, "odds_draw": o.odds_draw, "odds_away": o.odds_away,
        "asian_handicap": o.asian_handicap, "over_under": o.over_under,
        "collected_at": o.collected_at.isoformat() if o.collected_at else None,
    }


def _history_to_dict(h: OddsHistory) -> dict:
    return {
        "match_id": h.match_id, "source": h.source, "bookmaker": h.bookmaker,
        "odds_home": h.odds_home, "odds_draw": h.odds_draw, "odds_away": h.odds_away,
        "asian_handicap": h.asian_handicap, "over_under": h.over_under,
        "snapshot_at": h.snapshot_at.isoformat() if h.snapshot_at else None,
    }


# ── 入口 ─────────────────────────────────────────────────────────

def start_server(port: int = 8000, reload: bool = False):
    import uvicorn
    uvicorn.run(
        "crawler.api.full_server:create_app",
        host="0.0.0.0",
        port=port,
        reload=reload,
        factory=True,
        log_level="info",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="完整 API 服务器")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    start_server(args.port, args.reload)
