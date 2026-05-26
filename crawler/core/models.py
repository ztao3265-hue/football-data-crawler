"""数据模型 - 统一的比赛数据结构"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class MatchData:
    """统一的比赛数据结构"""
    source: str = ""
    league: str = ""
    home_team: str = ""
    away_team: str = ""
    kickoff_time: str = ""
    score: str = ""
    odds_home: str = ""
    odds_draw: str = ""
    odds_away: str = ""
    asian_handicap: str = ""
    over_under: str = ""
    odds_bookmaker: str = ""
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MatchData":
        return cls(**{k: data.get(k, "") for k in cls.__dataclass_fields__})


@dataclass
class CrawlResult:
    """采集结果"""
    source: str
    status: str  # success / partial / failed
    matches_count: int
    duration_seconds: float
    error_message: str = ""
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat())
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
