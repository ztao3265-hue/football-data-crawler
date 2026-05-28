#!/usr/bin/env python3
"""足球数据采集系统 - 命令行入口

用法:
    python main.py --source sofascore --date today
    python main.py --source fotmob --date today
    python main.py --all --date today
    python main.py --ui
    python main.py --setup
    python main.py --schedule
    python main.py --api
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
        default="D:/FootballData/exports",
        help="导出目录，默认 D:/FootballData/exports",
    )
    parser.add_argument(
        "--db-init",
        action="store_true",
        help="初始化 PostgreSQL 数据库表结构",
    )
    parser.add_argument(
        "--db-import",
        type=str,
        nargs="?",
        const="D:/FootballData/exports/clean_matches.json",
        help="将 clean_matches.json 导入数据库",
    )
    parser.add_argument(
        "--db-status",
        action="store_true",
        help="查看数据库状态和统计",
    )
    parser.add_argument(
        "--db-test",
        action="store_true",
        help="测试数据库连接",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="启动定时自动采集服务",
    )
    parser.add_argument(
        "--schedule-interval",
        type=int,
        default=30,
        help="定时采集间隔（分钟），默认 30",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="启动 FastAPI 数据接口服务",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=8000,
        help="API 服务端口，默认 8000",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="跳过数据库入库（仅导出文件）",
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

    # 数据库操作
    if args.db_test:
        asyncio.run(run_db_test())
        return
    if args.db_init:
        asyncio.run(run_db_init())
        return
    if args.db_import:
        asyncio.run(run_db_import(args.db_import))
        return
    if args.db_status:
        asyncio.run(run_db_status())
        return

    # 定时调度
    if args.schedule:
        asyncio.run(run_schedule(args.schedule_interval, args.all, args.headless))
        return

    # API 服务
    if args.api:
        run_api(args.api_port)
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
        engine.export(date_str, import_to_db=not args.no_db)

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
    print("    python main.py --db-init")
    print("    python main.py --db-import")
    print("=" * 60 + "\n")


async def run_db_test():
    """测试数据库连接"""
    from crawler.database.connection import get_db
    db = get_db()
    print(f"连接地址: {db.database_url.replace(db.database_url.split('@')[0].split('://')[-1], '***') if '@' in db.database_url else db.database_url}")
    ok = db.test_connection()
    if ok:
        print("数据库连接正常")
    else:
        print("数据库连接失败，请确认 PostgreSQL 已启动")


async def run_db_init():
    """初始化数据库表"""
    from crawler.database.connection import get_db
    db = get_db()
    if not db.test_connection():
        print("无法连接数据库，请先启动 PostgreSQL")
        return
    db.create_all()
    print("数据库表创建完成: leagues, teams, matches, odds")


async def run_db_import(filepath: str):
    """从文件导入数据到数据库"""
    from crawler.database.connection import get_db
    from crawler.database.importer import import_clean_file

    db = get_db()
    if not db.test_connection():
        print("无法连接数据库，请先启动 PostgreSQL")
        return

    db.create_all()
    stats = import_clean_file(filepath)

    print(f"\n导入结果:")
    print(f"  新增: {stats['inserted']} 条")
    print(f"  更新: {stats['updated']} 条")
    print(f"  跳过: {stats['skipped']} 条")
    print(f"  错误: {stats['errors']} 条")


async def run_db_status():
    """查看数据库状态"""
    from crawler.database.connection import get_db
    from sqlalchemy import text

    db = get_db()
    if not db.test_connection():
        print("无法连接数据库，请先启动 PostgreSQL")
        return

    with db.session() as session:
        tables = ["matches", "odds", "odds_history", "teams", "leagues"]
        print("\n数据库状态:")
        print("-" * 40)
        for table in tables:
            result = session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            count = result.scalar()
            print(f"  {table:12s}: {count:6d} 行")

        # 比赛状态分布
        result = session.execute(text(
            "SELECT status, COUNT(*) FROM matches GROUP BY status ORDER BY COUNT(*) DESC"
        ))
        print("\n比赛状态分布:")
        for row in result:
            print(f"  {row[0]:12s}: {row[1]:6d}")

        # 数据源分布
        result = session.execute(text(
            "SELECT source, COUNT(*) FROM matches GROUP BY source ORDER BY COUNT(*) DESC"
        ))
        print("\n数据源分布:")
        for row in result:
            print(f"  {row[0]:20s}: {row[1]:6d}")
        print("-" * 40)


async def run_schedule(interval_minutes: int = 30, all_sources: bool = False,
                       headless_str: str = "true"):
    """启动定时采集调度"""
    from crawler.core.scheduler import run_scheduler

    sources = None
    if not all_sources:
        sources = ["sofascore", "fotmob"]

    headless = headless_str.lower() == "true"
    print(f"\n定时采集已启动: 每 {interval_minutes} 分钟一次")
    print(f"数据源: {', '.join(sources or ['全部'])}")
    print("按 Ctrl+C 停止\n")
    await run_scheduler(interval_minutes=interval_minutes, sources=sources,
                        headless=headless)


def run_api(port: int = 8000):
    """启动 FastAPI 数据接口"""
    try:
        from crawler.api.server import start_api
        print(f"\nFastAPI 数据接口: http://localhost:{port}")
        print(f"API 文档: http://localhost:{port}/docs\n")
        start_api(port=port)
    except ImportError as e:
        print(f"FastAPI 未安装，请运行: pip install fastapi uvicorn")
        print(f"错误详情: {e}")


if __name__ == "__main__":
    main()
