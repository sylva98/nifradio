# -*- coding: utf-8 -*-
"""
魔搭实例保活守护（无需 notebook 单元格）
=====================================
背景：魔搭免费实例的规则是「30 分钟未在 notebook 内执行操作 → 1 小时后关闭实例」。
本进程由 boot.sh 拉起，常驻后台，每 10 分钟向 Jupyter 服务端发一次请求，
让服务端的活动时间戳持续刷新，从而不触发静默关机判定。

它做三件事：
  1) 每 10 分钟请求一次 Jupyter 服务端 API（activity 保活）
  2) 同时检查音频服务 :8080/health，异常只记日志（server 由 boot.sh 的守护循环负责重启）
  3) 把结果写进 keepalive.log，方便排查

用法：
    python keepalive.py            # 前台跑（调试用）
    nohup python keepalive.py &    # 后台跑（boot.sh 就是这么做的）
"""

from __future__ import annotations

import glob
import json
import os
import time
import urllib.error
import urllib.request

HEALTH = "http://127.0.0.1:8080/health"
INTERVAL = 600  # 秒；10 分钟，压在 30 分钟静默线以内


def log(msg: str) -> None:
    print(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


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


def _get(url: str, token: str = "", timeout: int = 8) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "nifradio-keepalive"})
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code  # 401/403 也算"请求到达服务端"，活动时间戳已刷新
    except Exception:
        return 0


def main() -> None:
    base, token = _jupyter_endpoint()
    log(f"保活启动：Jupyter={base} token={'有' if token else '无'}，间隔 {INTERVAL}s")
    while True:
        code_api = _get(base + "api/status", token)
        code_health = _get(HEALTH)
        if code_health == 200:
            log(f"心跳正常  jupyter={code_api}  音频服务=200")
        else:
            log(f"心跳  jupyter={code_api}  音频服务={code_health or '无响应'}（server 由 boot.sh 守护循环重启）")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
