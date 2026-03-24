#!/usr/bin/env python3
"""
OpenCode Bridge - 连接 opencode serve 并转发消息
支持流式输出、思考过程、工具调用、飞书转发

用法:
    python opencode_bridge.py -i
    python opencode_bridge.py "解释这个项目"
    python opencode_bridge.py --feishu "分析代码"
    python opencode_bridge.py -m claude-sonnet-4-20250514 -d /path/to/project "你好"
"""

import sys
import io

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    if getattr(sys.stdout, "encoding", "").lower() != "utf-8":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

import json
import subprocess
import time
import signal
import argparse
import os
from dataclasses import dataclass, field
from typing import Optional, Callable, Generator
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests")
    sys.exit(1)


# ============================================================
#  配置加载
# ============================================================

def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
#  日志（带日期文件名）
# ============================================================

class Logger:
    """双日志: 对话日志 + 错误日志，文件名含日期"""

    def __init__(self, log_dir: str = None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        self.chat_file = os.path.join(log_dir, f"chat_{date_str}.log")
        self.error_file = os.path.join(log_dir, f"error_{date_str}.log")
        self._chat_fh = open(self.chat_file, "a", encoding="utf-8")
        self._error_fh = open(self.error_file, "a", encoding="utf-8")
        self._log("chat", f"\n--- 新会话 {datetime.now()} ---")

    def _log(self, log_type: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        if log_type == "chat":
            self._chat_fh.write(line)
            self._chat_fh.flush()
        else:
            self._error_fh.write(line)
            self._error_fh.flush()

    def chat(self, role: str, content: str):
        self._log("chat", f"[{role}] {content}")

    def thinking(self, content: str):
        self._log("chat", f"[thinking] {content}")

    def tool(self, name: str, status: str, output: str = ""):
        self._log("chat", f"[tool:{status}] {name} {output}")

    def error(self, msg: str, exc: Exception = None):
        detail = f"{msg}"
        if exc:
            detail += f" | {type(exc).__name__}: {exc}"
        self._log("error", detail)
        self._log("chat", f"[error] {detail}")

    def info(self, msg: str):
        self._log("chat", f"[info] {msg}")

    def close(self):
        self._log("chat", f"=== 会话结束 {datetime.now()} ===")
        self._chat_fh.close()
        self._error_fh.close()

    def get_paths(self) -> tuple:
        return self.chat_file, self.error_file


# ============================================================
#  配置 & 常量
# ============================================================

_cfg = load_config()
OPENCODE_PATH = _cfg["opencode"]["path"]


# ============================================================
#  配置类
# ============================================================

@dataclass
class BridgeConfig:
    host: str = "127.0.0.1"
    port: int = 0
    model: Optional[str] = None
    directory: Optional[str] = None
    timeout: int = 30


# ============================================================
#  事件数据类
# ============================================================

@dataclass
class ReasoningEvent:
    text: str = ""
    delta: str = ""
    finished: bool = False

@dataclass
class TextEvent:
    text: str = ""
    delta: str = ""
    finished: bool = False

@dataclass
class ToolEvent:
    tool: str = ""
    status: str = ""
    title: Optional[str] = None
    input: dict = field(default_factory=dict)
    output: Optional[str] = None
    error: Optional[str] = None

@dataclass
class StepEvent:
    type: str = ""
    cost: float = 0.0
    tokens: dict = field(default_factory=dict)

@dataclass
class ParsedEvent:
    type: str
    reasoning: Optional[ReasoningEvent] = None
    text: Optional[TextEvent] = None
    tool: Optional[ToolEvent] = None
    step: Optional[StepEvent] = None
    raw: dict = field(default_factory=dict)


# ============================================================
#  核心桥接类
# ============================================================

class OpenCodeBridge:
    """opencode serve 的 Python 桥接"""

    def __init__(self, config: Optional[BridgeConfig] = None):
        self.config = config or BridgeConfig()
        self.base_url: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = None
        self._running = False
        self._chat_sessions: dict = {}

    # ---------- 服务器管理 ----------

    def start_server(self) -> str:
        if self.process and self.process.poll() is None:
            return self.base_url

        import tempfile
        import re

        self._log_file = tempfile.NamedTemporaryFile(
            mode="w+", suffix=".log", delete=False, prefix="opencode_"
        )

        cmd = [
            OPENCODE_PATH, "serve",
            "--hostname", self.config.host,
            "--port", str(self.config.port),
            "--print-logs",
        ]

        print(f"\033[90m启动服务器...\033[0m")

        self.process = subprocess.Popen(
            cmd,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
        )

        port = None
        start = time.time()
        while time.time() - start < self.config.timeout:
            try:
                with open(self._log_file.name, "r") as f:
                    content = f.read()
                match = re.search(r'listening\s+on\s+\S+:(\d+)', content)
                if match:
                    port = int(match.group(1))
                    break
            except Exception:
                pass
            if self.process.poll() is not None:
                raise RuntimeError("opencode 启动失败")
            time.sleep(0.3)

        if not port:
            raise RuntimeError(f"无法在 {self.config.timeout}s 内检测到端口")

        self.base_url = f"http://{self.config.host}:{port}"
        self._running = True
        print(f"\033[92m✓ 服务器已启动: {self.base_url}\033[0m")
        return self.base_url

    def stop_server(self):
        self._running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def __enter__(self):
        self.start_server()
        return self

    def __exit__(self, *args):
        self.stop_server()

    # ---------- API 调用 ----------

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise RuntimeError("服务器未启动，先调用 start_server()")
        return f"{self.base_url}{path}"

    def create_session(self, title: Optional[str] = None) -> dict:
        body = {}
        if title:
            body["title"] = title
        params = {}
        if self.config.directory:
            params["directory"] = self.config.directory
        r = requests.post(self._url("/session"), json=body or None, params=params)
        r.raise_for_status()
        session = r.json()
        self.session_id = session["id"]
        return session

    def list_sessions(self) -> list:
        r = requests.get(self._url("/session"))
        r.raise_for_status()
        return r.json()

    def get_or_create_session(self, chat_id: str) -> str:
        """为每个飞书对话维护独立会话"""
        if chat_id not in self._chat_sessions:
            session = self.create_session(title=f"飞书:{chat_id[:12]}")
            self._chat_sessions[chat_id] = session["id"]
        self.session_id = self._chat_sessions[chat_id]
        return self.session_id

    # ---------- SSE 流式解析 ----------

    def _iter_sse(self, resp) -> Generator[dict, None, None]:
        buffer = ""
        for chunk in resp.iter_content(chunk_size=1024, decode_unicode=False):
            if not self._running:
                break
            try:
                text = chunk.decode("utf-8", errors="replace")
            except Exception:
                continue
            buffer += text
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    raw = line[6:].strip()
                    try:
                        data = json.loads(raw)
                        etype = data.get("type", "unknown")
                        yield {"type": etype, "data": data}
                    except json.JSONDecodeError:
                        pass

    # ---------- 发送消息(真流式) ----------

    def send(
        self,
        message: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        agent: Optional[str] = None,
        system: Optional[str] = None,
    ) -> Generator[ParsedEvent, None, None]:
        import threading
        import queue as q

        sid = session_id or self.session_id
        if not sid:
            raise RuntimeError("没有会话，先调用 create_session()")

        event_queue: q.Queue = q.Queue()

        def listen():
            try:
                r = requests.get(
                    self._url("/event"),
                    stream=True,
                    headers={"Accept": "text/event-stream"},
                    timeout=180,
                )
                r.raise_for_status()
                for ev in self._iter_sse(r):
                    event_queue.put(ev)
                    if ev.get("type") == "session.idle":
                        event_queue.put({"type": "__done__"})
                        return
            except Exception as e:
                event_queue.put({"type": "__error__", "data": str(e)})
                event_queue.put({"type": "__done__"})

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.1)

        body: dict = {"parts": [{"type": "text", "text": message}]}

        if model:
            if "/" in model:
                pid, mid = model.split("/", 1)
                body["model"] = {"providerID": pid, "modelID": mid}
        if agent:
            body["agent"] = agent
        if system:
            body["system"] = system

        params = {}
        if self.config.directory:
            params["directory"] = self.config.directory

        print(f"\033[90m发送消息...\033[0m", flush=True)
        r = requests.post(
            self._url(f"/session/{sid}/prompt_async"),
            json=body,
            params=params,
            timeout=30,
        )
        r.raise_for_status()

        reasoning_buf = ""
        text_buf = ""
        part_types = {}

        while True:
            try:
                ev = event_queue.get(timeout=30)
            except q.Empty:
                break

            etype = ev.get("type")
            props = ev.get("data", {}).get("properties", ev.get("data", {}))

            if etype == "__done__":
                break
            if etype == "__error__":
                yield ParsedEvent(type="error", raw={"error": str(props)})
                break
            if etype in ("server.connected", "server.heartbeat"):
                continue

            ev_sid = props.get("sessionID", "")
            if ev_sid and ev_sid != sid:
                continue

            if etype == "message.part.updated":
                part = props.get("part", {})
                ptype = part.get("type")
                pid = part.get("id", "")
                if ptype in ("reasoning", "text") and pid:
                    part_types[pid] = ptype

                if ptype == "tool":
                    st = part.get("state", {})
                    yield ParsedEvent(
                        type="tool",
                        tool=ToolEvent(
                            tool=part.get("tool", ""),
                            status=st.get("status", ""),
                            title=st.get("title"),
                            input=st.get("input", {}),
                            output=st.get("output"),
                            error=st.get("error"),
                        ),
                    )
                elif ptype in ("step-start", "step-finish"):
                    yield ParsedEvent(
                        type="step",
                        step=StepEvent(
                            type=ptype,
                            cost=part.get("cost", 0),
                            tokens=part.get("tokens", {}),
                        ),
                    )

            elif etype == "message.part.delta":
                delta = props.get("delta", "")
                part_id = props.get("partID", "")
                ptype = part_types.get(part_id, "text")

                if ptype == "reasoning":
                    reasoning_buf += delta
                    yield ParsedEvent(
                        type="reasoning",
                        reasoning=ReasoningEvent(text=reasoning_buf, delta=delta),
                    )
                else:
                    text_buf += delta
                    yield ParsedEvent(
                        type="text",
                        text=TextEvent(text=text_buf, delta=delta),
                    )

            elif etype == "session.idle":
                break

        if text_buf:
            yield ParsedEvent(
                type="text",
                text=TextEvent(text=text_buf, delta="", finished=True),
            )
        if reasoning_buf:
            yield ParsedEvent(
                type="reasoning",
                reasoning=ReasoningEvent(text=reasoning_buf, delta="", finished=True),
            )

    # ---------- 高级 chat 方法 ----------

    def chat(
        self,
        message: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        on_reasoning: Optional[Callable[[str], None]] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool: Optional[Callable[[ToolEvent], None]] = None,
        on_step: Optional[Callable[[StepEvent], None]] = None,
    ) -> dict:
        full_text = ""
        full_reasoning = ""
        tools = []
        total_cost = 0.0

        for event in self.send(message, session_id=session_id, model=model):
            if event.reasoning and event.reasoning.delta and on_reasoning:
                on_reasoning(event.reasoning.delta)
            if event.reasoning and event.reasoning.finished:
                full_reasoning = event.reasoning.text

            if event.text and event.text.delta and on_text:
                on_text(event.text.delta)
            if event.text and event.text.finished:
                full_text = event.text.text

            if event.tool:
                tools.append(event.tool)
                if on_tool:
                    on_tool(event.tool)

            if event.step:
                if event.step.type == "step-finish":
                    total_cost += event.step.cost
                if on_step:
                    on_step(event.step)

        return {
            "text": full_text,
            "reasoning": full_reasoning,
            "tools": tools,
            "cost": total_cost,
        }


import re as _re

# ============================================================
#  Markdown 样式优化（参考 openclaw-lark markdown-style.ts）
# ============================================================

def _optimize_markdown(text: str, card_version: int = 2) -> str:
    """参考 openclaw-lark optimizeMarkdownStyle"""
    try:
        code_blocks = []
        def _save_code(m):
            code_blocks.append(m.group(0))
            return f"___CB_{len(code_blocks)-1}___"
        r = _re.sub(r"```[\s\S]*?```", _save_code, text)

        if card_version >= 2:
            if _re.search(r"^#{1,3} ", text, _re.MULTILINE):
                r = _re.sub(r"^#{2,6} (.+)$", r"##### \1", r, flags=_re.MULTILINE)
                r = _re.sub(r"^# (.+)$", r"#### \1", r, flags=_re.MULTILINE)
            r = _re.sub(r"^(#{4,5} .+)\n{1,2}(#{4,5} )", r"\1\n<br>\n\2", r, flags=_re.MULTILINE)
            for i, block in enumerate(code_blocks):
                r = r.replace(f"___CB_{i}___", f"\n<br>\n{block}\n<br>\n")
        else:
            for i, block in enumerate(code_blocks):
                r = r.replace(f"___CB_{i}___", block)

        r = _re.sub(r"\n{3,}", "\n\n", r)
        return r
    except Exception:
        return text



# ============================================================
#  飞书流式格式化器
#  参考 openclaw-lark: streaming-card-controller + builder
# ============================================================

class FeishuStreamingFormatter:
    """
    完全参考 openclaw-lark StreamingCardController:
    1. createCardEntity → sendCardByCardId → 同一张卡片
    2. streamCardContent → 流式更新（打字机效果）
    3. onIdle → setCardStreamingMode(false) → updateCardKitCard（完整卡片，同一消息更新）
    不删除、不发 post、不"已编辑"
    """

    def __init__(self, bridge, chat_id: str, feishu_api=None):
        self._api = feishu_api
        self.bridge = bridge
        self.chat_id = chat_id
        self._card_id = None
        self._card_msg_id = None
        self._seq = 0
        self._text = ""
        self._token = None
        self._token_time = 0.0
        self._last_flush = 0.0
        self._start_time = time.time()
        self._THROTTLE = 0.08  # 80ms（低于 API 延迟所以实际由 API 决定）
        self._sess = requests.Session()  # HTTP 连接复用

    def _get_token(self):
        if self._token and (time.time() - self._token_time) < 1800:
            return self._token
        self._token = self._api.get_token()
        self._token_time = time.time()
        return self._token

    # ---- 创建卡片（参考 openclaw ensureCardCreated） ----

    def _create(self):
        if not self._api:
            return
        try:
            token = self._get_token()
            card = {
                "schema": "2.0",
                "config": {
                    "wide_screen_mode": True,
                    "update_multi": True,
                    "streaming_mode": True,
                    "locales": ["zh_cn", "en_us"],
                    "summary": {"content": "Thinking...", "i18n_content": {"zh_cn": "思考中...", "en_us": "Thinking..."}},
                },
                "header": {
                    "title": {"tag": "plain_text", "content": "OpenCode", "i18n_content": {"zh_cn": "OpenCode", "en_us": "OpenCode"}},
                    "template": "blue"
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": "", "text_align": "left", "text_size": "normal_v2", "element_id": "stream"},
                        {"tag": "markdown", "content": " ", "icon": {"tag": "custom_icon", "img_key": "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg", "size": "16px 16px"}, "element_id": "loading_icon"},
                    ]
                }
            }
            r = self._sess.post(
                "https://open.feishu.cn/open-apis/cardkit/v1/cards",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"type": "card_json", "data": json.dumps(card, ensure_ascii=False)},
                timeout=5,
            ).json()
            self._card_id = r.get("data", {}).get("card_id")
            self._seq = 1

            resp = self._sess.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "receive_id": self.chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps({"type": "card", "data": {"card_id": self._card_id}}, ensure_ascii=False),
                },
                timeout=5,
            ).json()
            self._card_msg_id = resp.get("data", {}).get("message_id")
            self._last_flush = time.time()
            self._start_time = time.time()
        except Exception as e:
            print(f"\033[91m[Streaming] 创建失败: {e}\033[0m")

    # ---- 流式更新（参考 openclaw streamCardContent） ----

    def _stream(self):
        if not self._card_id:
            return
        try:
            token = self._get_token()
            self._seq += 1
            self._sess.put(
                f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{self._card_id}/elements/stream/content",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"content": self._text or "⏳", "sequence": self._seq},
                timeout=5,
            )
        except Exception:
            pass

    # ---- 完成（参考 openclaw onIdle） ----

    def _finish(self):
        if not self._card_id:
            return
        try:
            token = self._get_token()
            elapsed_ms = int((time.time() - self._start_time) * 1000)
            elapsed_s = elapsed_ms / 1000
            elapsed_str = f"{elapsed_s:.1f}s" if elapsed_s < 60 else f"{int(elapsed_s//60)}m {int(elapsed_s%60)}s"

            # 1. 最终流式更新
            self._stream()

            # 2. 关闭 streaming_mode（参考 setCardStreamingMode）
            self._seq += 1
            self._sess.put(
                f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{self._card_id}/settings",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"settings": json.dumps({"streaming_mode": False}), "sequence": self._seq},
                timeout=5,
            )

            # 3. 更新为完整卡片（参考 updateCardKitCard + buildCompleteCard）
            summary_text = self._text.replace("*", "").replace("`", "").replace("#", "").strip()[:120]

            complete_card = {
                "schema": "2.0",
                "config": {
                    "wide_screen_mode": True,
                    "update_multi": True,
                    "locales": ["zh_cn", "en_us"],
                    "summary": {"content": summary_text} if summary_text else None,
                },
                "header": {
                    "title": {"tag": "plain_text", "content": "OpenCode"},
                    "template": "green"
                },
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": self._text,
                            "text_align": "left",
                            "text_size": "normal_v2",
                        },
                        {
                            "tag": "markdown",
                            "content": f"已完成 · 耗时 {elapsed_str}",
                            "i18n_content": {
                                "zh_cn": f"已完成 · 耗时 {elapsed_str}",
                                "en_us": f"Completed · Elapsed {elapsed_str}"
                            },
                            "text_size": "notation",
                        },
                    ]
                }
            }
            if not summary_text:
                del complete_card["config"]["summary"]

            self._seq += 1
            self._sess.put(
                f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{self._card_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "card": {"type": "card_json", "data": json.dumps(complete_card, ensure_ascii=False)},
                    "sequence": self._seq,
                },
                timeout=5,
            )
        except Exception as e:
            print(f"\033[93m[Streaming] 收尾失败: {e}\033[0m")

    # ---- 入口 ----

    def on_delta(self, delta: str):
        self._text += delta
        if not self._card_id:
            self._create()
        now = time.time()
        if now - self._last_flush >= self._THROTTLE:
            self._stream()
            self._last_flush = now

    def on_finished(self):
        self._finish()


