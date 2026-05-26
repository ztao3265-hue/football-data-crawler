"""限速器 - 请求频率控制，避免被封"""

import asyncio
import random
import time
from collections import deque
from datetime import datetime

from crawler.core.logger import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """异步限速器，控制请求频率"""

    def __init__(self, min_delay: float = 2.0, max_delay: float = 5.0,
                 max_per_minute: int = 20):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_per_minute = max_per_minute
        self._request_times: deque = deque()
        self._last_request_time = 0.0

    async def wait(self):
        """等待直到可以发送下一个请求"""
        now = time.time()

        # 清理 60 秒前的记录
        cutoff = now - 60
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()

        # 检查每分钟限制
        if len(self._request_times) >= self.max_per_minute:
            wait_time = self._request_times[0] + 60 - now
            if wait_time > 0:
                logger.debug(f"达到每分钟限制 ({self.max_per_minute})，等待 {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
                now = time.time()

        # 随机延迟
        delay = random.uniform(self.min_delay, self.max_delay)
        time_since_last = now - self._last_request_time
        if time_since_last < delay:
            await asyncio.sleep(delay - time_since_last)

        self._request_times.append(time.time())
        self._last_request_time = time.time()

    async def execute(self, coro):
        """执行一个协程，自动添加限速"""
        await self.wait()
        return await coro
