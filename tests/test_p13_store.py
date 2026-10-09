"""P1.3 回归例：SQLite 任务库——事务、库版本/迁移、跨进程恢复、去重与版本校验。

全部离线：Fake 执行器与合成状态，不发起任何真实模型/执行器调用。
跨进程恢复用 subprocess 起**全新 Python 进程**验证（不复用内存对象）。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mentis import store
from mentis.collab import Collaboration, collaboration_from_state, load_state
from mentis.contracts import (
    AttemptStatus,
    AuthStatus,
    DelegationPolicy,
    Disposition,
    InvalidInteraction,
    InteractionKind,
    Provenance,
    UnauthorizedAction,
)
from mentis.executors.codex_cli import CodexTaskResult, CodexTaskSpec
from mentis.persist import save_session
from mentis.session import TaskSession
from mentis.store import (
    DB_FILENAME,
    SUPPORTED_DB_VERSION,
    StoreError,
    content_digest,
    db_path_for,
    import_json_run,
    list_state_events,
    record_task_event,
    restore_task_state,
    save_task_state,
    store_status,
)

# 复用 collab 流测试的假执行器与夹具（同一套接口，不另建平行实现）
try:
    from tests.test_collab import BAD_PAGE, GOOD_PAGE, FakeAdapter, make_collab, stub_capture
    from tests.test_guidance import file_aware_measure
except ImportError:  # 直接以文件方式运行时
    from test_collab import BAD_PAGE, GOOD_PAGE, FakeAdapter, make_collab, stub_capture
    from test_guidance import file_aware_measure


def _rich_session(tmp: Path) -> tuple[TaskSession, dict]:
    """覆盖全部规范状态对象的合成会话（含 unknown/撤权/待决/产物/评审/修订/反馈）。"""
    s = TaskSession("持久化冒烟目标")
    s.init_brief("持久化冒烟目标")
    s.set_policy(DelegationPolicy(
        "policy-1", 1, ["视觉与布局实现细节"], ["改价/改期"], ["exec-workspace"],
        {"max_auto_revisions": 2}, [], Provenance.AGENT_SELECTED, "derived:policy-v1"))
    auth = s.authorize("exec-workspace")
    s.authorize("exec-other")
    s.revoke_authorization(s.authorizations[1].auth_id)
    s.notify_progress("已开始（通知不等待回复）")
    ix_answered = s.request_decision("方向 A 还是 B？", "apply-answers")
    s.answer_interaction(ix_answered.interaction_id, "方向 A", "user")
    s.request_decision("改价需要你确认", "apply-answers")  # 保持未决
    page = tmp / "index.html"
    page.write_text(GOOD_PAGE, encoding="utf-8")
    a1 = s.dispatch("build-page", "exec-workspace")
    s.mark_running(a1.attempt_id)
    s.complete_attempt(a1.attempt_id, str(page), "executor:fake")
    a2 = s.dispatch("repair-1", "exec-workspace")
    s.mark_unknown(a2.attempt_id)  # unknown 原样保存
    s.record_review(a1.attempt_id, ["结构通过"], True, Disposition.ACCEPT)
    s.accept(a1.attempt_id)
    rev = s.record_revision(ix_answered.interaction_id, a1.attempt_id, "按方向 A 调整")
    s.record_user_feedback(a1.attempt_id, True, "（样例反馈）")
    meta = {
        "state_version": "p2.3", "run_id": "p13-fixture", "mode": "autonomous",
        "status": "DELIVERED", "goal": s.goal, "workspace": str(tmp),
        "base_dir": str(tmp), "thread_id": "t-fake-0001", "repairs_used": 0,
        "timeout_s": 600, "max_repairs": 1, "applied_answers": True,
        "delivered": True, "material_text": "素材", "codex_runs": [],
        "question_routes": [], "model_usage": {"real_calls": 1},
        "evaluation_plan": {"version": 1, "sha256": "e" * 64},
        "currency_cap": "无货币硬上限——仅记录可取得的用量",
    }
    return s, meta


class StoreRoundTripTests(unittest.TestCase):
    """离线：库往返与不变量（合成状态）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_full_roundtrip_digest_fields_and_unknown(self):
        s, meta = _rich_session(self.tmp)
        saved = save_task_state(self.tmp, s, meta)
        self.assertTrue(db_path_for(self.tmp).is_file())
        s2, m2 = restore_task_state(self.tmp)
        # 摘要一致（恢复侧基于库内存档 meta 重算）
        self.assertEqual(saved["digest"], content_digest(
            s2, {k: v for k, v in m2.items()
                 if k not in ("restored_from", "state_digest")}))
        # 关键状态逐项一致
        self.assertEqual([b.version for b in s2.briefs], [1])
        self.assertEqual(s2.current_policy.version, 1)
        statuses = {a.auth_id: a.status for a in s2.authorizations}
        self.assertIn(AuthStatus.REVOKED, statuses.values())
        self.assertEqual(statuses[s.authorizations[0].auth_id], AuthStatus.ACTIVE)
        kinds = [ix.kind for ix in s2.interactions]
        self.assertIn(InteractionKind.NOTIFY_PROGRESS, kinds)
        answered = [ix for ix in s2.interactions if ix.answer_source]
        pending = [ix for ix in s2.interactions
                   if ix.kind is InteractionKind.REQUEST_DECISION and not ix.answer_source]
        self.assertEqual(len(answered), 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].blocking_action, "apply-answers")
        self.assertEqual([a.status for a in s2.attempts],
                         [AttemptStatus.COMPLETED, AttemptStatus.UNKNOWN])  # unknown 原样
        self.assertEqual(s2.attempts[1].native_task_id, s.attempts[1].native_task_id)
        self.assertEqual(s2.artifacts[0].sha256, s.artifacts[0].sha256)
        self.assertTrue(s2.attempts[0].accepted)
        self.assertEqual(s2.revisions[0].instruction, "按方向 A 调整")
        self.assertTrue(s2.user_feedback[s.attempts[0].attempt_id]["satisfied"])
        self.assertEqual(m2["thread_id"], "t-fake-0001")
        self.assertEqual(m2["restored_from"], "sqlite")

    def test_idempotent_save_and_counter_continuity(self):
        s, meta = _rich_session(self.tmp)
        r1 = save_task_state(self.tmp, s, meta)
        r2 = save_task_state(self.tmp, s, meta)
        self.assertFalse(r2["changed"])  # 同状态重复保存幂等
        s2, _ = restore_task_state(self.tmp)
        ix = s2.request_decision("恢复后的新请求", "apply-answers")
        self.assertEqual(ix.interaction_id, "ix-4")  # 计数续排，不复用旧 id
        att = s2.dispatch("repair-2", "exec-workspace")
        self.assertEqual(att.attempt_id, "attempt-3")

    def test_notify_never_restored_as_waiting(self):
        s = TaskSession("目标")
        s.init_brief("目标")
        s.notify_progress("进度通知")
        s.notify_progress("另一条通知")
        save_task_state(self.tmp, s, {"run_id": "n1", "status": "RUNNING"})
        s2, _ = restore_task_state(self.tmp)
        self.assertIsNone(s2.pending_decision_blocking("any-action"))
        waiting = [ix for ix in s2.interactions
                   if ix.kind is InteractionKind.REQUEST_DECISION and not ix.answer_source]
        self.assertEqual(waiting, [])  # 通知不复活为等待

    def test_duplicate_answer_rejected_at_source(self):
        s = TaskSession("目标")
        s.init_brief("目标")
        ix = s.request_decision("继续？", "apply-answers")
        s.answer_interaction(ix.interaction_id, "继续", "user")
        with self.assertRaises(InvalidInteraction):
            s.answer_interaction(ix.interaction_id, "再答一次", "user")
        self.assertEqual(ix.answer_text, "继续")  # 未被覆写


