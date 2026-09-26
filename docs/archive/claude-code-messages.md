# Claude Code 接入 Anthropic Messages · 2026-09-25

结论：同一适配器新增 `/v1/messages` 和 `/v1/messages/count_tokens` 后，Claude Code 2.1.282 经 8790 端口的独立服务
完成了真实的 Bash 工具往返。默认配置下 Claude Code 有两处会放大 Cursor 推理消耗或打断长推理：流内报错后改发
非流式请求，并对 500 重试多达 10 次；事件级流看门狗在约 600 秒中止仍在 ping 的流。切换脚本因此写入更长的流空闲
时限，关闭非流式回退并限制重试；适配器的非流式错误响应带 `x-should-retry: false`。

## 证据来源

- 抓包和行为实验都使用隔离的临时 `CLAUDE_CONFIG_DIR`、本地假 Messages 服务和 `claude -p`：不读 `~/.claude`，
  不触发 hooks，不调用 SDK。日志只记录字段名、类型和长度，凭据头脱敏。
- 看门狗和重试的开关名取自 Claude Code 可执行文件中的字符串，只用来定位开关，结论以实验为准。
- 真实推理只有最后一次端到端验证（8790 服务，high 档）和切换预检。

## 请求形态

- 开启网关模型发现（`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`）时，启动先请求 `GET /v1/models?limit=1000`，
  读取 Anthropic 字段 type、display_name、created_at、has_more、first_id、last_id。
- 模型请求为 `POST /v1/messages?beta=true`，始终流式，`x-stainless-timeout` 默认 600。
- `messages` 里除 user 和 assistant 外，还会插入 role 为 system 的中途提醒，可能带 `output_config`。请求还带
  `thinking`（adaptive）、`context_management`、`cache_control`、`metadata`，适配器都忽略。
- 21 个工具都是客户端工具，只有 name、description、input_schema（draft-07）。工具结果以 tool_result 块返回，
  content 为字符串并带 `is_error`。
- 两次抓到的请求离线走翻译和输出校验：21 个工具全部转换成功；首轮 prompt 约 79 KB，估算约 3.7 万 token。

## 客户端行为实验

| 假服务行为 | Claude Code 设置 | 结果 |
|---|---|---|
| usage 只放在 message_delta | 默认 | 采用，缓存读写两项都计入 |
| 流内 api_error，非流式回 500 | 默认 | 1 次流式加 11 次非流式（10 次重试），约 3 分钟 |
| 同上 | `CLAUDE_CODE_MAX_RETRIES=0` 或 `1` | 1 次流式后，非流式 1 次或 2 次 |
| 同上，非流式 500 带 `x-should-retry: false` | 默认 | 1 次流式加 1 次非流式，不再重试 |
| 流内 overloaded_error，非流式回 529 | 默认，或 `CLAUDE_CODE_MAX_RETRIES=1` | 3 次流式加 1 次非流式 |
| 流内 invalid_request_error，非流式回 400 | 默认 | 1 次流式加 1 次非流式 |
| 流内 api_error | `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1` | 只有 1 次流式请求，直接报错 |
| 每 10 秒 ping，330 秒后给结果 | 默认 | 1 次流式完成 |
| 每 10 秒 ping，700 秒后给结果 | 默认，或 `API_TIMEOUT_MS=2700000` | 约 600 秒中止，改发非流式请求 |
| 同上 | 加 `CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=1800000` | 同样约 600 秒中止并改发非流式 |
| 同上 | 加 `CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000` | 1 次流式，700 秒完成 |
| 同上 | 关闭 `CLAUDE_ENABLE_STREAM_WATCHDOG` 和 `CLAUDE_ENABLE_BYTE_WATCHDOG` | 1 次流式，700 秒完成 |
| 同上 | 只加 `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1` | 约 600 秒报 `Stream idle timeout - no chunks received` |

- 默认配置下 700 秒那两例最终显示成功，是因为假服务的非流式请求立即返回；真实场景里这次非流式请求又是一轮完整推理。
- 调试日志：约 450 秒时警告 150 秒没有收到数据块，600 秒时以 300 秒没有数据块中止。ping 不能让事件级计时一直归零，
  所以只调字节级时限没用。代码中事件级时限取 `CLAUDE_STREAM_IDLE_TIMEOUT_MS` 与 300000 中较大者。
- 关闭看门狗也能通过，但 `CLAUDE_ENABLE_STREAM_WATCHDOG` 还决定后台异步子代理卡死判定的默认时限，不采用。

## 最终配置

`API_TIMEOUT_MS=2700000`、`CLAUDE_STREAM_IDLE_TIMEOUT_MS=2460000`、`CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1`、
`CLAUDE_CODE_MAX_RETRIES=2`。2460 秒长于适配器请求兜底时限 2430 秒（排队 1200 加推理 1200 加 30）。按最终配置复测：

- 流内报错：只有 1 次流式请求，直接显示 `API Error: Cursor SDK response failed (deadline_expired)`。
- 每 10 秒 ping、1300 秒后给结果：1 次流式请求，1301 秒完整收到。
- 同上、2000 秒后给结果：1 次流式请求，2001 秒完整收到；调试日志只在 1230 秒出现一条空闲警告，没有中止。

## 真实端到端

