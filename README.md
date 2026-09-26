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

#### 数据来源：web search 兜底

LLM 的训练知识有截止日期，对**近期赛事（报名期、比赛日期、季节标签、赛事认证等级）**几乎必然会编造或过时。为此 `/race/batch` 在调用模型前会**先做一次 web 搜索**，把真实片段塞进 prompt，让模型基于"搜索结果优先 + 自己知识次之"的策略回答。

实现要点：

1. **服务端先搜**：每条 race 独立跑一次搜索，并发受 `_analyze_race_batch` 的 `concurrency` 控制。
2. **两套搜索引擎**（按优先级）：
   - **Tavily Search API**（**默认**，只要 `.env` 里有 `TAVILY_API_KEY` 就走这条）：专为 LLM 设计，输出干净。免费层每月 1000 次：[tavily.com](https://tavily.com)
   - **DuckDuckGo HTML**：无需 key，直接抓 `html.duckduckgo.com/html/`，可作为兜底。**可能被限流**，生产建议配 Tavily
3. **结果缓存**：同一 `race_name` 在 `WEB_SEARCH_CACHE_TTL` 秒内（默认 30 分钟）复用上一次搜索结果，避免重复打网络。
4. **Tavily 配额保护**（重点）：
   - 当 Tavily 返回 **401 / 402 / 403 / 429** 时，service 自动进入冷却期 `TAVILY_COOLDOWN_SECONDS` 秒（默认 3600s = 1h）
   - 冷却期内 `_search_web` 静默跳过 Tavily，直接走 DDG——**不再消耗 Tavily 配额**
   - 如果 Tavily 的 `Retry-After` header 有值，冷却时长取 header 值（≤1h）
   - 通过 `GET /` 的 `search.tavily.{active, in_cooldown, cooldown_remaining_sec, last_disabled_reason}` 字段可观察当前状态
5. **失败降级**：搜索返回 None / 超时 / 解析失败 → 该 race 静默退回纯 LLM 路径，`confidence` 自然会偏低，但**不影响响应**。

实际验证（用你的 `TAVILY_API_KEY`）：

| 输入 | race_date | 季节标签 | confidence | 额外 |
|---|---|---|---|---|
| 2026南京马拉松 | `2026-11-22` | 秋季赛事 ✓ | high | 世界田联铜标 / A级赛事 / 历史文化 |
| 2026上海半程马拉松 | `2026-03-15` | 春季赛事 ✓ | high | 田协认证 / 金标赛事 / 直通上马 / 上海地标 |
| 2026北京马拉松 | `2026-10-18` | 秋季赛事 ✓ | high | IAAF金标赛事 / 全国马拉松锦标赛 / 国马 |

关闭 web search：

```env
WEB_SEARCH_ENABLED=false
```

关闭后会回到纯模型模式，`/race/batch` 的 `race_date` 等字段很可能回到 null（除非模型确实记得）。

观察 search 状态：

```bash
curl -s http://localhost:8000/ | jq .search
# {
#   "enabled": true,
#   "primary": "tavily",
#   "fallback": "duckduckgo",
#   "tavily": {
#     "configured": true,
#     "active": true,
#     "in_cooldown": false,
#     "cooldown_remaining_sec": 0,
#     "last_disabled_reason": ""
#   }
# }
```

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

## race_analyses 持久化缓存

`/race/batch` 的结果会自动写入 mongo 的 `race_analyses` 集合（key 是 `race_name`，unique index）。同一赛事后续查询直接读 mongo，**不打 Tavily、不调模型**——既快又省配额。

**每条结果的 `source` 字段**告诉调用方本次响应是怎么来的：

| `source` | 含义 |
|---|---|
| `manual` | 通过 `/race/correction` 人工录入的数据（最高优先级） |
| `cache` | 自动模型生成的结果，本次从 mongo 缓存读取 |
| `model` | 本次实时调用模型生成的（同时已写回 mongo，下次就成 `cache`） |

实测：

```
Run 1（5 race 全 cache miss）：elapsed 11.2s，5 race 都调模型 + 搜索
Run 2（同 5 race）：client wall 36ms，server elapsed 0ms，source 全 cache
```

### 适用场景

- **重复查询**：同一赛事被前端批量 / 多次调用，节省所有下游成本
- **数据稳定**：比赛日期、地点、tags 不会变（除非组织方改路线），首次入库永久有效

### `POST /race/correction`

模型返回了错误或 null，而你手头有权威数据（官网、纸质手册、GPX 轨迹、Strava 链接等）时，用这个接口覆盖：

```bash
curl -X POST http://localhost:8000/race/correction \
  -H "Content-Type: application/json" \
  -d '{
    "race_name": "2026南京马拉松",
    "race_date": "2026-11-22",
    "registration_start_date": "2026-09-15",
    "registration_end_date": "2026-10-31",
    "location": "南京",
    "distance_category": "full_marathon",
    "tags": ["城市马拉松", "田协认证", "秋季赛事"],
    "summary": "南京马拉松是华东地区知名城市马拉松...",
    "confidence": "high"
  }'
```

成功响应：

```json
{ "ok": true, "race_name": "2026南京马拉松", "source": "manual", "updated_at": "..." }
```

下次 `/race/batch` 查这个赛事会直接返回 `source: "manual"`、零耗时。所有字段（除了 `race_name`）都是可选的，**只传你想覆盖的部分**即可。

### `GET /race/cache`

缓存状态：

```bash
curl http://localhost:8000/race/cache
# {
#   "healthy": true,
#   "total": 5,
#   "by_source": { "model": 4, "manual": 1 }
# }
```

### `GET /race/cache`

缓存状态：

```bash
curl http://localhost:8000/race/cache
# {
#   "healthy": true,
#   "total": 5,
#   "by_source": { "model": 4, "manual": 1 }
# }
```

## 后续可扩展（未实现）

- **TTL 自动失效**：当前是永久缓存。如果路线改了，需要 `/race/correction` 覆盖（已经支持）或者加 `?force_refresh=true` 强制重生成。

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

## 本地开发与测试

完全在本机跑这套 stack，连接**已部署的远程 MongoDB**（不再依赖 docker）。

### 1. 准备 venv + 依赖

```bash
cd smart-run-minimax        # 仓库根目录
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
```

> ⚠️ **关键陷阱**：直接 `python3 -m uvicorn ...` 会用系统 python（macOS 上通常是 `/opt/anaconda3/bin/python3`），那个 python 没有装 `anthropic` 等依赖，进程会立即挂掉。**始终用绝对路径 `.venv/bin/python`** 或者先 `source .venv/bin/activate`。

### 2. 配置 `.env`（最小化）

复制模板并填值：

```bash
cp .env.example .env
```

`.env` 必填项：

```env
ANTHROPIC_BASE_URL=https://api.minimax.cn/anthropic
ANTHROPIC_API_KEY=sk-cp-...                # 你的 MiniMax 订阅 key

# 本地直接跑（不走 docker-compose）时，需要指向远程 mongo
MONGO_URL=mongodb://minimax_admin:YOUR_PASS@47.121.29.106:27017/minimax_demo?authSource=admin
MONGO_DB=minimax_demo
MONGO_COLLECTION=conversations
```

> `app.py` 里 `load_dotenv(override=True)`，所以 shell 里如果预设了 `ANTHROPIC_BASE_URL=ikuncode` 这种旧值也不会影响启动。

### 3. 启动服务

```bash
# 推荐：直接用绝对路径，不依赖 shell activate
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000

# 或者用脚本
.venv/bin/python run.sh       # 见底部说明
```

看到以下输出即启动成功：

```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
2026-09-25 10:28:15 INFO minimax-hello - MongoDB connected: db=minimax_demo ...
```

### 4. 烟测：根路径 + 健康检查

```bash
curl -s http://127.0.0.1:8000/ | python3 -m json.tool
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

`/healthz` 应该返回 `mongo.healthy: true`。

### 5. `/hello` 接口

```bash
# POST (推荐)
curl -s -X POST http://127.0.0.1:8000/hello \
  -H "Content-Type: application/json" \
  -d '{"text": "用一句话介绍你自己"}' | python3 -m json.tool

# GET (中文必须 URL encode)
curl -s --get http://127.0.0.1:8000/hello \
  --data-urlencode "text=你好" | python3 -m json.tool
```

### 6. `/history` 接口

```bash
curl -s "http://127.0.0.1:8000/history?limit=5" | python3 -m json.tool
```

### 7. `/race/batch` 接口（并发批量）

```bash
# POST — 5 个赛事并发，~3.7s 完成
curl -s -X POST http://127.0.0.1:8000/race/batch \
  -H "Content-Type: application/json" \
  -d '{
    "race_names": [
      "2026南京马拉松",
      "2026上海半程马拉松",
      "2026北京马拉松",
      "2026厦门马拉松",
      "2026成都马拉松"
    ],
    "concurrency": 5
  }' | python3 -m json.tool

