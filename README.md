# 小智收音机 · 云端服务

把浏览器插件里的电台、歌单和 B 站点歌接进小智 AI，让设备自己出声。

这个服务必须**常驻运行**：小智的 MCP 接入点是「服务主动去连它」的 WebSocket
长连接，进程一停，智能体里的工具就会消失。所以 GitHub Pages、Cloudflare
Workers 这类静态/按需运行的环境都用不了，需要 Koyeb、VPS 这类能一直跑的容器。

## 一键部署到 Koyeb（免费，不用信用卡，不休眠）

[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&builder=dockerfile&repository=github.com/sylva98/nifradio&branch=main&name=nif-radio&instance_type=free&region=fra&ports=8080%3Bhttp%3B%2F)

点开后用 GitHub 账号登录 Koyeb，确认表单里这几项，然后点 **Deploy**：

| 项目 | 值 |
|---|---|
| Source | Git → 本仓库 `main` 分支 |
| Builder | Dockerfile |
| Instance type | **Free**（512MB RAM / 0.1 vCPU） |
| Region | Frankfurt (`fra`) 或 Washington D.C. (`was`)，免费实例只有这两区 |
| Port | `8080`，HTTP，路径 `/` |

等待约 2~3 分钟构建完成，Koyeb 会给你一个 `https://xxxx.koyeb.app` 网址。

## 部署后只需做一次

1. 打开这个 `https://xxxx.koyeb.app`，会看到一个网页控制台。
2. 去小智控制台 → 你的智能体 → 扩展能力 → **MCP 接入点**，点复制，把整串
   `wss://api.xiaozhi.me/mcp/?token=...` 粘进网页的输入框，点「连接」。
3. 日志里出现 `Successfully connected` 和 `ListToolsRequest` 就通了。
   回小智控制台刷新扩展能力页，能看到工具列表即完成。

之后对小智说：「播放周杰伦」「放清晨电台」「播放 cctv1」「下一曲」。

### 建议再设一个环境变量（可选，但推荐）

Koyeb 容器的文件系统是临时的，重新部署后网页贴过的接入点会丢。想一劳永逸：
Koyeb 控制台 → Service → Settings → **Environment variables**，加一条
`MCP_ENDPOINT`，值就是那串 `wss://...`，勾上 Secret。服务启动时会自动读它重连。

## 目录说明

| 文件 | 作用 |
|---|---|
| `server.py` | 云上入口：网页控制台 + 短链代理 + MCP 桥接子进程 |
| `stream_proxy.py` | 短链代理：补 B 站 Referer、缩短 755 字符音频地址 |
| `bili_core.py` | B 站搜索（wbi 签名 + 指纹 + 风控退避）与音频解析 |
| `radio_core.py` | 蜻蜓电台、Jamendo、gitee 歌单的数据层 |
| `radio_mcp.py` | MCP 工具层：点歌、搜台、播放队列、字节预算裁剪 |
| `mcp_pipe.py` | 小智官方桥接脚本，出站连 `wss://api.xiaozhi.me` |

电台数据不在本仓库里，直接读 gitee `nif99/niffm` 和 `nif99/gedan`；
在 gitee 上增删改台，约 60 秒后自动生效，不需要重新部署。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8080` | HTTP 端口 |
| `MCP_ENDPOINT` | 空 | 小智接入点，设置后开机自动连 |
| `PUBLIC_BASE_URL` | 自动学 | 对外域名，用于生成音频短链；不设会从请求 Host 头自动学 |
| `KEEPALIVE_SEC` | `600` | 自探活间隔，防止免费实例空闲缩容 |
| `STREAM_PROXY` | 空 | 设为 `0` 时不在子进程里另起代理（server.py 已自动处理） |

## 注意

- `.endpoint` 里存的是接入点 token，已在 `.gitignore` 中排除，**不要提交**。
- 免费实例只有一个 Web Service 名额；本服务正好占一个。
