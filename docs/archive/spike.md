# Cursor SDK spike · 2026-09-24

范围更正（2026-09-25）：以下是历史阶段 B/C 的局部实验，不能等同 Desktop 切换成功。
旧版随后发生 401 / namespace 400 / 常驻 daemon 未刷新的事故；修复与新验收见
[事故复盘](incident-20260925.md)。

历史结论：**阶段 B 已通过。** 最终配置为 proxychains、HTTP/1.1 偏好、共享空工作目录中的
独立 agent；固定 Opus 5.5 high（context=1m、fast=false），24 / 24 输出首次严格校验成功，
包含 3 次完整函数结果往返。没有推理重试或 JSON 修复。先前失败实验保留如下。

## 三个问题

| 问题 | 当前证据 | 结论 |
|---|---|---|
| 能关闭 agent 自带工具吗？ | `tools=[]` 序列化为 `{"names": []}`；最终 24 次输出原生工具事件为 0，临时工作区无变化 | 支持禁用；实测未观察到原生执行 |
| prompt 注入 Codex tools schema 后，Opus high 能稳定输出工具调用吗？ | 最终 24 / 24 严格解析和语义校验成功，含 3 次完整往返；每次均有 finished、完整 result 与 done | 本组固定场景通过；不是任意工具或长期可用率保证 |
| 必须 proxychains4 才能看到 Opus 吗？ | 三条路径目录均有 Opus；ambient/direct 推理报地区限制，proxychains 成功推理 | 目录不必；本机推理使用 proxychains |

## 最终通过的实验

`--route proxychains --transport http1 --shared-workspace --rounds 3 --timeout-seconds 180`。
7 个场景 × 3 轮，加 3 次函数结果续答，共 24 次输出；21 个独立 agent，仅共享空工作目录和
SDK 内部目录缓存，不共享会话历史。SDK 原生工具仍关闭，未执行模型输出的任何命令。

24 / 24 首次严格 JSON、schema 和预期参数校验成功；所有 SDK 结果都回报指定 high 参数，
并收到完整 result、done、流结束。3 次 function_call_output 往返成功，原生工具事件为 0，
工作区变化为 0。单次推理耗时最短 5.728 秒、最长 44.081 秒、均值 16.76 秒。
`tool_probe_passed=true`，结束操作为 finished。安全报告：
`/tmp/codex-cursor-sdk-spike/tools-http1-shared-proxychains.json`。

本实验同时改变传输偏好和目录复用方式，不能单独归因于 HTTP/1.1；也没有抓包证明服务端
未覆盖协议偏好。阶段 C 沿用这组经过验证的组合，保留有界超时和明确错误返回。

## 认证与模型目录实测

Linux / Python 3.11，`cursor-sdk==1.0.32`，native bridge version `1.0.0`。
CLI OAuth accessToken 在 ambient / proxychains 下都返回 401 `Invalid User API Key`；
SDK key 与 CLI 登录令牌不同。用户随后完成官方 SDK 浏览器授权，key 已以 0600 保存在
本机受保护文件中，未写入仓库，也无需粘贴到对话。

同一 SDK key 下，三种网络路径的目录结果：

| 路径 | 成功 / 实际请求数 | 模型目录 | 异常 |
|---|---:|---|---|
| ambient，保留现有代理环境 | 3 / 3 | 每次 42 个模型，含 Opus 5.5 | 无 |
| direct，移除六个常用代理环境变量 | 1 / 2 | 成功时同样 42 个模型 | 第二次 HTTP 500，按探针规则停止 |
| native bridge 前置 `proxychains4 -q` | 3 / 3 | 每次同样 42 个模型 | 无 |

所有成功响应的模型 ID、Opus 参数与变体集合相同。direct 不等于证明网络中不存在透明代理；
这里只验证清除 HTTP/HTTPS/ALL_PROXY（大小写）后的行为。Python 到本机 bridge 的 HTTP
客户端设 `trust_env=False`，`proxychains4` 只包裹 native bridge。

实际目录 ID 是 `claude-opus-5-5`，没有独立的 `claude-opus-5-5-high` 条目。
模型参数：context=`300k|1m`，effort=`low|medium|high|xhigh|max`，fast=`false|true`。
唯一默认变体是 context=1m / effort=medium / fast=false；因此不能直接用默认档冒充 high。
本次 high 选择为：

