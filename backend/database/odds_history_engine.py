#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
赔率历史时间序列引擎 (Odds History Time Series Engine)

功能:
1. SQLite 数据库表创建与自动迁移
2. 盘口时间序列存储 (初盘→即时盘→临场盘)
3. Line Movement 特征提取
4. Steam Move 检测
5. Sharp Money 行为分析
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import sqlite3
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from config.paths import DB_FOOTBALL_HISTORY, PROJECT_ROOT

DB_PATH = DB_FOOTBALL_HISTORY
REPORTS_DIR = PROJECT_ROOT / "reports" / "odds_history"

# ── 盘口解析工具 ─────────────────────────────────────────────────────

ASIAN_HANDICAP_MAP = {
    "平手": 0.0, "平手/半球": 0.25, "半球": 0.5, "半球/一球": 0.75,
    "一球": 1.0, "一球/球半": 1.25, "球半": 1.5, "球半/两球": 1.75,
    "两球": 2.0, "两球/两球半": 2.25, "两球半": 2.5, "两球半/三球": 2.75,
    "三球": 3.0, "三球/三球半": 3.25, "三球半": 3.5, "三球半/四球": 3.75,
    "四球": 4.0,
}


def parse_asian_handicap(text: str) -> Optional[float]:
    """解析亚盘盘口字符串为数值。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip().replace(" ", "")
    if not text:
        return None
    sign = 1
    if text.startswith("受"):
        sign = -1
        text = text[1:]
    val = ASIAN_HANDICAP_MAP.get(text)
    return sign * val if val is not None else None


def parse_ou_handicap(text: str) -> Optional[float]:
    """解析大小球盘口字符串为数值。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip().replace(" ", "").replace("球", "")
    if not text:
        return None
    if "/" in text:
        parts = text.split("/")
        try:
            return (float(parts[0]) + float(parts[1])) / 2
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


# ── 数据库引擎 ───────────────────────────────────────────────────────

