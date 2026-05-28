"""五大联赛近5年历史比赛数据采集 — football-data.org

用法: py -3 collect_historical.py

联赛: 英超/西甲/意甲/德甲/法甲
赛季: 2025, 2024, 2023, 2022, 2021
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

from crawler.core.logger import setup_logger, get_logger
from crawler.database.connection import get_db

logger = get_logger(__name__)

API_KEY = os.getenv("FOOTBALL_DATA_ORG_API_KEY", "")
BASE_URL = "https://api.football-data.org/v4"

# 五大联赛 football-data.org competition ID
LEAGUES = {
    "PL": {"name": "Premier League", "cn": "英超"},
    "PD": {"name": "La Liga", "cn": "西甲"},
    "SA": {"name": "Serie A", "cn": "意甲"},
    "BL1": {"name": "Bundesliga", "cn": "德甲"},
    "FL1": {"name": "Ligue 1", "cn": "法甲"},
}

# 近5个赛季（2025 = 2025-2026, 2024 = 2024-2025, ...）
SEASONS = ["2025", "2024", "2023", "2022", "2021"]

# API 限速: 免费层每分钟10次，保守每请求间隔 7 秒
RATE_LIMIT_DELAY = 7.0
PAGE_SIZE = 500

# 全局统计
global_stats = {
    "total_processed": 0,   # 已处理联赛-赛季组合数
    "total_fetched": 0,     # API 返回场次
    "total_collected": 0,   # 成功解析
    "total_no_score": 0,    # 缺失比分
    "total_failed": 0,      # 解析失败
    "total_skipped": 0,     # 已存在跳过
    "db_inserted": 0,
    "db_updated": 0,
    "db_skipped": 0,
    "db_errors": 0,
}

all_collected = []  # 汇总所有比赛用于合并导出


def season_label(year: str) -> str:
    """2023 → 2023-2024"""
    return f"{year}-{int(year)+1}"


def check_db_count(session, league_name: str, season: str) -> int:
    """查询数据库中该联赛-赛季已有比赛数"""
    from sqlalchemy import text
    result = session.execute(
        text("SELECT COUNT(*) FROM matches WHERE league_name = :league AND season = :season"),
        {"league": league_name, "season": season}
    )
    return result.scalar() or 0


def fetch_league_season(comp_id: str, league_info: dict, season_year: str) -> list[dict]:
    """从 football-data.org 拉取指定联赛-赛季的比赛"""
    league_name = league_info["name"]
    label = season_label(season_year)

    url = f"{BASE_URL}/competitions/{comp_id}/matches"
    headers = {"X-Auth-Token": API_KEY}
    params = {"season": season_year, "limit": PAGE_SIZE}

    logger.info(f"[{league_info['cn']}] {label} 请求: competitions/{comp_id}/matches?season={season_year}")

    all_matches = []

    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=30)
        logger.info(f"[{league_info['cn']}] {label} HTTP {resp.status_code}")

        if resp.status_code == 429:
            logger.warning(f"[{league_info['cn']}] {label} API 限速，等待 70 秒...")
            time.sleep(70)
            resp = httpx.get(url, headers=headers, params=params, timeout=30)
            logger.info(f"[{league_info['cn']}] {label} 重试 HTTP {resp.status_code}")

        if resp.status_code != 200:
            logger.error(f"[{league_info['cn']}] {label} 请求失败: HTTP {resp.status_code}")
            return all_matches

        data = resp.json()
        result_set = data.get("resultSet", {})
        total = result_set.get("count", 0)
        played = result_set.get("played", 0)
        logger.info(f"[{league_info['cn']}] {label} 共 {total} 场, 已完赛 {played}")

        matches_raw = data.get("matches", [])

        for m in matches_raw:
            parsed = _parse_match(m, league_name, label)
            if parsed:
                all_matches.append(parsed)
            else:
                global_stats["total_failed"] += 1

        global_stats["total_fetched"] += len(matches_raw)
        global_stats["total_collected"] += len(all_matches)

    except httpx.HTTPStatusError as e:
        logger.error(f"[{league_info['cn']}] {label} HTTP 错误: {e}")
    except Exception as e:
        logger.error(f"[{league_info['cn']}] {label} 请求异常: {e}")

    return all_matches


def _parse_match(m: dict, league_name: str, season: str) -> dict | None:
    """解析 football-data.org 比赛数据为 dict"""
    try:
        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
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

        # 统计缺失比分
        if not score or score in ("未开始", "?-?"):
            global_stats["total_no_score"] += 1

        return {
            "source": "football-data",
            "league": league_name,
            "home_team": home_name,
            "away_team": away_name,
            "kickoff_time": kickoff,
            "score": score,
            "half_time_score": half_time_score,
            "season": season,
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
        logger.warning(f"解析失败: {e}")
        return None


def export_combined():
    """导出合并文件"""
    from config.paths import EXPORTS_DIR
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    json_path = EXPORTS_DIR / "historical_5leagues_5seasons.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_collected, f, ensure_ascii=False, indent=2)
    logger.info(f"合并 JSON: {json_path} ({len(all_collected)} 条)")

    csv_path = EXPORTS_DIR / "historical_5leagues_5seasons.csv"
    try:
        import pandas as pd
        df = pd.DataFrame(all_collected)
        df = df.sort_values(["league", "season", "kickoff_time"])
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"合并 CSV: {csv_path} ({len(all_collected)} 条)")
    except ImportError:
        logger.warning("pandas 未安装，跳过合并 CSV")


def import_to_db(matches: list[dict], batch_desc: str = ""):
    """导入 PostgreSQL"""
    if not matches:
        return

    db = get_db()
    if not db.test_connection():
        logger.error("数据库连接失败")
        return

    db.create_all()

    from crawler.database.importer import MatchImporter
    importer = MatchImporter()
    result = importer.import_matches(matches)

    global_stats["db_inserted"] += result.get("inserted", 0)
    global_stats["db_updated"] += result.get("updated", 0)
    global_stats["db_skipped"] += result.get("skipped", 0)
    global_stats["db_errors"] += result.get("errors", 0)

    if batch_desc:
        logger.info(
            f"[{batch_desc}] 入库: +{result.get('inserted',0)} "
            f"~{result.get('updated',0)} -{result.get('skipped',0)} x{result.get('errors',0)}"
        )


def print_league_summary(league_cn: str, label: str, matches: list[dict],
                          skipped: bool, db_count: int):
    """打印单个联赛-赛季统计"""
    total = len(matches)
    finished = sum(1 for m in matches if m.get("status") == "finished")
    nos_score = sum(1 for m in matches if not m.get("score") or m["score"] in ("未开始", "?-?"))

    print(f"  {league_cn} {label}: ", end="")
    if skipped:
        print(f"跳过 (DB 已有 {db_count} 场)")
    else:
        parts = [f"共 {total} 场"]
        parts.append(f"完赛 {finished}")
        if nos_score:
            parts.append(f"缺比分 {nos_score}")
        print(", ".join(parts))


def print_final_report():
    """打印最终汇总报告"""
    print()
    print("=" * 60)
    print("  五大联赛近5年历史比赛数据采集 - 最终报告")
    print("=" * 60)
    print(f"  数据源:     football-data.org")
    print(f"  联赛:       英超 / 西甲 / 意甲 / 德甲 / 法甲")
    print(f"  赛季:       2021~2025 (共 5 季)")
    print(f"  联赛-赛季组合: {global_stats['total_processed']}")
    print(f"  API 返回总场次: {global_stats['total_fetched']}")
    print(f"  成功解析:   {global_stats['total_collected']} 场")
    print(f"  缺失比分:   {global_stats['total_no_score']} 场")
    print(f"  解析失败:   {global_stats['total_failed']} 场")
    print(f"  已存在跳过: {global_stats['total_skipped']} 组")
    print(f"  ──────────────────────────")
    print(f"  DB 新增:    {global_stats['db_inserted']} 场")
    print(f"  DB 更新:    {global_stats['db_updated']} 场")
    print(f"  DB 跳过:    {global_stats['db_skipped']} 场")
    print(f"  DB 错误:    {global_stats['db_errors']} 场")
    print(f"  ──────────────────────────")
    print(f"  导出文件:   {len(all_collected)} 场")
    print(f"    {EXPORTS_DIR / 'historical_5leagues_5seasons.json'}")
    print(f"    {EXPORTS_DIR / 'historical_5leagues_5seasons.csv'}")
    print("=" * 60)
    print()


def main():
    setup_logger(log_level="INFO")

    if not API_KEY:
        logger.error("未设置 FOOTBALL_DATA_ORG_API_KEY")
        sys.exit(1)

    db = get_db()
    if not db.test_connection():
        logger.error("数据库连接失败，请先启动 PostgreSQL")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  五大联赛近5年历史比赛数据采集")
    print(f"  英超 / 西甲 / 意甲 / 德甲 / 法甲 × 2021~2025")
    print(f"{'='*60}\n")

    total_combos = len(LEAGUES) * len(SEASONS)
    combo_idx = 0

    with db.session() as session:
        for comp_id, league_info in LEAGUES.items():
            league_name = league_info["name"]
            league_cn = league_info["cn"]

            print(f"\n{'─'*50}")
            print(f"  {league_cn} ({league_name})")
            print(f"{'─'*50}")

            for season_year in SEASONS:
                combo_idx += 1
                label = season_label(season_year)

                # 断点续采: 检查数据库中是否已有该联赛-赛季数据
                existing_count = check_db_count(session, league_name, label)
                combo_desc = f"{league_cn} {label}"

                if existing_count >= 100:
                    # 已有足够数据，跳过
                    logger.info(f"[{combo_desc}] DB 已有 {existing_count} 场，跳过")
                    global_stats["total_skipped"] += 1
                    print_league_summary(league_cn, label, [], skipped=True, db_count=existing_count)
                    continue

                if existing_count > 0:
                    logger.info(f"[{combo_desc}] DB 已有 {existing_count} 场，但不足100场，重新采集")

                # 限速等待
                if global_stats["total_processed"] > 0:
                    logger.debug(f"限速等待 {RATE_LIMIT_DELAY}s...")
                    time.sleep(RATE_LIMIT_DELAY)

                # 采集
                print(f" [{combo_idx}/{total_combos}] {combo_desc} ...", end=" ", flush=True)
                matches = fetch_league_season(comp_id, league_info, season_year)
                global_stats["total_processed"] += 1

                if matches:
                    all_collected.extend(matches)
                    print_league_summary(league_cn, label, matches, skipped=False, db_count=0)
                    # 每采集完一组就入库（避免内存中积压太多）
                    import_to_db(matches, batch_desc=combo_desc)
                else:
                    print("API 返回 0 场（可能该赛季数据不可用）")

    # 合并导出
    export_combined()

    # 最终报告
    print_final_report()


if __name__ == "__main__":
    main()
