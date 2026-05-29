# 开发环境搭建指南

> football-data-crawler — 足球数据采集 & AI 预测系统

---

## 1. 环境要求

| 依赖 | 版本 | 备注 |
|------|------|------|
| Python | 3.11.9 | 本机使用 `py` 命令调用（`python` 未加入 PATH） |
| Git | 任意 | 代码托管于 GitHub |
| PostgreSQL | 14+ | 可选，仅 `--db-*` 功能需要 |
| 磁盘空间 | >5GB | Playwright 浏览器 + 依赖 + 数据 |

## 2. 克隆仓库

```powershell
git clone https://github.com/ztao3265-hue/football-data-crawler.git
cd football-data-crawler
```

## 3. 创建虚拟环境

```powershell
# 使用 py 启动器（Windows 默认方式）
py -m venv .venv

# 激活虚拟环境
.venv\Scripts\Activate.ps1

# 如果遇到执行策略限制，先执行：
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# 升级 pip
.venv\Scripts\python.exe -m pip install --upgrade pip

# 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 4. 安装 Playwright 浏览器

```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

## 5. 配置环境变量

```powershell
# 复制示例配置
copy .env.example .env

# 编辑 .env，填写必要配置
```

**.env 关键配置项：**

| 变量 | 说明 | 必填 |
|------|------|------|
| `FOOTBALL_DATA_ORG_API_KEY` | football-data.org API Key | 否（该数据源需 Key） |
| `DATA_ROOT` | 统一数据根目录 | 是，默认 `D:/FootballData` |
| `DATABASE_URL` | PostgreSQL 连接字符串 | 否（仅数据库功能需要） |
| `HEADLESS` | 浏览器无头模式 | 否，默认 `true` |

> 免费注册 football-data.org API Key: https://www.football-data.org/client/register

## 6. 启动命令

```powershell
# 查看帮助
.venv\Scripts\python.exe main.py --help

# 采集单个数据源（今天的数据）
.venv\Scripts\python.exe main.py --source sofascore --date today

# 采集所有数据源
.venv\Scripts\python.exe main.py --all --date today

# 启动 Web 管理界面（端口 8080）
.venv\Scripts\python.exe main.py --ui

# 启动 FastAPI 数据接口（端口 8000）
.venv\Scripts\python.exe main.py --api

# 启动定时采集服务（每 30 分钟）
.venv\Scripts\python.exe main.py --schedule

# 运行初始化向导（一键安装所有依赖）
.venv\Scripts\python.exe main.py --setup
```

## 7. football-data.org API Key 配置（DSV4-Pro）

本项目通过 football-data.org v4 API 获取欧洲五大联赛及欧冠数据。

### 注册

1. 访问 https://www.football-data.org/client/register
2. 填写邮箱注册免费账户
3. 在 Dashboard 获取 API Key

### 配置

在 `.env` 文件中设置：

```
FOOTBALL_DATA_ORG_API_KEY=your_api_key_here
```

### 免费套餐限制

- 每分钟 10 次请求
- 仅限足球数据
- 部分联赛有访问限制

> 项目已内置 rate limit 控制（见 `configs/sources.json`），无需手动处理。

## 8. Git 同步流程

```powershell
# 拉取最新代码
git pull origin main

# 开发完成后提交（CLAUDE.md 自动规则）
git add -A
git commit -m "feat: <简短描述> — <关键亮点>"
git push origin main
```

### 双电脑协作

从另一台电脑继续开发时：

```powershell
# 1. 拉取最新代码
git pull origin main

# 2. 重建虚拟环境（如果 .venv 不存在）
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium

# 3. 配置 .env（不会从 Git 同步，需手动创建）
copy .env.example .env
# 编辑 .env 填入 API Key 等配置

# 4. 验证环境
.venv\Scripts\python.exe main.py --help
```

### 安全机制（自动执行）

- `.env`、`credentials.json`、密钥文件 → `.gitignore` 排除
- `data/`、`datasets/`、`models/` → 不提交（生成数据）
- `>1MB` 的 CSV 审计文件 → 排除
- `node_modules/`、`cache/`、`logs/`、`*.db` → 全部排除

## 9. 项目结构

