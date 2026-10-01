# Mentis

代表非专业用户与执行型 Agent 持续协作的代理：帮助形成目标，决定下一步，委托工作，观察实际产物，诊断问题并给出有效修改指导。

**当前状态：P2.4进行中，T4技术真实往返已核对；负责人委托的[五问辅助回答](docs/progress/P2.4-T4-assisted-answers.md)已完成，本人体验仍未取证。E1进行中：5个开发样本、基线配置、白名单与哈希核对及20份输入包已准备；运行端答案读隔离已按审查补修并双端实测（事务化 deny ACL 窗口：原 DACL 存档+deny 排练+24/24 精确恢复；确定性探针双端 76/76 拒读含 git 历史副本；准入 v2 强绑定原始记录），E2比较未运行。** 入口见[E1准备说明](docs/evaluation/e1/README.md)。阶段性“已交付”表示初稿可以查看，不代表用户满意或最终完成。新T4运行的keep漏判与历史improved的归因限制已明确记录，不能据模型复查宣称专家能力。P3.2/P5.2a及当前能力范围见[开发状态](docs/DEVELOPMENT_STATUS.md)，案例与评价材料仍为AI草案。

## 运行（核心 Python 无第三方包依赖）

要求 Python ≥ 3.10。本机 `python` 当前指向 Anaconda Python 3.7.3；Windows 上使用 `py -3.14`（本机版本 3.14.7）。为正确显示中文及 `¥`，PowerShell 先设置 `$env:PYTHONIOENCODING='utf-8'`。在本目录执行：

```bash
py -3.14 -m mentis                # 入口说明
py -3.14 -m mentis --version      # 仅版本号
py -3.14 -m mentis demo-contract  # 脚本化契约演示，页面质量未验证
py -3.14 -m mentis demo-routing   # 确定性的资源选择与缺口分流演示
py -3.14 -m mentis diagnose --page runtime/demo/event_page_v1.html --fixture tests/fixtures/mock_diagnosis_response.json
py -3.14 -m mentis observe-page --page runtime/demo/event_page_v1.html  # 本机浏览器双视口截图
py -3.14 -m mentis guide-revise --baseline runtime/collab/<运行ID>      # P3.2 指导修订循环（真实运行加 --real）
py -3.14 -m mentis eval-check     # E1冻结核对；--isolation-evidence 接入真实隔离准入（窗口内才可能 passed）
py -3.14 -m mentis eval-prepare --out runtime/evaluation/my-e1-preview # 离线制作输入包，已有内容拒绝覆盖
py -3.14 -m mentis eval-isolate probe --prepared runtime/evaluation/e1-isolation-probe --arm input-001/M
                                  # 事务化开启 deny ACL 隔离窗口并双端确定性探测（含一次真实 Codex turn）
py -3.14 -m mentis eval-isolate release --prepared runtime/evaluation/e1-isolation-probe
                                  # 按原 DACL 事务恢复（逐项核验）；sweep 清扫崩溃遗留窗口
```

## 协作运行（P2.3：真实执行器 + 真实截图）

```bash
# 自主有界任务：用户不回复也能完成（需 Codex 已登录；DeepSeek 密钥用于诊断候选）
py -3.14 -m mentis collab --goal "为拾光咖啡（虚构品牌）周六手冲工作坊做一个温暖简洁的活动介绍页（本地单页，含报名表单）" --non-interactive

# 共同设计：先确认风格方向再委托（非交互时保持待决，不猜测）
py -3.14 -m mentis collab --design --non-interactive --goal "..."
py -3.14 -m mentis collab-resume --state runtime/collab/<运行ID>/state.json --answer "ix-3:暖棕纸质感，首屏大图"

# 明确修正进入新 brief 版本并续接修订；撤回授权后一切委托被拒绝
py -3.14 -m mentis collab-resume --state runtime/collab/<运行ID>/state.json --feedback "首图换成手冲壶插画感，其余保留"
py -3.14 -m mentis collab-resume --state runtime/collab/<运行ID>/state.json --revoke
```