# ============================================================
#  终端渲染
# ============================================================

C = {
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
    "italic":  "\033[3m",
    "red":     "\033[91m",
    "green":   "\033[92m",
    "yellow":  "\033[93m",
    "blue":    "\033[94m",
    "magenta": "\033[95m",
    "cyan":    "\033[96m",
    "gray":    "\033[90m",
}


def render_thinking(delta: str):
    sys.stdout.write(f"{C['gray']}{C['italic']}{delta}{C['reset']}")
    sys.stdout.flush()


def render_text(delta: str):
    sys.stdout.write(delta)
    sys.stdout.flush()


def render_tool(tool: ToolEvent):
    colors = {"pending": "yellow", "running": "cyan", "completed": "green", "error": "red"}
    icons = {"pending": "⏳", "running": "🔄", "completed": "✅", "error": "❌"}
    color = colors.get(tool.status, "gray")
    icon = icons.get(tool.status, "❓")
    title = tool.title or tool.tool
    print(f"\n{C[color]}{icon} {C['bold']}{title}{C['reset']}")
    if tool.status == "completed" and tool.output:
        preview = tool.output[:300] + ("..." if len(tool.output) > 300 else "")
        print(f"{C['gray']}   {preview}{C['reset']}")
    elif tool.status == "error" and tool.error:
        print(f"{C['red']}   Error: {tool.error}{C['reset']}")


