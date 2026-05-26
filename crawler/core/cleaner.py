"""数据清洗 - 去重合并多源数据"""

import json
from pathlib import Path
from datetime import datetime
from typing import List

import pandas as pd

from crawler.core.logger import get_logger

logger = get_logger(__name__)

# 球队名标准化映射（常用别名 → 标准名）
TEAM_ALIASES = {
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "manchester utd": "Manchester United",
    "fc barcelona": "Barcelona",
    "real madrid cf": "Real Madrid",
    "bayern munich": "Bayern Munich",
    "fc bayern": "Bayern Munich",
    "juventus fc": "Juventus",
    "paris saint-germain": "Paris Saint-Germain",
}

# 联赛名标准化映射
LEAGUE_ALIASES = {
    "premier league": "Premier League",
    "la liga": "La Liga",
    "laLiga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie a": "Serie A",
    "ligue 1": "Ligue 1",
    "uefa champions league": "UEFA Champions League",
}


def normalize_name(name: str, mapping: dict) -> str:
    """标准化名称"""
    key = name.strip().lower()
    return mapping.get(key, name.strip())


def make_match_key(match: dict) -> str:
    """生成比赛去重 key（基于球队名 + 日期）"""
    home = match.get("home_team", "").strip().lower()
    away = match.get("away_team", "").strip().lower()
    kickoff = match.get("kickoff_time", "")[:10]  # 只取日期部分
    return f"{home}|{away}|{kickoff}"


def merge_odds(existing: dict, incoming: dict) -> dict:
    """合并赔率数据：优先非空值"""
    odds_fields = ["odds_home", "odds_draw", "odds_away", "asian_handicap", "over_under", "odds_bookmaker"]
    for field in odds_fields:
        if not existing.get(field) and incoming.get(field):
            existing[field] = incoming[field]
    return existing


def clean_matches(matches: List[dict]) -> List[dict]:
    """清洗比赛数据：去重 + 合并多源 + 标准化"""
    logger.info(f"开始清洗 {len(matches)} 条原始数据")

    # 按 source 优先级排序（football-data 最权威优先，其次 sofascore，最后 fotmob）
    source_priority = {"football-data": 0, "sofascore": 1, "fotmob": 2}

    matches = sorted(matches, key=lambda m: source_priority.get(m.get("source", ""), 99))

    seen: dict[str, dict] = {}
    unique_count = 0

    for match in matches:
        # 跳过无效数据
        if not match.get("home_team") or not match.get("away_team"):
            continue

        key = make_match_key(match)

        if key in seen:
            # 合并赔率数据
            merge_odds(seen[key], match)
        else:
            # 标准化球队名和联赛名
            match["home_team"] = normalize_name(match["home_team"], TEAM_ALIASES)
            match["away_team"] = normalize_name(match["away_team"], TEAM_ALIASES)
            match["league"] = normalize_name(match.get("league", ""), LEAGUE_ALIASES)
            seen[key] = match
            unique_count += 1

    result = list(seen.values())
    logger.info(f"清洗完成: {len(matches)} → {unique_count} 条 (去重 {len(matches) - unique_count})")

    return result


def export_clean(matches: List[dict], output_dir: str = "exports", date_str: str = None):
    """导出清洗后的数据"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    cleaned = clean_matches(matches)

    # JSON
    json_path = output_dir / "clean_matches.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    logger.info(f"Clean JSON: {json_path} ({len(cleaned)} 条)")

    # CSV
    csv_path = output_dir / "clean_matches.csv"
    if cleaned:
        df = pd.DataFrame(cleaned)
        # 按联赛分组排序
        df = df.sort_values(["league", "kickoff_time"])
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"Clean CSV: {csv_path} ({len(cleaned)} 条, {df['league'].nunique()} 个联赛)")
    else:
        pd.DataFrame().to_csv(csv_path, index=False)
        logger.warning("无数据可导出")

    # 按日期归档
    date_json = output_dir / f"clean_matches_{date_str}.json"
    date_csv = output_dir / f"clean_matches_{date_str}.csv"
    with open(date_json, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    if cleaned:
        pd.DataFrame(cleaned).to_csv(date_csv, index=False, encoding="utf-8-sig")

    return cleaned
