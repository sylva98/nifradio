"""小智音频资源协议入口（STDIO JSON-RPC）。

背景
----
小智云端（xiaozhi.me）的 MCP 客户端支持「音频资源」播放：工具调用结果里除了
文本，还可以带一个 `resource_link`，指向 `resource://read_from_http`；云端随后
用 `resources/read` 按 100KB 分页（arguments: url/start/end）把音频读走，
读完后由云端解码、推到设备喇叭。因为音频是**服务端代替设备去取**的，
所以 B 站 CDN 的 Referer 限制在这里解决，设备端什么都不用改。

协议要点（与社区 xiaozhi-mcp-music 一致）：
  play_song 结果 → [text, {"type":"resource_link", "uri":"resource://read_from_http",
                           "description": "<真实音频地址>", "mimeType": "audio/mpeg"}]
  云端 → resources/read {uri: "resource://read_from_http",
                        arguments: {url, start, end}}
  本服务 → {"contents":[{"uri":..., "mimeType":..., "blob": "<base64 音频块>"}]}
  流末尾 → blob 为 base64("[DONE]")

工具层仍然复用 radio_mcp 里的 FastMCP 定义（schema、docstring 都不用重写），
这里只负责传输层与资源层。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import traceback
from typing import Any

import httpx

RES_URI = "resource://read_from_http"
PAGE = 102400  # 100KB，与小智 Go 服务端示例一致
DONE = base64.b64encode(b"[DONE]").decode()

_UA = "ESP32-HTTPClient/1.0"
_REFERER = "https://www.bilibili.com/"
_LOCALE = {"Content-Type": "application/json; charset=utf-8"}


def _mime_for(url: str) -> str:
    low = url.lower().split("?")[0]
    if low.endswith((".m4s", ".m4a", ".mp4")):
        return "audio/mp4"
    return "audio/mpeg"


def _fetch_headers(url: str, start: int, end: int) -> dict[str, str]:
    headers = {"User-Agent": _UA, "Range": f"bytes={start}-{end}"}
    # B 站 CDN 不带 Referer 会 403；其它源带上也无害
    if "bilivideo" in url or "bilibili" in url:
        headers["Referer"] = _REFERER
        headers["Origin"] = "https://www.bilibili.com"
    return headers


async def _read_page(url: str, start: int, end: int) -> tuple[bytes, bool]:
    """取 [start, end] 区间；返回 (数据, 是否已到末尾)。"""
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(url, headers=_fetch_headers(url, start, end))

    if resp.status_code == 416:  # Range 越界 = 已经读完
        return b"", True
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")

    body = resp.content
    content_range = resp.headers.get("content-range", "")
    total: int | None = None
    if "/" in content_range:
        try:
            total = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            total = None

    if not content_range and resp.status_code == 200:
        # 源站不支持 Range：整包回来了，自己切片
        total = len(body)
        body = body[start:end + 1]

    eof = bool(body) is False or (total is not None and end + 1 >= total)
    return body, eof


class Server:
    def __init__(self) -> None:
        os.environ.setdefault("STREAM_PROXY", "0")
        import radio_mcp  # 延迟导入：让 env 先生效

        self.radio = radio_mcp
        self.fastmcp = radio_mcp.mcp
        self._tools: list[dict[str, Any]] | None = None

    async def tool_list(self) -> list[dict[str, Any]]:
        if self._tools is None:
            tools = await self.fastmcp.list_tools()
            self._tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema,
                }
                for t in tools
            ]
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.fastmcp.call_tool(name, arguments or {})
        if isinstance(result, tuple):  # (content, structured)
            result = result[0]
        if isinstance(result, dict):
            blocks: list[dict[str, Any]] = [result]
        else:
            blocks = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)
                      for b in result]

        # 文本里恰好有一个 http 链接 → 附上音频资源，让云端拉到设备上播
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        urls = [w for w in text.split() if w.startswith(("http://", "https://"))]
        if len(urls) == 1:
            blocks.append({
                "type": "resource_link",
                "uri": RES_URI,
                "name": text.strip().splitlines()[0][:40] if text.strip() else "音频",
                "description": urls[0],
                "mimeType": _mime_for(urls[0]),
            })
        return {"content": blocks}

    async def read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri", "")
        if uri != RES_URI:
            raise ValueError(f"unknown resource {uri}")
        args = params.get("arguments") or {}
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("arguments.url 缺失")
        start = int(args.get("start") or 0)
        end = int(args.get("end") or (start + PAGE - 1))

        data, _eof = await _read_page(url, start, end)
        blob = base64.b64encode(data).decode() if data else DONE
        return {
            "contents": [{
                "uri": RES_URI,
                "mimeType": _mime_for(url),
                "blob": blob,
            }]
        }

    async def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._ok(msg_id, {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": "NIFradio", "version": "0.2.0"},
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._ok(msg_id, {})
        if method in ("tools/list", "mcp/tools/list"):
            return self._ok(msg_id, {"tools": await self.tool_list()})
        if method in ("tools/call", "mcp/tools/call"):
            return self._ok(msg_id, await self.call_tool(
                str(params.get("name") or ""), params.get("arguments") or {}))
        if method == "resources/list":
            return self._ok(msg_id, {"resources": [{
                "uri": RES_URI,
                "name": "音频流代理",
                "description": "按 HTTP Range 分页读取远程音频，供小智把音乐推给设备",
                "mimeType": "audio/mpeg",
            }]})
        if method == "resources/read":
            return self._ok(msg_id, await self.read_resource(params))
        if msg_id is None:  # 其余通知直接忽略
            return None
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    @staticmethod
    def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def serve() -> None:
    server = Server()
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            reply = await server.handle(msg)
        except Exception as exc:  # 单个请求出错不要拖垮整个进程
            traceback.print_exc(file=sys.stderr)
            if msg.get("id") is not None:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32000, "message": str(exc)}}
            else:
                reply = None
        if reply is not None:
            _write(reply)


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
