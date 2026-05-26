# Football Data Crawler

职业级足球数据采集系统 —— 为 football-betting-analysis 主系统提供真实数据。

## 数据源

| 数据源 | 类型 | 状态 | 说明 |
|--------|------|------|------|
| Sofascore | 浏览器 + API | 正常 | XHR 拦截采集 |
| FotMob | 浏览器 + API | 正常 | XHR 拦截采集 |
| football-data.org | REST API | 需配置 | 官方数据，需 API Key |

## 快速开始

```bash
# 安装依赖
py -3 main.py --setup

# 单数据源采集
py -3 main.py --source sofascore --date today

# 全量采集
py -3 main.py --all --date today

# Web 管理界面
py -3 main.py --ui
```

## football-data.org 配置

### 申请 API Key

1. 访问 [football-data.org](https://www.football-data.org/client/register)
2. 注册免费账号
3. 在 Dashboard 复制 API Key

### 配置方式

**方式一：编辑 .env 文件**

```bash
FOOTBALL_DATA_ORG_API_KEY=你的API_KEY
```

**方式二：设置环境变量**

```bash
# Windows PowerShell
$env:FOOTBALL_DATA_ORG_API_KEY = "你的API_KEY"

# Linux / Mac
export FOOTBALL_DATA_ORG_API_KEY=你的API_KEY
```

### API 限额

| 套餐 | 每分钟 | 每日 | 价格 |
|------|--------|------|------|
| Free | 10 次 | 100 次 | 免费 |
| Tier 1 | 10 次 | 不限 | 付费 |

> 免费套餐已满足日常采集需求。系统已内置限速 (10 req/min)，确保不超限。

## 输出文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 原始数据 | `exports/matches_YYYY-MM-DD.json` | 按日期归档 |
| 原始 CSV | `exports/matches_YYYY-MM-DD.csv` | 按日期归档 |
| 今日快捷 | `exports/matches_today.json` / `.csv` | 当天数据 |
| 清洗数据 | `exports/clean_matches.json` / `.csv` | 去重合并后 |
| 采集摘要 | `exports/summary_YYYY-MM-DD.json` | 各数据源状态 |

## 项目结构

```
crawler/
├── core/           # 引擎、清洗、导出、限速、日志
├── browser/        # Playwright 浏览器管理、XHR 拦截
├── sources/        # 各数据源采集器
├── ui/             # Web 管理界面 (port 8080)
└── utils/          # 工具函数
configs/            # 系统配置、数据源配置
```

## CLI 参数

```
--source, -s     数据源: sofascore / fotmob / football-data
--all, -a        采集全部数据源
--date, -d       日期: today / yesterday / YYYY-MM-DD
--ui             启动 Web 管理界面
--ui-port        Web UI 端口, 默认 8080
--setup          运行初始化设置
--headless       无头模式: true / false
--output, -o     导出目录, 默认 exports
```

## 安全策略

- 自动限速，每个数据源独立限制
- 随机延迟 2-5 秒
- 失败自动重试 3 次
- 不绕过付费墙
- 不暴力请求
- 完整错误日志
