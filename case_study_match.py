"""完整盘口变化案例 — 输出一场比赛所有赔率维度的完整时间线

用法: py -3 case_study_match.py
"""

import sys
from dotenv import load_dotenv
load_dotenv()

from crawler.database.connection import get_db
from sqlalchemy import text


def case_study(match_id: str = None):
    db = get_db()
    if not db.test_connection():
        print("数据库连接失败")
        sys.exit(1)

    with db.session() as session:
        # 1. 选取案例：优先选欧赔变化最大的比赛
        if not match_id:
            row = session.execute(text("""
                SELECT m.match_id
                FROM odds o
                JOIN matches m ON o.match_id = m.match_id
                WHERE o.odds_home_open IS NOT NULL
                AND o.odds_home != o.odds_home_open
                ORDER BY ABS(o.odds_home - o.odds_home_open) DESC
                LIMIT 1
            """)).fetchone()
            if not row:
                print("未找到合适的案例比赛")
                return
            match_id = row[0]

        # 2. 获取比赛基本信息
        match = session.execute(text("""
            SELECT match_id, home_team, away_team, league_name,
                   kickoff_time, season, round, status, source, score_display
            FROM matches WHERE match_id = :mid
        """), {"mid": match_id}).fetchone()

        if not match:
            print(f"未找到比赛: {match_id}")
            return

        (mid, home, away, league, kickoff, season, round_, status, source, score) = match

        # 3. 获取所有博彩商的赔率数据
        odds_records = session.execute(text("""
            SELECT bookmaker, odds_home, odds_draw, odds_away,
                   odds_home_open, odds_draw_open, odds_away_open,
                   asian_handicap, asian_handicap_open,
                   over_under, over_under_open,
                   collected_at, created_at
            FROM odds
            WHERE match_id = :mid
            ORDER BY bookmaker
        """), {"mid": match_id}).fetchall()

        # 4. 获取赔率历史
        history = session.execute(text("""
            SELECT bookmaker, odds_home, odds_draw, odds_away,
                   odds_home_open, odds_draw_open, odds_away_open,
                   asian_handicap, asian_handicap_open,
                   over_under, over_under_open,
                   snapshot_at
            FROM odds_history
            WHERE match_id = :mid
            ORDER BY snapshot_at
        """), {"mid": match_id}).fetchall()

        # 5. 输出完整案例
        print()
        print("=" * 72)
        print("  完整盘口变化案例")
        print("=" * 72)
        print(f"  比赛: {home} vs {away}")
        print(f"  联赛: {league}")
        print(f"  开球: {kickoff}")
        print(f"  比分: {score or '未赛'}")
        print(f"  赛季: {season}  轮次: {round_}")
        print(f"  match_id: {mid}")
        print(f"  数据源: {source}")
        print()

        for odds in odds_records:
            (bm, oh, od, oa, oho, odo, oao,
             asian, asian_open, ou, ou_open, coll_at, created) = odds

            print("─" * 72)
            print(f"  [博彩商: {bm or '(未标注)'}]")
            print("─" * 72)

            # ── 欧赔 (1X2) ──
            print()
            print(f"  ◆ 欧赔 (1X2) 变化轨迹")
            print(f"  {'─' * 50}")

            has_open = oho is not None
            has_change = has_open and (oho != oh or odo != od or oao != oa)

            if has_open:
                print(f"  │ 初盘 (开盘):  H:{_f(oho):>7}  D:{_f(odo):>7}  A:{_f(oao):>7}")
                if not has_change:
                    print(f"  │   (初盘后未变化)")
            else:
                print(f"  │ 初盘 (开盘):  (未捕获到初盘)")

            # 赔率历史快照
            bm_history = [h for h in history if h.bookmaker == (bm or "")]
            if bm_history:
                for hi, h in enumerate(bm_history):
                    snap_str = h.snapshot_at.strftime("%m-%d %H:%M")
                    label = "首次采集" if hi == 0 else f"第{hi+1}次变盘"
                    print(f"  │ [{snap_str}] {label}:")
                    print(f"  │   H:{_f(h.odds_home):>7}  D:{_f(h.odds_draw):>7}  A:{_f(h.odds_away):>7}")

            # 即时盘
            print(f"  │ 即时盘 (最新): H:{_f(oh):>7}  D:{_f(od):>7}  A:{_f(oa):>7}")

            if has_change:
                print(f"  │")
                print(f"  │ 变化分析:")
                h_dir = "↓走强" if oh < oho else "↑走弱"
                d_dir = "↓" if od < odo else "↑"
                a_dir = "↓" if oa < oao else "↑"
                h_diff = abs(oh - oho) if oh and oho else 0
                d_diff = abs(od - odo) if od and odo else 0
                a_diff = abs(oa - oao) if oa and oao else 0
                print(f"  │   主胜: {_f(oho)} → {_f(oh)} ({h_dir}, 价差 {h_diff:.2f})")
                print(f"  │   平局: {_f(odo)} → {_f(od)} ({d_dir}, 价差 {d_diff:.2f})")
                print(f"  │   客胜: {_f(oao)} → {_f(oa)} ({a_dir}, 价差 {a_diff:.2f})")

                # 市场解读
                print(f"  │")
                print(f"  │ 市场解读:")
                if has_open:
                    # 判断资金流向
                    if oh < oho:
                        h_flow = "资金流入主胜"
                    elif oh > oho:
                        h_flow = "资金流出主胜"
                    else:
                        h_flow = "主胜赔率稳定"
                    if oa > oao:
                        a_flow = "客胜不被看好"
                    elif oa < oao:
                        a_flow = "客胜获资金关注"
                    else:
                        a_flow = "客胜赔率稳定"
                    print(f"  │   {h_flow}, {a_flow}")

            # 返还率
            if oh and od and oa:
                overround = (1/oh + 1/od + 1/oa) * 100
                print(f"  │")
                print(f"  │ 返还率: {overround:.1f}% (博彩公司抽水 {overround-100:.1f}%)")

            # ── 亚盘 ──
            print()
            print(f"  ◆ 亚盘 (Asian Handicap) 变化轨迹")
            print(f"  {'─' * 50}")

            if asian:
                print(f"  │ 初盘: {asian_open or '无'}")
                for hi, h in enumerate(bm_history):
                    if h.asian_handicap:
                        snap_str = h.snapshot_at.strftime("%m-%d %H:%M")
                        print(f"  │ [{snap_str}]: {h.asian_handicap}")
                print(f"  │ 即时: {asian}")

                # 亚盘变化分析
                if asian_open and asian != asian_open:
                    print(f"  │")
                    print(f"  │ 盘口赔率已变化！")
                    # 简单的文本对比
                    curr_parts = [p.strip() for p in asian.split(",")]
                    open_parts = [p.strip() for p in asian_open.split(",")]
                    for i, (c, o) in enumerate(zip(curr_parts, open_parts)):
                        if c != o:
                            print(f"  │   方向{i+1}: {o} → {c}")
            else:
                print(f"  │ (无亚盘数据)")

            # ── 大小球 ──
            print()
            print(f"  ◆ 大小球 (Over/Under)")
            print(f"  {'─' * 50}")
            if ou:
                print(f"  │ 初盘: {ou_open or '无'}")
                for hi, h in enumerate(bm_history):
                    if h.over_under:
                        snap_str = h.snapshot_at.strftime("%m-%d %H:%M")
                        print(f"  │ [{snap_str}]: {h.over_under}")
                print(f"  │ 即时: {ou}")
            else:
                print(f"  │ [无] Sofascore featured 端点不提供 overUnder 数据")
                print(f"  │ → 需通过 FotMob matchDetails 或 /odds/all 端点获取")

        # 汇总
        print()
        print("=" * 72)
        print("  数据结构验证")
        print("=" * 72)
        print(f"  match_id: {mid}")
        print(f"  bookmaker(s): {', '.join(set(o.bookmaker for o in odds_records))}")
        print(f"  赔率快照数: {len(history)}")
        print(f"  标准结构: match_id + bookmaker + timestamp")
        print(f"  欧赔(1X2): OK")
        print(f"  亚盘: {'OK' if any(o.asian_handicap for o in odds_records) else '无数据'}")
        print(f"  大小球: {'OK' if any(o.over_under for o in odds_records) else '待研究(FotMob/雷速体育)'}")
        print("=" * 72)
        print()


def _f(val):
    """格式化赔率值"""
    if val is None:
        return "-"
    try:
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return str(val)


if __name__ == "__main__":
    case_study()
