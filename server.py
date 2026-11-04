"""云端入口：一个端口同时干三件事。

1. `/`            网页：粘贴小智控制台的接入点，看状态、看日志
2. `/p/{token}`   短链代理：设备从这里拉音频（由 stream_proxy 提供）
3. 后台子进程       mcp_pipe.py 拿着接入点主动连小智，把工具注册进去

为什么必须常驻：小智的接入点是「服务去连它」的 WebSocket 长连接，
进程一停工具就消失。所以这个文件是给 VPS / Koyeb 这类能一直跑的
环境用的，静态托管放不了。

启动：
    PORT=8080 python server.py
如果环境里已经有 MCP_ENDPOINT，启动后会自动去连。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import stream_proxy
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

_HERE = Path(__file__).resolve().parent
# 父进程学到的对外域名要落一份到磁盘：MCP 跑在子进程里，
# 它自己没经过 HTTP 请求，拿不到 Host 头。
_BASE_FILE = Path("/tmp/nif_public_base")
# 接入点也落盘：沙箱/容器重启后自动重连，用户不用再贴一次
_EP_FILE = _HERE / ".endpoint"

_PROC: subprocess.Popen | None = None
_ENDPOINT = ""
_STARTED_AT = 0.0
_LOG: deque[str] = deque(maxlen=400)
_LOCK = threading.Lock()


def _pump(stream) -> None:
    try:
        for line in iter(stream.readline, ""):
            line = line.rstrip()
            if line:
                _LOG.append(f"{time.strftime('%H:%M:%S')} {line}")
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _looks_connected() -> bool:
    keys = ("连接成功", "connected", "Connected", "注册成功", "initialize")
    return any(any(k in line for k in keys) for line in _LOG)


def stop_bridge() -> None:
    global _PROC
    if _PROC and _PROC.poll() is None:
        _PROC.terminate()
        try:
            _PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _PROC.kill()
    _PROC = None


def forget_endpoint() -> None:
    try:
        _EP_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def start_bridge(endpoint: str) -> tuple[bool, str]:
    global _PROC, _ENDPOINT, _STARTED_AT

    endpoint = (endpoint or "").strip()
    if not endpoint.lower().startswith(("ws://", "wss://")):
        return False, "接入点要以 wss:// 开头，去小智控制台复制完整那一行"

    with _LOCK:
        if _PROC and _PROC.poll() is None and endpoint == _ENDPOINT:
            return True, "已经在连了"
        stop_bridge()

        env = os.environ.copy()
        env["MCP_ENDPOINT"] = endpoint
        # 代理由本进程提供，别让子进程再起一个 uvicorn 抢同一个端口
        env["STREAM_PROXY"] = "0"
        base = stream_proxy.public_base()
        if base.startswith("http") and "127.0.0.1" not in base and "localhost" not in base:
            env["PUBLIC_BASE_URL"] = base

        _LOG.clear()
        try:
            _PROC = subprocess.Popen(
                # mcp_audio_server.py：在小智「音频资源」协议上跑工具层，
                # 工具结果会附带 resource_link，云端把音频分页读走推给设备。
                [sys.executable, "-u", "mcp_pipe.py", "mcp_audio_server.py"],
                cwd=str(_HERE),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            return False, f"起不来：{exc}"

        _ENDPOINT = endpoint
        _STARTED_AT = time.time()
        try:
            _EP_FILE.write_text(endpoint, encoding="utf-8")
        except OSError:
            pass
        threading.Thread(target=_pump, args=(_PROC.stdout,), daemon=True).start()
        return True, "已开始连接，等几秒看下面的日志"


def _status() -> dict:
    alive = bool(_PROC and _PROC.poll() is None)
    return {
        "running": alive,
        "connected": alive and _looks_connected(),
        "endpoint_tail": _ENDPOINT[-12:] if _ENDPOINT else "",
        "uptime": int(time.time() - _STARTED_AT) if alive and _STARTED_AT else 0,
        "base": stream_proxy.public_base(),
    }


def _learn_base(request: Request) -> None:
    host = (request.headers.get("host") or "").strip()
    if not host:
        return
    # 容器内部请求（自探活、平台健康检查走 127.0.0.1）不能覆盖已学到的
    # 公网域名，否则音频短链会指回 127.0.0.1，设备拉不到流。
    if host.lower().startswith(("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1")):
        return
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    base = f"{proto}://{host}"
    if stream_proxy.set_public_base(base):
        try:
            _BASE_FILE.write_text(base, encoding="utf-8")
        except OSError:
            pass


async def page(request: Request) -> HTMLResponse:
    _learn_base(request)
    return HTMLResponse(_PAGE)


async def api_connect(request: Request) -> JSONResponse:
    _learn_base(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ok, msg = start_bridge(str(body.get("endpoint") or ""))
    return JSONResponse({"ok": ok, "message": msg, "status": _status()})


async def api_disconnect(request: Request) -> JSONResponse:
    stop_bridge()
    forget_endpoint()
    return JSONResponse({"ok": True, "message": "已断开，重启后也不会自动重连", "status": _status()})


async def api_status(request: Request) -> JSONResponse:
    _learn_base(request)
    return JSONResponse({"status": _status(), "log": list(_LOG)[-60:]})


async def handle_health(request: Request) -> PlainTextResponse:
    _learn_base(request)
    st = _status()
    links = sum(1 for v in stream_proxy._ENTRIES.values() if v.expires > time.time())
    return PlainTextResponse(
        f"ok bridge={'up' if st['running'] else 'down'} "
        f"connected={int(st['connected'])} links={links} base={st['base']}"
    )


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小智收音机 · 云端控制台</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:#f6f6f4;color:#1c1c1a;line-height:1.6}
.wrap{max-width:680px;margin:0 auto;padding:40px 20px 60px}
h1{font-size:20px;font-weight:600;margin:0 0 6px}
.sub{font-size:13px;color:#6b6b64;margin:0 0 28px}
.card{background:#fff;border:1px solid rgba(0,0,0,.08);border-radius:12px;padding:20px;margin-bottom:16px}
label{display:block;font-size:13px;font-weight:500;margin-bottom:8px}
input{width:100%;padding:11px 12px;font-size:13px;font-family:ui-monospace,Menlo,monospace;
border:1px solid rgba(0,0,0,.14);border-radius:8px;background:#fafaf8;color:#1c1c1a}
input:focus{outline:2px solid #85b7eb;outline-offset:-1px;background:#fff}
.hint{font-size:12px;color:#8a8a82;margin:8px 0 0}
.row{display:flex;gap:10px;margin-top:14px;align-items:center;flex-wrap:wrap}
button{padding:10px 18px;font-size:13px;font-weight:500;border-radius:8px;border:1px solid transparent;
cursor:pointer;font-family:inherit}
.primary{background:#185fa5;color:#fff}
.primary:hover{background:#0c447c}
.ghost{background:#fff;border-color:rgba(0,0,0,.14);color:#3a3a36}
.ghost:hover{background:#f2f2ef}
.status{display:flex;align-items:center;gap:8px;font-size:13px;color:#3a3a36}
.dot{width:9px;height:9px;border-radius:50%;background:#b4b2a9;flex:none}
.dot.on{background:#639922}
.dot.wait{background:#ef9f27}
pre{margin:0;padding:14px;background:#1f1f1d;color:#d8d8d2;border-radius:8px;font-size:12px;
font-family:ui-monospace,Menlo,monospace;line-height:1.55;height:260px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.steps{font-size:13px;color:#3a3a36;margin:0;padding-left:18px}
.steps li{margin-bottom:6px}
code{background:#eeece6;padding:1px 5px;border-radius:4px;font-size:12px;
font-family:ui-monospace,Menlo,monospace}
</style>
</head>
<body>
<div class="wrap">
  <h1>小智收音机 · 云端控制台</h1>
  <p class="sub">这个服务 24 小时在云上跑，负责把你电台里的台和歌单变成小智能播的东西。</p>

  <div class="card">
    <label for="ep">小智 MCP 接入点</label>
    <input id="ep" placeholder="wss://api.xiaozhi.me/mcp/?token=..." autocomplete="off" spellcheck="false">
    <p class="hint">打开小智控制台 → 你的智能体 → 配置 → 扩展能力 → MCP 接入点，复制那一整行贴到这里。</p>
    <div class="row">
      <button class="primary" onclick="connect()">连接</button>
      <button class="ghost" onclick="disconnect()">断开</button>
      <div class="status"><span id="dot" class="dot"></span><span id="txt">未连接</span></div>
    </div>
  </div>

  <div class="card">
    <label>运行日志</label>
    <pre id="log">点「连接」之后这里会滚动出现日志。</pre>
  </div>

  <div class="card">
    <label>连上之后怎么用</label>
    <ol class="steps">
      <li>回到小智控制台的扩展能力页，刷新一下，能看到工具列表就说明通了。</li>
      <li>对小智说：「播放周杰伦」「放清晨电台」「下一曲」。</li>
      <li>设备要能拉音频，这个服务必须保持在线，别关。</li>
    </ol>
  </div>
</div>
<script>
let timer = null;
async function connect(){
  const ep = document.getElementById('ep').value.trim();
  if(!ep){ alert('先把接入点贴进来'); return; }
  setState('wait','正在连接…');
  const r = await fetch('api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:ep})});
  const j = await r.json();
  if(!j.ok){ setState('','失败'); alert(j.message); return; }
  localStorage.setItem('nif_ep', ep);
  poll();
}
async function disconnect(){
  await fetch('api/disconnect',{method:'POST'});
  setState('','未连接');
}
function setState(cls,text){
  document.getElementById('dot').className = 'dot ' + cls;
  document.getElementById('txt').textContent = text;
}
async function poll(){
  clearTimeout(timer);
  try{
    const r = await fetch('api/status');
    const j = await r.json();
    const s = j.status;
    document.getElementById('log').textContent = (j.log && j.log.length) ? j.log.join('\\n') : '还没有日志…';
    if(!s.running) setState('','未运行');
    else if(s.connected) setState('on','已连上小智 · 运行 ' + s.uptime + ' 秒');
    else setState('wait','桥接进程在跑，等待握手…');
  }catch(e){}
  timer = setTimeout(poll, 3000);
}
const saved = localStorage.getItem('nif_ep');
if(saved) document.getElementById('ep').value = saved;
poll();
</script>
</body>
</html>
"""

app = Starlette(
    routes=[
        Route("/", page),
        Route("/api/connect", api_connect, methods=["POST"]),
        Route("/api/disconnect", api_disconnect, methods=["POST"]),
        Route("/api/status", api_status, methods=["GET"]),
        Route("/health", handle_health, methods=["GET"]),
        Route("/p/{token}", stream_proxy.handle_stream, methods=["GET", "HEAD"]),
    ]
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    # 从磁盘捞上一次学到的域名，容器重启后不用等第一个请求
    try:
        if _BASE_FILE.exists():
            stream_proxy.set_public_base(_BASE_FILE.read_text(encoding="utf-8"))
    except OSError:
        pass

    auto = os.getenv("MCP_ENDPOINT", "").strip()
    if not auto:
        try:
            if _EP_FILE.exists():
                auto = _EP_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if auto:
        # 延迟 8 秒：给部署平台的探活请求留时间，先学到对外域名，
        # 这样桥接子进程一启动拿到的就是正确的短链前缀
        threading.Timer(8.0, lambda: start_bridge(auto)).start()

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=False)
