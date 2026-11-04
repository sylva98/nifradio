"""用户自己的电台表（就是 NIFradio 浏览器插件用的那几张 gitee JSON）。

**代码里不写死任何电台 / 任何链接**：每次查询都去 gitee 现拉，
你改完最多 2 分钟生效（`STATION_CACHE_SEC`）。连「有哪些表」也是自动发现的：

    ① 环境变量 STATION_TABLES=01cnmusic,02ommusic   （想手工指定时才用）
    ② data/index.json 里列的文件名                   （你自己维护的清单）
    ③ 列 gitee 仓库 data/ 目录，取所有 .json          （默认，你在加表就自动认）
    ④ 上面全挂 → 用默认四张表兜底（gitee 短暂抽风时还能顶）

一条电台有三种形态，取播放地址的方式完全不同：

    "url": "https://..."        → 单条直链（MP3 / AAC / HLS 都有），直接用
    "urls": ["...", "..."]      → 多候选（CCTV 这类），逐个验活，取第一个能出的
    "playlist": "https://.../xxx.json"
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
DIR_NAME = os.getenv("STATION_DIR", "data")
RAW = f"https://gitee.com/{REPO}/raw/{BRANCH}/{DIR_NAME}"
API_DIR = f"https://gitee.com/api/v5/repos/{REPO}/contents/{DIR_NAME}?ref={BRANCH}"

# ④ 兜底用的四张表：只在「自动发现失败 + 没有任何缓存」时才用
FALLBACK_TABLES: list[str] = ["01cnmusic", "02ommusic", "03cnnews", "04omnews"]

_CACHE_TTL = float(os.getenv("STATION_CACHE_SEC", "120"))  # 用户常改表，缓存只留 2 分钟
_DISCOVER_TTL = 600.0  # 「有哪些表」10 分钟重新发现一次就够，别太频繁打 API
_cache: dict[str, tuple[float, list[dict]]] = {}
_tables_seen: tuple[float, list[str]] | None = None

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nifradio/1.0)"}
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)


async def _get_json(url: str) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _clean_keys(items: list[Any]) -> list[str]:
    """把「文件名 / 带 .json 的名字 / 只给名字」统一成表 key，去掉重复。"""
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("file") or item.get("key") or item.get("name") or ""
        name = str(item or "").strip()
        if not name:
            continue
        if "/" in name:  # 允许写成 "data/05jazz.json"
            name = name.rsplit("/", 1)[-1]
        if name.lower().endswith(".json"):
            name = name[:-5]
        if name and name not in out and name != "index":
            out.append(name)
    return out


async def table_keys() -> list[str]:
    """这次要读哪几张表——你新增/改名都不用改代码。"""
    global _tables_seen
    if _tables_seen and time.time() - _tables_seen[0] < _DISCOVER_TTL:
        return _tables_seen[1]

    # ① 环境变量优先（想锁死表清单时用）
    env = _clean_keys(os.getenv("STATION_TABLES", "").split(","))

    # ② data/index.json：你自己维护一份清单也可以
    idx: list[str] = []
    try:
        data = await _get_json(f"{RAW}/index.json?t={int(time.time())}")
        if isinstance(data, list):
            idx = _clean_keys(data)
    except Exception:
        pass

    # ③ 直接列 gitee 的 data/ 目录（公开仓库无需 token）
    listed: list[str] = []
    try:
        data = await _get_json(f"{API_DIR}&t={int(time.time())}")
        if isinstance(data, list):
            listed = _clean_keys([x for x in data if str(x.get("name", "")).endswith(".json")])
    except Exception:
        pass

    keys = env or idx or sorted(listed)
    if not keys:
        # ④ 全都不行：有旧缓存就继续用旧表，否则退默认四张
        keys = sorted(_cache) or list(FALLBACK_TABLES)
    _tables_seen = (time.time(), keys)
    return keys


async def _table(key: str) -> list[dict]:
    """读一张表，2 分钟内复用。gitee 挂了就用上次的缓存顶着。"""
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
    """所有表合成一份。单张表挂了不影响其它表。"""
    keys = await table_keys()
    groups = await asyncio.gather(*(_table(key) for key in keys), return_exceptions=True)
    out: list[dict] = []
    for item in groups:
        if isinstance(item, list):
            out.extend(item)
    return out


def _haystack(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in ("name", "notes", "category", "id"))


def _norm(text: str) -> str:
    """归一化：小写 + 只留字母数字。

    用户嘴上说的和表里写的格式往往不一样（连字符、空格、大小写、
    全角半角），不归一化就对不上。
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
