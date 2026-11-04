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
import time
import traceback
from typing import Any

import diag
import media

RES_URI = "resource://read_from_http"
PAGE = 102400  # 100KB，与小智 Go 服务端示例一致
DONE = base64.b64encode(b"[DONE]").decode()

# 按 URL 累计「这次播放总共读了几页、多少字节」，读完打一条汇总，
# 用来判断云端是「读完了」还是「读了两页就放弃」。
_READS: dict[str, dict[str, Any]] = {}


def _short_url(url: str) -> str:
    base = url.split("?")[0]
    return base if len(base) <= 64 else base[:61] + "…"


def _short_args(arguments: dict[str, Any]) -> str:
    try:
        text = json.dumps(arguments, ensure_ascii=False)
    except Exception:
        text = str(arguments)
    return text if len(text) <= 60 else text[:57] + "…"


def _magic(data: bytes) -> str:
    if not data:
        return "-"
    return data[:4].hex()


def _mime_for(url: str) -> str:
    """对外统一报 audio/mpeg。

    m4s（fMP4/AAC）和 .m3u8 都由 media.py 用 ffmpeg 转成 MP3 再分页，
    所以不管源是什么格式，云端拿到的字节流都是 MP3 —— 报 audio/mpeg
    既准确，也避开了设备对 audio/mp4 支持不明的问题。
    """
    return "audio/mpeg"


async def _read_page(url: str, start: int, end: int) -> tuple[bytes, bool]:
    """取 [start, end] 区间；返回 (数据, 是否已到末尾)。

    由 media.py 决定透传还是转码（m4s / m3u8 → MP3）。
    """
    return await media.read_page(url, start, end)


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
        started = time.monotonic()
        try:
            result = await self.fastmcp.call_tool(name, arguments or {})
        except Exception as exc:
            diag.log("tool_error", tool=name, args=_short_args(arguments or {}),
                     err=f"{type(exc).__name__}: {exc}")
            raise
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
            # 提前起转码：等云端来分页读时首屏已有数据，省掉几秒等待
            asyncio.create_task(media.warm(urls[0]))

        diag.log(
            "tool",
            tool=name,
            args=_short_args(arguments or {}),
            ms=int((time.monotonic() - started) * 1000),
            text=text.strip().replace("\n", " / ")[:110],
            url=_short_url(urls[0]) if len(urls) == 1 else "",
        )
        if len(urls) == 1:
            asyncio.create_task(self._watch_taken(urls[0], name))
        return {"content": blocks}

    @staticmethod
    async def _watch_taken(url: str, tool: str, wait: int = 8) -> None:
        """工具返回后盯 8 秒：云端没来取音频就记一条告警。

        「小智说自己在放歌，但没声音」最常见的成因就是云端只念了文本、
        没走 resources/read —— 这条记录能直接把它和「取到了但播不出」分开。
        """
        await asyncio.sleep(wait)
        stat = _READS.get(url)
        if not stat or stat["pages"] == 0:
            diag.log("no_read", url=_short_url(url), tool=tool, wait=wait)

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

        try:
            data, _eof = await _read_page(url, start, end)
        except Exception as exc:
            diag.log("read_err", url=_short_url(url),
                     err=f"{type(exc).__name__}: {exc}")
            raise

        diag.log("read", url=_short_url(url), page=start // PAGE + 1,
                 start=start, end=end, n=len(data), eof=_eof, head=_magic(data))

        stat = _READS.get(url)
        if stat is None:
            stat = {"pages": 0, "bytes": 0, "t0": time.monotonic()}
            _READS[url] = stat
        stat["pages"] += 1
        stat["bytes"] += len(data)
        if _eof:
            diag.log("read_end", url=_short_url(url), pages=stat["pages"],
                     bytes=stat["bytes"],
                     secs=round(time.monotonic() - stat["t0"], 1))
            _READS.pop(url, None)

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
