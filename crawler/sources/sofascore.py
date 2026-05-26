"""Sofascore 数据采集器 - 含欧赔/亚盘/大小球赔率"""

import asyncio
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from crawler.sources.base import BaseCrawler
from crawler.core.models import MatchData
from crawler.browser.interceptor import APIInterceptor
from crawler.utils.helpers import save_json, save_text

ODDS_SEMAPHORE = asyncio.Semaphore(5)
ODDS_FETCH_ENABLED = True


class SofascoreCrawler(BaseCrawler):
    """Sofascore 采集器 - 通过 API + 浏览器双通道采集"""

    def __init__(self, browser, config: dict):
        super().__init__("sofascore", config)
        self.browser = browser
        self.api_base = config.get("api_base", "https://api.sofascore.com/api/v1")
        self.base_url = config.get("base_url", "https://www.sofascore.com")

    async def collect(self, date_str: str) -> List[MatchData]:
        self.logger.info(f"开始采集 Sofascore 数据: {date_str}")
        matches = []

        api_matches = await self._collect_via_api(date_str)
        if api_matches:
            matches.extend(api_matches)
            self.logger.info(f"API 采集到 {len(api_matches)} 条数据")

        browser_matches = await self._collect_via_browser(date_str)
        if browser_matches:
            matches.extend(browser_matches)
            self.logger.info(f"浏览器采集到 {len(browser_matches)} 条数据")

        raw_data = [m.to_dict() for m in matches]
        if raw_data:
            save_json(raw_data, f"data/raw/sofascore_{date_str}.json")

        return matches

    async def _collect_via_api(self, date_str: str) -> List[MatchData]:
        """通过 Sofascore API 采集（含赔率）"""
        matches = []
        url = f"{self.api_base}/sport/football/scheduled-events/{date_str}"

        async def _fetch():
            async with httpx.AsyncClient(timeout=30) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Origin": "https://www.sofascore.com",
                    "Referer": "https://www.sofascore.com/",
                }
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json()

        try:
            data = await self.safe_request(_fetch)
            events = data.get("events", [])

            # 第一步：解析事件基本信息
            event_map = {}
            for event in events:
                match = self._parse_event(event)
                if match:
                    event_id = str(event.get("id", ""))
                    matches.append(match)
                    if event_id:
                        event_map[event_id] = match

            # 第二步：批量获取赔率
            if ODDS_FETCH_ENABLED and event_map:
                odds_count = await self._fetch_odds_batch(event_map)
                self.logger.info(f"赔率采集: {odds_count}/{len(event_map)} 场比赛有赔率数据")

        except Exception as e:
            self.logger.error(f"Sofascore API 请求失败: {e}")

        return matches

    async def _fetch_odds_batch(self, event_map: dict) -> int:
        """批量并发获取 event 赔率数据"""
        count = 0

        async def _fetch_one(event_id, match):
            nonlocal count
            async with ODDS_SEMAPHORE:
                try:
                    odds = await self._fetch_event_odds(event_id)
                    if odds:
                        self._apply_odds(match, odds)
                        return True
                except Exception:
                    pass
            return False

        tasks = [_fetch_one(eid, m) for eid, m in event_map.items()]
        results = await asyncio.gather(*tasks)
        return sum(1 for r in results if r)

    async def _fetch_event_odds(self, event_id: str) -> dict | None:
        """获取单场比赛的赔率数据"""
        url = f"{self.api_base}/event/{event_id}/odds"

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                }
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()

        try:
            return await self.safe_request(_fetch)
        except Exception:
            return None

    def _apply_odds(self, match: MatchData, odds_data: dict):
        """将赔率数据应用到 MatchData"""
        if not odds_data:
            return

        # 欧赔（取第一个博彩公司的主赔率）
        main_odds = odds_data.get("main", {}) or {}
        if main_odds:
            match.odds_home = str(main_odds.get("home", ""))
            match.odds_draw = str(main_odds.get("draw", ""))
            match.odds_away = str(main_odds.get("away", ""))
            match.odds_bookmaker = str(main_odds.get("provider", {}).get("name", "") if isinstance(main_odds.get("provider"), dict) else "")

        # 亚盘
        asian = (odds_data.get("asianHandicap") or odds_data.get("asianHandicapOdds") or {})
        if isinstance(asian, dict):
            match.asian_handicap = str(asian.get("line", asian.get("handicap", "")))

        # 大小球
        ou = (odds_data.get("overUnder") or odds_data.get("overUnderOdds") or {})
        if isinstance(ou, dict):
            match.over_under = str(ou.get("line", ou.get("total", "")))

    async def _collect_via_browser(self, date_str: str) -> List[MatchData]:
        """通过浏览器采集（捕获 API 响应 + 页面解析）"""
        matches = []
        page = None

        try:
            page = await self.browser.new_page()

            interceptor = APIInterceptor(page)
            interceptor.add_url_filter("/api/v1/sport/football")
            await interceptor.start()

            url = f"{self.base_url}/football/{date_str}"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.browser.wait_for_network_idle(page, timeout=30000)
            await self.browser.scroll_page(page, times=3)

            html = await self.browser.get_page_html(page)
            save_text(html, f"data/raw/sofascore_{date_str}.html")

            await self.browser.screenshot(
                page, f"sofascore_{date_str}_{datetime.now():%H%M%S}.png"
            )

            api_responses = interceptor.get_filtered_responses("events")
            seen_ids = set()
            for resp in api_responses:
                body = resp.get("body", {})
                if isinstance(body, dict):
                    events = body.get("events", [])
                    for event in events:
                        eid = str(event.get("id", ""))
                        if eid in seen_ids:
                            continue
                        seen_ids.add(eid)
                        match = self._parse_event(event)
                        if match:
                            matches.append(match)

            interceptor.save_responses(f"data/raw/sofascore_api_{date_str}.json")

        except Exception as e:
            self.logger.error(f"Sofascore 浏览器采集失败: {e}")

        finally:
            if page:
                await page.close()

        return matches

    def _parse_event(self, event: dict) -> MatchData | None:
        """解析赛事事件为统一格式（不含赔率，赔率后续通过 _apply_odds 填充）"""
        try:
            home_team = event.get("homeTeam", {})
            away_team = event.get("awayTeam", {})
            tournament = event.get("tournament", {})
            status = event.get("status", {})

            home_name = home_team.get("name", "")
            away_name = away_team.get("name", "")

            if not home_name or not away_name:
                return None

            home_score = status.get("homeScore", "")
            away_score = status.get("awayScore", "")
            score = f"{home_score or '?'}-{away_score or '?'}" if status.get("type") != "notstarted" else "未开始"

            start_timestamp = event.get("startTimestamp", 0)
            kickoff = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d %H:%M") if start_timestamp else ""

            return MatchData(
                source="sofascore",
                league=tournament.get("name", ""),
                home_team=home_name,
                away_team=away_name,
                kickoff_time=kickoff,
                score=score,
            )
        except Exception as e:
            self.logger.warning(f"解析赛事失败: {e}")
            return None
