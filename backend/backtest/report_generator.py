#!/usr/bin/env python3
"""回测报告生成器 — 图表 + HTML + CSV + JSON"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


class ReportGenerator:
    """生成回测报告: 图表、HTML、CSV、JSON。"""

    def __init__(self, output_dir: str = "reports/backtest",
                 charts_dir: str = "reports/charts",
                 chart_dpi: int = 150):
        self.output_dir = Path(output_dir)
        self.charts_dir = Path(charts_dir)
        self.chart_dpi = chart_dpi
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.charts_dir.mkdir(parents=True, exist_ok=True)

    def generate_all(self, results: dict, bets_data: dict = None) -> dict:
        """生成所有报告。

        Args:
            results: 完整回测结果
            bets_data: {task_key: {model_name: DataFrame of bets}}

        Returns:
            报告路径字典
        """
        paths = {}

        # 图表
        paths["charts"] = self.generate_charts(results, bets_data)

        # JSON
        json_path = self.output_dir / "backtest_summary.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self._make_serializable(results), f, ensure_ascii=False, indent=2, default=str)
        paths["json"] = str(json_path)
        print(f"  JSON: {json_path}")

        # CSV
        if bets_data:
            csv_paths = self.generate_csv(bets_data)
            paths["csv"] = csv_paths

        # HTML
        html_path = self.output_dir / "backtest_report.html"
        html = self._build_html(results, paths.get("charts", {}))
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        paths["html"] = str(html_path)
        print(f"  HTML: {html_path}")

        return paths

    # ── 图表 ────────────────────────────────────────────────────

    def generate_charts(self, results: dict, bets_data: dict = None) -> dict:
        """生成全部 5 类图表。"""
        chart_paths = {}

        # 1. 资金曲线 + 回撤曲线 (合并)
        eq_paths = self._chart_equity_drawdown(results, bets_data)
        chart_paths["equity"] = eq_paths

        # 2. 月度收益热力图
        if bets_data:
            heatmap_paths = self._chart_monthly_heatmap(bets_data)
            chart_paths["monthly_heatmap"] = heatmap_paths

        # 3. 模型对比图
        cmp_path = self._chart_model_comparison(results)
        chart_paths["model_comparison"] = cmp_path

        # 4. 滚动 ROI 图
        if bets_data:
            roll_paths = self._chart_rolling_roi(bets_data)
            chart_paths["rolling_roi"] = roll_paths

        # 5. 年度收益分布
        yearly_paths = self._chart_yearly_returns(results, bets_data)
        chart_paths["yearly_returns"] = yearly_paths

        return chart_paths

    def _chart_equity_drawdown(self, results: dict, bets_data: dict) -> dict:
        """资金曲线 + 回撤曲线 (上下双图)。"""
        paths = {}

        for task_key, task_data in results.get("tasks", {}).items():
            task_name = task_data.get("name", task_key)
            models_data = task_data.get("models", {})

            n_models = len(models_data)
            if n_models == 0:
                continue

            fig, axes = plt.subplots(2, n_models, figsize=(6 * n_models, 10),
                                     squeeze=False)
            fig.suptitle(f"{task_name} — 资金曲线 & 回撤", fontsize=14, fontweight="bold")

            colors = {"xgboost": "#0f3460", "lightgbm": "#10b981", "catboost": "#f59e0b"}

            for col, (model_name, model_data) in enumerate(models_data.items()):
                color = colors.get(model_name, "#0f3460")
                ax_eq = axes[0, col]
                ax_dd = axes[1, col]

                # 资金曲线
                equity = model_data.get("aggregate_equity", [])
                if equity:
                    cum_eq = np.cumsum(equity)
                else:
                    cum_eq = np.array([0])
                    if bets_data and task_key in bets_data and model_name in bets_data[task_key]:
                        bd = bets_data[task_key][model_name]
                        if "profit" in bd.columns:
                            cum_eq = np.cumsum(bd["profit"].values)

                x = range(len(cum_eq))
                ax_eq.plot(x, cum_eq, linewidth=0.8, color=color)
                ax_eq.fill_between(x, 0, cum_eq, alpha=0.12, color=color)
                ax_eq.axhline(y=0, color="gray", linestyle="--", linewidth=0.5)

                # 标注
                agg = model_data.get("aggregate_metrics", {})
                roi = agg.get("roi", 0) if agg else 0
                sharpe = agg.get("sharpe_ratio", 0) if agg else 0
                ax_eq.set_title(f"{model_name} (ROI={roi:.4f}, Sharpe={sharpe:.2f})", fontsize=10)
                ax_eq.set_ylabel("累计盈亏")
                ax_eq.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))

                # 回撤曲线
                if len(cum_eq) > 0:
                    peak = np.maximum.accumulate(cum_eq)
                    dd = np.where(peak > 0, (peak - cum_eq) / peak, 0)
                    ax_dd.fill_between(x, 0, dd, color="#ef4444", alpha=0.3)
                    ax_dd.plot(x, dd, linewidth=0.5, color="#ef4444")
                    max_dd = dd.max()
                    ax_dd.set_title(f"最大回撤: {max_dd:.2%}", fontsize=10)
                ax_dd.set_ylabel("回撤 %")
                ax_dd.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
                ax_dd.set_xlabel("投注序号")
                ax_dd.invert_yaxis()

            plt.tight_layout()
            path = self.charts_dir / f"equity_dd_{task_key}.png"
            fig.savefig(path, dpi=self.chart_dpi, bbox_inches="tight")
            plt.close(fig)
            paths[task_key] = str(path)
            print(f"  资金曲线: {path}")

        return paths

    def _chart_monthly_heatmap(self, bets_data: dict) -> dict:
        """月度收益热力图。"""
        paths = {}

        for task_key, models in bets_data.items():
            for model_name, bd in models.items():
                if bd.empty or "kickoff_time" not in bd.columns:
                    continue

                df = bd.copy()
                df["month"] = pd.to_datetime(df["kickoff_time"], errors="coerce").dt.to_period("M")
                profit_col = "profit" if "profit" in df.columns else "return"
                monthly = df.groupby("month")[profit_col].sum()

                if len(monthly) < 2:
                    continue

                # 创建 pivot: year × month
                monthly_idx = monthly.index
                years = sorted(set(m.year for m in monthly_idx))
                months = range(1, 13)

                data = np.full((len(years), 12), np.nan)
                for i, (period, val) in enumerate(monthly.items()):
                    y_idx = years.index(period.year)
                    m_idx = period.month - 1
                    data[y_idx, m_idx] = val

                fig, ax = plt.subplots(figsize=(14, max(3, len(years) * 0.8)))
                month_labels = ["1月", "2月", "3月", "4月", "5月", "6月",
                                "7月", "8月", "9月", "10月", "11月", "12月"]

                cmap = sns.diverging_palette(10, 130, s=80, l=50, as_cmap=True)
                vmax = max(abs(np.nanmax(data)), abs(np.nanmin(data)), 1)
                sns.heatmap(data, annot=True, fmt=".1f", cmap=cmap,
                            center=0, vmin=-vmax, vmax=vmax,
                            xticklabels=month_labels, yticklabels=years,
                            ax=ax, cbar_kws={"label": "收益"}, linewidths=0.5)
                ax.set_title(f"{task_key} — {model_name} 月度收益热力图", fontsize=13, fontweight="bold")
                ax.set_ylabel("年份")
                ax.set_xlabel("月份")

                plt.tight_layout()
                path = self.charts_dir / f"heatmap_{task_key}_{model_name}.png"
                fig.savefig(path, dpi=self.chart_dpi, bbox_inches="tight")
                plt.close(fig)
                paths[f"{task_key}_{model_name}"] = str(path)
                print(f"  月度热力图: {path}")

        return paths

    def _chart_model_comparison(self, results: dict) -> str:
        """模型对比柱状图: ROI + Sharpe + MaxDD。"""
        tasks_data = results.get("tasks", {})
        if not tasks_data:
            return ""

        model_names = []
        roi_vals = []
        sharpe_vals = []
        maxdd_vals = []
        task_labels = []
        colors = []

        color_map = {"xgboost": "#0f3460", "lightgbm": "#10b981", "catboost": "#f59e0b"}

        for task_key, task_data in tasks_data.items():
            for model_name, model_data in task_data.get("models", {}).items():
                agg = model_data.get("aggregate_metrics", {})
                model_names.append(f"{task_key}\n{model_name}")
                roi_vals.append(agg.get("roi", 0))
                sharpe_vals.append(agg.get("sharpe_ratio", 0))
                maxdd_vals.append(agg.get("max_drawdown_pct", 0))
                task_labels.append(task_key)
                colors.append(color_map.get(model_name, "#888"))

        if not model_names:
            return ""

        fig, axes = plt.subplots(1, 3, figsize=(16, 5))
        x = range(len(model_names))

        # ROI
        axes[0].bar(x, roi_vals, color=colors, edgecolor="white", linewidth=0.5)
        axes[0].axhline(y=0, color="gray", linestyle="--", linewidth=0.5)
        axes[0].set_title("ROI", fontsize=12, fontweight="bold")
        axes[0].set_xticks(x)
        axes[0].set_xticklabels(model_names, fontsize=8)
        axes[0].yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
        for i, v in enumerate(roi_vals):
            axes[0].text(i, v + 0.002, f"{v:.2%}", ha="center", fontsize=7)

        # Sharpe
        axes[1].bar(x, sharpe_vals, color=colors, edgecolor="white", linewidth=0.5)
        axes[1].axhline(y=0, color="gray", linestyle="--", linewidth=0.5)
        axes[1].set_title("Sharpe Ratio", fontsize=12, fontweight="bold")
        axes[1].set_xticks(x)
        axes[1].set_xticklabels(model_names, fontsize=8)
        for i, v in enumerate(sharpe_vals):
            axes[1].text(i, v + 0.002, f"{v:.2f}", ha="center", fontsize=7)

        # Max Drawdown
        axes[2].bar(x, maxdd_vals, color=colors, edgecolor="white", linewidth=0.5)
        axes[2].set_title("Max Drawdown", fontsize=12, fontweight="bold")
        axes[2].set_xticks(x)
        axes[2].set_xticklabels(model_names, fontsize=8)
        axes[2].yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
        for i, v in enumerate(maxdd_vals):
            axes[2].text(i, v + 0.002, f"{v:.2%}", ha="center", fontsize=7)

        fig.suptitle("模型对比", fontsize=14, fontweight="bold")
        plt.tight_layout()

        path = self.charts_dir / "model_comparison.png"
        fig.savefig(path, dpi=self.chart_dpi, bbox_inches="tight")
        plt.close(fig)
        print(f"  模型对比图: {path}")
        return str(path)

    def _chart_rolling_roi(self, bets_data: dict, window: int = 50) -> dict:
        """滚动 ROI 图。"""
        paths = {}

        for task_key, models in bets_data.items():
            fig, ax = plt.subplots(figsize=(14, 5))
            colors = {"xgboost": "#0f3460", "lightgbm": "#10b981", "catboost": "#f59e0b"}

            for model_name, bd in models.items():
                if bd.empty:
                    continue
                profit_col = "profit" if "profit" in bd.columns else "return"
                returns = bd[profit_col].values
                if len(returns) < window:
                    continue

                cumsum = np.cumsum(returns)
                rolling = np.full(len(returns), np.nan)
                for i in range(window - 1, len(returns)):
                    seg = returns[i - window + 1 : i + 1]
                    rolling[i] = seg.sum() / window

                ax.plot(rolling, linewidth=0.8, color=colors.get(model_name),
                        label=f"{model_name}")

            ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.5)
            ax.set_title(f"{task_key} — 滚动 ROI (窗口={window}场)", fontsize=12, fontweight="bold")
            ax.set_xlabel("投注序号")
            ax.set_ylabel("滚动 ROI")
            ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
            ax.legend(fontsize=9)
            ax.grid(True, alpha=0.3)

            plt.tight_layout()
            path = self.charts_dir / f"rolling_roi_{task_key}.png"
            fig.savefig(path, dpi=self.chart_dpi, bbox_inches="tight")
            plt.close(fig)
            paths[task_key] = str(path)
            print(f"  滚动ROI: {path}")

        return paths

    def _chart_yearly_returns(self, results: dict, bets_data: dict) -> dict:
        """年度收益分布图。"""
        paths = {}

        for task_key, task_data in results.get("tasks", {}).items():
            task_name = task_data.get("name", task_key)
            yearly = task_data.get("yearly_aggregate", {})

            if not yearly and bets_data and task_key in bets_data:
                # 从 bets_data 计算
                for model_name, bd in bets_data[task_key].items():
                    if "kickoff_time" not in bd.columns:
                        continue
                    df = bd.copy()
                    df["year"] = pd.to_datetime(df["kickoff_time"], errors="coerce").dt.year
                    profit_col = "profit" if "profit" in df.columns else "return"
                    y_agg = df.groupby("year")[profit_col].sum()
                    yearly[model_name] = y_agg.to_dict()

            if not yearly:
                continue

            fig, ax = plt.subplots(figsize=(12, 5))
            colors = {"xgboost": "#0f3460", "lightgbm": "#10b981", "catboost": "#f59e0b"}

            all_years = sorted(set(y for m_data in yearly.values()
                                   for y in (m_data.keys() if isinstance(m_data, dict) else [])))
            if not all_years:
                plt.close(fig)
                continue

            x = np.arange(len(all_years))
            width = 0.25
            n_models = len(yearly)

            for i, (model_name, y_data) in enumerate(yearly.items()):
                if not isinstance(y_data, dict):
                    continue
                vals = [y_data.get(y, 0) for y in all_years]
                offset = (i - (n_models - 1) / 2) * width
                ax.bar(x + offset, vals, width, label=model_name,
                       color=colors.get(model_name, "#888"), edgecolor="white", linewidth=0.5)

            ax.set_xticks(x)
            ax.set_xticklabels([str(int(y)) for y in all_years])
            ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.5)
            ax.set_title(f"{task_name} — 年度收益", fontsize=12, fontweight="bold")
            ax.set_ylabel("总收益")
            ax.legend(fontsize=9)
            ax.grid(True, alpha=0.2, axis="y")

            plt.tight_layout()
            path = self.charts_dir / f"yearly_{task_key}.png"
            fig.savefig(path, dpi=self.chart_dpi, bbox_inches="tight")
            plt.close(fig)
            paths[task_key] = str(path)
            print(f"  年度收益: {path}")

        return paths

    # ── CSV 导出 ────────────────────────────────────────────────

    def generate_csv(self, bets_data: dict) -> dict:
        """导出投注明细 CSV。"""
        paths = {}
        for task_key, models in bets_data.items():
            for model_name, bd in models.items():
                if bd.empty:
                    continue
                path = self.output_dir / f"bets_{task_key}_{model_name}.csv"
                bd.to_csv(path, index=False, encoding="utf-8-sig")
                paths[f"{task_key}_{model_name}"] = str(path)
                print(f"  CSV: {path}")
        return paths

    # ── HTML 报告 ───────────────────────────────────────────────

    def _build_html(self, results: dict, chart_paths: dict) -> str:
        ts = results.get("generated_at", datetime.now().isoformat())
        config = results.get("config", {})

        # 摘要卡片
        summary_html = self._build_summary_cards(results)

        # 指标表格
        metrics_html = self._build_metrics_tables(results)

        # 窗口详情
        windows_html = self._build_windows_tables(results)

        # CLV 分析
        clv_html = self._build_clv_section(results)

        # 滑点分析
        slippage_html = self._build_slippage_section(results)

        # 图表
        charts_html = self._build_charts_html(chart_paths)

        # 配置信息
        config_html = self._build_config_section(config)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Walk Forward 回测报告 — {ts[:19]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#f0f2f5; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#0f3460 0%,#16213e 100%); color:white; padding:32px 48px; }}
.header h1 {{ font-size:24px; margin-bottom:8px; }}
.header p {{ opacity:0.8; font-size:14px; }}
.container {{ max-width:1400px; margin:0 auto; padding:24px; }}
.card {{ background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:24px; }}
.card h2 {{ font-size:16px; color:#16213e; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #e8e8e8; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #e8e8e8; }}
th {{ background:#f8f9fa; font-weight:600; color:#555; white-space:nowrap; }}
tr:hover {{ background:#f8f9fa; }}
h3 {{ font-size:14px; color:#333; margin:16px 0 8px 0; }}
img {{ max-width:100%; border-radius:8px; margin:8px 0; }}
.stat-grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:16px; }}
.stat-item {{ background:linear-gradient(135deg,#f8f9fa,#fff); border-radius:8px; padding:16px; text-align:center; border:1px solid #e8e8e8; }}
.stat-value {{ font-size:24px; font-weight:700; color:#0f3460; }}
.stat-value.green {{ color:#10b981; }}
.stat-value.red {{ color:#ef4444; }}
.stat-label {{ font-size:12px; color:#666; margin-top:4px; }}
.badge-ok {{ background:#d1fae5; color:#065f46; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }}
.badge-warn {{ background:#fef3c7; color:#92400e; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }}
.badge-bad {{ background:#fee2e2; color:#991b1b; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }}
.tabs {{ display:flex; gap:4px; margin-bottom:16px; flex-wrap:wrap; }}
.tab {{ padding:6px 16px; background:#e8e8e8; border-radius:6px 6px 0 0; cursor:pointer; font-size:13px; }}
.tab.active {{ background:#0f3460; color:white; }}
</style>
</head>
<body>
<div class="header">
<h1>Walk Forward 回测报告</h1>
<p>生成时间: {ts[:19]} | 窗口模式: {config.get('windows', {}).get('mode', 'N/A')} | 资金模式: {config.get('bankroll', {}).get('mode', 'N/A')}</p>
</div>
<div class="container">
<div class="card"><h2>策略摘要</h2>{summary_html}</div>
<div class="card"><h2>综合指标</h2>{metrics_html}</div>
<div class="card"><h2>图表分析</h2>{charts_html}</div>
<div class="card"><h2>各窗口详情</h2>{windows_html}</div>
{clv_html}
{slippage_html}
<div class="card"><h2>回测配置</h2>{config_html}</div>
</div>
</body>
</html>"""

    def _build_summary_cards(self, results: dict) -> str:
        rows = ""
        for task_key, task_data in results.get("tasks", {}).items():
            task_name = task_data.get("name", task_key)
            for model_name, model_data in task_data.get("models", {}).items():
                agg = model_data.get("aggregate_metrics", {})
                roi = agg.get("roi", 0)
                sharpe = agg.get("sharpe_ratio", 0)
                wr = agg.get("win_rate", 0)
                maxdd = agg.get("max_drawdown_pct", 0)
                bets = agg.get("total_bets", 0)
                pf = agg.get("profit_factor") or 0

                roi_cls = "green" if roi > 0 else "red"
                sharpe_cls = "green" if sharpe > 0.5 else ("red" if sharpe < 0 else "")
                dd_cls = "green" if maxdd < 0.15 else ("red" if maxdd > 0.30 else "")

                rows += f"""
<div style="margin-bottom:24px">
<h3>{task_name} — {model_name}</h3>
<div class="stat-grid">
<div class="stat-item"><div class="stat-value {roi_cls}">{roi:.2%}</div><div class="stat-label">ROI</div></div>
<div class="stat-item"><div class="stat-value">{sharpe:.2f}</div><div class="stat-label">Sharpe</div></div>
<div class="stat-item"><div class="stat-value">{wr:.1%}</div><div class="stat-label">胜率</div></div>
<div class="stat-item"><div class="stat-value {dd_cls}">{maxdd:.1%}</div><div class="stat-label">最大回撤</div></div>
<div class="stat-item"><div class="stat-value">{bets}</div><div class="stat-label">总投注</div></div>
<div class="stat-item"><div class="stat-value">{pf:.2f}</div><div class="stat-label">盈亏比</div></div>
</div></div>"""
        return rows

    def _build_metrics_tables(self, results: dict) -> str:
        html = ""
        for task_key, task_data in results.get("tasks", {}).items():
            task_name = task_data.get("name", task_key)
            html += f"<h3>{task_name}</h3><table><tr>"
            headers = ["模型", "投注数", "ROI", "胜率", "Sharpe", "Sortino", "Calmar",
                       "MaxDD%", "盈亏比", "波动率", "最长连胜", "最长连黑", "p值", "显著"]
            html += "".join(f"<th>{h}</th>" for h in headers) + "</tr>"

            for model_name, model_data in task_data.get("models", {}).items():
                m = model_data.get("aggregate_metrics", {})
                sig = m.get("significant", False)
                badge = '<span class="badge-ok">是</span>' if sig else '<span class="badge-warn">否</span>'
                html += (
                    f"<tr><td><strong>{model_name}</strong></td>"
                    f"<td>{m.get('total_bets', 0)}</td>"
                    f"<td>{self._fmt(m.get('roi'))}</td>"
                    f"<td>{self._fmt(m.get('win_rate'), pct=True)}</td>"
                    f"<td>{self._fmt(m.get('sharpe_ratio'))}</td>"
                    f"<td>{self._fmt(m.get('sortino_ratio'))}</td>"
                    f"<td>{self._fmt(m.get('calmar_ratio'))}</td>"
                    f"<td>{self._fmt(m.get('max_drawdown_pct'), pct=True)}</td>"
                    f"<td>{self._fmt(m.get('profit_factor'))}</td>"
                    f"<td>{self._fmt(m.get('volatility'))}</td>"
                    f"<td>{m.get('max_win_streak', '-')}</td>"
                    f"<td>{m.get('max_lose_streak', '-')}</td>"
                    f"<td>{self._fmt(m.get('p_value_one_sided'))}</td>"
                    f"<td>{badge}</td></tr>"
                )
            html += "</table>"
        return html

    def _build_windows_tables(self, results: dict) -> str:
        html = ""
        for task_key, task_data in results.get("tasks", {}).items():
            task_name = task_data.get("name", task_key)
            html += f"<h3>{task_name}</h3>"

            for model_name, model_data in task_data.get("models", {}).items():
                windows = model_data.get("windows", [])
                if not windows:
                    continue

                html += f"<h4 style='margin-top:12px'>{model_name}</h4><table><tr>"
                headers = ["窗口", "训练样本", "测试样本", "Accuracy", "AUC",
                           "ROI", "胜率", "Sharpe", "MaxDD", "投注数"]
                html += "".join(f"<th>{h}</th>" for h in headers) + "</tr>"

                for w in windows:
                    m = w.get("metrics", {})
                    html += (
                        f"<tr><td>{w.get('window', '-')}</td>"
                        f"<td>{w.get('train_samples', '-')}</td>"
                        f"<td>{w.get('test_samples', '-')}</td>"
                        f"<td>{self._fmt(w.get('accuracy'))}</td>"
                        f"<td>{self._fmt(w.get('auc'))}</td>"
                        f"<td>{self._fmt(m.get('roi'))}</td>"
                        f"<td>{self._fmt(m.get('win_rate'), pct=True)}</td>"
                        f"<td>{self._fmt(m.get('sharpe_ratio'))}</td>"
                        f"<td>{self._fmt(m.get('max_drawdown_pct'), pct=True)}</td>"
                        f"<td>{m.get('total_bets', '-')}</td></tr>"
                    )
                html += "</table>"
        return html

    def _build_clv_section(self, results: dict) -> str:
        clv_data = results.get("clv_analysis", {})
        if not clv_data:
            return ""

        html = '<div class="card"><h2>CLV 分析 (Closing Line Value)</h2>'
        for task_key, analysis in clv_data.items():
            if isinstance(analysis, dict) and "error" not in analysis:
                html += f"<h3>{task_key}</h3><table><tr>"
                for h in ["有效样本", "均值CLV", "中位数CLV", "正值率", "CLV标准差",
                          "t统计量", "p值(单侧)", "显著", "解读"]:
                    html += f"<th>{h}</th>"
                html += "</tr><tr>"
                sig = analysis.get("significant", False)
                badge = '<span class="badge-ok">是</span>' if sig else '<span class="badge-warn">否</span>'
                html += (
                    f"<td>{analysis.get('n_valid', 0)}</td>"
                    f"<td>{analysis.get('mean_clv', 0):.4f}</td>"
                    f"<td>{analysis.get('median_clv', 0):.4f}</td>"
                    f"<td>{analysis.get('positive_clv_rate', 0):.1%}</td>"
                    f"<td>{analysis.get('clv_std', 0):.4f}</td>"
                    f"<td>{analysis.get('t_statistic', 0)}</td>"
                    f"<td>{analysis.get('p_value_one_sided', 0)}</td>"
                    f"<td>{badge}</td>"
                    f"<td style='font-size:12px'>{analysis.get('interpretation', '-')}</td></tr>"
                )
                html += "</table>"

                # 分桶分析
                buckets = analysis.get("bucket_analysis", {})
                if buckets:
                    html += "<h4 style='margin-top:8px'>分桶分析</h4><table><tr>"
                    for h in ["区间", "样本数", "平均CLV", "胜率", "平均收益"]:
                        html += f"<th>{h}</th>"
                    html += "</tr>"
                    for label, b in buckets.items():
                        html += (
                            f"<tr><td>{label}</td>"
                            f"<td>{b.get('count', 0)}</td>"
                            f"<td>{b.get('avg_clv', 0):.4f}</td>"
                            f"<td>{self._fmt(b.get('win_rate'), pct=True)}</td>"
                            f"<td>{self._fmt(b.get('avg_return'))}</td></tr>"
                        )
                    html += "</table>"
        html += "</div>"
        return html

    def _build_slippage_section(self, results: dict) -> str:
        slip_data = results.get("slippage_analysis", {})
        if not slip_data:
            return ""

        html = '<div class="card"><h2>滑点模拟</h2>'
        for task_key, task_slip in slip_data.items():
            html += f"<h3>{task_key}</h3>"
            for model_name, levels in task_slip.items():
                if not isinstance(levels, dict):
                    continue
                html += f"<h4>{model_name}</h4><table><tr>"
                for h in ["滑点级别", "总收益", "ROI", "平均原始赔率", "平均滑点赔率", "投注数"]:
                    html += f"<th>{h}</th>"
                html += "</tr>"
                for level_key, lv in levels.items():
                    html += (
                        f"<tr><td>{lv.get('level_pct', level_key)}</td>"
                        f"<td>{self._fmt(lv.get('total_profit'))}</td>"
                        f"<td>{self._fmt(lv.get('roi'))}</td>"
                        f"<td>{self._fmt(lv.get('avg_odds_original'))}</td>"
                        f"<td>{self._fmt(lv.get('avg_odds_slipped'))}</td>"
                        f"<td>{lv.get('n_bets', '-')}</td></tr>"
                    )
                html += "</table>"

                be = task_slip.get(f"{model_name}_breakeven")
                if be is not None:
                    html += f"<p style='margin-top:4px;font-size:13px'>盈亏平衡滑点: <strong>{be:.1%}</strong></p>"

        html += "</div>"
        return html

    def _build_charts_html(self, chart_paths: dict) -> str:
        html = ""

        # 资金曲线
        eq = chart_paths.get("equity", {})
        for task_key, path in eq.items():
            fname = Path(path).name
            html += f"<img src='../charts/{fname}' alt='资金曲线 {task_key}' style='max-width:100%'>"

        # 模型对比
        cmp_path = chart_paths.get("model_comparison", "")
        if cmp_path:
            fname = Path(cmp_path).name
            html += f"<h3 style='margin-top:16px'>模型对比</h3><img src='../charts/{fname}' style='max-width:100%'>"

        # 滚动 ROI
        roll = chart_paths.get("rolling_roi", {})
        for task_key, path in roll.items():
            fname = Path(path).name
            html += f"<img src='../charts/{fname}' alt='滚动ROI {task_key}' style='max-width:100%'>"

        # 月度热力图
        heat = chart_paths.get("monthly_heatmap", {})
        for key, path in heat.items():
            fname = Path(path).name
            html += f"<img src='../charts/{fname}' alt='热力图 {key}' style='max-width:100%'>"

        # 年度收益
        yearly = chart_paths.get("yearly_returns", {})
        for task_key, path in yearly.items():
            fname = Path(path).name
            html += f"<img src='../charts/{fname}' alt='年度收益 {task_key}' style='max-width:100%'>"

        return html

    def _build_config_section(self, config: dict) -> str:
        if not config:
            return "<p>使用默认配置</p>"
        return f"<pre style='background:#f8f9fa;padding:16px;border-radius:8px;font-size:12px;overflow-x:auto'>{json.dumps(config, ensure_ascii=False, indent=2, default=str)}</pre>"

    @staticmethod
    def _fmt(val, pct=False):
        if val is None:
            return "-"
        if pct:
            return f"{val*100:.1f}%"
        if isinstance(val, float):
            return f"{val:.4f}"
        return str(val)

    @staticmethod
    def _make_serializable(obj):
        if isinstance(obj, dict):
            return {str(k): ReportGenerator._make_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [ReportGenerator._make_serializable(v) for v in obj]
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, pd.Timestamp):
            return str(obj)
        return obj
