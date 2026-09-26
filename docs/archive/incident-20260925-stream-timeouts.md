# Cursor 流中断复盘 · 2026-09-25 下午

结论：`stream disconnected before completion: Cursor SDK response failed (RuntimeError)` 的直接原因，
是旧适配器的 180 秒 SDK deadline 截断了仍在 thinking 的长历史推理。截断后超时被误报成
`RuntimeError`。Codex 自动重连，但每次都是一轮新的完整推理，又撞上同一个上限。另有少量
`JSONDecodeError`：SDK 正常完成，但模型输出不是裸 JSON。窗口内没有观察到上游流未完成或 500。
重启 Desktop 后的“恢复”只是后续某次推理恰好在 180 秒内完成；适配器进程自 00:49:33 起没有重启过。

## 证据来源（全部只读）

- `systemctl --user show cursor-sdk2api.service`：NRestarts=0，启动于 00:49:33。transient 单元直接执行
  `serve.sh`，没有参数，即 SDK 180 秒、请求总时限 210 秒。
- bridge 子进程启动时间为 14:06:39、14:30:14、14:53:32、15:54:39，每次都在一次失败运行开始后约
  180 秒：`generate()` 出错会关闭 bridge，下一次请求重新拉起。
- SDK 本地存储 `index.db` 的 runs/run_events：只读取状态、时间、档位、usage 计数、事件数和最后事件的
  类型字段，并检查结果能否解析为只含 output 的 JSON 对象；没有输出任何正文。
- 出事任务 rollout（约 1.2 MB，xhigh，挂 goal 自动续跑）：只读取时间戳、事件类型、行长度和
  turn_context 的 model/effort。
- 旧适配器没有日志，无法把每次失败和 HTTP 响应一一对应；下面的归类依赖时间吻合。

## 时间线（+08:00）

xhigh 阶段：

| 开始 | SDK 状态 | 耗时 | 输入 token |
|---|---|---:|---:|
| 13:32–13:37，6 次 | FINISHED | 34.5–92.7 s | 8.0–12.3 万 |
| 13:38:46 | 未完成，最后事件 thinking | 180.0 s | — |
| 13:41:54 | FINISHED | 91.8 s | 14.1 万 |
| 13:43:30、13:46:41、13:49:42 | 未完成 | 180.0 / 172.3 / 172.0 s* | — |
| 13:52:44 | 未完成，用户 13:54:44 中断 | 128.9 s | — |
| 13:57:42、14:00:47、14:03:46 | 未完成 | 173.9 / 169.0 / 170.4 s* | — |
| 14:06:48、14:09:31、14:12:28 | FINISHED | 152.4 / 172.0 / 116.6 s | 15.8–17.9 万 |
| 14:14:28、14:17:38、14:20:38、14:23:47 | 未完成 | 168.3–179.6 s* | — |
| 14:26:42（high） | FINISHED | 48.6 s | 18.0 万 |
| 14:27:32 | 未完成 | 157.3 s* | — |
| 14:30:22、14:33:39（high） | FINISHED | 64.5 / 61.0 s | 18.1–18.2 万 |

\* 为最后一条已落盘事件距创建的时间。下一次运行都在其后约 1–15 秒开始，符合“180 秒截断、关闭
bridge、Codex 重连”的节奏。

high 阶段：

- 14:39–14:50 共 11 次 FINISHED，耗时 19–134 秒，输入 11.1–14.2 万；其中 14:42:40 的结果不是裸
  JSON（对应 `JSONDecodeError`）。
- 14:50:32 未完成（178.4 秒），重连后 14:53:41 成功。
- 14:57:36 成功但用了 168.0 秒（15.9 万）；15:00:37、15:03:46 未完成；15:06:52 成功，167.9 秒。
- 15:13:55、15:19:07 未完成（约 180 秒）；15:22:15 结果不是裸 JSON；15:24:25、15:27:36、15:30:42、
  15:33:45 未完成（169–180 秒）。首发加 5 次重连全部失败，任务停止。
- 15:54:53 用户手动继续，45.4 秒成功（18.6 万）。

## 判断

- 所有未完成运行都已流出 1000–2300 个事件，最后事件是 thinking，而不是 status 或 error。说明被本地
  截断时模型仍在输出，不是上游断流或 500。被取消的运行在 SDK 本地存储中停留为 RUNNING。
- 成功运行的耗时随历史变长逼近 180 秒（xhigh 172 秒、high 168 秒）；输入超过约 14 万 token 后失败
  明显增多。决定因素更接近 token 数和档位，而不是请求字节数。
- Python 3.11 中 `concurrent.futures.TimeoutError`、`asyncio.TimeoutError` 与 `TimeoutError` 是同一个类。
  内层 `wait_for` 超时后，外层 `future.result(timeout=10)` 重新抛出它，被 except 捕获，此时
  `future.done()` 为真，于是抛出 `RuntimeError("SDK deadline expired")`。已在 venv 中复现，并写入
  `scripts/test_failures.py`。

