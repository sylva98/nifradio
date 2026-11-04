# 小智收音机 · MCP 服务

把浏览器插件里的电台、歌单和 B 站点歌接进小智 AI，让设备自己出声。

服务是标准的 **STDIO 型 MCP**，共 10 个工具：

| 工具 | 用途 |
|---|---|
| `play_song` | 点歌（说「放周杰伦」直接播） |
| `next_song` / `replay_song` | 下一首 / 重播当前 |
| `list_radio_categories` | 列出蜻蜓的 13 个广播分类 |
| `list_radio_stations` | 某个分类下的电台 |
| `find_radio_station` | 按名字找电台（「清晨」「交通台」） |
| `search_song` | 在 B 站搜歌，返回候选 |
| `get_song_url` | 取某一首的音频地址 |
| `search_open_music` | 开放版权曲库（Jamendo） |
| `play_on_computer` | 在本机播放器里放（仅本地调试用） |

## MCP Server Config

```json
{
  "mcpServers": {
    "nifradio": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/sylva98/nifradio", "nifradio-mcp"]
    }
  }
}
```

已经发布到 PyPI 的话，把 `args` 简化成 `["nifradio-mcp"]` 即可。

## 在魔搭 MCP 广场创建服务

1. 打开 <https://modelscope.cn/mcp/servers/create?template=customize>
2. 创建类型选 **自定义创建**
3. 基础信息

   | 字段 | 值 |
   |---|---|
   | 英文名 | `nifradio` |
   | 中文名 | `小智收音机` |
   | 来源地址 | `https://github.com/sylva98/nifradio` |
   | 托管类型 | **可托管部署** |

4. 服务配置：粘贴上面那段 `mcpServers` JSON
5. README：写清项目简介 / 部署指南 / 使用示例三部分（平台要求）
6. 创建后平台会自动做托管部署检测，通过后给出
   `https://mcp.api-inference.modelscope.net/{id}/sse`

## 关于「桥」

魔搭托管出来的是**被动 SSE 地址**，而小智控制台只提供**主动连出去的 wss 接入点**，
两者方向相反，不能直接对接。需要在中间放一座桥：

```
魔搭 SSE（工具）  ←→  桥  ←→  wss://api.xiaozhi.me（小智）
```

可选做法：

- **小智Pro** 的「接入点MCP」：云端替你保持桥，支持 SSE / StreamableHttp / STDIO，自动重连
- **xiaozhi-client**：本地跑 `npx -y xiaozhi-client start`，需要一台常开的机器

## 工具行为说明

- **播放架构（2026-11-04 起）**：不再是「把链接给设备让它自己拉」，而是走小智的
  **音频资源协议**——工具结果里除文本外附带 `resource_link`
  （`uri=resource://read_from_http`，真实地址放 `description`），
  小智云端再用 `resources/read` 按 100KB 分页把音频读走，解码后推到设备喇叭。
  入口是 `mcp_audio_server.py`（server.py 桥接拉起的就是它）。
- **格式与转码（`media.py`）**：先读前 1KB 验身再决定路线
  - 纯 MP3（蜻蜓直播、Jamendo 歌单）→ **透传**，按 Range 分页，零开销；
  - m4s / fMP4(AAC) / m3u8(HLS) / flac / ogg → **ffmpeg 转 MP3**（128k 44.1k 立体声），
    边转边落盘、分页读取渐进可用，首屏约 1~2 秒；
  - 单次任务用 `AUDIO_MAX_SECONDS`（默认 1800 秒）截断，避免 7 小时合集拖爆；
  - 对外 `mimeType` 统一报 `audio/mpeg`（转码后一定是 MP3）。
  - ffmpeg 优先取系统的，缺失时用 `imageio-ffmpeg` 自带的静态二进制。
- **B 站 CDN 要求**：不带 `Referer: https://www.bilibili.com/` 一律 403
  （nginx HTML，323 字节）。`media.py` 在服务端统一加上，设备/云端都不用管。
  蜻蜓 MP3 流无此要求。
- **播放队列**：进程内记「上次点了什么、放到第几首」，`next_song` 往后翻一个。
- **字节预算**：小智读回结果约 1024 字节封顶，播放类回复已精简到 951 字节以内。

## 自建容器部署（可选）

`server.py` 是一个单进程入口，把「网页控制台 + 短链代理 + MCP 桥接」合在一起，
适合跑在自己的 VPS / 容器里：

```bash
pip install -r requirements.txt
PORT=8080 python server.py
```

打开网页，把小智控制台的 wss 接入点粘进去，点「连接」。日志出现
`Successfully connected` 和 `ListToolsRequest` 即成功。
也可用环境变量 `MCP_ENDPOINT` 免手工粘贴。

| 文件 | 作用 |
|---|---|
| `nifradio_mcp.py` | 托管/uvx 入口（纯 STDIO） |
| `pyproject.toml` | 打包配置，console script = `nifradio-mcp` |
| `radio_mcp.py` | MCP 工具层：点歌、搜台、播放队列、字节预算 |
| `bili_core.py` | B 站搜索（wbi 签名 + 指纹 + 风控退避）与音频解析 |
| `radio_core.py` | 蜻蜓电台、Jamendo、gitee 歌单数据层 |
| `server.py` | 自建部署入口：网页 + 短链代理 + 桥接 |
| `stream_proxy.py` | 短链代理（仅自建部署需要） |
| `mcp_pipe.py` | 小智官方桥接脚本 |

电台数据不在本仓库，直接读 gitee `nif99/niffm` 和 `nif99/gedan`；
在 gitee 上增删改台约 60 秒后生效，不需要重新部署。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PUBLIC_BASE_URL` | 空 | 设了才启用短链代理；不设则直接返回 B 站原始地址 |
| `STREAM_PROXY` | 空 | 设为 `0` 强制关闭短链代理 |
| `PORT` | `8080` | 自建部署的 HTTP 端口 |
| `MCP_ENDPOINT` | 空 | 小智 wss 接入点，自建入口用它自动重连 |

## 注意

- 小智 MCP 接入点 token 存在 `.endpoint`，已在 `.gitignore` 排除，**不要提交**。
- `mcp` 依赖锁在 `1.x`：2.0 把 FastMCP 改名、传输层协议也变了。
