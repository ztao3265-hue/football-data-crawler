"""FotMob 数据采集器"""

from datetime import datetime
from typing import List

import httpx

from crawler.sources.base import BaseCrawler
from crawler.core.models import MatchData
from crawler.browser.interceptor import APIInterceptor
from crawler.utils.helpers import save_json, save_text


class FotmobCrawler(BaseCrawler):
    """FotMob 采集器 - 通过 API + 浏览器采集"""

    def __init__(self, browser, config: dict):
        super().__init__("fotmob", config)
        self.browser = browser
        self.api_base = config.get("api_base", "https://www.fotmob.com/api")
        self.base_url = config.get("base_url", "https://www.fotmob.com")

    async def collect(self, date_str: str) -> List[MatchData]:
        self.logger.info(f"开始采集 FotMob 数据: {date_str}")
        matches = []

        # API 采集
        api_matches = await self._collect_via_api(date_str)
        if api_matches:
            matches.extend(api_matches)
            self.logger.info(f"API 采集到 {len(api_matches)} 条数据")

        # 浏览器采集
        browser_matches = await self._collect_via_browser(date_str)
        if browser_matches:
            matches.extend(browser_matches)
            self.logger.info(f"浏览器采集到 {len(browser_matches)} 条数据")

        raw_data = [m.to_dict() for m in matches]
        if raw_data:
            save_json(raw_data, f"data/raw/fotmob_{date_str}.json")

        return matches

    async def _collect_via_api(self, date_str: str) -> List[MatchData]:
        """通过 FotMob API 采集"""
        matches = []
        url = f"{self.api_base}/matches?date={date_str}"

        async def _fetch():
            async with httpx.AsyncClient(timeout=30) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Origin": "https://www.fotmob.com",
                    "Referer": "https://www.fotmob.com/",
                }
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json()

        try:
            data = await self.safe_request(_fetch)
            leagues = data.get("leagues", [])

            for league in leagues:
                league_matches = league.get("matches", [])
                for m in league_matches:
                    match = self._parse_match(m, league)
                    if match:
                        matches.append(match)

        except Exception as e:
            self.logger.error(f"FotMob API 请求失败: {e}")

        return matches

    async def _collect_via_browser(self, date_str: str) -> List[MatchData]:
        """通过浏览器采集"""
        matches = []
        page = None

        try:
            page = await self.browser.new_page()

            interceptor = APIInterceptor(page)
            interceptor.add_url_filter("fotmob.com/api")
            await interceptor.start()

            url = f"{self.base_url}/matches"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.browser.wait_for_network_idle(page, timeout=30000)
            await self.browser.scroll_page(page, times=3)

            html = await self.browser.get_page_html(page)
            save_text(html, f"data/raw/fotmob_{date_str}.html")

            await self.browser.screenshot(
                page, f"fotmob_{date_str}_{datetime.now():%H%M%S}.png"
            )

            # 解析 API 响应
            api_responses = interceptor.get_filtered_responses("matches")
            for resp in api_responses:
                body = resp.get("body", {})
                if isinstance(body, dict):
                    leagues = body.get("leagues", [])
                    for league in leagues:
                        for m in league.get("matches", []):
                            match = self._parse_match(m, league)
                            if match:
                                matches.append(match)

            interceptor.save_responses(f"data/raw/fotmob_api_{date_str}.json")

        except Exception as e:
            self.logger.error(f"FotMob 浏览器采集失败: {e}")

        finally:
            if page:
                await page.close()

        return matches

    def _parse_match(self, match: dict, league: dict = None) -> MatchData | None:
        """解析比赛数据"""
        try:
            home = match.get("home", {})
            away = match.get("away", {})

            home_name = home.get("name", "")
            away_name = away.get("name", "")

            if not home_name or not away_name:
                return None

            status = match.get("status", {})
            home_score = status.get("homeScore", "")
            away_score = status.get("awayScore", "")
            score = f"{home_score or '?'}-{away_score or '?'}" if status.get("started") else "未开始"

            league_name = ""
            if league:
                league_name = league.get("name", "")
            elif match.get("league"):
                league_name = match["league"].get("name", "")

            kickoff = match.get("utcTime", "") or match.get("time", "")

            odds = match.get("odds", {}) or {}
            main_odds = odds.get("main", {}) if odds else {}

            return MatchData(
                source="fotmob",
                league=league_name,
                home_team=home_name,
                away_team=away_name,
                kickoff_time=kickoff,
                score=score,
                odds_home=str(main_odds.get("home", "")),
                odds_draw=str(main_odds.get("draw", "")),
                odds_away=str(main_odds.get("away", "")),
                asian_handicap=str(odds.get("asianHandicap", {}).get("line", "") if odds else ""),
                over_under=str(odds.get("overUnder", {}).get("line", "") if odds else ""),
            )
        except Exception as e:
            self.logger.warning(f"解析 FotMob 比赛失败: {e}")
            return None
