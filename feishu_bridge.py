#!/usr/bin/env python3
"""
飞书桥接器 - 飞书客户端 <-> OpenCode 双向通信

启动流程:
    1. 读取 config.json 配置
    2. 启动 OpenCode serve 服务器
    3. 连接飞书 WebSocket 监听消息
    4. 收到飞书消息 -> 转发给 OpenCode -> 流式回复到飞书卡片

日志:
    logs/chat_YYYYMMDD.log  - 会话日志
    logs/error_YYYYMMDD.log - 错误日志
"""

import sys, io

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

import json, os, time, datetime, threading, queue
import urllib.request, urllib.error

from opencode_bridge import OpenCodeBridge, BridgeConfig, Logger, load_config, FeishuStreamingFormatter

# ============================================================
#  配置 & 日志
# ============================================================

cfg = load_config()
APP_ID = cfg["feishu"]["app_id"]
APP_SECRET = cfg["feishu"]["app_secret"]
DEFAULT_CHAT_ID = cfg["feishu"]["default_chat_id"]

log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["logs"]["dir"])
logger = Logger(log_dir=log_dir)


# ============================================================
#  飞书 API 封装
# ============================================================

class FeishuAPI:
    """飞书 API 操作封装"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

    def get_token(self) -> str:
        data = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        return json.loads(urllib.request.urlopen(req, timeout=5).read())["tenant_access_token"]

    def api_send(self, receive_id, msg_type, content):
        token = self.get_token()
        if isinstance(content, dict):
            body_content = json.dumps(content, ensure_ascii=False)
        else:
            body_content = content
        body = json.dumps({
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": body_content,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8"
            },
            method="POST"
        )
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            logger.error(f"[SEND] {msg_type} HTTP {e.code}: {error_body[:300]}")
            return {"code": e.code, "msg": error_body}
        logger.info(f"[SEND] {msg_type} code={resp.get('code')}")
        return resp

    def send_text(self, chat_id, text):
        return self.api_send(chat_id, "text", {"text": text})

    def send_post(self, chat_id, title, content):
        return self.api_send(chat_id, "post", {"zh_cn": {"title": title, "content": content}})

    def send_card(self, chat_id, card):
        return self.api_send(chat_id, "interactive", card)


# ============================================================
#  消息队列 & 桥接处理
# ============================================================

msg_queue = queue.Queue()
feishu_api = FeishuAPI(APP_ID, APP_SECRET)


def message_worker(bridge: OpenCodeBridge):
    """工作线程: 从队列取飞书消息，转发给 OpenCode，流式回复到飞书卡片"""
    while True:
        try:
            item = msg_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        chat_id = item["chat_id"]
        text = item["text"]
        logger.chat("user", f"[飞书:{chat_id}] {text}")

        # 获取或创建该对话的独立会话
        sid = bridge.get_or_create_session(chat_id)

        # 流式格式化器 - 直接转发原始文本到飞书卡片
        formatter = FeishuStreamingFormatter(bridge, chat_id, feishu_api=feishu_api)

        def on_text(delta):
            formatter.on_delta(delta)
            if delta:
                logger.chat("assistant", delta)

        def on_tool(t):
            logger.tool(t.tool, t.status, t.output or t.error or "")

        try:
            result = bridge.chat(
                text,
                session_id=sid,
                on_text=on_text,
                on_tool=on_tool,
            )
            logger.chat("result", f"text={len(result.get('text', ''))} chars, cost=${result.get('cost', 0):.4f}")
        except Exception as e:
            logger.error("OpenCode 处理失败", e)
            try:
                feishu_api.send_text(chat_id, f"处理出错: {e}")
            except Exception:
                pass
        finally:
            try:
                formatter.on_finished()
            except Exception:
                pass


def on_msg(data):
    """飞书 WebSocket 消息回调"""
    try:
        event = data.event
        msg = event.message
        chat_id = msg.chat_id
        text = json.loads(msg.content or "{}").get("text", "")
        if not text.strip():
            return
        logger.info(f"[RECV] chat={chat_id} text={text[:100]}")
        msg_queue.put({"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("消息解析错误", e)


# ============================================================
#  主函数
# ============================================================

def main():
    print(f"\033[94m{'═' * 50}\033[0m")
    print(f"\033[94m   飞书-OpenCode 桥接器\033[0m")
    print(f"\033[94m{'═' * 50}\033[0m")
    print(f"\033[90m飞书 App ID: {APP_ID}\033[0m")
    print(f"\033[90m默认群 ID:   {DEFAULT_CHAT_ID}\033[0m")
    print(f"\033[90mOpenCode:    {cfg['opencode']['path']}\033[0m")
    chat_path, err_path = logger.get_paths()
    print(f"\033[90m对话日志:    {chat_path}\033[0m")
    print(f"\033[90m错误日志:    {err_path}\033[0m")

    # ---- 启动 OpenCode 服务器 ----
    bridge_config = BridgeConfig(
        host=cfg["opencode"]["host"],
        port=cfg["opencode"]["port"],
        timeout=cfg["opencode"]["timeout"],
    )
    bridge = OpenCodeBridge(bridge_config)
    try:
        bridge.start_server()
    except Exception as e:
        logger.error("OpenCode 启动失败", e)
        print(f"\033[91m❌ OpenCode 启动失败: {e}\033[0m")
        input("按回车退出...")
        return

    # ---- 启动飞书 WebSocket 监听 ----
    ws_connected = threading.Event()

    def on_msg_wrapper(data):
        ws_connected.set()
        on_msg(data)

    print(f"\033[90m连接飞书 WebSocket...\033[0m")
    import lark_oapi as lark
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_msg_wrapper).build()
    client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.WARNING)
    ws_thread = threading.Thread(target=client.start, daemon=True)
    ws_thread.start()
    time.sleep(3)
    print(f"\033[92m✓ 飞书 WebSocket 已连接\033[0m")
    print(f"\033[90m  请在飞书发一条消息测试连接...\033[0m")

    # 等待首条消息确认事件订阅正常（最多10秒）
    if ws_connected.wait(timeout=10):
        print(f"\033[92m✓ 事件接收正常\033[0m")
    else:
        print(f"\033[93m⚠ 10秒内未收到消息，请确认飞书后台已订阅 im.message.receive_v1 事件\033[0m")

    # ---- 发送就绪通知 ----
    try:
        feishu_api.send_text(DEFAULT_CHAT_ID, "✅ 飞书-OpenCode 桥接器已启动，可以开始对话了！")
    except Exception:
        pass

    # ---- 启动消息处理线程 ----
    threading.Thread(target=message_worker, args=(bridge,), daemon=True).start()
    logger.info("桥接器启动完成")
    print(f"\n\033[92m🚀 桥接器就绪！飞书消息将自动转发到 OpenCode\033[0m")
    print(f"\033[90m命令: status=状态 | test=测试 | quit=退出\033[0m")
    print(f"\033[94m{'─' * 50}\033[0m\n")

    # ---- 主循环 ----
    try:
        while True:
            try:
                cmd = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not cmd:
                continue

            if cmd == "quit":
                break
            elif cmd == "status":
                print(f"  飞书: 已连接")
                print(f"  OpenCode: {bridge.base_url}")
                print(f"  队列: {msg_queue.qsize()} 条待处理")
                print(f"  会话数: {len(bridge._chat_sessions)}")
            elif cmd == "test":
                try:
                    feishu_api.send_text(DEFAULT_CHAT_ID, "🔔 桥接器测试消息")
                    print("  已发送测试消息")
                except Exception as e:
                    print(f"  发送失败: {e}")
            elif cmd == "text":
                try:
                    feishu_api.send_text(DEFAULT_CHAT_ID, "📝 文本测试")
                    print("  已发送")
                except Exception as e:
                    print(f"  发送失败: {e}")
            elif cmd == "card":
                try:
                    feishu_api.send_card(DEFAULT_CHAT_ID, {
                        "schema": "2.0",
                        "header": {"title": {"tag": "plain_text", "content": "桥接器测试"}, "template": "blue"},
                        "body": {"elements": [{"tag": "markdown", "content": "**✅ 卡片测试**"}]},
                    })
                    print("  已发送")
                except Exception as e:
                    print(f"  发送失败: {e}")
            else:
                print(f"  未知命令: {cmd}  (status/test/text/card/quit)")

    except KeyboardInterrupt:
        pass

    # ---- 关闭 ----
    print(f"\n\033[90m正在关闭...\033[0m")
    try:
        feishu_api.send_text(DEFAULT_CHAT_ID, "⏸ 飞书-OpenCode 桥接器已关闭")
    except Exception:
        pass
    bridge.stop_server()
    logger.close()
    print(f"\033[92m✓ 已关闭\033[0m")


if __name__ == "__main__":
    main()
