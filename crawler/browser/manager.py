"""浏览器管理器 - Playwright Chromium 生命周期管理"""

import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from crawler.core.logger import get_logger

logger = get_logger(__name__)


class BrowserManager:
    """Playwright 浏览器管理器"""

    def __init__(self, headless: bool = True, viewport: dict = None,
                 locale: str = "zh-CN", timezone_id: str = "Asia/Shanghai"):
        self.headless = headless
        self.viewport = viewport or {"width": 1920, "height": 1080}
        self.locale = locale
        self.timezone_id = timezone_id
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self):
        """启动浏览器"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            viewport=self.viewport,
            locale=self.locale,
            timezone_id=self.timezone_id,
        )
        # 反检测：隐藏自动化标志
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            window.chrome = { runtime: {} };
        """)
        logger.info("浏览器已启动")

    async def stop(self):
        """关闭浏览器"""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("浏览器已关闭")

    async def new_page(self) -> Page:
        """创建新页面"""
        if not self._context:
            raise RuntimeError("浏览器未启动，请先调用 start()")
        page = await self._context.new_page()
        return page

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise RuntimeError("浏览器未启动")
        return self._context

    async def screenshot(self, page: Page, filename: str, full_page: bool = True):
        """截取页面截图"""
        screenshot_dir = Path("crawler/output/screenshots")
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = screenshot_dir / filename
        await page.screenshot(path=str(path), full_page=full_page)
        logger.info(f"截图已保存: {path}")
        return str(path)

    async def get_page_html(self, page: Page) -> str:
        """获取页面完整 HTML"""
        return await page.content()

    async def wait_for_network_idle(self, page: Page, timeout: int = 30000):
        """等待网络空闲"""
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            logger.warning("等待网络空闲超时")

    async def scroll_page(self, page: Page, times: int = 3, delay: float = 1.0):
        """滚动页面以触发懒加载"""
        for i in range(times):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {(i+1)/times})")
            await asyncio.sleep(delay)
        await page.evaluate("window.scrollTo(0, 0)")
