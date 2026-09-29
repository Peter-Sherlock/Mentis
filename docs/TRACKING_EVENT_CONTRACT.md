# Mentis 统一追踪事件契约（T0 冻结草案）

版本：**v1.0-draft**（2026-09-29，P2.4-T0 冻结；T1 实现及验收修复见 §11，T2 见 §12，T3 见 §13；修改须在进度记录中说明理由）｜性质：**已接入事后回放、实时旁观（--follow）与白名单导出（--export）**｜上游计划：[交互可见性与 P2.4 递进计划](INTERACTION_VISIBILITY_PLAN.md) §3.1。

本契约基于本机实测材料制定：Codex CLI `0.158.0`（`codex --version` 实测）、既有真实 rollout 文件（`~/.codex/sessions/2026/09/28/rollout-2026-09-28T09-28-26-01a0e833-….jsonl`，29 行，字段逐一枚举）、既有真实运行目录（[P2.4-T0 记录](progress/P2.4-T0.md) §3 的工作样例）。

## 1. 目的与不变量

一次运行回答五个问题（计划 §2）：依据哪版目标作了什么选择、发给 Codex 的确切指令、Codex 报告了哪些可观察步骤、作品如何版本演变及为何接受/修订/暂停、中断时最后的可靠状态。追踪器**只观察**：不回灌答案/案例材料给受测 Agent，不改变权限与执行，不声称展示模型内部推理。

- **写入者唯一**：现有运行流程（collab / guide-revise / 后续 field-start 接入）；旁观器与导出器只读。
- **先落盘后委托**：确切委托文本在**实际调用执行器前**写入 `trace/prompts/<attempt_id>.md` 并记录 SHA-256；落盘失败则不发起该次委托。
- **写失败不粉饰**：事件写入失败时 `manifest.integrity.events_complete = false`；委托期间失败保留执行器真实状态，完整性标未知，**禁止把缺证据的运行判为可审计通过**。
- **缺资料不回填**：旧运行未采集的信息标 `unrecorded`，永不补写伪事件。

## 2. 事件记录：`trace/events.jsonl`

追加式 JSONL，每行一个事件，写入即 flush；读取端容忍末尾半行。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | string | 固定 `mentis-trace/1` |
| `run_id` | string | 运行 ID（collab/guidance/field 现有 run_id） |
| `seq` | int | 运行内单调递增，从 1 开始；不因续接重置 |
| `time_utc` | string | ISO-8601 UTC（写入时刻；不是执行器事件的发生时刻——后者原生流不含可信时钟，如实缺失） |
| `source` | enum | `user` / `mentis` / `executor` / `observer` |
| `kind` | enum | 见 §3 |
| `attempt_id` | string\|null | 关联尝试（无尝试的事件为 null） |
| `brief_version` | int\|null | 事件生效时的 brief 版本（取自 session.attempts[].brief_version 或当前版本） |
| `native_thread_id` | string\|null | Codex 原生 thread（executor 事件与委托事件关联用） |
| `status` | enum | `ok` / `failed` / `unknown` / `pending` / `unexplained`（未知原生事件类型专用） |
| `summary` | string | 有界可读摘要（≤500 字符；截断必须以「…（已截断）」结尾） |
| `refs` | array | `[{path, sha256, role}]`；role ∈ `prompt` / `artifact` / `observation` / `review` / `diagnosis` / `native_log`。**哈希是关联主键，时间先后不是因果证明** |
| `executor_status` | string（可选） | `executor.completed` 的原始终态值（例如 completed/failed/unknown），供续接重建委托台账 |
| `usage` | object（可选） | `executor.completed` 的结构化用量；无终态时保持未知，不从摘要猜测 |

## 3. 事件类别（kind 枚举，首版冻结）

`source=mentis`：`goal.recorded`、`brief.updated`、`policy.set`、`authorization.granted/revoked`、`decision.requested/answered`（含阻塞动作）、`decision.routed`（自主分流及理由引用）、`delegation.prepared`（prompt 已落盘，refs 含 prompt 文件+哈希）、`review.recorded`（结论+依据引用）、`revision.recorded`、`run.finalized`。
`source=executor`：`executor.started`、`executor.item`（item_type/item_status/有界摘要）、`executor.completed`（含用量）、`executor.failed`、`executor.unexplained`（未知原生类型，保留原文类型名于 summary）。
`source=observer`：`observation.structure`、`observation.render`（测量+截图，refs 带哈希）。
`source=user`：`user.feedback`（仅档案引用，不在事件内复制原话；见 §6）。