执行器在**隔离 Git 工作区**（`runtime/collab/<运行ID>/workspace/`，只含 brief 与素材，不含任何秘密）内工作；产物与双视口截图在 `runtime/collab/<运行ID>/`，`report.md` 提供看结果/看变化/看依据三个入口。**阶段性交付（PROJECT_PLAN §7）：DELIVERED 表示"初稿做好，可以查看"、结构层硬要求检查通过，不表示最终任务完成或用户满意**；反馈修订前会自动把旧版产物快照到 `versions/` 供前后对照。brief 中每条约束/偏好标注来源：用户选定（user_selected）/ 系统约束（system，如隔离与无网络预置规则）/ Mentis 自主推导（agent_selected）/ 暂定（provisional）。**交付前专业评审（T4 补修，当前优先级）**：结构检查通过后，Mentis 以渲染测量+双视口截图+真实模型诊断按领域标准评审工作稿；候选逐条审查（采纳/驳回/保持现状/待用户，理由留档 `professional-review.json`）；成立问题在预算内转成修订指令（位置/证据/保留项/复检办法）委托 Codex 并重新观察复检（诊断与复查为同源模型）；合格不改但留依据；回归不交付、测量不可信不判通过、预算耗尽如实留档。执行器超时会进入 unknown 并保留写锁，需人工核对产物后用 `collab-resume --confirm-outcome completed` 确认；委托从不自动重发。执行器反问（QUESTIONS.md）先由 Mentis 分流：素材可答与常规专业问题自主回答，只有用户独有事实/取舍/许可才请求决定。没有货币硬上限；只记录调用次数、时长与可取得的 token 用量。

## 指导修订循环（P3.2：诊断 → 修改指令 → 修订 → 复检）

```bash
# 从 P2.3 真实运行冻结首稿，走完整闭环（真实运行必须显式 --real）
py -3.14 -m mentis guide-revise --baseline runtime/collab/<运行ID> --real --timeout 600

# 诊断请求用户独有决定时暂停；回答后继续修订
py -3.14 -m mentis guide-resume --state runtime/guidance/<运行ID>/state.json --answer "ix-4:需要"

# 执行器中断（非超时）后，核对无活跃写入者再续办（计入修订预算，不盲目重试）
py -3.14 -m mentis guide-resume --state runtime/guidance/<运行ID>/state.json --retry
```

## 真实使用会话（P5.2a：项目负责人本人操作）

```bash
# 路径 A：用你自己的话说目标 → 自主委托 → 三入口 → 当场逐字反馈（真实费用，约 5—15 分钟）
py -3.14 -m mentis field-start --label 我的第一次委托 --goal "给周末读书会做一个报名页，温暖一点，能填姓名和电话"

# 路径 B：先看已有真实产物，再补记逐字反馈（零成本）
py -3.14 -m mentis field-feedback --run runtime/guidance/20260928_092745-5465 --satisfied no --quote "你的原话" --block "哪里卡住或没看懂"
```

操作、系统行为、耗时、阻塞与你的原话自动归档在 `runtime/field/<会话ID>/`（操作/系统行为/研究者提示分列）。**P5.2a 当前未完成：入口已就绪，等待真实操作记录**；指南见 [docs/field-session-guide.md](docs/field-session-guide.md)。

## 交互追踪与回放（P2.4）

```bash
# 查看一次运行的追踪时间线（新运行=事件流回放+委托台账；历史运行=现存材料清单+未记录缺口，不回填）
py -3.14 -m mentis trace --run runtime/trace-demo/<演示ID>   # 机制演示（FakeAdapter，非真实委托）
py -3.14 -m mentis trace --run runtime/guidance/20260928_092745-5465   # 真实历史运行
py -3.14 -m mentis trace --run runtime/t2-real-stream --follow        # 实时旁观（运行中另开终端；本例已收尾会自动结束）

# 导出可共享的白名单时间线（JSON：仅契约字段与文件哈希；不含 prompt 原文/用户逐字反馈/密钥值）
py -3.14 -m mentis trace --run runtime/t2-real-stream --export 导出路径.json
# 本地阅读确切委托、Codex 对外回复与动作摘要（不是白名单导出）
py -3.14 -m mentis conversation --run runtime/collab/20260929_084347-5f82  # 本地交流视图
```

