# MiniMax Anthropic Demo

一个最小可运行的 Python HTTP 服务，使用 [Anthropic SDK](https://docs.anthropic.com/) 通过 [MiniMax 开放平台](https://platform.minimax.cn/) 提供的 Anthropic 兼容接口调用 **MiniMax-M3** 模型。

参考文档：[Anthropic SDK - MiniMax 开放平台文档](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)

## 功能

- 启动一个 FastAPI 服务，监听 `0.0.0.0:8000`
- 提供 `/hello` 接口，支持 `POST` (JSON body) 和 `GET` (query string) 两种方式调用
- 内部使用 `anthropic.Anthropic()` 客户端，把 base URL 指向 `https://api.minimax.cn/anthropic`
- 返回结构化的 JSON，包含模型输出文本、token 用量、stop reason 等

## 目录结构

```
.
├── app.py            # FastAPI 应用 + Anthropic 客户端 + /hello 端点
├── requirements.txt  # Python 依赖
├── .env.example      # 环境变量模板
├── .env              # 本地真实配置（含 API key，已 gitignore）
├── .gitignore
└── run.sh            # 便捷启动脚本（自动加载 .env，启动 uvicorn）
```

## 快速开始

### 1. 安装依赖

建议使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，把 ANTHROPIC_API_KEY 替换成你自己的 MiniMax 订阅 key
```

`.env` 默认内容：

```env
ANTHROPIC_BASE_URL=https://api.minimax.cn/anthropic
ANTHROPIC_API_KEY=YOUR_API_KEY_HERE
MINIMAX_MODEL=MiniMax-M3
MAX_TOKENS=1000
HOST=0.0.0.0
PORT=8000
```

`.env` 已被 `.gitignore` 排除，不会被提交。

### 3. 启动服务

```bash
./run.sh
# 或：
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## 调用示例

### POST `/hello`

```bash
curl -X POST http://localhost:8000/hello \
  -H "Content-Type: application/json" \
  -d '{"text": "用一句话介绍一下你自己"}'
```

可选字段：

```json
{
  "text": "用户问题",
  "system": "可选，自定义 system prompt",
  "model": "可选，默认 MiniMax-M3",
  "max_tokens": "可选，默认 1000"
}
```

### GET `/hello`

```bash
curl "http://localhost:8000/hello?text=hello"
```

### 响应示例

```json
{
  "model": "MiniMax-M3",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 12,
    "output_tokens": 87
  },
  "thinking": null,
  "text": "你好！我是 MiniMax-M3，一个由 MiniMax 训练的大语言模型..."
}
```

### 其他端点

- `GET /` — 返回服务基本信息（base URL、默认模型、可用端点）
- `GET /healthz` — 健康检查

## 切换模型

修改 `.env` 中的 `MINIMAX_MODEL`，或在请求中传 `model` 字段。可选值（来自官方文档）：

| 模型名 | 上下文窗口 | 定位 |
|---|---|---|
| `MiniMax-M3` | 1,000,000 | 最新 M 系列，Agent / 工具调用 / 长上下文 |
| `MiniMax-M2.7` / `MiniMax-M2.7-highspeed` | 204,800 | 自我迭代（highspeed ≈ 100 TPS） |
| `MiniMax-M2.5` / `MiniMax-M2.5-highspeed` | 204,800 | 顶尖性能 |
| `MiniMax-M2.1` / `MiniMax-M2.1-highspeed` | 204,800 | 多语言编程 |
| `MiniMax-M2` | 204,800 | 高效编码与 Agent |

## 注意事项

- `temperature` 推荐设为 `1.0`，取值范围 `[0, 2]`
- `MiniMax-M3` 默认关闭 thinking；如需开启，传 `thinking: {"type": "adaptive"}`（如需扩展可改 `app.py`）
- API key 仅保存在本地 `.env`，请勿提交到 git
- 如果遇到问题，可联系 `Model@minimaxi.com` 或在 [MiniMax-M2 GitHub 仓库](https://github.com/MiniMax-AI/MiniMax-M2/issues) 提 issue