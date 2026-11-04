"""小智音乐电台 · 开机自启插件。

原理：魔搭 Notebook（PAI-DSW）每次启动实例都会拉起 Jupyter 服务，
而这个包把自己注册成 Jupyter 的服务端扩展，于是 Jupyter 一启动，
`_load_jupyter_server_extension` 就会被调用 —— 我们就趁机把 boot.sh
在后台拉起来。这样用户只需要点「启动实例」，服务自己就上线了。

设计原则：**绝对不能影响 Jupyter 本身的启动**。
所有异常都吞掉；只做一次「已在运行就跳过」的检查，然后 detached 起进程。
"""

import os
import subprocess

WS = "/mnt/workspace/nifradio"
BOOT = os.path.join(WS, "boot.sh")
LOG = os.path.join(WS, "autostart.log")


def _running() -> bool:
    try:
        # 用 [s] 的写法避免 pgrep 匹配到自己
        return subprocess.run(
            ["pgrep", "-f", "[s]erver.py"], capture_output=True
        ).returncode == 0
    except Exception:
        return False


def _load_jupyter_server_extension(server_app=None):  # noqa: ARG001
    try:
        if not os.path.exists(BOOT):
            return
        if _running():
            return
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("\n=== 实例启动，自动拉起服务 ===\n")
        subprocess.Popen(
            ["bash", "-lc", f"sleep 8; bash {BOOT} >> {LOG} 2>&1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,   # 别被 Jupyter 的进程组回收
            cwd=WS,
        )
    except Exception:
        pass  # 自启失败也不能拖累 Jupyter


def _unload_jupyter_server_extension(server_app=None):  # noqa: ARG001
    pass
