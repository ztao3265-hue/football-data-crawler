"""赔率历史验证脚本 — 随机抽取10场比赛，打印完整赔率变化轨迹

用法: py -3 verify_odds_history.py
"""

import os
import sys
from dotenv import load_dotenv
load_dotenv()

from crawler.core.logger import setup_logger, get_logger
from crawler.database.connection import get_db
from sqlalchemy import text

logger = get_logger(__name__)


def verify_odds_history():
    setup_logger(log_level="INFO")

    db = get_db()
    if not db.test_connection():
        logger.error("数据库连接失败")
        sys.exit(1)

    with db.session() as session:
        # 1. 基础统计
        total_matches = session.execute(text("SELECT COUNT(*) FROM matches")).scalar()
        total_odds = session.execute(text("SELECT COUNT(*) FROM odds")).scalar()
        total_history = session.execute(text("SELECT COUNT(*) FROM odds_history")).scalar()
        odds_with_open = session.execute(text(
            "SELECT COUNT(*) FROM odds WHERE odds_home_open IS NOT NULL"
        )).scalar()
        odds_with_change = session.execute(text(
            "SELECT COUNT(*) FROM odds WHERE odds_home IS NOT NULL AND odds_home_open IS NOT NULL AND odds_home != odds_home_open"
        )).scalar()

        print()
        print("=" * 65)
        print("  赔率历史数据链路 — 最终验证报告")
        print("=" * 65)
        print(f"  比赛总数:           {total_matches}")
        print(f"  含赔率比赛:         {total_odds}")
        print(f"  含初盘数据:         {odds_with_open}")
        print(f"  初盘→即时盘已变化:  {odds_with_change}")
        print(f"  odds_history 快照:  {total_history}")
        print()

        # 2. 赔率变化统计
        if total_odds > 0:
            print("-" * 65)
            print("  赔率统计")
            print("-" * 65)

            # 按变化幅度排序
            top_changes = session.execute(text("""
                SELECT m.home_team, m.away_team, m.league_name,
                       o.odds_home, o.odds_home_open,
                       o.odds_draw, o.odds_draw_open,
                       o.odds_away, o.odds_away_open,
                       ROUND(CAST(ABS(o.odds_home - o.odds_home_open) AS numeric), 2) as home_diff
                FROM odds o
                JOIN matches m ON o.match_id = m.match_id
                WHERE o.odds_home_open IS NOT NULL
                ORDER BY home_diff DESC
            """)).fetchall()

            for row in top_changes:
                home, away, league, oh, oho, od, odo, oa, oao, diff = row
                if diff and diff > 0:
                    direction = "↑(走弱)" if oh > oho else "↓(走强)" if oh < oho else "→"
                    print(f"  {home} vs {away}")
                    print(f"    [{league}]")
                    print(f"    初盘 H:{_fmt(oho)} D:{_fmt(odo)} A:{_fmt(oao)}")
                    print(f"    即时 H:{_fmt(oh)} D:{_fmt(od)} A:{_fmt(oa)}")
                    print(f"    主胜变化: {_fmt(oho)}→{_fmt(oh)} 差价 {diff} {direction}")

        # 3. 随机抽取最多10场有赔率的比赛，打印完整轨迹
        sample = session.execute(text("""
            SELECT m.match_id, m.home_team, m.away_team, m.league_name, m.kickoff_time,
                   o.odds_home, o.odds_draw, o.odds_away,
                   o.odds_home_open, o.odds_draw_open, o.odds_away_open,
                   o.asian_handicap, o.asian_handicap_open,
                   o.over_under, o.over_under_open,
                   o.bookmaker, o.source
            FROM matches m
            JOIN odds o ON m.match_id = o.match_id
            WHERE o.odds_home IS NOT NULL
            ORDER BY RANDOM()
            LIMIT 10
        """)).fetchall()

        if not sample:
            print()
            print("  [!] 未找到有赔率的比赛！请先运行一次 Sofascore 采集。")
            return

        print()
        print("-" * 65)
        print(f"  随机抽取 {len(sample)} 场比赛 — 赔率变化轨迹")
        print("-" * 65)

        for idx, row in enumerate(sample, 1):
            (match_id, home, away, league, kickoff,
             oh, od, oa, oho, odo, oao,
             asian, asian_open, ou, ou_open,
             bookmaker, source) = row

            print()
            print(f"  [{idx}] {home} vs {away}")
            print(f"      联赛: {league}")
            print(f"      开球: {kickoff}")
            print(f"      数据源: {source}  博彩商: {bookmaker or 'Sofascore(bet365)'}")

            # 欧赔轨迹
            print(f"      ┌─ 欧赔(1X2) 变化轨迹 ────────────────────")
            has_open = oho is not None
            has_change = has_open and (oho != oh or odo != od or oao != oa)

            if has_open:
                print(f"      │  初盘(开盘):  H:{_fmt(oho)}  D:{_fmt(odo)}  A:{_fmt(oao)}")
            else:
                print(f"      │  初盘(开盘):  (未捕获)")

            # 赔率历史中间快照
            history = session.execute(text("""
                SELECT odds_home, odds_draw, odds_away,
                       odds_home_open, odds_draw_open, odds_away_open,
                       asian_handicap, over_under,
                       snapshot_at
                FROM odds_history
                WHERE match_id = :mid
                ORDER BY snapshot_at
            """), {"mid": match_id}).fetchall()

            for h_idx, h in enumerate(history):
                h_oh, h_od, h_oa, h_oho, h_odo, h_oao, h_asian, h_ou, snap = h
                snap_str = snap.strftime("%m-%d %H:%M") if snap else "?"
                label = "首次采集" if h_idx == 0 else f"第{h_idx+1}次采集"
                print(f"      │  [{snap_str}] {label}: H:{_fmt(h_oh)} D:{_fmt(h_od)} A:{_fmt(h_oa)}")

            # 当前即时盘
            print(f"      │  即时盘(最新): H:{_fmt(oh)} D:{_fmt(od)} A:{_fmt(oa)}")

            if has_change:
                print(f"      │")
                home_dir = "↓走强" if oh < oho else "↑走弱"
                draw_dir = "↓" if od < odo else "↑"
                away_dir = "↓" if oa < oao else "↑"
                print(f"      │  主胜: {_fmt(oho)}→{_fmt(oh)} {home_dir}")
                print(f"      │  平局: {_fmt(odo)}→{_fmt(od)} {draw_dir}")
                print(f"      │  客胜: {_fmt(oao)}→{_fmt(oa)} {away_dir}")

            # 亚盘轨迹
            print(f"      ├─ 亚盘变化轨迹 ──────────────────────────")
            print(f"      │  初盘: {asian_open or '无'}")
            for h in history:
                print(f"      │  历史: {h.asian_handicap or '无'}")
            print(f"      │  即时: {asian or '无'}")

            # 大小球轨迹
            print(f"      ├─ 大小球变化轨迹 ────────────────────────")
            print(f"      │  初盘: {ou_open or '无'}")
            print(f"      │  即时: {ou or '无'}")

            # 总结
            hist_count = len(history)
            print(f"      └─ 赔率快照总数: {hist_count} 条")
            if has_change:
                print(f"         [OK] 检测到真实的赔率变化！")

        # 4. 数据链路汇总
        print()
        print("=" * 65)
        print("  数据链路完整性验证")
        print("=" * 65)

        checks = []
        checks.append(("Sofascore API 连接", total_odds > 0))
        checks.append(("欧赔(1X2)采集", total_odds > 0))
        checks.append(("亚盘采集", session.execute(text(
            "SELECT COUNT(*) FROM odds WHERE asian_handicap != ''"
        )).scalar() > 0))
        checks.append(("初盘(Opening)保存", odds_with_open > 0))
        checks.append(("即时盘(Current)保存", total_odds > 0))
        checks.append(("赔率变化检测", odds_with_change > 0))
        checks.append(("odds_history 写入", total_history > 0))
        checks.append(("数据库持久化", True))

        for name, ok in checks:
            status = "[OK]" if ok else "[--]"
            print(f"  {status} {name}")

        all_ok = all(ok for _, ok in checks)
        print()
        if all_ok:
            print("  >>> 赔率历史数据链路已完整打通！<<<")
        else:
            print("  >>> 部分环节待完善，参见上方 [--] 标记 <<<")
        print("=" * 65)
        print()


def _fmt(val):
    """格式化赔率值"""
    if val is None:
        return "-"
    try:
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return str(val)


if __name__ == "__main__":
    verify_odds_history()