## 4. 关联规则

- **attempt 是锚**：每次委托前 `session.dispatch` 已生成 attempt（P1.2 契约），`delegation.prepared` 与其后全部 executor/observer 事件携带该 `attempt_id`。
- **brief 版本**：取尝试绑定的 `brief_version`；非委托事件取当前版本。旧版本尝试不可接受（StaleVersion）——回放据此展示"哪版目标支撑哪次委托"。
- **thread 关联**：`delegation.prepared` 时 thread 未知（null）；`executor.started` 起携带；续接（resume）创建**新 attempt 段**并携带同一 `native_thread_id`，旧段与原失败判定不覆盖。
- **产物版本**：不单独建事件流——`refs` 指向带 SHA-256 的文件（index.html、v1/v2、截图、测量 JSON）。Artifact 哈希沿用 `session.add_artifact` 的既有计算（`session.py:_sha256_of_file`，已实测）。

## 5. `trace/manifest.json`

```json
{
  "contract_version": "mentis-trace/1",
  "run_id": "…", "created_at_utc": "…", "mentis_version": "0.8.0",
  "executor": {"name": "codex-cli", "cli_version": "0.158.0", "model": "（CLI 默认）"},
  "delegations": [
    {"attempt_id": "attempt-2", "brief_version": 1, "native_thread_id": "01a0e833-…",
     "prompt_file": "trace/prompts/attempt-2.md", "prompt_sha256": "…",
     "terminal_status": "completed", "usage": {"input_tokens": 800006}}
  ],
  "integrity": {"events_written": 42, "events_complete": true, "write_failures": 0,
                "note": "write_failures>0 或 events_complete=false 时运行不得称完整追踪"}
}
```

## 6. 隐私与导出边界（三层）

1. **默认可读层**（入事件流）：Mentis 决策与理由引用、确切委托指令（prompt 文件本身在运行目录，本地留存）、executor 事件的有界摘要、终态/错误、观察与复检结论、版本/diff 入口。每项带来源/时间/attempt。
2. **私有诊断层**（不自动显示、不入 Git）：Codex 原生 stdout 原始行与 rollout 文件引用（rollout 本就由 Codex 写在 `~/.codex/sessions/`，仓库外；Mentis 至多记录其路径引用）。原生输出可能含用户资料或命令输出，**不宣称简单正则可完全脱敏**。
3. **导出层**：字段白名单（§2 全部字段 + manifest），**排除**：用户逐字反馈、工作区全文、原生原始行、一切密钥字段。用户逐字反馈只以 `user.feedback` 事件+档案路径引用出现，导出时按用户另行决定。

## 7. 状态映射（冻结）

| 执行器事实 | Adapter 判定 | trace status |
| --- | --- | --- |
| 退出码 0 + `turn.completed` + 无失败事件 | COMPLETED | `ok` |
| 退出码 0 + 有最终文本 + 无终态事件 | UNKNOWN（需产物核对） | `unknown` |
| 超时 / 进程死亡 / JSONL 损坏 | FAILED 或 UNKNOWN（按 P3.1 纪律） | `failed` / `unknown` |
| 收到进展事件（item.*） | —— | **不是成功**；仅 `executor.item` |
| 核对后续接 | 新 attempt 段 + 同 thread | 各段保留原判定 |
| 配额类错误（如 ChatGPT 限额） | FAILED，error 原文入 summary（有界） | `failed` |

## 8. Codex 原生事件映射（本机 0.158.0 实测）

Adapter 消费的 `codex exec --json` **stdout 流**事件（P3.1 起已解析）：`thread.started{thread_id}`、`turn.started`、`item.started{item{type,status}}`、`item.completed{item{type,status,text?}}`、`turn.completed{usage}`、`turn.failed`、`error{message}`。实测 item.type 至少含：`agent_message`、`command_execution`、`reasoning`、`file_change`、`image_view`（源自既有 rollout 枚举）。**映射规则：逐类型映射到 §3 的 `executor.*`；未见过的类型/字段保留为 `executor.unexplained`，summary 记原生类型名，不静默丢弃。**

