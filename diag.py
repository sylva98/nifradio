"""诊断日志：把「云端到底来没来、读了几页、读到什么」记下来。

为什么要单独一层
----------------
小智那边的失败对我们是黑盒：用户只听到「没声」或者一句含糊的话，
我们看不到它有没有调工具、有没有来分页读音频、读了几页就放弃。

所以把关键动作落成 JSONL（一行一个事件），两个进程都往里写：
  * server.py            —— 父进程，提供 /api/diag 给人看
  * mcp_audio_server.py  —— 子进程，真正处理 tools/call 与 resources/read

文件放在 .cache/diag.jsonl（/mnt/workspace 持久盘），超过 2MB 自动滚一份 .1。
只记摘要，不记音频内容，也不记 token。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent / ".cache"
_FILE = Path(os.environ.get("DIAG_FILE") or (_DIR / "diag.jsonl"))
MAX_BYTES = 2 * 1024 * 1024
KEEP_LINES = 400


def _rotate_if_big() -> None:
    try:
        if _FILE.exists() and _FILE.stat().st_size > MAX_BYTES:
            _FILE.replace(_FILE.with_suffix(".jsonl.1"))
    except OSError:
        pass


def log(kind: str, **fields: Any) -> None:
    """追加一条事件。任何异常都吞掉——诊断日志不能影响主流程。"""
    rec = {"t": time.strftime("%m-%d %H:%M:%S"), "k": kind}
    for k, v in fields.items():
        if isinstance(v, str) and len(v) > 300:
            v = v[:297] + "…"
        rec[k] = v
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_big()
        with _FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent(limit: int = 60) -> list[str]:
    """读最近若干条，格式化成给人看的单行文本（新的在后）。"""
    try:
        lines = _FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for raw in lines[-limit:]:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        out.append(format_event(rec))
    return out


def format_event(rec: dict) -> str:
    kind = rec.get("k", "?")
    t = rec.get("t", "")
    if kind == "tool":
        return (f"{t} 工具 {rec.get('tool')} {rec.get('ms')}ms "
                f"参数={rec.get('args')} 返回={rec.get('text')}")
    if kind == "tool_error":
        return f"{t} 工具 {rec.get('tool')} 报错 {rec.get('err')}"
    if kind == "read":
        tail = " 末页" if rec.get("eof") else ""
        n = rec.get("n") or 0
        return (f"{t} 取音频 第{rec.get('page')}页 {n}B "
                f"头={rec.get('head')}{tail} ← {rec.get('url')}")
    if kind == "read_err":
        return f"{t} 取音频失败 {rec.get('err')} ← {rec.get('url')}"
    if kind == "read_end":
        return (f"{t} 读完 {rec.get('pages')}页 共{rec.get('bytes')}B "
                f"（{(rec.get('bytes') or 0) / 1024 / 1024:.1f}MB，"
                f"{rec.get('secs')}s）← {rec.get('url')}")
    if kind == "no_read":
        return (f"{t} ⚠ {rec.get('wait')}秒内云端没来取音频"
                f"（工具 {rec.get('tool')}）← {rec.get('url')}")
    if kind == "selftest":
        return f"{t} 试音 {rec.get('state')} {rec.get('detail') or ''}"
    rest = " ".join(f"{k}={v}" for k, v in rec.items() if k not in ("t", "k"))
    return f"{t} {kind} {rest}"


def tail_text(limit: int = 60) -> str:
    lines = recent(limit)
    return "\n".join(lines) if lines else "（暂无记录）"
