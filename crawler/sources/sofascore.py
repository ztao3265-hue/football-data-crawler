"""Sofascore 数据采集器 - 含欧赔/亚盘/大小球赔率"""

from datetime import datetime
from typing import List

from crawler.sources.base import BaseCrawler
from crawler.core.models import MatchData
from crawler.browser.interceptor import APIInterceptor
from crawler.utils.helpers import save_json, save_text

ODDS_FETCH_ENABLED = True


def _frac_to_decimal(frac: str) -> str:
    """分数赔率转小数，如 '11/20' -> '1.55'"""
    if not frac or '/' not in frac:
        return frac
    try:
        parts = frac.split('/')
        num = float(parts[0])
        den = float(parts[1])
        return f"{num / den + 1:.2f}"
    except (ValueError, ZeroDivisionError):
        return frac


def _extract_asian_line(name: str) -> str:
    """从赔率选项名称提取亚盘盘口，如 '(-1.5) Team' -> '-1.5'"""
    if not name:
        return ""
    start = name.find("(")
    end = name.find(")")
    if start != -1 and end != -1 and end > start:
        return name[start + 1:end]
    return ""


class SofascoreCrawler(BaseCrawler):
    """Sofascore 采集器 - 通过 API + 浏览器双通道采集"""

    def __init__(self, browser, config: dict):
        super().__init__("sofascore", config)
        self.browser = browser
        self.api_base = config.get("api_base", "https://api.sofascore.com/api/v1")
        self.base_url = config.get("base_url", "https://www.sofascore.com")

    async def collect(self, date_str: str) -> List[MatchData]:
        self.logger.info(f"开始采集 Sofascore 数据: {date_str}")
        all_matches = []
        seen = set()

        # 浏览器采集（捕获 per-tournament API + 赔率）
        browser_matches = await self._collect_via_browser(date_str)
        for m in browser_matches:
            key = f"{m.home_team}|{m.away_team}|{m.kickoff_time[:10]}"
            if key not in seen:
                seen.add(key)
                all_matches.append(m)
        self.logger.info(f"浏览器采集到 {len(browser_matches)} 条（去重后 {len(all_matches)} 条）")

        # API 采集作为补充
        api_matches = await self._collect_via_api(date_str)
        added = 0
        for m in api_matches:
            key = f"{m.home_team}|{m.away_team}|{m.kickoff_time[:10]}"
            if key not in seen:
                seen.add(key)
                all_matches.append(m)
                added += 1
        if added:
            self.logger.info(f"API 补充采集到 {added} 条")

        raw_data = [m.to_dict() for m in all_matches]
        if raw_data:
            save_json(raw_data, f"data/raw/sofascore_{date_str}.json")

        return all_matches

    async def _collect_via_api(self, date_str: str) -> List[MatchData]:
        """通过 API 采集（全局 API 需浏览器 cookie 已禁用，由浏览器路径替代）"""
        return []

    async def _collect_via_browser(self, date_str: str) -> List[MatchData]:
        """浏览器采集：拦截 per-tournament API + 赔率 API 响应"""
        matches = []
        page = None

        try:
            page = await self.browser.new_page()

            interceptor = APIInterceptor(page)
            interceptor.add_url_filter("/api/v1/unique-tournament/")
            interceptor.add_url_filter("/api/v1/sport/football")
            interceptor.add_url_filter("/api/v1/event/")
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

            # 解析拦截到的赛事数据
            api_responses = interceptor.get_filtered_responses("events")
            seen_ids = set()
            event_map = {}

            for resp in api_responses:
                body = resp.get("body", {})
                if isinstance(body, dict):
                    events = body.get("events", [])
                    for event in events:
                        eid = str(event.get("id", ""))
                        if eid in seen_ids or not eid:
                            continue
                        seen_ids.add(eid)
                        match = self._parse_event(event)
                        if match:
                            matches.append(match)
                            event_map[eid] = match

            # 从拦截的赔率响应中提取赔率数据
            if ODDS_FETCH_ENABLED:
                odds_responses = interceptor.get_filtered_responses("/odds/")
                odds_count = 0
                for resp in odds_responses:
                    body = resp.get("body", {})
                    if isinstance(body, dict) and "featured" in body:
                        # 从 URL 中提取 event_id: /api/v1/event/{event_id}/odds/1/featured
                        url = resp.get("url", "")
                        parts = url.split("/")
                        try:
                            odds_idx = parts.index("odds")
                            eid = parts[odds_idx - 1]
                        except (ValueError, IndexError):
                            continue
                        match = event_map.get(eid)
                        if match and body.get("featured"):
                            self._apply_odds(match, body)
                            odds_count += 1
                self.logger.info(f"赔率采集: {odds_count}/{len(event_map)} 场")

            interceptor.save_responses(f"data/raw/sofascore_api_{date_str}.json")

        except Exception as e:
            self.logger.error(f"Sofascore 浏览器采集失败: {e}")

        finally:
            if page:
                await page.close()

        return matches

    def _apply_odds(self, match: MatchData, odds_data: dict):
        """解析真实 API 响应: featured.default (1X2) + featured.asian (亚盘)"""
        featured = odds_data.get("featured", {}) or {}

        # 欧赔 1X2: featured.default.choices[] = [{name:"1",fractionalValue:"11/20"}, ...]
        default_market = featured.get("default") or featured.get("fullTime") or {}
        choices = default_market.get("choices", [])
        if choices:
            for c in choices:
                name = c.get("name", "")
                frac = str(c.get("fractionalValue", ""))
                decimal = _frac_to_decimal(frac)
                if name == "1":
                    match.odds_home = decimal
                elif name == "X":
                    match.odds_draw = decimal
                elif name == "2":
                    match.odds_away = decimal

        # 亚盘: featured.asian.choices[] = [{name:"(-1.5) Team", fractionalValue:"1/1"}, ...]
        asian_market = featured.get("asian", {})
        asian_choices = asian_market.get("choices", [])
        if asian_choices:
            lines = []
            for c in asian_choices:
                line = _extract_asian_line(c.get("name", ""))
                if line:
                    lines.append(line)
            if lines:
                match.asian_handicap = ", ".join(lines)

        # 大小球: 检查是否有 overUnder 市场
        ou_market = featured.get("overUnder", {})
        ou_choices = ou_market.get("choices", [])
        if ou_choices:
            totals = []
            for c in ou_choices:
                name = c.get("name", "")
                total = _extract_asian_line(name)
                if total:
                    totals.append(total)
            if totals:
                match.over_under = ", ".join(totals)

    def _parse_event(self, event: dict) -> MatchData | None:
        """解析赛事事件为统一格式"""
        try:
            home_team = event.get("homeTeam", {})
            away_team = event.get("awayTeam", {})
            tournament = event.get("tournament", {})

            home_name = home_team.get("name", "")
            away_name = away_team.get("name", "")

            if not home_name or not away_name:
                return None

            status = event.get("status", {})
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
