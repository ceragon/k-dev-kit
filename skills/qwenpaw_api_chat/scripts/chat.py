#!/usr/bin/env python3
"""
通过 REST API 与 QwenPaw 中的 Reactor 智能体对话。

用法：
    python3 chat.py --message "你的问题" [--session-id SESSION_ID] [--port PORT] [--token TOKEN] [--timeout SECONDS]

如果不提供 --session-id，脚本会交互式提示用户从 sessions.json 中选择会话或创建新会话。
"""

import argparse
import json
import sys
import socket
import time
import http.client
from pathlib import Path

# --- 常量 ---
AGENT_ID = "Reactor"
DEFAULT_PORT = 7860
DEFAULT_CHANNEL = "api"
DEFAULT_USER_ID = "api-user"
DEFAULT_TIMEOUT = 120  # 流式读取超时（秒）
CONNECT_TIMEOUT = 10   # 建立连接超时（秒），单独设置
MAX_RETRIES = 2        # 网络错误最大重试次数
RETRY_DELAY = 2        # 重试间隔（秒）
HEARTBEAT_INTERVAL = 60  # 心跳检测间隔（秒），工具调用期间可能静默较久

SCRIPT_DIR = Path(__file__).parent.resolve()
SESSIONS_FILE = SCRIPT_DIR / "sessions.json"


class ChatError(Exception):
    """对话过程中的错误。"""
    pass


class TimeoutError(ChatError):
    """超时错误。"""
    pass


class ConnectionError(ChatError):
    """连接错误。"""
    pass


def load_sessions():
    """从 sessions.json 加载预配置的会话。"""
    if SESSIONS_FILE.exists():
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_sessions(sessions):
    """将会话保存回 sessions.json。"""
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2, ensure_ascii=False)
        f.write("\n")


def prompt_session():
    """交互式提示用户选择或创建会话。"""
    sessions = load_sessions()

    if sessions:
        print("=== 可用会话列表 ===", file=sys.stderr)
        for i, s in enumerate(sessions, 1):
            print(f"  {i}. {s['name']}  (id: {s['id']})", file=sys.stderr)
        print(f"  {len(sessions) + 1}. 创建新会话", file=sys.stderr)
        print(file=sys.stderr)

        while True:
            choice = input("请选择会话（输入编号）或直接输入自定义会话 ID：").strip()
            if not choice:
                continue

            try:
                idx = int(choice)
                if 1 <= idx <= len(sessions):
                    return sessions[idx - 1]["id"]
                elif idx == len(sessions) + 1:
                    new_id = input("请输入新会话 ID：").strip()
                    if new_id:
                        new_name = input("会话名称（可选，按回车跳过）：").strip()
                        if not new_name:
                            new_name = new_id
                        sessions.append({"name": new_name, "id": new_id})
                        save_sessions(sessions)
                        print(f"会话 '{new_name}' 已保存，下次可直接使用。", file=sys.stderr)
                    return new_id
                else:
                    print("无效编号，请重试。", file=sys.stderr)
                    continue
            except ValueError:
                pass

            # 当作自定义会话 ID 处理
            return choice
    else:
        print("没有找到预配置的会话。", file=sys.stderr)
        new_id = input("请输入会话 ID：").strip()
        if not new_id:
            new_id = "default-session"
        new_name = input("会话名称（可选）：").strip() or new_id
        sessions.append({"name": new_name, "id": new_id})
        save_sessions(sessions)
        print(f"会话 '{new_name}' 已保存，下次可直接使用。", file=sys.stderr)
        return new_id


