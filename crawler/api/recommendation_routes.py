"""
推荐系统 REST API 路由

提供:
- 今日推荐 / 每日精选
- Top5 / 低风险 / 高EV
- 历史追踪
- 引擎状态
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        from backend.engine.recommendation_engine import UnifiedRecommendationEngine
        _engine = UnifiedRecommendationEngine()
    return _engine


# ── 今日推荐 ─────────────────────────────────────────────────────

@router.get("/today")
def get_today_recommendations():
    """获取今日所有推荐"""
    engine = get_engine()
    picks = engine.get_today_picks()
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(picks),
        "data": picks,
    }


@router.get("/summary")
def get_daily_summary(date: Optional[str] = Query(None, description="日期 YYYY-MM-DD")):
    """获取每日推荐摘要"""
    engine = get_engine()
    return engine.get_daily_summary(date)


# ── 精选分类 ─────────────────────────────────────────────────────

@router.get("/top5")
def get_top5(date: Optional[str] = Query(None, description="日期 YYYY-MM-DD")):
    """获取 Top5 推荐"""
    engine = get_engine()
    data = engine.get_top5(date)
    return {"date": date or datetime.now().strftime("%Y-%m-%d"), "total": len(data), "data": data}


@router.get("/low-risk")
def get_low_risk(date: Optional[str] = Query(None)):
    """获取低风险推荐"""
    engine = get_engine()
    data = engine.get_low_risk(date)
    return {"date": date or datetime.now().strftime("%Y-%m-%d"), "total": len(data), "data": data}


@router.get("/high-ev")
def get_high_ev(date: Optional[str] = Query(None)):
    """获取高EV推荐 (EV >= 5%)"""
    engine = get_engine()
    data = engine.get_high_ev(date)
    return {"date": date or datetime.now().strftime("%Y-%m-%d"), "total": len(data), "data": data}


@router.get("/strongest")
def get_strongest_picks(date: Optional[str] = Query(None)):
    """获取最强精选"""
    engine = get_engine()
    data = engine.get_strongest(date)
    return {"date": date or datetime.now().strftime("%Y-%m-%d"), "total": len(data), "data": data}


@router.get("/by-risk/{risk_level}")
def get_by_risk_level(
    risk_level: str,
    date: Optional[str] = Query(None),
):
    """按风险等级获取推荐 (low/medium/high)"""
    if risk_level not in ("low", "medium", "high"):
        raise HTTPException(status_code=400, detail="风险等级必须是 low/medium/high")
    engine = get_engine()
    data = engine.get_by_risk(risk_level, date)
    return {"risk_level": risk_level, "total": len(data), "data": data}


# ── 历史 ─────────────────────────────────────────────────────────

@router.get("/history")
def get_history(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=1000, description="返回条数"),
):
    """获取历史推荐"""
    engine = get_engine()
    data = engine.get_history(start_date, end_date, limit)
    return {"total": len(data), "data": data}


@router.get("/history/stats")
def get_history_stats(days: int = Query(30, ge=1, le=365, description="统计天数")):
    """获取历史统计"""
    engine = get_engine()
    return engine.get_history_stats(days)


# ── 引擎状态 ─────────────────────────────────────────────────────

@router.get("/engine/status")
def get_engine_status():
    """获取推荐引擎状态"""
    engine = get_engine()
    return engine.get_engine_status()


# ── 流水线触发 ───────────────────────────────────────────────────

@router.post("/pipeline/run")
def trigger_daily_pipeline(
    date: Optional[str] = Query(None, description="目标日期 YYYY-MM-DD"),
    bankroll: float = Query(10000.0, description="资金量"),
):
    """手动触发每日流水线"""
    from backend.engine.daily_pipeline import DailyPipeline

    pipeline = DailyPipeline()
    report = pipeline.run(target_date=date, bankroll=bankroll)
    return {"status": "completed", "report": report}


@router.post("/pipeline/scan")
def scan_matches(date: Optional[str] = Query(None)):
    """扫描即将开赛的比赛"""
    from backend.engine.daily_pipeline import DailyPipeline

    pipeline = DailyPipeline()
    matches = pipeline.scan_upcoming_matches(date)
    return {"date": date or datetime.now().strftime("%Y-%m-%d"), "total": len(matches), "data": matches}
