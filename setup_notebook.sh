#!/usr/bin/env bash
# 魔搭 Notebook 专用：修好接入点文件 + 启动守护（崩溃自动拉起）
# 用法：bash setup_notebook.sh
cd "$(dirname "$0")" || exit 1

# 1. 交互式拿接入点，粘错自动重试，绝不写进坏数据
while true; do
  read -p " 在这行字后面粘贴接入点然后按回车 > " EP
  EP=$(printf '%s' "$EP" | tr -d '[:space:]')
  case "$EP" in
    wss://api.xiaozhi.me/mcp/*)
      break
      ;;
    *)
      echo "   不对哦：开头必须是 wss://api.xiaozhi.me/mcp/"
      echo "   你粘的开头是：${EP:0:40}"
      echo "   再来一次，注意等 > 出现后再粘"
      ;;
  esac
done
printf '%s' "$EP" > .endpoint
echo " 已保存。开头30个字：$(head -c 30 .endpoint)"

# 2. 清掉所有旧的进程（server.py / mcp_pipe / 旧守护循环）
pkill -f "python.*server.py" 2>/dev/null
pkill -f "python.*mcp_pipe" 2>/dev/null
pkill -f "MCP_ENDPOINT" 2>/dev/null
sleep 1

# 3. 启动守护：mcp_pipe 崩了 5 秒后自动拉起
nohup bash -c 'cd "$(dirname "$0")" 2>/dev/null; while true; do MCP_ENDPOINT=$(cat .endpoint) python mcp_pipe.py radio_mcp.py; sleep 5; done' > bridge.log 2>&1 &

# 4. 等 12 秒看连接日志
sleep 12
echo " ==== bridge.log 最新日志（看到 initialize/注册成功 就是通了） ===="
tail -30 bridge.log
