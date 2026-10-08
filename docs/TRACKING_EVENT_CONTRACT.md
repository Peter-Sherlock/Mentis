# Mentis 统一追踪事件契约（T0 冻结草案）

版本：**v1.0-draft**（2026-09-29，P2.4-T0 冻结；T1 实现及验收修复见 §11，T2 见 §12，T3 见 §13，T4 见 §14—15，V1 见 §16，O1 见 §17，O2 见 §18，C1 见 §19，O2/C1 审查修正见 §20；修改须在进度记录中说明理由）｜性质：**已接入事后回放、实时旁观（--follow）与白名单导出（--export），经真实运行核验**｜上游计划：[交互可见性与 P2.4 递进计划](INTERACTION_VISIBILITY_PLAN.md) §3.1。

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

## 14. T4 实现附录（2026-09-29，真实使用核验段）

1. **新增 kind：`executor.confirmed`**（mentis）：人工核对后确认 unknown 尝试的结局时写入（summary 含核对依据"产物存在且可解析、无活跃写入者"，不声称执行器完成其全部计划步骤；`executor_status` 携带确认值）。理由：核对确认是计划 §2 问题 5 的关键依据，此前只存在于终端输出，事件流中缺失则"ERROR→确认→交付"过程无法从 trace 审计。
2. **真实运行验证记录与更正**：运行 `20260929_084347-5f82`（详见 [P2.4-T4 记录](progress/P2.4-T4.md)）——旁观 9 分 43 秒，初次 ERROR 收尾前 32 条事件已落盘，第 33 条为收尾事件；恢复后合计 36 条；`run.resumed` 中断区间 425s 在真实数据生效。`executor.completed` 因 T2 遗留崩溃缺陷未发出，台账显示"终态未取得"。**本次原始事件流没有 `executor.confirmed`**：确认事实保存在 `session.json`；该事件写入逻辑在真实运行之后加入，仅通过离线回归，不能据新代码宣称本次运行已记录它。

## 15. T4 负责人发现的交流可见性缺口与补修（2026-09-29）

负责人指出 `session.json` 和时间线都不能直接读到 Mentis 与 Codex 的交流。原因是确切委托虽已归档，Codex 的公开回复却仅以最多 160 字的摘要入事件；T4 真实运行最后一条回复被截去临时目录删除受审批拦截的说明。

- 新运行在 `item.completed/agent_message` 时把**公开回复全文**原样保存于本地私有 `trace/messages/<attempt>-<sha256>.md`，事件 `refs` 以 `role=executor_message` 和 SHA-256 关联；归档失败计入追踪写失败，不改变执行器终态。内部推理与原生命令输出不归档到此处。
- `mentis conversation --run <目录>` 只读组合确切 prompt、可核验的公开回复、动作摘要和 Mentis 复检/收尾；`collab` 与 `guide-revise` 新运行另生成 `conversation.md` 并从报告“看依据”指向。此视图是本地私有材料，不进入 `trace --export` 白名单；不等同完整原生聊天记录。
- 旧事件无全文引用时明确显示“仅存事件摘要，可能已截断”，不自动搜索私有 rollout，不回填原事件。T4 真实运行的原生 rollout 中可核对的五条对外回复另作一次性只读整理，来源和缺口见 `docs/progress/P2.4-T4.md`。

## 16. V1 实现附录（2026-10-02，评估计划先行）

按 ADR-008 第 1、4 条接入评估计划（EvaluationPlan）时序，新增两类 `source=mentis` 事件与一个 refs role：

