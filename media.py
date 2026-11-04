"""音频取流与转码层。

为什么需要这一层
----------------
小智设备那条「音频资源」通道最终是**云端解码**后推给设备的，但不同源给的容器
差别很大：

  * 蜻蜓直播 / Jamendo 歌单 → 纯 MP3，直接按 HTTP Range 分页透传即可；
  * B 站 m4s → fMP4 容器（`ftyp isom`）+ AAC，设备/云端不一定吃得下；
  * 有些电台给的是 HLS 播放列表（`.m3u8`），那只是一张清单，根本不能当音频播。

所以这里统一先「验身」（读头 16 字节看魔数），再决定：
  透传（passthrough）  —— MP3 类，Range 分页，零开销，直播流也照旧；
  转码（transcode）    —— m4s/AAC/HLS/其它封装，用 ffmpeg 边读边转成 MP3 落盘，
                          再按字节切片喂给云端。转码是渐进式的：ffmpeg 一边
                          产出，分页读取一边就能拿到数据，首屏 1~2 秒；
                          单次任务用 `AUDIO_MAX_SECONDS`（默认 1800 秒）截断，
                          避免 7 小时合集把磁盘和转码时间拖爆。

ffmpeg 找不到时（比如精简镜像）自动退化为 passthrough，宁可播不出也不报错，
并在 stderr 打一行提示。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

UA = "ESP32-HTTPClient/1.0"
REFERER = "https://www.bilibili.com/"

MAX_SECONDS = int(os.getenv("AUDIO_MAX_SECONDS", "1800"))
PAGE = 102400
CACHE_TTL = 3 * 3600          # 缓存最多留 3 小时
MAX_CACHE_MB = int(os.getenv("AUDIO_CACHE_MB", "512"))  # 缓存总量上限


def _default_cache_dir() -> Path:
    """优先放在代码同级的 .cache（/mnt/workspace 是 NAS 持久盘，重启不丢），
    建不出来才退回临时目录。"""
    try:
        cand = Path(__file__).resolve().parent / ".cache" / "media"
        cand.mkdir(parents=True, exist_ok=True)
        return cand
    except OSError:
        return Path(tempfile.gettempdir()) / "nifradio-media"


CACHE_DIR = Path(os.environ.get("AUDIO_CACHE_DIR") or _default_cache_dir())

_plan_cache: dict[str, str] = {}
_jobs: dict[str, "Job"] = {}
_jobs_lock: asyncio.Lock | None = None

_FFMPEG: str | None = None
_FFMPEG_READY = False


# ---------------------------------------------------------------- 工具

def _headers(url: str, start: int | None = None, end: int | None = None) -> dict[str, str]:
    headers = {"User-Agent": UA}
    if "bilivideo" in url or "bilibili" in url or "biliapi" in url:
        headers["Referer"] = REFERER
        headers["Origin"] = "https://www.bilibili.com"
    if start is not None and end is not None:
        headers["Range"] = f"bytes={start}-{end}"
    return headers


def ffmpeg_path() -> str | None:
    """优先系统 ffmpeg，其次 imageio-ffmpeg 自带的静态二进制。"""
    global _FFMPEG, _FFMPEG_READY
    if _FFMPEG_READY:
        return _FFMPEG
    _FFMPEG_READY = True
    _FFMPEG = shutil.which("ffmpeg")
    if _FFMPEG:
        return _FFMPEG
    try:
        import imageio_ffmpeg  # type: ignore

        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _FFMPEG = None
    return _FFMPEG


def _classify(head: bytes, ctype: str, url: str) -> str:
    """看魔数定策略：返回 'transcode' 或 'passthrough'。"""
    if b"ftyp" in head[4:12]:            # mp4 / m4s / m4a
        return "transcode"
    if head.startswith(b"#EXTM3U"):       # HLS 清单
        return "transcode"
    if head.startswith(b"ID3"):           # 带标签的 MP3
        return "passthrough"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "passthrough"              # 裸 MP3 帧同步字
    if head.startswith((b"OggS", b"fLaC")) or head[:4] == b"RIFF":
        return "transcode"                # ogg / flac / wav 设备未必认，转成 mp3
    low_type = ctype.lower()
    if "mpegurl" in low_type:
        return "transcode"
    if "mpeg" in low_type or "mp3" in low_type:
        return "passthrough"
    if "mp4" in low_type or "aac" in low_type:
        return "transcode"
    if url.lower().split("?")[0].endswith((".mp3", ".mp3/")):
        return "passthrough"
    # 判断不出来时：能转就转，MP3 设备一定吃得下
    return "transcode" if ffmpeg_path() else "passthrough"


async def probe(url: str) -> str:
    """读前 1KB 判定策略，结果按 URL 缓存。"""
    hit = _plan_cache.get(url)
    if hit:
        return hit
    if ".m3u8" in url.lower():
        _plan_cache[url] = "transcode"
        return "transcode"

    plan = "passthrough"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=_headers(url, 0, 1023)) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                head = b""
                async for part in resp.aiter_bytes():
                    head += part
                    if len(head) >= 32:
                        break
        plan = _classify(head, ctype, url)
    except Exception:
        # 探测失败就按扩展名兜底，避免把直播流误送进 ffmpeg
        plan = "passthrough" if url.lower().split("?")[0].endswith(".mp3") else "transcode"

    if plan == "transcode" and not ffmpeg_path():
        plan = "passthrough"  # 没有转码器，只能原样给
    _plan_cache[url] = plan
    return plan


# ---------------------------------------------------------------- 透传

async def _passthrough(url: str, start: int, end: int) -> tuple[bytes, bool]:
    """按 Range 透传；源站不支持 Range 时自己数着读、够数就断。

    直播流（蜻蜓 64k.mp3）永远没有"总长度"，用 `client.get()` 等整包会一直挂着，
    所以必须 stream 读取，凑满这一页需要的字节数就主动停下。
    """
    want = end - start + 1
    buf = bytearray()
    total: int | None = None
    skip = 0
    has_range = False

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=_headers(url, start, end)) as resp:
                if resp.status_code == 416:
                    return b"", True
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")

                content_range = resp.headers.get("content-range", "")
                if "/" in content_range:
                    has_range = True
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                if resp.status_code == 200 and not has_range and start > 0:
                    skip = start  # 源站整包返回：先把前面的字节丢掉

                async for chunk in resp.aiter_bytes():
                    if skip:
                        if len(chunk) <= skip:
                            skip -= len(chunk)
                            continue
                        chunk = chunk[skip:]
                        skip = 0
                    buf += chunk
                    if len(buf) >= want:
                        break
    except httpx.TimeoutException:
        pass  # 直播流读满一页就断，超时按"数据不全"处理

    data = bytes(buf[:want])
    eof = len(data) < want or (total is not None and end + 1 >= total)
    return data, eof


# ---------------------------------------------------------------- 转码任务

class Job:
    """一次「源 → MP3 文件」的转码任务，支持渐进读取。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.key = hashlib.sha1(url.encode()).hexdigest()[:20]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = CACHE_DIR / f"{self.key}.mp3"
        self.ok_mark = CACHE_DIR / f"{self.key}.ok"
        self.done = self.ok_mark.exists() and self.path.exists()
        self.error: str | None = None
        self.started = time.monotonic()
        self.proc: subprocess.Popen | None = None
        if not self.done:
            self._start()

    # -- 启动 ffmpeg
    def _start(self) -> None:
        ff = ffmpeg_path()
        if not ff:
            raise RuntimeError("没有可用的 ffmpeg")
        if self.path.exists():
            self.path.unlink(missing_ok=True)

        header_block = (
            f"User-Agent: {UA}\r\n"
            f"Referer: {REFERER}\r\n"
            f"Origin: https://www.bilibili.com\r\n"
        )
        cmd = [
            ff, "-hide_banner", "-loglevel", "error", "-nostdin",
            # 注意：不要加 -protocol_whitelist / -allowed_extensions。
            # ffmpeg 9 起这两个选项对 HLS 输入会直接报
            # "Error opening input: Invalid argument"，实测带与不带差别就在这。
            "-headers", header_block,
            "-i", self.url,
            "-t", str(MAX_SECONDS),
            "-vn", "-map", "0:a:0?",
            "-acodec", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-f", "mp3", "-y", str(self.path),
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self) -> None:
        assert self.proc is not None
        try:
            _, err = self.proc.communicate(timeout=MAX_SECONDS + 300)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.error = "转码超时"
            return
        size = self.path.stat().st_size if self.path.exists() else 0
        if size < 4096:
            detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
            self.error = detail[-1] if detail else "转码失败（无音频数据）"
            return
        self.ok_mark.touch()
        self.done = True

    # -- 读一页
    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    async def read_page(self, start: int, end: int, timeout: float = 60.0) -> tuple[bytes, bool]:
        deadline = time.monotonic() + timeout
        while True:
            if self.error:
                raise RuntimeError(self.error)
            size = self._size()
            if self.done:
                if start >= size:
                    return b"", True
                data = self._slice(start, min(end, size - 1))
                return data, end + 1 >= size
            if size > end:
                return self._slice(start, end), False
            if time.monotonic() > deadline:
                if size > start:
                    return self._slice(start, size - 1), False
                raise RuntimeError("转码首屏超时")
            await asyncio.sleep(0.1)

    def _slice(self, start: int, end: int) -> bytes:
        try:
            with self.path.open("rb") as fh:
                fh.seek(start)
                return fh.read(end - start + 1)
        except OSError:
            return b""


