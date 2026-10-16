"""Small read-only web UI for the worker status and rotated log files."""

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


LOG_NAME = re.compile(r"worker\.log(?:\.\d+)?\Z")
STATUS_FIELDS = (
    "phase", "pid", "started_at", "heartbeat_at", "auth_mode", "last_api_at",
    "last_auth_at", "last_auth_failure_at", "last_auth_failure_http_status",
    "last_error", "retry_in", "active_signs", "last_sign", "dry_run",
    "credential_source",
)


INDEX = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>wzj · 运行日志</title>
  <style>
    :root{color-scheme:dark;--bg:#0b1020;--panel:#121a2d;--line:#263554;--text:#e8eefc;--muted:#91a0bd;--accent:#6ea8fe;--good:#42d392;--warn:#f0b45b;--bad:#ff7474}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#172442 0,#0b1020 42%);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
    button,input,select{font:inherit;color:inherit}button,select,input{background:#0e172a;border:1px solid var(--line);border-radius:8px;padding:8px 10px}button{cursor:pointer}button:hover{border-color:var(--accent)}
    .shell{max-width:1500px;margin:auto;padding:28px}.top{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:12px}.mark{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,#6ea8fe,#9b8cff);font-weight:800;color:#091020}.eyebrow{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.16em}.title{font-size:24px;font-weight:750}.status{display:flex;align-items:center;gap:8px;border:1px solid var(--line);background:#111a2e;border-radius:999px;padding:8px 12px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.dot.good{background:var(--good);box-shadow:0 0 12px #42d392}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
    .layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px}.panel{background:rgba(18,26,45,.9);border:1px solid var(--line);border-radius:16px;box-shadow:0 16px 50px rgba(0,0,0,.18)}.sidebar{padding:16px}.section-title{display:flex;align-items:center;justify-content:space-between;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.1em;margin:2px 4px 12px}.files{display:grid;gap:8px}.file{width:100%;text-align:left;padding:11px 12px;border-radius:10px}.file.active{border-color:var(--accent);background:#172544}.file-name{display:block;color:var(--text);font-weight:650}.file-meta{display:block;color:var(--muted);font-size:12px;margin-top:3px}.empty{color:var(--muted);padding:12px 4px}.stats{display:grid;gap:8px;margin-top:24px}.stat{padding:11px 12px;background:#0f1729;border:1px solid var(--line);border-radius:10px}.stat-label{display:block;color:var(--muted);font-size:12px}.stat-value{display:block;margin-top:3px;word-break:break-word}
    .content{min-width:0}.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--line)}.toolbar strong{margin-right:auto;min-width:150px}.toolbar input[type=search]{min-width:180px}.check{display:flex;align-items:center;gap:6px;color:var(--muted);white-space:nowrap}.check input{accent-color:var(--accent)}.viewer{padding:0}.log{margin:0;min-height:560px;max-height:calc(100vh - 230px);overflow:auto;padding:18px 20px;background:#0a101d;color:#dce7ff;font:13px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.footer{display:flex;justify-content:space-between;gap:12px;padding:10px 16px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
    @media(max-width:800px){.shell{padding:16px}.top{align-items:flex-start;flex-direction:column}.layout{grid-template-columns:1fr}.log{min-height:420px;max-height:none}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
  </style>
</head>
<body>
  <main class="shell">
    <header class="top">
      <div class="brand"><div class="mark">wzj</div><div><div class="eyebrow">TeacherMate worker</div><div class="title">运行日志</div></div></div>
      <div class="status"><span id="status-dot" class="dot"></span><span id="status-text">加载中…</span></div>
    </header>
    <section class="layout">
      <aside class="panel sidebar">
        <div class="section-title"><span>日志文件</span><button id="refresh-files" title="刷新文件列表">刷新</button></div>
        <div id="files" class="files"><div class="empty">正在读取…</div></div>
        <div class="stats">
          <div class="stat"><span class="stat-label">心跳</span><span id="heartbeat" class="stat-value">—</span></div>
          <div class="stat"><span class="stat-label">当前阶段</span><span id="phase" class="stat-value">—</span></div>
          <div class="stat"><span class="stat-label">最近错误</span><span id="error" class="stat-value">—</span></div>
        </div>
      </aside>
      <section class="panel content">
        <div class="toolbar"><strong id="selected-file">worker.log</strong><input id="search" type="search" placeholder="筛选日志内容…" aria-label="筛选日志内容"><select id="tail" aria-label="显示行数"><option value="100">100 行</option><option value="300">300 行</option><option value="500" selected>500 行</option><option value="1000">1000 行</option><option value="2000">2000 行</option></select><label class="check"><input id="auto" type="checkbox" checked> 自动刷新</label><button id="refresh-log">刷新</button><button id="copy-log">复制</button></div>
        <div class="viewer"><pre id="log" class="log">正在读取日志…</pre></div>
        <div class="footer"><span id="log-meta">—</span><span id="updated">—</span></div>
      </section>
    </section>
  </main>
  <script>
    const $=id=>document.getElementById(id); let selected='worker.log', timer=null, raw='';
    const fmtSize=n=>n<1024?`${n} B`:`${(n/1024).toFixed(n<10240?1:0)} KB`;
    const fmtTime=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'—';
    const api=async(path)=>{const r=await fetch(path,{cache:'no-store'});if(!r.ok)throw new Error(await r.text()||r.status);return r.json()};
    function draw(){const q=$('search').value.trim().toLowerCase();const lines=raw.split('\n');const shown=q?lines.filter(line=>line.toLowerCase().includes(q)):lines;$('log').textContent=shown.join('\n');$('log-meta').textContent=`${shown.filter(Boolean).length} 行 · ${q?'已筛选':''}`}
    async function files(){const data=await api('api/logs');const box=$('files');box.textContent='';if(!data.files.length){box.innerHTML='<div class="empty">暂无日志文件</div>';return}if(!data.files.some(f=>f.name===selected))selected=data.files[0].name;for(const f of data.files){const b=document.createElement('button');b.className='file'+(f.name===selected?' active':'');b.innerHTML=`<span class="file-name"></span><span class="file-meta">${fmtSize(f.size)} · ${fmtTime(f.modified_at)}</span>`;b.firstElementChild.textContent=f.name;b.onclick=()=>{selected=f.name;files().then(load)};box.appendChild(b)}$('selected-file').textContent=selected}
    async function load(){const data=await api(`api/log?name=${encodeURIComponent(selected)}&tail=${$('tail').value}`);raw=data.content;draw();$('selected-file').textContent=data.name;$('updated').textContent=`更新于 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`}
    async function status(){const s=await api('api/status');const phase=s.phase||'unknown';$('phase').textContent=phase;$('heartbeat').textContent=s.heartbeat_at?fmtTime(s.heartbeat_at):'暂无';$('error').textContent=s.last_error||'无';const good=['running','ready'].includes(phase),bad=['crashed','auth_expired'].includes(phase);$('status-dot').className='dot '+(good?'good':bad?'bad':'warn');$('status-text').textContent=phase}
    async function refresh(){try{await files();await Promise.all([load(),status()])}catch(e){$('status-text').textContent='读取失败';$('status-dot').className='dot bad';$('log').textContent=e.message}}
    $('refresh-files').onclick=refresh;$('refresh-log').onclick=load;$('tail').onchange=load;$('search').oninput=draw;$('copy-log').onclick=()=>navigator.clipboard?.writeText($('log').textContent);$('auto').onchange=()=>{if(timer)clearInterval(timer);if($('auto').checked)timer=setInterval(refresh,5000)};refresh();timer=setInterval(refresh,5000);
  </script>
</body>
</html>'''


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
        return default


def _log_paths(runtime):
    runtime = Path(runtime).resolve()
    return sorted(
        (path for path in runtime.iterdir() if LOG_NAME.fullmatch(path.name) and path.is_file()
         and path.resolve().parent == runtime),
        key=lambda path: (path.name != "worker.log", path.name),
    )


def _tail(path, lines):
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


class Handler(BaseHTTPRequestHandler):
    server_version = "wzj-log-ui/1.0"

    def _send(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        request = urlsplit(self.path)
        try:
            if request.path == "/":
                return self._send(200, INDEX, "text/html; charset=utf-8")
            if request.path == "/api/status":
                status = _read_json(self.server.runtime / "status.json", {"phase": "not_started"})
                if not isinstance(status, dict):
                    status = {"phase": "unknown"}
                return self._json(200, {key: status[key] for key in STATUS_FIELDS if key in status})
            if request.path == "/api/logs":
                return self._json(200, {"files": [
                    {"name": path.name, "size": path.stat().st_size, "modified_at": path.stat().st_mtime}
                    for path in _log_paths(self.server.runtime)
                ]})
            if request.path == "/api/log":
                query = parse_qs(request.query)
                name = query.get("name", ["worker.log"])[0]
                count = min(max(int(query.get("tail", ["500"])[0]), 1), 2000)
                path = next((path for path in _log_paths(self.server.runtime) if path.name == name), None)
                if path is None:
                    return self._json(404, {"error": "日志文件不存在"})
                return self._json(200, {"name": name, "content": _tail(path, count), "lines": count})
            return self._json(404, {"error": "Not found"})
        except (ValueError, OSError) as exc:
            return self._json(400, {"error": str(exc)})

    def log_message(self, *_):
        return


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, runtime):
        super().__init__(address, Handler)
        self.runtime = Path(runtime).resolve()


def start_server(runtime, host="127.0.0.1", port=8765):
    server = Server((host, port), runtime)
    threading.Thread(target=server.serve_forever, name="wzj-log-ui", daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(__file__).with_name(".runtime"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = Server((args.host, args.port), args.runtime_dir)
    print(f"日志 UI：http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