`collab` / `guide-revise` 的每次委托**先归档确切指令**（`trace/prompts/<attempt>.md`，SHA-256 与实际发送文本一致）再调用执行器，并写 `trace/events.jsonl`（目标/授权/委托/执行器终态/复检/决策/收尾/核对确认）与 `trace/manifest.json`（完整性）。事件丢失时运行不得称完整追踪；历史运行不补写伪事件。契约见 [docs/TRACKING_EVENT_CONTRACT.md](docs/TRACKING_EVENT_CONTRACT.md)。故障与隐私边界：运行意外异常会收尾为 **ERROR** 并落盘（无终态尝试记 unknown，不假完成；`field-start` 的半份会话日志明确标注不完整并保留日志入口）；unknown 的人工核对确认以 `executor.confirmed` 事件入流；事件摘要与导出对已知密钥字段名（api_key/token/password/密码/密钥等）的赋值做确定性隐去——只认已知字段名，不承诺识别任意秘密；`input_tokens` 等用量计数不隐去；续接/恢复在回放中以 `run.resumed` + 中断区间与委托台账（同 thread 多 attempt）呈现。T4 真实运行核验记录（含一次真实 ERROR→核对→交付）见 [P2.4-T4 记录](docs/progress/P2.4-T4.md)。

“看依据”现在另有本地 `conversation.md`：顺序展示给 Codex 的确切指令、Codex 对外回复全文和动作摘要；新运行的公开回复全文归档在 `trace/messages/` 并以哈希关联事件。它不包含内部推理或原生命令输出，也不进入白名单导出。**同一委托中的多条 Codex 进度消息不是 Mentis 与 Codex 的多轮往返。**T4 旧运行仅有一次委托、无真实反馈修订；原事件只存回复摘要，本机原生会话中可核对的五条公开回复另整理于 `runtime/t4-observed-exchange.md`（只读补充，不回填 36 条事件）。

