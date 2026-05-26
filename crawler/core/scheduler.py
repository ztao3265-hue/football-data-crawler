"""定时采集调度器 — 按固定间隔自动执行全量采集"""

import asyncio
from datetime import datetime

from crawler.core.logger import get_logger
from crawler.core.engine import CrawlerEngine
from crawler.utils.helpers import load_json

logger = get_logger(__name__)


class CrawlScheduler:
    """定时采集调度器"""

    def __init__(self, interval_minutes: int = 30, sources: list = None,
                 headless: bool = True, output_dir: str = "exports"):
        self.interval = interval_minutes * 60
        self.sources = sources or ["sofascore", "fotmob"]
        self.headless = headless
        self.output_dir = output_dir
        self._running = False
        self._task: asyncio.Task | None = None
        self.stats = {"runs": 0, "total_matches": 0, "last_run": None, "errors": 0}

    async def start(self):
        """启动定时调度"""
        self._running = True
        logger.info(f"定时采集已启动: 每 {self.interval // 60} 分钟采集一次")
        logger.info(f"数据源: {', '.join(self.sources)}")

        # 首次立即执行一次
        await self._run_once()

        while self._running:
            await asyncio.sleep(self.interval)
            if self._running:
                await self._run_once()

    async def _run_once(self):
        """执行一次采集"""
        run_start = datetime.now()
        date_str = run_start.strftime("%Y-%m-%d")
        logger.info(f"[定时采集 #{self.stats['runs'] + 1}] 开始: {run_start.isoformat()}")

        engine = CrawlerEngine(headless=self.headless, output_dir=self.output_dir)

        try:
            sources_config = load_json("configs/sources.json")
            await engine.start_browser()
            await engine.run_sources(self.sources, date_str, sources_config)
            engine.export(date_str)
            engine.print_summary()

            total = sum(r.matches_count for r in engine.results)
            self.stats["runs"] += 1
            self.stats["total_matches"] += total
            self.stats["last_run"] = datetime.now().isoformat()

            duration = (datetime.now() - run_start).total_seconds()
            logger.info(f"[定时采集 #{self.stats['runs']}] 完成: {total} 条, 耗时 {duration:.1f}s")

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"[定时采集] 失败: {e}")

        finally:
            await engine.stop_browser()

    def stop(self):
        """停止调度"""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info(f"定时采集已停止 (共执行 {self.stats['runs']} 次)")


async def run_scheduler(interval_minutes: int = 30, sources: list = None,
                        headless: bool = True):
    """入口函数：创建并启动调度器"""
    scheduler = CrawlScheduler(
        interval_minutes=interval_minutes,
        sources=sources,
        headless=headless,
    )
    await scheduler.start()
