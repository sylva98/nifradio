#!/usr/bin/env bash
# 小智音乐电台 · 一键启动 / 自愈脚本
#
# 用法（实例重启后，或任何时候想重启服务）：
#     bash /mnt/workspace/nifradio/boot.sh
#
# 改完 gitee 上的电台表，想确认哪些台还能播（不重启服务）：
#     bash /mnt/workspace/nifradio/boot.sh check
#     bash /mnt/workspace/nifradio/boot.sh check 摇滚 cctv   # 只看这几个关键词
#
# 它做五件事，全部幂等，重复跑没有副作用：
#   ① 拉最新代码（git 走不通就换镜像压缩包）
#   ② 准备运行环境：虚拟环境装到 /mnt/workspace（NAS 持久盘，重启不丢），
#      依赖按 requirements.txt 的哈希打标记，没变就不重装
#   ③ 把开机自启插件装进系统 Python（Jupyter 服务端扩展），
#      装好之后**实例一启动服务就自己上线**，不用再敲命令
#   ④ 杀掉旧进程、启动守护循环 + 保活进程
#      （保活每 10 分钟刷一次 Jupyter 活动，避免实例被判定「静默」而关机）
#   ⑤ 等 25 秒后打印健康状态
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

if [ "${1:-}" = "check" ]; then
  echo "==== 电台体检（只看不改，不动正在跑的服务）===="
  shift
  cd "$WS" && "$PY" -u check_stations.py "$@"
  exit $?
fi

if [ -s "$WS/.endpoint" ]; then
  echo "  接入点: $(head -c 32 "$WS/.endpoint")…"
else
  echo "  ⚠ 缺少 .endpoint（接入点没写进来），服务起来了也连不上小智"
fi

echo "③ 安装开机自启（只做一次，之后就自动了）…"
SYS_PY=$(command -v python3)
AUTO_PKG="$WS/autostart"
AUTO_CFG="$AUTO_PKG/jupyter-config/jupyter_server_config.d/nifradio_autostart.json"

if [ -f "$AUTO_PKG/setup.py" ]; then
  "$SYS_PY" -m pip install -q --no-deps --no-build-isolation "$AUTO_PKG" >/tmp/auto_install.log 2>&1 \
    || "$SYS_PY" -m pip install -q --no-deps "$AUTO_PKG" >>/tmp/auto_install.log 2>&1 \
    || "$SYS_PY" -m pip install -q --no-deps --break-system-packages \
         "$AUTO_PKG" >>/tmp/auto_install.log 2>&1
fi

if "$SYS_PY" -c "import nifradio_autostart" >/dev/null 2>&1; then
  echo "  自启插件就位：实例启动后会自动跑 boot.sh，你不用敲命令"
else
  echo "  pip 装不上，改用直接拷贝…"
  SP=$("$SYS_PY" -c "import site;print(site.getsitepackages()[0])" 2>/dev/null)
  [ -n "${SP:-}" ] && cp -r "$AUTO_PKG/nifradio_autostart" "$SP/" 2>/dev/null
  if "$SYS_PY" -c "import nifradio_autostart" >/dev/null 2>&1; then
    echo "  已拷贝到 $SP"
  else
    echo "  ⚠ 自启没装成（不影响本次运行；以后重启仍需手动跑 boot.sh）"
  fi
fi

# 配置兜底：保证 Jupyter 无论从哪个目录读配置都能看到这个扩展
for d in /usr/local/etc/jupyter/jupyter_server_config.d \
         /etc/jupyter/jupyter_server_config.d; do
  mkdir -p "$d" 2>/dev/null && cp -f "$AUTO_CFG" "$d/" 2>/dev/null
done

echo "④ 重启服务…"
pkill -f '[s]erver\.py' 2>/dev/null
pkill -f '[m]cp_pipe' 2>/dev/null
pkill -f '[m]cp_audio_server' 2>/dev/null
pkill -f '[k]eepalive\.py' 2>/dev/null
sleep 2
nohup bash -c "while true; do cd $WS && $PY server.py; sleep 5; done" \
  > "$WS/radio.log" 2>&1 &
echo "  已启动，等 25 秒连小智…"

if [ -f keepalive.py ]; then
  nohup "$PY" keepalive.py > "$WS/keepalive.log" 2>&1 &
  echo "  保活已挂上（每 10 分钟刷新活动，日志 keepalive.log）"
fi

sleep 25

echo "==== 健康 ===="
curl -s http://127.0.0.1:8080/health
echo
echo "==== 最近日志 ===="
curl -s http://127.0.0.1:8080/api/status | head -c 900
echo
echo "==== 保活 ===="
tail -n 3 "$WS/keepalive.log" 2>/dev/null || echo "  未启动"
echo
echo "==== 缓存占用 ===="
du -sh "$WS/.cache/media" 2>/dev/null || echo "  暂无"
