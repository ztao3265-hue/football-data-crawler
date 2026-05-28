#!/usr/bin/env python3
"""
每日自动推荐系统 — 主入口

用法:
    # 运行今日完整流水线 (扫描比赛 + 评估 + 生成推荐)
    python run_daily_pipeline.py

    # 指定日期
    python run_daily_pipeline.py --date 2026-05-28

    # 从外部 JSON 传入比赛数据
    python run_daily_pipeline.py --matches matches.json

    # 添加单场比赛
    python run_daily_pipeline.py --add-match

    # 查看今日摘要
    python run_daily_pipeline.py --summary

    # 启动 API 服务器 + 仪表盘
    python run_daily_pipeline.py --serve

    # 启动定时调度 (每30分钟运行一次)
    python run_daily_pipeline.py --schedule --interval 30

    # 查看引擎状态
    python run_daily_pipeline.py --status

    # 导出 JSON 报告
    python run_daily_pipeline.py --export report.json
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


def cmd_today(args):
    """运行今日完整流水线"""
    from backend.engine.daily_pipeline import DailyPipeline

    pipeline = DailyPipeline()

    # 如果指定了 matches JSON, 先加载
    matches = None
    if args.matches:
        with open(args.matches, "r", encoding="utf-8") as f:
            matches = json.load(f)
        print(f"从 {args.matches} 加载了 {len(matches)} 场比赛")

    target = args.date or datetime.now().strftime("%Y-%m-%d")
    report = pipeline.run(
        target_date=target,
        matches=matches,
        bankroll=args.bankroll,
        output_json=args.json,
    )

    if args.export:
        export_path = Path(args.export)
        export_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"报告已导出: {export_path}")

    return report


def cmd_add_match(args):
    """交互式添加比赛"""
    from backend.engine.daily_pipeline import DailyPipeline

    pipeline = DailyPipeline()

    print("\n=== 手动添加比赛 ===\n")
    matches = []

    while True:
        home = input("主队名称 (回车结束): ").strip()
        if not home:
            break
        away = input("客队名称: ").strip()
        league = input("联赛名称: ").strip()
        kickoff = input("开赛时间 (YYYY-MM-DD HH:MM): ").strip()

        try:
            odds_home = float(input("主胜赔率 [2.5]: ") or "2.5")
            odds_draw = float(input("平局赔率 [3.5]: ") or "3.5")
            odds_away = float(input("客胜赔率 [3.0]: ") or "3.0")
        except ValueError:
            print("赔率格式错误, 使用默认值")
            odds_home, odds_draw, odds_away = 2.5, 3.5, 3.0

        match = pipeline.add_manual_match(
            home_team=home,
            away_team=away,
            league=league,
            kickoff_time=kickoff,
            odds_home=odds_home,
            odds_draw=odds_draw,
            odds_away=odds_away,
        )
        matches.append(match)
        print(f"  已添加: {home} vs {away} ({league})\n")

    if matches:
        target = args.date or datetime.now().strftime("%Y-%m-%d")
        print(f"\n共添加 {len(matches)} 场比赛, 开始运行流水线...")
        pipeline.run(target_date=target, matches=matches, bankroll=args.bankroll, output_json=args.json)
    else:
        print("未添加任何比赛。")


def cmd_summary(args):
    """查看每日摘要"""
    from backend.engine.recommendation_engine import UnifiedRecommendationEngine

    engine = UnifiedRecommendationEngine()
    target = args.date or datetime.now().strftime("%Y-%m-%d")
    summary = engine.get_daily_summary(target)

    if summary.get("total", 0) == 0:
        print(f"\n{target}: 暂无推荐")
        return

    print(f"\n{'='*60}")
    print(f"  每日推荐摘要 — {target}")
    print(f"{'='*60}")
    print(f"  总推荐:         {summary['total']}")
    print(f"  强烈推荐:       {summary['strong_buy']}")
    print(f"  Top5 精选:      {summary['top5']}")
    print(f"  低风险:         {summary['low_risk']}")
    print(f"  高EV:           {summary['high_ev']}")
    print(f"  最强精选:       {summary['strongest']}")
    print(f"  平均EV:         {summary['average_ev']:.2%}")
    print(f"  平均置信度:     {summary['average_confidence']:.2%}")
    print(f"  Steam Move:     {summary['steam_move_alerts']}")
    print(f"  Sharp Money:    {summary['sharp_money_alerts']}")
    print(f"{'='*60}\n")

    # 显示 Top5
    top5 = engine.get_top5(target)
    if top5:
        print("  [Top5 推荐]")
        for i, r in enumerate(top5, 1):
            print(
                f"  {i}. {r.get('league', '?'):20s} "
                f"{r.get('home_team', '?'):12s} vs {r.get('away_team', '?'):12s} "
                f"| {r.get('pick', '?'):10s} "
                f"| EV={r.get('ev', 0):.2%} "
                f"| 风险={r.get('risk_level', '?')}"
            )


def cmd_serve(args):
    """启动 API 服务器"""
    from crawler.api.full_server import start_server

    print(f"\n启动 API 服务器: http://0.0.0.0:{args.port}")
    print(f"仪表盘:          http://localhost:{args.port}/dashboard")
    print(f"API 文档:        http://localhost:{args.port}/docs")
    print(f"健康检查:        http://localhost:{args.port}/api/v1/health\n")

    start_server(port=args.port, reload=args.reload)


def cmd_schedule(args):
    """定时调度模式"""
    from backend.engine.daily_pipeline import DailyPipeline

    interval_minutes = args.interval
    print(f"\n定时调度模式: 每 {interval_minutes} 分钟运行一次")
    print("按 Ctrl+C 停止\n")

    pipeline = DailyPipeline()
    iteration = 0

    try:
        while True:
            iteration += 1
            now = datetime.now()
            print(f"\n{'─'*50}")
            print(f"[#{iteration}] {now.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'─'*50}")

            try:
                report = pipeline.run(target_date=now.strftime("%Y-%m-%d"))
                s = report.get("summary", {})
                print(f"  → 生成 {s.get('total_recommendations', 0)} 条推荐 "
                      f"(强烈={s.get('strong_buy', 0)}, Top5={s.get('top5', 0)})")
            except Exception as e:
                print(f"  [ERROR] 流水线执行失败: {e}")

            print(f"\n等待 {interval_minutes} 分钟...")
            time.sleep(interval_minutes * 60)

    except KeyboardInterrupt:
        print(f"\n\n调度器已停止。共运行 {iteration} 轮。")


def cmd_status(args):
    """查看引擎状态"""
    from backend.engine.recommendation_engine import UnifiedRecommendationEngine

    engine = UnifiedRecommendationEngine()
    status = engine.get_engine_status()

    print(f"\n{'='*50}")
    print("  推荐引擎状态")
    print(f"{'='*50}")
    print(f"  ML模型可用:  {status['ml_models_available']}")
    if status['ml_models_available']:
        print(f"  已加载模型:")
        for task, models in status['ml_models'].items():
            if models:
                print(f"    [{task}] {', '.join(models)}")
    else:
        print(f"  (使用规则引擎 fallback)")
    print(f"  数据库:      {status['database']}")
    print(f"  过滤配置:    EV>={status['filter_config']['min_ev']}, "
          f"Conf>={status['filter_config']['min_confidence']}, "
          f"Max/day={status['filter_config']['max_per_day']}")
    print(f"{'='*50}\n")


def cmd_history(args):
    """查看历史记录"""
    from backend.engine.recommendation_engine import UnifiedRecommendationEngine

    engine = UnifiedRecommendationEngine()
    history = engine.get_history(
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
    )

    print(f"\n历史推荐 ({len(history)} 条):")
    print(f"{'─'*80}")
    for r in history[:20]:
        print(
            f"  {r.get('date', '?'):12s} | {r.get('league', '?'):18s} | "
            f"{r.get('home_team', '?'):10s} vs {r.get('away_team', '?'):10s} | "
            f"{r.get('pick', '?'):8s} | EV={r.get('ev', 0):.2%} | "
            f"风险={r.get('risk_level', '?')}"
        )

    if len(history) > 20:
        print(f"  ... 还有 {len(history) - 20} 条")

    # 统计
    stats = engine.get_history_stats(args.stats_days)
    print(f"\n近{args.stats_days}天统计:")
    print(f"  总推荐: {stats['total_recommendations']} | 活跃天: {stats['active_days']}")
    print(f"  日均: {stats['avg_daily_count']} | 平均EV: {stats['avg_ev']:.2%}")
    print(f"  最高EV: {stats['max_ev']:.2%}\n")


# ── CLI 主入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Football AI 每日自动推荐系统 v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_daily_pipeline.py                        # 运行今日流水线
  python run_daily_pipeline.py --date 2026-05-28      # 指定日期
  python run_daily_pipeline.py --summary              # 查看今日摘要
  python run_daily_pipeline.py --status               # 引擎状态
  python run_daily_pipeline.py --serve --port 8080    # 启动API
  python run_daily_pipeline.py --schedule --interval 30  # 定时调度
  python run_daily_pipeline.py --add-match            # 手动添加比赛
        """,
    )

    # 主命令
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD")
    parser.add_argument("--bankroll", type=float, default=10000.0, help="资金量")
    parser.add_argument("--json", action="store_true", help="输出JSON到stdout")

    # 模式选择
    parser.add_argument("--summary", action="store_true", help="查看每日摘要")
    parser.add_argument("--status", action="store_true", help="查看引擎状态")
    parser.add_argument("--history", action="store_true", help="查看历史记录")
    parser.add_argument("--serve", action="store_true", help="启动 API 服务器")
    parser.add_argument("--schedule", action="store_true", help="定时调度模式")
    parser.add_argument("--add-match", action="store_true", help="手动添加比赛")

    # 参数
    parser.add_argument("--matches", type=str, default=None, help="比赛JSON文件路径")
    parser.add_argument("--export", type=str, default=None, help="导出报告路径")
    parser.add_argument("--port", type=int, default=8000, help="API服务器端口")
    parser.add_argument("--reload", action="store_true", help="API热重载")
    parser.add_argument("--interval", type=int, default=30, help="调度间隔 (分钟)")
    parser.add_argument("--start-date", type=str, default=None, help="历史起始日期")
    parser.add_argument("--end-date", type=str, default=None, help="历史结束日期")
    parser.add_argument("--limit", type=int, default=100, help="历史记录上限")
    parser.add_argument("--stats-days", type=int, default=30, help="历史统计天数")

    args = parser.parse_args()

    # 路由到对应命令
    if args.add_match:
        cmd_add_match(args)
    elif args.status:
        cmd_status(args)
    elif args.summary:
        cmd_summary(args)
    elif args.history:
        cmd_history(args)
    elif args.serve:
        cmd_serve(args)
    elif args.schedule:
        cmd_schedule(args)
    else:
        # 默认: 运行今日流水线
        cmd_today(args)


if __name__ == "__main__":
    main()
