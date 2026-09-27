---
name: cursor-sdk-bridge-ops
description: 运维独立 cursor-sdk-bridge 服务：检查 Codex 与 Claude Code 的 Cursor SDK 适配器、排查流中断和超时、部署版本、空闲重启、切换新会话或恢复原 provider。用于 Cursor SDK response failed、stream disconnected before completion、dashboard 状态及运行版本不符；不用于 Mew 设置或迁移已有会话 provider。
---

# Cursor SDK Bridge 运维

## 快速入口

- 首条命令：`cursor-sdk-bridge status`，只读；命令不在 PATH 时用 `~/.local/bin/cursor-sdk-bridge status`。
- 只读检查服务进度、版本、配置和日志；部署、切换及验收按当前任务已有授权执行。
- 独立项目维护实现，本 skill 只提供操作入口；不要把代码或依赖重新拷回 skill。
- 项目根目录为本 skill 目录的 `../../..`；项目命令从根目录执行。
- 复杂操作读 [operations.md](../../../docs/operations.md)，
  协议和恢复设计在 [design.md](../../../docs/design.md)，脱敏实验和验收在 [docs/evidence/](../../../docs/evidence/)。
- SDK key、原始配置备份和日志留在原地，不打印凭据、不迁移目录、不改现有权限。
- 不改 Mew 设置，不迁移已有会话 provider，不从正在运行的 Codex 任务里停 daemon。

## 实例与版本

| 实例 | 端口 | 普通 user unit | 状态目录 |
| --- | --- | --- | --- |
| Codex | 8789 | `cursor-sdk-bridge-codex.service` | `~/.codex/cursor-sdk2api` |
| Claude Code | 8790 | `cursor-sdk-bridge-claude.service` | `~/.codex/cursor-sdk2api-claude` |
| Dashboard | 8791 | `cursor-sdk-bridge-dashboard.service` | 读取两个实例的元数据 |

Dashboard：`http://127.0.0.1:8791/`；远端机器需要端口转发，JSON 状态在 `/api/status`。
`running_version` 是进程运行的提交，`deployed_version` 是 `current` 指向的提交。
`needs_restart` 比较两者；修改 checkout 源码不会影响已部署进程，也不应触发重启提示。

## 发布与重启

从项目 checkout 操作，先验证并提交所需版本：

```bash
cursor-sdk-bridge deploy <commit> --install-only
cursor-sdk-bridge deploy <commit>
cursor-sdk-bridge restart claude
cursor-sdk-bridge restart codex
cursor-sdk-bridge restart dashboard
```

- `--install-only` 安装到 `~/.local/share/cursor-sdk-bridge/<commit>`，用于临时端口验收。
- 正式 deploy 切换 `current` 并注册、enable 三个 user units；它不启动、停止或重启服务。
- units 从固定提交和独立 venv 启动；部署失败保留旧版本，运行参数和 proxychains 路由见项目文档。
- 重启前立即核实 active、queued、unfinished/pending 和未结束连接；任一非零或无法核实就等待。
- CLI 与 dashboard 用 drain 关闭新请求入口，再检查未完成请求与连接；不要直接绕过门禁重启。
- 客户端断开不等于空闲：推理可能继续运行并落盘；按摘要去重的请求也可能仍在等待原任务。
- pending 工具等待客户端结果也属于进行中的工作，不得因为界面没有输出而重启。
- 重启完成须确认预期版本的 `/health` 恢复；失败检查 drain 是否恢复，按文档回退。
- dashboard 重启只影响页面；provider 配置与 Codex daemon 的重启是独立操作。

## 切换与恢复

Claude Code 用同一份备份识别当前设置，只有新会话使用目标 provider：

```bash
cursor-sdk-bridge switch claude cursor
cursor-sdk-bridge switch claude restore
```

保留用户已选的 Cursor 上下文、档位和速度；仅 1m 组合使用 `[1m]` 后缀，300k 组合不加。保留流空闲时限、关闭非流式回退和受管重试设置。
备份在 `~/.codex/cursor-fallback-state/claude-code.json`；含原 provider 凭据，禁止直接展示。

