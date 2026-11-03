"""魔搭 MCP 广场 / uvx 的托管入口。

托管环境是纯 STDIO 进程，没有额外 HTTP 端口，所以工具会直接把 B 站原始
音频地址交给设备（实测裸 UA、无 Referer 也能拉到 200）。原来的
server.py + mcp_pipe.py 那套仍是本机/容器部署用的入口，两者互不干扰。
"""

from __future__ import annotations

import os


def main() -> None:
    os.environ.setdefault("STREAM_PROXY", "0")
    from radio_mcp import mcp

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