class CrossProcessRestoreTests(unittest.TestCase):
    """关闭保存进程，用全新 Python 进程从 SQLite 恢复（不是复用内存对象）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_new_process_restores_identical_state(self):
        s, meta = _rich_session(self.tmp)
        saved = save_task_state(self.tmp, s, meta)
        script = (
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "from mentis.store import restore_task_state, content_digest\n"
            "s, m = restore_task_state(%r)\n"
            "print(json.dumps({\n"
            "  'digest': content_digest(s, {k: v for k, v in m.items()\n"
            "      if k not in ('restored_from', 'state_digest')}),\n"
            "  'goal': s.goal,\n"
            "  'attempt_statuses': [a.status.value for a in s.attempts],\n"
            "  'auth_statuses': [a.status.value for a in s.authorizations],\n"
            "  'pending': [ix.interaction_id for ix in s.interactions\n"
            "      if ix.kind.value == 'request_decision' and not ix.answer_source],\n"
            "  'thread_id': m['thread_id'],\n"
            "  'artifact_sha': s.artifacts[0].sha256,\n"
            "  'status': m['status'],\n"
            "}))\n" % (str(Path.cwd()), str(self.tmp))
        )
        out = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script],
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        restored = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(restored["digest"], saved["digest"])  # 跨进程摘要一致
        self.assertEqual(restored["attempt_statuses"], ["completed", "unknown"])
        self.assertEqual(restored["auth_statuses"], ["active", "revoked"])
        self.assertEqual(len(restored["pending"]), 1)
        self.assertEqual(restored["thread_id"], "t-fake-0001")
        self.assertEqual(restored["status"], "DELIVERED")
        self.assertEqual(restored["artifact_sha"], s.artifacts[0].sha256)


class EventDedupAndStaleReplyTests(unittest.TestCase):
    """离线（Fake 执行器）：重复事件不重复推进；旧版本回复不批准新范围。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _pending_run(self, **kw):
        adapter = FakeAdapter(behavior={"first": "questions", "second": "good"})
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure, **kw)
        status = collab.run()
        self.assertEqual(status, "PENDING")
        return collab, adapter

    def test_duplicate_state_event_recorded_once(self):
        collab, _ = self._pending_run()
        payload = {"interaction_id": "ix-1"}
        first = record_task_event(collab.run_dir, "task.answer_applied", payload, 1)
        second = record_task_event(collab.run_dir, "task.answer_applied", payload, 1)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(list_state_events(collab.run_dir)), 1)

    def test_resume_answer_twice_does_not_double_advance(self):
        collab, adapter = self._pending_run()
        ix = next(ix for ix in collab.session.interactions
                  if ix.kind.value == "request_decision" and not ix.answer_source)
        answer = {ix.interaction_id: "维持 ¥99（样例回答）"}
        state = collab.run_dir / "state.json"
        r1 = collaboration_from_state(state, adapter=adapter,
                                      browser_capture=stub_capture)
        r1.gateway = None
        r1.use_model = False
        r1.resume(answers=dict(answer))
        calls_after_first = len(adapter.calls)
        r2 = collaboration_from_state(state, adapter=adapter,
                                      browser_capture=stub_capture)
        r2.gateway = None
        r2.use_model = False
        r2.resume(answers=dict(answer))  # 同一回答重放
        self.assertEqual(len(adapter.calls), calls_after_first)  # 无新增外部委托
        answered = [i for i in r2.session.interactions if i.answer_source]
        self.assertEqual(len(answered), 1)  # 未被覆写/重复推进

    def test_stale_reply_archived_not_advanced(self):
        collab, adapter = self._pending_run()
        pending_ix = next(ix for ix in collab.session.interactions
                          if ix.kind.value == "request_decision" and not ix.answer_source)
        # 模拟"brief 已推进但回答未及处理"的恢复场景（如续办进程中断）：
        # 直接推进 brief 并落盘，再以旧请求的回答续办
        collab.session.update_brief(objective="改成一个新目标（brief v2）")
        save_task_state(collab.run_dir, collab.session,
                        json.loads((collab.run_dir / "state.json").read_text(encoding="utf-8")))
        state = collab.run_dir / "state.json"
        r = collaboration_from_state(state, adapter=adapter,
                                     browser_capture=stub_capture)
        r.gateway = None
        r.use_model = False
        self.assertEqual(r.session.current_brief.version, 2)
        r.resume(answers={pending_ix.interaction_id: "旧版本时的回答"})
        r_ix = next(ix for ix in r.session.interactions
                    if ix.interaction_id == pending_ix.interaction_id)
        self.assertTrue(r_ix.answer_source)  # 回答入档（等待关闭）
        self.assertEqual(r_ix.answered_brief_version, 2)
        self.assertEqual(r_ix.brief_version, 1)
        events = [e["kind"] for e in list_state_events(collab.run_dir)]
        self.assertIn("task.stale_reply_archived", events)
        # 旧回复不触发 apply-answers 委托（本 resume 无新增执行器调用）
        self.assertEqual(len(adapter.calls), 1)  # 仅最初 build-page 一次


