"""Web UI 服务器 - 苹果风深色主题管理页面"""

import json
import time
import asyncio
import threading
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from crawler.core.logger import get_logger

logger = get_logger("ui")

STATUS_COLORS = {
    "running": "#34C759",
    "success": "#30D158",
    "partial": "#FF9F0A",
    "failed": "#FF453A",
    "idle": "#8E8E93",
}


def collect_status() -> dict:
    """收集当前采集状态"""
    sources_status = {}

    # 检查各数据源的最近采集状态
    for source in ["sofascore", "fotmob", "football-data"]:
        json_file = Path(f"exports/matches_today.json")

        if json_file.exists():
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                source_matches = [m for m in data if m.get("source") == source]
                sources_status[source] = {
                    "status": "success" if source_matches else "partial",
                    "last_collected": data[0].get("collected_at", "") if data else "",
                    "match_count": len(source_matches),
                    "success_rate": "100%" if source_matches else "0%",
                    "error": "",
                }
            except Exception:
                sources_status[source] = _default_status("failed", "读取数据失败")
        else:
            sources_status[source] = _default_status("idle")

    return sources_status


def _default_status(status: str, error: str = ""):
    return {
        "status": status,
        "last_collected": "",
        "match_count": 0,
        "success_rate": "0%",
        "error": error,
    }


def read_recent_logs(lines: int = 50) -> list[str]:
    """读取最近的日志"""
    log_dir = Path("crawler/logs")
    logs = []

    log_files = sorted(log_dir.glob("crawler_*.log"), reverse=True)
    if log_files:
        content = log_files[0].read_text(encoding="utf-8", errors="ignore")
        log_lines = content.strip().split("\n")
        logs = log_lines[-lines:]

    return logs


class UIHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理"""

    def log_message(self, format, *args):
        pass  # 静默访问日志

    def _set_headers(self, content_type="text/html; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _send_json(self, data: dict):
        self._set_headers("application/json; charset=utf-8")
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _send_html(self, html: str):
        self._set_headers("text/html; charset=utf-8")
        self.wfile.write(html.encode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_page()
        elif path == "/api/status":
            self._send_json(collect_status())
        elif path == "/api/logs":
            logs = read_recent_logs(50)
            self._send_json({"logs": logs})
        elif path == "/api/crawl":
            params = parse_qs(parsed.query)
            source = params.get("source", ["all"])[0]
            self._send_json({
                "message": f"采集任务已触发: {source}",
                "source": source,
                "timestamp": datetime.now().isoformat(),
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/crawl":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
            try:
                params = json.loads(body)
            except json.JSONDecodeError:
                params = {}

            source = params.get("source", "all")
            logger.info(f"Web UI 触发采集: {source}")

            # 在新线程中执行采集
            def run_crawl():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    from crawler.core.engine import CrawlerEngine
                    from crawler.utils.helpers import load_json

                    sources_config = load_json("configs/sources.json")

                    async def _run():
                        engine = CrawlerEngine(headless=True)
                        try:
                            await engine.start_browser()
                            source_names = (
                                [source] if source != "all"
                                else ["sofascore", "fotmob", "football-data"]
                            )
                            await engine.run_sources(
                                source_names, datetime.now().strftime("%Y-%m-%d"),
                                sources_config
                            )
                            engine.export(datetime.now().strftime("%Y-%m-%d"))
                            engine.print_summary()
                        finally:
                            await engine.stop_browser()

                    loop.run_until_complete(_run())
                    loop.close()
                except Exception as e:
                    logger.error(f"Web UI 采集失败: {e}")

            thread = threading.Thread(target=run_crawl, daemon=True)
            thread.start()

            self._send_json({
                "success": True,
                "message": f"采集任务已触发: {source}",
                "timestamp": datetime.now().isoformat(),
            })
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_page(self):
        """返回管理页面"""
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Football Data Crawler</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }

:root {
  --bg-primary: #0D0D0D;
  --bg-secondary: #1A1A1A;
  --bg-card: #1C1C1E;
  --bg-hover: #2C2C2E;
  --text-primary: #F5F5F7;
  --text-secondary: #98989D;
  --border: #2C2C2E;
  --green: #34C759;
  --orange: #FF9F0A;
  --red: #FF453A;
  --blue: #0A84FF;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 4px 24px rgba(0,0,0,0.4);
}

body {
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

.container { max-width: 960px; margin: 0 auto; padding: 40px 24px; }

header {
  text-align: center;
  margin-bottom: 48px;
}

header h1 {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.5px;
  margin-bottom: 6px;
}

header p {
  color: var(--text-secondary);
  font-size: 15px;
  font-weight: 400;
}

/* Actions Bar */
.actions {
  display: flex;
  gap: 12px;
  margin-bottom: 32px;
  flex-wrap: wrap;
  justify-content: center;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 12px 24px;
  border-radius: 24px;
  border: none;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;
  font-family: inherit;
  color: white;
}

.btn-primary { background: var(--blue); }
.btn-primary:hover { background: #0077ED; transform: scale(1.02); }
.btn-success { background: var(--green); }
.btn-success:hover { background: #2DB84D; transform: scale(1.02); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.btn-sm { padding: 8px 16px; font-size: 13px; border-radius: 20px; }

/* Cards */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 32px;
}

.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px;
  transition: all 0.2s ease;
}

.card:hover { border-color: #48484A; }

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}

.status-dot.success { background: var(--green); box-shadow: 0 0 8px rgba(52,199,89,0.4); }
.status-dot.partial { background: var(--orange); box-shadow: 0 0 8px rgba(255,159,10,0.4); }
.status-dot.failed { background: var(--red); box-shadow: 0 0 8px rgba(255,69,58,0.4); }
.status-dot.idle { background: #8E8E93; }
.status-dot.running { background: var(--blue); animation: pulse 1.5s infinite; }

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.card-stats {
  display: flex;
  gap: 24px;
  margin: 12px 0 16px;
}

.stat { text-align: center; }

.stat-value {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.3px;
}

.stat-label {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 2px;
  text-transform: uppercase;
}

.card-meta {
  font-size: 12px;
  color: var(--text-secondary);
}

.card-meta span { display: block; margin-top: 4px; }

/* Logs */
.log-section { margin-top: 40px; }

.log-section h2 {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 12px;
}

.log-container {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  max-height: 320px;
  overflow-y: auto;
  font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  font-size: 12px;
  line-height: 1.6;
}

.log-line { color: var(--text-secondary); }
.log-line.error { color: var(--red); }
.log-line.warning { color: var(--orange); }
.log-line.success { color: var(--green); }

.log-container::-webkit-scrollbar { width: 6px; }
.log-container::-webkit-scrollbar-track { background: transparent; }
.log-container::-webkit-scrollbar-thumb { background: #3A3A3C; border-radius: 3px; }

/* Toast */
.toast {
  position: fixed;
  top: 20px;
  right: 20px;
  padding: 14px 24px;
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  border: 1px solid var(--border);
  font-size: 14px;
  font-weight: 500;
  opacity: 0;
  transform: translateY(-10px);
  transition: all 0.3s ease;
  pointer-events: none;
  z-index: 100;
  backdrop-filter: blur(20px);
}

.toast.show { opacity: 1; transform: translateY(0); }
.toast.success { border-color: var(--green); color: var(--green); }
.toast.error { border-color: var(--red); color: var(--red); }

/* Footer */
footer {
  text-align: center;
  margin-top: 48px;
  padding: 24px;
  color: var(--text-secondary);
  font-size: 12px;
}
</style>
</head>
<body>

<div class="container">
  <header>
    <h1>Football Data Crawler</h1>
    <p>职业级足球数据采集系统</p>
  </header>

  <div class="actions">
    <button class="btn btn-primary" onclick="triggerCrawl('all')" id="btn-all">
      <span>⟳</span> 一键采集全部
    </button>
    <button class="btn btn-sm" onclick="triggerCrawl('sofascore')" id="btn-sf">Sofascore</button>
    <button class="btn btn-sm" onclick="triggerCrawl('fotmob')" id="btn-fm">FotMob</button>
    <button class="btn btn-sm" onclick="triggerCrawl('football-data')" id="btn-fd">football-data</button>
  </div>

  <div class="cards" id="cards">
    <div class="card"><div class="card-title">加载中...</div></div>
  </div>

  <div class="log-section">
    <h2>最近日志</h2>
    <div class="log-container" id="log-container">加载中...</div>
  </div>

  <footer>Football Data Crawler v1.0 &copy; 2026</footer>
</div>

<div class="toast" id="toast"></div>

<script>
const API = {
  status: () => fetch('/api/status').then(r => r.json()),
  logs: () => fetch('/api/logs').then(r => r.json()),
  crawl: (source) => fetch('/api/crawl', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source})
  }).then(r => r.json()),
};

const sourceNames = { sofascore: 'Sofascore', fotmob: 'FotMob', 'football-data': 'football-data.org' };
const sourceOrder = ['sofascore', 'fotmob', 'football-data'];

function renderCards(status) {
  const container = document.getElementById('cards');
  let html = '';

  sourceOrder.forEach(key => {
    const s = status[key] || { status: 'idle', match_count: 0, success_rate: '0%', last_collected: '', error: '' };
    const name = sourceNames[key] || key;
    html += `<div class="card">
      <div class="card-header">
        <span class="card-title">${name}</span>
        <span class="status-dot ${s.status}"></span>
      </div>
      <div class="card-stats">
        <div class="stat">
          <div class="stat-value">${s.match_count}</div>
          <div class="stat-label">比赛数</div>
        </div>
        <div class="stat">
          <div class="stat-value">${s.success_rate}</div>
          <div class="stat-label">成功率</div>
        </div>
      </div>
      <div class="card-meta">
        <span>状态: ${s.status === 'running' ? '采集运行中...' : s.status}</span>
        ${s.last_collected ? `<span>最近采集: ${s.last_collected}</span>` : '<span>尚未采集</span>'}
        ${s.error ? `<span style="color:var(--red)">错误: ${s.error}</span>` : ''}
      </div>
      <div style="margin-top:12px">
        <button class="btn btn-sm btn-primary" onclick="triggerCrawl('${key}')">立即采集</button>
      </div>
    </div>`;
  });

  container.innerHTML = html;
}

function renderLogs(data) {
  const container = document.getElementById('log-container');
  const logs = data.logs || [];
  if (!logs.length) {
    container.innerHTML = '<div class="log-line">暂无日志</div>';
    return;
  }
  container.innerHTML = logs.map(line => {
    let cls = 'log-line';
    if (line.includes('ERROR')) cls += ' error';
    if (line.includes('WARNING')) cls += ' warning';
    return `<div class="${cls}">${escapeHtml(line)}</div>`;
  }).join('');
  container.scrollTop = container.scrollHeight;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

function showToast(msg, type) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.className = 'toast ' + type + ' show';
  setTimeout(() => { toast.className = 'toast'; }, 3000);
}

async function triggerCrawl(source) {
  const label = source === 'all' ? '全部' : (sourceNames[source] || source);
  showToast(`正在触发采集: ${label}...`, 'success');

  // Set running state
  const keys = source === 'all' ? sourceOrder : [source];
  keys.forEach(k => {
    const cardTitle = document.querySelector(`.card-title`);
  });

  try {
    const resp = await API.crawl(source);
    showToast(resp.message || '采集已触发', 'success');
    setTimeout(refreshAll, 3000);
  } catch(e) {
    showToast('触发失败: ' + e.message, 'error');
  }
}

async function refreshAll() {
  try {
    const [status, logs] = await Promise.all([API.status(), API.logs()]);
    renderCards(status);
    renderLogs(logs);
  } catch(e) {
    console.error('刷新失败:', e);
  }
}

// Initial load
refreshAll();
// Auto-refresh every 15s
setInterval(refreshAll, 15000);
</script>
</body>
</html>"""
        self._send_html(html)


def start_ui(port: int = 8080):
    """启动 Web UI 服务器"""
    server = HTTPServer(("127.0.0.1", port), UIHandler)
    print(f"\n  Web 管理界面已启动: http://127.0.0.1:{port}")
    print("  按 Ctrl+C 退出\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  服务器已停止")
        server.shutdown()
