"""
市场分析器 — Steam Move / Sharp Money / CLV 检测
"""
from datetime import datetime, timedelta
from typing import Any, Optional


class MarketAnalyzer:
    """
    市场微观结构分析器

    检测三种关键信号:
    - Steam Move: 短时间内的剧烈赔率变动
    - Sharp Money: 聪明钱流向 (赔率与基本面背离)
    - CLV: Closing Line Value (收盘线价值)
    """

    # 阈值配置
    STEAM_MOVE_THRESHOLD = 0.08       # 15分钟内赔率变动 > 8% 视为 Steam Move
    STEAM_WINDOW_MINUTES = 15
    SHARP_MONEY_OVERROUND_THRESHOLD = 0.03  # 市场溢价 < 3% (高流动性市场)
    CLV_MIN_EDGE = 0.02               # CLV 至少 2% 才算有价值

    def __init__(self):
        self._odds_history: dict[str, list[dict]] = {}

    def feed_odds_snapshot(self, match_id: str, odds_data: dict[str, Any]):
        """喂入一次赔率快照"""
        if match_id not in self._odds_history:
            self._odds_history[match_id] = []
        self._odds_history[match_id].append({
            "timestamp": datetime.now(),
            "data": odds_data,
        })
        # 只保留最近 2 小时
        cutoff = datetime.now() - timedelta(hours=2)
        self._odds_history[match_id] = [
            s for s in self._odds_history[match_id]
            if s["timestamp"] > cutoff
        ]

    # ── Steam Move 检测 ──────────────────────────────────────────

    def detect_steam_move(self, match_id: str) -> dict[str, Any]:
        """
        检测 Steam Move (蒸汽移动)

        Steam Move = 短时间内多家博彩公司同时急剧调整赔率
        通常意味着内幕消息或大资金入场
        """
        history = self._odds_history.get(match_id, [])
        if len(history) < 2:
            return {"detected": False, "reason": "数据不足"}

        now = datetime.now()
        recent = [
            s for s in history
            if (now - s["timestamp"]).total_seconds() <= self.STEAM_WINDOW_MINUTES * 60
        ]

        if len(recent) < 2:
            return {"detected": False, "reason": "近期快照不足"}

        oldest = recent[0]["data"]
        newest = recent[-1]["data"]

        signals = []

        # 检测欧赔变动
        for key, label in [("home_win", "主胜"), ("draw", "平局"), ("away_win", "客胜")]:
            old_val = float(oldest.get(key, 0))
            new_val = float(newest.get(key, 0))
            if old_val > 1.0 and new_val > 1.0:
                change_pct = abs(new_val - old_val) / old_val
                if change_pct >= self.STEAM_MOVE_THRESHOLD:
                    direction = "下降" if new_val < old_val else "上升"
                    signals.append({
                        "type": "steam_move",
                        "market": label,
                        "change_pct": round(change_pct * 100, 1),
                        "direction": direction,
                        "old_odds": round(old_val, 3),
                        "new_odds": round(new_val, 3),
                    })

        # 检测亚盘变动
        old_asian = oldest.get("asian", {})
        new_asian = newest.get("asian", {})
        old_line = float(old_asian.get("handicap", 0))
        new_line = float(new_asian.get("handicap", 0))
        if abs(new_line - old_line) >= 0.25:
            signals.append({
                "type": "steam_move",
                "market": "亚盘",
                "change_pct": round(abs(new_line - old_line) * 100, 1),
                "direction": f"{old_line} → {new_line}",
                "old_odds": old_line,
                "new_odds": new_line,
            })

        # 检测大小球变动
        old_ou = oldest.get("over_under", {})
        new_ou = newest.get("over_under", {})
        old_ou_line = float(old_ou.get("line", 2.5))
        new_ou_line = float(new_ou.get("line", 2.5))
        if abs(new_ou_line - old_ou_line) >= 0.25:
            signals.append({
                "type": "steam_move",
                "market": "大小球",
                "change_pct": round(abs(new_ou_line - old_ou_line) / max(old_ou_line, 0.1) * 100, 1),
                "direction": f"{old_ou_line} → {new_ou_line}",
                "old_odds": old_ou_line,
                "new_odds": new_ou_line,
            })

        return {
            "detected": len(signals) > 0,
            "signals": signals,
            "signal_count": len(signals),
            "severity": "high" if len(signals) >= 2 else ("medium" if len(signals) == 1 else "none"),
        }

    # ── Sharp Money 检测 ─────────────────────────────────────────

    def detect_sharp_money(self, odds_data: dict[str, Any]) -> dict[str, Any]:
        """
        检测 Sharp Money (聪明钱)

        Sharp Money 特征:
        1. 低市场溢价 (高流动性市场, sharp 才会进场)
        2. 赔率与公开预期背离 (博彩公司赔率向非热门方向移动)
        3. 亚盘水位异常 (某一侧水位持续走低)
        """
        signals = []

        home = float(odds_data.get("home_win", 2.5))
        draw = float(odds_data.get("draw", 3.5))
        away = float(odds_data.get("away_win", 3.0))

        total_implied = 1 / home + 1 / draw + 1 / away
        overround = total_implied - 1.0

        # 信号1: 低市场溢价 → 流动性好，sharp money 常在此类市场行动
        if overround <= self.SHARP_MONEY_OVERROUND_THRESHOLD:
            signals.append({
                "type": "sharp_money",
                "indicator": "低市场溢价",
                "value": round(overround * 100, 2),
                "description": f"市场溢价仅 {overround:.2%}, 高流动性市场",
            })

        # 信号2: 赔率方向背离 (开赔后赔率向非热门方向移动)
        open_home = float(odds_data.get("open_home_win", home))
        open_away = float(odds_data.get("open_away_win", away))

        if open_home > 0 and open_away > 0:
            home_change = home - open_home
            away_change = away - open_away

            # 如果主队是热门(低赔), 但主胜赔率上升而客胜赔率下降
            if home < away and home_change > 0 and away_change < 0:
                signals.append({
                    "type": "sharp_money",
                    "indicator": "赔率背离",
                    "value": round(abs(home_change) + abs(away_change), 3),
                    "description": "热门方向赔率走弱, 资金流向冷门方向",
                })
            elif away < home and away_change > 0 and home_change < 0:
                signals.append({
                    "type": "sharp_money",
                    "indicator": "赔率背离",
                    "value": round(abs(home_change) + abs(away_change), 3),
                    "description": "热门方向赔率走弱, 资金流向冷门方向",
                })

        # 信号3: 亚盘水位异常
        asian = odds_data.get("asian", {})
        home_water = float(asian.get("home_odds", 0.9))
        away_water = float(asian.get("away_odds", 0.9))
        water_diff = abs(home_water - away_water)

        if water_diff >= 0.15:
            sharp_side = "主队" if home_water < away_water else "客队"
            signals.append({
                "type": "sharp_money",
                "indicator": "亚盘水位异常",
                "value": round(water_diff, 3),
                "description": f"{sharp_side}水位持续走低, 疑似聪明钱流入",
            })

        return {
            "detected": len(signals) > 0,
            "signals": signals,
            "signal_count": len(signals),
            "confidence": "high" if len(signals) >= 2 else ("medium" if len(signals) == 1 else "low"),
        }

    # ── CLV 分析 ─────────────────────────────────────────────────

    def analyze_clv(
        self,
        match_id: str,
        bet_type: str,
        bet_odds: float,
        closing_odds: Optional[float] = None
    ) -> dict[str, Any]:
        """
        Closing Line Value 分析

        CLV = (下注赔率 - 收盘赔率) / 下注赔率
        正 CLV 意味着你拿到比收盘更好的赔率 → +EV
        """
        if closing_odds is None:
            history = self._odds_history.get(match_id, [])
            if history:
                closing_odds = float(history[-1]["data"].get(bet_type, bet_odds))
            else:
                closing_odds = bet_odds

        if bet_odds <= 0:
            return {"clv": 0.0, "edge": "none", "assessment": "无数据"}

        clv = round((bet_odds - closing_odds) / bet_odds, 4)

        if clv >= self.CLV_MIN_EDGE:
            edge = "positive"
            assessment = f"获得 {clv:.1%} 正CLV, 下注时赔率优于收盘"
        elif clv <= -self.CLV_MIN_EDGE:
            edge = "negative"
            assessment = f"{abs(clv):.1%} 负CLV, 收盘赔率更优, 下注时机不佳"
        else:
            edge = "neutral"
            assessment = "CLV接近零, 赔率基本稳定"

        return {
            "clv": clv,
            "edge": edge,
            "bet_odds": bet_odds,
            "closing_odds": closing_odds,
            "assessment": assessment,
        }

    # ── 综合分析 ─────────────────────────────────────────────────

    def full_analysis(self, match_id: str, odds_data: dict[str, Any]) -> dict[str, Any]:
        """对一场比赛执行完整市场分析"""
        steam = self.detect_steam_move(match_id)
        sharp = self.detect_sharp_money(odds_data)

        # 综合评分
        score = 0
        warnings_list = []

        if steam["detected"]:
            score += steam["signal_count"] * 2
            for s in steam["signals"]:
                warnings_list.append(f"Steam Move: {s['market']} {s['direction']} ({s['change_pct']}%)")

        if sharp["detected"]:
            score += sharp["signal_count"] * 3
            for s in sharp["signals"]:
                warnings_list.append(f"Sharp Money: {s['indicator']} — {s['description']}")

        # 风险标签
        if score >= 6:
            market_risk = "high_alert"
            risk_note = "多个市场异常信号, 谨慎跟进"
        elif score >= 3:
            market_risk = "notable"
            risk_note = "存在值得关注的市场信号"
        else:
            market_risk = "normal"
            risk_note = "市场信号正常"

        return {
            "match_id": match_id,
            "steam_move": steam,
            "sharp_money": sharp,
            "market_score": score,
            "market_risk": market_risk,
            "risk_note": risk_note,
            "warnings": warnings_list,
        }
