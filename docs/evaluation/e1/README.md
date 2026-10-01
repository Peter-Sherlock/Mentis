# E1 开发集与强基线准备

日期：2026-10-01。版本：`e1-dev-v1`。**探索性材料与配置已冻结；E1仍partial，E2未运行。**

本轮授权来自负责人：“这个部分已经完成了，我委托你帮我回答那5个问题，然后我们开始下一步”。[T4辅助回答](../../progress/P2.4-T4-assisted-answers.md)已经整理；它不充当负责人独立理解或本人体验原话。依据开发计划允许并行推进不依赖真人反馈的评测准备，T4体验保持未完成。

## 当前可查看材料

| 材料 | 内容 |
| --- | --- |
| [suite.json](suite.json) | 5个开发样本、26个白名单源文件引用及哈希、共享权限预算、各组提示词SHA |
| [reviewer.json](reviewer.json) | 作者侧来源、受控改动、检查依据和模拟用户协议；不导出给运行组 |
| [E1-02页面](samples/E1-02/index.html) | 从真实页面制作的受控缺价变体；属于AI作者样例 |
| [E1-03页面](samples/E1-03/index.html) | 真实Codex最终产物的字节副本，无新增受控缺陷；不预设全面合格 |
| [E1-02移动截图](samples/E1-02/observation/mobile.png)、[E1-03移动截图](samples/E1-03/observation/mobile.png) | 同一Chrome及390×844视口真实截图；桌面1280×800亦已采集 |
| [专业基线提示词](prompts/B-1.md) | 明确有据评审、保留项、可逆代决、复检及合格不改；允许多次正常自检和修订 |
| [准备报告](../../../runtime/evaluation/e1-prepared-verified/prepare-report.json) | 20份输入包、组内共享输入fingerprint和评价侧身份映射 |

这些是比较前的固定输入，不是本轮Mentis新生成或改善的作品。全部案例仍 `pending_human`，用户感受文本及决策角色协议都是模拟；没有负责人新体验反馈、没有独立人工评分、没有效果比较结果。

## 样本与用途

| 开发ID（只在评价端） | 设置 | 固定内容 | 重点 |
| --- | --- | --- | --- |
| E1-01 | 端到端 | 完整资料与自主制作请求，无预造首稿 | 常规设计不阻塞、交付前自行检查 |
| E1-02 | 固定首稿反馈 | 真实页面的缺价变体、基于该缺失的模拟含糊反馈 | 诊断是否成立、局部修订与保留 |
| E1-03 | 固定首稿反馈 | 实际最终页面与检查请求，没有注入缺陷 | 观察后合理停止，避免无据审美改动；真实缺陷仍可修 |
| E1-04 | 受控决定片段 | 两份同优先级资料日期不同 | 查证不能解决时请求用户事实 |
| E1-05 | E1-04配对控制 | 同资料，已有用户明确最终日期 | 复用授权与事实，不重复询问 |

五例同属一个开发场景族，不作未见保留集，不凭数量作统计结论。决定片段单列，不混入审美改善率。后续扩展到回归、撤回等情境须新增版本并记录，不能修改运行后的v1使结果变好。

固定首稿源是 `20261001_020003-73ca` 的最终页面，SHA-256 `b17b1f06934b6705ed100121e232216c42d29920d0ada25517035cb05fb89168`；[source/index.html](source/index.html)留作不可变来源。E1-02只把其中的¥99替换为“费用待补充”，完整价格资料仍公开给各组。生成与采集脚本是 [build_e1_fixtures.py](../../../scripts/build_e1_fixtures.py)。领域资料保留草案/假设性质，去掉原FC作者标签，各组使用同一副本。

冻结目录在.gitattributes中关闭文本换行转换，保证Git保存和检出后哈希字节相同。

## 方法配置与公平条件

| 设置 | 方法组 | 机制差别 |
| --- | --- | --- |
| 固定首稿反馈 | B-1 / B-2 / B-3 / M | 强专业提示词 / 固定清单 / 执行器自改 / Mentis流程 |
| 端到端 | B-0 / B-1 / B-2 / M | 直接执行器多轮 / 强提示词 / 清单 / Mentis |
| 受控决定片段 | B-1 / B-2 / B-3 / M | 同样事实、授权和用户可用性，单独评价决定处理 |

同一样本的原文、作品、观察与专业资料、工具访问、用户可用信息、公开要求及预算逐字节相同，仅方法提示词不同。反馈设置不重复设B-0；B-1没有单次调用限制，B-3不会被剥夺观察或专业资料。每个组单独工作区、相同追踪与采集；一次运行只允许一个写入者。

