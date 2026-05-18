---
name: qwenpaw_reactor_chat
description: 通过 REST API 与运行中的 QwenPaw 实例中的 Reactor 智能体对话。支持会话持久化、交互式会话选择，适用于任何外部智能体（Claude Code、Codex 等）。
metadata:
  builtin_skill_version: "1.0"
---

# QwenPaw Reactor 对话

通过 REST API 与 QwenPaw 中的 **Reactor** 智能体进行对话。

适用于**任何智能体**（Claude Code、Codex、Cursor 等）需要咨询 Reactor —— 一个 QwenPaw 托管的 AI 编程助手。

## 概览

| 项目 | 值 |
|------|-----|
| 目标智能体 ID | `Reactor` |
| 默认 API 端口 | `7860`（如果可用，会从 QwenPaw 配置自动检测） |
| 对话端点 | `POST /api/console/chat` |
| 认证 | 本地 `localhost` 请求自动跳过；远程需要 Bearer token |
| 响应格式 | Server-Sent Events (SSE) |
| 上下文持久化 | 通过 `session_id` 自动管理 |

## 快速开始

### 第一步：确定 QwenPaw API 端口

默认端口是 `7860`，但可能不同。检查 QwenPaw 配置：

```bash
cat ~/.qwenpaw/config.json | python3 -c "import sys,json; c=json.load(sys.stdin); print(c.get('api',{}).get('port', c.get('last_api',{}).get('port', 7860)))"
```

或者直接试 `7860` —— 如果失败，再检查配置。

### 第二步：选择会话

对话上下文按 `session_id` 维护。选择一个：

#### 选项 A — 使用预配置的会话

读取 `scripts/sessions.json` 查看已注册的会话。每个条目有 `name`（人类可读名称）和 `id`（实际的 session_id）。

#### 选项 B — 创建新会话

自己选一个 session_id（例如 `chat-20250514`）或自动生成。新会话从空白开始。

**如果未指定 session_id：** 脚本会交互列出 `sessions.json` 中的会话，让操作者选择或创建新的。

### 第三步：发送消息

运行对话脚本（从此 skill 目录）：

```bash
cd skills/qwenpaw_api_chat
python3 scripts/chat.py --message "你的问题" --session-id "选定的会话id" --port 8088
```

不带 `--session-id` 时，脚本会交互式提示：

```bash
python3 scripts/chat.py --message "你的问题" --port 8088
```

脚本将 Reactor 的回复以纯文本输出到 stdout。

### 第四步：继续对话

复用同一个 `--session-id` 发送后续消息。Reactor 会记住对话上下文：

```bash
python3 scripts/chat.py --message "后续问题" --session-id "选定的会话id" --port 8088
```

## API 详情

### 端点

```
POST http://localhost:{port}/api/console/chat
```

### 请求头

```
Content-Type: application/json
X-Agent-Id: Reactor
Authorization: Bearer <token>   # 仅远程访问时需要
```

### 请求体

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "你的消息内容"}
      ]
    }
  ],
  "channel": "api",
  "session_id": "你的会话id",
  "user_id": "api-user"
}
```

### 响应（SSE 流）

API 返回 Server-Sent Events。每行以 `data: ` 开头，后面跟 JSON：

```
data: {"sequence_number":0,"object":"response","status":"created",...}
data: {"sequence_number":1,"object":"response","status":"in_progress",...}
data: {"sequence_number":2,"object":"message","status":"in_progress",...}
data: {"sequence_number":3,"object":"content","status":"in_progress","text":"回复文本..."}
data: {"sequence_number":N,"object":"content","status":"completed","text":"完整回复"}
```

支持两种文本格式：
- **格式 A**：文本在 `output[].content[].text` 中（部分 QwenPaw 版本）
- **格式 B**：文本直接在 `event.text` 中（累积式 —— 每个事件包含到目前为止的完整文本）

脚本自动处理两种格式。

## 脚本参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--message` / `-m` | 是 | — | 要发送给 Reactor 的消息 |
| `--session-id` / `-s` | 否 | (交互式提示) | 用于对话上下文的会话 ID |
| `--port` / `-p` | 否 | `7860` | QwenPaw API 端口 |
| `--token` / `-t` | 否 | (无) | 认证令牌（仅远程访问时需要） |
| `--timeout` | 否 | `120` | 流式读取超时（秒） |
| `--verbose` / `-v` | 否 | 关闭 | 输出思考过程到 stderr（调试用） |

## 会话管理

### 添加会话

直接编辑 `scripts/sessions.json`。每个条目：

```json
{
  "name": "项目 Alpha 讨论",
  "id": "project-alpha-001"
}
```

### 删除会话

从 `sessions.json` 中删除条目。这**不会**删除 QwenPaw 服务器上的会话 —— 只是从便捷列表中移除。

### 以编程方式列出会话

```bash
cd skills/qwenpaw_api_chat
python3 -c "import json; [print(f'{s[\"id\"]}  —  {s[\"name\"]}') for s in json.load(open('scripts/sessions.json'))]"
```

## 错误处理

| 错误 | 原因 | 修复方法 |
|------|------|----------|
| 连接被拒绝 | QwenPaw 未运行或端口错误 | 检查 QwenPaw 是否运行；用 `--port` 确认端口 |
| 404 Agent Not Found | `X-Agent-Id` 错误 | 确保 QwenPaw 中存在 `Reactor` 智能体 |
| 401 Unauthorized | 缺少/无效的认证令牌 | 远程访问时提供 `--token` |
| 无文本响应 | 智能体发送了非文本响应（仅工具调用） | 检查智能体日志；回复可能只有工具调用 |

## 使用场景

- **Claude Code** 需要咨询 Reactor 获取代码的第二意见
- **Codex** 想向 Reactor 询问 QwenPaw 特定的 API 或工作流
- **任何外部智能体** 需要在多次调用之间与 Reactor 保持持久的多轮对话
