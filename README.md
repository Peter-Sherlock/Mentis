# Mentis

代表非专业用户与执行型 Agent 持续协作的代理：帮助形成目标，决定下一步，委托工作，观察实际产物，诊断问题并给出有效修改指导。

**当前状态：P1.1 最小工程起点。** 入口未接入模型、执行器与存储，不假装完整系统。开发位置与证据边界见 [docs/DEVELOPMENT_STATUS.md](docs/DEVELOPMENT_STATUS.md)；案例与评价材料为 AI 草案，未经独立人工复核。

## 运行（无第三方依赖，无需安装）

要求 Python ≥ 3.9（本机验证版本 3.14.7）。在本目录执行：

```bash
python -m mentis             # 启动信息：版本、当前阶段
python -m mentis --version   # 仅版本号
```

## 开发检查

```bash
python -m unittest discover -s tests -v
```

## 目录

```
AGENTS.md            Agent 协作约定（含每轮可观察交付要求，第 6 节）
docs/                规划、开发计划、状态、决定记录、案例、阶段报告
mentis/              Python 包；当前只有入口（结构按需增长，见 docs/PROJECT_PLAN.md §21.3）
tests/               当前不变量与关键行为检查
runtime/             默认忽略；后续存放项目状态与真实产物
```

## 文档入口

- [开发计划](docs/DEVELOPMENT_PLAN.md)：22 个工作包的真实执行顺序（不按编号排序）。
- [领域策略](docs/DOMAIN_STRATEGY.md)：首试点（小型网页界面与交互，proposed）与领域能力组合。
- [案例](docs/cases/README.md)：网页试点反馈链案例（草案）与文档问答回归草案。
