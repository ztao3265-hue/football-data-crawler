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
  tools/         数据质量验证
  features/      特征工程引擎
  models/        模型训练系统
  backtest/      Walk Forward 回测
crawler/        爬虫系统
data/           数据 (不提交)
datasets/       生成数据集 (不提交)
models/         训练好的模型 (不提交)
reports/        报告文件 (提交)
configs/        配置文件
```
