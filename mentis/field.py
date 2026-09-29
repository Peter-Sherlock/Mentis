"""P5.2a 早期真实使用记录：把既有 CLI 包成一次可归档的真实使用会话。

纪律：
- 只记录不制造：没有真实用户操作就没有使用记录；开发者的机制冒烟单独标注，
  不冒充使用证据，不用模拟对话填充。
- 研究者提示（开发者提供的入口命令与说明文本）与系统贡献（运行记录中带
  provenance 的行为）在档案中分列。
- 不扩建界面：CLI 即入口；不推断普遍收益；反馈绑定 P1.2 契约的尝试记录。
"""

import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from mentis.persist import save_session, session_from_dict

FIELD_DIR = Path("runtime") / "field"


class _Tee(io.TextIOBase):
    """stdout 同时写控制台与会话日志（系统行为按实际输出留痕）。"""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, text):
        self._stream.write(text)
        self._stream.flush()
        self._log.write(text)
        self._log.flush()
        return len(text)

    def flush(self):
        self._stream.flush()
        self._log.flush()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    def isatty(self):
        return bool(getattr(self._stream, "isatty", lambda: False)())


class FieldSession:
    """一次真实使用会话的归档目录：命令、系统输出、耗时、阻塞与用户原话。"""

    def __init__(self, label: str = "", base_dir=None):
        self.base_dir = Path(base_dir) if base_dir else FIELD_DIR
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + os.urandom(2).hex()
        self.dir = self.base_dir / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.label = label or self.session_id
        self.started_at = datetime.now()
        self.command = ""
        self._log = (self.dir / "session-log.txt").open("a", encoding="utf-8")
        self._old_stdout = None
        self.event("会话开始", f"性质：项目负责人自用（P5.2a）；记录由 CLI 自动采集+用户原话")

    def event(self, kind: str, text: str = ""):
        stamp = datetime.now().isoformat(timespec="seconds")
        self._log.write(f"[{stamp}] [{kind}] {text}".rstrip() + "\n")
        self._log.flush()

    def begin_capture(self):
        self._old_stdout = sys.stdout
        sys.stdout = _Tee(self._old_stdout, self._log)

    def end_capture(self):
        if self._old_stdout is not None:
            sys.stdout = self._old_stdout
            self._old_stdout = None

    def finish(self, run_dir: Path | None, status: str, mode: str) -> str:
        """收尾：记录耗时、生成使用记录档案。反馈由用户提交后由 record_feedback 追加。"""
        self.end_capture()
        ended = datetime.now()
        elapsed = (ended - self.started_at).total_seconds()
        self.event("会话结束", f"状态 {status}；耗时 {elapsed:.1f}s")
        self._log.close()
        record = self._usage_record(run_dir, status, mode, ended, elapsed)
        (self.dir / "usage-record.md").write_text(record, encoding="utf-8")
        print(f"[使用记录] {(self.dir / 'usage-record.md').resolve()}")
        return record

    def _usage_record(self, run_dir: Path | None, status: str, mode: str,
                      ended: datetime, elapsed: float) -> str:
        lines = [f"# 使用记录 {self.session_id}（P5.2a 早期真实使用）", ""]
        lines.append(f"- 会话标签：{self.label}；性质：**项目负责人自用**；"
                     "本记录由 CLI 包装自动采集，反馈为用户逐字原话，无模拟对话")
        lines.append(f"- 研究者提示（开发者提供的固定入口，非系统自主行为）：`{self.command}`")
        lines.append(f"- 会话时间：{self.started_at.isoformat(timespec='seconds')} → "
                     f"{ended.isoformat(timespec='seconds')}（总耗时 {elapsed:.1f} 秒）；结束状态 **{status}**")
        if status == "SESSION_ERROR":
            lines.append("- **本会话因异常提前结束：记录不完整**（半份日志如实保留，"
                         "不补写未发生事件；系统已执行部分见下方日志入口）")
        lines.append("")
        lines.append("## 实际操作（自动采集）")
        lines.append(f"- `{self.command}` @ {self.started_at.isoformat(timespec='seconds')}")
        lines.append("- 后续手动操作由操作者补记或见 session-log.txt 逐条输出")
        lines.append("")
        lines.append("## 系统行为（来自运行记录；来源标注见 report.md 看依据）")
        lines += self._system_behavior_lines(run_dir)
        lines.append("")
        lines.append("## 阻塞与等待")
        lines += self._blocking_lines(run_dir)
        lines.append("")
        lines.append("## 用户原话")
        lines.append("- （尚未提交；可稍后用 `field-feedback` 补记，逐字入档）")
        lines.append("")
        lines.append("## 证据入口（真实产物，本轮未修改）")
        lines.append(f"- 本会话日志：`{(self.dir / 'session-log.txt').resolve()}`"
                     "（异常收尾后仍可查看系统已产生的输出）")
        if run_dir is not None and Path(run_dir).is_dir():
            lines.append(f"- 运行目录：`{Path(run_dir).resolve()}`"
                         "（report.md 含 看结果/看变化/看依据）")
        else:
            lines.append("- 无关联运行目录")
        lines.append("")
        return "\n".join(lines)

    def _system_behavior_lines(self, run_dir) -> list:
        if run_dir is None or not Path(run_dir, "state.json").is_file():
            return ["- 无运行记录（未产生委托）"]
        meta = json.loads(Path(run_dir, "state.json").read_text(encoding="utf-8"))
        lines = [f"- 目标：{meta.get('goal', '')}；模式 {meta.get('mode', '')}"]
        for r in meta.get("codex_runs", []):
            lines.append(f"- 委托 {r.get('action_key')}（{r.get('attempt_id')}，状态 "
                         f"{r.get('status')}，时长 {r.get('duration_s')}s，用量 {r.get('usage') or '未取得'}）")
        for r in meta.get("rounds", []):
            lines.append(f"- 修订第 {r.get('round_no')} 轮：执行器 {r.get('codex_status')}"
                         f"（{r.get('codex_duration_s')}s）；结局 {r.get('outcome')}；判定 {r.get('verdict') or '（未复检）'}")
        for route in meta.get("question_routes", []) + meta.get("decision_routes", []):
            lines.append(f"- 决策分流：{str(route.get('question', ''))[:60]} → {route.get('route')}")
        usage = meta.get("model_usage") or {}
        if usage:
            lines.append(f"- 模型用量：{usage}")
        if meta.get("currency_cap"):
            lines.append(f"- 费用口径：{meta['currency_cap']}")
        session_file = Path(run_dir) / "session.json"
        if session_file.is_file():
            try:
                session = session_from_dict(json.loads(session_file.read_text(encoding="utf-8")))
                decisions = [ix for ix in session.interactions if ix.kind.value == "request_decision"]
                autonomous = [b for b in session.briefs for i in b.constraints + b.preferences
                              if i.provenance.value == "agent_selected"]
                user_sel = [b for b in session.briefs for i in b.constraints + b.preferences
                            if i.provenance.value == "user_selected"]
                lines.append(f"- 请求决定 {len(decisions)} 条（已答 "
                             f"{sum(1 for ix in decisions if ix.answer_source)} / 待决 "
                             f"{sum(1 for ix in decisions if not ix.answer_source)}）；"
                             f"系统自主取舍（agent_selected）{len(autonomous)} 条；"
                             f"用户选定（user_selected）{len(user_sel)} 条")
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                lines.append(f"- 会话记录解析失败（{type(exc).__name__}）：以 state.json 为准")
        return lines

    def _blocking_lines(self, run_dir) -> list:
        if run_dir is None or not Path(run_dir, "session.json").is_file():
            return ["- 无（未产生委托）"]
        try:
            session = session_from_dict(
                json.loads(Path(run_dir, "session.json").read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError):
            return ["- 会话记录不可解析：无法统计，见 session-log.txt"]
        lines = []
        pending = [ix for ix in session.interactions
                   if ix.kind.value == "request_decision" and not ix.answer_source]
        for ix in pending:
            lines.append(f"- 待决（阻塞依赖动作 {ix.blocking_action}，沉默≠批准）：{ix.message[:80]}")
        if run_dir is not None and Path(run_dir, "state.json").is_file():
            meta = json.loads(Path(run_dir, "state.json").read_text(encoding="utf-8"))
            for r in meta.get("codex_runs", []):
                if r.get("status") in ("unknown", "failed"):
                    lines.append(f"- 执行器 {r.get('status')}：{r.get('action_key')}"
                                 f"（{r.get('error', '')[:100]}）")
        if not lines:
            lines.append("- 无待决与执行器异常")
        return lines


def prompt_feedback(run_dir: Path) -> bool:
    """交互式收集反馈（逐字原话）；回车跳过=稍后用 field-feedback 补记。"""
    print("\n[反馈] 请评价这次结果（原话会逐字记录）。")
    satisfied_raw = input("  整体满意吗？(是/否，直接回车=稍后再说)：").strip()
    if not satisfied_raw:
        print("  已跳过；查看作品后可用 field-feedback 补记。")
        return False
    quote = input("  你的原话（一句话即可，逐字记录）：").strip()
    blocks = input("  有觉得被卡住/被打断/不明白的地方吗？（原话，回车=无）：").strip()
    record_feedback(run_dir, satisfied=satisfied_raw in ("是", "y", "Y", "yes"),
                    quote=quote, blocks=blocks, operation="field-start 会话内提交")
    return True


def record_feedback(run_dir: Path, satisfied: bool, quote: str, blocks: str = "",
                    operation: str = "查看作品与报告后提交") -> dict:
    """把用户逐字反馈写入会话（P1.2 契约）并归档到运行目录。

    无已完成尝试（如待决中的运行）时不伪造契约记录，反馈只入档案并明确标注。
    """
    run_dir = Path(run_dir)
    session_file = run_dir / "session.json"
    if not session_file.is_file():
        raise ValueError(f"运行目录缺少 session.json：{run_dir}")
    session = session_from_dict(json.loads(session_file.read_text(encoding="utf-8")))
    attempts = [a for a in session.attempts if a.status.value in ("completed", "unknown")]
    bound_attempt = attempts[-1].attempt_id if attempts else None
    contract_note = ""
    if bound_attempt:
        session.record_user_feedback(bound_attempt, satisfied=satisfied, note=quote)
        save_session(session, str(session_file))
    else:
        contract_note = "（该运行没有已完成尝试：按 P1.2 契约不绑定尝试，仅入档案）"
    stamp = datetime.now().isoformat(timespec="seconds")
    entry = {
        "at": stamp, "satisfied": satisfied, "quote": quote, "blocks": blocks,
        "operation": operation, "bound_attempt": bound_attempt or "（无）",
    }
    feedback_file = run_dir / "field-feedback.md"
    lines = [f"## 用户反馈 @ {stamp}", ""]
    lines.append(f"- 操作：{operation}{'；' + contract_note if contract_note else ''}"
                 f"（绑定尝试 {entry['bound_attempt']}）")
    lines.append(f"- 整体满意：{'是' if satisfied else '否'}")
    lines.append(f"- 原话（逐字）：「{quote}」")
    if blocks:
        lines.append(f"- 阻塞/打断/不明白（逐字）：「{blocks}」")
    lines.append("")
    with feedback_file.open("a", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")
    print(f"[反馈已入档] {feedback_file.resolve()}"
          + (f"（并写入会话记录 {session_file.resolve()}）" if bound_attempt else "（仅档案，未绑定尝试）"))
    return entry


def run_field_start(goal: str, design: bool = False, direction: str = "",
                    interactive: bool = False, timeout_s: int = 420, max_repairs: int = 1,
                    label: str = "", base_dir=None, collab_base_dir=None,
                    collab_factory=None) -> tuple[str, Path, Path]:
    """自然语言目标 → 一次被记录的真实使用会话。返回 (状态, 会话目录, 运行目录)。

    collab_base_dir 控制运行目录的落点（默认 runtime/collab；测试须隔离）。
    """
    from mentis.collab import COLLAB_DIR, Collaboration

    fs = FieldSession(label=label or "collab", base_dir=base_dir)
    fs.command = (f"field-start --goal \"{goal[:60]}...\"" if len(goal) > 60
                  else f"field-start --goal \"{goal}\"") + (" --design" if design else "")
    fs.event("研究者提示", "入口命令与说明由开发 Agent 提供；系统行为见下方输出")
    fs.begin_capture()
    collab = None
    run_dir = None
    status = "SESSION_ERROR"
    try:
        collab_base = Path(collab_base_dir) if collab_base_dir else COLLAB_DIR
        factory = collab_factory or (lambda: Collaboration(
            goal=goal, mode="design" if design else "autonomous", direction=direction,
            interactive=interactive, max_repairs=max_repairs, timeout_s=timeout_s,
            base_dir=collab_base))
        collab = factory()
        # T2：委托前先给出运行目录与旁观命令（用户可另开终端实时旁观）
        print(f"[运行目录] {collab.run_dir.resolve()}")
        print(f"[实时旁观] 另开终端运行：py -3.14 -m mentis trace --run \"{collab.run_dir.resolve()}\" --follow")
        status = collab.run()
        run_dir = collab.run_dir
        index = collab.workspace / "index.html"
        print("\n[三个入口]")
        print(f"- 看结果：{index.resolve()}（{'存在' if index.is_file() else '未生成'}，"
              f"可直接用浏览器打开）")
        if status == "DELIVERED":
            print("- [初稿说明] DELIVERED = 结构检查通过、初稿可查看；≠最终完成或满意；"
                  "视觉与表单交互未验证；有意见直接说，会形成下一轮修改")
        print(f"- 看变化/看依据：{(run_dir / 'report.md').resolve()}")
        if status == "PENDING":
            state = (run_dir / "state.json").resolve()
            pending = [ix for ix in collab.session.interactions
                       if ix.kind.value == "request_decision" and not ix.answer_source]
            for ix in pending:
                print(f"- [需要你决定] {ix.interaction_id}：{ix.message}")
            print(f"- [继续] py -3.14 -m mentis collab-resume --state \"{state}\" "
                  "--answer \"<请求ID>:<你的回答>\"")
        if interactive:
            prompt_feedback(run_dir)
    except KeyboardInterrupt:
        fs.event("会话中断", "用户中断（Ctrl+C）；会话按异常收尾：记录不完整，不补写未发生事件")
        run_dir = getattr(collab, "run_dir", run_dir)
        status = "SESSION_ERROR"
    except Exception as exc:
        # T3：现场会话异常也收尾——半份日志标不完整并保留日志入口；不补写未发生事件
        fs.event("会话异常", f"{type(exc).__name__}：{str(exc)[:200]}；"
                 "会话按异常收尾：记录不完整（系统已执行部分见 session-log.txt）")
        run_dir = getattr(collab, "run_dir", run_dir)
        status = "SESSION_ERROR"
    fs.finish(run_dir, status, "design" if design else "autonomous")
    return status, fs.dir, run_dir


def run_field_feedback(run_dir: Path, satisfied: bool, quote: str, blocks: str = "",
                       operation: str = "", label: str = "", base_dir=None) -> dict:
    """查看后的补记反馈：逐字入档并绑定该运行的会话记录。"""
    fs = FieldSession(label=label or "feedback", base_dir=base_dir)
    fs.command = f"field-feedback --run {Path(run_dir).name} --satisfied {'yes' if satisfied else 'no'}"
    fs.event("操作", fs.command)
    try:
        if not quote.strip():
            raise ValueError("反馈原话不能为空（P5.2a 只收逐字原话，不用代述填充）")
        entry = record_feedback(run_dir, satisfied=satisfied, quote=quote.strip(),
                                blocks=blocks.strip(),
                                operation=operation.strip() or "查看作品与报告后提交")
        fs.event("用户原话", f"满意={'是' if satisfied else '否'}；「{quote.strip()[:120]}」")
    except Exception as exc:
        # T3：反馈会话异常同样收尾落盘，不留未收尾的半份会话
        fs.event("会话异常", f"{type(exc).__name__}：{str(exc)[:160]}；反馈未记录，会话按异常收尾")
        fs.finish(Path(run_dir), "SESSION_ERROR", "feedback")
        raise
    fs.finish(Path(run_dir), "FEEDBACK_RECORDED", "feedback")
    return entry
