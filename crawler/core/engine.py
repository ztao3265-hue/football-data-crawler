"""采集引擎 - 协调所有采集器运行"""

import time
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List

from crawler.core.logger import get_logger
from crawler.core.models import MatchData, CrawlResult
from crawler.core.exporter import Exporter
from crawler.core.rate_limiter import RateLimiter
from crawler.browser.manager import BrowserManager

logger = get_logger(__name__)


class CrawlerEngine:
    """采集引擎：管理浏览器、协调采集器、处理导出"""

    def __init__(self, headless: bool = True, output_dir: str = "exports"):
        self.headless = headless
        self.browser: BrowserManager | None = None
        self.exporter = Exporter(output_dir=output_dir)
        self.results: list[CrawlResult] = []
        self.all_matches: list[MatchData] = []

    async def start_browser(self):
        """启动浏览器"""
        self.browser = BrowserManager(headless=self.headless)
        await self.browser.start()

    async def stop_browser(self):
        """关闭浏览器"""
        if self.browser:
            await self.browser.stop()

    async def run_source(self, source_name: str, date_str: str,
                         config: dict) -> CrawlResult:
        """运行单个采集源"""
        start_time = time.time()
        logger.info(f"开始采集 [ {source_name} ] 日期: {date_str}")

        try:
            match source_name:
                case "sofascore":
                    from crawler.sources.sofascore import SofascoreCrawler
                    crawler = SofascoreCrawler(self.browser, config)
                case "fotmob":
                    from crawler.sources.fotmob import FotmobCrawler
                    crawler = FotmobCrawler(self.browser, config)
                case "football-data":
                    from crawler.sources.football_data import FootballDataCrawler
                    crawler = FootballDataCrawler(config)
                case _:
                    raise ValueError(f"未知数据源: {source_name}")

            matches = await crawler.collect(date_str)
            duration = time.time() - start_time

            result = CrawlResult(
                source=source_name,
                status="success" if matches else "partial",
                matches_count=len(matches),
                duration_seconds=round(duration, 2),
                details={"date": date_str},
            )

            self.all_matches.extend(matches)
            logger.info(f"[ {source_name} ] 完成，获取 {len(matches)} 条数据，耗时 {duration:.1f}s")

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"[ {source_name} ] 采集失败: {e}")
            result = CrawlResult(
                source=source_name,
                status="failed",
                matches_count=0,
                duration_seconds=round(duration, 2),
                error_message=str(e),
                details={"date": date_str},
            )

        self.results.append(result)
        return result

    async def run_sources(self, source_names: List[str], date_str: str,
                          sources_config: dict) -> list[CrawlResult]:
        """依次运行多个采集源"""
        all_sources = sources_config.get("sources", [])
        source_map = {s["name"]: s for s in all_sources if s.get("enabled", True)}

        for name in source_names:
            if name not in source_map:
                logger.warning(f"数据源 '{name}' 未在配置中找到或已禁用，已跳过")
                continue
            await self.run_source(name, date_str, source_map[name])

        return self.results

    def export(self, date_str: str, import_to_db: bool = True):
        """导出所有采集数据（含清洗版本 + 数据库入库）"""
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        if self.all_matches:
            self.exporter.export(self.all_matches, date_str)
            # 清洗导出
            from crawler.core.cleaner import export_clean
            raw_data = [m.to_dict() if hasattr(m, "to_dict") else m for m in self.all_matches]
            cleaned = export_clean(raw_data, output_dir=self.exporter.output_dir, date_str=date_str)
            # 数据库入库
            if import_to_db:
                self._import_to_database(cleaned)
        if self.results:
            self.exporter.export_summary(self.results, date_str)

    def _import_to_database(self, cleaned_matches: list):
        """将清洗后的数据导入 PostgreSQL"""
        try:
            from crawler.database.importer import MatchImporter
            from crawler.database.connection import get_db

            db = get_db()
            if db.test_connection():
                db.create_all()
                importer = MatchImporter()
                stats = importer.import_matches(cleaned_matches)
                logger.info(
                    f"数据库入库: +{stats['inserted']} 新增, "
                    f"~{stats['updated']} 更新, "
                    f"-{stats['skipped']} 跳过, "
                    f"x{stats['errors']} 错误"
                )
        except Exception as e:
            logger.warning(f"数据库导入跳过（PostgreSQL 未就绪）: {e}")

    def print_summary(self):
        """打印采集摘要"""
        print("\n" + "=" * 60)
        print("  采集摘要")
        print("=" * 60)
        total_matches = sum(r.matches_count for r in self.results)
        success_count = sum(1 for r in self.results if r.status == "success")
        for r in self.results:
            status_icon = "[OK]" if r.status == "success" else ("[~]" if r.status == "partial" else "[X]")
            print(f"  {status_icon} {r.source:20s} | {r.matches_count:4d} 条 | {r.duration_seconds:6.1f}s | {r.status}")
        print("-" * 60)
        print(f"  总计: {len(self.results)} 个数据源, {total_matches} 条比赛数据")
        print(f"  成功: {success_count}/{len(self.results)}")
        print("=" * 60 + "\n")