1. **新增 kind：`evaluation_plan.recorded`**：运行内首次生成评估计划并在**首次委托前**落盘时写入（`refs` 含 `role=plan` 的计划文件与 SHA-256；summary 记维度数、阻塞条数与模型实例化状态）。理由：ADR-008 要求评估标准先于委托冻结且可审计——"标准在看到产物前已存在"必须能从事件时间序核对，此前的 `goal.recorded`/`policy.set` 只记录目标与代决范围，不含判定标准。
2. **新增 kind：`evaluation_plan.revised`**：brief 改版/目标变化等有原因的改版落盘新版本时写入（summary 记版本迁移、原因与受影响条目；refs 指向新版本文件）。旧版本文件不改写；旧尝试的评审记录以 `evaluation_plan: {version, sha256}` 绑定其当时版本。理由：计划修订必须"只在有原因时发生"且可审计，不改写历史。
3. **新增 refs role：`plan`**（§2 role 枚举追加）：指向 `evaluation-plan/plan-v<N>.json` 及其 SHA-256。
4. **先落盘后委托的强化**：`delegation.prepared` 事件 refs 现在同时含 `prompt` 与 `plan` 两个引用；计划落盘失败抛 `PlanPersistenceError`，该次委托不发起（执行器零调用），与 prompt 落盘失败同一纪律。委托文本尾部追加"本轮验收项"段（来自计划阻塞条目），归档哈希=实际发送文本。
5. 不变量补充：评估计划事件与委托事件的先后关系是"标准先于执行"的证据；回放/旁观按既有 seq 顺序呈现，不需要新解析逻辑。

## 17. O1 实现附录（2026-10-08，统一记录与可读回放）

按 [可观测性补强计划](OBSERVABILITY_PLAN.md) §3—4 实施。**不新增事件 kind、不改动 §2 事件字段**——`mentis-trace/1` 事件结构保持不变；以下为记录/视图层的增补：

1. **三入口共用执行器事件采集**（`mentis.trace.describe_executor_event`，collab / guide-revise / eval-run 同一实现）：`agent_message`（item.completed）全文经 `archive_executor_message` 归档、`refs` 以 `role=executor_message` + SHA-256 引用；事件摘要**首尾保留**（`bounded_text`：≤160 字，超出时头部+「…（中略）…」+尾部 40 字）——中文长消息的尾部不再因截断丢失（T4 缺口）。归档失败时摘要显式标注「全文归档失败：追踪完整性缺口」，`write_failures` 由归档器计入 manifest。`command_execution` 摘要为「命令 …；输出未采集（状态）」——动作状态明确（completed/failed/in_progress，缺失记 unknown），未采集输出为固定缺口标记，状态后缀仍居摘要末位（回放子项小结的解析约定不变）。`reasoning` 仍只记类型。采集回调自身异常降级为「事件采集异常（类型）；原文未入流」，由调用方 `note_capture_failure` 计入完整性——采集失败不静默吞掉。
2. **模型调用 sidecar：`trace/model-calls.jsonl`**（追加式 JSONL，格式 `mentis-model-call/1`；非事件流，不入 §2 白名单导出）：O1 起的新运行在每次模型调用关联点由调用方写入——request_id、model、host、created_at、latency_ms、ok、usage、error（有界）、mock、http_attempted、prompt_sha256（输入指纹；**输入全文不保存**，`input_text_archived=false` 显式为假）、image_refs（名称;sha256 前 12 位）、stage（调用方声明的用途阶段，如 plan_instantiation / diagnosis / blind_review；未设置如实写 unrecorded）、output_refs（该调用产出的已落盘文件引用）。写入失败计 `write_failures`（events_complete 随之为 false）。O1 之前的运行无此文件——投影按现存材料（如 runner-state.model_requests）确定性读取并标缺口，不做猜测性关联。
3. **conversation 视图补齐 Mentis 事件**（`mentis conversation --run`，本地私有层，不进导出白名单）：在既有「确切委托 / 执行器公开消息 / 动作摘要」之外，按 seq 真实顺序呈现 `evaluation_plan.recorded/.revised`（含计划文件引用）、`observation.structure/render`、`observation.diagnosis`（含诊断全文引用）、`decision.routed`、`decision.requested/answered` 与既有 `review.recorded` 等。零委托运行明示「0 次委托……不为展示交流补造执行器消息」，不把没有执行器的流程伪装成对话。判定值不在事件摘要中时，从同目录状态文件（runner-state.json / state.json）确定性读取并标注来源与「原判定不改写」。
4. **只读运行投影**（`mentis/run_projection.py`，格式 `mentis-run-projection/1`；CLI `mentis replay --run [--out]`，O3 页面共用同一投影）：确定性读取现存材料生成决策记录（模型提案 → 程序/用户处置、计划维度、映射方式与得分、理由、后续动作、复检）、模型调用（含上述 sidecar；历史运行降级标 unrecorded）、执行器委托台账、版本与证据（计划/产物/截图/测量，哈希现在计算并与事件 refs 比对：一致 verified、不一致或缺失或**越界** unverified，越界引用不读取）。记账分别展示模型调用数 / HTTP 实发数 / 执行器委托数；未知用量不写 0，不虚构货币成本。投影只读运行目录、不改写业务判定、输出不写入运行目录；「unrecorded / unverified」是展示标签，不构成新的业务判定。


