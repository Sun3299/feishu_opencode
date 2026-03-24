# OpenCode ↔ 飞书桥接器

> 🚀 让 OpenCode 的强大 AI 能力，通过飞书即时触达你的团队

[English](./README.en.md) | 中文

<p align="center">
  <img src="./images/running_example.jpg" alt="运行效果" width="800" />
</p>

---

## 💡 为什么需要这个桥接器？

你是否遇到过这些痛点：

- **频繁切换窗口** — 在终端和飞书之间来回复制粘贴 AI 回复
- **丢失上下文** — 每次对话都要重新描述背景，AI 无法记住上次聊了什么
- **团队协作低效** — 有了好的 AI 回复，还要手动转发给同事
- **批处理等待** — 看着终端转圈，不知道 AI 在思考还是卡住了

**OpenCode 飞书桥接器** 直接解决这些问题 — 飞书消息即问，AI 即答，团队共享。

---

## ✨ 核心亮点

### 🌊 真流式输出，打字机效果

不是简单的"请求-等待-返回"，而是 **实时流式传输** — AI 边想边说，你在飞书卡片上看到文字一个个蹦出来，就像真人打字一样。

```
传统方式：  [发送] → 等待... → 等待... → 一次性返回完整回复
桥接器：    [发送] → 思考中... → 开始回答 → 文字逐字流式显示 → 完成
```

### 📊 飞书卡片流式更新

参考飞书官方 OpenClaw 插件的实现，采用 **CardKit 流式更新 API**：

- 同一张卡片实时更新，不会产生多条消息
- 打字机效果 + 加载动画，体验流畅
- 完成后自动切换为完成状态，显示耗时

### 🔒 独立会话隔离

每个飞书群/私聊 **独立维护会话上下文**：

- 群 A 问项目 A，群 B 问项目 B，互不干扰
- AI 能记住同一个对话的上下文，连续追问更自然
- 支持同时处理多个会话，消息队列自动排队

### 📝 Markdown 渲染优化

针对飞书卡片的 Markdown 渲染特性做了深度优化：

- 代码块自动添加间距，避免粘连
- 标题层级自动适配飞书卡片最大支持层级
- 连续空行自动合并，保持排版整洁

### 📋 完整日志追踪

双日志系统，按日期自动分割：

- `logs/chat_YYYYMMDD.log` — 完整对话记录（用户提问 + AI 回复 + 工具调用）
- `logs/error_YYYYMMDD.log` — 错误日志，方便排查问题

---

## 🏗️ 架构设计

```
┌─────────────┐     WebSocket      ┌──────────────────┐
│   飞书客户端   │ ◄──────────────► │   feishu_bridge   │
│  (手机/桌面)  │                   │   (消息监听+分发)  │
└─────────────┘                    └────────┬─────────┘
                                            │
                                     消息队列 │
                                            │
                                            ▼
                                   ┌──────────────────┐
                                   │  opencode_bridge  │
                                   │  (核心桥接逻辑)   │
                                   └────────┬─────────┘
                                            │
                                   SSE 流式 │
                                            │
                                            ▼
                                   ┌──────────────────┐
                                   │  opencode serve   │
                                   │   (AI 引擎)       │
                                   └──────────────────┘
```

**数据流：**
1. 用户在飞书发消息 → 飞书 WebSocket 推送到桥接器
2. 桥接器将消息放入队列，工作线程取出并转发给 OpenCode
3. OpenCode 通过 SSE 流式返回 AI 思考过程和回复
4. 桥接器实时解析流，通过 CardKit API 流式更新飞书卡片

---

## 🚀 快速开始

### 环境要求

- **Python**: 3.8+
- **OpenCode**: 已安装并可正常运行（运行 `opencode --version` 确认）
- **飞书应用**: 已创建飞书自建应用，获取 App ID 和 App Secret

### 安装

```bash
# 克隆仓库
git clone https://github.com/Sun3299/feishu_opencode.git
cd feishu_opencode
```

### 配置

复制配置模板并编辑：

```bash
cp config.json.example config.json
```

编辑 `config.json`：

```json
{
    "feishu": {
        "app_id": "cli_xxxxxxxxxxxx",
        "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxx",
        "default_chat_id": "oc_xxxxxxxxxxxxxxxxxxxxxxxx"
    },
    "opencode": {
        "path": "opencode",
        "host": "127.0.0.1",
        "port": 0,
        "timeout": 30
    },
    "logs": {
        "dir": "logs"
    }
}
```

**配置说明：**

| 字段 | 说明 |
|------|------|
| `feishu.app_id` | 飞书应用 App ID |
| `feishu.app_secret` | 飞书应用 App Secret |
| `feishu.default_chat_id` | 默认消息接收群 ID（用于启动通知） |
| `opencode.path` | OpenCode 可执行文件路径 |
| `opencode.port` | 服务端口（0 = 自动分配） |

