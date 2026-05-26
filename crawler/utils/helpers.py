"""工具函数"""

import os
import json
import hashlib
from pathlib import Path
from datetime import datetime, date
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent.parent


def get_project_root() -> Path:
    return PROJECT_ROOT


def load_json(path: str | Path) -> dict:
    """加载 JSON 文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: dict | list, path: str | Path, indent: int = 2):
    """保存 JSON 文件"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def save_text(content: str, path: str | Path):
    """保存文本文件"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def make_cache_key(*args) -> str:
    """生成缓存 key"""
    raw = "|".join(str(a) for a in args)
    return hashlib.md5(raw.encode()).hexdigest()


def get_today_str() -> str:
    """获取今天的日期字符串"""
    return datetime.now().strftime("%Y-%m-%d")


def parse_date(date_str: str) -> str:
    """解析日期参数，支持 'today', 'yesterday', 'YYYY-MM-DD' 格式"""
    today = date.today()
    match date_str.lower():
        case "today":
            return today.isoformat()
        case "yesterday":
            return (today.replace(day=today.day - 1)).isoformat()
        case _:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
                return date_str
            except ValueError:
                raise ValueError(f"无效的日期格式: {date_str}，请使用 YYYY-MM-DD")


def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def get_env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").lower()
    if not val:
        return default
    return val in ("true", "1", "yes", "on")


def safe_filename(name: str) -> str:
    """生成安全的文件名"""
    return "".join(c for c in name if c.isalnum() or c in "._- ").strip().replace(" ", "_")