```json
{"id":"claude-opus-5-5","params":[{"id":"context","value":"1m"},{"id":"effort","value":"high"},{"id":"fast","value":"false"}]}
```

目录包含对应的 xhigh / max 组合，后续三个对外别名只改变 effort。
探针根据目录默认变体替换 effort，并验证目标组合确实在 variants 中；不会静默换模型。

## 推理路径与错误诊断

ambient 首轮 21 个初始请求都返回 SDK `error`、零文本 delta，没有取得可解析模型输出；
旧探针未在 terminal error 后停止，现已修正为立即停止。这不是 21 次 JSON 解析失败，
也不能据此评估 Opus 的工具协议能力。

补充的单条诊断在 ambient 和 direct 均得到“模型提供商不支持当前地区”的 SDK 状态消息。
proxychains 的第一次单条诊断超时；保持同样配置的第二次成功，Opus high 返回 2 字符文本，
SDK 状态 `finished`，模型推理耗时约 30 秒。未修改系统代理配置。

所以**目录可见不要求显式 proxychains；本机实测成功的推理路径是 proxychains**。
该结论限于本次机器、出口和账号，不能将目录可见等同于推理可用，也不能承诺路径永不超时。

Python 1.0.32 的 bridge 转换会丢弃 TypeScript `RunResult.error`，但错误仍存在于 SDK
`status` 消息。探针先消费 `run.events()`，只保存白名单错误分类，再取 `run.wait()`；
不保存原始错误正文、URL 或认证信息。

## SDK 浏览器授权

