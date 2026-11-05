#!/usr/bin/env bash
# 远程部署到一台全新的 Ubuntu 22.04 服务器
# 用法: ./deploy_remote.sh <host> <root密码> [接入点ws地址]
#
# 做四件事：装依赖 → 拉代码 → 写接入点 → 跑 boot.sh
set -u

HOST="${1:?用法: $0 <host> <password> [endpoint]}"
PASS="${2:?需要 root 密码}"
EP="${3:-}"

run_remote() {
  local cmd="$1"
  expect -c "
    set timeout 900
    log_user 1
    spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 root@${HOST} {${cmd}}
    expect {
      -re \"(?i)password:\" { send \"${PASS}\r\"; exp_continue }
      \"Permission denied\" { exit 2 }
      eof
    }
    catch wait result
    exit [lindex \$result 3]
  "
}

echo "=========== 1/4 安装系统依赖 ==========="
run_remote "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq python3-venv python3-pip ffmpeg git curl ca-certificates 2>&1 | tail -5; echo '---版本---'; python3 -V; ffmpeg -version 2>/dev/null | head -1"

echo
echo "=========== 2/4 拉代码 ==========="
run_remote "rm -rf /opt/nifradio; git clone --depth 1 https://github.com/sylva98/nifradio.git /opt/nifradio 2>&1 | tail -3; ls /opt/nifradio | head -20"

if [ -n "$EP" ]; then
  echo
  echo "=========== 3/4 写入接入点 ==========="
  run_remote "printf '%s' '${EP}' > /opt/nifradio/.endpoint; echo 已写入: \$(head -c 40 /opt/nifradio/.endpoint)…"
else
  echo
  echo "=========== 3/4 跳过接入点（未提供）==========="
fi

echo
echo "=========== 4/4 启动服务 ==========="
run_remote "cd /opt/nifradio && bash boot.sh"

echo
echo "=========== 完成，上面若出现 \"connected\":1 即打通 ==========="
