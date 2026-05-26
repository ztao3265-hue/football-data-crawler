"""英超 2023-2024 历史赛季采集 - football-data.org 优先

用法: py -3 collect_epl_historical.py
"""

import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

from crawler.core.models import MatchData
from crawler.core.logger import setup_logger, get_logger
from crawler.database.connection import get_db

logger = get_logger(__name__)

API_KEY = os.getenv("FOOTBALL_DATA_ORG_API_KEY", "")
BASE_URL = "https://api.football-data.org/v4"
COMPETITION_ID = "PL"       # Premier League
SEASON_YEAR = "2023"        # 2023-2024 season
SEASON_LABEL = "2023-2024"

# 每页最多 500 条，英超 380 场一页就够了
PAGE_SIZE = 500

stats = {
    "fetched": 0,    # API 返回的场次
    "collected": 0,  # 成功解析
    "duplicates": 0, # 已在数据库中
    "no_score": 0,   # 缺失比分
    "failed": 0,     # 解析失败
    "db_inserted": 0,
    "db_updated": 0,
    "db_skipped": 0,
    "db_errors": 0,
}


def fetch_epl_season() -> list[dict]:
    """从 football-data.org 拉取英超整个赛季的比赛"""
    if not API_KEY:
        logger.error("未设置 FOOTBALL_DATA_ORG_API_KEY")
        return []

    url = f"{BASE_URL}/competitions/{COMPETITION_ID}/matches"
    headers = {"X-Auth-Token": API_KEY}
    params = {"season": SEASON_YEAR, "limit": PAGE_SIZE}

    logger.info(f"[EPL] 请求: {url}?season={SEASON_YEAR}&limit={PAGE_SIZE}")

    all_matches = []
    page = 0

    while url:
        try:
            resp = httpx.get(url, headers=headers, params=params if page == 0 else None, timeout=30)
            logger.info(f"[EPL] 第 {page+1} 页: HTTP {resp.status_code}")

            if resp.status_code == 429:
                logger.warning("[EPL] API 限速，等待 65 秒...")
                time.sleep(65)
                continue

            resp.raise_for_status()
            data = resp.json()

            result_set = data.get("resultSet", {})
            total = result_set.get("count", 0)
            played = result_set.get("played", 0)
            logger.info(f"[EPL] 赛季总计: {total} 场, 已完成: {played}")

            matches_raw = data.get("matches", [])
            stats["fetched"] += len(matches_raw)

            for m in matches_raw:
                parsed = _parse_epl_match(m)
                if parsed:
                    all_matches.append(parsed)
                    stats["collected"] += 1
                    if not parsed.get("score") or parsed["score"] in ("未开始", "?-?"):
                        stats["no_score"] += 1
                else:
                    stats["failed"] += 1

            # 检查是否有下一页（试用 pagination header）
            url = None  # 英超 380 场在 limit=500 下一页就够了
            page += 1

        except httpx.HTTPStatusError as e:
            logger.error(f"[EPL] HTTP 错误: {e}")
            break
        except Exception as e:
            logger.error(f"[EPL] 请求异常: {e}")
            time.sleep(5)

    return all_matches


