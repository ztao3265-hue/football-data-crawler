#!/usr/bin/env python3
"""自动测试 — 验证历史数据导入完整性"""

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

from config.paths import DB_FOOTBALL_HISTORY, RAW_DATA_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = DB_FOOTBALL_HISTORY
CSV_DIR = RAW_DATA_DIR / "csv"
JSON_DIR = RAW_DATA_DIR / "json"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"


class ImportTests:
    def __init__(self):
        self.conn = None
        self.passed = 0
        self.failed = 0
        self.errors = []

    def assert_true(self, condition, msg):
        if condition:
            self.passed += 1
            print(f"  PASS: {msg}")
        else:
            self.failed += 1
            self.errors.append(msg)
            print(f"  FAIL: {msg}")

    def run_all(self):
        print("=" * 60)
        print("历史数据导入 — 自动测试")
        print("=" * 60)

        self.conn = sqlite3.connect(str(DB_PATH))

        self.test_database_exists()
        self.test_tables_exist()
        self.test_table_counts()
        self.test_league_data()
        self.test_match_data()
        self.test_score_parsing()
        self.test_season_data()
        self.test_odds_completeness()
        self.test_features_computation()
        self.test_deduplication()
        self.test_csv_exports()
        self.test_json_exports()
        self.test_reports_generated()

        self.conn.close()

        print(f"\n{'=' * 60}")
        print(f"结果: {self.passed} 通过, {self.failed} 失败")
        if self.errors:
            print("失败项:")
            for e in self.errors:
                print(f"  - {e}")
        print(f"{'=' * 60}")

        return self.failed == 0

    def test_database_exists(self):
        print("\n## 1. 数据库文件")
        self.assert_true(DB_PATH.exists(), f"数据库文件存在: {DB_PATH.name}")
        if DB_PATH.exists():
            size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
            self.assert_true(size_mb > 10, f"数据库大小合理 ({size_mb:.1f} MB > 10 MB)")

    def test_tables_exist(self):
        print("\n## 2. 表结构")
        expected_tables = [
            "leagues", "teams", "seasons", "matches",
            "odds_europe", "odds_asian", "odds_over_under",
            "odds_history", "match_features", "import_logs"
        ]
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in expected_tables:
            self.assert_true(t in tables, f"表 '{t}' 存在")

    def test_table_counts(self):
        print("\n## 3. 记录计数")
        checks = [
            ("leagues", 7, 10),
            ("teams", 200, 500),
            ("seasons", 3, 10),
            ("matches", 8000, 20000),
            ("odds_europe", 80000, 200000),
            ("odds_asian", 50000, 150000),
            ("odds_over_under", 60000, 200000),
            ("match_features", 8000, 20000),
        ]
        for table, min_val, max_val in checks:
            count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assert_true(min_val <= count <= max_val,
                           f"{table}: {count} (预期 {min_val}-{max_val})")

    def test_league_data(self):
        print("\n## 4. 联赛数据")
        leagues = self.conn.execute(
            "SELECT name, code FROM leagues WHERE code != 'SUM'").fetchall()
        expected_codes = {"EPL", "BUN", "ISA", "LLG", "FL1", "UCL", "UEL"}
        found_codes = {l[1] for l in leagues}
        self.assert_true(expected_codes == found_codes,
                        f"7大联赛编码完整: {found_codes}")

        # 每个联赛至少有 500 场比赛
        for league_code in expected_codes:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM matches m JOIN leagues l ON m.league_id = l.id WHERE l.code = ?",
                (league_code,)
            ).fetchone()[0]
            self.assert_true(count >= 500,
                           f"{league_code}: {count} 场 (>= 500)")

    def test_match_data(self):
        print("\n## 5. 比赛数据完整性")
        # 比分不为 NULL
        total = self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        with_score = self.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL"
        ).fetchone()[0]
        self.assert_true(with_score / total >= 0.95,
                        f"比分覆盖率 {with_score}/{total} = {with_score/total*100:.1f}% (>= 95%)")

        # 赛果不为空
        with_result = self.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE ft_result != ''"
        ).fetchone()[0]
        self.assert_true(with_result / total >= 0.95,
                        f"赛果覆盖率 {with_result}/{total} (>= 95%)")

        # 开球时间不为 NULL
        with_kickoff = self.conn.execute(
            "SELECT COUNT(*) FROM matches WHERE kickoff_time IS NOT NULL"
        ).fetchone()[0]
        self.assert_true(with_kickoff == total,
                        f"开球时间覆盖率 {with_kickoff}/{total} = 100%")

    def test_score_parsing(self):
        print("\n## 6. 比分解析正确性")
        # 随机抽样检查
        sample = self.conn.execute(
            "SELECT home_score, away_score, score_display FROM matches "
            "WHERE home_score IS NOT NULL AND score_display != '' LIMIT 100"
        ).fetchall()
        correct = 0
        for h, a, sd in sample:
            expected = f"{h}-{a}"
            actual = sd.strip().replace(" ", "")
            if expected == actual:
                correct += 1
        rate = correct / len(sample) * 100 if sample else 0
        self.assert_true(rate >= 80,
                        f"比分一致性 {correct}/{len(sample)} = {rate:.0f}% (>= 80%)")

    def test_season_data(self):
        print("\n## 7. 赛季数据")
        seasons = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT season FROM matches ORDER BY season").fetchall()]
        expected = ['2021', '2022', '2023', '2024', '2025']
        self.assert_true(seasons == expected or all(s in seasons for s in expected),
                        f"赛季范围: {seasons} (期望 {expected})")

    def test_odds_completeness(self):
        print("\n## 8. 赔率数据完整性")
        # 每场比赛至少有一组欧赔
        match_count = self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        matches_with_odds = self.conn.execute(
            "SELECT COUNT(DISTINCT match_id) FROM odds_europe").fetchone()[0]
        self.assert_true(matches_with_odds / match_count >= 0.95,
                        f"欧赔覆盖率 {matches_with_odds}/{match_count} (>= 95%)")

        # 博彩公司数量
        bookmakers = self.conn.execute(
            "SELECT COUNT(DISTINCT bookmaker) FROM odds_europe").fetchone()[0]
        self.assert_true(bookmakers >= 4,
                        f"博彩公司数量: {bookmakers} (>= 4)")

    def test_features_computation(self):
        print("\n## 9. AI 特征")
        features_count = self.conn.execute(
            "SELECT COUNT(*) FROM match_features").fetchone()[0]
        match_count = self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        self.assert_true(features_count == match_count,
                        f"特征行数 = 比赛行数 ({features_count} = {match_count})")

        # 隐含概率
        has_prob = self.conn.execute(
            "SELECT COUNT(*) FROM match_features WHERE implied_home_prob IS NOT NULL"
        ).fetchone()[0]
        self.assert_true(has_prob / features_count >= 0.85,
                        f"隐含概率覆盖率 {has_prob}/{features_count} (>= 85%)")

        # 标签
        labeled = self.conn.execute(
            "SELECT COUNT(*) FROM match_features WHERE home_win + draw + away_win = 1"
        ).fetchone()[0]
        self.assert_true(labeled / features_count >= 0.85,
                        f"标签覆盖率 {labeled}/{features_count} (>= 85%)")

        # 大小球标签
        ou_labeled = self.conn.execute(
            "SELECT COUNT(*) FROM match_features WHERE over_2_5 IN (0,1) AND total_goals IS NOT NULL"
        ).fetchone()[0]
        self.assert_true(ou_labeled / features_count >= 0.85,
                        f"O/U标签覆盖率 {ou_labeled}/{features_count} (>= 85%)")

    def test_deduplication(self):
        print("\n## 10. 去重验证")
        # match_id 唯一
        total = self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        unique = self.conn.execute("SELECT COUNT(DISTINCT match_id) FROM matches").fetchone()[0]
        self.assert_true(total == unique,
                        f"match_id 唯一性 {unique}/{total} = 100%")

        # 赔率表 match_id 唯一约束
        duplicate_odds = self.conn.execute(
            "SELECT match_id, bookmaker, odds_type, COUNT(*) as cnt "
            "FROM odds_europe GROUP BY match_id, bookmaker, odds_type HAVING cnt > 1"
        ).fetchall()
        self.assert_true(len(duplicate_odds) == 0,
                        f"欧赔去重: {len(duplicate_odds)} 条重复 (期望 0)")

    def test_csv_exports(self):
        print("\n## 11. CSV 导出")
        expected_files = [
            "leagues.csv", "teams.csv", "seasons.csv", "matches.csv",
            "odds_europe.csv", "odds_asian.csv", "odds_over_under.csv"
        ]
        for f in expected_files:
            path = CSV_DIR / f
            self.assert_true(path.exists(), f"CSV 文件存在: {f}")
            if path.exists():
                size = os.path.getsize(path)
                self.assert_true(size > 100, f"  {f}: {size:,} bytes (> 100)")

    def test_json_exports(self):
        print("\n## 12. JSON 导出")
        expected_files = [
            "leagues.json", "teams.json", "seasons.json", "matches.json",
            "odds_europe.json", "odds_asian.json", "odds_over_under.json",
            "data_package.json"
        ]
        for f in expected_files:
            path = JSON_DIR / f
            self.assert_true(path.exists(), f"JSON 文件存在: {f}")
            if path.exists():
                size = os.path.getsize(path)
                self.assert_true(size > 100, f"  {f}: {size:,} bytes (> 100)")

    def test_reports_generated(self):
        print("\n## 13. 质量报告")
        reports = list(REPORTS_DIR.glob("import_report_*.txt"))
        self.assert_true(len(reports) >= 1, f"文本报告: {len(reports)} 个")
        json_reports = list(REPORTS_DIR.glob("import_report_*.json"))
        self.assert_true(len(json_reports) >= 1, f"JSON 报告: {len(json_reports)} 个")


if __name__ == "__main__":
    tester = ImportTests()
    success = tester.run_all()
    sys.exit(0 if success else 1)
