"""NIFradio 的数据层：把浏览器插件里的「电台列表 / 歌单搜索」搬到服务端。

接口来源是 NIFradio v3.3.0 的 popup.js，原样复用，不重新猜：
  - 电台：rapi.qtfm.cn（蜻蜓 FM）分类 -> 频道 -> lhttp.qtfm.cn 直播流
  - 歌单：api.jamendo.com/v3.0/tracks（开放版权曲库，audio 字段是签名 MP3 直链）

这一层不依赖 MCP，可以单独 import 做自测（见 selftest.py）。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

QTFM_BASE = "https://rapi.qtfm.cn"
LIVE_URL = "https://lhttp.qtfm.cn/live/{channel_id}/64k.mp3"
JAMENDO_BASE = "https://api.jamendo.com/v3.0"
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "fc4f9346")

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
_TIMEOUT = httpx.Timeout(10.0, connect=6.0)


async def _get(url: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _unwrap_qtfm(payload: Any) -> Any:
    """蜻蜓有两种响应外壳：{"Success":"ok","Data":...} 和 {"errcode":0,"data":...}。"""
    if isinstance(payload, dict):
        if payload.get("Success") == "ok":
            return payload.get("Data")
        if isinstance(payload.get("errcode"), int):
            return payload.get("data") if payload["errcode"] == 0 else None
    return payload


def _as_list(data: Any, *keys: str) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------- 电台

async def get_categories(limit: int = 20) -> list[dict]:
    raw = _as_list(
        _unwrap_qtfm(await _get(f"{QTFM_BASE}/categories", {"type": "channel"})),
        "categories", "list",
    )
    out: list[dict] = []
    for item in raw:
        cid = str(item.get("id") or "").strip()
        name = (item.get("title") or item.get("name") or "").strip()
        if cid and name:
            out.append({"id": cid, "name": name})
    return out[:limit]


async def get_channels(category_id: str | int, page: int = 1, size: int = 10) -> list[dict]:
    data = await _get(
        f"{QTFM_BASE}/categories/{category_id}/channels",
        {"page": max(1, page), "pagesize": max(1, min(size, 50))},
    )
    raw = _as_list(_unwrap_qtfm(data), "channels", "Data", "list")
    out: list[dict] = []
    for item in raw:
        cid = str(item.get("content_id") or item.get("id") or "").strip()
        if not cid:
            continue
        now = item.get("nowplaying") or {}
        out.append({
            "id": cid,
            "name": (item.get("title") or item.get("name") or "未知电台").strip(),
            "now_playing": (now.get("title") or now.get("name") or "").strip(),
            "stream_url": LIVE_URL.format(channel_id=cid),
        })
    return out


async def search_stations(keyword: str, size: int = 5) -> list[dict]:
    """蜻蜓没有公开搜索接口，改成在各分类里并发抓一轮、按台名过滤。"""
    keyword = keyword.strip()
    if not keyword:
        return []

    async def scan(cat: dict) -> tuple[dict, list[dict]]:
        try:
            return cat, await get_channels(cat["id"], 1, 50)
        except Exception:
            return cat, []

    results = await asyncio.gather(*(scan(c) for c in await get_categories()))
    hits: list[dict] = []
    seen: set[str] = set()
    for cat, channels in results:
        for ch in channels:
            if keyword in ch["name"] and ch["id"] not in seen:
                seen.add(ch["id"])
                hits.append({**ch, "category": cat["name"]})
                if len(hits) >= size:
                    return hits
    return hits


# ---------------------------------------------------------------- 歌单

def _track_row(track: dict) -> dict:
    return {
        "id": str(track.get("id") or ""),
        "name": (track.get("name") or "").strip(),
        "artist": (track.get("artist_name") or "").strip(),
        "album": (track.get("album_name") or "").strip(),
        "duration": int(track.get("duration") or 0),
        "url": (track.get("audio") or "").strip(),
    }


async def _jamendo_tracks(**params: Any) -> list[dict]:
    payload = await _get(
        f"{JAMENDO_BASE}/tracks/",
        {"client_id": JAMENDO_CLIENT_ID, "format": "json", **params},
    )
    if not isinstance(payload, dict):
        return []
    header = payload.get("headers") or {}
    status = header.get("status")
    if status and status != "success":
        raise RuntimeError(header.get("error_message") or "Jamendo 请求失败")
    return [x for x in (payload.get("results") or []) if isinstance(x, dict)]


async def search_tracks(keyword: str, limit: int = 5) -> list[dict]:
    rows = await _jamendo_tracks(search=keyword.strip(), limit=max(1, min(limit, 20)))
    return [r for r in (_track_row(t) for t in rows) if r["url"]]


async def get_track(track_id: str) -> dict | None:
    """按 ID 重新解析直链：Jamendo 的 audio 是签名 URL，会过期。"""
    rows = await _jamendo_tracks(id=str(track_id).strip(), limit=1)
    for track in rows:
        row = _track_row(track)
        if row["url"]:
            return row
    return None