def render_step(step: StepEvent):
    if step.type == "step-finish" and step.tokens:
        t = step.tokens
        total = t.get("input", 0) + t.get("output", 0) + t.get("reasoning", 0)
        print(f"\n{C['gray']}📊 Tokens: {total:,} | Cost: ${step.cost:.4f}{C['reset']}")


# ============================================================
#  CLI 入口
# ============================================================

def cli_single(bridge: OpenCodeBridge, message: str, feishu_forwarder=None):
    session = bridge.create_session()
    print(f"{C['green']}✓ Session: {session['id'][:8]}...{C['reset']}")
    print(f"{C['blue']}{'─' * 50}{C['reset']}\n")

    result = bridge.chat(
        message,
        on_reasoning=render_thinking,
        on_text=render_text,
        on_tool=render_tool,
        on_step=render_step,
    )

    print(f"\n{C['blue']}{'─' * 50}{C['reset']}")
    if feishu_forwarder:
        feishu_forwarder.send_result(result, question=message)
        print(f"{C['green']}✓ 已转发到飞书{C['reset']}")

    return result


def cli_interactive(bridge: OpenCodeBridge, feishu_forwarder=None, logger=None, feishu_api=None):
    session = bridge.create_session()
    print(f"{C['green']}✓ Session: {session['id'][:8]}...{C['reset']}")
    print(f"{C['gray']}输入消息开始对话{C['reset']}")
    print(f"{C['gray']}命令: /new 新会话 | /sessions 列出会话 | /quit 退出{C['reset']}")
    if logger:
        chat_path, err_path = logger.get_paths()
        print(f"{C['gray']}对话日志: {chat_path}{C['reset']}")
        print(f"{C['gray']}错误日志: {err_path}{C['reset']}")
    print(f"{C['blue']}{'─' * 50}{C['reset']}\n")

    while True:
        try:
            user_input = input(f"{C['green']}> {C['reset']}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C['gray']}再见!{C['reset']}")
            break

        if not user_input:
            continue
        if user_input.lower() in ("/quit", "/exit", "/q"):
            print(f"{C['gray']}再见!{C['reset']}")
            break
        if user_input.lower() == "/new":
            session = bridge.create_session()
            print(f"{C['green']}✓ 新会话: {session['id'][:8]}...{C['reset']}")
            if logger:
                logger.info(f"新会话: {session['id']}")
            continue
        if user_input.lower() == "/sessions":
            for s in bridge.list_sessions():
                title = s.get("title", "无标题")
                print(f"  {C['cyan']}{s['id'][:8]}...{C['reset']} {title}")
            continue

        if logger:
            logger.chat("user", user_input)

        def on_thinking(d):
            render_thinking(d)
            if logger:
                logger.thinking(d)

        def on_text(d):
            render_text(d)
            if logger and d:
                logger.chat("assistant", d)

        def on_tool(t):
            render_tool(t)
            if logger:
                logger.tool(t.tool, t.status, t.output or t.error or "")

        def on_step(s):
            render_step(s)

        formatter = None
        if feishu_forwarder and feishu_api:
            formatter = FeishuStreamingFormatter(bridge, user_input, feishu_api=feishu_api)

        if formatter:
            def combined_on_text(d):
                on_text(d)
                formatter.on_delta(d)
            text_cb = combined_on_text
        else:
            text_cb = on_text

        try:
            result = bridge.chat(
                user_input,
                on_reasoning=on_thinking,
                on_text=text_cb,
                on_tool=on_tool,
                on_step=on_step,
            )
            if formatter:
                formatter.on_finished()
        except Exception as e:
            if logger:
                logger.error("发送消息失败", e)
            print(f"{C['red']}错误: {e}{C['reset']}")
            continue

        if logger:
            logger.chat("result", f"text={len(result.get('text',''))} chars, cost=${result.get('cost',0):.4f}")

        print(f"\n{C['blue']}{'─' * 50}{C['reset']}\n")


