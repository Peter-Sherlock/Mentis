# Mentis

代表非专业用户与执行型 Agent 持续协作的代理：帮助形成目标，决定下一步，委托工作，观察实际产物，诊断问题并给出有效修改指导。

**当前状态：P2.2 partial。** 契约、领域资源与缺口分流，以及页面结构观察和模型诊断候选链路已离线验证。真实 DeepSeek 调用尚未执行，执行器与持久化存储未接入。开发位置与证据边界见 [docs/DEVELOPMENT_STATUS.md](docs/DEVELOPMENT_STATUS.md)；案例与评价材料仍为 AI 草案。

## 运行（无第三方依赖，无需安装）

要求 Python ≥ 3.10。本机 `python` 当前指向 Anaconda Python 3.7.3；Windows 上使用 `py -3.14`（本机版本 3.14.7）。为正确显示中文及 `¥`，PowerShell 先设置 `$env:PYTHONIOENCODING='utf-8'`。在本目录执行：

```bash
py -3.14 -m mentis                # 入口说明
py -3.14 -m mentis --version      # 仅版本号
py -3.14 -m mentis demo-contract  # 脚本化契约演示，页面质量未验证
py -3.14 -m mentis demo-routing   # 确定性的资源选择与缺口分流演示
py -3.14 -m mentis diagnose --page runtime/demo/event_page_v1.html --fixture tests/fixtures/mock_diagnosis_response.json
```

`demo-routing` 展示：三份网页试点资源（信息组织/视觉表达/基础交互，规则分硬要求/建议/偏好/待验证假设四类）按任务需要加载；七类缺口分流到自决/工具查询/执行器咨询/小样实验/请求用户/受限未验证/升级，每条带理由；两条纪律自检（常规专业问题不转交用户、必要介入不省略）；未知领域与声明冲突的受限处理。

`diagnose --fixture` 用真实 HTML 文件做**结构观察**，再处理 Mock 固定响应；它不证明视觉效果、交互行为或模型诊断正确。真实调用使用官方模型 ID `deepseek-flash`，需环境变量 `DEEPSEEK_API_KEY` 且显式加 `--real`。这会发送页面结构、目标、反馈与领域规则至 `api.deepseek.com`；单次命令至多两次请求，每次输出上限 2000 token、输入文本上限 25000 字符。请只对已核对的数据执行。

## 开发检查

```bash
py -3.14 -m unittest discover -s tests -v
```

当前 49 项：入口、契约不变量、能力资源与分流、P2.2 网关/观察/诊断检查。Mock HTTP 响应只验证请求格式、用量采集与记录边界，不计真实模型调用。

## 目录

```
AGENTS.md            Agent 协作约定（含每轮可观察交付要求，第 6 节）
docs/                规划、开发计划、状态、决定记录、案例、阶段报告
mentis/              contracts / session / persist / export / demo_contract
                     capabilities/：registry 资源加载、selection 按需选择、routing 缺口分流
                     models/：DeepSeek Model Gateway；observation / diagnosis：候选与结构观察
resources/           首领域资源（草案）：registry.json + web_pilot/ 三份
tests/               入口、契约不变量、能力与分流检查
runtime/             默认忽略；演示与会话数据
```

## 文档入口

- [开发计划](docs/DEVELOPMENT_PLAN.md)：22 个工作包的真实执行顺序（不按编号排序）。
- [领域策略](docs/DOMAIN_STRATEGY.md)：首试点（小型网页界面与交互，proposed）与领域能力组合。
- [案例](docs/cases/README.md)：网页试点反馈链案例（草案）与文档问答回归草案。