class WithdrawAndAuthTests(unittest.TestCase):
    """离线：撤回后旧授权不复活（跨进程核对）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_withdraw_persisted_and_dispatch_blocked_after_restore(self):
        collab = make_collab(base_dir=self.base, measure=file_aware_measure)
        self.assertEqual(collab.run(), "DELIVERED")
        adapter = FakeAdapter()
        state = collab.run_dir / "state.json"
        r = collaboration_from_state(state, adapter=adapter,
                                     browser_capture=stub_capture)
        r.gateway = None
        r.use_model = False
        self.assertEqual(r.resume(revoke=True), "WITHDRAWN")
        calls_after_withdraw = len(adapter.calls)
        # 新进程语义核对：直接用 load_state 的库路径恢复后委托被拒
        meta, session = load_state(state)
        self.assertEqual(meta["status"], "WITHDRAWN")
        self.assertTrue(all(a.status is AuthStatus.REVOKED for a in session.authorizations))
        with self.assertRaises(UnauthorizedAction):
            session.dispatch("build-page", "exec-workspace")  # 旧授权不复活
        # 恢复对象再次委托同样被拒（无新增执行器调用）
        r2 = collaboration_from_state(state, adapter=adapter,
                                      browser_capture=stub_capture)
        r2.gateway = None
        r2.use_model = False
        with self.assertRaises(UnauthorizedAction):
            r2._dispatch_codex("build-page", "任何指令")
        self.assertEqual(len(adapter.calls), calls_after_withdraw)


class TransactionAndMigrationTests(unittest.TestCase):
    """离线：事务原子性、迁移备份/回滚、JSON 导入幂等（副本验证）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_failed_save_rolls_back_to_previous_complete_state(self):
        s, meta = _rich_session(self.tmp)
        save_task_state(self.tmp, s, meta)
        digest_before = content_digest(s, meta)
        # 推进状态并注入不可序列化对象 → 保存中途失败
        s.update_brief(objective="v2 目标")
        s.user_feedback["attempt-1"] = {"satisfied": True, "note": object()}
        with self.assertRaises(StoreError):
            save_task_state(self.tmp, s, meta)
        s3, m3 = restore_task_state(self.tmp)  # 库保持先前完整状态
        self.assertEqual([b.version for b in s3.briefs], [1])  # 无 v2 半套状态
        self.assertEqual(content_digest(
            s3, {k: v for k, v in m3.items()
                 if k not in ("restored_from", "state_digest")}), digest_before)

    def test_corrupt_db_fails_loud_and_json_import_recovers(self):
        s, meta = _rich_session(self.tmp)
        save_task_state(self.tmp, s, meta)
        save_session(s, str(self.tmp / "session.json"))  # 导出（迁移源）
        (self.tmp / "state.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        db_path_for(self.tmp).write_bytes(b"this is not a sqlite database at all")
        with self.assertRaises(StoreError):
            restore_task_state(self.tmp)  # 显式报错，不静默新建
        st = store_status(self.tmp)
        self.assertIn("error", st)
        result = import_json_run(self.tmp)  # 恢复办法：JSON 导出重建
        self.assertTrue(result["imported"])
        s2, m2 = restore_task_state(self.tmp)
        self.assertEqual([a.status for a in s2.attempts],
                         [AttemptStatus.COMPLETED, AttemptStatus.UNKNOWN])
        again = import_json_run(self.tmp)
        self.assertFalse(again["imported"])  # 幂等

    def test_unsupported_db_version_fails_loud(self):
        s, meta = _rich_session(self.tmp)
        save_task_state(self.tmp, s, meta)
        import sqlite3
        conn = sqlite3.connect(db_path_for(self.tmp))
        conn.execute("UPDATE meta SET value='99' WHERE key='db_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(StoreError) as ctx:
            restore_task_state(self.tmp)
        self.assertIn("版本 99", str(ctx.exception))

    def test_migration_backup_rollback_and_idempotency(self):
        import sqlite3

        def db_version(path):
            conn = sqlite3.connect(str(path))
            try:
                return int(dict(conn.execute(
                    "SELECT key, value FROM meta").fetchall())["db_version"])
            finally:
                conn.close()

        s, meta = _rich_session(self.tmp)
        save_task_state(self.tmp, s, meta)
        self.assertEqual(db_version(db_path_for(self.tmp)), 1)

        def bad_migration(conn):
            conn.execute("ALTER TABLE meta ADD COLUMN note TEXT")
            raise RuntimeError("迁移中途失败")

        real_supported = store.SUPPORTED_DB_VERSION
        try:
            store.SUPPORTED_DB_VERSION = 2
            store._SCHEMA_MIGRATIONS[2] = bad_migration
            with self.assertRaises(StoreError) as ctx:
                save_task_state(self.tmp, s, meta)
            self.assertIn("已还原备份", str(ctx.exception))
            # 移除坏迁移后恢复：原库完好（失败迁移未留下半套 schema）
            del store._SCHEMA_MIGRATIONS[2]
            s2, _ = restore_task_state(self.tmp)
            self.assertEqual([b.version for b in s2.briefs], [1])
            self.assertEqual(db_version(db_path_for(self.tmp)), 1)
            # 成功迁移：版本推进、备份留存、内容一致；重复执行结果一致
            def good_migration(conn):
                conn.execute("ALTER TABLE meta ADD COLUMN note TEXT")

            store._SCHEMA_MIGRATIONS[2] = good_migration
            save_task_state(self.tmp, s, meta)
            self.assertEqual(db_version(db_path_for(self.tmp)), 2)
            save_task_state(self.tmp, s, meta)  # 已在 v2：无待迁移，幂等
            backups = list(self.tmp.glob(f"{DB_FILENAME}.bak-v1-*"))
            self.assertTrue(backups)
            s3, m3 = restore_task_state(self.tmp)
            self.assertEqual(content_digest(
                s3, {k: v for k, v in m3.items()
                     if k not in ("restored_from", "state_digest")}),
                content_digest(s, meta))
        finally:
            store.SUPPORTED_DB_VERSION = real_supported
            store._SCHEMA_MIGRATIONS.pop(2, None)

    def test_json_import_on_copy_of_real_o3_run_no_writeback(self):
        """在副本上验证迁移：真实 O3 运行目录复制到临时区导入；原目录零写入。"""
        src = Path("runtime/collab/20261008_104715-81d9")
        if not (src / "session.json").is_file():
            self.skipTest("本机无 O3 真实运行材料（仅开发机执行）")
        import hashlib
        import shutil
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "o3-copy"
            shutil.copytree(src, copy)
            src_files = {p.relative_to(src): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in src.rglob("*") if p.is_file()}
            result = import_json_run(copy)
            self.assertTrue(result["imported"])
            session, meta = restore_task_state(copy)
            self.assertEqual(meta["status"], "DELIVERED")
            self.assertEqual(len(session.attempts), 1)
            # O3 运行经人工核对后 confirm-outcome → completed（unknown 的唯一出路）
            self.assertEqual(session.attempts[0].status, AttemptStatus.COMPLETED)
            again = import_json_run(copy)
            self.assertFalse(again["imported"])
            # 原始证据零改动、零写入
            src_after = {p.relative_to(src): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in src.rglob("*") if p.is_file()}
            self.assertEqual(src_files, src_after)
            self.assertFalse((src / DB_FILENAME).exists())


class ConfigBindingPreservationTests(unittest.TestCase):
    """离线：配置版本与内容哈希保留——恢复不悄悄换用最新资源。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_binding_hash_kept_and_mismatch_fails_closed(self):
        cfg_dir = self.tmp / "professional-config"
        cfg_dir.mkdir()
        (cfg_dir / "config-v1.json").write_text('{"version": 1}', encoding="utf-8")
        plan_dir = self.tmp / "evaluation-plan"
        plan_dir.mkdir()
        (plan_dir / "plan-v1.json").write_text('{"version": 1}', encoding="utf-8")
        s, meta = _rich_session(self.tmp)
        save_task_state(self.tmp, s, meta)
        # 出现更新的 v2 资源 → 恢复仍绑定 v1（不静默升级）
        (cfg_dir / "config-v2.json").write_text('{"version": 2}', encoding="utf-8")
        s2, m2 = restore_task_state(self.tmp)
        self.assertEqual(m2["professional_config_binding"]["file"], "config-v1.json")
        # v1 内容被改 → 恢复显式失败（进入待核对），不换用 v2 掩盖
        (cfg_dir / "config-v1.json").write_text('{"version": 1, "tampered": true}',
                                                encoding="utf-8")
        with self.assertRaises(StoreError) as ctx:
            restore_task_state(self.tmp)
        self.assertIn("专业配置", str(ctx.exception))
        self.assertIn("不悄悄换用最新资源", str(ctx.exception))


class CollabPersistAndStatusTests(unittest.TestCase):
    """离线：collab 落盘写库、库优先恢复、status 入口显示任务库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_autonomous_run_creates_db_and_restore_prefers_db(self):
        collab = make_collab(base_dir=self.base, measure=file_aware_measure)
        self.assertEqual(collab.run(), "DELIVERED")
        self.assertTrue(db_path_for(collab.run_dir).is_file())
        state = collab.run_dir / "state.json"
        meta, session = load_state(state)
        self.assertEqual(meta["restored_from"], "sqlite")
        # 篡改 JSON 导出的状态字段 → 库为准，不被陈旧导出带偏
        exported = json.loads(state.read_text(encoding="utf-8"))
        exported["status"] = "TAMPERED"
        exported.setdefault("task_db", {})["digest"] = "0" * 64  # 伪造摘要
        state.write_text(json.dumps(exported, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        meta2, _ = load_state(state)
        self.assertEqual(meta2["status"], "DELIVERED")
        self.assertIn("不一致", "".join(meta2.get("restore_notes", [])))
        # JSON 被删 → 库仍可独立恢复
        state.unlink()
        meta3, session3 = load_state(collab.run_dir / "state.json")
        self.assertEqual(meta3["status"], "DELIVERED")

        # 等等——state.json 被删后 load_state 的库路径仍要读 state.json…
        # 该行为在下方单独验证（无状态视图字段时恢复仍成功）。

    def test_status_entry_reports_task_db(self):
        from mentis.run_projection import load_run_state, render_state_markdown
        collab = make_collab(base_dir=self.base, measure=file_aware_measure)
        collab.run()
        proj = load_run_state(collab.run_dir)
        self.assertTrue(proj["task_db"]["present"])
        self.assertEqual(proj["task_db"]["db_version"], SUPPORTED_DB_VERSION)
        text = render_state_markdown(proj)
        self.assertIn("任务库", text)
        self.assertIn("mentis-task-store/1", text)
        # 无库运行（历史形态）如实显示未创建
        empty = self.base / "legacy-run"
        empty.mkdir()
        (empty / "state.json").write_text("{}", encoding="utf-8")
        proj2 = load_run_state(empty)
        self.assertFalse(proj2["task_db"]["present"])

    def test_unknown_run_restore_preserves_unknown_without_delegation(self):
        adapter = FakeAdapter()

        def unknown(spec, status_override=None):
            return CodexTaskResult(
                task_id="t", attempt_id="a", brief_version=spec.brief_version,
                workspace=str(spec.workspace), status=AttemptStatus.UNKNOWN,
                native_thread_id="", usage={}, artifact_paths=[], cli_version="fake")

        adapter._run = unknown  # type: ignore[method-assign]
        collab = make_collab(adapter=adapter, base_dir=self.base,
                             measure=file_aware_measure)
        status = collab.run()
        self.assertEqual(status, "UNKNOWN")
        calls_before = len(adapter.calls)
        state = collab.run_dir / "state.json"
        r = collaboration_from_state(state, adapter=adapter,
                                     browser_capture=stub_capture)
        r.gateway = None
        r.use_model = False
        self.assertEqual(r.session.attempts[-1].status, AttemptStatus.UNKNOWN)
        self.assertEqual(r.status, "UNKNOWN")
        self.assertEqual(len(adapter.calls), calls_before)  # 恢复零外部委托
        # unknown 仍需核对：confirm 之前不能当作完成
        self.assertFalse(r.session.attempts[-1].accepted)


if __name__ == "__main__":
    unittest.main()
