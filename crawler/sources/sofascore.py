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
        self.logger.info(f"[Sofascore] 开始采集: {date_str}")
        all_matches = []
        seen = set()

        # 浏览器采集（捕获 per-tournament API + 赔率）
        browser_matches = await self._collect_via_browser(date_str)
        for m in browser_matches:
            key = f"{m.home_team}|{m.away_team}|{m.kickoff_time[:10]}"
            if key not in seen:
                seen.add(key)
                all_matches.append(m)
        self.logger.info(f"[Sofascore] 浏览器采集: {len(browser_matches)} 条（去重后 {len(all_matches)} 条）")

        # 按联赛统计
        league_counts = {}
        league_odds = {}
        for m in all_matches:
            league_counts[m.league] = league_counts.get(m.league, 0) + 1
            if m.odds_home:
                league_odds[m.league] = league_odds.get(m.league, 0) + 1
        top_leagues = sorted(league_counts.items(), key=lambda x: x[1], reverse=True)[:8]
        self.logger.info(f"[Sofascore] 联赛分布: {', '.join(f'{l}({c})' for l, c in top_leagues)}")
        if league_odds:
            self.logger.info(f"[Sofascore] 含赔率联赛: {', '.join(f'{l}({c})' for l, c in sorted(league_odds.items(), key=lambda x: x[1], reverse=True))}")

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
            self.logger.info(f"[Sofascore] API 补充采集: {added} 条")

        raw_data = [m.to_dict() for m in all_matches]
        if raw_data:
            from config.paths import RAW_DATA_DIR
            save_json(raw_data, str(RAW_DATA_DIR / f"sofascore_{date_str}.json"))

        return all_matches

    async def _collect_via_api(self, date_str: str) -> List[MatchData]:
        """通过 API 采集（全局 API 需浏览器 cookie 已禁用，由浏览器路径替代）"""
        return []

    async def _collect_via_browser(self, date_str: str) -> List[MatchData]:
        """浏览器采集：拦截 per-tournament API + 赔率 API 响应"""
        matches = []
        page = None
        stats = {"events_api": 0, "odds_api": 0, "odds_ok": 0, "odds_no_match": 0,
                 "odds_no_featured": 0, "odds_url_parse_err": 0}

        try:
            page = await self.browser.new_page()

            interceptor = APIInterceptor(page)
            interceptor.add_url_filter("/api/v1/unique-tournament/")
            interceptor.add_url_filter("/api/v1/sport/football")
            interceptor.add_url_filter("/api/v1/event/")
            interceptor.add_url_filter("featured-events")
            await interceptor.start()

            page_url = f"{self.base_url}/football/{date_str}"
            self.logger.info(f"[Sofascore] 请求页面: {page_url}")
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                self.logger.warning(f"[Sofascore] 页面加载超时/失败: {e}")
                # 尝试继续使用已加载的内容
            try:
                await self.browser.wait_for_network_idle(page, timeout=30000)
            except Exception:
                self.logger.debug(f"[Sofascore] wait_for_network_idle 超时，使用已有数据继续")

            try:
                await self.browser.scroll_page(page, times=3)
            except Exception:
                self.logger.debug(f"[Sofascore] scroll_page 失败，使用已有数据继续")

            html = await self.browser.get_page_html(page)
            from config.paths import RAW_DATA_DIR
            save_text(html, str(RAW_DATA_DIR / f"sofascore_{date_str}.html"))

            await self.browser.screenshot(
                page, f"sofascore_{date_str}_{datetime.now():%H%M%S}.png"
            )

            # 解析拦截到的赛事数据（来自多种 API 响应格式）
            # 1) tournament API: /unique-tournament/{id}/scheduled-events/{date}
            tournament_resps = interceptor.get_filtered_responses("scheduled-events")
            # 2) event detail API: /event/{id} (不含 odds/votes)
            event_detail_resps = [
                r for r in interceptor.api_responses
                if "/event/" in r.get("url", "") and "/odds/" not in r.get("url", "")
                and "/votes" not in r.get("url", "")
            ]
            # 3) featured-events API: /odds/{n}/featured-events/football
            featured_resps = interceptor.get_filtered_responses("featured-events")

            all_event_resps = tournament_resps + event_detail_resps + featured_resps
            stats["events_api"] = len(all_event_resps)
            self.logger.info(
                f"[Sofascore] 拦截赛事 API: tournament={len(tournament_resps)} "
                f"event_detail={len(event_detail_resps)} featured={len(featured_resps)}"
            )
            seen_ids = set()
            event_map = {}

            for resp in all_event_resps:
                body = resp.get("body", {})
                if not isinstance(body, dict):
                    continue

                candidates = []
                # tournament API → body.events[]
                if "events" in body:
                    candidates.extend(body["events"])
                # event detail → body.event (单对象)
                if isinstance(body.get("event"), dict):
                    candidates.append(body["event"])
                # featured-events → body.featuredEvents[]
                if "featuredEvents" in body:
                    candidates.extend(body["featuredEvents"])

                for event in candidates:
                    if not isinstance(event, dict):
                        continue
                    eid = str(event.get("id", ""))
                    if eid in seen_ids or not eid:
                        continue
                    seen_ids.add(eid)
                    match = self._parse_event(event)
                    if match:
                        matches.append(match)
                        event_map[eid] = match

            self.logger.info(f"[Sofascore] 解析赛事: {len(matches)} 场 (event_map: {len(event_map)} 个 ID)")

            # 从拦截的赔率响应中提取赔率数据
            if ODDS_FETCH_ENABLED:
                odds_responses = interceptor.get_filtered_responses("/odds/")
                stats["odds_api"] = len(odds_responses)
                matched_event_ids = set()

                for resp in odds_responses:
                    resp_url = resp.get("url", "")
                    resp_status = resp.get("status", 0)
                    body = resp.get("body", {})

                    if not isinstance(body, dict) or "featured" not in body:
                        stats["odds_no_featured"] += 1
                        continue

                    featured = body.get("featured")
                    if not featured:
                        stats["odds_no_featured"] += 1
                        self.logger.debug(f"[Sofascore] 赔率响应无 featured: {resp_url}")
                        continue

                    # 从 URL 中提取 event_id
                    parts = resp_url.split("/")
                    eid = ""
                    try:
                        odds_idx = parts.index("odds")
                        eid = parts[odds_idx - 1]
                    except (ValueError, IndexError):
                        stats["odds_url_parse_err"] += 1
                        self.logger.debug(f"[Sofascore] 赔率 URL 解析失败: {resp_url}")
                        continue

                    match = event_map.get(eid)
                    if not match:
                        stats["odds_no_match"] += 1
                        self.logger.debug(f"[Sofascore] 赔率 event_id={eid} 无匹配赛事 (status={resp_status})")
                        continue

                    self._apply_odds(match, body)
                    stats["odds_ok"] += 1
                    matched_event_ids.add(eid)
                    self.logger.debug(
                        f"[Sofascore] 赔率命中: event={eid} "
                        f"{match.home_team} vs {match.away_team} "
                        f"({match.league}) "
                        f"欧赔={match.odds_home}/{match.odds_draw}/{match.odds_away} "
                        f"亚盘={match.asian_handicap or '无'} "
                        f"大小球={match.over_under or '无'}"
                    )

                # 汇总日志
                self.logger.info(
                    f"[Sofascore] 赔率汇总: 拦截 {stats['odds_api']} 个响应, "
                    f"命中 {stats['odds_ok']} 场 ({len(matched_event_ids)} 个独立赛事), "
                    f"跳过: 无 featured={stats['odds_no_featured']}, "
                    f"无匹配赛事={stats['odds_no_match']}, "
                    f"URL 解析错={stats['odds_url_parse_err']}"
                )

                # 联赛维度统计
                league_odds = {}
                for eid in matched_event_ids:
                    m = event_map.get(eid)
                    if m:
                        league_odds[m.league] = league_odds.get(m.league, 0) + 1
                if league_odds:
                    self.logger.info(
                        f"[Sofascore] 含赔率联赛: "
                        + ", ".join(f"{l}({c})" for l, c in
                                     sorted(league_odds.items(), key=lambda x: x[1], reverse=True))
                    )

            from config.paths import RAW_DATA_DIR
            interceptor.save_responses(str(RAW_DATA_DIR / f"sofascore_api_{date_str}.json"))

        except Exception as e:
            self.logger.error(f"[Sofascore] 浏览器采集异常: {type(e).__name__}: {e}")

        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

        return matches

    def _apply_odds(self, match: MatchData, odds_data: dict):
        """解析真实 API 响应: featured.default (1X2) + featured.asian (亚盘)

        提取初盘(initialFractionalValue)和即时盘(fractionalValue)
        Provider ID=1 → bet365
        """
        featured = odds_data.get("featured", {}) or {}
        # 设置博彩商标识（Sofascore provider=1 对应 bet365）
        if not match.odds_bookmaker:
            match.odds_bookmaker = "bet365"

        # 欧赔 1X2: featured.default.choices[] = [{name:"1",fractionalValue:"11/20",initialFractionalValue:"1/1"}, ...]
        default_market = featured.get("default") or featured.get("fullTime") or {}
        choices = default_market.get("choices", [])
        if choices:
            for c in choices:
                name = c.get("name", "")
                # 即时盘
                frac = str(c.get("fractionalValue", ""))
                decimal = _frac_to_decimal(frac)
                # 初盘
                init_frac = str(c.get("initialFractionalValue", ""))
                init_decimal = _frac_to_decimal(init_frac)
                if name == "1":
                    match.odds_home = decimal
                    match.odds_home_open = init_decimal
                elif name == "X":
                    match.odds_draw = decimal
                    match.odds_draw_open = init_decimal
                elif name == "2":
                    match.odds_away = decimal
                    match.odds_away_open = init_decimal

        # 亚盘: featured.asian.choices[] = [{name:"(-1.5) Team", fractionalValue:"1/1",initialFractionalValue:"9/10"}, ...]
        asian_market = featured.get("asian", {})
        asian_choices = asian_market.get("choices", [])
        if asian_choices:
            parts_curr = []
            parts_open = []
            for c in asian_choices:
                name = c.get("name", "")
                line = _extract_asian_line(name)
                frac = str(c.get("fractionalValue", ""))
                init_frac = str(c.get("initialFractionalValue", ""))
                odds_curr = _frac_to_decimal(frac)
                odds_open = _frac_to_decimal(init_frac)
                if line:
                    parts_curr.append(f"({line})@{odds_curr}")
                    parts_open.append(f"({line})@{odds_open}")
            if parts_curr:
                match.asian_handicap = ", ".join(parts_curr)
                match.asian_handicap_open = ", ".join(parts_open)

        # 大小球: featured.overUnder.choices[] = [{name:"(2.5) Over",...}, ...]
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
                match.over_under_open = ", ".join(totals)

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
