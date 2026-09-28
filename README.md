# Mentis

代表非专业用户与执行型 Agent 持续协作的代理：帮助形成目标，决定下一步，委托工作，观察实际产物，诊断问题并给出有效修改指导。

**当前状态：P2.2、P3.1 技术退出条件完成；下一包 P2.3。** DeepSeek 真实诊断、Codex CLI 隔离委托和本地网页双视口截图已分别实测，尚未连接成自主指导闭环。模型曾因观察遗漏给出错误诊断，修复后仍有不必要的用户问题；不能据此宣称专业能力通过。开发位置与证据边界见 [docs/DEVELOPMENT_STATUS.md](docs/DEVELOPMENT_STATUS.md)；案例与评价材料仍为 AI 草案。

## 运行（核心 Python 无第三方包依赖）

要求 Python ≥ 3.10。本机 `python` 当前指向 Anaconda Python 3.7.3；Windows 上使用 `py -3.14`（本机版本 3.14.7）。为正确显示中文及 `¥`，PowerShell 先设置 `$env:PYTHONIOENCODING='utf-8'`。在本目录执行：

```bash
py -3.14 -m mentis                # 入口说明
py -3.14 -m mentis --version      # 仅版本号
py -3.14 -m mentis demo-contract  # 脚本化契约演示，页面质量未验证
py -3.14 -m mentis demo-routing   # 确定性的资源选择与缺口分流演示
py -3.14 -m mentis diagnose --page runtime/demo/event_page_v1.html --fixture tests/fixtures/mock_diagnosis_response.json
py -3.14 -m mentis observe-page --page runtime/demo/event_page_v1.html  # 本机浏览器双视口截图
```

`demo-routing` 展示：三份网页试点资源（信息组织/视觉表达/基础交互，规则分硬要求/建议/偏好/待验证假设四类）按任务需要加载；七类缺口分流到自决/工具查询/执行器咨询/小样实验/请求用户/受限未验证/升级，每条带理由；两条纪律自检（常规专业问题不转交用户、必要介入不省略）；未知领域与声明冲突的受限处理。

`diagnose --fixture` 用真实 HTML 文件做**结构观察**，再处理 Mock 固定响应；它不证明视觉效果、交互行为或模型诊断正确。真实调用使用官方模型 ID `deepseek-flash`，需提供 `DEEPSEEK_API_KEY` 且显式加 `--real`。可在项目根目录新建 `.env`，写入单独一行 `DEEPSEEK_API_KEY=你的密钥`；程序会读取这个文件，无需额外安装依赖。若系统环境变量中已定义同名键，则它优先（即使为空）。`.env` 已被 Git 忽略，但仍需仅保存在可信电脑上，切勿提交或分享。`--real` 会发送页面结构、目标、反馈与领域规则至 `api.deepseek.com`；单次命令至多两次请求，每次输出上限 2000 token、输入文本上限 25000 字符。请只对已核对的数据执行。

`observe-page` 需要本机 Chrome 或 Edge，将桌面 1280×800 与移动 390×844 的初始截图及 `observation.json` 保存至 `runtime/observations/`。它不调用模型，不执行点击、表单提交、对比度或自动溢出检测。P3.1 Codex CLI Adapter 见 [mentis/executors/codex_cli.py](mentis/executors/codex_cli.py)；需要本机 Codex CLI 已登录，写入任务应在独立 Git 工作区运行。真实隔离探针、能力矩阵和限制见 [P3.1 记录](docs/progress/P3.1.md)。

## 开发检查

```bash
py -3.14 -m unittest discover -s tests -v
```

当前 57 项：入口、契约不变量、能力资源与分流、P2.2 网关/观察/诊断、P3.1 Adapter 与截图边界。Mock HTTP 响应只验证请求格式、用量采集与记录边界；真实调用证据另见阶段报告。

## 目录

```
AGENTS.md            Agent 协作约定（含每轮可观察交付要求，第 6 节）
docs/                规划、开发计划、状态、决定记录、案例、阶段报告
mentis/              contracts / session / persist / export / demo_contract
                     capabilities/：registry 资源加载、selection 按需选择、routing 缺口分流
                     models/：DeepSeek Model Gateway；observation / diagnosis：候选与结构观察
                     executors/：Codex CLI Adapter；browser_observation：本地双视口截图
resources/           首领域资源（草案）：registry.json + web_pilot/ 三份
tests/               入口、契约不变量、能力与分流检查
runtime/             默认忽略；演示与会话数据
```

## 文档入口

- [开发计划](docs/DEVELOPMENT_PLAN.md)：22 个工作包的真实执行顺序（不按编号排序）。
- [领域策略](docs/DOMAIN_STRATEGY.md)：首试点（小型网页界面与交互，proposed）与领域能力组合。
- [案例](docs/cases/README.md)：网页试点反馈链案例（草案）与文档问答回归草案。
