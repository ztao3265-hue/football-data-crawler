"""采集器基类"""

import asyncio
from abc import ABC, abstractmethod
from typing import List

from crawler.core.models import MatchData
from crawler.core.rate_limiter import RateLimiter
from crawler.core.logger import get_logger


class BaseCrawler(ABC):
    """所有采集器的基类"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.logger = get_logger(f"crawler.{name}")

        rate_config = config.get("rate_limit", {})
        self.rate_limiter = RateLimiter(
            min_delay=rate_config.get("delay_range", [2, 4])[0],
            max_delay=rate_config.get("delay_range", [2, 4])[1],
            max_per_minute=rate_config.get("requests_per_minute", 15),
        )

    @abstractmethod
    async def collect(self, date_str: str) -> List[MatchData]:
        """采集指定日期的比赛数据"""
        pass

    async def safe_request(self, factory):
        """安全的请求：带限速和重试，factory 是一个返回协程的可调用对象"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await self.rate_limiter.execute(factory())
            except Exception as e:
                self.logger.warning(f"请求失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    raise
