"""数据库模型 — SQLAlchemy ORM"""

import hashlib
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, DateTime,
    ForeignKey, Index, UniqueConstraint, Text, Boolean
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _utcnow():
    """返回 naive UTC datetime，替代已弃用的 _utcnow()"""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def generate_match_id(source: str, home_team: str, away_team: str, kickoff_time: str) -> str:
    """根据 来源 + 主队 + 客队 + 开球时间 生成唯一 match_id"""
    raw = f"{source}|{home_team.strip().lower()}|{away_team.strip().lower()}|{kickoff_time[:10]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def generate_team_id(name: str) -> str:
    """生成球队唯一 ID"""
    raw = name.strip().lower()
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def generate_league_id(name: str) -> str:
    """生成联赛唯一 ID"""
    raw = name.strip().lower()
    return hashlib.md5(raw.encode()).hexdigest()[:8]


class League(Base):
    """联赛表"""
    __tablename__ = "leagues"

    id = Column(String(8), primary_key=True)
    name = Column(String(255), nullable=False)
    country = Column(String(100), default="")
    source = Column(String(50), default="")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    matches = relationship("Match", back_populates="league_rel")
    teams = relationship("Team", back_populates="league_rel")

    def __repr__(self):
        return f"<League {self.id} '{self.name}'>"


class Team(Base):
    """球队表"""
    __tablename__ = "teams"

    id = Column(String(12), primary_key=True)
    name = Column(String(255), nullable=False, index=True)
    country = Column(String(100), default="")
    league_id = Column(String(8), ForeignKey("leagues.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    league_rel = relationship("League", back_populates="teams")

    # name 字段已通过 index=True 自动创建索引，无需在 __table_args__ 中重复定义

    def __repr__(self):
        return f"<Team {self.id} '{self.name}'>"


class Match(Base):
    """比赛表"""
    __tablename__ = "matches"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    match_id = Column(String(16), unique=True, nullable=False, index=True)
    source = Column(String(50), nullable=False)
    source_match_id = Column(String(100), default="")
    league_id = Column(String(8), ForeignKey("leagues.id"), nullable=True)
    league_name = Column(String(255), default="")
    home_team_id = Column(String(12), ForeignKey("teams.id"), nullable=True)
    home_team = Column(String(255), nullable=False)
    away_team_id = Column(String(12), ForeignKey("teams.id"), nullable=True)
    away_team = Column(String(255), nullable=False)
    kickoff_time = Column(DateTime, nullable=True)
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    score_display = Column(String(20), default="")
    status = Column(String(20), default="scheduled")
    collected_at = Column(DateTime, default=_utcnow)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    league_rel = relationship("League", back_populates="matches")
    home_team_rel = relationship("Team", foreign_keys=[home_team_id])
    away_team_rel = relationship("Team", foreign_keys=[away_team_id])
    odds = relationship("Odds", back_populates="match_rel", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_matches_kickoff", "kickoff_time"),
        Index("ix_matches_league", "league_id"),
        Index("ix_matches_source", "source"),
    )

    def __repr__(self):
        return f"<Match {self.match_id} '{self.home_team} vs {self.away_team}'>"


class Odds(Base):
    """赔率表"""
    __tablename__ = "odds"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    match_id = Column(String(16), ForeignKey("matches.match_id"), nullable=False, index=True)
    source = Column(String(50), nullable=False)
    bookmaker = Column(String(100), default="")
    odds_home = Column(Float, nullable=True)
    odds_draw = Column(Float, nullable=True)
    odds_away = Column(Float, nullable=True)
    asian_handicap = Column(String(50), default="")
    over_under = Column(String(50), default="")
    collected_at = Column(DateTime, default=_utcnow)
    created_at = Column(DateTime, default=_utcnow)

    match_rel = relationship("Match", back_populates="odds")

    __table_args__ = (
        Index("ix_odds_source", "source"),
    )

    def __repr__(self):
        return f"<Odds {self.match_id} H:{self.odds_home} D:{self.odds_draw} A:{self.odds_away}>"
