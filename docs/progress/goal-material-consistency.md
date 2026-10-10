# 任务目标—素材—验收一致性修复 — 工作包记录

日期：2026-10-10 ｜ 基线：提交 `80cc8e3`（标签 `goal-consistency-baseline`，整树 `903b6251…`，211 文件清单）｜ 对应审计：问题 #1，分析见 [验收修复轮留档副本](../../runtime/observability/p34/acceptance-fix/audit-20261010-analysis.md)（原件在旧 ceb7 工作区 runtime/audit/20261010-p34/analysis.md）

## 1. 审计问题的结构性根因与修复

审计 #1：真实首次委托同时要求拿铁上新页与旧手冲工作坊的日期、价格、报名表单；修订后正文仍保留工作坊内容；诊断的目标一致性候选被映射为 unverifiable 跳过。逐层根因与修复：

| 层 | 根因 | 修复 |
| --- | --- | --- |
| 计划组装 | 任务实例层从素材机械提事实，**不问素材主体是否属于本目标** | 新增确定性判定 `assess_material_goal_consistency`（目标「」引用主体 vs 素材主体：互含→consistent；互不含→mismatch；无引用时去停用词二元组重叠 ≥2→consistent、0→mismatch、否则 undetermined——判不了不装懂）；mismatch 时 T1/T2 不建（素材事实不进验收），计划记 `material_assessment` |
| 验收维度 | 计划无「目标一致性」维度——工作坊内容混进拿铁页反而通过（T1 只查素材事实可见） | 始终建 **T3 目标主体一致**（deterministic：目标引用主体须在产物文本可见）；mismatch 时另建 **T4 不一致素材内容不出现**（新 spec `text_absent`：素材主体/日期/价格等禁现，阻塞） |
| 委托指令 | `build_delegation_prompt` 在 facts 为空时**静默回退到内置工作坊素材**（把上一任务的素材事实注入本目标）；报名表单行**无条件硬编码** | 回退删除（空即空）；mismatch 时写 `[素材声明]`（素材不适用、不虚构无依据事实）；表单行改 `require_form` 参数（跟素材走） |
| 预期值 | `_expected_values` 计划无 needle 时回退素材推导 | mismatch 门控：直接返回 `{}` |
| 评审路由 | 目标一致性候选按词面映射到 unverifiable 维度 → 跳过 | T3/T4 为 deterministic 维度 → 同类候选映射后路由 `adopt`（自主修订）——离线回归固化 |
| 表单跟随 | T2 条件含「有 signup 标签即建」（「无需报名」也算）；`_form_required` 同病；**委托 prompt 尾部的验收项列表不过滤不适用条目**（真实运行观察：执行器据 D4 实现了素材说不需要的表单） | T2/`_form_required` 只认素材报名方式给出具体字段（姓名/联系等）；`acceptance_block` 支持 `skip_entry_ids`（不适用条目标注「本轮不适用（素材未要求），不需要实现」）；`hard_requirement_review` 对不适用表单条目记非阻塞说明 |
| 其他 | 反问分流/模型回答素材硬编码 `COPY_DECK`（不随注入素材） | 改 `self.material_text` |

CLI 新增 `collab --material <路径>`（标注化素材文件，≤64KB；缺省仍内置合成素材）。

## 2. 真实验证（预算内：执行器 turn 2/4、模型请求 4/12、预检 2 次）

**限定任务**（审计要求：不同于旧工作坊素材）：目标=焦糖苹果拿铁上新页（含门店与供应时间），素材=同主体标注化拿铁素材（¥38/2026-10-24/梧桐街 12 号/到店点单无需报名）。运行 `runtime/collab/20261010_071357-fc05`：

1. 计划：`material_assessment=consistent`；**T1 needles=素材值**（¥38/2026-10-24/梧桐街 12 号/焦糖苹果拿铁）；**T3 目标主体**在；无 T4。
2. 构建（120.4s）：执行器 `turn.completed` 伴失败子项 → **unknown 如实**（通知按原始错误陈述，不再硬编码"超时"）→ 人工核对：产物含全部目标+素材事实、标题「焦糖苹果拿铁（周末限定新品）｜拾光咖啡」、无工作坊残留 → **核对确认 completed** → DELIVERED。
3. 真实观察到的残余不一致：产物带报名表单而素材写「无需报名」——根因是 prompt 验收项列表未过滤 D4（上表最后一行）——**先修代码再继续**。
4. 反馈修订（移除表单，改为到店点单提示）：修订产物**表单已移除、到店点单在、目标+素材事实保留**；执行器又 unknown → 核对确认 → **DELIVERED**（revision-b2 accepted）。
5. **前后版本保留**：`versions/index-attempt-1.html`（e8fcab43…）vs 当前（30a1f687…）。
6. 用量与证据：`usage-accounting.json`、console 日志、probe-result.json。

