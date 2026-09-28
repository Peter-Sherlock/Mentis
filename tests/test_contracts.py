"""P1.2 关键不变量检查。

覆盖开发计划 P1.2 退出条件：
- 同一目标的首稿/反馈/修订可回查
- 执行完成、系统检查通过与用户满意分离
- 通知不转为等待
- 缺少必要授权不能执行；旧授权与用户沉默不能批准新范围
- 序列化无损往返；过期版本结果不能接受
"""

import os
import sys
import tempfile
import unittest

from mentis.contracts import (
    AttemptStatus,
    BlockedByDecision,
    DelegationPolicy,
    Disposition,
    InvalidInteraction,
    InvalidState,
    Provenance,
    SourcedItem,
    StaleVersion,
    UnauthorizedAction,
)
from mentis.export import render_session_markdown
from mentis.persist import load_session, save_session, session_from_dict, session_to_dict
from mentis.session import TaskSession


def make_policy(session):
    session.set_policy(
        DelegationPolicy(
            policy_id=f"policy-{len(session.policies) + 1}",
            version=len(session.policies) + 1,
            decided_scope=["实现细节"],
            reserved_decisions=["价格变更"],
            execution_scopes=["workspace:demo"],
            budget={"max_auto_revisions": 2},
            provenance=Provenance.AGENT_SELECTED,
            source_ref="derived:user-instruction",
        )
    )


def make_authorized_session():
    session = TaskSession(goal="测试目标")
    session.init_brief("测试目标")
    make_policy(session)
    session.authorize("workspace:demo")
    return session