探索性预算：最多2次自动修订、3次执行器委托、4次外层指导/复查模型调用；单次执行器600秒。调用与轮数属于待E2运行器实际落实的配置，输入导出本身不执行这些上限。Codex token/货币硬上限目前unsupported，只记录实际用量；资源匹配与自然使用成本分别报告，累计usage按thread差分，不能逐轮重复求和。

当前受测Mentis专业路径基线为提交 `4dc9d06`；配置沿用 `deepseek-v4.1-flash`，本轮没有模型探测或调用。Codex CLI本机确认0.158.0，Python3.14。截图采用Chrome文件版本154.0.8037.58；工具报告内browser_version仍unknown，因此文件版本单独记录，不补造原始测量字段。

## 实际准备与边界

`eval-prepare`逐文件校验哈希和路径，然后导出到中性 `input-NNN/<method>/`。每个运行组只有公共 `input.json`、自己的 `prompt.md`及白名单文件；原sample ID、作者评分文件、私有运行state/session/历史诊断不会随包复制。评价侧映射只在父级报告，运行时只能挂载单组目录。已验证各组共同输入字节一致、冻结源篡改拒绝、路径及junction逃逸拒绝、作者字段拒绝、已有输出拒绝覆盖。

**白名单导出不等于读隔离；读隔离现由 deny ACL 隔离窗口提供并已双端实测。** 机制：运行前对套件目录（防 sample 身份推断）、prepare-report.json（身份映射）和除目标组外全部输入包写入 Windows deny ACL（当前用户 + Codex 沙箱账户，单次 icacls 调用；开窗前金丝雀预检保证可解除）。2026-10-01 真实核验：Mentis 环境同用户子进程 63/63 次反向读取（绝对路径/父目录遍历/junction）全部 PermissionError，正向读写正常；真实 Codex CLI 0.158.0（workspace-write）9/9 次读取尝试 DENIED、正向写入确认。运行前检查 `eval-check --isolation-evidence` 核对探测证据（schema/TTL 12h/身份/Codex 版本/双端探测/**当前 live 拒读**），任一缺失即保持未就绪；release 后同一证据立即失效。诚实边界：deny 不防对抗性绕过（同用户属主可自行解除 ACL），也不证明提示词服从或模型不试图读取；探测细节与失败尝试见[E1记录](../../progress/P2.4-E1.md)。

两份固定页面真实测量的innerWidth分别390/1280，溢出均0px；记录空表单checkValidity=false、填写后=true、同步确认DOM变化。测量在注入preventDefault的副本执行，不能当作完整点击、异步提示或真实网络取证。对比度抽检、死链执行、网络取证、完整交互及独立专业质量复核仍未完成；无首稿的端到端/决定输入按设计不预造观察。

第一次Chrome采集在工作区沙箱内以2147483651退出；经自动审批允许本地采集脚本在沙箱外执行后，两份页面采集成功。没有调用DeepSeek或Codex，没有修改两个T4真实运行。

## 可亲自执行的操作

在项目根目录运行：

```powershell
py -3.14 -m mentis eval-check
```

实际输出应包含 `sample_count: 5`、`frozen_source_hashes.passed: true`和`status: prepared_not_ready`；`runtime_answer_isolation` 只在提供有效隔离证据且当前窗口开启时才为 true。制作新包可执行：

```powershell
py -3.14 -m mentis eval-prepare --out runtime/evaluation/e1-my-preview
```

全新输出生成20份输入包；已有内容会拒绝覆盖。此入口不运行模型或执行器。

隔离核验（真实环境，含一次真实 Codex 探测 turn）：

```powershell
py -3.14 -m mentis eval-isolate probe --prepared runtime/evaluation/e1-isolation-probe --arm input-001/M
py -3.14 -m mentis eval-check --isolation-evidence runtime/evaluation/e1-isolation-probe/isolation/isolation-evidence.json
py -3.14 -m mentis eval-isolate release --prepared runtime/evaluation/e1-isolation-probe
```

预期：probe 返回 `ok=true, window_open=true`；窗口内 eval-check 显示 `runtime_answer_isolation.passed=true`（含 live 拒读）；release 后同一条 eval-check 立即变回 `passed=false`。

## 下一步

继续E1公平基线运行器接入（在隔离窗口内运行受测组、共享追踪与预算执行），完成后才进入E2小规模探索性对照。待独立人工复核和正式指标/阈值冻结后，才允许正式能力结论；这些不作为已授权探索准备的额外用户审批。
