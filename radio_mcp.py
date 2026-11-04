"""NIFradio 的 MCP 工具层：把小智接进电台列表和歌单搜索。

启动方式（stdio 被 MCP 协议占用，本文件里禁止 print，日志一律走 logging）：
    cp .env.example .env      # 把 MCP_ENDPOINT 换成你自己的小智接入点
    python mcp_pipe.py radio_mcp.py

工具返回值会被小智的模型读进去再念给人听，所以：
  1. 只返回必要的几行，别把整页数据塞回去；
  2. 音频直链别让模型逐字朗读（在描述里已经交代）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from mcp.server.fastmcp import FastMCP

import bili_core
import radio_core

# stream_proxy 只在「有公网 HTTP 服务」时才需要。魔搭托管的是纯 STDIO 进程，
# 没有额外 HTTP 端口，此时直接把 B 站原始音频地址交给设备——实测 m4s 用裸 UA、
# 不带 Referer 也返回 200，设备可以直接拉。
_PROXY_ENABLED = bool(os.getenv("PUBLIC_BASE_URL", "").strip())


async def _play_link(bvid: str) -> tuple[str, dict]:
    """取可播放地址：有代理走短链，无代理走 B 站原始地址。"""
    if _PROXY_ENABLED:
        import stream_proxy
        return await stream_proxy.link_for_bvid(bvid)
    info = await bili_core.bili.resolve_audio(bvid)
    return info["url"], info

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nifradio-mcp")

mcp = FastMCP("NIFradio")

NO_READ_URL = "音频链接是给播放器用的，不要逐字念给用户，只在用户明确要链接时才报出来。"

# 小智设备端读取 MCP 返回通常封顶 1024 字节。5 条中文歌名 + 长直链就顶到 1021 了，
# 歌名再长一点就整包被截断 —— 所以按字节预算拼，宁少列两条也不要发残缺链接。
MAX_RESPONSE_BYTES = 900


def _fit(header: str, items: list[str], footer: str = "") -> str:
    """按字节预算装填：装不下的条目直接省掉，并说明还剩几条。"""
    picked: list[str] = []
    used = len(header.encode("utf-8")) + (len(footer.encode("utf-8")) + 1 if footer else 0)
    for piece in items:
        cost = len(piece.encode("utf-8")) + 1
        if picked and used + cost > MAX_RESPONSE_BYTES:
            picked.append(f"（还有 {len(items) - len(picked)} 条未列出，缩小范围可看全）")
            break
        picked.append(piece)
        used += cost
    body = "\n".join(picked)
    return f"{header}\n{body}" + (f"\n{footer}" if footer else "")


def _station_lines(stations: list[dict]) -> list[str]:
    lines = []
    for i, st in enumerate(stations, 1):
        line = f"{i}. {st['name']}"
        if st.get("now_playing"):
            line += f"（正在播：{st['now_playing']}）"
        if st.get("category"):
            line += f"［{st['category']}］"
        lines.append(f"{line}\n   {st['stream_url']}")
    return lines


def _track_lines(tracks: list[dict]) -> list[str]:
    lines = []
    for i, t in enumerate(tracks, 1):
        minutes, seconds = divmod(t["duration"], 60)
        lines.append(f"{i}. {t['name']} - {t['artist']}（{minutes}:{seconds:02d}）\n   {t['url']}")
    return lines


@mcp.tool()
async def list_radio_categories() -> str:
    """列出可收听的电台分类（音乐台、资讯台、交通台等）以及对应的分类 ID。
    用户说「想听广播」「打开电台」但没指定具体电台时，先调用本工具让用户挑分类。"""
    try:
        categories = await radio_core.get_categories()
    except Exception as exc:  # 网络问题直接告诉用户，别抛栈
        logger.warning("取分类失败: %s", exc)
        return f"电台分类获取失败：{exc}"
    if not categories:
        return "没有取到电台分类，可能是网络不通。"
    return "可用电台分类：\n" + "\n".join(f"{c['id']} {c['name']}" for c in categories)


@mcp.tool()
async def list_radio_stations(category_id: str = "442", limit: int = 5) -> str:
    """按分类 ID 列出电台，返回台名、正在播出的节目和可直接播放的 MP3 直播流地址。
    category_id 来自 list_radio_categories（例如 442 是音乐台、433 是资讯台）；
    limit 不要超过 5，设备端能读到的返回长度有限。"""
    try:
        stations = await radio_core.get_channels(category_id, 1, max(1, min(limit, 10)))
    except Exception as exc:
        logger.warning("取频道失败: %s", exc)
        return f"该分类下的电台获取失败：{exc}"
    if not stations:
        return f"分类 {category_id} 下没有电台，换个分类试试。"
    return _fit(f"分类 {category_id} 的电台：", _station_lines(stations), NO_READ_URL)


@mcp.tool()
async def find_radio_station(keyword: str, limit: int = 5) -> str:
    """按电台名关键词找台，比如「交通」「河北新闻」「音乐」「经济」。
    用户直接说出想听的电台名或类型时用这个，比逐层选分类快。"""
    try:
        stations = await radio_core.search_stations(keyword, max(1, min(limit, 8)))
    except Exception as exc:
        logger.warning("搜台失败: %s", exc)
        return f"搜台失败：{exc}"
    if not stations:
        return f"没找到名字里带「{keyword}」的电台。"
    return _fit("搜到这些电台：", _station_lines(stations), NO_READ_URL)


@mcp.tool()
async def play_radio(keyword: str) -> str:
    """直接打开某个电台开始播放。用户说「放清晨电台」「听交通广播」「开个新闻台」
    这类「想直接听某个台」的话时用，一步到位不用先列分类。"""
    try:
        stations = await radio_core.search_stations(keyword, 5)
    except Exception as exc:
        logger.warning("放台失败: %s", exc)
        return f"电台搜索失败（{exc}），过几秒再说一次。"

    if not stations:
        return f"没找到「{keyword}」这样的电台，换个说法试试，比如「音乐」「交通」「新闻」。"

    st = stations[0]
    now = f"（正在播：{st['now_playing']}）" if st.get("now_playing") else ""
    return (
        f"正在播放电台：{st['name']}{now}\n"
        f"{st['stream_url']}\n"
        "把上面这个链接交给播放器，不要念出来。"
    )


def _short(text: str, limit: int = 28) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- 播放队列
# 设备播完一首就停，不会反过来告诉云端「我放完了」。但用户会主动说「下一曲」，
# 所以只要云端记住「这次点的是什么、放到第几首」，就能接着往下放。
# 单设备场景，全局一份够用；进程重启丢失，属于可接受。
_QUEUE: dict = {"keyword": "", "items": [], "index": -1}
_QUEUE_SIZE = 8


def _queue_reset(keyword: str, items: list[dict]) -> None:
    _QUEUE["keyword"] = keyword
    _QUEUE["items"] = items
    _QUEUE["index"] = 0


def _queue_current() -> dict | None:
    items = _QUEUE["items"]
    index = _QUEUE["index"]
    if not items or not (0 <= index < len(items)):
        return None
    return items[index]


async def _play_current(prefix: str = "正在播放") -> str:
    """把当前这一首解析成短链，拼成交给模型的话。"""
    hit = _queue_current()
    if not hit:
        return "播放列表空了，重新点一首吧。"
    try:
        link, _info = await _play_link(hit["bvid"])
    except Exception as exc:
        logger.warning("取播放地址失败: %s", exc)
        return f"找到《{_short(hit['title'])}》了，但取播放地址失败，稍后再试。"

    # 无代理时给的是 B 站原始地址（约 785 字符），再堆作者/时长会顶破
    # 小智那 1024 字节的结果上限，所以精简成「标题 + 链接 + 一句交代」。
    if not _PROXY_ENABLED:
        return (
            f"{prefix}：{_short(hit['title'], 32)}\n"
            f"{link}\n"
            "把上面链接原样交给设备播放工具，不要念出来。"
        )

    minutes, seconds = divmod(hit["duration"], 60)
    position = _QUEUE["index"] + 1
    total = len(_QUEUE["items"])
    return (
        f"{prefix}：{_short(hit['title'], 32)}\n"
        f"（{_short(hit['author'], 12)}，{minutes}:{seconds:02d}，"
        f"第 {position}/{total} 首）\n"
        f"{link}\n"
        "把上面这个链接原样交给设备播放工具，不要逐字念出来。"
    )


@mcp.tool()
async def play_song(keyword: str) -> str:
    """点歌并直接播放。用户说「放周杰伦的歌」「来一首林俊杰的三国」「放点音乐」
    这类「想听某首歌 / 某个歌手」的话时，用这个工具一步到位，不需要先搜。
    会把这一批搜索结果记成播放列表，之后用户说「下一曲」能接着往下放。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return "想听什么歌？说个歌名或歌手名。"

    try:
        rows = await bili_core.bili.search(keyword, _QUEUE_SIZE)
    except Exception as exc:
        logger.warning("点歌搜索失败: %s", exc)
        return f"搜歌没成功（{exc}），过几秒再跟我说一次。"

    if not rows:
        return f"没找到「{keyword}」相关的歌，换个说法试试，比如「周杰伦 晴天」。"

    _queue_reset(keyword, rows)
    return await _play_current()


