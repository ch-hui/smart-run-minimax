# MiniMax Anthropic Demo

一个最小可运行的 Python HTTP 服务，使用 [Anthropic SDK](https://docs.anthropic.com/) 通过 [MiniMax 开放平台](https://platform.minimax.cn/) 提供的 Anthropic 兼容接口调用 **MiniMax-M3** 模型，并把每次对话持久化到 MongoDB。

参考文档：[Anthropic SDK - MiniMax 开放平台文档](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)

## 部署信息

| 项 | 值 |
|---|---|
| 服务器公网 IP | `47.121.29.106` |
| 应用访问 | `http://47.121.29.106:8000` |
| 健康检查 | `http://47.121.29.106:8000/healthz` |
| MongoDB 监听 | `47.121.29.106:27017`（主机端口 27017 已暴露到容器外） |
| 数据库 | `minimax_demo` / 集合 `conversations` |
| Mongo 用户 | `minimax_admin` |
| Mongo 密码 | 见服务器 `.env` 文件中的 `MONGO_PASS`（**不入库**） |

完整 MongoDB 连接 URI（生产部署版本）：

```
mongodb://minimax_admin:<MONGO_PASS>@47.121.29.106:27017/minimax_demo?authSource=admin
```

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

### `POST /race/batch`

并发批量分析比赛。一次请求传入 1–20 个赛事名，服务端用 `asyncio.gather` + 信号量控制并发度，把同步 SDK 调用丢进线程池执行，N 条 race 的总耗时约等于"最慢那条"的耗时，而不是 N× 单条耗时。

请求体：

```json
{
  "race_names": ["2026南京马拉松", "2026上海半程马拉松", "2026北京马拉松"],
  "model": "MiniMax-M3",
  "max_tokens": 1500,
  "concurrency": 5
}
```

字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `race_names` | 是 | 1–20 个比赛名；空白和重复条目会被自动去重 |
| `model` | 否 | 默认 `MINIMAX_MODEL` 环境变量 |
| `max_tokens` | 否 | 默认 1500 |
| `concurrency` | 否 | 1–10，默认 5；自动 clamp 到 `len(race_names)` |

固定响应 schema（`RaceBatchResponse`）：

```json
{
  "count": 3,
  "success": 3,
  "failed": 0,
  "elapsed_ms": 3682,
  "results": [
    {
      "race_name": "2026南京马拉松",
      "race_date": "2026-11-29",
      "registration_start_date": null,
      "registration_end_date": null,
      "location": "南京",
      "distance_category": "full_marathon",
      "tags": ["城市马拉松", "田协认证", "秋季赛事", "pb友好", "全马+半马"],
      "summary": "……",
      "confidence": "medium",
      "model": "MiniMax-M3",
      "usage": {"input_tokens": 102, "output_tokens": 226}
    },
    ...
  ]
}
```

`results` 中每个元素要么是 `RaceItemResult`（成功的固定 schema），要么是 `RaceItemError`：

```json
{ "race_name": "某赛事", "error": "upstream 503: rate limit" }
```

一条 race 失败不会影响其他 race（并发独立执行，每条单独 try/except）。

`distance_category` 取值：`full_marathon` / `half_marathon` / `10k` / `5k` / `trail` / `ultra` / `other`
`confidence` 取值：`high` / `medium` / `low` —— 不确定的字段会设为 `null` 并把 `confidence` 降到 `low`，避免模型编造事实。

### `GET /race/batch`

query string 形式（中文必须 URL encode）：

```bash
curl --get "http://localhost:8000/race/batch" \
  --data-urlencode "race_names=2026武汉马拉松" \
  --data-urlencode "race_names=2026杭州马拉松" \
  --data-urlencode "concurrency=2"
```

不编码的中文 query 会触发 HTTP 协议级错误（`Invalid HTTP request received`），这是 FastAPI/Starlette 拒绝处理非法 URL 的正常行为。

> 历史说明：早期版本曾提供单条接口 `/race/analyze`，已删除，统一走批量接口以提升吞吐。

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

## 测试

仓库自带一个 MongoDB 烟雾测试脚本，使用同步 `pymongo` 驱动，验证"连得上 → 能写 → 能查 → 能清理"。

```bash
# 1) 安装依赖（如果还没装）
pip install -r requirements.txt

# 2) 确保 .env 里有 MONGO_PASS（或显式 export）
python3 tests/test_mongo.py
```

脚本会：
1. 用 `MONGO_URL`（默认指向 `47.121.29.106` 这台部署实例）打开认证连接
2. `ping` 数据库
3. 在 `smoke_test` 集合插入一条带 UUID marker 的文档
4. 按 marker 查回该文档
5. 统计 `smoke_test` 集合文档数
6. 删除刚插入的 marker 文档（集合保持干净，可重复运行）

预期输出（实际部署验证通过）：

```
[2026-09-19T03:04:46+00:00] connecting to mongodb://***@47.121.29.106:27017/...
[2026-09-19T03:04:46+00:00] ping ok: {'ok': 1.0}
[2026-09-19T03:04:46+00:00] insert ok: _id=... marker=smoke-...
[2026-09-19T03:04:46+00:00] read ok: marker=... created_at=...
[2026-09-19T03:04:46+00:00] count ok: smoke_test now has 1 docs total
[2026-09-19T03:04:46+00:00] cleanup ok: removed 1 doc(s) with marker=...
[2026-09-19T03:04:46+00:00] ALL CHECKS PASSED ✓
```

要测试其他实例：

```bash
export MONGO_URL='mongodb://USER:PASS@host:27017/dbname?authSource=admin'
python3 tests/test_mongo.py
```