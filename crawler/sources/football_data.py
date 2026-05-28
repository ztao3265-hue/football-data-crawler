"""football-data.org API 采集器

使用官方 REST API，需要 API Key
免费层: 每分钟 10 次请求
"""

import os
from datetime import datetime
from typing import List

import httpx

from crawler.sources.base import BaseCrawler
from crawler.core.models import MatchData
from crawler.utils.helpers import save_json
from crawler.core.logger import get_logger

logger = get_logger(__name__)


class FootballDataCrawler(BaseCrawler):
    """football-data.org 采集器

    文档: https://www.football-data.org/documentation/quickstart
    """

    def __init__(self, config: dict):
        super().__init__("football-data", config)
        self.base_url = config.get("base_url", "https://api.football-data.org/v4")
        self.api_key = os.getenv("FOOTBALL_DATA_ORG_API_KEY", "")

        # 联赛 ID 映射
        self.league_ids = [
            "PL",    # 英超
            "PD",    # 西甲
            "BL1",   # 德甲
            "SA",    # 意甲
            "FL1",   # 法甲
            "CL",    # 欧冠
            "DED",   # 荷甲
            "PPL",   # 葡超
        ]

    async def collect(self, date_str: str) -> List[MatchData]:
        self.logger.info(f"开始采集 football-data.org 数据: {date_str}")

        if not self.api_key:
            self.logger.warning("未设置 FOOTBALL_DATA_ORG_API_KEY，跳过 football-data.org 采集")
            self.logger.info("请前往 https://www.football-data.org/client/register 注册获取免费 API Key")
            return []

        matches = []
        for league_id in self.league_ids:
            try:
                league_matches = await self._fetch_league_matches(league_id, date_str)
                if league_matches:
                    matches.extend(league_matches)
                    self.logger.info(f"联赛 {league_id}: {len(league_matches)} 场")
            except Exception as e:
                self.logger.error(f"联赛 {league_id} 采集失败: {e}")

        raw_data = [m.to_dict() for m in matches]
        if raw_data:
            from config.paths import RAW_DATA_DIR
            save_json(raw_data, str(RAW_DATA_DIR / f"football_data_{date_str}.json"))

        return matches

    async def _fetch_league_matches(self, league_id: str, date_str: str) -> List[MatchData]:
        """获取指定联赛的比赛"""
        url = f"{self.base_url}/competitions/{league_id}/matches"
        params = {
            "dateFrom": date_str,
            "dateTo": date_str,
        }

        async def _fetch():
            async with httpx.AsyncClient(timeout=30) as client:
                headers = {
                    "X-Auth-Token": self.api_key,
                    "Accept": "application/json",
                }
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 429:
                    self.logger.warning("football-data.org API 限速，等待 60 秒...")
                    import asyncio
                    await asyncio.sleep(60)
                    resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                return resp.json()

        data = await self.safe_request(_fetch)
        matches_raw = data.get("matches", [])

        parsed = []
        for m in matches_raw:
            match = self._parse_match(m)
            if match:
                parsed.append(match)

        return parsed

    async def collect_all_leagues(self, date_str: str) -> List[MatchData]:
        """获取所有可用联赛的比赛（不指定联赛 ID）"""
        url = f"{self.base_url}/matches"
        params = {"dateFrom": date_str, "dateTo": date_str}

        async def _fetch():
            async with httpx.AsyncClient(timeout=30) as client:
                headers = {
                    "X-Auth-Token": self.api_key,
                    "Accept": "application/json",
                }
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 429:
                    import asyncio
                    await asyncio.sleep(60)
                    resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                return resp.json()

        try:
            data = await self.safe_request(_fetch)
            matches_raw = data.get("matches", [])

            parsed = []
            for m in matches_raw:
                match = self._parse_match(m)
                if match:
                    parsed.append(match)
            return parsed
        except Exception as e:
            self.logger.error(f"football-data.org 全量采集失败: {e}")
            return []

    def _parse_match(self, match_data: dict) -> MatchData | None:
        """解析 API 返回的比赛数据"""
        try:
            home = match_data.get("homeTeam", {})
            away = match_data.get("awayTeam", {})
            competition = match_data.get("competition", {})
            score_info = match_data.get("score", {})

            home_name = home.get("name", "")
            away_name = away.get("name", "")

            if not home_name or not away_name:
                return None

            full_time = score_info.get("fullTime", {})
            home_score = full_time.get("home", "")
            away_score = full_time.get("away", "")
            if home_score is not None and away_score is not None:
                score = f"{home_score}-{away_score}"
            else:
                score = "未开始"

            utc_date = match_data.get("utcDate", "")
            if utc_date:
                try:
                    dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
                    kickoff = dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    kickoff = utc_date
            else:
                kickoff = ""

            # football-data.org 免费版不提供赔率数据
            odds_data = match_data.get("odds", {})

            return MatchData(
                source="football-data",
                league=competition.get("name", ""),
                home_team=home_name,
                away_team=away_name,
                kickoff_time=kickoff,
                score=score,
                odds_home=str(odds_data.get("homeWin", "")),
                odds_draw=str(odds_data.get("draw", "")),
                odds_away=str(odds_data.get("awayWin", "")),
            )
        except Exception as e:
            logger.warning(f"解析 football-data.org 比赛失败: {e}")
            return None