def _build_request(port, body, token=None):
    """构建 HTTP 请求对象。"""
    payload = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": body}
                ]
            }
        ],
        "channel": DEFAULT_CHANNEL,
        "session_id": "",  # 由调用者设置
        "user_id": DEFAULT_USER_ID,
    }

    request_body = json.dumps(payload)

    headers = {
        "Content-Type": "application/json",
        "X-Agent-Id": AGENT_ID,
        "Accept": "text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return request_body, headers


def _send_request(port, session_id, message, token=None):
    """发送一次请求，返回 response 对象或抛出异常。"""
    payload = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": message}
                ]
            }
        ],
        "channel": DEFAULT_CHANNEL,
        "session_id": session_id,
        "user_id": DEFAULT_USER_ID,
    }

    body = json.dumps(payload)

    headers = {
        "Content-Type": "application/json",
        "X-Agent-Id": AGENT_ID,
        "Accept": "text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    conn = http.client.HTTPConnection("localhost", port, timeout=CONNECT_TIMEOUT)

    try:
        conn.request("POST", "/api/console/chat", body=body, headers=headers)
        response = conn.getresponse()

        if response.status != 200:
            resp_body = response.read().decode("utf-8", errors="replace")
            conn.close()

            if response.status == 404:
                raise ChatError(f"智能体 '{AGENT_ID}' 未找到。请确认 QwenPaw 中已创建该智能体。")
            elif response.status == 401:
                raise ChatError("未授权。如果 QwenPaw 需要认证，请提供 --token 参数。")
            else:
                raise ChatError(f"HTTP 错误 {response.status}: {response.reason}\n响应: {resp_body[:300]}")

        return conn, response

    except (socket.timeout, OSError) as e:
        conn.close()
        raise ConnectionError(f"连接失败：{e}")


def _read_sse_stream(response, conn, timeout, verbose=False):
    """读取 SSE 流式响应，等待 response completed 才返回。

    QwenPaw agent SSE 事件序列：
      response created/in_progress
      message(reasoning) → content(text) deltas → completed   ← 思考
      message(plugin_call) → content(data) → completed        ← 工具调用
      message(plugin_call_output) → completed                 ← 工具结果
      message(reasoning) → completed                          ← 再次思考
      message(message) → content(text) deltas → completed     ← 最终回复
      response completed                                      ← 真正结束
    """
    import signal

    message_text = ""
    reasoning_text = ""
    current_msg_type = ""
    buffer = ""
    last_data_time = time.time()

    timed_out = False

    def _timeout_handler(signum, frame):
        nonlocal timed_out
        timed_out = True

    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout)
    except (ValueError, OSError):
        pass

    try:
        while True:
            current_time = time.time()

            if current_time - last_data_time > HEARTBEAT_INTERVAL:
                partial = message_text or reasoning_text
                raise TimeoutError(
                    f"响应已停滞超过 {HEARTBEAT_INTERVAL} 秒，连接可能已断开。\n"
                    f"已收到的内容: {partial[:100] or '(无)'}..."
                )

            if timed_out:
                partial = message_text or reasoning_text
                raise TimeoutError(
                    f"读取响应超时（超过 {timeout} 秒未收到完整响应）。\n"
                    f"可能原因：\n"
                    f"  1. QwenPaw 正在处理复杂请求，需要更长时间\n"
                    f"  2. 网络连接不稳定\n"
                    f"已收到的内容: {partial[:100] or '(无)'}...\n"
                    f"建议：使用 --timeout 参数增加超时时间，例如 --timeout 300"
                )

            try:
                chunk = response.read(1)
            except OSError as e:
                raise ConnectionError(f"读取响应时网络错误: {e}")

            if not chunk:
                break

            last_data_time = current_time
            buffer += chunk.decode("utf-8", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()

                if not line.startswith("data: "):
                    continue

                event_str = line[6:]
                try:
                    event = json.loads(event_str)
                except json.JSONDecodeError:
                    continue

                obj = event.get("object", "")
                status = event.get("status", "")
                evt_type = event.get("type", "")

                if event.get("error"):
                    err_msg = event["error"].get("message", "未知错误")
                    raise ChatError(f"QwenPaw 返回错误: {err_msg}")

                # 跟踪当前 message 类型
                if obj == "message" and status == "in_progress":
                    current_msg_type = evt_type

                # 收集 delta 文本，按 message type 分流
                if obj == "content" and evt_type == "text" and event.get("delta"):
                    text = event.get("text", "")
                    if text:
                        if current_msg_type == "message":
                            message_text += text
                        else:
                            reasoning_text += text
                            if verbose:
                                print(text, end="", file=sys.stderr, flush=True)

                # 只在整个 response 完成时返回
                if obj == "response" and status == "completed":
                    if verbose and reasoning_text:
                        print("", file=sys.stderr)

                    # 优先用流式累积的 message_text
                    if message_text:
                        return message_text

                    # fallback: 从 response output 中提取 type="message" 的文本
                    output_list = event.get("output", []) or []
                    for msg in output_list:
                        if msg.get("type") == "message" and msg.get("role") == "assistant":
                            for content_item in (msg.get("content", []) or []):
                                if content_item.get("type") == "text":
                                    text = content_item.get("text", "")
                                    if text:
                                        return text

                    # 最后 fallback: 返回 reasoning 文本
                    if reasoning_text:
                        return reasoning_text

                    break

    finally:
        try:
            signal.alarm(0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
        except (ValueError, OSError):
            pass
        conn.close()

    # 连接关闭但未收到 response completed，返回已收集的内容
    if message_text:
        return message_text
    if reasoning_text:
        return reasoning_text

    raise ChatError("（未收到智能体的文本回复）")


def chat(message, session_id, port=DEFAULT_PORT, token=None, timeout=DEFAULT_TIMEOUT, verbose=False):
    """
    向 Reactor 智能体发送消息并返回回复文本。

    带重试机制：网络错误会自动重试最多 MAX_RETRIES 次。
    """
    last_exception = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                print(f"第 {attempt} 次尝试（共 {MAX_RETRIES + 1} 次）...", file=sys.stderr)
                time.sleep(RETRY_DELAY)

            conn, response = _send_request(port, session_id, message, token)

            try:
                return _read_sse_stream(response, conn, timeout, verbose=verbose)
            except (TimeoutError, ConnectionError):
                conn.close()
                raise

        except (ConnectionError, socket.timeout, OSError) as e:
            last_exception = e
            if attempt <= MAX_RETRIES:
                print(f"⚠️  第 {attempt} 次尝试失败: {e}", file=sys.stderr)
                if attempt < MAX_RETRIES:
                    print(f"   等待 {RETRY_DELAY} 秒后重试...", file=sys.stderr)
                continue
            # 超出最大重试次数
            raise ChatError(
                f"连接失败（已重试 {MAX_RETRIES} 次）。\n"
                f"请检查：\n"
                f"  1. QwenPaw 是否在 localhost:{port} 上运行\n"
                f"  2. 端口是否正确（默认 7860，可用 ~/.qwenpaw/config.json 查看）\n"
                f"  3. 网络连接是否正常\n"
                f"原始错误: {last_exception}"
            ) from last_exception

        except ChatError:
            # 非网络错误（如 404、401、API 返回错误），不重试
            raise

    # 理论上不会到这里，但防御性编程
    raise ChatError(f"请求失败: {last_exception}")


def main():
    parser = argparse.ArgumentParser(
        description="通过 REST API 与 QwenPaw 中的 Reactor 智能体对话。"
    )
    parser.add_argument("--message", "-m", required=True, help="要发送的消息")
    parser.add_argument("--session-id", "-s", default=None, help="用于对话上下文的会话 ID")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"QwenPaw API 端口（默认: {DEFAULT_PORT}）")
    parser.add_argument("--token", "-t", default=None, help="认证令牌（仅远程访问时需要）")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"响应超时时间（秒），默认 {DEFAULT_TIMEOUT}。复杂任务可设为 300 或更长。"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="输出思考过程到 stderr（调试用）"
    )

    args = parser.parse_args()

    session_id = args.session_id
    if not session_id:
        session_id = prompt_session()

    try:
        reply = chat(
            args.message,
            session_id,
            port=args.port,
            token=args.token,
            timeout=args.timeout,
            verbose=args.verbose,
        )
        print(reply)
    except ChatError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