Codex 另自留 rollout 会话文件（顶层 `session_meta`/`event_msg`/`response_item`/`token_usage_record`/`turn_context`/`world_state`，含逐命令输出与加密推理内容）——属私有诊断层素材，首版不解析、不导入，仅在 manifest 以路径引用（存在性可核对）。**注**：rollout 的时间戳（`timestamp`/`started_at_ms`）比 stdout 流更完整；T1 若需要事件级时刻，可在此注明"来自 rollout，私有层"，但仍不把 rollout 内容并入默认可读层。

## 9. 旧运行缺失的表示

历史运行（2026-09-28 的 collab/guidance 运行）没有 `trace/`，**不追溯生成**。回放器对旧运行展示：现有材料（指令/事件 JSON/报告/产物）+ 每项的 `unrecorded` 缺口清单（如"事件无时间戳""collab 未存确切 prompt 文本""round1 用量因失败未取得"）。缺口清单是 T0 工作样例的固定输出（见 [P2.4-T0 记录](progress/P2.4-T0.md) §3），T1 的 `mentis trace --run` 必须原样展示缺口，不得以摘要冒充完整对话。

## 10. 性能与有界性（首版）

Mentis 自身延迟目标：收到一行完整 CLI 事件后 2 秒内落盘可读（正常本地磁盘）；不计执行器首事件等待。摘要与命令输出有界截断并标注；事件流内存占用常数级。观察到的"用户目标与实际委托不一致"登记为产品缺陷，不在评测中偷偷改 prompt 或评分标准。

## 11. T1 实现附录（2026-09-29，按约说明增改）

实现于 P2.4-T1（`mentis/trace.py`），对本草案的增改与解释：

1. **新增 kind**：`run.resumed`（mentis；恢复续接，事件 seq 从已有流续排）与 `observation.diagnosis`（mentis；模型诊断候选落盘引用）——原 §3 枚举未列，属实现期补充。
2. **`executor.completed` 语义**：指"执行器调用结束"，`status` 字段携带 ok/failed/unknown——未知终态不是 `executor.failed`。
3. **`delegation.prepared` 的 `status=failed`**：表示 prompt 落盘失败、**该次委托未发起**（调用方已把尝试记为 failed，执行器未被调用）。
4. **prompt 归档字节一致性**：`trace/prompts/<attempt_id>.md` 以字节写入（不经 newline 转换），使文件 SHA-256 与实际发送文本逐字节一致（Windows 下 `write_text` 的 \n→\r\n 转换会破坏一致性，已被测试捕获并修复）。
5. **半行容错**：读端跳过损坏行并原样标记 `corrupt_line`；写入器续排 seq 时对半行用正则提取已写出的 seq，不复用该号。
6. **manifest 从事件重建**：恢复场景下新写入器从既有 `delegation.prepared` 事件重建委托台账，保证 manifest 覆盖全运行的委托。

### T1 验收修复（2026-09-29）

7. **半行隔离与完整性重算**：续写前若文件末尾不是换行，先补换行隔离坏行；新事件保持可读。`finalize` 从磁盘重扫坏行、seq 连续性、契约与 run_id；存在异常或历史写失败时 `events_complete=false`。`events_written` 是可解析事件数，`seq_max` 是已占用最大序号，两者不再混用。
8. **恢复终态**：新 `executor.completed` 事件记录可选的 `executor_status`、结构化 `usage`；续接从事件重建。旧 T1 事件缺结构化用量时，仅在同 run_id、同契约、同 prompt 哈希下继承旧 manifest 台账；找不到来源则保持未知。
9. **回放现核**：`mentis trace --run` 除显示落盘 manifest，还重算当前事件流和 `refs` 文件 SHA-256。引用缺失、内容变化或事件流异常会显示“当前核验：未通过”；历史无 trace 的运行继续只读列缺口，不回填。
10. **指导决策链**：guide-revise 的用户请求、路由、自主决定、用户回答及 brief 更新写入对应 `decision.*` / `brief.updated` 事件。回答原文仍在会话记录，事件只记交互 ID 与版本。