## 18. O2 实现附录（2026-10-08，运行阶段、等待与活动状态）

按 [可观测性补强计划](OBSERVABILITY_PLAN.md) §4/O2 实施。**仍不新增 `mentis-trace/1` 事件 kind、不改动 §2 事件字段**；以下为两个版本化 sidecar、代次语义与共用状态投影：

1. **阶段 sidecar：`trace/stages.jsonl`**（追加式 JSONL，格式 `mentis-run-stage/1`）：O2 起的新运行由三入口（collab / guide-revise / eval-run）在流程边界写入阶段起止——stage ∈ {plan, build, observe, diagnose, adjudicate, revise, reverify, waiting_user, finalize}（只记录实际发生的阶段），phase=start/end，含 instance（同名阶段内序号）、generation、attempt_id、brief_version、plan 引用、detail/reason/blocking（waiting_user）；end 记 status（执行器真实终态/ok/interrupted/error/unknown）与 duration_s（单调时钟差；时钟异常记 null，不生成负数）。未关闭阶段在下一阶段开始、恢复对账或收尾时以 `interrupted` 如实关闭，不冒充正常结束；**未决的等待用户阶段保持打开**（等待跨进程持续，由回答/续办关闭）。损坏行原样保留为 `corrupt_line`。写入失败计 `write_failures`。
2. **生产者活动信号：`trace/heartbeat.json`**（原子重写：临时文件+replace，单写者；格式 `mentis-heartbeat/1`）：由生产者进程内的后台线程周期刷新（默认 15s，有效期默认 45s），记录 run_id、generation（无事件流时为 null=不可导出）、producer {pid, role}、time_utc、ttl_s、source。信号**独立于旁观器**；缺失/过期/损坏/旧代次只表示「无法确认生产者活跃」或「不代表当前生产者」，不得推断任务失败、不解锁、不自动重发。停止信号不写「已停止」记录——文件保持最后状态并自然过期。写入失败计 `write_failures`。活动不等于有进展；子进程活动未采集时如实说明。
3. **运行代次**：generation = 1 + `run.resumed` 事件数（从既有事件确定性推导；无事件流=unrecorded）。恢复/续办产生新代次：旧代次的 run.finalized、manifest 与活动信号不得覆盖新代次状态，读取方按代次匹配判定活动信号归属。
4. **`--follow` 代次语义**（T2 兼容扩展）：重放/跟随中 `run.resumed` 清除「当前代次已收尾」标记——旧收尾+旧 manifest 不再使恢复中的运行被误报完成；「manifest 静默收尾」兜底只适用于**从未恢复过**的运行（T2 原语义保持），恢复后的静默只表示暂无新记录（活动信号类别变化时才汇报，不刷屏）。附着时输出一行当前代次与活动摘要。旁观器停止/Ctrl+C 只结束读取，不影响生产者。
5. **最后可靠状态投影**（`mentis/run_projection.py: load_run_state`，格式 `mentis-run-state/1`；CLI `mentis status --run`、`--follow` 与本地页共用同一读取函数）：分列当前阶段（含进行中/最近结束与耗时）、未决等待（原因/开始时刻/只阻塞依赖动作；通知不阻塞）、生产者活动（类别 fresh/stale/corrupt/missing/old_generation/unclear + 来源与有效期）、最近业务事件、最后采集时刻与**读取方时刻分列**、收尾归属（当前代次 vs 旧代次）、预算（上限/已用/剩余——缺任一项不给剩余，未知不填 0）与模型调用/HTTP 实发/执行器委托分账。只读、可再生成；历史运行如实显示「未采集」，不回填，不改变原业务判定。
6. 页面（`mentis view`）新增「当前状态」面板：普通语言展示上述状态；刷新只表示读取成功并显示旁观器刷新时刻，不作为生产者活跃证据。活动信号、阶段记录与状态投影均为本地私有层材料，不入 `--export` 白名单。

