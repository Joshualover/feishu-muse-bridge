# 飞书（Lark）桥接服务

把飞书企业自建应用机器人的消息桥接到本地处理：消息经长连接（WebSocket）接收后写入本机 SQLite 队列，外部 agent 轮询队列处理，再通过飞书 API 回发。

## 架构

```
飞书开放平台(企业自建应用机器人)
        │  长连接 WebSocket (lark-oapi SDK, 自动重连)
        ▼
bridge/ingest.py       接收 im.message.receive_v1 事件，解析文本，去 @ 提及
        │
        ▼
SQLite 队列 queue.db   表 messages (pending/processing/done/failed)
        ▲
        │  轮询 list_pending → mark_processing → mark_done/mark_failed
外部 agent(自行实现)
        │
        ▼
bridge/send_reply.py   取 tenant_access_token → 发消息/回复消息
        │
        ▼
飞书 API (open.feishu.cn)
```

数据流：飞书消息 → 入队 → agent 处理 → 回发到飞书。长连接断线由 SDK 自动重连；消息先落库保证不丢。

## 目录结构

```
feishu-bridge/
├── requirements.txt       依赖：lark-oapi, requests
├── bridge/
│   ├── __init__.py        包标记
│   ├── queue_store.py     SQLite 队列（读写函数 + CLI）
│   ├── ingest.py          飞书长连接接入（事件 → 入队）
│   ├── config.py          配置：发送者白名单 / 自动 ack 参数（CLI 管理）
│   ├── progress.py        进度回执卡片（占位 → 更新 → 定稿）
│   └── send_reply.py      消息回发 CLI
├── run_ingest.sh          后台启动 ingest 并写 pid
├── healthcheck.sh         健康检查 / 自动重启
├── SETUP.md               飞书开放平台配置清单
└── README.md              本文档
```

数据目录（默认 `~/feishu-bridge-data`，可用环境变量 `FEISHU_BRIDGE_DATA_DIR` 覆盖）：

- `credentials.json`：app_id / app_secret（权限 600）
- `queue.db`：SQLite 消息队列
- `ingest.log` / `ingest.pid`：ingest 进程日志与 pid

## 快速开始

1. 按 [SETUP.md](SETUP.md) 在飞书开放平台完成应用配置，并写入 `~/feishu-bridge-data/credentials.json`。
2. 安装依赖（仅首次）：
   ```bash
   pip install -r requirements.txt
   ```
3. 启动接入：
   ```bash
   ./run_ingest.sh          # 后台运行，日志 ~/feishu-bridge-data/ingest.log
   ./healthcheck.sh         # 应输出 STATUS: OK
   ```

## 队列用法

```bash
# 手动入队一条消息（可用于测试）
python3 -m bridge.queue_store add --json '{"feishu_message_id":"om_test_001","chat_id":"oc_test","chat_type":"p2p","sender_open_id":"ou_test","msg_type":"text","text":"你好","create_time_ms":1700000000000}'

# 查看待处理消息
python3 -m bridge.queue_store pending --limit 10

# 查看各状态统计
python3 -m bridge.queue_store stats
```

## 回发消息（给外部 agent 的参考调用）

```bash
# 直接发消息到会话
python3 -m bridge.send_reply --chat-id oc_xxx --text "处理结果"

# 回复某条消息（reply-to 传飞书 message_id）
python3 -m bridge.send_reply --chat-id oc_xxx --reply-to om_xxx --text "回复内容"
```

## 主会话接入（2026-10-29 起）

飞书消息不再由独立边聊的 worker 处理，改由主会话的 Edison 本人直接处理：

- 钩子 `feishu-inbox-watch`（`~/hooks/definitions/feishu-inbox-watch.json`，每 15 秒轮询 `~/hooks/scripts/feishu-inbox-check.sh`）的唤醒投递面改为 `main`，此前投递的边聊（"将飞书接入AI Agent Muse"）保留、不删除，仅不再接收新唤醒。
- 处理指令原文见 [docs/main-session-prompt.md](docs/main-session-prompt.md)：飞书消息与主聊天消息同等对待，主会话的全部记忆、目标、工具与对话上下文都可用，飞书里的指代优先用本聊天上下文理解；进度回执卡片流程、审批先行规则、回复格式要求不变。
- 处理完且已在飞书回复后，主聊天静默结束，不重复搬运对话；仅处理失败、需主聊天拍板、或飞书明确要求同步时，才在主聊天里说一声。
- ingest / 队列 / 回发 / 看门狗 / 审批提醒均未动，健康检查与白名单机制不变。

## 白名单与超时自动 ack（2026-10-23 新增，借鉴 muse2feishu）

配置存于数据目录的 `config.json`（默认 `~/feishu-bridge-data/config.json`，可用 `FEISHU_BRIDGE_DATA_DIR` 切换）：

- **发送者白名单**：`whitelist_enabled=true` 时，只有 `allowed_sender_open_ids` 里的发送者消息才会被入队处理；陌生人消息**静默忽略**（不入库、不发回执、不回应），防止陌生人通过飞书指挥 agent。白名单为空且开启时，全部消息都会被拦截（安全默认值）。
- **超时自动 ack**：`auto_ack_enabled=true` 时，消息入队 `auto_ack_seconds`（默认 60）秒仍无人认领（钩子/worker 没起来），ingest 自动发一条文字提醒兜底。正常情况几秒内就被认领，这条永远不会触发。

