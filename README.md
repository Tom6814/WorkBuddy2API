# 🚀 WorkBuddy2API

把 WorkBuddy 的内部 API 包装成 **OpenAI 兼容的 REST API**，一行部署，随处调用。

> English version: [README.en.md](README.en.md)

---

## 这是什么？

一个超轻量的代理服务：你在 WorkBuddy 里能用的所有模型（DeepSeek、Kimi、GLM、混元、MiniMax……），现在都能通过标准的 OpenAI 接口调出来。任何支持 OpenAI API 的工具（Claude Code、各类客户端、脚本……）都能直接用上 WorkBuddy 的模型。

## 特性一览

- ✨ **OpenAI 完全兼容** — `/v1/chat/completions`、`/v1/models`、tools/tool_calls 透传
- 🧠 **思考内容输出** — 支持 `reasoning_content`（推理过程）透传
- 🚀 **默认最高深度思考** — 不传参数也能拿到深度推理结果
- ⚡ **默认流式输出** — SSE 边想边答，延迟更低
- 🖼️ **AI 生图** — 文生图 `/v1/images/generations`、图生图 `/v1/images/edits`
- 🔄 **Token 自动刷新** — 过期自动刷新，无需手动干预
- 🛡️ **反封号保护** — 内置限速、随机延迟、UA 轮换、指数退避
- 🐳 **一行部署** — Docker / Zeabur / Railway 随便放

## 快速开始

### 1. 拿到 WodeBuddy Token

登录 WorkBuddy 后，token 会自动存在本地：

```bash
cat ~/Library/Application\ Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
```

### 2. 本地运行

```bash
# 安装依赖
pip install fastapi uvicorn

# 设置环境变量并启动
export CODEBUDDY_AUTH_TOKEN="你的token"
export API_KEY="你自定的key"
python server.py
```

### 3. 部署到 Zeabur / Railway / 任意 Docker 平台

只需设置两个环境变量：

| 变量 | 必填 | 说明 |
|------|------|------|
| `CODEBUDDY_AUTH_TOKEN` | ✅ | WorkBuddy 的 Bearer Token |
| `API_KEY` | ✅ | 调用本 API 所需的密钥 |
| `DEFAULT_MODEL` | ❌ | 默认模型，默认 `deepseek-v3` |
| `DEFAULT_THINKING` | ❌ | 默认思考深度，默认 `max` |
| `PORT` | ❌ | 监听端口，默认 `8000` |

## 调用示例

**聊天（流式，默认最高思考）：**

```bash
curl https://your-domain/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kimi-k3-1",
    "messages": [{"role": "user", "content": "用 Python 写一个快速排序"}]
  }'
```

**指定思考深度：**

```bash
-d '{
  "model": "deepseek-v3",
  "messages": [...],
  "reasoning_effort": "high"
}'
```

`reasoning_effort` 可选值：`low` | `medium` | `high` | `max` | `off`

**工具调用（tools 透传）：**

```bash
-d '{
  "model": "glm-5.2",
  "messages": [{"role": "user", "content": "今天天气怎么样？"}],
  "tools": [{"type": "function", "function": {"name": "get_weather", "description": "查天气", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
}'
```

**文生图：**

```bash
curl https://your-domain/v1/images/generations \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a cute orange cat, cartoon style", "model": "hunyuan-image-v3.0"}'
```

**图生图：**

```bash
curl https://your-domain/v1/images/edits \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "make it blue", "image": "data:image/png;base64,..."}'
```

`image` 支持：data URL / http(s) URL / raw base64 / 本地路径。

## 可用模型

```
# DeepSeek
deepseek-v3        deepseek-v3-0324        deepseek-v3-1
deepseek-v3-0324-lkeap  deepseek-v3-1-lkeap
deepseek-r1        deepseek-r1-0528        deepseek-r1-0528-lkeap
deepseek-v4-flash  deepseek-v4-pro         deepseek-v3-2-volc

# Kimi
kimi-k2.5          kimi-k2.6               kimi-k2.7
kimi-k3-1

# Hunyuan
hunyuan-chat       hunyuan-2.0-instruct    hunyuan-2.0-thinking

# MiniMax
minimax-m2.7

# GLM
glm-4.7            glm-5.0                 glm-5.1
glm-5.2            glm-5.0-turbo           glm-5v-turbo

# HY
hy3                hy3-preview             hy3-preview-agent

# 生图
hunyuan-image-v3.0                        （文生图）
hunyuan-image-v2.0-general-edit           （图生图）
```

> 也可以通过 `GET /v1/models` 实时获取完整模型列表。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/v1/models` | 列出所有支持的模型 |
| `POST` | `/v1/chat/completions` | 聊天补全（支持 SSE 流式） |
| `POST` | `/v1/images/generations` | 文生图 |
| `POST` | `/v1/images/edits` | 图生图 |

## 项目结构

```
codebuddy-api-server/
├── server.py                 # OpenAI 兼容 REST API 服务器
├── codebuddy_direct_api.py   # CodeBuddy 直连客户端（token 管理/SSE/生图）
├── Dockerfile                # Python 3.11 容器
└── README.md
```

---

*Happy coding!* 🎉
