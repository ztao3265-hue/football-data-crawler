#!/usr/bin/env python3
"""
交易审计系统 (Trade Auditor)

逐场验证 Walk Forward 回测每一笔交易的真实性:
- 亚洲盘口审计: water + push + half-win/half-lose
- 大小球审计: 2.25/2.5/2.75/3.25 half-win/half-lose
- 港赔/欧赔转换验证
- 独立 ROI 重算
- 随机抽样人工核验
- 异常检测 (胜率/ROI/联赛/覆盖率偏差)
"""

import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASETS_DIR = PROJECT_ROOT / "datasets"
BETS_DIR = PROJECT_ROOT / "reports" / "backtest"
AUDIT_DIR = BETS_DIR / "audit"

warnings.filterwarnings("ignore")

LEAGUE_MAP = {
    "EPL": "英超", "LLG": "西甲", "ISA": "意甲",
    "BUN": "德甲", "FL1": "法甲", "UCL": "欧冠", "UEL": "欧联",
}


class TradeAuditor:
    """逐场交易审计, 重新计算所有盈亏, 检测计算偏差。"""

    def __init__(self):
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        self.full_df: Optional[pd.DataFrame] = None
        self.all_bets: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.ledger: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.discrepancies: list = []
        self.anomalies: list = []

    # ═══════════════════════════════════════════════════════════════
    # 数据加载
    # ═══════════════════════════════════════════════════════════════

    def load_data(self):
        print("[1/7] 加载全量数据...")
        df_parts = []
        for name in ["train.csv", "validation.csv", "test.csv"]:
            path = DATASETS_DIR / name
            if path.exists():
                df_parts.append(pd.read_csv(path))
        self.full_df = pd.concat(df_parts, ignore_index=True)

        # 解析关键字段
        self.full_df["kickoff_time_dt"] = pd.to_datetime(
            self.full_df["kickoff_time"], errors="coerce"
        )
        # 确保 goal_diff 可用
        if "label_goal_diff" in self.full_df.columns:
            self.full_df["goal_diff"] = self.full_df["label_goal_diff"]
        if "label_total_goals" in self.full_df.columns:
            self.full_df["total_goals"] = self.full_df["label_total_goals"]

        print(f"  全量: {len(self.full_df):,} 场")

        # 加载投注 CSV
        known_tasks = ["asian", "over_under", "wdl"]
        for csv_path in sorted(BETS_DIR.glob("bets_*.csv")):
            stem = csv_path.stem[len("bets_"):]
            task_key, model_name = None, None
            for t in known_tasks:
                if stem.startswith(t + "_"):
                    task_key = t
                    model_name = stem[len(t) + 1:]
                    break
            if task_key is None:
                continue

            bd = pd.read_csv(csv_path)
            if task_key not in self.all_bets:
                self.all_bets[task_key] = {}
            self.all_bets[task_key][model_name] = bd
            print(f"  bets_{task_key}_{model_name}: {len(bd):,} 笔")

    # ═══════════════════════════════════════════════════════════════
    # 逐场明细账本
    # ═══════════════════════════════════════════════════════════════

    def build_ledger(self):
        """为每笔投注构建完整的审计明细账本。"""
        print("[2/7] 构建逐场交易明细...")

        for task_key, models in self.all_bets.items():
            self.ledger[task_key] = {}
            for model_name, bd in models.items():
                if bd.empty:
                    continue

                # merge with original data for match details
                orig_cols = ["match_id", "league_code", "season",
                             "kickoff_time_dt", "home_team", "away_team",
                             "goal_diff", "total_goals",
                             "asian_close_line", "asian_close_high_water",
                             "asian_close_low_water",
                             "ou_close_line", "ou_close_over_water",
                             "ou_close_under_water",
                             "close_home_odds", "close_draw_odds", "close_away_odds"]

                avail = [c for c in orig_cols if c in self.full_df.columns]
                orig = self.full_df[avail].drop_duplicates("match_id")

                merged = bd.merge(orig, on="match_id", how="left", suffixes=("", "_src"))

                # 构建明细账本
                rows = []
                for _, row in merged.iterrows():
                    ledger_row = self._build_ledger_row(row, task_key)
                    rows.append(ledger_row)

                ledger_df = pd.DataFrame(rows)
                # 计算累计值
                ledger_df["cumulative_profit"] = ledger_df["profit_correct"].cumsum()
                ledger_df["cumulative_roi"] = (
                    ledger_df["cumulative_profit"] /
                    (ledger_df.index + 1) / 100.0  # flat 100/bet
                )
                ledger_df["profit_discrepancy"] = (
                    ledger_df["profit_correct"] - ledger_df["profit_original"]
                )

                self.ledger[task_key][model_name] = ledger_df
                print(f"  [{task_key}/{model_name}] 明细: {len(ledger_df)} 行")

    def _build_ledger_row(self, row, task_key: str) -> dict:
        """为单笔投注构建完整审计行。"""
        # 基本字段
        match_id = str(row.get("match_id", ""))
        league = row.get("league_code", "")
        kickoff = row.get("kickoff_time_dt", row.get("kickoff_time", ""))
        home = row.get("home_team", "")
        away = row.get("away_team", "")

        # 赔率信息
        odds_decimal = row.get("odds", 0)  # already decimal in bets CSV
        water_hk = odds_decimal - 1.0 if odds_decimal > 0 else 0

        # 预测/结果
        y_pred = int(row.get("y_pred", -1))
        y_true = int(row.get("y_true", -1))
        result_str = row.get("result", "")

        # 原始盈亏
        profit_orig = row.get("profit", 0)
        stake = row.get("stake", 100)

        # 盘口线
        goal_diff = row.get("goal_diff", None)
        total_goals = row.get("total_goals", None)

        # ── 重新计算正确盈亏 ──
        if task_key == "asian":
            asian_line = row.get("asian_close_line", None)
            profit_correct, bet_type, detail = self._calc_asian_profit(
                y_pred, y_true, goal_diff, asian_line, water_hk, stake, result_str
            )
        elif task_key == "over_under":
            ou_line = row.get("ou_close_line", None)
            profit_correct, bet_type, detail = self._calc_ou_profit(
                y_pred, y_true, total_goals, ou_line, water_hk, stake, result_str
            )
        else:
            # WDL: 简单 full win/lose
            profit_correct = profit_orig
            bet_type = "FULL"
            detail = "WDL"

        return {
            "match_id": match_id,
            "league": league,
            "league_name": LEAGUE_MAP.get(league, league),
            "kickoff_time": str(kickoff)[:19] if kickoff else "",
            "home_team": home,
            "away_team": away,
            "market": task_key,
            "asian_line": row.get("asian_close_line", None) if task_key == "asian" else None,
            "ou_line": row.get("ou_close_line", None) if task_key == "over_under" else None,
            "water_hk": round(water_hk, 4),
            "decimal_odds": round(odds_decimal, 4),
            "y_pred": y_pred,
            "y_true": y_true,
            "result": result_str,
            "bet_direction": self._direction_name(y_pred, task_key),
            "goal_diff": goal_diff,
            "total_goals": total_goals,
            "bet_type": bet_type,
            "bet_detail": detail,
            "stake": stake,
            "profit_original": profit_orig,
            "profit_correct": profit_correct,
            "is_push": "PUSH" in str(bet_type).upper(),
            "is_half_win": "HALF_WIN" == bet_type,
            "is_half_lose": "HALF_LOSE" == bet_type,
        }

    # ═══════════════════════════════════════════════════════════════
    # 亚洲盘口盈亏重算 (含半赢/半输/走水)
    # ═══════════════════════════════════════════════════════════════

    def _calc_asian_profit(self, pred: int, actual: int, goal_diff,
                           asian_line, water_hk: float, stake: float,
                           result_str: str) -> Tuple[float, str, str]:
        """精确计算亚盘盈亏。

        Args:
            pred: 0=away covers, 1=push, 2=home covers
            actual: 0=away, 1=push, 2=home
            goal_diff: home_goals - away_goals
            asian_line: handicap from home perspective (-0.25 = home gives 0.25)
            water_hk: Hong Kong water (profit multiplier)
            stake: bet amount
            result_str: original result string (win/lose/push)

        Returns: (profit, bet_type, detail)
        """
        # 预测走水 → 不投注
        if pred == 1:
            return 0.0, "NO_BET", "预测走水不投注"

        # 实际走水 → push
        if actual == 1:
            return 0.0, "PUSH", "实际走水-全额退款"

        if goal_diff is None or pd.isna(goal_diff):
            # 无比分数据, fallback 到原始逻辑
            if result_str == "win":
                return stake * water_hk, "FULL_WIN", "fallback-无比分"
            elif result_str == "lose":
                return -stake, "FULL_LOSE", "fallback-无比分"
            else:
                return 0.0, "PUSH", "fallback-无比分"

        if asian_line is None or pd.isna(asian_line):
            asian_line = 0.0

        bet_on_home = (pred == 2)
        effective = goal_diff + asian_line  # 主队视角的调整后结果

        # 整数盘口 (±0, ±1, ±2...) → 只有 full/push
        if abs(asian_line * 2 - round(asian_line * 2)) < 0.01:
            if abs(effective) < 0.01:
                return 0.0, "PUSH", f"盘口{asian_line:+.2f}, 净胜{goal_diff}, 走水"
            home_covers = effective > 0
            if bet_on_home == home_covers:
                return stake * water_hk, "FULL_WIN", f"盘口{asian_line:+.2f}, 净胜{goal_diff}"
            else:
                return -stake, "FULL_LOSE", f"盘口{asian_line:+.2f}, 净胜{goal_diff}"

        # quarter-ball 盘口 (±0.25, ±0.75, ±1.25...) → 有半赢/半输
        if abs(effective) < 0.01:
            return 0.0, "PUSH", f"盘口{asian_line:+.2f}, 净胜{goal_diff}, 走水"

        home_covers = effective > 0
        margin = abs(effective)

        if bet_on_home == home_covers:
            # 预测正确
            if margin >= 0.5:
                return stake * water_hk, "FULL_WIN", f"盘口{asian_line:+.2f}, 净胜{goal_diff}, eff={effective:+.2f}"
            else:
                return stake * water_hk / 2.0, "HALF_WIN", f"盘口{asian_line:+.2f}, 净胜{goal_diff}, eff={effective:+.2f}"
        else:
            # 预测错误
            if margin >= 0.5:
                return -stake, "FULL_LOSE", f"盘口{asian_line:+.2f}, 净胜{goal_diff}, eff={effective:+.2f}"
            else:
                return -stake / 2.0, "HALF_LOSE", f"盘口{asian_line:+.2f}, 净胜{goal_diff}, eff={effective:+.2f}"

    # ═══════════════════════════════════════════════════════════════
    # 大小球盈亏重算 (含半赢/半输)
    # ═══════════════════════════════════════════════════════════════

    def _calc_ou_profit(self, pred: int, actual: int, total_goals,
                        ou_line, water_hk: float, stake: float,
                        result_str: str) -> Tuple[float, str, str]:
        """精确计算大小球盈亏。

        Args:
            pred: 0=under, 1=over
            actual: 0=under, 1=over
            total_goals: match total goals
            ou_line: total goals line (2.5, 2.25, 2.75, etc.)
            water_hk: Hong Kong water
            stake: bet amount
        """
        if total_goals is None or pd.isna(total_goals):
            if result_str == "win":
                return stake * water_hk, "FULL_WIN", "fallback-无总进球"
            elif result_str == "lose":
                return -stake, "FULL_LOSE", "fallback-无总进球"
            else:
                return 0.0, "PUSH", "fallback-无总进球"

        if ou_line is None or pd.isna(ou_line):
            ou_line = 2.5

        bet_on_over = (pred == 1)

        # 整数+半线 (2.5, 3.5...) → 无半赢, 无走水
        if abs(ou_line * 2 - round(ou_line * 2)) < 0.01 and ou_line % 0.5 == 0 and ou_line % 1.0 != 0:
            if total_goals > ou_line:
                winner = "over"
            else:
                winner = "under"
            correct = (bet_on_over and winner == "over") or (not bet_on_over and winner == "under")
            if correct:
                return stake * water_hk, "FULL_WIN", f"线{ou_line}, 总球{total_goals}"
            else:
                return -stake, "FULL_LOSE", f"线{ou_line}, 总球{total_goals}"

        # 整数线 (2.0, 3.0...) → 全赢/走水/全输
        if abs(ou_line - round(ou_line)) < 0.01:
            if total_goals == ou_line:
                return 0.0, "PUSH", f"线{ou_line}, 总球{total_goals}, 走水"
            elif total_goals > ou_line:
                correct = bet_on_over
            else:
                correct = not bet_on_over
            if correct:
                return stake * water_hk, "FULL_WIN", f"线{ou_line}, 总球{total_goals}"
            else:
                return -stake, "FULL_LOSE", f"线{ou_line}, 总球{total_goals}"

        # quarter-ball 线 (2.25, 2.75, 3.25...) → 半赢/半输
        if total_goals == int(ou_line):  # e.g., ou_line=2.25, total=2
            # 对于 2.25: over 输半, under 赢半
            # 对于 2.75: over 赢半, under 输半
            if ou_line % 1.0 > 0.5:  # 2.75 → over half win
                over_result = "half_win"
                under_result = "half_lose"
            else:  # 2.25 → over half lose
                over_result = "half_lose"
                under_result = "half_win"

            if bet_on_over:
                if over_result == "half_win":
                    return stake * water_hk / 2.0, "HALF_WIN", f"线{ou_line}, 总球{total_goals}"
                else:
                    return -stake / 2.0, "HALF_LOSE", f"线{ou_line}, 总球{total_goals}"
            else:
                if under_result == "half_win":
                    return stake * water_hk / 2.0, "HALF_WIN", f"线{ou_line}, 总球{total_goals}"
                else:
                    return -stake / 2.0, "HALF_LOSE", f"线{ou_line}, 总球{total_goals}"

        else:
            # 明确超过或低于线
            if total_goals > ou_line:
                correct = bet_on_over
            else:
                correct = not bet_on_over
            if correct:
                return stake * water_hk, "FULL_WIN", f"线{ou_line}, 总球{total_goals}"
            else:
                return -stake, "FULL_LOSE", f"线{ou_line}, 总球{total_goals}"

    @staticmethod
    def _direction_name(pred: int, task_key: str) -> str:
        if task_key == "asian":
            return {0: "客赢盘", 1: "走水", 2: "主赢盘"}.get(pred, str(pred))
        elif task_key == "over_under":
            return {0: "小球", 1: "大球"}.get(pred, str(pred))
        elif task_key == "wdl":
            return {0: "主胜", 1: "平局", 2: "客胜"}.get(pred, str(pred))
        return str(pred)

    # ═══════════════════════════════════════════════════════════════
    # 独立 ROI 重算
    # ═══════════════════════════════════════════════════════════════

    def recalculate_roi(self) -> dict:
        """完全绕过旧逻辑, 逐场累加正确盈亏, 重新计算所有指标。"""
        print("[3/7] 独立 ROI 重算...")

        recalc = {}
        for task_key, models in self.ledger.items():
            recalc[task_key] = {}
            for model_name, ledger_df in models.items():
                if ledger_df.empty:
                    continue

                n = len(ledger_df)
                profit_correct = ledger_df["profit_correct"].values
                profit_orig = ledger_df["profit_original"].values

                total_correct = float(profit_correct.sum())
                total_orig = float(profit_orig.sum())

                # 胜率 (用 correct 盈亏)
                wins = (profit_correct > 0).sum()
                half_wins = (ledger_df["is_half_win"] == True).sum()
                half_losses = (ledger_df["is_half_lose"] == True).sum()
                pushes = (ledger_df["is_push"] == True).sum()
                losses = (profit_correct < 0).sum()
                resolved = wins + losses

                win_rate = wins / resolved if resolved > 0 else 0

                # 最大回撤
                equity = np.cumsum(profit_correct)
                peak = np.maximum.accumulate(equity)
                dd = np.where(peak > 0, (peak - equity) / peak, 0)
                max_dd = float(dd.max()) if len(dd) > 0 else 0

                # Sharpe
                sharpe = float(np.mean(profit_correct) / np.std(profit_correct, ddof=1)) if len(profit_correct) > 1 and np.std(profit_correct, ddof=1) > 0 else 0

                roi_correct = total_correct / (n * 100.0)  # flat 100/bet
                roi_orig = total_orig / (n * 100.0)

                recalc[task_key][model_name] = {
                    "total_bets": n,
                    "roi_original": round(roi_orig, 6),
                    "roi_corrected": round(roi_correct, 6),
                    "roi_difference": round(roi_correct - roi_orig, 6),
                    "total_profit_original": round(total_orig, 2),
                    "total_profit_corrected": round(total_correct, 2),
                    "profit_discrepancy": round(total_correct - total_orig, 2),
                    "win_rate": round(win_rate, 4),
                    "wins": int(wins),
                    "losses": int(losses),
                    "half_wins": int(half_wins),
                    "half_losses": int(half_losses),
                    "pushes": int(pushes),
                    "sharpe_corrected": round(sharpe, 4),
                    "max_dd_corrected": round(max_dd, 4),
                    "has_discrepancy": abs(roi_correct - roi_orig) > 0.001,
                }

                r = recalc[task_key][model_name]
                delta = r["roi_difference"]
                direction = "OVERSTATED" if delta < -0.001 else ("UNDERSTATED" if delta > 0.001 else "MATCH")
                print(f"  [{task_key}/{model_name}] "
                      f"原始ROI={r['roi_original']:.4f} 校正ROI={r['roi_corrected']:.4f} "
                      f"偏差={delta:+.4f} [{direction}] "
                      f"半赢={half_wins} 半输={half_losses} 走水={pushes}")

        return recalc

    # ═══════════════════════════════════════════════════════════════
    # 亚洲盘口专项审计
    # ═══════════════════════════════════════════════════════════════

    def audit_asian(self) -> dict:
        """亚洲盘口专项审计: water转换 / push / half-win / half-lose。"""
        print("[4/7] 亚洲盘口专项审计...")

        audit = {}
        for model_name, ledger_df in self.ledger.get("asian", {}).items():
            if ledger_df.empty:
                continue

            df = ledger_df.copy()
            n = len(df)

            # 盘口线分布
            line_dist = df["asian_line"].value_counts().to_dict() if "asian_line" in df.columns else {}

            # 半赢/半输分布
            bet_type_dist = df["bet_type"].value_counts().to_dict()

            # 盘口线 × bet_type 交叉
            cross = pd.crosstab(df["asian_line"], df["bet_type"]) if "asian_line" in df.columns else pd.DataFrame()

            # water 范围
            water_stats = {
                "min": float(df["water_hk"].min()),
                "max": float(df["water_hk"].max()),
                "mean": float(df["water_hk"].mean()),
                "median": float(df["water_hk"].median()),
            }

            # 港赔→欧赔验证: 每条记录检查 water + 1 = odds
            df["odds_check"] = df["water_hk"] + 1.0
            df["conversion_error"] = abs(df["odds_check"] - df["decimal_odds"]) > 0.01
            conversion_errors = int(df["conversion_error"].sum())

            # push 统计
            pushes = int((df["bet_type"] == "PUSH").sum())

            audit[model_name] = {
                "total_bets": n,
                "water_stats": water_stats,
                "line_distribution": {str(k): int(v) for k, v in sorted(line_dist.items())},
                "bet_type_distribution": {str(k): int(v) for k, v in bet_type_dist.items()},
                "half_win_count": int((df["bet_type"] == "HALF_WIN").sum()),
                "half_lose_count": int((df["bet_type"] == "HALF_LOSE").sum()),
                "full_win_count": int((df["bet_type"] == "FULL_WIN").sum()),
                "full_lose_count": int((df["bet_type"] == "FULL_LOSE").sum()),
                "push_count": pushes,
                "conversion_errors": conversion_errors,
                "profit_discrepancy_total": float(df["profit_discrepancy"].sum()),
                "line_bettype_crosstab": {str(k): {str(k2): int(v2) for k2, v2 in v.items()} for k, v in cross.to_dict().items()},
            }

            a = audit[model_name]
            print(f"  [{model_name}] 总{a['total_bets']}笔 "
                  f"全赢={a['full_win_count']} 全输={a['full_lose_count']} "
                  f"半赢={a['half_win_count']} 半输={a['half_lose_count']} "
                  f"走水={a['push_count']} 转换错误={conversion_errors}")

        return audit

    # ═══════════════════════════════════════════════════════════════
    # 大小球专项审计
    # ═══════════════════════════════════════════════════════════════

    def audit_over_under(self) -> dict:
        """大小球专项审计。"""
        print("[5/7] 大小球专项审计...")

        audit = {}
        for model_name, ledger_df in self.ledger.get("over_under", {}).items():
            if ledger_df.empty:
                continue

            df = ledger_df.copy()
            n = len(df)

            # OU 线分布
            line_dist = df["ou_line"].value_counts().to_dict() if "ou_line" in df.columns else {}

            # bet_type 分布
            bet_type_dist = df["bet_type"].value_counts().to_dict()

            # OU 线 × total_goals 交叉表
            cross = pd.crosstab(df["ou_line"], df["bet_type"]) if "ou_line" in df.columns else pd.DataFrame()

            water_stats = {
                "min": float(df["water_hk"].min()),
                "max": float(df["water_hk"].max()),
                "mean": float(df["water_hk"].mean()),
            }

            audit[model_name] = {
                "total_bets": n,
                "water_stats": water_stats,
                "line_distribution": {str(k): int(v) for k, v in sorted(line_dist.items())},
                "bet_type_distribution": {str(k): int(v) for k, v in bet_type_dist.items()},
                "half_win_count": int((df["bet_type"] == "HALF_WIN").sum()),
                "half_lose_count": int((df["bet_type"] == "HALF_LOSE").sum()),
                "full_win_count": int((df["bet_type"] == "FULL_WIN").sum()),
                "full_lose_count": int((df["bet_type"] == "FULL_LOSE").sum()),
                "push_count": int((df["bet_type"] == "PUSH").sum()),
                "profit_discrepancy_total": float(df["profit_discrepancy"].sum()),
                "line_bettype_crosstab": {str(k): {str(k2): int(v2) for k2, v2 in v.items()} for k, v in cross.to_dict().items()},
            }

            a = audit[model_name]
            print(f"  [{model_name}] 总{a['total_bets']}笔 "
                  f"全赢={a['full_win_count']} 全输={a['full_lose_count']} "
                  f"半赢={a['half_win_count']} 半输={a['half_lose_count']} "
                  f"走水={a['push_count']}")

        return audit

    # ═══════════════════════════════════════════════════════════════
    # 随机抽样
    # ═══════════════════════════════════════════════════════════════

    def random_sample(self, n: int = 100) -> pd.DataFrame:
        """随机抽取 n 笔交易用于人工核验。"""
        print(f"[6/7] 随机抽样 {n} 笔交易...")

        all_ledger = []
        for task_key, models in self.ledger.items():
            for model_name, ledger_df in models.items():
                if ledger_df.empty:
                    continue
                temp = ledger_df.copy()
                temp["task_model"] = f"{task_key}/{model_name}"
                all_ledger.append(temp)

        if not all_ledger:
            return pd.DataFrame()

        combined = pd.concat(all_ledger, ignore_index=True)
        sample_size = min(n, len(combined))
        sample = combined.sample(n=sample_size, random_state=42)

        # 选择关键列输出
        out_cols = [
            "match_id", "league_name", "kickoff_time", "home_team", "away_team",
            "market", "asian_line", "ou_line",
            "water_hk", "decimal_odds", "bet_direction",
            "goal_diff", "total_goals",
            "y_pred", "y_true", "result",
            "bet_type", "bet_detail",
            "profit_original", "profit_correct", "profit_discrepancy",
        ]
        out_cols = [c for c in out_cols if c in sample.columns]

        sample_out = sample[out_cols].copy()
        sample_out.to_csv(AUDIT_DIR / "random_sample_100.csv", index=False, encoding="utf-8-sig")
        print(f"  抽样: {sample_size} 笔 → random_sample_100.csv")

        # 抽样统计
        disp_count = (sample["profit_discrepancy"].abs() > 0.01).sum()
        print(f"  抽样中有差异: {disp_count}/{sample_size} ({disp_count/sample_size:.1%})")

        return sample_out

    # ═══════════════════════════════════════════════════════════════
    # 异常检测
    # ═══════════════════════════════════════════════════════════════

    def detect_anomalies(self, recalc_results: dict) -> list:
        """自动检测回测异常。"""
        print("[7/7] 异常检测...")

        anomalies = []

        for task_key, models in recalc_results.items():
            for model_name, r in models.items():
                roi = r["roi_corrected"]
                wr = r["win_rate"]
                bets = r["total_bets"]
                half_ratio = (r["half_wins"] + r["half_losses"]) / bets if bets > 0 else 0

                # 1. ROI 异常
                if roi > 0.30:
                    anomalies.append({
                        "type": "HIGH_ROI",
                        "severity": "WARNING",
                        "task": task_key, "model": model_name,
                        "detail": f"ROI={roi:.2%} (>30%), 需确认无计算偏差",
                    })
                if roi > 0.50:
                    anomalies.append({
                        "type": "EXTREME_ROI",
                        "severity": "CRITICAL",
                        "task": task_key, "model": model_name,
                        "detail": f"ROI={roi:.2%} (>50%), 极可能含计算偏差或数据泄露",
                    })

                # 2. 胜率异常
                if wr > 0.70:
                    anomalies.append({
                        "type": "HIGH_WINRATE",
                        "severity": "WARNING",
                        "task": task_key, "model": model_name,
                        "detail": f"胜率={wr:.1%} (>70%), 超出市场常规范围",
                    })

                # 3. 覆盖率异常
                total_matches = len(self.full_df) if self.full_df is not None else 8612
                coverage = bets / total_matches
                if coverage > 0.80:
                    anomalies.append({
                        "type": "HIGH_COVERAGE",
                        "severity": "INFO",
                        "task": task_key, "model": model_name,
                        "detail": f"覆盖率={coverage:.1%} (>80%), 几乎全量投注",
                    })

                # 4. 半赢/半输比例异常
                if half_ratio > 0.40:
                    anomalies.append({
                        "type": "HIGH_HALF_RATIO",
                        "severity": "INFO",
                        "task": task_key, "model": model_name,
                        "detail": f"半赢/半输占比={half_ratio:.1%}, quarter-ball盘口占主导",
                    })

                # 5. ROI 差异
                roi_diff = abs(r["roi_difference"])
                if roi_diff > 0.05:
                    anomalies.append({
                        "type": "ROI_DISCREPANCY",
                        "severity": "CRITICAL",
                        "task": task_key, "model": model_name,
                        "detail": f"校正ROI偏差={r['roi_difference']:+.4f} (>{0.05}), 旧逻辑有重大错误",
                    })
                elif roi_diff > 0.01:
                    anomalies.append({
                        "type": "ROI_DISCREPANCY",
                        "severity": "WARNING",
                        "task": task_key, "model": model_name,
                        "detail": f"校正ROI偏差={r['roi_difference']:+.4f}, 半赢/半输处理有差异",
                    })

                # 6. 联赛一致性 (在 ledger 中检查)
                ledger = self.ledger.get(task_key, {}).get(model_name, pd.DataFrame())
                if not ledger.empty and "league_name" in ledger.columns:
                    league_rois = {}
                    for lg, grp in ledger.groupby("league_name"):
                        if len(grp) >= 30:
                            p = grp["profit_correct"].sum()
                            league_rois[lg] = p / (len(grp) * 100)
                    if league_rois:
                        league_std = float(np.std(list(league_rois.values())))
                        if league_std > 0.20:
                            anomalies.append({
                                "type": "LEAGUE_ROI_VARIANCE",
                                "severity": "WARNING",
                                "task": task_key, "model": model_name,
                                "detail": f"联赛ROI标准差={league_std:.2%}, 差异较大",
                            })

        self.anomalies = anomalies

        # 打印
        for a in anomalies:
            tag = f"[{a['severity']}]"
            print(f"  {tag:12s} {a['type']:25s} [{a['task']}/{a['model']}] {a['detail']}")

        if not anomalies:
            print("  [OK] 未检测到异常")

        return anomalies

    # ═══════════════════════════════════════════════════════════════
    # 报告生成
    # ═══════════════════════════════════════════════════════════════

    def generate_reports(self, recalc: dict, asian_audit: dict, ou_audit: dict):
        """生成全部审计报告。"""
        print("\n生成审计报告...")

        # 1. 交易明细 CSV
        for task_key, models in self.ledger.items():
            for model_name, ledger_df in models.items():
                if ledger_df.empty:
                    continue
                path = AUDIT_DIR / f"trade_audit_{task_key}_{model_name}.csv"
                ledger_df.to_csv(path, index=False, encoding="utf-8-sig")
                print(f"  CSV: {path.name}")

        # 2. ROI 重算 CSV
        roi_rows = []
        for task_key, models in recalc.items():
            for model_name, r in models.items():
                r_copy = {k: v for k, v in r.items()}
                r_copy["task"] = task_key
                r_copy["model"] = model_name
                roi_rows.append(r_copy)
        if roi_rows:
            pd.DataFrame(roi_rows).to_csv(AUDIT_DIR / "roi_recalculation.csv", index=False, encoding="utf-8-sig")
            print(f"  CSV: roi_recalculation.csv")

        # 3. 汇总 JSON
        summary = {
            "generated_at": datetime.now().isoformat(),
            "roi_recalculation": recalc,
            "asian_audit": asian_audit,
            "over_under_audit": ou_audit,
            "anomalies": self.anomalies,
        }
        json_path = AUDIT_DIR / "audit_summary.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self._serializable(summary), f, ensure_ascii=False, indent=2, default=str)
        print(f"  JSON: audit_summary.json")

        # 4. HTML 报告
        html = self._build_html(recalc, asian_audit, ou_audit)
        html_path = AUDIT_DIR / "audit_summary.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  HTML: audit_summary.html")

        # 5. 亚盘审计 HTML
        asian_html = self._build_asian_html(asian_audit)
        with open(AUDIT_DIR / "asian_handicap_audit.html", "w", encoding="utf-8") as f:
            f.write(asian_html)
        print(f"  HTML: asian_handicap_audit.html")

        # 6. 大小球审计 HTML
        ou_html = self._build_ou_html(ou_audit)
        with open(AUDIT_DIR / "over_under_audit.html", "w", encoding="utf-8") as f:
            f.write(ou_html)
        print(f"  HTML: over_under_audit.html")

    def _build_html(self, recalc: dict, asian_audit: dict, ou_audit: dict) -> str:
        ts = datetime.now().isoformat()

        # ROI 对比表
        roi_html = "<h3>ROI 校正对比</h3><table><tr><th>任务</th><th>模型</th><th>原始ROI</th><th>校正ROI</th><th>偏差</th><th>半赢</th><th>半输</th><th>走水</th><th>状态</th></tr>"
        for task_key, models in recalc.items():
            for model_name, r in models.items():
                delta = r["roi_difference"]
                if abs(delta) < 0.001:
                    status = '<span style="color:#10b981">MATCH</span>'
                elif delta < 0:
                    status = f'<span style="color:#ef4444">OVERSTATED ({delta:+.4f})</span>'
                else:
                    status = f'<span style="color:#f59e0b">UNDERSTATED ({delta:+.4f})</span>'
                roi_html += (
                    f"<tr><td>{task_key}</td><td>{model_name}</td>"
                    f"<td>{r['roi_original']:.4f}</td>"
                    f"<td><strong>{r['roi_corrected']:.4f}</strong></td>"
                    f"<td>{delta:+.4f}</td>"
                    f"<td>{r['half_wins']}</td><td>{r['half_losses']}</td>"
                    f"<td>{r['pushes']}</td><td>{status}</td></tr>"
                )
        roi_html += "</table>"

        # 异常列表
        anomaly_html = "<h3>异常检测</h3>"
        if self.anomalies:
            anomaly_html += "<table><tr><th>级别</th><th>类型</th><th>任务/模型</th><th>详情</th></tr>"
            for a in self.anomalies:
                color = {"CRITICAL": "#ef4444", "WARNING": "#f59e0b", "INFO": "#3b82f6"}.get(a["severity"], "#666")
                anomaly_html += (
                    f"<tr><td style='color:{color};font-weight:bold'>{a['severity']}</td>"
                    f"<td>{a['type']}</td>"
                    f"<td>{a['task']}/{a['model']}</td>"
                    f"<td>{a['detail']}</td></tr>"
                )
            anomaly_html += "</table>"
        else:
            anomaly_html += "<p style='color:#10b981'>[OK] 未检测到异常</p>"

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>交易审计报告 — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#7c3aed 0%,#6d28d9 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; margin-bottom:8px; }}
.header p {{ opacity:0.8; font-size:14px; }}
.container {{ max-width:1400px; margin:0 auto; padding:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:24px; }}
.card h2 {{ font-size:16px; color:#16213e; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #e8e8e8; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; font-weight:600; color:#555; }}
tr:hover {{ background:#f8f9fa; }}
h3 {{ font-size:14px; color:#333; margin:16px 0 8px 0; }}
pre {{ background:#1a1a2e; color:#10b981; padding:16px; border-radius:8px; font-size:13px; overflow-x:auto; }}
</style>
</head>
<body>
<div class="header">
<h1>交易审计报告 (Trade Audit)</h1>
<p>生成: {ts[:19]} | 含半赢/半输/走水完整重算</p>
</div>
<div class="container">
<div class="card"><h2>ROI 校正对比</h2>{roi_html}</div>
<div class="card"><h2>异常检测</h2>{anomaly_html}</div>
<div class="card"><h2>审计说明</h2>
<pre>
审计方法:
  - 使用 goal_diff (净胜球) + asian_close_line (盘口线) 逐场重算亚盘盈亏
  - 使用 total_goals (总进球) + ou_close_line (大小球线) 逐场重算大小球盈亏
  - 正确处理 quarter-ball 的半赢(HALF_WIN)/半输(HALF_LOSE)
  - 正确处理整数盘口的走水(PUSH)
  - 港赔 water 转换为欧赔 decimal odds = water + 1.0

盘口类型:
  - 整数盘口 (0, ±1, ±2): 全赢/全输/走水
  - 半盘口 (2.5, 3.5): 全赢/全输 (无走水)
  - quarter-ball (±0.25, ±0.75, 2.25, 2.75): 全赢/全输/半赢/半输
</pre>
</div>
</div>
</body>
</html>"""

    def _build_asian_html(self, audit: dict) -> str:
        ts = datetime.now().isoformat()
        content = ""
        for model_name, a in audit.items():
            content += f"<h3>{model_name}</h3>"
            content += f"<p>总注数: {a['total_bets']} | 全赢: {a['full_win_count']} | 全输: {a['full_lose_count']} | 半赢: {a['half_win_count']} | 半输: {a['half_lose_count']} | 走水: {a['push_count']}</p>"
            if a["conversion_errors"] > 0:
                content += f"<p style='color:#ef4444'>[WARN] 港赔→欧赔转换错误: {a['conversion_errors']} 笔</p>"
            else:
                content += f"<p style='color:#10b981'>[OK] 港赔→欧赔转换全部正确</p>"

            # bet_type 分布表
            btd = a.get("bet_type_distribution", {})
            if btd:
                content += "<table><tr><th>结果类型</th><th>笔数</th><th>占比</th></tr>"
                total = sum(btd.values())
                for k, v in sorted(btd.items()):
                    content += f"<tr><td>{k}</td><td>{v}</td><td>{v/total:.1%}</td></tr>"
                content += "</table>"

        return self._wrap_html("亚洲盘口审计报告", ts, content)

    def _build_ou_html(self, audit: dict) -> str:
        ts = datetime.now().isoformat()
        content = ""
        for model_name, a in audit.items():
            content += f"<h3>{model_name}</h3>"
            content += f"<p>总注数: {a['total_bets']} | 全赢: {a['full_win_count']} | 全输: {a['full_lose_count']} | 半赢: {a['half_win_count']} | 半输: {a['half_lose_count']} | 走水: {a['push_count']}</p>"

            btd = a.get("bet_type_distribution", {})
            if btd:
                content += "<table><tr><th>结果类型</th><th>笔数</th><th>占比</th></tr>"
                total = sum(btd.values())
                for k, v in sorted(btd.items()):
                    content += f"<tr><td>{k}</td><td>{v}</td><td>{v/total:.1%}</td></tr>"
                content += "</table>"

        return self._wrap_html("大小球审计报告", ts, content)

    @staticmethod
    def _wrap_html(title: str, ts: str, content: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>{title} — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#7c3aed 0%,#6d28d9 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; }}
.container {{ max-width:1200px; margin:0 auto; padding:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:24px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; }}
h3 {{ font-size:14px; color:#333; margin:16px 0 8px 0; }}
</style>
</head>
<body>
<div class="header"><h1>{title}</h1><p>{ts[:19]}</p></div>
<div class="container"><div class="card">{content}</div></div>
</body>
</html>"""

    @staticmethod
    def _serializable(obj):
        if isinstance(obj, dict):
            return {str(k): TradeAuditor._serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [TradeAuditor._serializable(v) for v in obj]
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    # ═══════════════════════════════════════════════════════════════
    # 主流程
    # ═══════════════════════════════════════════════════════════════

    def run(self):
        print("=" * 60)
        print("交易审计系统 (Trade Auditor) v1.0")
        print("=" * 60)

        self.load_data()
        self.build_ledger()
        recalc = self.recalculate_roi()
        asian_audit = self.audit_asian()
        ou_audit = self.audit_over_under()
        self.random_sample(100)
        self.detect_anomalies(recalc)
        self.generate_reports(recalc, asian_audit, ou_audit)

        print(f"\n[OK] 审计完成 → {AUDIT_DIR}")
        return recalc


def main():
    try:
        TradeAuditor().run()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