# ============================================================
#  主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="OpenCode Bridge - opencode 的 Python 桥接工具",
    )
    parser.add_argument("message", nargs="?", help="要发送的消息")
    parser.add_argument("-i", "--interactive", action="store_true", help="交互模式")
    parser.add_argument("-m", "--model", help="模型 (provider/model)")
    parser.add_argument("-d", "--directory", help="工作目录")
    parser.add_argument("--feishu", action="store_true", help="转发到飞书")
    parser.add_argument("--feishu-url", help="飞书 Webhook URL")
    parser.add_argument("--feishu-app-id", help="飞书 App ID")
    parser.add_argument("--feishu-app-secret", help="飞书 App Secret")
    parser.add_argument("--feishu-chat-id", help="飞书群 ID (App 模式)")
    parser.add_argument("--no-thinking", action="store_true", help="隐藏思考过程")
    parser.add_argument("--no-tools", action="store_true", help="隐藏工具调用")

    args = parser.parse_args()

    config = load_config()

    feishu_forwarder = None
    if args.feishu or args.feishu_url or args.feishu_app_id:
        try:
            from feishu_forwarder import FeishuForwarder
            feishu_forwarder = FeishuForwarder(
                webhook_url=args.feishu_url or "",
                app_id=args.feishu_app_id or "",
                app_secret=args.feishu_app_secret or "",
            )
            if args.feishu_chat_id:
                feishu_forwarder.default_chat_id = args.feishu_chat_id
        except ImportError:
            print(f"\033[93m⚠ feishu_forwarder.py 未找到，跳过飞书转发\033[0m")

    bridge_config = BridgeConfig(
        host=config["opencode"]["host"],
        port=config["opencode"]["port"],
        model=args.model or config["opencode"].get("model"),
        directory=args.directory or config["opencode"].get("directory"),
        timeout=config["opencode"]["timeout"],
    )

    bridge = OpenCodeBridge(bridge_config)

    def signal_handler(sig, frame):
        print(f"\n\033[90m正在停止...\033[0m")
        bridge.stop_server()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        bridge.start_server()
        print(f"\033[94m🚀 opencode 已启动: {bridge.base_url}\033[0m")

        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), config["logs"]["dir"])
        logger = Logger(log_dir=log_dir)

        feishu_api = None
        try:
            from feishu_bridge import FeishuAPI
            feishu_api = FeishuAPI(
                config["feishu"]["app_id"],
                config["feishu"]["app_secret"],
            )
        except Exception:
            pass

        if args.interactive:
            cli_interactive(bridge, feishu_forwarder, logger, feishu_api)
        elif args.message:
            cli_single(bridge, args.message, feishu_forwarder)
        else:
            parser.print_help()

        logger.close()
    finally:
        bridge.stop_server()


if __name__ == "__main__":
    main()