```
football-data-crawler/
├── main.py                 # 命令行入口
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量模板
├── .env                    # 本地环境变量（不提交）
├── CLAUDE.md               # Claude Code 指令
├── configs/
│   ├── sources.json        # 数据源配置（联赛、API、限速）
│   └── settings.json       # 全局设置（浏览器、导出、缓存、调度）
├── crawler/                # 爬虫系统
│   ├── core/               # 引擎、日志、调度器
│   ├── api/                # FastAPI 数据接口
│   ├── ui/                 # Web 管理界面
│   ├── database/           # PostgreSQL 连接和导入
│   └── utils/              # 工具函数
├── backend/                # 后端分析系统
│   ├── tools/              # 数据质量验证
│   ├── features/           # 特征工程引擎
│   ├── models/             # 模型训练系统
│   └── backtest/           # Walk Forward 回测
├── reports/                # 报告文件（提交）
├── data/                   # 本地数据（不提交）
├── datasets/               # 生成数据集（不提交）
└── models/                 # 训练好的模型（不提交）
```

## 10. 常见故障排查

### 10.1 `python` 命令找不到

**症状：** `python : The term 'python' is not recognized...`

**解决：**
```powershell
# 使用 py 命令代替
py --version
py -m venv .venv
.venv\Scripts\python.exe main.py --help
```

或永久修复：将 Python 加入 PATH
1. 找到 Python 安装路径：`py -c "import sys; print(sys.prefix)"`
2. 将该路径加入系统环境变量 PATH

### 10.2 虚拟环境激活失败

**症状：** `Activate.ps1 cannot be loaded because running scripts is disabled`

**解决：**
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
# 或直接使用完整路径，跳过激活
.venv\Scripts\python.exe main.py --help
```

### 10.3 Playwright 浏览器未安装

**症状：** `Executable doesn't exist at ... chromium ...`

**解决：**
```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

### 10.4 PostgreSQL 连接失败

**症状：** `could not connect to server: Connection refused`

**解决：**
1. 确认 PostgreSQL 服务已启动
2. 检查 `.env` 中 `DATABASE_URL` 或 `DB_HOST/DB_PORT/DB_USER/DB_PASSWORD` 配置
3. 仅文件采集（无需数据库）：`python main.py --all --no-db`
4. 测试连接：`python main.py --db-test`

### 10.5 依赖版本冲突

**症状：** `ERROR: pip's dependency resolver...`

**解决：**
```powershell
# 删除虚拟环境重建
Remove-Item -Recurse -Force .venv
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 10.6 数据目录 D:/FootballData 不存在

**症状：** `FileNotFoundError: D:/FootballData/exports`

**解决：**
```powershell
# 创建统一数据目录结构
New-Item -ItemType Directory -Force -Path D:\FootballData\exports
New-Item -ItemType Directory -Force -Path D:\FootballData\raw_data
New-Item -ItemType Directory -Force -Path D:\FootballData\database
New-Item -ItemType Directory -Force -Path D:\FootballData\snapshots
New-Item -ItemType Directory -Force -Path D:\FootballData\models
```

或修改 `.env` 中 `DATA_ROOT` 指向已有目录。

### 10.7 端口被占用

**症状：** `Address already in use` (API 8000 / UI 8080)

**解决：**
```powershell
# 查看端口占用
netstat -ano | findstr :8000

# 使用其他端口
.venv\Scripts\python.exe main.py --api --api-port 8001
.venv\Scripts\python.exe main.py --ui --ui-port 8081
```

## 11. 一键环境验证

```powershell
# 复制以下脚本到 PowerShell 运行
Write-Output "===== 环境验证 ====="

# Python
py --version
Write-Output ""

# 虚拟环境
if (Test-Path ".venv") { Write-Output "[OK] .venv 存在" } else { Write-Output "[MISS] .venv 不存在 - 运行: py -m venv .venv" }

# 入口文件
if (Test-Path "main.py") { Write-Output "[OK] main.py" } else { Write-Output "[MISS] main.py" }

# .env
if (Test-Path ".env") { Write-Output "[OK] .env" } else { Write-Output "[MISS] .env - 运行: copy .env.example .env" }

# Git
git status 2>&1 | Select-Object -First 1

# 依赖快速检查
.venv\Scripts\python.exe -c "import playwright,pandas,httpx,fastapi,uvicorn; print('[OK] 核心依赖导入正常')" 2>&1

Write-Output "===== 验证完成 ====="
```