@mcp.tool()
async def next_song() -> str:
    """播放列表里的下一首。用户说「下一曲」「下一首」「换一首」「这首不好听」
    时调用，会接着上一次 play_song 搜到的那批结果往后播。"""
    items = _QUEUE["items"]
    if not items:
        return "现在还没在放歌，先说一首想听的，比如「放周杰伦的歌」。"
    if _QUEUE["index"] + 1 >= len(items):
        keyword = _QUEUE["keyword"]
        return f"「{keyword}」这批 {len(items)} 首都放完了，再点一次或换个歌手吧。"

    _QUEUE["index"] += 1
    return await _play_current("下一首")


@mcp.tool()
async def replay_song() -> str:
    """重放当前这首（从头开始）。用户说「重放」「再放一遍」「刚才那首再来一次」时用。"""
    if not _QUEUE["items"]:
        return "现在还没在放歌，先说一首想听的。"
    return await _play_current("重放")


@mcp.tool()
async def search_song(keyword: str, limit: int = 3) -> str:
    """搜歌看有哪些版本（B 站音源），只列候选不播放。
    用户说「有哪些版本」「找一下」「搜搜看」时才用；已经说要放就直接用 play_song。"""
    try:
        rows = await bili_core.bili.search(keyword, max(1, min(limit, 4)))
    except Exception as exc:
        logger.warning("搜歌失败: %s", exc)
        return f"搜歌没成功（{exc}），过几秒再说一次。"
    if not rows:
        return f"没找到「{keyword}」相关的歌。"

    lines: list[str] = []
    for i, row in enumerate(rows, 1):
        try:
            link, _info = await _play_link(row["bvid"])
        except Exception:
            continue
        minutes, seconds = divmod(row["duration"], 60)
        lines.append(
            f"{i}. {_short(row['title'])}（{minutes}:{seconds:02d}）\n   {link}"
        )
    if not lines:
        return "搜到了但都取不到播放地址，换个关键词或稍后再试。"
    return _fit("搜到这些（B站音源）：", lines, "用户选定后把对应链接交给设备播放工具。")


