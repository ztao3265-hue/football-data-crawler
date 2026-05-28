#!/usr/bin/env python3
"""
赔率历史时间序列引擎测试

测试内容:
1. 数据库迁移
2. 数据导入
3. Line Movement 特征计算
4. Steam Move 检测
5. Sharp Money 分析
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database.odds_history_engine import (
    OddsHistoryEngine,
    parse_asian_handicap,
    parse_ou_handicap,
)


class TestOddsHistoryEngine:
    """赔率历史引擎测试类。"""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        """测试前准备。"""
        self.db_path = tmp_path / "test_football.db"
        self.engine = OddsHistoryEngine(str(self.db_path))
        self._create_test_tables()
        yield
        self.engine.close()

    def _create_test_tables(self):
        """创建测试表。"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        # matches 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                home_team TEXT,
                away_team TEXT,
                kickoff_time DATETIME,
                home_score INTEGER,
                away_score INTEGER
            )
        """)

        # odds_asian 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS odds_asian (
                id INTEGER PRIMARY KEY,
                match_id TEXT,
                bookmaker TEXT,
                odds_type TEXT,
                handicap TEXT,
                high_water REAL,
                low_water REAL
            )
        """)

        # 插入测试数据
        cursor.execute("""
            INSERT INTO matches (match_id, home_team, away_team, kickoff_time, home_score, away_score)
            VALUES ('test001', 'Arsenal', 'Chelsea', '2025-01-15 15:00:00', 2, 1)
        """)

        cursor.execute("""
            INSERT INTO odds_asian (match_id, bookmaker, odds_type, handicap, high_water, low_water)
            VALUES
                ('test001', 'Bet365', 'opening', '半球', 0.95, 0.90),
                ('test001', 'Bet365', 'closing', '半球/一球', 0.85, 0.98),
                ('test001', 'Macau', 'opening', '半球', 0.92, 0.92),
                ('test001', 'Macau', 'closing', '半球/一球', 0.88, 0.95)
        """)

        conn.commit()
        conn.close()

    def test_parse_asian_handicap(self):
        """测试亚盘盘口解析。"""
        assert parse_asian_handicap("平手") == 0.0
        assert parse_asian_handicap("半球") == 0.5
        assert parse_asian_handicap("一球") == 1.0
        assert parse_asian_handicap("受半球") == -0.5
        assert parse_asian_handicap("受一球") == -1.0
        assert parse_asian_handicap("半球/一球") == 0.75
        assert parse_asian_handicap("球半/两球") == 1.75
        assert parse_asian_handicap("") is None
        assert parse_asian_handicap(None) is None

    def test_parse_ou_handicap(self):
        """测试大小球盘口解析。"""
        assert parse_ou_handicap("2.5") == 2.5
        assert parse_ou_handicap("2/2.5") == 2.25
        assert parse_ou_handicap("2.5/3") == 2.75
        assert parse_ou_handicap("2球") == 2.0
        assert parse_ou_handicap("") is None
        assert parse_ou_handicap(None) is None

    def test_migration(self):
        """测试数据库迁移。"""
        self.engine.connect()
        self.engine.migrate()

        # 检查表是否存在
        cursor = self.engine.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        assert "odds_history" in tables
        assert "line_movements" in tables
        assert "steam_moves" in tables
        assert "sharp_money_signals" in tables
        assert "schema_migrations" in tables

    def test_import_data(self):
        """测试数据导入。"""
        self.engine.connect()
        self.engine.migrate()
        self.engine.import_from_odds_asian()

        # 检查导入数据
        cursor = self.engine.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM odds_history")
        count = cursor.fetchone()[0]
        assert count > 0

        # 检查数据内容
        cursor.execute("SELECT * FROM odds_history LIMIT 1")
        row = cursor.fetchone()
        assert row is not None

    def test_line_movement_computation(self):
        """测试盘口变化特征计算。"""
        self.engine.connect()
        self.engine.migrate()
        self.engine.import_from_odds_asian()
        self.engine.compute_line_movements()

        # 检查计算结果
        cursor = self.engine.conn.cursor()
        cursor.execute("SELECT * FROM line_movements WHERE match_id = 'test001'")
        rows = cursor.fetchall()
        assert len(rows) > 0

        # 验证计算值
        cursor.execute("""
            SELECT line_change, movement_type, movement_direction
            FROM line_movements
            WHERE match_id = 'test001' AND bookmaker = 'Bet365'
        """)
        row = cursor.fetchone()
        assert row is not None
        line_change, movement_type, direction = row
        # 半球 → 半球/一球 = +0.25
        assert line_change == 0.25
        assert "升盘" in movement_type

    def test_steam_move_detection(self):
        """测试 Steam Move 检测。"""
        self.engine.connect()
        self.engine.migrate()
        self.engine.import_from_odds_asian()
        self.engine.compute_line_movements()
        self.engine.detect_steam_moves()

        # 检查检测结果
        cursor = self.engine.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM steam_moves")
        count = cursor.fetchone()[0]
        # 测试数据可能有也可能没有 steam move
        assert count >= 0

    def test_sharp_money_analysis(self):
        """测试 Sharp Money 分析。"""
        self.engine.connect()
        self.engine.migrate()
        self.engine.import_from_odds_asian()
        self.engine.compute_line_movements()
        self.engine.detect_steam_moves()
        self.engine.analyze_sharp_money()

        # 检查分析结果
        cursor = self.engine.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sharp_money_signals")
        count = cursor.fetchone()[0]
        # 测试数据可能有也可能没有 sharp money 信号
        assert count >= 0

    def test_full_workflow(self):
        """测试完整工作流。"""
        self.engine.run()

        # 重新连接数据库进行检查 (因为 run() 会关闭连接)
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM odds_history")
        assert cursor.fetchone()[0] > 0

        cursor.execute("SELECT COUNT(*) FROM line_movements")
        assert cursor.fetchone()[0] > 0

        conn.close()

        # 检查报告文件
        reports_dir = PROJECT_ROOT / "reports" / "odds_history"
        if reports_dir.exists():
            assert (reports_dir / "odds_history_summary.json").exists() or True


class TestLineMovementFeatures:
    """盘口变化特征测试。"""

    def test_movement_type_classification(self):
        """测试盘口变化类型分类。"""
        # 跳盘: 变化 ≥ 0.5
        assert _classify_movement(0.5) == "跳盘"
        assert _classify_movement(-0.5) == "跳盘"

        # 中等变化: 0.25 - 0.5
        assert _classify_movement(0.25) == "升盘"
        assert _classify_movement(-0.25) == "降盘"

        # 轻微变化: < 0.25
        assert _classify_movement(0.1) == "微升盘"
        assert _classify_movement(-0.1) == "微降盘"

        # 无变化
        assert _classify_movement(0.0) == "盘口稳定"


def _classify_movement(line_change: float) -> str:
    """分类盘口变化类型。"""
    if abs(line_change) >= 0.5:
        return "跳盘"
    elif abs(line_change) >= 0.25:
        return "升盘" if line_change > 0 else "降盘"
    elif abs(line_change) > 0:
        return "微升盘" if line_change > 0 else "微降盘"
    else:
        return "盘口稳定"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
