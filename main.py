#!/usr/bin/env python3
"""足球数据采集系统 - 命令行入口

用法:
    python main.py --source sofascore --date today
    python main.py --source fotmob --date today
    python main.py --all --date today
    python main.py --ui
    python main.py --setup
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from crawler.core.logger import setup_logger
from crawler.core.engine import CrawlerEngine
from crawler.utils.helpers import parse_date, get_env_bool, load_json


def main():
    parser = argparse.ArgumentParser(
        description="足球数据采集系统 - Football Data Crawler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--source", "-s",
        type=str,
        choices=["sofascore", "fotmob", "football-data"],
        help="指定数据源",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="采集所有数据源",
    )
    parser.add_argument(
        "--date", "-d",
        type=str,
        default="today",
        help="采集日期 (today / yesterday / YYYY-MM-DD), 默认 today",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="启动 Web 管理界面",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=8080,
        help="Web UI 端口, 默认 8080",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="运行初始化设置（安装依赖、Playwright、初始化 git）",
    )
    parser.add_argument(
        "--headless",
        type=str,
        default="true",
        choices=["true", "false"],
        help="是否无头模式运行浏览器 (true/false)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="exports",
        help="导出目录，默认 exports",
    )

    args = parser.parse_args()

    # 设置日志
    setup_logger(log_level="INFO")

    # 初始化
    if args.setup:
        asyncio.run(run_setup())
        return

    # Web UI
    if args.ui:
        run_web_ui(args.ui_port)
        return

    # 采集
    if not args.source and not args.all:
        parser.print_help()
        print("\n请使用 --source 或 --all 指定数据源，或使用 --ui 启动 Web 界面")
        sys.exit(1)

    date_str = parse_date(args.date)
    headless = args.headless.lower() == "true"

    asyncio.run(run_crawl(args, date_str, headless))


async def run_crawl(args, date_str: str, headless: bool):
    """执行采集任务"""
    engine = CrawlerEngine(headless=headless, output_dir=args.output)

    try:
        # 加载配置
        sources_config = load_json("configs/sources.json")

        if args.all:
            source_names = ["sofascore", "fotmob", "football-data"]
        else:
            source_names = [args.source]

        # 启动浏览器
        await engine.start_browser()

        # 运行采集
        await engine.run_sources(source_names, date_str, sources_config)

        # 导出
        engine.export(date_str)

        # 打印摘要
        engine.print_summary()

    except Exception as e:
        from crawler.core.logger import get_logger
        get_logger("main").error(f"采集任务失败: {e}")
        raise

    finally:
        await engine.stop_browser()


def run_web_ui(port: int = 8080):
    """启动 Web 管理界面"""
    from crawler.core.logger import get_logger
    log = get_logger("ui")

    try:
        from crawler.ui.server import start_ui
        log.info(f"启动 Web 管理界面: http://127.0.0.1:{port}")
        start_ui(port=port)
    except ImportError:
        log.error("Web UI 模块未找到，请确保 crawler/ui/ 目录存在")
        sys.exit(1)


async def run_setup():
    """运行初始化设置"""
    print("\n" + "=" * 60)
    print("  足球数据采集系统 - 初始化设置")
    print("=" * 60 + "\n")

    # 1. 检查 Python 版本
    import platform
    py_ver = platform.python_version()
    print(f"[1/5] Python 版本: {py_ver}")

    # 2. 安装 Python 依赖
    print("[2/5] 安装 Python 依赖...")
    req_file = Path("requirements.txt")
    if req_file.exists():
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            check=True,
        )
        print("  [OK] 依赖安装完成")
    else:
        print("  [FAIL] requirements.txt 未找到")

    # 3. 安装 Playwright Chromium
    print("[3/5] 安装 Playwright Chromium...")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        print("  [OK] Playwright Chromium 已就绪")
    except Exception:
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )
        print("  [OK] Playwright Chromium 安装完成")

    # 4. 初始化 Git
    print("[4/5] 初始化 Git...")
    if not Path(".git").exists():
        import subprocess
        subprocess.run(["git", "init"], check=True)
        print("  [OK] Git 仓库已初始化")
    else:
        print("  - Git 仓库已存在，跳过")

    # 5. 检查 .env 文件
    print("[5/5] 检查 .env 配置...")
    env_file = Path(".env")
    if env_file.exists():
        content = env_file.read_text(encoding="utf-8")
        if "FOOTBALL_DATA_ORG_API_KEY=" in content:
            key_part = content.split("FOOTBALL_DATA_ORG_API_KEY=")[1].split("\n")[0].strip()
            if key_part and key_part != '""' and key_part != "''":
                print("  [OK] football-data.org API Key 已配置")
            else:
                print("  [WARN] football-data.org API Key 未设置（该数据源将跳过）")
                print("    前往 https://www.football-data.org/client/register 免费注册")
    else:
        print("  [X] .env 文件未找到")

    print("\n" + "=" * 60)
    print("  初始化完成！")
    print()
    print("  快速开始:")
    print("    python main.py --source sofascore --date today")
    print("    python main.py --all --date today")
    print("    python main.py --ui")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