@mcp.tool()
async def search_open_music(keyword: str, limit: int = 3) -> str:
    """在开放版权曲库里搜歌（Jamendo），只有 B 站也搜不到、
    或者用户明确要「无版权音乐 / 背景音乐」时才用。返回 MP3 直链。"""
    try:
        tracks = await radio_core.search_tracks(keyword, max(1, min(limit, 5)))
    except Exception as exc:
        logger.warning("开放曲库搜歌失败: %s", exc)
        return f"搜歌失败：{exc}"
    if not tracks:
        return f"开放曲库里没搜到「{keyword}」。"
    return _fit("开放曲库搜到这些：", _track_lines(tracks), NO_READ_URL)


@mcp.tool()
async def get_song_url(song_id: str) -> str:
    """按歌曲 ID 重新解析一次播放直链。
    Jamendo 的直链带签名、过几小时就失效，之前拿到的链接播不了时用它刷新。"""
    try:
        track = await radio_core.get_track(song_id)
    except Exception as exc:
        logger.warning("解析直链失败: %s", exc)
        return f"直链解析失败：{exc}"
    if not track:
        return f"歌曲 {song_id} 没有可用的音频地址。"
    minutes, seconds = divmod(track["duration"], 60)
    return (
        f"{track['name']} - {track['artist']}（{minutes}:{seconds:02d}）\n"
        f"{track['url']}\n{NO_READ_URL}"
    )


@mcp.tool()
async def play_on_computer(url: str) -> str:
    """在运行本服务的这台电脑上播放音频 URL，声音从电脑扬声器出，不是从小智设备出。
    只有用户明确说「在电脑上放」时才用；小智硬件本身播不了这种 MP3 直链。"""
    player = shutil.which("mpv") or shutil.which("afplay")
    if not player:
        return "这台电脑没装播放器。装一个 mpv（brew install mpv）再试。"
    try:
        subprocess.Popen(
            [player, "--no-video", url] if player.endswith("mpv") else [player, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("调用播放器失败: %s", exc)
        return f"播放器启动失败：{exc}"
    return f"已在这台电脑上开始播放（{player.split('/')[-1]}）。"


if __name__ == "__main__":
    # 只有存在公网 HTTP 服务时才起短链代理；托管在魔搭这类纯 STDIO 环境里
    # 没有额外端口，代理不起，工具会直接返回 B 站原始音频地址。
    if _PROXY_ENABLED and os.getenv("STREAM_PROXY", "1").strip() != "0":
        import stream_proxy
        stream_proxy.run_in_thread()
    mcp.run(transport="stdio")
