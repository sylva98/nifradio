"""短链 + 音频流代理。

为什么必须有这一层（三个问题一次解决）：

1. B站的音频直链是 DASH（fMP4 封装的 AAC），必须带 `Referer: bilibili.com`
   才给数据，而小智设备拉流时不会带任何 Referer —— 由代理替它补；
2. 那些直链长 750 字符以上，而小智一次能读到的 MCP 返回约 1024 字节，
   一条长链就把预算吃光，模型还容易把长串传错 —— 换成 8 字符短链；
3. 直链带 deadline，约两小时过期 —— 映射里记着 bvid，过期时自动重新解析，
   设备拿到的短链一直不变。

运行时只占内存，不需要数据库。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("nifradio.proxy")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 透传给设备的响应头，其余丢掉，避免把 CDN 的私有头带出去
_PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")

_REFRESH_AHEAD = 120.0  # 提前两分钟续期，别等设备请求时才现算


@dataclass
class Entry:
    url: str
    referer: str
    expires: float
    bvid: str = ""
    kind: str = "bili"
    refreshing: asyncio.Lock = field(default_factory=asyncio.Lock)


_ENTRIES: dict[str, Entry] = {}
_LOCK = asyncio.Lock()


async def _resolve_bvid(bvid: str) -> dict:
    """延迟 import，避免 bili_core 反过来依赖本模块造成循环。"""
    from bili_core import bili

    return await bili.resolve_audio(bvid)


async def _ensure_fresh(token: str, entry: Entry) -> bool:
    """快过期时重新解析一次真实地址。返回是否可用。"""
    if time.time() < entry.expires - _REFRESH_AHEAD:
        return True
    if not entry.bvid:
        return time.time() < entry.expires
    async with entry.refreshing:
        if time.time() < entry.expires - _REFRESH_AHEAD:
            return True
        try:
            info = await _resolve_bvid(entry.bvid)
        except Exception as exc:
            logger.warning("短链 %s 续期失败：%s", token, exc)
            return time.time() < entry.expires
        entry.url = info["url"]
        entry.expires = info["expires"]
        entry.referer = info.get("referer") or entry.referer
        logger.info("短链 %s 已续期，余 %.1f 小时", token, (entry.expires - time.time()) / 3600)
        return True


async def create_link(
    *, url: str, referer: str = "", expires: float = 0.0, bvid: str = "", kind: str = "bili"
) -> str:
    """登记一条短链，返回 token。"""
    token = secrets.token_urlsafe(6)
    entry = Entry(
        url=url,
        referer=referer,
        expires=expires or (time.time() + 3600),
        bvid=bvid,
        kind=kind,
    )
    async with _LOCK:
        _ENTRIES[token] = entry
        if len(_ENTRIES) > 2000:
            now = time.time()
            for key in [k for k, v in _ENTRIES.items() if v.expires < now - 600][:1000]:
                _ENTRIES.pop(key, None)
    return token


_LEARNED_BASE = ""


def set_public_base(base: str) -> bool:
    """部署到云上时域名事先不知道，从第一个进来的请求里学一次。

    返回 True 表示这次学到了新域名（调用方可据此打日志）。
    """
    global _LEARNED_BASE
    base = (base or "").strip().rstrip("/")
    if not base or base == _LEARNED_BASE:
        return False
    _LEARNED_BASE = base
    logger.info("对外地址已确定：%s", base)
    return True


def public_base() -> str:
    import os

    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    if _LEARNED_BASE:
        return _LEARNED_BASE
    port = os.getenv("PORT", "8080").strip() or "8080"
    return f"http://127.0.0.1:{port}"


async def link_for_bvid(bvid: str) -> tuple[str, dict]:
    """解析 bvid 并直接生成短链，返回 (完整短链, 音频信息)。"""
    info = await _resolve_bvid(bvid)
    token = await create_link(
        url=info["url"],
        referer=info.get("referer", "https://www.bilibili.com/"),
        expires=info["expires"],
        bvid=bvid,
    )
    return f"{public_base()}/p/{token}", info


async def handle_stream(request):
    token = request.path_params["token"]
    entry = _ENTRIES.get(token)
    if not entry:
        return PlainTextResponse(
            "这个播放链接已失效，请重新点一次歌。", status_code=404
        )
    if not await _ensure_fresh(token, entry):
        return PlainTextResponse("链接已过期且续期失败，请重新点一次歌。", status_code=410)

    headers = {"User-Agent": _UA, "Referer": entry.referer, "Accept": "*/*"}
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng

    if request.method == "HEAD":
        return Response(
            status_code=200,
            headers={"Content-Type": "audio/mp4", "Accept-Ranges": "bytes"},
        )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0, read=30.0), follow_redirects=True
    )
    try:
        upstream = client.build_request("GET", entry.url, headers=headers)
        resp = await client.send(upstream, stream=True)
    except Exception as exc:
        await client.aclose()
        logger.warning("短链 %s 拉取上游失败：%s", token, exc)
        return PlainTextResponse(f"上游取流失败：{exc}", status_code=502)

    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        logger.warning("短链 %s 上游 %s：%s", token, resp.status_code, body[:120])
        return PlainTextResponse(
            f"上游返回 {resp.status_code}", status_code=resp.status_code
        )

    out_headers = {}
    for name in _PASS_HEADERS:
        if name in resp.headers:
            out_headers[name] = resp.headers[name]
    # 强制覆盖 Content-Type：B 站的 m4s 经常回 application/octet-stream，
    # 设备拿到这个类型会当成未知二进制直接拒绝播；必须明确告诉它这是音频。
    out_headers["content-type"] = "audio/mp4"
    out_headers["accept-ranges"] = "bytes"

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        except Exception as exc:
            logger.info("短链 %s 传输中断：%s", token, exc)
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers)


async def handle_health(request):
    now = time.time()
    alive = sum(1 for v in _ENTRIES.values() if v.expires > now)
    return PlainTextResponse(f"ok links={len(_ENTRIES)} alive={alive}")


app = Starlette(
    routes=[
        Route("/p/{token}", handle_stream, methods=["GET", "HEAD"]),
        Route("/health", handle_health, methods=["GET"]),
    ]
)


def run_in_thread(port: int | None = None) -> None:
    """给 radio_mcp 用：把代理塞进后台线程，和 MCP stdio 共处一个进程。"""
    import os
    import threading

    import uvicorn

    listen_port = port or int(os.getenv("PORT", "8080"))

    def _serve() -> None:
        # log_config=None + access_log=False 很关键：MCP 走的是 stdio，
        # uvicorn 默认会把访问日志打到 stdout，混进去会直接破坏 JSON-RPC 帧。
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=listen_port,
            log_level="critical",
            access_log=False,
            log_config=None,
        )

    threading.Thread(target=_serve, name="stream-proxy", daemon=True).start()
    logger.info("短链代理已启动，监听 0.0.0.0:%s", listen_port)
