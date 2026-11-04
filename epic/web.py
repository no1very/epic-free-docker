"""可视化面板：纯标准库 HTTP 服务 + 内嵌单页仪表盘（无外部 CDN 依赖）。

GET  /                 仪表盘页面
GET  /api/status       运行状态（账号/上次运行/下次运行/周期/摘要）
GET  /api/free-games   本周免费游戏（含领取状态）
GET  /api/logs         最近日志
GET  /api/last-run     上次领取结果（last_run.json）
GET  /api/login        登录授权流程状态（url/user_code/state）
POST /api/run          立即触发一次领取
POST /api/detect       立即刷新免费游戏列表（无需登录）
POST /api/login        在面板上发起设备码授权（生成登录链接）
"""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import api, parser, state


def _iso(ts) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _build_status(cfg: dict) -> dict:
    from . import store

    token = store.load(cfg["DATA_DIR"])
    logged_in = False
    display_name = None
    if token:
        exp = parser.parse_iso(token.get("refresh_expires_at", ""))
        logged_in = bool(exp and exp > datetime.now(timezone.utc))
        display_name = token.get("displayName") or token.get("account_id")
    snap = state.snapshot()
    return {
        "logged_in": logged_in,
        "display_name": display_name,
        "busy": snap["busy"],
        "last_run_at": _iso(snap["last_run_at"]),
        "next_run_at": _iso(snap["next_run_at"]),
        "interval_hours": cfg["INTERVAL_HOURS"],
        "country": cfg["COUNTRY"],
        "notify_method": cfg["NOTIFY_METHOD"],
        "started_at": _iso(snap["started_at"]),
        "last_error": snap["last_error"],
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def _detect_bg(cfg: dict) -> None:
    """后台刷新免费游戏列表（公开接口，无需登录；保留已知状态）"""
    try:
        body = api.fetch_weekly_free_games_json(cfg["COUNTRY"], cfg["LOCALE"])
        games = parser.parse_weekly_free_games(body)
        old = {g["title"]: g.get("status") for g in (state.snapshot()["free_games"] or [])}
        old_end = {g["title"]: g.get("end_date") for g in (state.snapshot()["free_games"] or [])}
        state.set_free_games(games, {g["title"]: old.get(g["title"], "claimable") for g in games}, old_end)
        state.log(f"面板：免费游戏列表已刷新（{len(games)} 款）")
    except Exception as e:
        with state.LOCK:
            state.STATE["last_error"] = f"刷新免费列表失败: {e}"
        state.log(f"面板：刷新免费列表失败 {e}")


def _run_bg() -> None:
    """后台执行一次领取（复用 main 的运行锁，避免与周期任务并发）"""
    from main import run_claim_guarded

    run_claim_guarded()


def _login_bg() -> None:
    """后台执行登录授权流程（纯 OAuth 轮询，与领取任务互不冲突）"""
    from main import run_login_flow

    run_login_flow(log_fn=state.log)


def _start_thread(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "EpicFreeDash/1.0"
    cfg: dict = {}

    def log_message(self, fmt, *args):  # 静默访问日志（面板轮询太吵）
        pass

    # ---------- 路由 ----------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML)
        elif path == "/api/status":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(_build_status(self.cfg), ensure_ascii=False))
        elif path == "/api/free-games":
            snap = state.snapshot()
            self._send(200, "application/json; charset=utf-8", json.dumps(
                {"updated_at": _iso(snap["free_games_at"]), "games": snap["free_games"] or []},
                ensure_ascii=False))
        elif path == "/api/logs":
            with state.LOCK:
                lines = list(state.LOGS)
            self._send(200, "application/json; charset=utf-8", json.dumps(
                {"lines": lines[-300:]}, ensure_ascii=False))
        elif path == "/api/last-run":
            import os

            from . import store

            try:
                with open(os.path.join(self.cfg["DATA_DIR"], "last_run.json"), encoding="utf-8") as f:
                    body = f.read()
            except Exception:
                body = "null"
            self._send(200, "application/json; charset=utf-8", body or "null")
        elif path == "/api/login":
            snap = state.snapshot()
            self._send(200, "application/json; charset=utf-8", json.dumps(
                {
                    "state": snap["login_state"],
                    "url": snap["login_url"],
                    "user_code": snap["login_code"],
                    "message": snap["login_message"],
                    "expires_at": _iso(snap["login_expires_at"]),
                },
                ensure_ascii=False))
        else:
            self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/run":
            if state.snapshot()["busy"]:
                self._send(409, "application/json; charset=utf-8",
                           json.dumps({"ok": False, "message": "已有任务在运行，请稍候"}))
                return
            _start_thread(_run_bg)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "message": "已触发，正在检测领取…"}))
        elif path == "/api/detect":
            _start_thread(_detect_bg, self.cfg)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "message": "正在刷新免费游戏列表…"}))
        elif path == "/api/login":
            if state.snapshot()["login_state"] in ("starting", "waiting"):
                self._send(409, "application/json; charset=utf-8",
                           json.dumps({"ok": False, "message": "授权流程进行中，请打开链接完成授权"}))
                return
            _start_thread(_login_bg)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "message": "正在生成 Epic 授权链接…"}))
        else:
            self._send(404, "text/plain; charset=utf-8", "not found")

    def _send(self, code: int, ctype: str, body: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def serve_forever(cfg: dict, port: int) -> None:
    Handler.cfg = cfg
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    state.log(f"可视化面板: http://0.0.0.0:{port}/")
    server.serve_forever()


# ================= 内嵌仪表盘 =================

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Epic 白嫖助手</title>
<style>
  :root {
    --bg: #0f1420; --card: #1a2132; --line: #2a3550;
    --text: #e8ecf4; --muted: #98a2b8;
    --accent: #0078f2; --ok: #4caf50; --warn: #f44336; --info: #2196f3;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    min-height: 100vh; padding: 24px 16px 48px;
  }
  .wrap { max-width: 980px; margin: 0 auto; }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }
  h1 { font-size: 22px; }
  h1 span { margin-right: 8px; }
  .chip {
    font-size: 12px; padding: 4px 12px; border-radius: 999px;
    background: var(--card); border: 1px solid var(--line); color: var(--muted);
  }
  .chip.on { color: #b6f2c1; border-color: rgba(76,175,80,.5); }
  .chip.off { color: #ffc9c4; border-color: rgba(244,67,54,.5); }
  .chip.busy { color: #ffe08a; border-color: rgba(255,193,7,.5); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 16px;
  }
  .card h2 { font-size: 14px; color: var(--muted); margin-bottom: 12px; font-weight: 600; }
  .kv { display: flex; justify-content: space-between; padding: 5px 0; font-size: 14px; }
  .kv .k { color: var(--muted); }
  .btns { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  button {
    background: var(--accent); color: #fff; border: 0; border-radius: 10px;
    padding: 10px 16px; font-size: 14px; cursor: pointer;
  }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--muted); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .games { display: flex; flex-direction: column; gap: 10px; }
  .game {
    display: flex; align-items: center; justify-content: space-between;
    background: rgba(255,255,255,.03); border-radius: 10px; padding: 12px 14px;
  }
  .game .name { font-size: 15px; font-weight: 600; }
  .game .end { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .tag { font-size: 12px; padding: 3px 10px; border-radius: 999px; white-space: nowrap; }
  .tag.claimable { color: #b6f2c1; background: rgba(76,175,80,.15); }
  .tag.claimed { color: #bfe3ff; background: rgba(33,150,243,.18); }
  .tag.owned { color: var(--muted); background: rgba(152,162,184,.15); }
  .tag.failed { color: #ffc9c4; background: rgba(244,67,54,.15); }
  .summary .row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
  .summary .pill {
    font-size: 13px; background: rgba(255,255,255,.04);
    border-radius: 8px; padding: 6px 10px;
  }
  #log {
    background: #0b0f18; border: 1px solid var(--line); border-radius: 10px;
    padding: 12px; font: 12px/1.6 Consolas, "JetBrains Mono", monospace;
    color: #9fb3d1; height: 260px; overflow-y: auto; white-space: pre-wrap;
    word-break: break-all;
  }
  .hint { font-size: 13px; color: var(--muted); background: rgba(0,120,242,.08);
          border: 1px dashed rgba(0,120,242,.4); border-radius: 10px; padding: 10px 14px; margin-bottom: 14px; }
  .hint code { background: rgba(255,255,255,.08); padding: 1px 6px; border-radius: 5px; }
  #loginUrl { color: var(--accent); word-break: break-all; font-size: 14px; }
  #loginBox { margin-top: 10px; background: rgba(0,120,242,.10); border-radius: 8px; padding: 10px 12px; }
  #loginMeta { margin-top: 6px; color: var(--muted); font-size: 13px; }
  #toast {
    position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%);
    background: #223; border: 1px solid var(--line); color: var(--text);
    padding: 10px 18px; border-radius: 10px; font-size: 14px;
    opacity: 0; transition: opacity .25s; pointer-events: none; max-width: 80vw;
  }
  #toast.show { opacity: 1; }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span>🎁</span>Epic 白嫖助手</h1>
    <div id="acct" class="chip">加载中…</div>
  </header>

  <div id="loginHint" class="hint" style="display:none">
    <div>尚未登录。点击下方按钮生成 <b>Epic 官方授权链接</b>，在浏览器打开并完成登录（也可用命令行
      <code>docker compose run --rm epic python main.py login</code>）。</div>
    <div class="btns" style="margin-top:10px">
      <button id="btnLogin">🔑 生成授权链接</button>
    </div>
    <div id="loginBox" style="display:none">
      <a id="loginUrl" href="#" target="_blank" rel="noopener"></a>
      <div id="loginMeta"></div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>运行状态</h2>
      <div class="kv"><span class="k">账号</span><span id="stAccount">—</span></div>
      <div class="kv"><span class="k">上次运行</span><span id="stLast">—</span></div>
      <div class="kv"><span class="k">下次运行</span><span id="stNext">—</span></div>
      <div class="kv"><span class="k">检测周期</span><span id="stInterval">—</span></div>
      <div class="kv"><span class="k">地区 / 通知</span><span id="stRegion">—</span></div>
      <div class="btns">
        <button id="btnRun">⚡ 立即检测领取</button>
        <button id="btnDetect" class="ghost">↻ 刷新免费列表</button>
      </div>
    </div>

    <div class="card summary">
      <h2>上次领取结果</h2>
      <div id="sumBody"><div class="empty">还没有运行记录</div></div>
    </div>

    <div class="card">
      <h2>本周免费游戏</h2>
      <div id="games" class="games"><div class="empty">加载中…（可点右上"刷新免费列表"）</div></div>
    </div>

    <div class="card">
      <h2>运行日志</h2>
      <div id="log"></div>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let serverOffsetMs = 0;      // 服务器时间 - 本地时间
let nextRunAt = null;        // epoch ms（服务器时钟）

function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 3500);
}

function fmtTs(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
}

function fmtEnd(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return '截止 ' + d.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
}

const TAG = {claimable:'可领取', claimed:'已领取', owned:'已拥有', failed:'领取失败'};

async function pollStatus() {
  try {
    const s = await (await fetch('/api/status')).json();
    serverOffsetMs = new Date(s.server_time).getTime() - Date.now();
    nextRunAt = s.next_run_at ? new Date(s.next_run_at).getTime() : null;

    const acct = $('acct');
    if (s.busy) { acct.textContent = '运行中…'; acct.className = 'chip busy'; }
    else if (s.logged_in) { acct.textContent = '在线 · ' + (s.display_name || '已登录'); acct.className = 'chip on'; }
    else { acct.textContent = '未登录'; acct.className = 'chip off'; }
    $('loginHint').style.display = s.logged_in ? 'none' : '';

    $('stAccount').textContent = s.logged_in ? (s.display_name || '已登录') : '未登录';
    $('stLast').textContent = fmtTs(s.last_run_at);
    $('stInterval').textContent = '每 ' + s.interval_hours + ' 小时';
    $('stRegion').textContent = s.country + ' / ' + s.notify_method;
    $('btnRun').disabled = s.busy;
    if (s.last_error) toast('⚠ ' + s.last_error);
  } catch (e) { /* 服务未就绪 */ }
}

function renderNext() {
  const el = $('stNext');
  if (!nextRunAt) { el.textContent = '—'; return; }
  let diff = Math.round((nextRunAt - (Date.now() + serverOffsetMs)) / 1000);
  if (diff <= 0) { el.textContent = '即将运行…'; return; }
  const h = Math.floor(diff / 3600), m = Math.floor(diff % 3600 / 60), sec = diff % 60;
  el.textContent = (h ? h + ' 时 ' : '') + m + ' 分 ' + sec + ' 秒后';
}

async function pollGames() {
  try {
    const r = await (await fetch('/api/free-games')).json();
    const box = $('games');
    if (!r.games.length) {
      box.innerHTML = '<div class="empty">暂无数据 — 点"刷新免费列表"获取</div>';
      return;
    }
    box.innerHTML = r.games.map(g => `
      <div class="game">
        <div><div class="name">${escapeHtml(g.title)}</div>
             <div class="end">${fmtEnd(g.end_date)}</div></div>
        <span class="tag ${g.status}">${TAG[g.status] || '可领取'}</span>
      </div>`).join('');
  } catch (e) {}
}

function summaryHtml(s) {
  if (!s) return '<div class="empty">还没有运行记录</div>';
  let html = '';
  const claimed = s.claimed || [], owned = s.owned || [], failed = s.failed || [];
  if (claimed.length) {
    html += `<div class="kv"><span class="k">🎉 成功领取（${claimed.length}）</span></div>
             <div class="row">${claimed.map(t => `<span class="pill">${escapeHtml(t)}</span>`).join('')}</div>`;
  }
  if (owned.length) {
    html += `<div class="kv" style="margin-top:8px"><span class="k">✅ 已拥有（${owned.length}）</span></div>
             <div class="row">${owned.map(t => `<span class="pill">${escapeHtml(t)}</span>`).join('')}</div>`;
  }
  if (failed.length) {
    html += `<div class="kv" style="margin-top:8px"><span class="k">❌ 失败（${failed.length}）</span></div>
             <div class="row">${failed.map(f => `<span class="pill">⚠ ${escapeHtml(f.title)}：${escapeHtml(f.message)}</span>`).join('')}</div>`;
  }
  if (!html) html = '<div class="empty">上次运行没有可领取的免费游戏</div>';
  return html;
}

async function pollSummary() {
  try {
    const s = await (await fetch('/api/status')).json();
    const sum = await (await fetch('/api/last-run')).json();
    $('sumBody').innerHTML = summaryHtml(sum);
  } catch (e) {}
}

async function pollLogs() {
  try {
    const r = await (await fetch('/api/logs')).json();
    const el = $('log');
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = r.lines.join('\n') || '暂无日志';
    if (stick) el.scrollTop = el.scrollHeight;
  } catch (e) {}
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function post(path) {
  try {
    const r = await (await fetch(path, {method: 'POST'})).json();
    toast(r.message || (r.ok ? '已触发' : '操作失败'));
  } catch (e) { toast('请求失败：' + e.message); }
}

// ---------- 面板授权登录 ----------
let lastLoginState = 'idle';

async function pollLogin() {
  const hint = $('loginHint');
  if (hint.style.display === 'none') return;
  try {
    const s = await (await fetch('/api/login')).json();
    const box = $('loginBox'), link = $('loginUrl'), meta = $('loginMeta'), btn = $('btnLogin');
    if (s.state === 'waiting' || s.state === 'starting') {
      box.style.display = '';
      if (s.url) { link.href = s.url; link.textContent = s.url; }
      meta.textContent = (s.user_code ? '用户码: ' + s.user_code + ' · ' : '') +
        (s.state === 'starting' ? '正在生成链接…' : '等待你在 Epic 页面完成授权…（成功后本页面自动识别）');
      btn.disabled = true; btn.textContent = '等待授权中…';
    } else if (s.state === 'failed' || s.state === 'expired') {
      box.style.display = '';
      link.removeAttribute('href'); link.textContent = '';
      meta.textContent = s.message || '授权失败，请重试';
      btn.disabled = false; btn.textContent = '🔑 重试授权';
    } else {
      box.style.display = 'none';
      btn.disabled = false; btn.textContent = '🔑 生成授权链接';
    }
    if (s.state !== lastLoginState) {
      if (s.state === 'success') toast('✅ 登录成功');
      if (s.state === 'expired') toast('授权超时，请重新生成链接');
      lastLoginState = s.state;
    }
  } catch (e) {}
}

$('btnRun').onclick = () => post('/api/run');
$('btnDetect').onclick = () => post('/api/detect');
$('btnLogin').onclick = () => post('/api/login');

pollStatus(); pollGames(); pollSummary(); pollLogs(); pollLogin();
setInterval(pollStatus, 5000);
setInterval(pollGames, 8000);
setInterval(pollSummary, 8000);
setInterval(pollLogs, 4000);
setInterval(pollLogin, 3000);
setInterval(renderNext, 1000);
</script>
</body>
</html>
"""