# GET — 中文必须 URL encode
curl -s --get http://127.0.0.1:8000/race/batch \
  --data-urlencode "race_names=2026武汉马拉松" \
  --data-urlencode "race_names=2026杭州马拉松" \
  --data-urlencode "concurrency=2" | python3 -m json.tool
```

返回里每个 `results[i]` 要么是完整 `RaceItemResult`，要么是 `{race_name, error}`。

### 8. 一键回归脚本

把下面所有调用打包跑一遍：

```bash
#!/usr/bin/env bash
# 本地回归脚本：依次调每个接口，确认没有 5xx
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "1) /healthz"
curl -fsS "$BASE/healthz" >/dev/null && echo "   OK"

echo "2) POST /hello"
curl -fsS -X POST "$BASE/hello" -H "Content-Type: application/json" \
  -d '{"text":"smoke test"}' >/dev/null && echo "   OK"

echo "3) GET /hello"
curl -fsS --get "$BASE/hello" --data-urlencode "text=smoke" >/dev/null && echo "   OK"

echo "4) GET /history?limit=1"
curl -fsS "$BASE/history?limit=1" >/dev/null && echo "   OK"

echo "5) POST /race/batch"
curl -fsS -X POST "$BASE/race/batch" -H "Content-Type: application/json" \
  -d '{"race_names":["2026南京马拉松","2026北京马拉松"],"concurrency":2}' >/dev/null \
  && echo "   OK"

echo
echo "ALL GREEN ✓"
```

保存为 `scripts/smoke.sh`，`chmod +x` 后即可 `BASE=http://your-host:8000 ./scripts/smoke.sh`。

### 9. 常见问题排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: anthropic` | 用系统 python 起的 uvicorn | 改用绝对路径 `.venv/bin/python` |
| `Invalid HTTP request received` | GET 路径里中文没 URL encode | 用 `--data-urlencode` 或 `requests`/`urllib.parse.quote` |
| `/healthz` 的 `mongo.healthy: false` | `.env` 里 `MONGO_URL` 缺失或密码错 | 检查 `.env`，或 `python3 tests/test_mongo.py` 直接验证 mongo 连通性 |
| `/hello` 返回 401 | `ANTHROPIC_API_KEY` 缺失或无效 | 检查 `.env` 里的 key 是否过期 |
| `/race/batch` 的 `failed` 字段非零 | 至少一条 race 的上游调用失败 | 查看 `results[i].error` 字段定位是限流还是网络 |
```