### 飞书应用配置

1. 登录 [飞书开放平台](https://open.feishu.cn/)
2. 创建企业自建应用
3. 添加 **机器人** 能力
4. 添加权限：
   - `im:message` — 获取与发送单聊、群组消息
   - `im:message:send_as_bot` — 以应用身份发送消息
5. 事件订阅：添加 `im.message.receive_v1`
6. 发布应用并获取 `App ID` 和 `App Secret`

### 启动

**Windows：**
```bash
start.bat
```

**通用方式：**
```bash
python feishu_bridge.py
```

启动后，桥接器会：
1. 启动 OpenCode 服务
2. 连接飞书 WebSocket
3. 发送就绪通知到默认群

---

## 📖 使用方式

### 飞书对话

直接在飞书群里 **@机器人** 或私聊发消息，AI 会自动回复。

### 终端交互模式

```bash
python opencode_bridge.py -i
```

支持命令：
- `/new` — 创建新会话
- `/sessions` — 列出所有会话
- `/quit` — 退出

### 单次问答

```bash
python opencode_bridge.py "解释一下这个项目的架构"
```

### 指定模型和目录

```bash
python opencode_bridge.py -m claude-sonnet-4-20250514 -d /path/to/project "帮我重构这段代码"
```

---

## 🎨 效果展示

```
┌─────────────────────────────────────────┐
│  🤖 OpenCode                            │
├─────────────────────────────────────────┤
│                                         │
│  关于这个 Python 项目的架构，我来分析一下： │
│                                         │
│  ## 核心组件                             │
│                                         │
│  1. **feishu_bridge.py** — 飞书消息监听   │
│  2. **opencode_bridge.py** — AI 桥接核心  │
│                                         │
│  ```python                             │
│  # 主要类                               │
│  class OpenCodeBridge:                 │
│      def send(self, message): ...      │
│  ```                                   │
│                                         │
├─────────────────────────────────────────┤
│  ✅ 已完成 · 耗时 3.2s                   │
└─────────────────────────────────────────┘
         ↑ 完成后自动切换绿色主题
```

---

## 🔧 项目结构

```
feishu_opencode/
├── feishu_bridge.py      # 飞书桥接（WebSocket 监听 + 消息队列 + 飞书 API）
├── opencode_bridge.py    # OpenCode 桥接（服务启动 + SSE 流式解析 + 终端渲染）
├── config.json           # 配置文件
├── start.bat             # Windows 启动脚本
├── images/               # 截图
│   └── running_example.jpg
└── logs/                 # 日志目录（自动创建）
    ├── chat_YYYYMMDD.log
    └── error_YYYYMMDD.log
```

---

## ⚠️ 安全与风险提示（使用前必读）

本桥接器对接 OpenCode AI 自动化能力，存在以下固有风险：

- **模型幻觉** — AI 可能生成错误或误导性内容
- **执行不可控** — AI 执行操作的结果可能超出预期
- **提示词注入** — 恶意输入可能诱导 AI 执行危险操作

授权飞书权限后，OpenCode 将以您的用户身份在授权范围内执行操作，可能导致：
- 敏感数据泄露
- 越权操作
- 非预期修改

### 安全建议

1. **强烈建议不要主动修改任何默认安全配置**
2. **建议将桥接器作为私人对话助手使用**，请勿将其拉入群聊或允许其他用户与其交互
3. 飞书应用权限遵循最小化原则，只开启必要的权限
4. 请妥善保管 `config.json` 中的 `app_secret`，不要提交到公开仓库

> ⚠️ 一旦放宽相关限制，风险将显著提高，由此产生的后果需由您自行承担。

### 免责声明

本软件采用 MIT 许可证。运行时会调用飞书开放平台 API，使用这些 API 需要遵守：

- [飞书用户服务协议](https://www.feishu.cn/terms)
- [飞书隐私政策](https://www.feishu.cn/privacy)
- [飞书开放平台独立软件服务商安全管理运营规范](https://open.larkoffice.com/document/uAjLw4CM/uMzNwEjLzcDMx4yM3ATM/management-practice/app-service-provider-security-management-specifications)

使用本桥接器即视为您自愿承担相关所有责任。

---

## 📄 许可证

MIT License

---

## 🙏 致谢

- [OpenCode](https://github.com/opencode-ai/opencode) — 强大的 AI 编程助手
- [openclaw-lark](https://github.com/larksuite/openclaw-lark) — 飞书官方 OpenClaw 插件，流式卡片实现参考
- [飞书开放平台](https://open.feishu.cn/) — 完善的 API 和文档

---

<p align="center">
  <b>如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！</b>
</p>