## Codex 客户端行为实验

使用临时 CODEX_HOME、`--ephemeral` 和本地假 Responses 服务（Codex 0.155.1），不触碰实际 daemon
和 8789 服务：

| 条件 | 结果 |
|---|---|
| `stream_idle_timeout_ms=15000`，每 5 秒一行 SSE 注释 | 约 15 秒报 idle timeout waiting for SSE |
| 同上，完全静默 | 同上 |
| 每 5 秒重发 `response.in_progress` | 30 秒后正常完成 |
| 每 5 秒发一个未知类型的 data 事件 | 30 秒后正常完成 |
| `response.failed`，code=`context_length_exceeded`，`stream_max_retries=2` | 1 次 POST，不重试，提示上下文已满、开新任务 |
| code=`invalid_prompt` | 1 次 POST，不重试，原样显示消息 |
| 自定义 code（`deadline_expired`） | 3 次 POST，Reconnecting 1/2、2/2 后报错 |

旧版每 10 秒一次的注释保活对 Codex 无效；180 秒低于 Codex 默认流空闲超时，所以此前没有暴露。
时限一旦超过默认空闲超时，就必须用 data 事件保活。本机没有设置 `stream_idle_timeout_ms`，
默认值没有从二进制中直接读出。

## 修复与取舍

- `scripts/failures.py` 定义固定标签；`asyncio.timeout().expired()` 区分适配器自己的 deadline 与其他
  TimeoutError。SDK 层超时为 `deadline_expired`，排队超时为 `queue_timeout`，总兜底超时为
  `request_timeout`（见文末追加）。
- HTTP 层改用 `concurrent.futures.wait` 轮询，不再依靠 TimeoutError 判断是否完成。
- 模型输出解析、schema、tool_choice、call_id 检查失败统一为 `invalid_model_output`。
- prompt 超过 3 MiB（可配置）时不调用 SDK，返回 `context_length_exceeded`，Codex 不会重试。
- 保活改为每 10 秒重发 `response.in_progress` data 事件。
- 默认时限：high 300 秒、xhigh 420 秒、max 600 秒，推理时限拿到 SDK 后才开始计算，排队另限 900 秒；可用参数或环境变量
  调整，上限 1800 秒。高于观察到的最长成功耗时（约 172 秒）并留有余量，但仍有上界。
- 代价：失败要等更久才暴露，Codex 还会对可重试错误自动重连（本机观察到 5 次）。连续出现
  `deadline_expired` 时应开新任务或降档，而不是等重连耗尽。适配器仍然不自动重试模型生成。
- 私有日志 `~/.codex/cursor-sdk2api/logs/requests.jsonl` 记录每次请求的元数据，不含正文或凭据。
- `probe_service.py` 的客户端超时按档位改为 335 / 455 / 635 秒。

## 验证

- 离线单元测试全部通过，`probe_desktop.py --stub` 通过。
- 在临时端口 18799 用新代码起服务，`probe_service.py --effort high` 两次真实调用分别 23.0 / 8.1 秒完成；
  日志目录 0700、文件 0600，字段符合预期；验完关闭。8789 上的服务需用户重启后才使用新代码。

## 仍未确认

- Codex 默认 `stream_idle_timeout_ms` 与 `stream_max_retries` 的确切值（观察到 5 次重连）。
- 历史继续增长时新时限是否够用；长任务仍可能出现 `deadline_expired`。
- 本窗口没有上游 500，但它仍可能发生，见 [切换事故复盘](incident-20260925.md) 中的首轮 500 样本。

## 追加：16:08–16:30 两个任务并发时的排队超时

服务重启前，图像支持任务（xhigh）与本修复任务（high）同时使用 8789。SDK 后端用一把锁串行执行请求，
旧版 210 秒总时限从请求到达就开始计时，把排队时间也算在内。

- 16:10:11、16:13:20、16:16:23 三次 xhigh 运行在 171–181 秒时停在 thinking：仍是 180 秒推理上限。
- 之后两个任务交替占用 SDK。16:20:51（xhigh，117 秒）、16:23:52（xhigh，148 秒）、16:26:32（high，
  48 秒）、16:27:31（xhigh，144 秒）、16:30:06（high，42 秒）都在请求到达后约 210 秒被取消，即排队
  60–160 秒加推理 40–150 秒；16:19:25 的 high 运行只落盘 2 个事件。bridge 重新拉起时间（16:22:48、
  16:30:54）与取消时间吻合。
- 图像任务首次请求加 5 次重连全部失败，16:29:55 停止；本任务在对方停止后于 16:31 独占 SDK 才成功。
- 当时 8789 仍是旧代码（NRestarts=0，/health 没有 failure_labels），这次报错不能用来评价第一版修复。
  但第一版（bfeccc6）仍把排队计入总时限（档位时限加 30 秒），并发时同样会互相拖垮。

第二版修正：

