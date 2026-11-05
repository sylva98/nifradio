#!/usr/bin/env python3
"""MCP 协议自检：模拟小智云端，把我们自己的服务端完整跑一遍。

背景
----
实机「说播放周杰伦没反应」时，我们无法判断是「小智没调工具」还是「调了但我们这边
返回的东西不对」。这个脚本扮成云端客户端，用 JSON-RPC 直连 mcp_audio_server.py，
把 初始化 → tools/list → tools/call → resources/read 四步走完，逐项报通过/失败。

用法
----
    .venv/bin/python selfcheck.py            # 只测试音（3 秒 440Hz，不依赖外网音源）
    .venv/bin/python selfcheck.py 周杰伦      # 再测一遍点歌（会真连 B 站）

诊断日志写进 /tmp/selfcheck_diag.jsonl，不污染 .cache/diag.jsonl ——
那个文件是「小智到底来没来」的证据，不能被自检搅浑。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = 102400
TIMEOUT = 150

OK = "\033[32m✓\033[0m"
NO = "\033[31m✗\033[0m"
_WARN: list[str] = []


def ok(msg: str) -> None:
    print(f"  {OK} {msg}")


def bad(msg: str) -> None:
    print(f"  {NO} {msg}")
    _WARN.append(msg)


def sniff(data: bytes) -> str:
    if not data:
        return "空"
    if data[:3] == b"ID3":
        return "MP3(ID3)"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "MP3(帧同步)"
    if data[:4] == b"ftyp":
        return "MP4/AAC"
    if data[:4] == b"OggS":
        return "OGG"
    if data[:4] == b"fLaC":
        return "FLAC"
    return f"未知({data[:4].hex()})"


class Client:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.n = 0

    async def send(self, method: str, params: dict | None = None) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        self.n += 1
        if params is not None:
            msg["params"] = params
        notify = method.startswith("notifications/")
        if not notify:
            msg["id"] = self.n
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()
        if notify:
            return None
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), TIMEOUT)
            if not line:
                raise RuntimeError("服务端提前关闭了连接")
            text = line.decode(errors="replace").strip()
            try:
                resp = json.loads(text)
            except Exception:
                continue  # 忽略混进来的非 JSON 日志行
            if resp.get("id") != self.n:
                continue
            if "error" in resp:
                raise RuntimeError(f"{method} 报错: {resp['error']}")
            return resp.get("result")


async def read_first_page(c: Client, link: dict) -> bytes:
    res = await c.send("resources/read", {
        "uri": link["uri"],
        "arguments": {"url": link["description"], "start": 0, "end": PAGE - 1},
    })
    blob = (res or {}).get("contents", [{}])[0].get("blob", "")
    return base64.b64decode(blob) if blob else b""


async def check_tool(c: Client, name: str, args: dict, label: str) -> bool:
    print(f"\n[{label}] tools/call {name}")
    try:
        res = await c.send("tools/call", {"name": name, "arguments": args})
    except Exception as exc:
        bad(f"调用失败：{exc}")
        return False
    blocks = (res or {}).get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    link = next((b for b in blocks if b.get("type") == "resource_link"), None)
    if text:
        ok(f"文本返回：{text.strip()[:70]}")
    else:
        bad("没有文本返回")
    if not link:
        bad("没有 resource_link —— 云端拿不到音频地址")
        return False
    ok(f"resource_link uri={link['uri']} mime={link.get('mimeType')}")
    ok(f"音频地址：{link.get('description', '')[:70]}")
    if link.get("mimeType") != "audio/mpeg":
        bad(f"mimeType 应为 audio/mpeg，实际 {link.get('mimeType')}")
    data = await read_first_page(c, link)
    if not data:
        bad("resources/read 第一页是空的")
        return False
    kind = sniff(data)
    ok(f"第一页 {len(data)} 字节，格式 {kind}")
    if kind.startswith("MP3"):
        ok("格式正确：云端拿到的是 MP3")
    else:
        bad(f"格式异常：{kind}（云端可能解不出声）")
    return True


async def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    env = dict(os.environ)
    env["DIAG_FILE"] = "/tmp/selfcheck_diag.jsonl"
    env.setdefault("STREAM_PROXY", "0")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "mcp_audio_server.py",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(HERE), env=env,
        # 一页音频 base64 后有 137KB，是单行 JSON —— 默认 64KB 管道上限会直接炸
        limit=4 * 1024 * 1024,
    )
    c = Client(proc)
    try:
        print("[1] initialize")
        r = await c.send("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "selfcheck", "version": "1.0"},
        })
        ok(f"握手成功，serverInfo={r.get('serverInfo')}")
        await c.send("notifications/initialized")

        print("\n[2] tools/list")
        tools = (await c.send("tools/list") or {}).get("tools", [])
        names = [t["name"] for t in tools]
        ok(f"注册 {len(names)} 个工具：{', '.join(names[:6])}…")
        for must in ("play_song", "play_station", "selftest_audio"):
            if must in names:
                ok(f"工具存在：{must}")
            else:
                bad(f"缺少工具：{must}")
        if "play_music" in names:
            bad("居然有 play_music —— 与人格提示词重名，要理清到底谁调谁")

        print("\n[3] 试音（不依赖外网）")
        await check_tool(c, "selftest_audio", {}, "试音")

        if keyword:
            print(f"\n[4] 点歌「{keyword}」（真连 B 站）")
            await check_tool(c, "play_song", {"keyword": keyword}, "点歌")
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except Exception:
            proc.kill()

    print("\n" + "=" * 46)
    if _WARN:
        print(f"结果：{len(_WARN)} 项不通过 —— 我们这侧还有问题")
        for w in _WARN:
            print(f"  - {w}")
        return 1
    print("结果：全部通过 —— 我们这侧随时可以被调用")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
