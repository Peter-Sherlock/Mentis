# E2 运行与复核入口

本文件供下一包的运行负责人使用。本轮准备核验不调用真实模型/执行器；正式20臂数据仍未产生。

## 运行前

使用普通本机权限的PowerShell；核对Codex登录与额度、DeepSeek凭据、Chrome、Windows ACL恢复能力。凭据只核对可用性，不打印、不写入契约。正式开窗前确认没有旧窗口/写入者。

干净提交上冻结方法，再生成新的相同输入副本（已有目录拒绝覆盖）：

```powershell
py -3.14 -X utf8 -m mentis eval-freeze --out runtime/evaluation/e2-run-spec.json
py -3.14 -X utf8 -m mentis eval-prepare --out runtime/evaluation/e2-prepared
```

契约包含实际commit与协议版本；源代码/领域资源漂移后必须重新提交、修订协议并冻结，不编辑契约伪造绑定。此处不会发送数据。

## 单臂串行运行

按PROTOCOL §1顺序依次执行，示例是首臂；probe会真实调用Codex只读探针。

```powershell
py -3.14 -X utf8 -m mentis eval-isolate probe --prepared runtime/evaluation/e2-prepared --arm input-001/B-0 --run-spec runtime/evaluation/e2-run-spec.json
py -3.14 -X utf8 -m mentis eval-run --prepared runtime/evaluation/e2-prepared --arm input-001/B-0 --evidence runtime/evaluation/e2-prepared/isolation/isolation-evidence.json --suite-sha 7fe7365cab0cb12e02518e4055f95a1b9d8c18cae47d43380ba32cadd0a76787 --run-spec runtime/evaluation/e2-run-spec.json --out runtime/evaluation/e2-runs
```

运行目录由输出中的run_id定位；不要重用目录。返回 `needs_user` 时先读pending问题；受控回答只在请求后发送，回答原话由独立评价侧事先保有，不能把答案表存进受测可读目录。

```powershell
py -3.14 -X utf8 -m mentis eval-answer --run <运行目录> --answer "<该问题的受控回答原话>" --answered-by "<控制器或核对人>" --source controlled_simulation
```

已有确认的案例直接复用上下文。运行器当前只处理标注化日期冲突的受控回答；其它问题保持未验证，记录为方法限制。

unknown不会自动重发。先确认无活跃写入者，再检查作品和原委托；机制还会执行CIM停写核对。确认需续办时：

```powershell
py -3.14 -X utf8 -m mentis eval-continue --run <运行目录> --checked-by "<核对人>" --basis "<实际看过的进程、文件及未完事项>"
```

若作品已完成，只加 `--artifact-complete` 留档，不发送新委托，原执行状态仍unknown。预算耗尽/无原会话/检查点漂移/进程查询失败均拒绝续办。续办unknown或failed后终止。

核对执行器确已停止再释放窗口；释放失败不开始下一臂：

```powershell
py -3.14 -X utf8 -m mentis eval-isolate release --prepared runtime/evaluation/e2-prepared
```

## 看结果、变化与依据

- 结果：运行目录的 `versions/final-*.html`（不可覆盖快照），当前工作区作品另见manifest的workspace_path。
- 变化：`versions/initial.html`（有共同首稿的案例）、`observation/review-first/page-snapshot.html`与final快照；续办前状态在 `history/`。
- 依据：`material-provenance.json`、evaluation-plan、trace/prompts与events、run-manifest中的候选/预算/模型请求/核对记录。内部记录只供过程评价。

## 评价端汇总

```powershell
py -3.14 -X utf8 -m mentis eval-summary --runs runtime/evaluation/e2-runs --out runtime/evaluation/e2-review
py -3.14 -X utf8 -m mentis eval-guard-report --review runtime/evaluation/e2-review/guard-review.json
```

comparison.csv列出所有20臂和缺失；额外重跑不静默择优。blind-artifacts目录只给作品复核者；private-map、trace及预算保留在评价侧。先按order.txt盲评，再做带标签过程评。guard-review需独立人工标签、姓名和依据；未标注时率为null。e2_candidate只表示运行契约资格，效果仍pending_human。

## 开发验证

```powershell
py -3.14 -X utf8 -m unittest discover -s tests -q
$env:MENTIS_BROWSER_INTEGRATION = '1'
py -3.14 -X utf8 -m unittest tests.integration.test_browser -v
Remove-Item Env:MENTIS_BROWSER_INTEGRATION
```

第一项为无模型/执行器真实调用的回归，含临时Git、ACL、假进程演练；默认跳过真实浏览器1项。第二项使用真实Chrome验证冻结页双视口测量/截图，仍无模型调用。测试失败不能靠修改验收断言或重判unknown为成功处理。
