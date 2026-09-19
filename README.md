# MiniMax Anthropic Demo

一个最小可运行的 Python HTTP 服务，使用 [Anthropic SDK](https://docs.anthropic.com/) 通过 [MiniMax 开放平台](https://platform.minimax.cn/) 提供的 Anthropic 兼容接口调用 **MiniMax-M3** 模型，并把每次对话持久化到 MongoDB。

参考文档：[Anthropic SDK - MiniMax 开放平台文档](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)

## 功能

- FastAPI 服务，监听 `0.0.0.0:8000`
- `/hello` 同时支持 `POST` (JSON body) 和 `GET` (query string) 两种调用方式
- 内部使用 `anthropic.Anthropic()` 客户端，base URL 指向 `https://api.minimax.cn/anthropic`
- 调用结果自动写入 MongoDB `conversations` 集合（带索引）
- `/history` 列出最近的对话记录
- 提供 `docker-compose.yml`，一键拉起 app + mongo 7
- MongoDB 数据持久化到 `./data/mongo`

## 目录结构

```
.
├── app.py               # FastAPI 应用 + Anthropic 客户端 + MongoDB 持久化
├── requirements.txt     # Python 依赖（包含 motor / pymongo）
├── Dockerfile           # app 容器镜像
├── docker-compose.yml   # 一键部署 app + mongo
├── .env.example         # 环境变量模板（含 MONGO_USER/MONGO_PASS/...）
├── .env                 # 本地真实配置（含 API key + mongo 密码，已 gitignore）
├── .gitignore
├── run.sh               # 本地直接启动（非 docker）
└── README.md
```

## 快速开始（两种方式）

### 方式 A：docker-compose 部署（推荐）

#### 1. 准备环境变量

```bash
cp .env.example .env
# 编辑 .env，至少修改以下几项：
#   ANTHROPIC_API_KEY  →  你的 MiniMax 订阅 key
#   MONGO_PASS         →  一个强密码（首次部署请改掉示例值）
```

#### 2. 启动服务

```bash
docker compose up -d --build
```

首次构建会拉取 `python:3.11-slim` 和 `mongo:7` 镜像，时间取决于网络。

#### 3. 验证

```bash
# 健康检查（含 mongo ping）
curl http://localhost:8000/healthz

# 调用 /hello
curl -X POST http://localhost:8000/hello \
  -H "Content-Type: application/json" \
  -d '{"text": "用一句话介绍你自己"}'

# 查看对话历史
curl http://localhost:8000/history?limit=10

# 查看日志
docker compose logs -f app
docker compose logs -f mongo
```

#### 4. 停 / 重启 / 清理

```bash
docker compose down            # 停止（保留数据）
docker compose down -v         # 停止并删除 mongo 数据卷（慎用）
docker compose restart app     # 只重启 app（修改 .env 后）
```

#### 5. 数据持久化

MongoDB 的数据文件落在容器内 `/data/db`，通过 volume 挂载到主机 `./data/mongo`（相对 docker-compose.yml 所在目录）。这个目录已经在 `.gitignore` 里，**不会被提交到 git**。

### 方式 B：本地直接运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env  # 编辑填入真实值

# 本地需要另起一个 mongo（最简单的办法是只跑 mongo 服务）
docker run -d --name minimax-mongo-local \
  -p 27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=minimax_admin \
  -e MONGO_INITDB_ROOT_PASSWORD=YOUR_STRONG_PASSWORD \
  -e MONGO_INITDB_DATABASE=minimax_demo \
  -v "$PWD/data/mongo:/data/db" \
  mongo:7

./run.sh
```

## API 文档

### `POST /hello`

```bash
curl -X POST http://localhost:8000/hello \
  -H "Content-Type: application/json" \
  -d '{"text": "法国的首都是哪里？只回答城市名"}'
```

请求体：

```json
{
  "text": "用户问题",
  "system": "可选，自定义 system prompt",
  "model": "可选，默认 MiniMax-M3",
  "max_tokens": "可选，默认 1000"
}
```

### `GET /hello`

```bash
curl "http://localhost:8000/hello?text=hello"
```

支持 query 参数 `text` / `system` / `model` / `max_tokens`。

### 响应示例

```json
{
  "model": "MiniMax-M3",
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 39, "output_tokens": 25},
  "thinking": null,
  "text": "我是MiniMax-M3，一个由MiniMax开发的人工智能助手...",
  "persisted_id": "65f0...",
  "mongo_healthy": true
}
```

### `GET /history?limit=N`

返回最近 N 条对话记录（默认 20，最大 200），按时间倒序。

```bash
curl "http://localhost:8000/history?limit=10"
```

### 其他端点

- `GET /` — 服务基本信息（默认模型、base URL、mongo 状态、可用端点）
- `GET /healthz` — 健康检查，同时探测 MongoDB 连接

## MongoDB 凭证约定

为了符合"凭证不提交 git 仓库"的要求：

| 文件 | 是否提交 | 内容 |
|---|---|---|
| `.env.example` | ✓ | 占位符 + 注释 |
| `.env` | ✗ (gitignore) | 真实 `ANTHROPIC_API_KEY` 和 `MONGO_PASS` |
| `docker-compose.yml` | ✓ | 只引用 `${VAR}` 占位符，docker compose 会自动从 `.env` 读取 |

部署到生产时，**强烈建议**把 `.env` 移到更安全的位置（如 Vault / Kubernetes Secret），并通过环境变量注入容器。

## 切换模型

修改 `.env` 中的 `MINIMAX_MODEL`，或在请求中传 `model` 字段。可选值：

| 模型名 | 上下文窗口 | 定位 |
|---|---|---|
| `MiniMax-M3` | 1,000,000 | 最新 M 系列，Agent / 工具调用 / 长上下文 |
| `MiniMax-M2.7` / `MiniMax-M2.7-highspeed` | 204,800 | 自我迭代 |
| `MiniMax-M2.5` / `MiniMax-M2.5-highspeed` | 204,800 | 顶尖性能 |
| `MiniMax-M2.1` / `MiniMax-M2.1-highspeed` | 204,800 | 多语言编程 |
| `MiniMax-M2` | 204,800 | 高效编码与 Agent |

## 注意事项

- `temperature` 推荐设为 `1.0`，取值范围 `[0, 2]`
- `MiniMax-M3` 默认关闭 thinking；如需开启，传 `thinking: {"type": "adaptive"}`（如需扩展可改 `app.py`）
- MongoDB 不可达时，`/hello` 仍能正常工作，只是不持久化、不返回 `persisted_id`
- API key 与 Mongo 密码都只保存在本地 `.env`，请勿提交到 git
- 如果遇到 MiniMax API 问题，可联系 `Model@minimaxi.com` 或在 [MiniMax-M2 GitHub 仓库](https://github.com/MiniMax-AI/MiniMax-M2/issues) 提 issue