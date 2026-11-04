"""把 nifradio_autostart 装进系统 Python 的 site-packages，
顺便把 Jupyter 的服务端配置写进 etc/jupyter，实现「实例启动 → 服务自动上线」。

安装方式：python3 -m pip install --no-deps /mnt/workspace/nifradio/autostart
"""

from setuptools import setup

setup(
    name="nifradio-autostart",
    version="1.0.0",
    description="ModelScope DSW 实例启动时自动拉起小智音乐电台服务",
    packages=["nifradio_autostart"],
    data_files=[
        (
            "etc/jupyter/jupyter_server_config.d",
            ["jupyter-config/jupyter_server_config.d/nifradio_autostart.json"],
        )
    ],
    python_requires=">=3.8",
)
