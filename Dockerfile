FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# 先只拷依赖清单，让这一层能被缓存住
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 容器里跑 server.py 一个进程，它同时干三件事：
#   /                 网页：粘贴小智接入点、看状态
#   /p/{token}        短链代理，设备从这里拉音频
#   子进程 mcp_pipe.py 出站连 wss://api.xiaozhi.me（MCP 桥接）
# 平台只需要把 PORT 暴露出来，不需要别的入站规则。
EXPOSE 8080

CMD ["python", "server.py"]
