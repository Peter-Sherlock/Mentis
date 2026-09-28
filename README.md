# Mentis

代表非专业用户与执行型 Agent 持续协作的代理：帮助形成目标，决定下一步，委托工作，观察实际产物，诊断问题并给出有效修改指导。

**当前状态：P1.2 轻量任务与反馈契约。** 数据对象、不变量与导出已实现并测试；未接入真实模型、执行器与持久化存储。开发位置与证据边界见 [docs/DEVELOPMENT_STATUS.md](docs/DEVELOPMENT_STATUS.md)；案例与评价材料为 AI 草案，未经独立人工复核。

## 运行（无第三方依赖，无需安装）

要求 Python ≥ 3.10（本机验证版本 3.14.7）。在本目录执行：

```bash
python -m mentis                # 入口说明：版本、当前阶段、可用命令
python -m mentis --version      # 仅版本号
python -m mentis demo-contract  # 最小任务与反馈契约演示（脚本化数据，含拒绝路径）
```

`demo-contract` 会在 `runtime/demo/` 下真实生成样例页面与两份记录文件（`session_demo.json`、`session_demo.md`），并在控制台打印同样的可读导出；内容包括：来源标注的目标/偏好、从用户指令推导的委托策略、通知与请求决定的区别、两条拒绝路径（沉默不批准、旧授权不批新范围）、"完成 ≠ 检查通过 ≠ 用户满意"。

## 开发检查

```bash
python -m unittest discover -s tests -v
```

16 项：入口 3 项 + 契约不变量 13 项（通知不阻塞、请求只阻塞依赖动作、缺授权拒绝、新范围需新授权、撤回后拒绝、沉默不构成批准、无检查不接受、接受与用户满意分离、过期版本拒收、未知状态不自动重发、修订链可回查、序列化往返）。

## 目录

```
AGENTS.md            Agent 协作约定（含每轮可观察交付要求，第 6 节）
docs/                规划、开发计划、状态、决定记录、案例、阶段报告
mentis/              contracts.py 对象与语义 / session.py 不变量操作 /
                     persist.py 序列化 / export.py 可读导出 / demo_contract.py 演示
tests/               入口与契约不变量检查
runtime/             默认忽略；演示与会话数据
```

## 文档入口

- [开发计划](docs/DEVELOPMENT_PLAN.md)：22 个工作包的真实执行顺序（不按编号排序）。
- [领域策略](docs/DOMAIN_STRATEGY.md)：首试点（小型网页界面与交互，proposed）与领域能力组合。
- [案例](docs/cases/README.md)：网页试点反馈链案例（草案）与文档问答回归草案。
