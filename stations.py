"""用户自己的电台表（就是 NIFradio 浏览器插件用的那几张 gitee JSON）。

表的位置与插件同源，你在 gitee 上改完，云端下一次查询就生效（10 分钟内缓存）：

    https://gitee.com/nif99/niffm/raw/master/data/01cnmusic.json   华语音乐
    https://gitee.com/nif99/niffm/raw/master/data/02ommusic.json   欧美音乐
    https://gitee.com/nif99/niffm/raw/master/data/03cnnews.json    华语新闻
    https://gitee.com/nif99/niffm/raw/master/data/04omnews.json    欧美广播

一条电台有三种形态，取播放地址的方式完全不同：

    "url": "https://..."        → 单条直链（MP3 / AAC / HLS 都有），直接用
    "urls": ["...", "..."]      → 多候选（CCTV 这类），逐个验活，取第一个能出的
    "playlist": "https://.../rock.json"
                                → 指向一个歌单，文件里是 [{name, url}, ...]，
                                  一首一首放，「下一曲」就是往后翻

对上层只暴露两个动作：`search()` 找台、`first_playable()` 拿能播的地址。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

import media

REPO = os.getenv("STATION_REPO", "nif99/niffm")
BRANCH = os.getenv("STATION_BRANCH", "master")
RAW = f"https://gitee.com/{REPO}/raw/{BRANCH}/data"

TABLES: list[tuple[str, str]] = [
    ("01cnmusic", "华语音乐"),
    ("02ommusic", "欧美音乐"),
    ("03cnnews", "华语新闻"),
    ("04omnews", "欧美广播"),
]

_CACHE_TTL = float(os.getenv("STATION_CACHE_SEC", "120"))  # 用户常改表，缓存只留 2 分钟
_cache: dict[str, tuple[float, list[dict]]] = {}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nifradio/1.0)"}
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)


async def _get_json(url: str) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _table(key: str) -> list[dict]:
    """读一张表，10 分钟内复用。gitee 挂了就用上次的缓存顶着。"""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        # 带一个时间戳绕开 gitee/CDN 的缓存，保证你刚改完立刻能读到
        rows = await _get_json(f"{RAW}/{key}.json?t={int(time.time())}")
    except Exception:
        return hit[1] if hit else []
    rows = [r for r in rows if isinstance(r, dict) and r.get("name")]
    for row in rows:
        row["_table"] = key
    _cache[key] = (time.time(), rows)
    return rows


async def all_stations() -> list[dict]:
    """四张表合成一份。单张表挂了不影响其它表。"""
    groups = await asyncio.gather(*(_table(key) for key, _label in TABLES), return_exceptions=True)
    out: list[dict] = []
    for item in groups:
        if isinstance(item, list):
            out.extend(item)
    return out


def _haystack(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in ("name", "notes", "category", "id"))


def _norm(text: str) -> str:
    """归一化：小写 + 去掉空格和常见分隔符。

    用户嘴上说的是「cctv1」「90s」，表里写的是「CCTV-1 综合」「90s Lovesongs」，
    不归一化就对不上。
    """
    out = []
    for ch in str(text or "").lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


async def search(keyword: str, limit: int = 5) -> list[dict]:
    """按台名 / 分类 / 备注模糊找台。空关键词返回全部（按表顺序）。"""
    rows = await all_stations()
    kw = (keyword or "").strip()
    if not kw:
        return rows[:limit]
    nkw = _norm(kw)
    if not nkw:
        return rows[:limit]
    # 先按台名匹配，再退到分类/备注，避免「华语摇滚」被「华语」类目抢走
    exact = [r for r in rows if nkw in _norm(r.get("name"))]
    # 「cctv1」也是「cctv10」的子串，台名越短说明越贴近，排前面
    exact.sort(key=lambda r: len(_norm(r.get("name"))))
    loose = [r for r in rows if r not in exact and nkw in _norm(_haystack(r))]
    return (exact + loose)[:limit]


def kind_of(row: dict) -> str:
    if row.get("playlist"):
        return "playlist"
    if row.get("urls"):
        return "urls"
    return "url" if row.get("url") else "none"


async def tracks(row: dict) -> list[dict]:
    """歌单型电台的歌曲列表；其它形态返回空。"""
    url = (row.get("playlist") or "").strip()
    if not url:
        return []
    try:
        data = await _get_json(url)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        u = (item.get("url") or "").strip()
        if u:
            out.append({"name": (item.get("name") or "").strip() or "未命名", "url": u})
    return out


async def candidates(row: dict) -> list[str]:
    """这条电台所有可能能播的地址（歌单型只给第 1 首）。"""
    kind = kind_of(row)
    if kind == "playlist":
        items = await tracks(row)
        return [items[0]["url"]] if items else []
    if kind == "urls":
        return [str(u).strip() for u in (row.get("urls") or []) if str(u).strip()]
    if kind == "url":
        return [str(row["url"]).strip()]
    return []


async def first_playable(row: dict) -> dict | None:
    """挑一个真能出声音的地址，返回 {url, title, kind}。全都不行就 None。"""
    kind = kind_of(row)
    if kind == "playlist":
        items = await tracks(row)
        for item in items[:3]:
            if await media.alive(item["url"]):
                return {"url": item["url"], "title": item["name"], "kind": kind}
        return None

    urls = await candidates(row)
    for url in urls[:5]:
        if await media.alive(url):
            return {"url": url, "title": str(row.get("name") or ""), "kind": kind}

    # 单条源：短请求被拒不代表播不了（有的 CDN 对 HEAD/小 Range 敏感），
    # 真读一小段再判一次，避免把「慢但能用」的台误判成坏的。
    if kind == "url" and urls:
        url = urls[0]
        try:
            data, _eof = await asyncio.wait_for(media.read_page(url, 0, 4095), 25)
            if data:
                return {"url": url, "title": str(row.get("name") or ""), "kind": kind}
        except Exception:
            pass
    return None