class InvariantTests(unittest.TestCase):
    def test_notify_does_not_block(self):
        session = make_authorized_session()
        session.notify_progress("开始工作")
        attempt = session.dispatch("draft", "workspace:demo")
        self.assertEqual(attempt.status, AttemptStatus.DISPATCHED)

    def test_request_decision_blocks_only_dependent_action(self):
        session = make_authorized_session()
        session.request_decision("价格是否调整？", blocking_action="change-price")
        with self.assertRaises(BlockedByDecision):
            session.dispatch("change-price", "workspace:demo")
        attempt = session.dispatch("other-action", "workspace:demo")
        self.assertEqual(attempt.action_key, "other-action")

    def test_missing_authorization_refuses(self):
        session = TaskSession(goal="无授权")
        session.init_brief("目标")
        make_policy(session)
        with self.assertRaises(UnauthorizedAction):
            session.dispatch("draft", "workspace:demo")

    def test_new_scope_needs_new_authorization(self):
        session = make_authorized_session()  # 只有 workspace:demo
        with self.assertRaises(UnauthorizedAction):
            session.dispatch("fetch-assets", "workspace:demo+network:fetch")

    def test_revoked_authorization_refuses(self):
        session = make_authorized_session()
        session.revoke_authorization("auth-1")
        with self.assertRaises(UnauthorizedAction):
            session.dispatch("draft", "workspace:demo")

    def test_silence_is_not_approval(self):
        session = make_authorized_session()
        ix = session.request_decision("是否扩大范围？", blocking_action="expand")
        # 不存在"沉默回答"的 API：保持未回答，动作持续阻塞
        self.assertIsNone(ix.answer_source)
        with self.assertRaises(BlockedByDecision):
            session.dispatch("expand", "workspace:demo")
        # 由用户显式回答后解除
        session.answer_interaction(ix.interaction_id, "同意", source="user")
        attempt = session.dispatch("expand", "workspace:demo")
        self.assertIsNotNone(attempt)

    def test_answer_source_whitelist(self):
        session = make_authorized_session()
        ix = session.request_decision("问题", blocking_action="act")
        with self.assertRaises(InvalidInteraction):
            session.answer_interaction(ix.interaction_id, "默认同意", source="silence")
        self.assertIsNone(ix.answer_source)

    def test_completed_not_accepted_without_review(self):
        session = make_authorized_session()
        attempt = session.dispatch("draft", "workspace:demo")
        session.complete_attempt(attempt.attempt_id)
        with self.assertRaises(InvalidState):
            session.accept(attempt.attempt_id)
        session.record_review(
            attempt.attempt_id, findings=["x"], passed=True, disposition=Disposition.REVISE
        )
        with self.assertRaises(InvalidState):
            session.accept(attempt.attempt_id)  # 检查存在但处置不是 accept

    def test_accept_separate_from_user_satisfaction(self):
        session = make_authorized_session()
        attempt = session.dispatch("draft", "workspace:demo")
        session.complete_attempt(attempt.attempt_id)
        session.record_review(
            attempt.attempt_id, findings=[], passed=True, disposition=Disposition.ACCEPT
        )
        session.accept(attempt.attempt_id)
        # 接受与用户满意是两种状态：满意尚未记录时依然成立
        self.assertNotIn(attempt.attempt_id, session.user_feedback)
        session.record_user_feedback(attempt.attempt_id, satisfied=False, note="不满意")
        self.assertFalse(session.user_feedback[attempt.attempt_id]["satisfied"])
        self.assertTrue(attempt.accepted)  # 两者互不覆盖

    def test_stale_brief_version_not_accepted(self):
        session = make_authorized_session()
        attempt = session.dispatch("draft", "workspace:demo")
        session.complete_attempt(attempt.attempt_id)
        session.record_review(
            attempt.attempt_id, findings=[], passed=True, disposition=Disposition.ACCEPT
        )
        session.update_brief(add_preferences=[SourcedItem("新增偏好", Provenance.USER_SELECTED, "user-msg:2")])
        with self.assertRaises(StaleVersion):
            session.accept(attempt.attempt_id)

    def test_unknown_not_confirmed_cannot_complete(self):
        session = make_authorized_session()
        attempt = session.dispatch("draft", "workspace:demo")
        session.mark_unknown(attempt.attempt_id)
        with self.assertRaises(InvalidState):
            session.complete_attempt(attempt.attempt_id)  # 未知状态不得当作完成或自动重发
        session.confirm_outcome(attempt.attempt_id, AttemptStatus.COMPLETED)
        self.assertEqual(attempt.status, AttemptStatus.COMPLETED)

    def test_revision_chain_traceable_and_exported(self):
        session = make_authorized_session()
        session.current_brief.preferences.append(
            SourcedItem("保留暖色底", Provenance.USER_SELECTED, "user-msg:1")
        )
        attempt1 = session.dispatch("draft", "workspace:demo")
        session.complete_attempt(attempt1.attempt_id)
        session.record_review(
            attempt1.attempt_id, findings=["促销感偏重"], passed=False, disposition=Disposition.REVISE
        )
        feedback = session.request_decision("（演示）用户反馈：像促销广告", blocking_action="__none__")
        revision = session.record_revision(
            feedback.interaction_id, attempt1.attempt_id, "减少按钮层级，保留暖色底", round_no=1
        )
        attempt2 = session.dispatch("draft-v2", "workspace:demo", revision_id=revision.revision_id)
        self.assertEqual(revision.to_attempt_id, attempt2.attempt_id)
        text = render_session_markdown(session, data_origin="测试")
        self.assertIn("修订链", text)
        self.assertIn("attempt-1", text)
        self.assertIn("attempt-2", text)
        self.assertIn("用户选定", text)  # 来源标注可见

    def test_serialization_roundtrip(self):
        session = make_authorized_session()
        session.notify_progress("n1")
        ix = session.request_decision("问题", blocking_action="act")
        session.answer_interaction(ix.interaction_id, "好", source="user")
        attempt = session.dispatch("draft", "workspace:demo")
        session.complete_attempt(attempt.attempt_id)
        session.record_review(attempt.attempt_id, findings=[], passed=True, disposition=Disposition.ACCEPT)
        session.accept(attempt.attempt_id)
        session.record_user_feedback(attempt.attempt_id, satisfied=True, note="ok")

        restored = session_from_dict(session_to_dict(session))
        self.assertEqual(restored.goal, session.goal)
        self.assertEqual(restored.attempts[0].status, AttemptStatus.COMPLETED)
        self.assertTrue(restored.attempts[0].accepted)
        self.assertEqual(restored.interactions[1].answer_source, "user")
        self.assertEqual(restored.current_brief.version, 1)

        # 文件保存与读取（含契约版本校验）
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            save_session(session, path)
            loaded = load_session(path)
            self.assertEqual(loaded.attempts[0].attempt_id, "attempt-1")
            self.assertEqual(loaded.user_feedback["attempt-1"]["satisfied"], True)


if __name__ == "__main__":
    unittest.main()
