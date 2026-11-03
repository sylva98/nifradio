"""B站音源：按关键词搜视频，再解析出可播放的音频流。

为什么用 B 站当点歌曲库：
  - 版权华语歌在开放曲库（Jamendo）里一首都没有，B 站上齐全；
  - 但 B 站只给 DASH 音频（fMP4 封装的 AAC），且直链要 Referer、两小时过期，
    所以这一层只负责「拿到真实地址」，怎么给设备用由 stream_proxy 处理。

实现要点（都是踩过的）：
  - 搜索接口要 wbi 签名：把 img_key/sub_key 按固定表重排后取前 32 位当 mixin key，
    参数按字典序排好、滤掉 !'()* 再做 md5；
  - 不带 buvid3/buvid4 指纹 cookie 时搜索会返回 code 0 但结果为空，必须先从
    /x/frontend/finger/spi 取；
  - playurl 的 fnval=16 才是 DASH，返回里 audio 有多条码率，优先取 30280。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import urllib.parse
from typing import Any

import httpx

_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    # 不要手写 Accept-Encoding：声明 br 而环境里没有 brotli 时，nav 接口
    # 会回压缩流导致 JSON 解析崩掉，交给 httpx 按已安装的编码协商即可。
    # 这几个 sec-ch-ua 头本身不参与签名，但缺了会更容易被判成脚本，
    # 实测补上后 v_voucher（风控）出现的频率明显下降。
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# 风控是「短时高频」触发的：静置几秒就恢复，但死等太久用户体验更差
# （设备那边 MCP 调用是有超时的）。所以总共只等 9 秒，超过就如实报错，
# 让用户过几秒再说一遍，比卡着不动强。
_RETRY_DELAYS = (0.0, 3.0, 6.0)

# 同一句话短时间内反复问很常见（"放周杰伦""再来一首周杰伦"），
# 缓存五分钟能显著降低被风控的概率，也省一次往返。
_SEARCH_TTL = 300.0
_search_cache: dict[str, tuple[float, list[dict]]] = {}

WBI_TTL = 6 * 3600          # wbi 密钥按天轮换，六小时刷一次足够
_EM_TAG = re.compile(r"</?em[^>]*>")

# 用户要的就是「歌单合集」——「周杰伦100首精选」那种几小时的整段，
# 所以不设时长上限，让它留在结果里。
# 只挡掉明显是广告、片段、教程的短内容（90 秒以内）。
MIN_TRACK_SEC = 90


class _RiskControl(Exception):
    """搜索接口返回了 v_voucher 而不是结果，属于风控命中，可重试。"""


def _fmt_duration(seconds: int) -> str:
    minutes, sec = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{sec:02d}"


def _parse_duration(raw: Any) -> int:
    """搜索接口给的是 "4:30" 这种字符串，也可能直接给秒数。"""
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 0
    return 0


def _expire_from_url(url: str) -> float:
    """B站直链带 deadline 参数（秒级时间戳），解析出来算过期时刻。"""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        deadline = query.get("deadline", [None])[0]
        if deadline:
            return float(deadline)
    except Exception:
        pass
    return time.time() + 3600


class BiliClient:
    """懒加载的单例：第一次用到时才建连接、取指纹、备签名。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._img_key = ""
        self._sub_key = ""
        self._wbi_at = 0.0
        self._boot_at = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=8.0),
                headers=_HEADERS,
                follow_redirects=True,
            )
        return self._client

    async def _bootstrap(self) -> None:
        """首页拿 cookie + spi 拿指纹 + nav 拿 wbi 密钥，两小时内不重复做。"""
        client = await self._get_client()
        try:
            await client.get("https://www.bilibili.com/")
        except Exception:
            pass
        try:
            spi = (await client.get("https://api.bilibili.com/x/frontend/finger/spi")).json()
            data = spi.get("data") or {}
            if data.get("b_3"):
                client.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
            if data.get("b_4"):
                client.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
        except Exception:
            pass
        nav = (await client.get("https://api.bilibili.com/x/web-interface/nav")).json()
        wbi = ((nav.get("data") or {}).get("wbi_img")) or {}
        self._img_key = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
        self._sub_key = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
        if not self._img_key or not self._sub_key:
            raise RuntimeError("没能取到 B 站 wbi 密钥，可能是网络或风控")
        self._wbi_at = time.time()
        self._boot_at = time.time()

    async def _ensure(self) -> None:
        if self._img_key and (time.time() - self._wbi_at) < WBI_TTL:
            return
        async with self._lock:
            if self._img_key and (time.time() - self._wbi_at) < WBI_TTL:
                return
            await self._bootstrap()

    def _mixin_key(self) -> str:
        origin = self._img_key + self._sub_key
        return "".join(origin[i] for i in _MIXIN_KEY_ENC_TAB)[:32]

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        signed = dict(params)
        signed["wts"] = int(time.time())
        signed = dict(sorted(signed.items()))
        signed = {
            k: "".join(ch for ch in str(v) if ch not in "!'()*")
            for k, v in signed.items()
        }
        query = urllib.parse.urlencode(signed)
        signed["w_rid"] = hashlib.md5((query + self._mixin_key()).encode()).hexdigest()
        return signed

    async def _search_once(self, keyword: str, page_size: int) -> list[dict]:
        client = await self._get_client()
        params = self._sign({
            "search_type": "video",
            "keyword": keyword,
            "page": 1,
            "page_size": page_size,
        })
        payload = (
            await client.get(
                "https://api.bilibili.com/x/web-interface/wbi/search/type", params=params
            )
        ).json()
        if payload.get("code") != 0:
            raise RuntimeError(f"B站搜索失败：{payload.get('message') or payload.get('code')}")
        data = payload.get("data") or {}
        if "result" not in data:
            # 只有 v_voucher / seid，没有 result：被风控挡了，交给外层重试
            raise _RiskControl()
        return data.get("result") or []

    async def search(self, keyword: str, limit: int = 5) -> list[dict]:
        """按关键词搜视频，返回名字、作者、时长、bvid。"""
        keyword = keyword.strip()
        if not keyword:
            return []

        now = time.time()
        hit = _search_cache.get(keyword)
        if hit and (now - hit[0]) < _SEARCH_TTL and len(hit[1]) >= limit:
            return [dict(r) for r in hit[1][:limit]]

        rows = await self._search_uncached(keyword, limit)
        _search_cache[keyword] = (time.time(), rows)
        if len(_search_cache) > 200:
            for key in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:100]:
                _search_cache.pop(key, None)
        return [dict(r) for r in rows[:limit]]

    async def _search_uncached(self, keyword: str, limit: int) -> list[dict]:
        raw: list[dict] = []
        last_error: Exception | None = None
        # 多要一些：合集会占掉前面好几个位置，只要 limit 条很可能全被筛没
        want = max(1, min(limit * 6, 50))
        for delay in _RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            await self._ensure()
            try:
                raw = await self._search_once(keyword, want)
            except _RiskControl as exc:
                last_error = exc
                continue
            except Exception:
                raise
            break
        else:
            raise RuntimeError("B站搜索被风控拦了，稍等几秒再说一次")

        rows: list[dict] = []
        for item in raw:
            if item.get("type") not in (None, "video"):
                continue
            bvid = item.get("bvid")
            if not bvid:
                continue
            duration = _parse_duration(item.get("duration"))
            if duration and duration < MIN_TRACK_SEC:
                continue
            # 不设上限：几小时的精选合集正是用户想要的
            title = _EM_TAG.sub("", str(item.get("title") or "")).replace("&quot;", '"')
            rows.append({
                "bvid": bvid,
                "title": title.strip(),
                "author": str(item.get("author") or "").strip(),
                "duration": duration,
                "play": int(item.get("play") or 0),
            })
            if len(rows) >= limit:
                break
        if not rows and last_error:
            raise last_error
        return rows

    async def resolve_audio(self, bvid: str) -> dict:
        """取这个视频的音频流地址，优先 30280（较高码率），退 30216。"""
        await self._ensure()
        client = await self._get_client()
        view = (
            await client.get(
                "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}
            )
        ).json()
        if view.get("code") != 0:
            raise RuntimeError(f"取视频信息失败：{view.get('message') or view.get('code')}")
        pages = ((view.get("data") or {}).get("pages")) or []
        cid = pages[0].get("cid") if pages else None
        if not cid:
            raise RuntimeError("这个稿件没有可用的分P")

        play = (
            await client.get(
                "https://api.bilibili.com/x/player/playurl",
                params={"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1},
            )
        ).json()
        if play.get("code") != 0:
            raise RuntimeError(f"取播放地址失败：{play.get('message') or play.get('code')}")
        audios = (((play.get("data") or {}).get("dash")) or {}).get("audio") or []
        if not audios:
            raise RuntimeError("这个稿件没有独立音频轨（可能是图文或直播回放）")

        audios.sort(key=lambda a: 0 if a.get("id") == 30280 else 1)
        best = audios[0]
        url = str(best.get("baseUrl") or "")
        if not url:
            raise RuntimeError("音频轨里没有地址")
        return {
            "url": url,
            "mime": str(best.get("mimeType") or "audio/mp4"),
            "codecs": str(best.get("codecs") or ""),
            "expires": _expire_from_url(url),
            "referer": "https://www.bilibili.com/",
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


bili = BiliClient()