## 19. C1 实现附录（2026-10-08，最小任务专业配置冻结）

按 [本地产品大纲](LOCAL_PRODUCT_ROADMAP.md) §3 实施。`mentis-trace/1` 事件结构与既有 kind 不变；新增一类 mentis 事件与两个版本化运行内文件（本地私有层，不入 `--export` 白名单）：

1. **新增 kind（`source=mentis`）**：
   - `professional_config.recorded`：任务专业配置在**首次使用前**落盘时写入（refs 含 `role=professional_config` 的配置文件与 SHA-256；summary 记资源 id@哈希前 12 位、复核状态与能力范围标签）。理由：大纲 §3「配置先落盘；必要配置或资源无法核验时不声称已绑定」必须能从事件时序核对——配置先于计划与委托存在。
   - `professional_config.revised`：目标/brief 变化等有原因的改版落盘新版本时写入（summary 记原因、版本迁移、「受影响旧计划/委托/评审以新版本为准，旧版本文件不改写」）。授权撤回等用户决定继续走既有 `authorization.*` 事件，本事件只记录配置层面的改版。
   - `professional_config.drift`：注册表资源内容与任务快照不同时写入（每个资源至多一次；summary 记「继续使用快照，不静默换内容」）。理由：大纲 §3 第 3 条——同任务不得无记录地换用不同内容。
   - `professional_config.blocked`：配置/资源无法核验（文件缺失、哈希不符、绑定记录缺失）时写入；依赖配置的计划与委托不发起（`ConfigPersistenceError`，与 §16 第 4 条 PlanPersistenceError 同一 fail-closed 纪律）。
2. **新增 refs role：`professional_config`**（§2 role 枚举追加）。
3. **配置文件：`professional-config/config-v<N>.json`**（格式 `mentis-professional-config/1`；N 从 1 递增，旧版本不改写）：记录六类最小信息——职责（需求/方案/指导/观察/验收与明确不支持范围；程序预置并标注来源，不授予专业资格）、专业依据（资源 id、版本、**内容 SHA-256**、适用理由、来源、复核状态；资源内容快照存 `professional-config/resources/`，保证可复现）、任务条件（brief 版本/目标、策略引用、授权清单、用户来源、预算引用）、能力范围（有资料/可执行/可观察/已验证范围分列，来自 CapabilityContext 四分）、评价依据说明。**文件不含自身哈希**（避免循环哈希）；哈希由 bindings 记录与事件 refs 承载。选择只做一次（决策需要取计划层与诊断层的并集），阶段取用同一配置的相关部分。
4. **绑定记录：`professional-config/bindings.jsonl`**（追加式 JSONL，格式 `mentis-config-binding/1`）：`bind` 行（配置版本/哈希↔brief 版本↔原因）与 `bind_plan` 行（配置版本↔计划版本/计划 SHA-256）——配置、brief/策略与计划的关联主键；同版计划不重复记。
5. **调用关联字段（可选，不回填）**：`trace/model-calls.jsonl` 行新增 `config_version` / `config_sha256` / `stage_responsibility`（仅调用方提供时写入；O1—O2 历史行与未走配置链的运行不留这些字段）；`trace/stages.jsonl` start 行新增 `config` {version, sha256}（同理）。历史运行不回填，投影按字段是否存在如实展示。
6. **盲评隔离不变**：盲比（`run_blind_review`）输入仍然只有评估计划标准与前后观察；配置仅以关联信息（版本/哈希/本阶段职责）写入结果 JSON 的 `professional_config` 字段，不进入提示词，不携带资源内容或诊断结论。
7. **共用只读视图**：`load_professional_view`（run_projection）供投影 Markdown、`mentis status`、conversation 与本地页「专业配置」面板共用；`recorded`（哈希核验通过）/`unverified`（存在但核验不过——展示标签，不是业务判定）/`unrecorded`（C1 前运行或未走配置链，如 eval B-1 基线臂——如实显示未采集，不回填「当时身份」）。读取前后源材料字节不变。

