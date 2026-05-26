"""赛前高频赔率采集 — 临近开球时缩短采样间隔

策略:
- 距开球 >6h:    每 60 分钟采集一次
- 距开球 2h-6h:  每 30 分钟采集一次
- 距开球 30m-2h: 每 15 分钟采集一次
- 距开球 <30m:   每 5 分钟采集一次
- 比赛进行中:    每 10 分钟采集一次 (滚球盘)

用法:
    py -3 collect_pre_match.py                    # 单次采集 (手动定时触发)
    py -3 collect_pre_match.py --loop 60          # 循环采集, 间隔 60 分钟
    py -3 collect_pre_match.py --loop 5 --urgent  # 紧急模式, 5 分钟间隔

标准结构: match_id + bookmaker + timestamp
"""

import os
import sys
import time
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from crawler.core.logger import setup_logger, get_logger
from crawler.database.connection import get_db
from crawler.browser.manager import BrowserManager
from crawler.sources.sofascore import SofascoreCrawler
from sqlalchemy import text

logger = get_logger(__name__)


def get_upcoming_matches(session, hours_ahead: int = 12) -> list[dict]:
    """获取未来 N 小时内开球的比赛"""
    now = datetime.now()
    cutoff = now + timedelta(hours=hours_ahead)

    rows = session.execute(text("""
        SELECT match_id, home_team, away_team, league_name, kickoff_time,
               home_score, away_score, status
        FROM matches
        WHERE kickoff_time BETWEEN :now AND :cutoff
        ORDER BY kickoff_time
    """), {"now": now.strftime("%Y-%m-%d %H:%M"), "cutoff": cutoff.strftime("%Y-%m-%d %H:%M")}).fetchall()

    matches = []
    for r in rows:
        kickoff = r.kickoff_time
        minutes_to_ko = (kickoff - now).total_seconds() / 60 if kickoff else 999

        # 确定采集优先级和间隔
        if minutes_to_ko <= 0:
            priority, interval = "IN-PLAY", 10
        elif minutes_to_ko <= 30:
            priority, interval = "URGENT", 5
        elif minutes_to_ko <= 120:
            priority, interval = "HIGH", 15
        elif minutes_to_ko <= 360:
            priority, interval = "MEDIUM", 30
        else:
            priority, interval = "LOW", 60

        matches.append({
            "match_id": r.match_id,
            "home_team": r.home_team,
            "away_team": r.away_team,
            "league": r.league_name,
            "kickoff": kickoff,
            "minutes_to_ko": round(minutes_to_ko, 1),
            "priority": priority,
            "interval_min": interval,
            "status": r.status,
        })

    return matches


def print_match_schedule(matches: list[dict]):
    """打印赛前比赛排期"""
    if not matches:
        print("  无即将开球的比赛")
        return

    print(f"\n  {'优先':<8} {'距开球':<10} {'间隔':<8} {'比赛':<40} {'开球时间'}")
    print(f"  {'-'*8} {'-'*10} {'-'*8} {'-'*40} {'-'*16}")

    by_priority = {"URGENT": [], "IN-PLAY": [], "HIGH": [], "MEDIUM": [], "LOW": []}
    for m in matches:
        p = m["priority"]
        if p in by_priority:
            by_priority[p].append(m)

    for priority in ["URGENT", "IN-PLAY", "HIGH", "MEDIUM", "LOW"]:
        for m in by_priority[priority]:
            ko_str = m["kickoff"].strftime("%m-%d %H:%M") if m["kickoff"] else "?"
            icon = {"URGENT": "!!!", "IN-PLAY": ">>", "HIGH": "!!", "MEDIUM": "!", "LOW": " "}[priority]
            print(f"  {icon} {priority:<6} {m['minutes_to_ko']:>4.0f}min{'':<3} "
                  f"{m['interval_min']:>3}min{'':<3} "
                  f"{m['home_team'][:18]} vs {m['away_team'][:18]:<20} {ko_str}")


