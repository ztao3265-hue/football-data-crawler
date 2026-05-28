# CLAUDE.md

本项目是足球数据采集 & AI 预测系统 (football-data-crawler)。

## 自动同步规则

**每次代码开发完成后，必须自动执行：**

```bash
git add -A
git commit -m "<描述性commit message>"
git push
```

不要等用户提醒，完成后立即提交推送。

## 安全机制

提交前自动检查：
- 不提交 >1MB 的单个文件（模型 .pkl、数据库 .db 已在 .gitignore 排除）
- 不提交 node_modules、cache、logs、临时文件
- 不提交 .env、credentials、密钥文件
- 所有生成数据 (datasets/, models/, data/raw/, data/processed/) 由 .gitignore 保护

## 提交风格

使用中文 commit message，格式：
```
feat: <简短描述> — <关键亮点>
```

## 项目结构

```
backend/
  engine/        统一推荐引擎 (每日自动推荐)
  tools/         数据质量验证
  features/      特征工程引擎
  models/        模型训练系统
  backtest/      Walk Forward 回测
  execution/     执行层 (资金管理/推荐生成/追踪)
  live/          实时预测 & 赔率采集
  market/        市场微观结构分析
crawler/        爬虫系统
  api/           REST API (含推荐接口)
  ui/templates/  前端仪表盘
data/           数据 (不提交)
datasets/       生成数据集 (不提交)
models/         训练好的模型 (不提交)
reports/        报告文件 (提交)
configs/        配置文件
```

## 每日推荐系统

入口: `python run_daily_pipeline.py`

```bash
python run_daily_pipeline.py                        # 运行今日完整流水线
python run_daily_pipeline.py --summary              # 查看今日摘要
python run_daily_pipeline.py --status               # 引擎状态
python run_daily_pipeline.py --serve --port 8080    # 启动API+仪表盘
python run_daily_pipeline.py --schedule --interval 30  # 定时调度
python run_daily_pipeline.py --add-match            # 手动添加比赛
python run_daily_pipeline.py --history              # 查看历史
```

流水线流程: 扫描比赛 → 采集赔率 → ML预测 → 市场分析 → 推荐生成 → 去重风控 → 保存DB

API端点:
- GET /api/v1/recommendations/today — 今日推荐
- GET /api/v1/recommendations/top5 — Top5精选
- GET /api/v1/recommendations/low-risk — 低风险
- GET /api/v1/recommendations/high-ev — 高EV
- GET /api/v1/recommendations/summary — 每日摘要
- GET /api/v1/recommendations/history — 历史追踪
- POST /api/v1/recommendations/pipeline/run — 触发流水线
- GET /dashboard — 前端仪表盘