```bash
python3 -m bridge.config show                    # 查看当前配置
python3 -m bridge.config whitelist add <open_id> # 加入白名单
python3 -m bridge.config whitelist remove <open_id> # 移出白名单
python3 -m bridge.config whitelist on|off        # 开关白名单
python3 -m bridge.config set auto_ack_seconds 60 # 改自动 ack 超时秒数
```

## 审批提醒同步到飞书（2026-10-24 新增）

痛点：agent 申请敏感操作（发邮件、改账号等）时，审批卡片只出现在客户端；用户没看客户端就不知道有事项在等他拍板。

做法：新增事件钩子 `approval-watch`（`~/hooks/definitions/approval-watch.json`，每 5 分钟一轮；脚本 `~/hooks/scripts/approval-watch.sh`）：

1. 钩子唤醒 worker，worker 调 `permissions.list_pending` 查当前待审批事项；
2. 对照 `~/feishu-bridge-data/approval_notified.json` 去重，每个事项只推一次；
3. 用 `bridge.send_reply` 发一条飞书 p2p 消息（`--chat-id oc_xxx`），写清**事项说明 + 授权范围**，并提醒用户去**客户端审批卡片**上点同意/拒绝（飞书侧没有批准接口，只能提醒、不能代点）；
4. 已解决的事项自动从已通知记录中清理。

设计取舍：bash 侧没有查询审批队列的接口，所以脚本每轮固定唤醒、真正的检查放在 worker 里做；无新事项时 worker 直接静默结束，不打扰。

## 干跑测试（不联网）

在没有真实凭证、不装依赖的情况下，也可以本地自测大部分逻辑：

```bash
# 1. 编译检查所有 Python 文件
python3 -m py_compile bridge/queue_store.py bridge/ingest.py bridge/send_reply.py

# 2. 队列 CLI：入队 → 查询 pending → 统计
python3 -m bridge.queue_store add --json '{"feishu_message_id":"om_test_001", ...}'
python3 -m bridge.queue_store pending
python3 -m bridge.queue_store stats

# 3. 回发 dry-run：只打印将发送的 payload，不发网络请求
python3 -m bridge.send_reply --chat-id oc_test --text "你好" --dry-run
python3 -m bridge.send_reply --chat-id oc_test --reply-to om_xxx --text "回复" --dry-run
```

注意：

- `ingest.py` 运行需要 `lark-oapi`（`pip install -r requirements.txt`）和真实凭证，否则按设计打印中文错误并退出码 2。
- 干跑时可临时放一个假凭证文件 `~/feishu-bridge-data/credentials.json`（`{"app_id":"cli_x","app_secret":"x"}`，权限 600）供 `send_reply --dry-run` 读取；用完记得删除，不要与真实凭证混淆。

## 健康检查

```bash
./healthcheck.sh
```

输出状态：`OK`（进程与队列均正常）、`REPAIRED`（进程不在但凭证齐全，已自动重启）、`DOWN-CREDENTIALS-MISSING`（无凭证，需先配置）、`DOWN`（其他故障）。进程不在且凭证齐全时脚本会自动调用 `run_ingest.sh` 重启。

## 说明

- 本仓库是骨架代码，不包含真实凭证、不包含 agent 业务逻辑；外部 agent 直接复用 `queue_store` 的函数即可。
- 队列状态机：`pending` → `processing` → `done` / `failed`；agent 用 `mark_processing` 抢占后处理，成功 `mark_done(id, reply_text)`，失败 `mark_failed(id, error)`。

## 示例消费者

`examples/consumer_openai.py` 是一个最小可运行示例：轮询队列 → 调任意 OpenAI 兼容的聊天接口生成回复 → 回发飞书。

```bash
export LLM_BASE_URL="https://api.deepseek.com/v1"
export LLM_API_KEY="<你的-deepseek-key>"
export LLM_MODEL="deepseek-chat"
python3 examples/consumer_openai.py          # 常驻轮询
python3 examples/consumer_openai.py --once   # 只跑一轮
```

队列就是桥与处理器之间的全部接口，你可以把示例里的 LLM 调用换成自己的 agent、脚本或工作流引擎。

## English summary

**feishu-muse-bridge** connects a Feishu (Lark) enterprise self-built app bot to a local processor:
messages arrive over the official `lark-oapi` WebSocket long connection (no public IP or
webhook endpoint needed), are stored in a local SQLite queue (`pending / processing /
done / failed`), and any external agent can poll the queue, process messages, and reply
through the Feishu API using `bridge/send_reply.py`. Includes a healthcheck with
auto-restart, an optional HTTP-proxy patch for environments without direct egress,
sender whitelisting (stranger messages silently dropped), a timeout auto-ack
safety net when no worker claims a queued message in time, progress-receipt
cards that update in place until the final answer, and an example consumer
for OpenAI-compatible chat APIs. See `SETUP.md` for the Feishu
console configuration checklist. MIT licensed.

## License

MIT，见 [LICENSE](LICENSE)。