同一 Cursor 账号可通过官方 `Cursor.auth.login()` 创建 user API key，默认有效期 90 天。
Python SDK 外观没有此入口；`scripts/login_sdk.py` 调用 bundled TS，默认只预览，
`--login` 才启动授权。它独占创建 0600 文件，不打印 key，不覆盖旧文件，不更改 CLI 登录。
也可在 Cursor Dashboard → API Keys 创建 User API key，Admin API key 不适用。参见
[SDK 官方认证说明](https://cursor.com/docs/sdk/typescript#cursorauth)。

## 已完成的离线准备

`scripts/spike_tools.py` 默认只输出实验计划；显式 `--run` 且有 key 才会发推理。
默认 24 次预期输出，覆盖单调用、多调用、转义、嵌套参数、枚举、纯文本、
function_call_output 往返及写文件请求的隔离。探针只解析调用，不执行它们。
离线 stub 覆盖成功往返、格式/schema 拒绝、SDK 非成功状态、原生工具事件、工作目录变化、
密钥脱敏与未完成分母。这些是探针验证，**不计入 Opus 成功率**。
此外已用持续 heartbeat 的 stub 验证 wall-clock 超时，用关闭失败的 stub 验证仅清理本次
bridge 进程，并完整跑过 24 次输出的 stub 流程。14 项离线测试通过，另覆盖 high 参数解析、
SDK 状态错误脱敏与运行失败立即停止。

1.0.32 的回调必须用 `SendOptions` 注册；原生事件名是 `toolCall`（step）和
`tool-call-started` / `tool-call-completed` / `partial-tool-call`（delta）。成功状态是
`finished`，不是 `completed`。探针同时检查 agent 创建、运行和关闭后的目录状态。

另核对 bundled `@cursor/sdk` native runtime 的 `setting-sources.js`：Python 会在 wire 上
省略空 `setting_sources`，但 native 将 undefined 和 [] 都映射为 project/user/team/mdm/plugins
层全部关闭。因此固定版本下仍可隔离这些层的 hooks/MCP/rules；无需更改 HOME。
`~/.cursor/sandbox.json` 读取独立于 settings 层，不能据此声称所有用户文件都不会被读取。

## 早期工具协议实验（未通过）

按三个 Codex function schema（exec_command、write_stdin、request_user_input）构造
7 个固定场景，每组包含一次 function_call_output 往返。所有调用只做 JSON/参数校验，
不执行生成的命令；往返使用模拟工具结果，沿用模型返回的 call_id。

proxychains 主实验计划 3 轮共 24 次输出，实际完成前 15 次，随后 SDK HTTP 500 `internal`
使实验退出。已完成的 15 次均使用指定 high 参数，状态 finished；严格解析、schema 与
预期参数匹配为 15 / 15（100%），没有修复 JSON、重试推理或切换模型。
但按原定全部计划输出计算为 15 / 24（62.5%），`tool_probe_passed=false`。

保留主实验失败后另做一次独立补测（1 轮、8 次计划输出），前两次通过；第三次多调用场景
收到 15 个文本 delta 后发生 `APITimeoutError`，没有成功终态，立即停止。没有把部分文本
拼成成功响应，也没有将补测覆盖主实验。

| 实验 | 计划输出 | 成功完整输出 | 有结果记录的尝试 | 结束原因 |
|---|---:|---:|---:|---|
| 主实验，3 轮 | 24 | 15 | 15 | SDK HTTP 500 `internal` |
| 补测，1 轮 | 8 | 2 | 3 | `APITimeoutError`；该次仅收到部分流 |
| 合计 | 32 | 17 | 18 | 两组均未整体通过 |

合计 17 / 17 **完整返回**严格解析与参数匹配成功；按全部计划输出计算为 17 / 32（53.125%）。
这两个分母必须分别陈述。一个多调用尝试虽有文本 delta，也不计为完整输出或解析成功。

成功样本覆盖单/多调用、引号与换行、嵌套参数、枚举、纯文本、写文件请求的调用输出，
以及 2 次完整函数结果往返；原生工具事件为 0，所有测试工作区无变化。
隔离证据不等于全机文件审计，也不能把这组固定样本推广为任意工具或长会话的可靠性保证。

本机原始安全报告（凭据不在报告内）：
`/tmp/codex-cursor-sdk-spike/tools-live-proxychains.json`、
`/tmp/codex-cursor-sdk-spike/tools-live-proxychains-supplement.json`；目录报告为同目录的
`catalog-live-{ambient,direct,proxychains}.json`。这些临时文件不作为长期可用资源依赖。

## 总时限与终态诊断修正

旧探针的 `DeadlineExpired` 继承 `TimeoutError`；Python 3.11 中 `socket.timeout` 是同一类，
SIGALRM 在 socket recv 中触发时会被 httpcore 转成 ReadTimeout，再被 SDK 转成 APITimeoutError。
已用本机 socketpair 与实际 httpcore read 离线复现，改为继承 RuntimeError 并加回归测试。
因此旧补测确实未成功结束，但其 APITimeoutError 标签**不能证明是 HTTP 空闲超时**；
也可能是探针 120 秒总时限到期。未取得原始阶段时间，不能倒推确定原因。

修正后新增的诊断字段保留异常链类名、失败操作、单次耗时、流阶段、首个文本时间、完整 result / done
是否出现，不记录文本正文或原始错误消息；格式失败样本仍可记录合成案例的模型输出。即便收到可解析文本或完整 result，若流随后异常，
仍不会记为成功。新增测试分别验证流未结束和 terminal wait 异常不会混为模型格式错误。

独立多调用诊断以 180 秒总时限重测一次：约 10.08 秒发 Send，35.73 秒首文本，37.69 秒依次
收到 FINISHED、完整 result、done、流结束，38.40 秒关闭 agent。该次工具参数校验成功，
没有重试；说明 Python 通路可以正常完成，不证明先前全部超时都已解决。
安全跟踪记录在 `/tmp/codex-cursor-sdk-spike/trace-multiple-python.json`。

修正后的默认传输、独立工作目录实验（每次 180 秒，计划 24 次）成功返回 6 次；首个
function_call_output 的续答持续 RUNNING，仅收到 11 个 heartbeat 对应的 unknown 事件，
无文本、完整 result 或 done，180.001 秒以 DeadlineExpired 结束。已定位为等待 SDK 生成
终态，而非 JSON 解析或拿到成功结果后的收尾。安全报告：
`/tmp/codex-cursor-sdk-spike/tools-diagnosed-proxychains.json`。

直接调用 TS SDK 的默认配置和请求 HTTP/1.1 配置各做一次两轮函数结果往返，分别约
42 / 80 秒完成；4 个输出再用 Python 探针的严格 JSON/schema/模型档位校验，全部通过，
无原生工具事件或工作区变化。这些单次对照证明两种配置可用，不能单独证明稳定性修复。

公开 API `Cursor.configure({local:{useHttp1ForAgent:true}})` 可以请求 HTTP/1.1 + SSE。
Python bridge 没有对应选项；`--transport http1` 用 bundled Node 的 ESM preload 配置
**同一个** SDK 模块。服务端 FORCE 配置仍可能覆盖偏好，因此报告只称 transport preference。
直接持有 Node PID，也避免仅终止外层 shell 后漏掉 bridge。SDK 关闭和进程清理失败单独
保留，不覆盖原始目录/创建/发送错误。

`--shared-workspace` 让独立 agent 共用一个空目录，复用 SDK 自带模型目录缓存；每个场景
仍调用 create 新建 agent，会话与 checkpoint 按 agent ID 隔离。每次创建、运行和关闭前后
继续检查目录变化。没有用内部环境变量伪造目录，也没有重试推理或忽略错误终态。


## 阶段 C 验证（2026-09-24）

服务沿用最终 spike 的 SDK 配置。真实 loopback HTTP 验证全部 completed：

| 请求 | 用时 | 验证 |
|---|---:|---|
| high function_call | 28.84 秒 | lookup 参数严格等于合成输入 DEMO-42 |
| high function_call_output 续答 | 13.91 秒 | previous_response_id 关联，回答包含宿主结果 17 |
| xhigh 文本 | 15.02 秒 | 完整文本 SSE 生命周期，SDK 模型档位一致 |
| max 文本 | 31.91 秒 | 完整文本 SSE 生命周期，SDK 模型档位一致 |

没有重试推理；服务校验完整 result、done、流结束、finished、模型档位、原生工具事件为零和
工作区未变化。文本 SSE 包含 content_part.added/done，函数 SSE 包含 arguments.delta/done。

另用真实 Codex 0.155.1 + 当前服务 + StubSDK 验证：Codex 收到 exec_command 调用，宿主执行
合成 printf，下一请求携带匹配 call_id 的 function_call/function_call_output，服务回复最终
ROUNDTRIP_OK，CLI exit 0。这证明协议往返兼容，不代表该次使用了真实 Cursor 模型。
当前目录 apply_patch_tool_type=null 实测只发 function 工具；对照设为 freeform 时多出
custom/apply_patch。请求压缩须通过 features.enable_request_compression=false 关闭。
当时另以用户 CLI 配置验证，仅观察到四个 function，CLI exit 0；该实验没有复现 Desktop
的 code_mode_host 与登录后插件工具形态。把 namespace 当作“未来额外插件”是错误判断：
事故任务已经包含 codex_app namespace 下的 28 个函数。2026-09-25 适配器已支持 namespace，
实际 app-server + 插件环境的三档真实 SDK 往返另见事故复盘。

本地 stub 单元测试覆盖严格解析、SSE 顺序、函数往返、失败终态、超时、取消/关闭、SDK 工具
限制、三档模型映射，以及配置/凭据恢复、中断事务和外部修改保护。上游仍可能返回 500 或
超时，服务明确失败，不接受部分成功。

## 官方依据

- [Python SDK：限制工具与 SDK 用法](https://cursor.com/docs/sdk/python#restricting-the-toolset)。
- [TypeScript SDK：模型目录和认证](https://cursor.com/docs/sdk/typescript#cursormodelslist)。
- [Python SDK 1.0.32 发布包](https://pypi.org/project/cursor-sdk/1.0.32/)。
- [SDK bridge 流式规范](https://github.com/cursor/sdk-bridge/blob/main/docs/streaming.md)。

已核对 1.0.32 wheel：`cursor_sdk/types.py` 的 AgentOptions/tools 序列化（1149、1163、1196–1198），
`LocalAgentOptions`（615–654），`SendOptions.on_delta`（1226–1233）；
`_client.py` 的 `launch_bridge(command, workspace, state_root)`（189–227）。
TypeScript 文档的 `systemPrompt` 替换有账号限制，spike 不依赖它；
Python 1.0.32 的 `AgentOptions` 没有这个字段。
