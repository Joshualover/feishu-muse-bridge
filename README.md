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
export LLM_API_KEY="sk-..."
export LLM_MODEL="deepseek-chat"
python3 examples/consumer_openai.py          # 常驻轮询
python3 examples/consumer_openai.py --once   # 只跑一轮
```

队列就是桥与处理器之间的全部接口，你可以把示例里的 LLM 调用换成自己的 agent、脚本或工作流引擎。

## English summary

**feishu-bridge** connects a Feishu (Lark) enterprise self-built app bot to a local processor:
messages arrive over the official `lark-oapi` WebSocket long connection (no public IP or
webhook endpoint needed), are stored in a local SQLite queue (`pending / processing /
done / failed`), and any external agent can poll the queue, process messages, and reply
through the Feishu API using `bridge/send_reply.py`. Includes a healthcheck with
auto-restart, an optional HTTP-proxy patch for environments without direct egress, and
an example consumer for OpenAI-compatible chat APIs. See `SETUP.md` for the Feishu
console configuration checklist. MIT licensed.

## License

MIT，见 [LICENSE](LICENSE)。