## 20. O2/C1 审查修正附录（2026-10-08，四项缺陷修复）

按 [独立审查](../runtime/reviews/p2.4-o2-c1-audit/REVIEW.md)（HEAD c555497、版本 0.22.0 工作树）实施修正；`mentis-trace/1` 事件结构不变，新增一个可选字段并澄清关联语义：

1. **新增可选字段 `interaction_id`（§2 事件可选字段）**：`decision.requested` / `decision.answered` / `decision.received` 及 `waiting_user` 阶段 start 行在产生时写入稳定的决定关联标识（session 的 `ix-*` / eval 的 `IX-E*`）；未提供时不留字段，历史事件不回填。
2. **等待关联语义（修正"已回答仍待决"）**：状态投影按标识把答复与请求配对——同一标识的 `decision.answered`/`decision.received` 关闭对应等待，其余未回答保留；`waiting_user` 阶段行与事件行不重复计数（同一决定只列一条）。历史事件无该字段时，按程序生成摘要中的 `ix-*` 确定性解析（大小写不敏感）；仍无法关联的回答记录只记缺口（"无法关联到具体决定请求……待决事项按未回答保留，不臆造对应"），不清空全部等待。回答产生时同步按标识关闭对应的 waiting_user 阶段（`finish_waiting(interaction_id=…)`；无匹配时不误关其他未决等待，退回历史"最近未匹配"仅用于无标识场景）。
3. **资源快照按内容哈希寻址（修正"改版覆盖旧快照"）**：`professional-config/resources/<内容哈希前16位>-<资源id>.<ext>`——内容不同→不同文件，配置改版不覆盖旧版本引用的快照；内容相同→同一文件幂等复用（不重写字节）。旧布局（按文件名）的已有可核验配置保持可读：恢复/视图一律读取配置 `grounds[].snapshot` 记录路径，不按文件名猜目录，读取不改写历史文件。
4. **同实例复用同一核验（修正"缓存复用跳过完整性检查"）**：`ensure()` 在内存缓存路径上每次使用前重验配置字节（对冻结时哈希与绑定记录）、绑定记录存在性与资源快照哈希，任一不可核验抛 `ConfigPersistenceError`——与冷恢复同一规则；依赖配置的计划/诊断/委托/复检在入口失败，相关执行器委托为 0 次。
5. **首次恢复即比较当前条件（修正"首次恢复忽略新目标"）**：磁盘读回配置后立即与当前 brief/目标比较，已变化则在第一次使用前完成有原因改版（`professional_config.revised`）并使后续阶段/调用/委托引用新版本；不依赖第二次调用纠正。
6. 上述语义的行为回归见 `tests/test_o2c1_fix.py`（13 项，先复现旧行为再修复）；修复前后复现材料在 `runtime/observability/o2c1-fix/repro-before.json` / `repro-after.json`。
