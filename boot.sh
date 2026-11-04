#!/usr/bin/env bash
# 小智音乐电台 · 一键启动 / 自愈脚本
#
# 用法（实例重启后，或任何时候想重启服务）：
#     bash /mnt/workspace/nifradio/boot.sh
#
# 它做四件事，全部幂等，重复跑没有副作用：
#   ① 拉最新代码（git 走不通就换镜像压缩包）
#   ② 准备运行环境：虚拟环境装到 /mnt/workspace（NAS 持久盘，重启不丢），
#      依赖按 requirements.txt 的哈希打标记，没变就不重装
#   ③ 确认 ffmpeg 可用（系统没有就用 imageio-ffmpeg 自带的那份）
#   ④ 杀掉旧进程、启动守护循环、等 25 秒后打印健康状态
set -u

WS=/mnt/workspace/nifradio
cd "$WS" 2>/dev/null || { echo "找不到 $WS，请先把仓库放到这里"; exit 1; }

echo "① 更新代码…"
if git pull -q --ff-only 2>/dev/null; then
  echo "  已是最新（$(git log --oneline -1 2>/dev/null | cut -c1-7)）"
else
  echo "  git 走不通，试镜像压缩包…"
  ok=0
  for u in \
    "https://codeload.github.com/sylva98/nifradio/tar.gz/refs/heads/main" \
    "https://ghfast.top/https://github.com/sylva98/nifradio/archive/refs/heads/main.tar.gz" \
    "https://gh-proxy.com/https://github.com/sylva98/nifradio/archive/refs/heads/main.tar.gz"
  do
    if curl -fsSL -m 90 "$u" -o /tmp/nf.tgz 2>/dev/null; then
      if tar xzf /tmp/nf.tgz -C "$WS" --strip-components=1 2>/dev/null; then
        echo "  镜像包更新成功"; ok=1; break
      fi
    fi
  done
  [ "$ok" = 1 ] || echo "  更新失败，先用现有代码继续"
fi

echo "② 准备运行环境…"
PY="$WS/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "  建虚拟环境（放在持久盘，重启不丢）…"
  if ! python3 -m venv "$WS/.venv" 2>/dev/null; then
    echo "  建虚拟环境失败，改用系统 python"
    PY=$(command -v python3)
  fi
fi

if command -v md5sum >/dev/null 2>&1; then
  HASH=$(md5sum requirements.txt | cut -d' ' -f1)
else
  HASH=$(md5 -q requirements.txt)
fi
MARK="$WS/.venv/.deps_$HASH"
if [ -f "$MARK" ]; then
  echo "  依赖已就绪"
else
  echo "  安装依赖（首次约 1~2 分钟）…"
  "$PY" -m pip install -q --upgrade pip >/dev/null 2>&1
  if "$PY" -m pip install -q -r requirements.txt; then
    mkdir -p "$WS/.venv" && touch "$MARK"
    echo "  依赖装好了"
  else
    echo "  ⚠ 依赖安装失败，往下跑通常会报缺包，把输出发我"
  fi
fi

echo -n "  ffmpeg: "
"$PY" - <<'PYEOF'
import shutil
try:
    p = shutil.which("ffmpeg")
    if p:
        print(p)
    else:
        import imageio_ffmpeg
        print(imageio_ffmpeg.get_ffmpeg_exe())
except Exception as exc:
    print(f"缺失（{exc}）—— 会退化为不转码")
PYEOF

if [ -s "$WS/.endpoint" ]; then
  echo "  接入点: $(head -c 32 "$WS/.endpoint")…"
else
  echo "  ⚠ 缺少 .endpoint（接入点没写进来），服务起来了也连不上小智"
fi

echo "③ 重启服务…"
pkill -f '[s]erver\.py' 2>/dev/null
pkill -f '[m]cp_pipe' 2>/dev/null
pkill -f '[m]cp_audio_server' 2>/dev/null
sleep 2
nohup bash -c "while true; do cd $WS && $PY server.py; sleep 5; done" \
  > "$WS/radio.log" 2>&1 &
echo "  已启动，等 25 秒连小智…"
sleep 25

echo "==== 健康 ===="
curl -s http://127.0.0.1:8080/health
echo
echo "==== 最近日志 ===="
curl -s http://127.0.0.1:8080/api/status | head -c 900
echo
echo "==== 缓存占用 ===="
du -sh "$WS/.cache/media" 2>/dev/null || echo "  暂无"
