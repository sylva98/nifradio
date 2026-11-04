# -*- coding: utf-8 -*-
"""
NIF Radio 魔搭保活 cell
======================
用法：把本文件全部代码复制到魔搭 Notebook 的一个单元格里运行，之后不要停它。

原理：
  魔搭免费实例的判定是「超过 30 分钟未在 notebook 内执行操作 → 1 小时后关闭实例」，
  检测对象是 Jupyter 内核活动。只要有一个 cell 正在执行，就不算静默。
  本 cell 每 20 分钟心跳一次（内核持续占用），顺带巡检音频服务，
  服务无响应时自动用 boot.sh 拉起。

注意：
  1) 这个 cell 会一直占用当前内核，其他代码请新建 notebook（新内核）再跑。
  2) 关掉浏览器不影响，内核在服务端继续执行。
  3) 魔搭「单次最长 10 小时」的硬顶无法绕过，到点仍会停，属预期行为。
"""

import datetime
import subprocess
import time
import urllib.request

BOOT = "/mnt/workspace/nifradio/boot.sh"    # boot.sh 在魔搭持久盘上的位置
HEALTH = "http://127.0.0.1:8080/health"     # 音频服务健康检查端点
HEARTBEAT_MINUTES = 20                      # 心跳间隔（< 30 分钟静默线）
LOG = "/mnt/workspace/keepalive_boot.log"   # boot.sh 拉起时的输出日志


def _stamp() -> str:
    return datetime.datetime.now().strftime("%m-%d %H:%M:%S")


def _service_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def _boot() -> None:
    try:
        with open(LOG, "ab") as f:
            subprocess.Popen(["bash", BOOT], stdout=f, stderr=subprocess.STDOUT)
        print(f"[{_stamp()}] 服务无响应，已调用 boot.sh 拉起", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{_stamp()}] boot.sh 拉起失败: {e}", flush=True)


if __name__ == "__main__" or True:  # 允许直接粘贴进 cell 执行
    print(
        f"[{_stamp()}] 保活启动：每 {HEARTBEAT_MINUTES} 分钟心跳，"
        f"服务挂了自动 boot.sh 拉起（10 小时硬顶仍在，属魔搭规则）",
        flush=True,
    )
    while True:
        for _ in range(HEARTBEAT_MINUTES):  # 碎片化 sleep，便于手动中断
            time.sleep(60)
        if _service_ok():
            print(f"[{_stamp()}] 心跳正常，服务在线", flush=True)
        else:
            _boot()
