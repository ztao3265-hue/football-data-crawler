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
├── database/       # PostgreSQL 连接、ORM、导入器
├── ui/             # Web 管理界面 (port 8080)
└── utils/          # 工具函数
configs/            # 系统配置、数据源配置
```

## PostgreSQL 数据库

### 环境要求

- PostgreSQL 12+
- 创建数据库: `CREATE DATABASE football_crawler;`

### 连接配置

编辑 `.env` 文件：

```bash
# 方式一：完整连接字符串（推荐）
DATABASE_URL=postgresql://用户名:密码@主机:5432/football_crawler

# 方式二：逐个字段配置
DB_HOST=localhost
DB_PORT=5432
DB_NAME=football_crawler
DB_USER=postgres
DB_PASSWORD=你的密码
```

> DATABASE_URL 优先级高于逐个字段配置。

### 数据库 CLI 命令

```bash
# 测试连接
py -3 main.py --db-test

# 初始化表结构
py -3 main.py --db-init

# 导入 clean_matches.json 到数据库
py -3 main.py --db-import

# 指定文件导入
py -3 main.py --db-import exports/clean_matches_2026-05-26.json

# 查看数据库统计
py -3 main.py --db-status
```

### 数据库结构

```
football_crawler
├── leagues (联赛表)
│   ├── id          VARCHAR(8)   PK  (MD5 hash)
│   ├── name        VARCHAR(255)
│   ├── country     VARCHAR(100)
│   ├── source      VARCHAR(50)
│   └── created_at  TIMESTAMP
│
├── teams (球队表)
│   ├── id          VARCHAR(12)  PK  (MD5 hash)
│   ├── name        VARCHAR(255)
│   ├── country     VARCHAR(100)
│   ├── league_id   VARCHAR(8)   FK → leagues.id
│   └── created_at  TIMESTAMP
│
├── matches (比赛表)
│   ├── id            BIGSERIAL    PK
│   ├── match_id      VARCHAR(16)  UNIQUE INDEX
│   ├── source        VARCHAR(50)
│   ├── league_id     VARCHAR(8)   FK → leagues.id
│   ├── league_name   VARCHAR(255)
│   ├── home_team_id  VARCHAR(12)  FK → teams.id
│   ├── home_team     VARCHAR(255)
│   ├── away_team_id  VARCHAR(12)  FK → teams.id
│   ├── away_team     VARCHAR(255)
│   ├── kickoff_time  TIMESTAMP
│   ├── home_score    INT
│   ├── away_score    INT
│   ├── score_display VARCHAR(20)
│   ├── status        VARCHAR(20)  (scheduled/live/finished)
│   ├── is_archived   BOOLEAN
│   ├── collected_at  TIMESTAMP
│   └── created_at    TIMESTAMP
│
└── odds (赔率表)
    ├── id            BIGSERIAL   PK
    ├── match_id      VARCHAR(16) FK → matches.match_id
    ├── source        VARCHAR(50)
    ├── bookmaker     VARCHAR(100)
    ├── odds_home     FLOAT
    ├── odds_draw     FLOAT
    ├── odds_away     FLOAT
    ├── asian_handicap VARCHAR(50)
    ├── over_under    VARCHAR(50)
    └── collected_at  TIMESTAMP
```

### 重复检测机制

`match_id` 由 `source + home_team + away_team + kickoff_date` 经过 SHA256 生成：

```
match_id = SHA256("sofascore|man united|liverpool|2026-05-26")[:16]
```

- 同来源、同球队、同日期 → 相同 match_id → **更新**已有记录（补全比分/赔率）
- 不同来源 → 不同 match_id → **新增**记录
- 采集 `--all` 时自动清洗去重后入库

### 自动入库

采集命令执行时自动将 clean_matches 导入数据库：

```bash
py -3 main.py --all --date today
# = 采集 → 清洗 → 导出JSON/CSV → 导入PostgreSQL
```

若 PostgreSQL 未就绪，跳过入库步骤，不影响采集和文件导出。

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

--db-test        测试数据库连接
--db-init        初始化数据库表结构
--db-import      导入 clean_matches 到数据库
--db-status      查看数据库统计
```

## 安全策略

- 自动限速，每个数据源独立限制
- 随机延迟 2-5 秒
- 失败自动重试 3 次
- 不绕过付费墙
- 不暴力请求
- 完整错误日志