- SDK 锁等待单独计时，默认 900 秒（`--queue-timeout`，环境变量 `CURSOR_FALLBACK_QUEUE_TIMEOUT`），
  超时为 `queue_timeout`。
- 推理时限在拿到锁之后才开始计算，排队不再消耗推理时间。
- Service 总时限为排队上限加档位时限再加 30 秒，仅作兜底，超时为 `request_timeout`。
- 日志增加 `queue_s`（拿到 SDK 前的排队耗时）。
- 代价：请求仍然串行，并发任务的等待会叠加。同时跑两个长历史 xhigh 任务时，后到的请求可能等上几分钟。
  建议同一时间只跑一个长 Cursor 任务。

## 追加：JSONDecodeError 统计与处理

截至 16:40，SDK 本地存储共 80 次已完成结果，6 次不是裸 JSON（只统计开头字符类别、解析错误位置和能否
从中解析出 output 对象，没有读取正文）：

| 形态 | 次数 | 是否含完整合法的 output 对象 |
|---|---:|---|
| 整段是一个 json 代码块 | 2 | 是，代码块后无多余内容 |
| 前面一段说明文字，后接 output 对象 | 3 | 是，对象后无多余内容 |
| 前面说明文字，后面 JSON 写坏 | 1 | 否 |

长历史（约 18–22 万 token）下约 7% 的输出违反格式；每次失败都会让 Codex 重连并重跑完整推理。

处理：

- 只接受“整段恰好一个代码块”这一种包装，去掉后按原规则严格解析。无歧义、不丢模型内容。
- prompt 在请求 JSON 之后追加一句格式提醒，因为开头的格式要求在长历史里离模型输出位置太远。
  `request_payload()` 负责从 prompt 中取回请求 JSON，测试与探针统一使用它。
- 前面有说明文字的输出仍报 `invalid_model_output`：静默丢弃说明文字会改变模型的回答，不做修补。
- 效果需要重启服务后从私有日志观察 `invalid_model_output` 的比例，目前没有上线数据。

## 追加：三档时限统一为 1200 秒

按用户要求，high、xhigh、max 的默认推理时限统一为 1200 秒；排队上限同步调到 1200 秒，否则排在一次跑满
时限的请求后面时会先报 `queue_timeout`。请求总兜底时限为 1200 + 1200 + 30 秒；`probe_service.py`
客户端超时改为 1235 秒。代价：真正卡住的推理要约 20 分钟才报 `deadline_expired`，之后 Codex 还会自动重连。
仍可用参数或环境变量单独调整，上限 1800 秒。

## 追加：20:23 max 请求“完全没有反应”

- 20:23:04 请求到达（input 4 项，prompt 149 KB，queue_s 0）。SDK 运行 20:23:14–20:36:57，状态 FINISHED，
  持续流出 1682 个事件，中间没有停顿或错误。20:37:01 用户重启远程连接，适配器记为 `client_disconnected`
  （836 秒），结果没有送达。
- 另一个 max 请求（已排队 196.6 秒）和一个 xhigh 请求排在它后面，同时断开。重连后队列为空，新请求立即执行。
- 结论：不是故障，而是缓冲发送加开放式 max 长推理。`/health` 新增 `progress` 快照，`switch.sh status`
  展示排队数、运行时长、流更新数和空闲秒数，用来区分“仍在思考”和“上游卡住”。

## 追加：说明文字前缀的无损补救 · 2026-09-26

- 截至 09-26 16:30，两份请求日志共 8 次 `invalid_model_output`（Codex 7 次、Claude Code 1 次），都能对上 SDK 已完成
  的运行，形态相同：开头一段进度说明，后面是完整合法、只含 output 数组的 JSON 对象，之后没有其他内容（一次对象包
  在代码块里）。长历史（prompt 约 60–90 万字节）下，模型偶尔把宿主要求的“调用工具前先说明”写在 JSON 外面。
- 处理：`recover_output` 在严格解析失败时，接受“说明文字 + 一个只含 output 数组的合法对象（可包在代码块中）+ 之后无内容”，
  把说明文字插为 output 开头的 message；已有 message 包含同样文字时不重复。对象仍走原有 schema、tool_choice、call_id
  校验；JSON 写坏、对象后有文字、多余字段或重复键仍报 `invalid_model_output`。prompt 同时要求说明写进 message 条目。
- 这修正了上文“前面有说明文字的输出仍报 `invalid_model_output`”的取舍：说明文字保留为 message，不改变模型的回答。
- 请求日志新增 `output_repair`（`code_fence` 或 `prose_prefix`）。
- 离线回放 SDK 本地存储中全部已完成结果：Codex 225 个（裸 JSON 210、代码块 4、说明前缀 11），Claude Code 65 个（裸 JSON 64、
  说明前缀 1）。8 次失败全部可补救，其中 2 次 output 里已有相同说明、没有重复插入；裸 JSON 结果不受影响。
- 两个适配服务重启后生效。
