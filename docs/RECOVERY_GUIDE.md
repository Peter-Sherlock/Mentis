# Mentis 恢复指引（P3.3）

> v1.0（2026-10-09）。适用版本 0.23.0+（含 P3.3 缺口修复）。本文只陈述有真实运行或回归证据的行为；
> 证据链接见每节末尾。**中断不等于回滚；不是所有中断都能自动恢复——每一步都需要按本指引核对后推进。**

## 0. 三个状态要先分清

| 状态 | 含义 | 能否继续推进 |
| --- | --- | --- |
| 中断等待（UNKNOWN / running 孤儿 / PENDING） | 有未核对的外部执行或未决请求；等待本身不是失败 | 可以：核对后推进（unknown→confirm；pending→answer） |
| 已确认停止（WITHDRAWN） | 你撤回了授权：后续委托一律拒绝 | 不能：新委托被 UnauthorizedAction 拒绝；已产出文件保留待核对 |
| 终态（DELIVERED / NOT_DELIVERED / FAILED） | 本轮已有结论 | 反馈走修订；NOT_DELIVERED 不自动重试 |

## 1. 进程中断（Mentis 进程被杀 / 崩溃 / 停机）

**会发生什么（S1 真实验证）：**
- 委托发起瞬间任务已落盘（P3.3 修复）：尝试以 running 状态进入 `state.sqlite3`，
  即使适配器调用还在阻塞中。中断后它不会消失。
- 执行器子进程**可能**成为孤儿继续写入（Windows 不随父进程退出）——它写完的文件是真实产物。
- 该工作区的写入锁保留：任何新委托都会被 `ExecutorBusy` 拒绝（不会盲目重发）。

**恢复步骤：**
1. 找到运行目录 `runtime/collab/<运行ID>/`，确认 `state.json` 与 `state.sqlite3` 存在。
2. 确认没有执行器还在写：
   `powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'codex*' -and $_.CommandLine -notmatch 'app-server' -and $_.CommandLine -notmatch 'computer-use' } | Select ProcessId,CreationDate"`
   （仍有 codex 进程晚于运行开始 → 等它退出；不要杀它来"解锁"——先核对它的产物。）
3. 核对工作区：`runtime/collab/<运行ID>/workspace/index.html` 是否存在、能否打开。
4. 新进程核对确认（不需要重启任何常驻服务）：
   ```
   py -3.14 -X utf8 -B -m mentis collab-resume --state runtime/collab/<运行ID>/state.json --confirm-outcome completed
   ```
   系统会：从 SQLite 恢复（进入时状态=中断前最后落盘状态）→ 把 running 孤儿尝试按
   unknown 纳入核对（`attempt.reconciled` 事件留痕）→ 释放锁 → 按当前检查标准收尾。
   **诚实规则：确认 completed 只表示"产物存在且可解析、无活跃写入者"，不声称执行器完成了全部计划。**
   产物不完整时收尾仍会 NOT_DELIVERED（S2 真实出现过）。
5. 若你在第 4 步前改了目标（brief 升版）：**改目标本身会先被写锁拦下**——旧 unknown
   未核对前不允许任何新委托（S5 真实：ExecutorBusy）。先完成核对，再改目标。
   旧目标的尝试事后即使被晚到确认，也**不会被接受**、不复活旧目标（S5 真实：
   确认后按当前检查标准收尾，NOT_DELIVERED；版本门禁 StaleVersion 由回归覆盖）。

**续接凭据（需要原生线程续接时）：** 执行器线程 ID 在 `trace/events.jsonl` 的
`executor.started` 事件 `native_thread_id` 字段（P3.3 起首事件即记录，不等委托返回）。
带反馈的恢复（`--feedback`）会自动用它走 `codex exec resume`（S3 真实验证：被杀线程可续接）。

## 2. 提交超时（unknown）

- 适配器在共享截止时间杀整棵执行器进程树，状态记 **unknown、锁保留**；事件流留
  "禁止自动重试"。S2 真实记录：50.8s 超时 → unknown → 新进程核对。
- 恢复：同 §1 第 4 步（`--confirm-outcome`）。核对后系统按当前检查标准决定：
  缺什么走有界修复（`--max-repairs` 预算内），预算用尽即有界停止，**不自动重发**。