Codex 切换由用户在独立 SSH 终端执行；先结束远端任务并断开 Desktop 的该远程连接：

```bash
cursor-sdk-bridge switch codex cursor --restart-daemon
cursor-sdk-bridge switch codex restore --restart-daemon
```

agent 先把部署、预检和命令准备好，再请用户断开并执行；用户确认后检查 `cursor-sdk-bridge status`。
成功须确认 provider 等于本次目标、`runtime_matches_config: true`、无 pending transaction、目录路径不需升级，
并用新任务验收目标服务；恢复时 provider 应匹配备份。
已有对话保留原 provider；切回 OpenAI 时保留本工具新增的 `[model_providers.cursor]`，备份原有定义则还原它。
`Model provider cursor not found` 先查已加载配置是否包含旧线程所需定义，不改历史记录来绕过错误。
`model is not supported ... ChatGPT account` 先核对线程的 provider 与模型是否匹配；旧 OpenAI 线程选 Claude 不会改变 provider。
旧线程改回 OpenAI 模型；切换后重连 Desktop 并新建 Cursor 任务。不要据此删除 `auth.json`。
默认 provider 切换通过与 Desktop 重开旧线程、保留原模型须分别验证。
`catalog_path_upgrade_required: true` 表示旧 skill/旧部署目录仍被引用，按上面的 cursor 命令升级。
不要手改 `model_catalog_json`、删除状态文件或覆盖备份绕过冲突；保留 `auth.json` 与凭据存储设置。

## 排障与私有日志

收到 `Cursor SDK response failed (<标签>)` 或流中断转述后，agent 自己按时间查对应日志：

- Codex：`~/.codex/cursor-sdk2api/logs/requests.jsonl`。
- Claude Code：`~/.codex/cursor-sdk2api-claude/logs/requests.jsonl`。
- 展示必要元数据：标签、`duration_s`、`queue_s`、`prompt_bytes`、usage、运行版本；不粘贴整份私有文件。
- 结果账本 `results.sqlite3` 和 SDK 本地历史可能包含内容；不要当元数据日志输出或提交。

| 标签或现象 | 首先检查与处置 |
| --- | --- |
| `deadline_expired` / `request_timeout` | 对照实际时限、排队与耗时；继续观察事件，必要时降档或压缩历史 |
| `queue_timeout` / `queue_full` | 查排队和当前推理，等实例空闲，避免重复发起长请求 |
| `upstream_incomplete` / `upstream_error:<类名>` | 查上游失败元数据；认证失败先看 `credential_probe`，不要用付费推理试 key |
| `key_invalid` | 检查 SDK key 文件及认证有效性；不打印 key，不把所有 403 当 key 失效 |
| `invalid_model_output` | 先确认是否 legacy/reuse JSON 路径；保留标签证据并查协议解析 |
| `native_protocol_error` | 检查原生工具回调和事件转换，按证据定位；不要假装已完成 |
| `invalid_request` | 本地校验拒绝；查 `request_error` 和未完成工具批次，不当作 SDK 上游错误重试 |
| `model_mismatch` / `isolation_failed` | 停止新验收请求并排查模型、允许的工具和工作目录隔离 |
| `context_length_exceeded` / `prompt_too_large` | 请求未进 SDK；压缩历史或开新会话 |
| 界面空白或客户端断开 | 看事件、idle_s、pending 和账本状态；不能据此断定推理已停止 |

## 验收与真实用量

真实推理只用于已授权的对照实验、预检或验收；每次先说明预计调用数和大致耗时/用量。
`cursor-sdk-bridge probe --port 8789` 检查 namespace，`--messages --port 8790` 检查工具往返，
`--image --port 8789` 检查图片；通常各 1–2 次 SDK 输出，high 约 1–2 分钟，按实际记录 token。
首次切换也可能运行两次真实预检；幂等检查是否耗用推理以 CLI 结果为准，不反复重跑。
当前默认 `native`；`--mode legacy|reuse` 显式回退，实际模式以实例 `/health.mode` 核对。
需要回退时部署已验证提交，等空闲再重启；协议对照用临时端口与独立状态目录。
