# Mentis

代表非专业用户与执行型 Agent 持续协作的代理：帮助形成目标，决定下一步，委托工作，观察实际产物，诊断问题并给出有效修改指导。

**当前状态：P2.1 首领域资源与行动依据。** 契约、不变量、领域资源与缺口分流已实现并测试；未接入真实模型、执行器与持久化存储。开发位置与证据边界见 [docs/DEVELOPMENT_STATUS.md](docs/DEVELOPMENT_STATUS.md)；案例与评价材料为 AI 草案，未经独立人工复核。

## 运行（无第三方依赖，无需安装）

要求 Python ≥ 3.10（本机验证版本 3.14.7）。在本目录执行：

```bash
python -m mentis                # 入口说明：版本、当前阶段、可用命令
python -m mentis --version      # 仅版本号
python -m mentis demo-contract  # 最小任务与反馈契约演示（脚本化数据，含拒绝路径）
python -m mentis demo-routing   # 首领域资源按需加载与缺口分流演示（确定性规则）
```

`demo-routing` 展示：三份网页试点资源（信息组织/视觉表达/基础交互，规则分硬要求/建议/偏好/待验证假设四类）按任务需要加载；七类缺口分流到自决/工具查询/执行器咨询/小样实验/请求用户/受限未验证/升级，每条带理由；两条纪律自检（常规专业问题不转交用户、必要介入不省略）；未知领域与声明冲突的受限处理。

## 开发检查

```bash
python -m unittest discover -s tests -v
```

31 项：入口 3 + 契约不变量 13 + 能力资源与分流 15。

## 目录

```
AGENTS.md            Agent 协作约定（含每轮可观察交付要求，第 6 节）
docs/                规划、开发计划、状态、决定记录、案例、阶段报告
mentis/              contracts / session / persist / export / demo_contract
                     capabilities/：registry 资源加载、selection 按需选择、routing 缺口分流
resources/           首领域资源（草案）：registry.json + web_pilot/ 三份
tests/               入口、契约不变量、能力与分流检查
runtime/             默认忽略；演示与会话数据
```

## 文档入口

- [开发计划](docs/DEVELOPMENT_PLAN.md)：22 个工作包的真实执行顺序（不按编号排序）。
- [领域策略](docs/DOMAIN_STRATEGY.md)：首试点（小型网页界面与交互，proposed）与领域能力组合。
- [案例](docs/cases/README.md)：网页试点反馈链案例（草案）与文档问答回归草案。