## 12. T2 实现附录（2026-09-29）

1. **executor.started / executor.item**：T2 起逐行回调即时落盘（不再等终态）——`executor.started` 仅首事件（summary 含首事件延迟秒数），其后每条原生事件一条 `executor.item`（命令/最终消息有界摘要；推理条目只记类型）。`time_utc` 因此具有"到达/落盘时刻"含义。
2. **首事件延迟**：进程启动→首个非空 stdout 行，存于 `CodexTaskResult.first_event_latency_s` 并随 `executor.started` 展示；真实流实测 2.393s（CLI 0.158.0，见 P2.4-T2 记录）。
3. **follow 终止语义**：`run.finalized` 事件 + manifest 为正常收尾；manifest 存在但事件流连续静默（无 run.finalized 的写入器/旧探针）也按收尾结束——两者都以 manifest 为准，旁观器永不写事件。

## 13. T3 实现附录（2026-09-29，故障与隐私加固）

1. **敏感字段规则**（`mentis.trace.redact_text`，写入 `emit` 时对 summary 生效）：已知字段名（`api_key`/`token`/`secret`/`password`/`credential`/`authorization`/密码/密钥/令牌/凭证等）的 `key=value`、`key: value`、`"key": "value"` 赋值与 `Bearer <令牌>` → 值替换为 `⟨已隐去⟩`，字段名保留。**明确不隐去**：用量计数字段（`input_tokens` 等以 `tokens`/`_count` 结尾）——用量是契约要求的数据。**边界如实**：只对已知字段名做确定性匹配，不承诺识别任意秘密；prompt 归档（`trace/prompts/`）**不经此函数**——哈希必须等于实际发送文本，prompt 文件本就在本地私有层。
2. **导出白名单**（`mentis trace --run <目录> --export [路径]`，`mentis/trace_export.py`，格式 `mentis-trace-export/1`）：事件仅保留 §2 字段（附加字段防御性剔除）；manifest 仅保留版本/执行器/委托台账（只含 `prompt_sha256` 不含本地路径）/integrity/outcome；refs 只留 `role`+文件名+哈希。排除：用户逐字反馈、工作区全文、prompt 原文、执行器原生原始行、密钥值。导出只读运行目录，不产生事件。
3. **旧记录兼容（导出）**：无 `trace/` 的历史运行导出 `mode=historical`——兼容说明 + 现存材料视图（`render_historical`）+ unrecorded 缺口；不回填、不生成 trace/。
4. **异常收尾语义**（collab / guidance 的 `run`/`resume`/`reverify`，T3 起）：意外异常时——①仍处于 `dispatched`/`running` 的尝试改记 **unknown**（无执行器终态，不判失败、不假完成、不自动重发）；②写 `run.finalized`（status=`failed`，summary 含异常类型与原文有界）；③状态落盘为 **ERROR** 并正常 finalize manifest；④既有执行器终态与事件不覆盖、不重复判定；⑤收尾完成后向调用方重抛（CLI 层打印短消息而非裸 traceback）。**含恢复路径**：从 state.json 恢复的对象同样适用（并修复 collab 恢复对象缺 `_finalized_emitted` 导致恢复后收尾崩溃的缺陷）。
5. **现场会话异常**（field-start/field-feedback）：会话按 `SESSION_ERROR` 收尾，usage-record 明确标注"记录不完整（半份日志如实保留，不补写未发生事件）"，并保留 session-log.txt 日志入口；关联运行目录的 ERROR 落盘不受影响。
6. **多 attempt 连续时间线**：回放新增**委托台账**（每次委托的 attempt→终态、thread、prompt 哈希，来自 manifest）；`run.resumed` 事件标注**中断区间**（与上一事件的间隔秒数，"期间无记录，不回填"）。续接新 attempt 段与同一原生 thread 关联、原判定不覆盖的语义不变（§4/§7），现在在回放中显式可见。
7. **执行器原生错误保留**：`error`/`turn.failed` 的原生 message 有界存入 `CodexTaskResult.native_error` 并拼入 FAILED 判定文本——配额类错误的原文可进入 `executor.completed` summary（§7 的"错误原文入 summary"由此落实），供事后回放核对。
