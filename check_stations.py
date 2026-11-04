"""体检：把 gitee 上「我的电台表」全跑一遍，告诉你哪些台现在真的能出声。

用法（在云服务器 / 本机项目目录里）：
    python check_stations.py            # 全部台
    python check_stations.py 摇滚 cctv  # 只看匹配这些关键词的台

它做的检查：
  1. 四张表（或你新增的表）能不能读到
  2. 每个台按形态取地址：url 直链 / urls 多候选 / playlist 歌单
  3. 真的去读一小段音频（歌单型抽前 5 首），连 m3u8 也会追到第一个切片
  4. 顺带报出 playlist 是相对路径还是完整地址（两种都支持）

输出只有两类：✅ 能播、❌ 不能播（附原因）。改完 gitee 之后跑一次，
就知道是「表写错了」还是「源站挂了」。
"""

from __future__ import annotations

import asyncio
import sys

import media
import stations


async def check_one(row: dict) -> tuple[bool, str]:
    name = str(row.get("name") or "")
    kind = stations.kind_of(row)
    if kind == "none":
        return False, "条目里 url / urls / playlist 一个都没有"

    if kind == "playlist":
        items, err = await stations.tracks_detail(row)
        if err:
            return False, err
        bad = 0
        for item in items[:5]:
            if await media.alive(item["url"]):
                return True, f"歌单 {len(items)} 首，第 {bad + 1} 首可播"
            bad += 1
        return False, f"歌单 {len(items)} 首，前 {bad} 首全连不上"

    for url in (await stations.candidates(row))[:5]:
        if await media.alive(url):
            return True, url[:60]
    return False, f"{len(await stations.candidates(row))} 个源全连不上"


async def main() -> int:
    keywords = [k for k in sys.argv[1:] if k.strip()]
    keys = await stations.table_keys()
    all_rows = await stations.all_stations()
    rows = all_rows
    if keywords:
        rows = [r for r in all_rows if any(k in stations._haystack(r) for k in keywords)]

    print(f"发现 {len(keys)} 张表：{', '.join(keys)}")
    print(f"共 {len(all_rows)} 个台，本次检查 {len(rows)} 个")
    print(f"转码器：{media.ffmpeg_path() or '（没有，只能透传）'}")
    print("-" * 70)

    good = bad = 0
    problems: list[str] = []
    for row in rows:
        try:
            ok, info = await asyncio.wait_for(check_one(row), 90)
        except asyncio.TimeoutError:
            ok, info = False, "检查超时（源站响应太慢）"
        except Exception as exc:
            ok, info = False, f"{type(exc).__name__}: {exc}"
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {str(row.get('name'))[:22]:<24} {str(row.get('category') or '-'):<8} {info}")
        if ok:
            good += 1
        else:
            bad += 1
            problems.append(f"{row.get('name')}（{info}）")

    print("-" * 70)
    print(f"结果：{good} 个能播 / {bad} 个不能播")
    if problems:
        print("\n需要你改 json 的：")
        for p in problems:
            print("  -", p)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