- 修复轮自身也可能超时（S2 真实出现）：再次 unknown + 锁保留，等下一轮核对；
  修复预算**跨核对轮不重置**（P1.4 回归 + S2 真实）。

## 3. 执行器失败（FAILED）

- 执行器进程被杀/报错 → `FAILED（进程退出码 N）` 如实落盘，运行不粉饰（S3 真实：18.5s 击杀 → FAILED）。
- 恢复：核对工作区后 `--feedback "继续完成…"` → 按新 brief 版本走原生线程续接
  （S3 真实：revision-b2 续接被杀线程，122.7s 完成页面，核对后 DELIVERED）。
  被杀的尝试保持 failed，**不会被改判或复活**。

## 4. 撤回授权（已确认停止）

```
py -3.14 -X utf8 -B -m mentis collab-resume --state <state.json> --revoke
```
- 所有活动授权 revoke，事件流留 `authorization.revoked`，状态 WITHDRAWN。
- 撤回后的任何续办（回答/反馈/改目标）都会在委托前被 `UnauthorizedAction` 拒绝；
  追踪留痕，产物哈希不变、零新写入（S4 真实 + P1.4 回归）。
- 撤回 ≠ 回滚：已写入工作区的文件保留，供你核对。

## 5. 改目标与晚到结果

```
py -3.14 -X utf8 -B -m mentis collab-resume --state <state.json> --goal-new "新目标"
```
- 目标变更 = brief 升版 + 配置/评估计划改版（旧文件不改写）+ 按新版本重新委托。
- **晚到结果不自动验收**：旧版本尝试即使事后被确认 completed，接受时也会被
  `StaleVersion` 拒绝（追踪 `accept.rejected` 留痕）；已交付的新版本不受影响
  （S5 真实 + 离线回归）。
- 旧 brief 版本的回复（回答/方向确认）同理：入档但不自动推进新范围（P1.3）。

## 6. 单写入

- 同一工作区同时只允许一个写入者；第二委托在任何进程启动前被
  `ExecutorBusy` 拒绝（S1 孤儿存活期间真实复现）。
- 锁文件在 `runtime/executor_locks/<工作区摘要>.lock`。**不要手工删除**：
  它由核对路径（§1 第 4 步）在确认无活跃写入者后释放。

## 7. 状态核对入口（只读）

```
py -3.14 -X utf8 -B -m mentis status  --run runtime/collab/<运行ID>
py -3.14 -X utf8 -B -m mentis replay  --run runtime/collab/<运行ID>
py -3.14 -X utf8 -B -m mentis view    --run runtime/collab/<运行ID>
```
- 状态视图含：阶段、未决等待、生产者活动信号、任务库（schema/版本/摘要）。
- 活动信号过期只表示无法确认生产者活跃，不代表任务失败、不解锁、不自动重发。

## 8. 证据索引

| 场景 | 运行目录 / 记录 | 结果 |
| --- | --- | --- |
| 进程中断→恢复→交付 | `runtime/collab/20261009_064724-85f9`；`runtime/observability/p33/records/S1-process-exit.json` | 收养核对→DELIVERED，零重发 |
| 提交超时→两轮核对 | `runtime/collab/20261009_065804-5e56`；`records/S2-submit-timeout.json` | 预算有界，NOT_DELIVERED 如实 |
| 执行器失败→原生续接 | `runtime/collab/20261009_070230-5443`；`records/S3-executor-failure.json` | FAILED 如实→续接→DELIVERED |
| 单写入 | `records/S6-single-writer.json` | ExecutorBusy 拒绝 |
| 撤回（中断等待状态撤回=已确认停止） | `runtime/collab/20261009_070837-5e60`；`records/S4-revoke-confirmed-stop.json` | WITHDRAWN 后拒绝零新写入 |
| 改目标 / 晚到结果 | `runtime/collab/20261009_071417-47ba`；`records/S5-goal-change-late-result.json` | 锁拦下未核对的改目标；晚到确认不被接受 |
| 离线回归 | `tests/test_p33_recovery.py`（4 项） | 活跃拒确认、孤儿收养、晚到拒收（StaleVersion 全链）、checkpoint 语义 |
