"""XHR/API 拦截器 - 捕获页面发出的 API 请求"""

import json
import asyncio
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import Page, Route

from crawler.core.logger import get_logger

logger = get_logger(__name__)


class APIInterceptor:
    """拦截并记录页面发出的 API 请求"""

    def __init__(self, page: Page):
        self.page = page
        self.api_responses: list[dict] = []
        self._url_filters: list[str] = []
        self._on_response: Optional[Callable] = None

    def add_url_filter(self, pattern: str):
        """添加 URL 过滤规则（包含 pattern 的 URL 才会被记录）"""
        self._url_filters.append(pattern)

    def on_response(self, callback: Callable):
        """设置响应回调"""
        self._on_response = callback

    async def start(self):
        """开始拦截"""
        self.page.on("response", self._handle_response)
        logger.info("API 拦截器已启动")

    async def _handle_response(self, response):
        """处理响应"""
        try:
            url = response.url
            content_type = response.headers.get("content-type", "")

            # 只拦截 API 请求（JSON）
            is_api = "json" in content_type or any(
                f in url for f in self._url_filters
            )

            if not is_api and self._url_filters:
                is_api = any(f in url for f in self._url_filters)

            if not is_api:
                return

            if response.status >= 400:
                return

            body = None
            try:
                body = await response.json()
            except Exception:
                try:
                    text = await response.text()
                    body = {"_text": text[:5000]}
                except Exception:
                    body = {"_error": "无法读取响应体"}

            record = {
                "url": url,
                "status": response.status,
                "method": response.request.method,
                "headers": dict(response.headers),
                "body": body,
                "timestamp": asyncio.get_event_loop().time(),
            }

            self.api_responses.append(record)

            if self._on_response:
                self._on_response(record)

        except Exception as e:
            logger.debug(f"拦截响应出错: {e}")

    async def stop(self):
        """停止拦截"""
        self.page.remove_listener("response", self._handle_response)

    def get_filtered_responses(self, url_fragment: str) -> list[dict]:
        """获取匹配 URL 片段的响应"""
        return [r for r in self.api_responses if url_fragment in r["url"]]

    def save_responses(self, filepath: str | Path):
        """保存所有拦截到的 API 响应"""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 去掉 body 的深层嵌套用于保存
        saved = []
        for r in self.api_responses:
            item = {
                "url": r["url"],
                "status": r["status"],
                "method": r["method"],
            }
            if isinstance(r.get("body"), dict):
                # 限制保存大小
                body_str = json.dumps(r["body"], ensure_ascii=False)
                if len(body_str) > 100000:
                    item["body"] = body_str[:100000] + "...[截断]"
                else:
                    item["body"] = r["body"]
            saved.append(item)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f, ensure_ascii=False, indent=2)
        logger.info(f"API 响应已保存: {path} ({len(saved)} 条)")
