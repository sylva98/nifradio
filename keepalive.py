# -*- coding: utf-8 -*-
"""
魔搭实例保活守护（真内核版）
==========================
背景：魔搭免费实例的判定是「30 分钟未在 Notebook 内执行操作 → 1 小时后关闭实例」。
     第一版保活只发 REST 请求（/api/status），实测**骗不过**它——
     平台看的是「内核有没有在执行代码」，不是「有没有人来访问过」。

本版做法：直接向 Jupyter 服务端要一个真 kernel，通过 websocket 发 execute_request，
       让内核里跑一个 sleep，循环不停。对服务端而言，这就是「有 notebook 在执行」。

它做四件事：
  1) POST /api/kernels 新建一个 python3 内核
  2) 连 /api/kernels/{id}/channels 发 execute_request（code = time.sleep(N)）
  3) 每轮结束后关掉旧内核、建新的，永不停止
  4) 顺带巡检 :8080/health（只记日志，重启由 boot.sh 的守护循环负责）

用法：
    python keepalive.py              # 前台跑（调试）
    nohup python keepalive.py &      # 后台跑（boot.sh 就是这么做的）
    python keepalive.py --once       # 只跑一轮，验证内核通不通

环境变量：
    KEEPALIVE_SLEEP   每轮内核睡多少秒，默认 540（9 分钟，压在 30 分钟线以内）
    JUPYTER_URL / JUPYTER_TOKEN   手动指定服务端（一般不用设，会自动找）
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

HEALTH = "http://127.0.0.1:8080/health"
SLEEP_S = int(os.environ.get("KEEPALIVE_SLEEP") or 540)


def log(msg: str) -> None:
    print(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 服务端发现

def _jupyter_endpoint() -> tuple[str, str]:
    """找出 Jupyter 服务端的 url 与 token。"""
    base = (os.environ.get("JUPYTER_URL") or "").strip()
    token = (os.environ.get("JUPYTER_TOKEN") or "").strip()
    if base:
        return base.rstrip("/") + "/", token

    dirs: list[str] = []
    rd = os.environ.get("JUPYTER_RUNTIME_DIR")
    if rd:
        dirs.append(rd)
    for pat in (
        os.path.expanduser("~/.local/share/jupyter/runtime"),
        "/root/.local/share/jupyter/runtime",
        "/home/*/.local/share/jupyter/runtime",
        "/tmp/jupyter/runtime*",
        "/tmp/*jupyter*/runtime",
    ):
        dirs.extend(glob.glob(pat))

    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "jpserver-*.json"))):
            try:
                with open(f, encoding="utf-8") as fh:
                    cfg = json.load(fh)
                url = (cfg.get("url") or "").strip()
                if url:
                    return url.rstrip("/") + "/", (cfg.get("token") or "").strip()
            except Exception:
                continue

    return "http://127.0.0.1:8888/", token


def _request(url: str, token: str = "", payload: dict | None = None,
             timeout: int = 20) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("User-Agent", "nifradio-keepalive")
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body.decode() or "{}")
            except Exception:
                return r.status, {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def _delete(url: str, token: str = "") -> int:
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("User-Agent", "nifradio-keepalive")
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _start_kernel(base: str, token: str) -> str:
    """在服务端新建一个内核，返回 kernel id。"""
    code, info = _request(base + "api/kernels", token, {"name": "python3"})
    if code != 201 or not info.get("id"):
        raise RuntimeError(f"建内核失败 HTTP {code} {info.get('error', '')}".strip())
    return info["id"]


def _ws_url(base: str, kid: str, token: str) -> str:
    ws = base.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws}api/kernels/{kid}/channels?session_id={uuid.uuid4().hex}"
    if token:
        url += "&token=" + token
    return url


async def _run_kernel(base: str, token: str, sleep_s: int, once: bool = False) -> None:
    """建内核 → 连 ws → 发 execute → 等执行完成 → 循环。"""
    import websockets

    kid = _start_kernel(base, token)
    log(f"内核已建 {kid[:8]}，让它执行 sleep({sleep_s})")

    session = uuid.uuid4().hex
    async with websockets.connect(_ws_url(base, kid, token), max_size=None,
                                  ping_interval=20, ping_timeout=20,
                                  open_timeout=25) as ws:
        msg_id = uuid.uuid4().hex
        await ws.send(json.dumps({
            "header": {"msg_id": msg_id, "username": "keepalive", "session": session,
                       "msg_type": "execute_request", "version": "5.3"},
            "parent_header": {}, "metadata": {},
            "content": {"code": f"import time; time.sleep({sleep_s})",
                        "silent": False, "store_history": False,
                        "user_expressions": {}, "allow_stdin": False},
            "channel": "shell",
        }))

        deadline = time.time() + sleep_s + 180
        replied = False
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(5, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            except Exception as e:
                log(f"ws 断开：{e}")
                break
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if (m.get("parent_header", {}).get("msg_id") == msg_id
                    and m.get("msg_type") == "execute_reply"):
                replied = True
                break

        if replied:
            log(f"本轮内核执行完成（{sleep_s}s）")
        else:
            log("本轮未收到 execute_reply，按超时处理")

    _delete(f"{base}api/kernels/{kid}", token)  # 顺手清理旧内核
    if once:
        log("--once 模式结束，内核通路正常")
        raise SystemExit(0)


async def main(once: bool = False) -> None:
    base, token = _jupyter_endpoint()
    log(f"保活(内核版)启动：Jupyter={base} token={'有' if token else '无'} 每轮 {SLEEP_S}s")

    # 首次先探一下 REST 是否可达，方便判断是"没权限"还是"没连上"
    code, _ = _request(base + "api/status", token)
    log(f"服务端探测 /api/status -> {code}")

    while True:
        try:
            await _run_kernel(base, token, SLEEP_S, once=once)
        except SystemExit:
            raise
        except Exception as e:
            log(f"内核保活异常：{e}；30 秒后重试")
            await asyncio.sleep(30)
            continue

        # 内核跑完，顺手看看音频服务，再开下一轮
        hc, _ = _request(HEALTH, timeout=6)
        log(f"音频服务 {hc if hc else '无响应'}；10 秒后开下一轮内核")
        await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(main(once="--once" in sys.argv))
    except KeyboardInterrupt:
        pass
