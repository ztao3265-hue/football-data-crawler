"""Sofascore 数据采集器"""

import asyncio
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from crawler.sources.base import BaseCrawler
from crawler.core.models import MatchData
from crawler.browser.interceptor import APIInterceptor
from crawler.utils.helpers import save_json, save_text


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

        # 方式 1: 直接调用 API
        api_matches = await self._collect_via_api(date_str)
        if api_matches:
            matches.extend(api_matches)
            self.logger.info(f"API 采集到 {len(api_matches)} 条数据")

        # 方式 2: 浏览器采集（补充赔率等数据）
        browser_matches = await self._collect_via_browser(date_str)
        if browser_matches:
            matches.extend(browser_matches)
            self.logger.info(f"浏览器采集到 {len(browser_matches)} 条数据")

        # 保存原始数据
        raw_data = [m.to_dict() for m in matches]
        if raw_data:
            save_json(raw_data, f"data/raw/sofascore_{date_str}.json")

        return matches

    async def _collect_via_api(self, date_str: str) -> List[MatchData]:
        """通过 Sofascore API 采集"""
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

            for event in events:
                match = self._parse_event(event)
                if match:
                    matches.append(match)

        except Exception as e:
            self.logger.error(f"Sofascore API 请求失败: {e}")

        return matches

    async def _collect_via_browser(self, date_str: str) -> List[MatchData]:
        """通过浏览器采集（捕获 API 响应 + 页面解析）"""
        matches = []
        page = None

        try:
            page = await self.browser.new_page()

            # 启动 API 拦截
            interceptor = APIInterceptor(page)
            interceptor.add_url_filter("/api/v1/sport/football")
            await interceptor.start()

            # 访问 Sofascore 足球页面
            url = f"{self.base_url}/football/{date_str}"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.browser.wait_for_network_idle(page, timeout=30000)
            await self.browser.scroll_page(page, times=3)

            # 保存 HTML
            html = await self.browser.get_page_html(page)
            save_text(html, f"data/raw/sofascore_{date_str}.html")

            # 截图
            await self.browser.screenshot(
                page, f"sofascore_{date_str}_{datetime.now():%H%M%S}.png"
            )

            # 解析拦截到的 API 响应
            api_responses = interceptor.get_filtered_responses("events")
            for resp in api_responses:
                body = resp.get("body", {})
                if isinstance(body, dict):
                    events = body.get("events", [])
                    for event in events:
                        match = self._parse_event(event)
                        if match:
                            matches.append(match)

            # 保存 API 原始响应
            interceptor.save_responses(f"data/raw/sofascore_api_{date_str}.json")

        except Exception as e:
            self.logger.error(f"Sofascore 浏览器采集失败: {e}")

        finally:
            if page:
                await page.close()

        return matches

    def _parse_event(self, event: dict) -> MatchData | None:
        """解析赛事事件为统一格式"""
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

            odds_data = event.get("odds", {})
            main_odds = odds_data.get("main", {}) if odds_data else {}

            return MatchData(
                source="sofascore",
                league=tournament.get("name", ""),
                home_team=home_name,
                away_team=away_name,
                kickoff_time=kickoff,
                score=score,
                odds_home=str(main_odds.get("home", "")),
                odds_draw=str(main_odds.get("draw", "")),
                odds_away=str(main_odds.get("away", "")),
                asian_handicap=str(odds_data.get("asianHandicap", {}).get("home", "") if odds_data else ""),
                over_under=str(odds_data.get("overUnder", {}).get("over", "") if odds_data else ""),
            )
        except Exception as e:
            self.logger.warning(f"解析赛事失败: {e}")
            return None