def _gc_cache() -> None:
    """清缓存：先删过期文件，再按总量上限从旧到新删。

    单首 30 分钟 MP3 约 28MB，连续点十几首就能吃掉几百 MB，
    所以除了 TTL 还要有总量闸门，避免把 /mnt/workspace 撑满。
    """
    try:
        files = [f for f in CACHE_DIR.glob("*") if f.is_file()]
    except OSError:
        return

    now = time.time()
    alive: list[tuple[float, int, Path]] = []
    for f in files:
        try:
            mtime = f.stat().st_mtime
            size = f.stat().st_size
        except OSError:
            continue
        if now - mtime > CACHE_TTL:
            f.unlink(missing_ok=True)
            continue
        alive.append((mtime, size, f))

    total = sum(size for _, size, _ in alive)
    limit = MAX_CACHE_MB * 1024 * 1024
    if total <= limit:
        return
    for _mtime, size, f in sorted(alive):        # 旧文件先删
        if total <= limit:
            break
        f.unlink(missing_ok=True)
        total -= size


async def _job_for(url: str) -> Job:
    global _jobs_lock
    if _jobs_lock is None:
        _jobs_lock = asyncio.Lock()
    async with _jobs_lock:
        job = _jobs.get(url)
        if job is not None and job.error:
            _jobs.pop(url, None)
            job = None
        if job is None:
            _gc_cache()
            job = Job(url)
            _jobs[url] = job
        return job


# ---------------------------------------------------------------- 对外

async def read_page(url: str, start: int, end: int) -> tuple[bytes, bool]:
    """取 [start, end] 字节；返回 (数据, 是否已到末尾)。自动选透传或转码。"""
    if await probe(url) == "passthrough":
        return await _passthrough(url, start, end)
    job = await _job_for(url)
    return await job.read_page(start, end)


async def warm(url: str) -> None:
    """点歌时提前起转码，等云端来分页读时首屏已经有一块了。"""
    try:
        if await probe(url) == "transcode":
            await _job_for(url)
    except Exception:
        pass