def _parse_epl_match(m: dict) -> dict | None:
    """解析 football-data.org 比赛数据为 dict"""
    try:
        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
        competition = m.get("competition", {})
        score_info = m.get("score", {})

        home_name = home.get("name", "")
        away_name = away.get("name", "")
        if not home_name or not away_name:
            return None

        # 全场比分
        full_time = score_info.get("fullTime", {})
        h_score = full_time.get("home")
        a_score = full_time.get("away")
        if h_score is not None and a_score is not None:
            score = f"{h_score}-{a_score}"
        else:
            score = "未开始"

        # 半场比分
        half_time = score_info.get("halfTime", {})
        ht_home = half_time.get("home")
        ht_away = half_time.get("away")
        half_time_score = f"{ht_home}-{ht_away}" if ht_home is not None and ht_away is not None else ""

        # 开球时间
        utc_date = m.get("utcDate", "")
        kickoff = ""
        if utc_date:
            try:
                dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
                kickoff = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                kickoff = utc_date

        # 轮次
        matchday = m.get("matchday", "")
        stage = m.get("stage", "")
        round_str = f"{stage}, {matchday}" if stage and matchday else (stage or str(matchday) or "")

        # 状态
        status = m.get("status", "").lower()

        return {
            "source": "football-data",
            "league": competition.get("name", "Premier League"),
            "home_team": home_name,
            "away_team": away_name,
            "kickoff_time": kickoff,
            "score": score,
            "half_time_score": half_time_score,
            "season": SEASON_LABEL,
            "round": round_str,
            "status": status,
            "odds_home": "",
            "odds_draw": "",
            "odds_away": "",
            "asian_handicap": "",
            "over_under": "",
            "odds_bookmaker": "",
        }
    except Exception as e:
        logger.warning(f"[EPL] 解析失败: {e}")
        return None


def export_files(matches: list[dict]):
    """导出 JSON + CSV"""
    Path("exports").mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = "exports/epl_2023_2024_matches.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=False, indent=2)
    logger.info(f"[EPL] JSON 导出: {json_path} ({len(matches)} 条)")

    # CSV
    csv_path = "exports/epl_2023_2024_matches.csv"
    try:
        import pandas as pd
        df = pd.DataFrame(matches)
        df = df.sort_values("kickoff_time")
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"[EPL] CSV 导出: {csv_path} ({len(matches)} 条)")
    except ImportError:
        logger.warning("[EPL] pandas 未安装，跳过 CSV 导出")


def import_to_db(matches: list[dict]):
    """导入 PostgreSQL"""
    db = get_db()
    if not db.test_connection():
        logger.error("[EPL] 数据库连接失败")
        return

    db.create_all()

    from crawler.database.importer import MatchImporter
    importer = MatchImporter()
    result = importer.import_matches(matches)

    stats["db_inserted"] = result.get("inserted", 0)
    stats["db_updated"] = result.get("updated", 0)
    stats["db_skipped"] = result.get("skipped", 0)
    stats["db_errors"] = result.get("errors", 0)


def print_summary():
    """打印统计摘要"""
    finished = stats["collected"] - stats["no_score"]

    print()
    print("=" * 55)
    print(f"  英超 {SEASON_LABEL} 赛季数据采集报告")
    print("=" * 55)
    print(f"  联赛:       Premier League")
    print(f"  赛季:       {SEASON_LABEL}")
    print(f"  数据源:     football-data.org")
    print(f"  API 返回:   {stats['fetched']} 场")
    print(f"  成功采集:   {stats['collected']} 场")
    print(f"  已完成比赛: {finished} 场")
    print(f"  缺失比分:   {stats['no_score']} 场")
    print(f"  解析失败:   {stats['failed']} 场")
    print(f"  DB 新增:    {stats['db_inserted']} 场")
    print(f"  DB 更新:    {stats['db_updated']} 场")
    print(f"  DB 跳过:    {stats['db_skipped']} 场")
    print(f"  DB 错误:    {stats['db_errors']} 场")
    print("=" * 55)

    # 轮次分布
    rounds = {}
    for m in _all_matches if "_all_matches" in dir() else []:
        r = m.get("round", "?")
        rounds[r] = rounds.get(r, 0) + 1
    if rounds:
        print(f"  轮次分布: {len(rounds)} 轮")
    print()


def main():
    setup_logger(log_level="INFO")

    print(f"\n采集英超 {SEASON_LABEL} 赛季数据...\n")

    matches = fetch_epl_season()

    if not matches:
        logger.error("未采集到任何数据")
        sys.exit(1)

    export_files(matches)
    import_to_db(matches)

    # 让 print_summary 能访问到 matches
    global _all_matches
    _all_matches = matches
    print_summary()


if __name__ == "__main__":
    main()