- 22:28–22:30，隔离配置目录下的真实 `claude -p`，high 档，经 8790：2 轮，88.6 秒，执行 Bash `echo` 后原样回复了随机标记。
- 8790 日志两条 completed：prompt 约 75 KB，SDK input 约 3.0 万 token（缓存写入约 2.8 万），耗时 63.8 秒和 22.3 秒。
  Claude Code 汇总的 usage 与 SDK 计数换算一致：input 4 + 4，cache_creation 27779 + 27477，cache_read 2374 + 2892，
  output 222 + 38。
- `count_tokens` 对 75 KB prompt 估算约 3.57 万，SDK 实际 3.02 万，估算偏高约 18%。

## 切换后验证 · 2026-09-26

- 09:06，真实 `~/.claude` 配置下的 `claude -p`（2.1.282）：36.7 秒，modelUsage 只有 `claude-opus-5-5-max`；8790 记录
  一条 max 流式 completed，25.6 秒。
- `claude --bg`：隔离配置目录和独立 daemon、关闭自动更新、假服务。settings 从 A 改成 B 后立即启动的会话领取的是在 A
  下预热的 spare，请求仍全部发往 B；之后新预热的 spare 也发往 B。预热进程在被领取时重新读取 settings。
- 09:27，真实配置下的 `claude --bg`（daemon 已是 2.1.283）：回复正确，会话记录的模型为 `claude-opus-5-5-max`；8790
  记录 max 流式 completed（19.4 秒）和一次 high 非流式后台小请求（6.4 秒）。验证会话已 stop 并 rm。
- 切换前就已启动的后台会话保持原 provider，不迁移。
- Mew 以 SDK 方式拉起的 Claude Code 会话（entrypoint `sdk-ts`，Mew agent 模型为 `super-relay:auto_model/alwaysday1`）
  切换后仍走 super-relay，8790 没有对应请求，也没有 400。隔离实验中 `--model`、`--settings`、进程级 ANTHROPIC_* 和
  `sdk-ts` 入口都不会让 settings.json 的 env 失效，原因在 Mew 侧，未深究；需要时改 Mew 的 agent 模型设置。
- 首次隔离 `--bg` 实验未关闭自动更新，隔离 daemon 把全局 Claude Code 从 2.1.282 升到 2.1.283；真实 daemon 09:21
  按设计自重启并接管原后台会话。之后的实验都设 `DISABLE_AUTOUPDATER=1`。
- 2.1.283 复核（假服务，关闭自动更新）：流空闲时限计算和 `x-should-retry` 处理与 2.1.282 相同；非流式回退开关改由
  类型化环境访问器读取，行为不变。按最终配置流内报错只有 1 次流式请求；去掉该开关时为 1 次流式加 1 次非流式，
  非流式 500 带 `x-should-retry: false` 不再重试；700 秒长流 1 次流式请求完成。

## 上下文窗口与 `[1m]` 后缀 · 2026-09-26

- 真实后台会话（max，2.1.283）四轮上下文依次约 6.0 万、7.5 万、10.7 万、14.5 万 token，约 19.6 万时自动压缩到
  2.1 万；压缩本身也用 max，耗时 542 秒、输出约 6.3 万 token。适配器调用 SDK 时一直带 `context=1m`，上游不是瓶颈。
- 原因：Claude Code 对不认识的 `claude-` 开头模型名按默认 200k 窗口管理（调试日志 `effectiveWindow=180000`）；
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 对这类名字不生效，原设置的 1000000 没有起作用。
- 假服务实验（关闭自动更新）：不带后缀时上下文 25 万 token 被判超限并发起压缩；带 `[1m]` 后 contextWindow 为
  1000000，不压缩。主模型、子代理和 Haiku 档会话标题请求发出的模型名都去掉了后缀，并带 1M context beta 头，
  适配器无需改动。关闭未知模型窗口限制、开启网关模型发现（/v1/models 带 max_input_tokens）、设置
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 都没有效果。
- 切换脚本改为写入带 `[1m]` 的模型名，检查服务模型目录仍用原名；已切换的环境再执行一次 `cursor` 即原地升级，
  不重新推理。已开的会话保持启动时的模型名，需要退出后重开。

## max 单轮超时与 8790 时限 · 2026-09-26

- 1M 窗口下一个会话 12:47–14:14 连续 7 轮 max 正常完成，上下文从约 7.2 万涨到 21.4 万 token，单轮 287–769 秒；
  第 8 轮（prompt 约 503 KB）跑满 1200 秒被截断为 `deadline_expired`。SDK 记录显示截断前 1 秒仍在流出 thinking
  事件（共 10294 个），上游没有卡住；按切换写入的配置，Claude Code 没有重试。
- 8790 改由 `scripts/serve_claude.sh` 启动：max 推理时限和排队上限各 1800 秒（适配器上限），请求兜底 3630 秒；
  切换脚本相应写入流空闲时限 3660 秒、请求超时 3900 秒。`/health` 新增 `limits`，`claude_switch.sh status`
  据此提示是否需要重启。

## 未验证与遗留

- Claude Code 的 WebSearch 工具会另发带服务端 web_search 工具的请求；经适配器后服务端工具被去掉，推测搜索不可用，未实测。
- 适配器仍是缓冲发送；Haiku 档后台请求与主请求共用 8790 的串行队列。
- 两个服务都是 transient systemd 单元，机器重启后需要重新启动。