async def collect_odds_for_matches():
    """针对即将开球的比赛进行高频赔率采集"""
    setup_logger(log_level="INFO")

    db = get_db()
    if not db.test_connection():
        logger.error("数据库连接失败")
        sys.exit(1)

    # 1. 查看赛程
    with db.session() as session:
        upcoming = get_upcoming_matches(session, hours_ahead=24)

    print()
    print("=" * 72)
    print("  赛前高频赔率采集")
    print("=" * 72)

    if not upcoming:
        print("\n  今日无即将开球的比赛。")
        print("  提示: 先运行 py -3 main.py --source sofascore --date today 采集赛程")
        return

    # 统计
    urgent_count = sum(1 for m in upcoming if m["priority"] in ("URGENT", "IN-PLAY"))
    high_count = sum(1 for m in upcoming if m["priority"] == "HIGH")
    print(f"\n  即将开球: {len(upcoming)} 场比赛")
    print(f"  紧急 (<30m): {urgent_count} 场")
    print(f"  高优先级 (30m-2h): {high_count} 场")

    print_match_schedule(upcoming)

    # 2. 执行采集
    print()
    print("-" * 72)
    print("  开始采集...")
    print("-" * 72)

    browser = BrowserManager()
    await browser.start()

    try:
        config = {
            "api_base": "https://api.sofascore.com/api/v1",
            "base_url": "https://www.sofascore.com",
        }
        crawler = SofascoreCrawler(browser, config)
        today = datetime.now().strftime("%Y-%m-%d")
        await crawler.collect(today)

        # 打印本次采集统计
        with db.session() as session:
            # 最新赔率快照统计
            recent = session.execute(text("""
                SELECT COUNT(*) as cnt, MAX(snapshot_at) as latest
                FROM odds_history
                WHERE snapshot_at > NOW() - INTERVAL '1 hour'
            """)).fetchone()
            if recent and recent.cnt:
                print(f"\n  近1小时新增赔率快照: {recent.cnt} 条 (最新: {recent.latest})")

            # 按博彩商统计
            bookmakers = session.execute(text("""
                SELECT bookmaker, COUNT(*) as cnt
                FROM odds
                WHERE bookmaker != ''
                GROUP BY bookmaker
                ORDER BY cnt DESC
            """)).fetchall()
            if bookmakers:
                print(f"  博彩商分布: {', '.join(f'{b.bookmaker}({b.cnt})' for b in bookmakers)}")

    finally:
        await browser.stop()

    print()
    print("=" * 72)
    print("  下次采集建议:")
    print("=" * 72)
    if urgent_count > 0:
        print(f"  [紧急] {urgent_count} 场比赛临近开球，建议 5 分钟后再次采集")
    if high_count > 0:
        print(f"  [关注] {high_count} 场比赛 2h 内开球，建议 15-30 分钟后再次采集")
    if upcoming:
        next_interval = min(m["interval_min"] for m in upcoming)
        print(f"  最短采样间隔: {next_interval} 分钟")
    print()


async def loop_mode(interval_min: int, urgent: bool = False):
    """循环采集模式"""
    print(f"\n  循环采集模式: 每 {interval_min} 分钟一次 (紧急模式: {urgent})")
    print(f"  按 Ctrl+C 停止\n")

    iteration = 0
    while True:
        iteration += 1
        print(f"\n{'#'*50}")
        print(f"# 第 {iteration} 轮采集 — {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'#'*50}")

        try:
            await collect_odds_for_matches()
        except Exception as e:
            logger.error(f"采集异常: {e}")

        if urgent:
            # 紧急模式: 检查是否有临近比赛
            db = get_db()
            with db.session() as session:
                upcoming = get_upcoming_matches(session, hours_ahead=2)
            urgent_count = sum(1 for m in upcoming if m["priority"] in ("URGENT", "IN-PLAY"))
            actual_interval = 5 if urgent_count > 0 else interval_min
            print(f"\n  等待 {actual_interval} 分钟... ({urgent_count} 场紧急比赛)")
            time.sleep(actual_interval * 60)
        else:
            print(f"\n  等待 {interval_min} 分钟...")
            time.sleep(interval_min * 60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="赛前高频赔率采集")
    parser.add_argument("--loop", type=int, default=0, help="循环采集间隔(分钟), 0=单次")
    parser.add_argument("--urgent", action="store_true", help="紧急模式: 有临近比赛时缩至5分钟")
    args = parser.parse_args()

    if args.loop > 0:
        asyncio.run(loop_mode(args.loop, args.urgent))
    else:
        asyncio.run(collect_odds_for_matches())


if __name__ == "__main__":
    main()