流程：冻结 v1（原运行不改动）→ 结构观察 + 渲染测量（iframe 包装获得真实 390/1280 布局，Windows 无头 500px 最小窗宽已被绕开）+ 真实视口截图 → DeepSeek 诊断（**双视口截图作为图像输入**，证据引用必须指向观察条目）→ 组装修改指令（缺陷与修法只来自模型诊断与机器测量，含必须保留的活动事实）→ 真实 Codex 修订（隔离 Git 工作区，v1 已提交为基线）→ 独立复检（保留项逐段核对、表单逐字段必填核对、溢出复测须测量可信、无网络声明 + 模型带图复查须绑定当前产物版本）→ 判定改善/无改善/回归/**未知**（测量报错、视口尺寸不符、缺复查截图或复查无法绑定当前产物时判 UNVERIFIED，不以零溢出判通过）；修复缺陷但破坏任一保留事实或任一字段必填 = 回归，不标改善。自动修改最多 2 轮。诊断提出的决策请求先由 Mentis 分流：触及保留决定（改价/改期/发布等）或需要素材外新事实才暂停等待用户，可逆的呈现取舍自主决定并留痕（分流的必要性本身未经证明，见 P3.2 记录）。`runtime/guidance/<运行ID>/report.md` 提供三个入口（v1/v2 作品与截图、同条件对比与保留项、完整指令/诊断/事件/用量）。

`demo-routing` 展示：三份网页试点资源（信息组织/视觉表达/基础交互，规则分硬要求/建议/偏好/待验证假设四类）按任务需要加载；七类缺口分流到自决/工具查询/执行器咨询/小样实验/请求用户/受限未验证/升级，每条带理由；两条纪律自检（常规专业问题不转交用户、必要介入不省略）；未知领域与声明冲突的受限处理。

`diagnose --fixture` 用真实 HTML 文件做**结构观察**，再处理 Mock 固定响应；它不证明视觉效果、交互行为或模型诊断正确。真实调用使用官方模型 ID `deepseek-flash`，需提供 `DEEPSEEK_API_KEY` 且显式加 `--real`。可在项目根目录新建 `.env`，写入单独一行 `DEEPSEEK_API_KEY=你的密钥`；程序会读取这个文件，无需额外安装依赖。若系统环境变量中已定义同名键，则它优先（即使为空）。`.env` 已被 Git 忽略，但仍需仅保存在可信电脑上，切勿提交或分享。`--real` 会发送页面结构、目标、反馈与领域规则至 `api.deepseek.com`；单次命令至多两次请求，每次输出上限 2000 token、输入文本上限 25000 字符。请只对已核对的数据执行。

`observe-page` 需要本机 Chrome 或 Edge，将桌面 1280×800 与移动 390×844 的初始截图及 `observation.json` 保存至 `runtime/observations/`。它不调用模型，不执行点击、表单提交、对比度或自动溢出检测。P3.1 Codex CLI Adapter 见 [mentis/executors/codex_cli.py](mentis/executors/codex_cli.py)；需要本机 Codex CLI 已登录，写入任务应在独立 Git 工作区运行。真实隔离探针、能力矩阵和限制见 [P3.1 记录](docs/progress/P3.1.md)。

## 开发检查

```bash
py -3.14 -m unittest discover -s tests -v
```

当前 195 项：入口、契约不变量、能力资源与分流、P2.2 网关/观察/诊断、P3.1 Adapter 与截图边界、P2.3 协作循环（含 v1→反馈→v2 修订路径核验：原目标/硬约束/旧版产物快照/修改依据保留）、P3.2 指导修订循环（合格不改、回归拒收、决策分流/暂停、失败轮核对续办、unknown 产物核对、状态恢复；验收修复：测量不可信判未知、保留项逐段与表单逐字段核对、复查绑定当前产物——均先复现误判后验证修复）、P5.2a 现场会话与逐字反馈归档、P2.4-T1 追踪写入与事后回放（seq/哈希/不回填）、P2.4-T2 逐行流与实时旁观、P2.4-T3 故障与隐私加固（敏感输出隐去、追踪写失败不粉饰、截断/限额不判成功、续接多 attempt 时间线、撤回落trace、现场会话异常收尾、导出白名单与旧记录兼容）、T4 补充（unknown 不崩溃回归、失败子项消息、核对确认事件、终态未入流的 session.json 交叉引用、公开回复全文归档与交流视图）、**T4 交付前专业评审**（有据触发修订、不成立/保持现状不触发、合格不改留依据、无截图不冒充视觉验证、测量不可信判 UNVERIFIED、回归不交付、预算耗尽留档、修订 unknown 停止、保留决定暂停修订、"保留当前"措辞防回归）、**E1 隔离与 stdin**（假 .cmd 执行器：UTF-8/引号完整性、启动+恢复双调用、不读 stdin 阻塞到共享截止时间、BrokenPipe 不判 completed、超时进程树杀；事务窗口：原 DACL 精确恢复含第三方 deny 保真、原子日志失败不开窗、部分 deny 失败全回滚、并发互斥、恢复失败逐项报告、执行器存活门控；探针四分类与缺测不计通过；准入七类拒绝路径；--no-codex 只产调试）。Mock HTTP 响应只验证请求格式、用量采集与记录边界；真实调用证据另见阶段报告。

## 目录

```
AGENTS.md            Agent 协作约定（含每轮可观察交付要求，第 6 节）
docs/                规划、开发计划、状态、决定记录、案例、阶段报告
mentis/              contracts / session / persist / export / demo_contract
                     capabilities/：registry 资源加载、selection 按需选择、routing 缺口分流
                     models/：DeepSeek Model Gateway（含图像输入）；observation / diagnosis：候选与结构观察
                     executors/：Codex CLI Adapter；browser_observation：本地双视口截图
                     render_measurement：真实视口渲染测量（溢出/裁剪/表单）
                     collab.py 协作循环 + collab_cli.py（collab / collab-resume）
                     guidance.py 指导修订循环 + guidance_cli.py（guide-revise / guide-resume）
resources/           首领域资源（草案）：registry.json + web_pilot/ 三份
tests/               入口、契约不变量、能力与分流检查
runtime/             默认忽略；演示与会话数据
```

## 文档入口

- [开发计划](docs/DEVELOPMENT_PLAN.md)：22 个工作包的真实执行顺序（不按编号排序）。
- [领域策略](docs/DOMAIN_STRATEGY.md)：首试点（小型网页界面与交互，proposed）与领域能力组合。
- [案例](docs/cases/README.md)：网页试点反馈链案例（草案）与文档问答回归草案。