诚实边界：acceptance 过滤（D4 不列入验收项）与 `_form_required` 修正的**线上验证只到「修订产物正确」**——修订委托发生在修复前（其 prompt 仍带旧表单行，产物正确是执行器服从反馈指令）；两者的 prompt 级验证由离线回归覆盖（test_form_not_required_when_material_says_walk_in 断言 prompt 无表单行+验收块含「本轮不适用」）。两次执行器 unknown 均为 turn.completed 伴失败子项（Codex 同型已知行为，约 120s/轮，非超时）。

### 2.1 mismatch 全流程真实验证（2026-10-10 收尾轮，预算 /2：执行器 1/4、模型 2/12、探针 1 组合）

运行 `runtime/collab/20261010_080852-248b`（目标=「桂花乌龙茶」上新页，**不传 --material**：内置工作坊素材与目标主体不一致——审计 #1 原始失败场景）：

1. 计划：`assessment=mismatch`（引用主体互不含）；**无 T1/T2**；T3（目标主体「桂花乌龙茶」）+ T4（工作坊主体/日期/价格禁现）；`material.consistency` 事件留痕。
2. 委托 prompt：含 `[素材声明]`（素材不作为信息来源、不虚构具体事实）；**无工作坊事实**（无日期/价格行）；验收块 **D4 标「本轮不适用（素材未要求），不需要实现」**（acceptance 过滤的真实生效证据）。
3. 产物（13,363 字节）：茶主题页，**零工作坊残留**（手冲/工作坊/2026-11-14/¥99 全部未命中）；**零虚构事实**（无任何价格/日期/时间数字——门店与时段写「具体待确认，出发前向门店询问」，正是素材声明要求的通用表述）；页面内"表单"实为本地周末便签交互（非报名，未虚构报名事实）。
4. 执行器 turn.completed 伴失败子项 → unknown 如实 → 人工核对（上述内容核对+无活跃写入者）→ 确认 completed → 硬要求检查通过（T3/T4 全过）→ 专业评审（真实模型，5 候选合理分流）→ **DELIVERED，全程 1 次委托零重发**。

前置修复（本轮先修后跑）：mismatch 下执行器反问的素材分支曾会用不适用素材行回答（¥99 注入 ANSWERS.md 的风险）——素材分支与 `_answer_by_model` 均加 mismatch 防护（离线回归 test_material_question_answered_without_workshop_facts）。本轮真实运行未出现反问，该防护保持离线证据。

## 3. 离线回归

- 新增 `tests/test_goal_material_consistency.py` **14 项**：判定四态（审计案例 mismatch/匹配 consistent/无引用工作坊 consistent/无主体 undetermined）；mismatch 计划（无 T1/T2、有 T3/T4、禁现值正确）与 consistent 计划（T1+T3、¥38、无报名素材无 T2）；T4 执行器（工作坊页阻塞、拿铁页通过）；prompt 无静默回退+素材声明；审计案例全流程离线复刻（首稿工作坊页被 T4 拦下→修复为拿铁页→DELIVERED，prompt 无工作坊事实，material.consistency 事件留痕）；`_expected_values` 门控；表单不跟随「无需报名」素材（prompt+验收块）；目标一致性候选路由 adopt（不再 unverifiable）。
- 既有夹具语义演进（3 处，各保留原验证目的）：书店夹具改配书店目标；两处改目标测试改用同主体目标（异主体目标现会判 mismatch 并禁现旧素材内容——新语义）。
- 全量回归 **417 项（416+1 跳过）**通过（2026-10-10 实跑两次）。

## 4. 诚实边界（未真实验证/残留）

- ~~mismatch 全流程未跑真实链~~（2026-10-10 收尾轮已补：见 §2.1）。mismatch 下的执行器反问防护为离线证据（真实 mismatch 运行未出现反问）。审计原运行（20261009_095200）不回填不重判。
- 「工作台页面传素材」未实现（--material 仅 CLI；单用户可先用 CLI）。
- 判定是词面启发式：目标不加引号且主体词面不重叠时会误判 mismatch（保守方向：素材不注入+禁现——若素材实际适用会损失事实注入；undetermined 时维持旧行为）。停用表/阈值以常量落盘可审计。
- 专业效果仍未验证；E2 独立评审、T4 本人体验待负责人。

## 5. Git

基线 `80cc8e3`（标签 goal-consistency-baseline）；本轮提交=mentis/evaluation_plan.py、mentis/collab.py、mentis/__main__.py、mentis/collab_cli.py、tests/test_goal_material_consistency.py（新增）、tests/test_evaluation_plan.py、tests/test_collab.py、tests/test_p33_recovery.py（夹具语义演进）、docs/progress/goal-material-consistency.md（本文件）、docs/DEVELOPMENT_STATUS.md。运行证据 runtime/observability/goal-consistency/（不入库）。

## 6. 推荐下一包

负责人自然语言反馈体验（P5.2a 收尾方向）或 E2 独立评审——技术侧本包后无已知阻塞项；等指令，不自动开始。