class OddsHistoryEngine:
    """赔率历史时间序列引擎。"""

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.conn = None
        self.report = {}

    def connect(self):
        """连接数据库。"""
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        print(f"[DB] 连接: {self.db_path}")

    def close(self):
        """关闭连接。"""
        if self.conn:
            self.conn.close()

    def migrate(self):
        """执行数据库迁移 — 创建/更新 odds_history 表。"""
        print("\n[Migration] 检查数据库结构...")

        cursor = self.conn.cursor()

        # 检查现有表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cursor.fetchall()}
        print(f"  现有表: {existing_tables}")

        # ── 1. odds_history 表 (核心时间序列表) ──
        if "odds_history" not in existing_tables:
            print("  [创建] odds_history 表...")
            cursor.execute("""
                CREATE TABLE odds_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    odds_type TEXT NOT NULL,
                    snapshot_at DATETIME NOT NULL,
                    handicap_line REAL,
                    high_water REAL,
                    low_water REAL,
                    odds_home REAL,
                    odds_draw REAL,
                    odds_away REAL,
                    over_water REAL,
                    under_water REAL,
                    ou_line REAL,
                    collected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT DEFAULT ''
                )
            """)
            cursor.execute("CREATE INDEX ix_odds_history_match ON odds_history(match_id)")
            cursor.execute("CREATE INDEX ix_odds_history_snapshot ON odds_history(snapshot_at)")
            cursor.execute("CREATE INDEX ix_odds_history_match_book ON odds_history(match_id, bookmaker)")
            cursor.execute("CREATE INDEX ix_odds_history_match_snapshot ON odds_history(match_id, snapshot_at)")
            self.conn.commit()
            print("    ✓ odds_history 表已创建")
        else:
            # 检查现有列
            cursor.execute("PRAGMA table_info(odds_history)")
            existing_cols = {row[1] for row in cursor.fetchall()}
            required_cols = {
                "match_id": "TEXT", "bookmaker": "TEXT", "market_type": "TEXT",
                "odds_type": "TEXT", "snapshot_at": "DATETIME",
                "handicap_line": "REAL", "high_water": "REAL", "low_water": "REAL",
                "odds_home": "REAL", "odds_draw": "REAL", "odds_away": "REAL",
                "over_water": "REAL", "under_water": "REAL", "ou_line": "REAL",
                "collected_at": "DATETIME", "notes": "TEXT"
            }
            for col, col_type in required_cols.items():
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE odds_history ADD COLUMN {col} {col_type}")
                    print(f"    + 添加列: {col}")
            self.conn.commit()
            print("    ✓ odds_history 表结构已更新")

        # ── 2. line_movements 表 (盘口变化特征) ──
        if "line_movements" not in existing_tables:
            print("  [创建] line_movements 表...")
            cursor.execute("""
                CREATE TABLE line_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    open_line REAL,
                    close_line REAL,
                    line_change REAL,
                    line_change_pct REAL,
                    open_high_water REAL,
                    close_high_water REAL,
                    hw_change REAL,
                    open_low_water REAL,
                    close_low_water REAL,
                    lw_change REAL,
                    movement_type TEXT,
                    movement_magnitude TEXT,
                    movement_direction INTEGER,
                    n_snapshots INTEGER DEFAULT 0,
                    volatility REAL DEFAULT 0,
                    reversal_count INTEGER DEFAULT 0,
                    steam_move_flag INTEGER DEFAULT 0,
                    steam_move_score REAL DEFAULT 0,
                    sharp_money_signal INTEGER DEFAULT 0,
                    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX ix_line_movements_match ON line_movements(match_id)")
            cursor.execute("CREATE INDEX ix_line_movements_steam ON line_movements(steam_move_flag)")
            self.conn.commit()
            print("    ✓ line_movements 表已创建")

        # ── 3. steam_moves 表 (蒸汽移动检测) ──
        if "steam_moves" not in existing_tables:
            print("  [创建] steam_moves 表...")
            cursor.execute("""
                CREATE TABLE steam_moves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    detected_at DATETIME NOT NULL,
                    start_snapshot DATETIME,
                    end_snapshot DATETIME,
                    line_before REAL,
                    line_after REAL,
                    line_delta REAL,
                    water_before REAL,
                    water_after REAL,
                    water_delta REAL,
                    duration_seconds INTEGER,
                    steam_type TEXT,
                    steam_score REAL,
                    confidence TEXT,
                    is_reverse_line INTEGER DEFAULT 0,
                    sharp_bookmaker TEXT,
                    notes TEXT
                )
            """)
            cursor.execute("CREATE INDEX ix_steam_moves_match ON steam_moves(match_id)")
            cursor.execute("CREATE INDEX ix_steam_moves_detected ON steam_moves(detected_at)")
            self.conn.commit()
            print("    ✓ steam_moves 表已创建")

        # ── 4. sharp_money_signals 表 (职业资金信号) ──
        if "sharp_money_signals" not in existing_tables:
            print("  [创建] sharp_money_signals 表...")
            cursor.execute("""
                CREATE TABLE sharp_money_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    signal_strength REAL,
                    bookmakers_involved TEXT,
                    line_direction INTEGER,
                    water_direction INTEGER,
                    detection_time DATETIME,
                    confidence TEXT,
                    reasoning TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX ix_sharp_money_match ON sharp_money_signals(match_id)")
            self.conn.commit()
            print("    ✓ sharp_money_signals 表已创建")

        # ── 5. 数据库元信息 ──
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 记录迁移版本
        cursor.execute("""
            INSERT OR REPLACE INTO schema_migrations (version, applied_at)
            VALUES ('2026_05_28_odds_history', ?)
        """, (datetime.now().isoformat(),))
        self.conn.commit()

        print("\n[Migration] 完成 ✓")

    # ═══════════════════════════════════════════════════════════════
    # 数据导入
    # ═══════════════════════════════════════════════════════════════

    def import_from_odds_asian(self):
        """从 odds_asian 表导入数据到 odds_history。"""
        print("\n[Import] 从 odds_asian 导入历史数据...")

        cursor = self.conn.cursor()

        # 检查 odds_asian 表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='odds_asian'")
        if not cursor.fetchone():
            print("  [警告] odds_asian 表不存在")
            return

        # 获取已导入的记录数
        cursor.execute("SELECT COUNT(*) FROM odds_history")
        existing = cursor.fetchone()[0]

        # 导入 opening 数据
        cursor.execute("""
            SELECT match_id, bookmaker, 'asian' as market_type, 'opening' as odds_type,
                   handicap, high_water, low_water
            FROM odds_asian
            WHERE odds_type = 'opening'
              AND handicap IS NOT NULL
              AND high_water IS NOT NULL
        """)
        opening_rows = cursor.fetchall()

        # 导入 closing 数据
        cursor.execute("""
            SELECT match_id, bookmaker, 'asian' as market_type, 'closing' as odds_type,
                   handicap, high_water, low_water
            FROM odds_asian
            WHERE odds_type = 'closing'
              AND handicap IS NOT NULL
              AND high_water IS NOT NULL
        """)
        closing_rows = cursor.fetchall()

        # 获取比赛开球时间作为时间基准
        cursor.execute("""
            SELECT match_id, kickoff_time FROM matches WHERE kickoff_time IS NOT NULL
        """)
        match_times = {row[0]: row[1] for row in cursor.fetchall()}

        inserted = 0
        for row in opening_rows + closing_rows:
            match_id, bookmaker, market_type, odds_type, handicap, hw, lw = row

            # 解析盘口线
            line = parse_asian_handicap(handicap)
            if line is None:
                continue

            # 计算快照时间
            kickoff = match_times.get(match_id)
            if kickoff:
                try:
                    if isinstance(kickoff, str):
                        kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                    else:
                        kickoff_dt = kickoff
                    # opening = 开球前24小时, closing = 开球前5分钟
                    if odds_type == "opening":
                        snapshot = kickoff_dt - timedelta(hours=24)
                    else:
                        snapshot = kickoff_dt - timedelta(minutes=5)
                except:
                    snapshot = datetime.now()
            else:
                snapshot = datetime.now()

            # 插入数据
            cursor.execute("""
                INSERT OR IGNORE INTO odds_history
                (match_id, bookmaker, market_type, odds_type, snapshot_at,
                 handicap_line, high_water, low_water)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (match_id, bookmaker, market_type, odds_type, snapshot,
                  line, hw, lw))
            inserted += cursor.rowcount

        self.conn.commit()
        print(f"  导入 {inserted} 条新记录 (已有 {existing} 条)")

        # 统计
        cursor.execute("SELECT COUNT(*) FROM odds_history")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT match_id) FROM odds_history")
        matches = cursor.fetchone()[0]
        print(f"  总计: {total:,} 条记录, 覆盖 {matches:,} 场比赛")

    # ═══════════════════════════════════════════════════════════════
    # Line Movement 特征计算
    # ═══════════════════════════════════════════════════════════════

    def compute_line_movements(self):
        """计算所有比赛的盘口变化特征。"""
        print("\n[Line Movement] 计算盘口变化特征...")

        cursor = self.conn.cursor()

        # 获取所有有开盘和收盘数据的比赛
        cursor.execute("""
            SELECT DISTINCT match_id, bookmaker
            FROM odds_history
            WHERE market_type = 'asian'
        """)
        match_bookmaker_pairs = cursor.fetchall()

        print(f"  待处理: {len(match_bookmaker_pairs)} 个 (比赛, 公司) 对")

        computed = 0
        for match_id, bookmaker in match_bookmaker_pairs:
            if self._compute_single_line_movement(match_id, bookmaker):
                computed += 1

        self.conn.commit()
        print(f"  完成: {computed} 条盘口变化记录")

    def _compute_single_line_movement(self, match_id: str, bookmaker: str) -> bool:
        """计算单场比赛的盘口变化。"""
        cursor = self.conn.cursor()

        # 获取该比赛该公司的所有快照
        cursor.execute("""
            SELECT odds_type, snapshot_at, handicap_line, high_water, low_water
            FROM odds_history
            WHERE match_id = ? AND bookmaker = ? AND market_type = 'asian'
            ORDER BY snapshot_at
        """, (match_id, bookmaker))

        rows = cursor.fetchall()
        if len(rows) < 2:
            return False

        # 分离开盘和收盘数据
        snapshots = []
        for row in rows:
            snapshots.append({
                "odds_type": row[0],
                "snapshot_at": row[1],
                "line": row[2],
                "hw": row[3],
                "lw": row[4],
            })

        # 开盘数据 (第一个 snapshot)
        open_data = snapshots[0]
        # 收盘数据 (最后一个 snapshot)
        close_data = snapshots[-1]

        open_line = open_data["line"]
        close_line = close_data["line"]
        open_hw = open_data["hw"]
        close_hw = close_data["hw"]
        open_lw = open_data["lw"]
        close_lw = close_data["lw"]

        if open_line is None or close_line is None:
            return False

        # 计算变化
        line_change = close_line - open_line
        line_change_pct = line_change / abs(open_line) if open_line != 0 else 0
        hw_change = (close_hw - open_hw) if close_hw and open_hw else None
        lw_change = (close_lw - open_lw) if close_lw and open_lw else None

        # 变化类型
        if abs(line_change) >= 0.5:
            movement_type = "跳盘"
            magnitude = "大幅"
        elif abs(line_change) >= 0.25:
            movement_type = "升盘" if line_change > 0 else "降盘"
            magnitude = "中等"
        elif abs(line_change) > 0:
            movement_type = "微升盘" if line_change > 0 else "微降盘"
            magnitude = "轻微"
        else:
            movement_type = "盘口稳定"
            magnitude = "无变化"

        # 变化方向
        movement_direction = 1 if line_change > 0 else (-1 if line_change < 0 else 0)

        # 计算波动率和反转次数
        lines = [s["line"] for s in snapshots if s["line"] is not None]
        volatility = np.std(lines) if len(lines) > 1 else 0

        reversals = 0
        for i in range(1, len(lines)):
            if lines[i] != lines[i-1]:
                # 检测反转
                if i > 1:
                    prev_dir = lines[i-1] - lines[i-2]
                    curr_dir = lines[i] - lines[i-1]
                    if prev_dir * curr_dir < 0:
                        reversals += 1

        # 插入或更新
        cursor.execute("""
            INSERT OR REPLACE INTO line_movements
            (match_id, bookmaker, market_type, open_line, close_line, line_change,
             line_change_pct, open_high_water, close_high_water, hw_change,
             open_low_water, close_low_water, lw_change, movement_type,
             movement_magnitude, movement_direction, n_snapshots, volatility,
             reversal_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (match_id, bookmaker, "asian", open_line, close_line, line_change,
              line_change_pct, open_hw, close_hw, hw_change,
              open_lw, close_lw, lw_change, movement_type,
              magnitude, movement_direction, len(snapshots), volatility, reversals))

        return True

    # ═══════════════════════════════════════════════════════════════
    # Steam Move 检测
    # ═══════════════════════════════════════════════════════════════

    def detect_steam_moves(self):
        """检测蒸汽移动 (Steam Move) 和反向盘口移动 (Reverse Line Movement)。"""
        print("\n[Steam Move] 检测蒸汽移动...")

        cursor = self.conn.cursor()

        # 获取所有盘口变化数据
        cursor.execute("""
            SELECT lm.*, m.home_score, m.away_score, m.home_team, m.away_team
            FROM line_movements lm
            JOIN matches m ON lm.match_id = m.match_id
            WHERE lm.market_type = 'asian'
        """)
        rows = cursor.fetchall()

        print(f"  待分析: {len(rows)} 条盘口变化记录")

        steam_count = 0
        rlm_count = 0

        for row in rows:
            data = dict(row)

            # 计算 steam move 分数
            steam_score, steam_type = self._compute_steam_score(data)

            if steam_score > 0:
                steam_count += 1
                self._save_steam_move(data, steam_score, steam_type)

            # 检测反向盘口移动 (RLM)
            is_rlm = self._detect_reverse_line_movement(data)
            if is_rlm:
                rlm_count += 1

        self.conn.commit()
        print(f"  Steam Moves: {steam_count} 个")
        print(f"  Reverse Line Movements: {rlm_count} 个")

    def _compute_steam_score(self, data: dict) -> Tuple[float, str]:
        """计算 Steam Move 分数。"""
        score = 0
        steam_type = ""

        line_change = abs(data.get("line_change", 0) or 0)
        hw_change = data.get("hw_change")
        volatility = data.get("volatility", 0) or 0
        reversals = data.get("reversal_count", 0) or 0

        # 因子1: 盘口变化幅度
        if line_change >= 0.5:
            score += 40
            steam_type = "大幅跳盘"
        elif line_change >= 0.25:
            score += 25
            steam_type = "中等升盘"
        elif line_change >= 0.10:
            score += 10
            steam_type = "小幅升盘"

        # 因子2: 水位变化方向与盘口方向一致
        if hw_change is not None:
            # 升盘 + 高水下降 = 强势升盘
            if data.get("line_change", 0) > 0 and hw_change < -0.03:
                score += 30
                steam_type += "+降水确认"
            # 降盘 + 低水下降 = 强势降盘
            elif data.get("line_change", 0) < 0 and (data.get("lw_change") or 0) < -0.03:
                score += 30
                steam_type += "+降水确认"

        # 因子3: 波动率 (快速变化)
        if volatility > 0.2:
            score += 15
        elif volatility > 0.1:
            score += 8

        # 因子4: 无反转 (单向流动)
        if reversals == 0 and line_change > 0.1:
            score += 10

        return score, steam_type

    def _detect_reverse_line_movement(self, data: dict) -> bool:
        """检测反向盘口移动 (RLM)。"""
        # RLM: 盘口变化方向与公众投注方向相反
        # 例如: 公众投主队 → 盘口降盘 (诱主队)
        # 或: 公众投客队 → 盘口升盘 (诱客队)

        line_change = data.get("line_change", 0) or 0
        hw_change = data.get("hw_change") or 0
        lw_change = data.get("lw_change") or 0

        # 简化判断:
        # 升盘 + 高水上升 = 可能是诱上盘
        # 降盘 + 低水上升 = 可能是诱下盘
        if line_change > 0.25 and hw_change and hw_change > 0.05:
            return True
        if line_change < -0.25 and lw_change and lw_change > 0.05:
            return True

        return False

    def _save_steam_move(self, data: dict, steam_score: float, steam_type: str):
        """保存 Steam Move 记录。"""
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO steam_moves
            (match_id, bookmaker, market_type, detected_at, start_snapshot, end_snapshot,
             line_before, line_after, line_delta, water_before, water_after, water_delta,
             steam_type, steam_score, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["match_id"], data["bookmaker"], data["market_type"],
            datetime.now().isoformat(),
            None, None,  # 时间戳信息
            data.get("open_line"), data.get("close_line"), data.get("line_change"),
            data.get("open_high_water"), data.get("close_high_water"), data.get("hw_change"),
            steam_type, steam_score,
            "HIGH" if steam_score >= 60 else ("MEDIUM" if steam_score >= 40 else "LOW")
        ))

    # ═══════════════════════════════════════════════════════════════
    # Sharp Money 分析
    # ═══════════════════════════════════════════════════════════════

    def analyze_sharp_money(self):
        """分析 Sharp Money 行为。"""
        print("\n[Sharp Money] 分析职业资金信号...")

        cursor = self.conn.cursor()

        # Sharp money 特征:
        # 1. 多家公司同时同向变动
        # 2. 盘口变动早于公众投注高峰
        # 3. 变动方向与市场共识相反 (RLM)
        # 4. 变动后不回调

        cursor.execute("""
            SELECT match_id, bookmaker, line_change, hw_change, lw_change,
                   movement_type, volatility, reversal_count
            FROM line_movements
            WHERE market_type = 'asian' AND line_change IS NOT NULL
        """)
        rows = cursor.fetchall()

        # 按 match_id 分组
        match_data = {}
        for row in rows:
            match_id = row[0]
            if match_id not in match_data:
                match_data[match_id] = []
            match_data[match_id].append(dict(row))

        sharp_signals = []

        for match_id, movements in match_data.items():
            signal = self._detect_sharp_signal(match_id, movements)
            if signal:
                sharp_signals.append(signal)
                self._save_sharp_signal(match_id, signal)

        self.conn.commit()
        print(f"  检测到 {len(sharp_signals)} 个 Sharp Money 信号")

        return sharp_signals

    def _detect_sharp_signal(self, match_id: str, movements: List[dict]) -> Optional[dict]:
        """检测单场比赛的 Sharp Money 信号。"""
        if len(movements) < 2:
            return None

        # 多家公司同时同向变动
        directions = [m["line_change"] for m in movements if m["line_change"]]
        if not directions:
            return None

        avg_direction = np.mean(directions)
        consistency = sum(1 for d in directions if d * avg_direction > 0) / len(directions)

        if consistency >= 0.8 and abs(avg_direction) >= 0.15:
            return {
                "signal_type": "multi_bookmaker_consensus",
                "signal_strength": consistency * abs(avg_direction) * 100,
                "line_direction": 1 if avg_direction > 0 else -1,
                "n_bookmakers": len(movements),
                "consistency": consistency,
            }

        # 检测 RLM (反向盘口移动)
        for m in movements:
            if m.get("movement_type") in ["跳盘", "升盘", "降盘"]:
                hw_chg = m.get("hw_change") or 0
                lw_chg = m.get("lw_change") or 0
                line_chg = m.get("line_change") or 0

                # 升盘 + 高水上升 = RLM
                if line_chg > 0.25 and hw_chg > 0.05:
                    return {
                        "signal_type": "reverse_line_movement",
                        "signal_strength": 50 + abs(line_chg) * 50,
                        "line_direction": -1,  # 诱上盘, 实际看好下盘
                        "bookmaker": m["bookmaker"],
                    }
                # 降盘 + 低水上升 = RLM
                if line_chg < -0.25 and lw_chg > 0.05:
                    return {
                        "signal_type": "reverse_line_movement",
                        "signal_strength": 50 + abs(line_chg) * 50,
                        "line_direction": 1,  # 诱下盘, 实际看好上盘
                        "bookmaker": m["bookmaker"],
                    }

        return None

    def _save_sharp_signal(self, match_id: str, signal: dict):
        """保存 Sharp Money 信号。"""
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO sharp_money_signals
            (match_id, signal_type, signal_strength, bookmakers_involved,
             line_direction, confidence, reasoning)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            match_id,
            signal["signal_type"],
            signal["signal_strength"],
            signal.get("bookmaker", ""),
            signal["line_direction"],
            "HIGH" if signal["signal_strength"] >= 70 else "MEDIUM",
            json.dumps(signal, ensure_ascii=False)
        ))

    # ═══════════════════════════════════════════════════════════════
    # 报告生成
    # ═══════════════════════════════════════════════════════════════

    def generate_reports(self):
        """生成分析报告。"""
        print("\n[Reports] 生成报告...")

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        cursor = self.conn.cursor()

        # ── 1. 盘口变化统计 ──
        cursor.execute("""
            SELECT movement_type, COUNT(*) as n,
                   AVG(line_change) as avg_change,
                   AVG(volatility) as avg_volatility
            FROM line_movements
            GROUP BY movement_type
            ORDER BY n DESC
        """)
        movement_stats = cursor.fetchall()

        # ── 2. Steam Move 统计 ──
        cursor.execute("""
            SELECT steam_type, COUNT(*) as n, AVG(steam_score) as avg_score
            FROM steam_moves
            GROUP BY steam_type
            ORDER BY n DESC
        """)
        steam_stats = cursor.fetchall()

        # ── 3. Sharp Money 信号统计 ──
        cursor.execute("""
            SELECT signal_type, COUNT(*) as n, AVG(signal_strength) as avg_strength
            FROM sharp_money_signals
            GROUP BY signal_type
        """)
        sharp_stats = cursor.fetchall()

        # ── 4. 博彩公司对比 ──
        cursor.execute("""
            SELECT lm.bookmaker, COUNT(*) as n,
                   AVG(ABS(lm.line_change)) as avg_line_change,
                   AVG(lm.volatility) as avg_volatility,
                   SUM(CASE WHEN sm.steam_score > 0 THEN 1 ELSE 0 END) as steam_count
            FROM line_movements lm
            LEFT JOIN steam_moves sm ON lm.match_id = sm.match_id AND lm.bookmaker = sm.bookmaker
            GROUP BY lm.bookmaker
            ORDER BY n DESC
        """)
        bookmaker_stats = cursor.fetchall()

        # ── 5. 导出 CSV ──
        # 盘口变化明细
        cursor.execute("""
            SELECT lm.*, m.home_team, m.away_team, m.kickoff_time,
                   m.home_score, m.away_score
            FROM line_movements lm
            JOIN matches m ON lm.match_id = m.match_id
        """)
        lm_df = pd.DataFrame([dict(row) for row in cursor.fetchall()])
        lm_df.to_csv(REPORTS_DIR / "line_movements.csv", index=False, encoding="utf-8-sig")
        print(f"  ✓ line_movements.csv ({len(lm_df)} 行)")

        # Steam moves 明细
        cursor.execute("""
            SELECT sm.*, m.home_team, m.away_team, m.kickoff_time
            FROM steam_moves sm
            JOIN matches m ON sm.match_id = m.match_id
        """)
        sm_df = pd.DataFrame([dict(row) for row in cursor.fetchall()])
        sm_df.to_csv(REPORTS_DIR / "steam_moves.csv", index=False, encoding="utf-8-sig")
        print(f"  ✓ steam_moves.csv ({len(sm_df)} 行)")

        # Sharp money 信号
        cursor.execute("""
            SELECT sms.*, m.home_team, m.away_team
            FROM sharp_money_signals sms
            JOIN matches m ON sms.match_id = m.match_id
        """)
        sharp_df = pd.DataFrame([dict(row) for row in cursor.fetchall()])
        sharp_df.to_csv(REPORTS_DIR / "sharp_money_signals.csv", index=False, encoding="utf-8-sig")
        print(f"  ✓ sharp_money_signals.csv ({len(sharp_df)} 行)")

        # ── 6. 生成 HTML 报告 ──
        self._generate_html_report(movement_stats, steam_stats, sharp_stats, bookmaker_stats)

        # ── 7. JSON 汇总 ──
        summary = {
            "generated_at": datetime.now().isoformat(),
            "movement_stats": [dict(r) for r in movement_stats],
            "steam_stats": [dict(r) for r in steam_stats],
            "sharp_stats": [dict(r) for r in sharp_stats],
            "bookmaker_stats": [dict(r) for r in bookmaker_stats],
        }
        with open(REPORTS_DIR / "odds_history_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        print(f"  ✓ odds_history_summary.json")

    def _generate_html_report(self, movement_stats, steam_stats, sharp_stats, bookmaker_stats):
        """生成 HTML 报告。"""

        # 表格生成函数
        def make_table(rows, headers):
            html = "<table><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
            for row in rows:
                html += "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
            return html + "</table>"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>赔率历史分析报告</title>
<style>
body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; background: #f5f5f5; }}
h1 {{ color: #1a1a2e; }} h2 {{ color: #16213e; border-bottom: 2px solid #0f3460; padding-bottom: 5px; }}
.card {{ background: white; border-radius: 8px; padding: 20px; margin: 15px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
th {{ background: #0f3460; color: white; }}
tr:nth-child(even) {{ background: #f8f8f8; }}
.stats {{ display: flex; gap: 15px; flex-wrap: wrap; }}
.stat {{ background: #0f3460; color: white; padding: 15px; border-radius: 8px; min-width: 120px; text-align: center; }}
.stat .value {{ font-size: 28px; font-weight: bold; }}
.stat .label {{ font-size: 12px; opacity: 0.8; margin-top: 5px; }}
</style>
</head>
<body>
<h1>赔率历史时间序列分析报告</h1>
<p>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

<div class="card">
<h2>核心指标</h2>
<div class="stats">
<div class="stat"><div class="value">{len(movement_stats)}</div><div class="label">盘口变化类型</div></div>
<div class="stat"><div class="value">{len(steam_stats)}</div><div class="label">Steam Move 类型</div></div>
<div class="stat"><div class="value">{sum(r[1] for r in steam_stats)}</div><div class="label">Steam Move 数量</div></div>
<div class="stat"><div class="value">{sum(r[1] for r in sharp_stats)}</div><div class="label">Sharp Money 信号</div></div>
</div>
</div>

<div class="card">
<h2>盘口变化统计</h2>
{make_table([(r[0], r[1], f"{r[2]:.3f}", f"{r[3]:.3f}") for r in movement_stats], ["变化类型", "数量", "平均变化", "平均波动率"])}
</div>

<div class="card">
<h2>Steam Move 统计</h2>
{make_table([(r[0], r[1], f"{r[2]:.1f}") for r in steam_stats], ["类型", "数量", "平均分数"])}
</div>

<div class="card">
<h2>Sharp Money 信号</h2>
{make_table([(r[0], r[1], f"{r[2]:.1f}") for r in sharp_stats], ["信号类型", "数量", "平均强度"])}
</div>

<div class="card">
<h2>博彩公司对比</h2>
{make_table([(r[0], r[1], f"{r[2]:.3f}", f"{r[3]:.3f}", r[4] or 0) for r in bookmaker_stats], ["公司", "样本数", "平均变化", "平均波动率", "Steam Move"])}
</div>

<div class="card">
<h2>方法论</h2>
<pre>
<b>Steam Move (蒸汽移动)</b>
定义: 短时间内盘口大幅变动，通常由职业资金驱动
检测标准:
  - 盘口变化 ≥ 0.25 球
  - 水位同向变化 (升盘+降水)
  - 波动率高 + 无反转

<b>Reverse Line Movement (反向盘口移动)</b>
定义: 盘口变动方向与公众投注方向相反
检测标准:
  - 升盘 + 高水上升 (诱上盘)
  - 降盘 + 低水上升 (诱下盘)

<b>Sharp Money Signal</b>
定义: 职业资金留下的市场痕迹
检测标准:
  - 多家公司同时同向变动 (一致性 ≥ 80%)
  - 反向盘口移动 (RLM)
</pre>
</div>
</body></html>"""

        with open(REPORTS_DIR / "odds_history_report.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  ✓ odds_history_report.html")

    # ═══════════════════════════════════════════════════════════════
    # 主流程
    # ═══════════════════════════════════════════════════════════════

    def run(self):
        """运行完整流程。"""
        print("=" * 60)
        print("赔率历史时间序列引擎 v1.0")
        print("Odds History Time Series Engine")
        print("=" * 60)

        self.connect()
        self.migrate()
        self.import_from_odds_asian()
        self.compute_line_movements()
        self.detect_steam_moves()
        self.analyze_sharp_money()
        self.generate_reports()

        print("\n" + "=" * 60)
        print("[完成] 所有分析已完成")
        print("=" * 60)

        self.close()


def main():
    engine = OddsHistoryEngine()
    engine.run()


if __name__ == "__main__":
    main()
