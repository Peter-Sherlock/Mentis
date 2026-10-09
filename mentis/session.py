"""P1.2 任务会话：以操作强制不变量，而不是靠约定。

不变量（DEVELOPMENT_PLAN P1.2 退出条件）：
1. 同一目标的首稿/反馈/修订可回查（brief 版本 + 修订链）。
2. 执行完成、系统检查通过与用户满意三者分离。
3. notify_progress 不转为等待；request_decision 只阻塞依赖动作。
4. 缺少覆盖该范围的授权不能执行。
5. 旧授权与用户沉默不能批准新范围；未确认状态不自动重发。
"""

import hashlib
from datetime import datetime

from mentis.contracts import (
    Artifact,
    AttemptStatus,
    AuthStatus,
    Authorization,
    BlockedByDecision,
    Brief,
    ContractError,
    DelegationPolicy,
    Disposition,
    ExecutionAttempt,
    Interaction,
    InteractionKind,
    InvalidInteraction,
    InvalidState,
    Provenance,
    RevisionRecord,
    Review,
    SourcedItem,
    StaleVersion,
    UnauthorizedAction,
)

CONTRACT_VERSION = "p1.2"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256_of_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TaskSession:
    """一轮协作的全部可回查记录。"""

    def __init__(self, goal: str):
        self.goal = goal
        self.created_at = _now()
        self.briefs: list[Brief] = []
        self.policies: list[DelegationPolicy] = []
        self.authorizations: list[Authorization] = []
        self.interactions: list[Interaction] = []
        self.attempts: list[ExecutionAttempt] = []
        self.artifacts: list = []
        self.reviews: list[Review] = []
        self.revisions: list[RevisionRecord] = []
        self.user_feedback: dict = {}  # attempt_id -> {satisfied, note, at}
        self._counters: dict = {}

    # ---- id 与工具 ----

    def _next_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}-{n}"

    @property
    def current_brief(self) -> Brief:
        return self.briefs[-1]

    @property
    def current_policy(self) -> DelegationPolicy:
        return self.policies[-1]

    def brief_by_version(self, version: int) -> Brief:
        for b in self.briefs:
            if b.version == version:
                return b
        raise ContractError(f"brief v{version} 不存在")

    # ---- brief 与策略 ----

    def init_brief(self, objective: str) -> Brief:
        brief = Brief(
            brief_id=self._next_id("brief"),
            version=1,
            objective=objective,
            created_at=_now(),
        )
        self.briefs.append(brief)
        return brief

    def update_brief(
        self,
        objective: str | None = None,
        add_constraints: list[SourcedItem] = (),
        add_preferences: list[SourcedItem] = (),
    ) -> Brief:
        old = self.current_brief
        brief = Brief(
            brief_id=self._next_id("brief"),
            version=old.version + 1,
            objective=objective if objective is not None else old.objective,
            constraints=list(old.constraints) + list(add_constraints),
            preferences=list(old.preferences) + list(add_preferences),
            created_at=_now(),
        )
        old.superseded_by = brief.version
        self.briefs.append(brief)
        return brief

    def set_policy(self, policy: DelegationPolicy) -> DelegationPolicy:
        self.policies.append(policy)
        return policy

    # ---- 交互：通知与请求决定 ----

    def notify_progress(self, message: str) -> Interaction:
        interaction = Interaction(
            interaction_id=self._next_id("ix"),
            kind=InteractionKind.NOTIFY_PROGRESS,
            message=message,
            created_at=_now(),
            blocking_action=None,  # 通知一律不阻塞
        )
        self.interactions.append(interaction)
        return interaction

    def request_decision(self, message: str, blocking_action: str) -> Interaction:
        brief_version = None
        if self.briefs:
            brief_version = self.current_brief.version
        interaction = Interaction(
            interaction_id=self._next_id("ix"),
            kind=InteractionKind.REQUEST_DECISION,
            message=message,
            created_at=_now(),
            blocking_action=blocking_action,
            brief_version=brief_version,
        )
        self.interactions.append(interaction)
        return interaction

    def answer_interaction(self, interaction_id: str, text: str, source: str) -> Interaction:
        """回答请求决定。来源只允许 user 或 existing_authorization；
        不存在"沉默"回答方式——不回答就保持未决并继续阻塞。
        P1.3：已回答的请求拒绝重复回答——重复事件不重复推进（幂等跳过由调用方处理）。"""
        if source not in ("user", "existing_authorization"):
            raise InvalidInteraction(f"回答来源不合法: {source}")
        ix = self._get_interaction(interaction_id)
        if ix.kind is not InteractionKind.REQUEST_DECISION:
            raise InvalidInteraction("通知不是请求，无需回答")
        if ix.answer_source is not None:
            raise InvalidInteraction(
                f"{interaction_id} 已于 {ix.answered_at} 回答过；重复回答被拒绝，"
                "不得据此重复推进")
        ix.answer_text = text
        ix.answer_source = source
        ix.answered_at = _now()
        if self.briefs:
            ix.answered_brief_version = self.current_brief.version
        return ix

    def is_answered(self, interaction_id: str) -> bool:
        return self._get_interaction(interaction_id).answer_source is not None

    def _get_interaction(self, interaction_id: str) -> Interaction:
        for ix in self.interactions:
            if ix.interaction_id == interaction_id:
                return ix
        raise ContractError(f"交互 {interaction_id} 不存在")

    def pending_decision_blocking(self, action_key: str) -> Interaction | None:
        for ix in self.interactions:
            if (
                ix.kind is InteractionKind.REQUEST_DECISION
                and ix.blocking_action == action_key
                and ix.answer_source is None
            ):
                return ix
        return None

    # ---- 授权 ----

    def authorize(self, scope: str, granted_by: str = "user") -> Authorization:
        auth = Authorization(
            auth_id=self._next_id("auth"),
            scope=scope,
            granted_by=granted_by,
            granted_at=_now(),
        )
        self.authorizations.append(auth)
        return auth

    def revoke_authorization(self, auth_id: str) -> None:
        self._get_auth(auth_id).status = AuthStatus.REVOKED

    def supersede_authorization(self, auth_id: str, new_auth: Authorization) -> Authorization:
        self._get_auth(auth_id).status = AuthStatus.SUPERSEDED
        self.authorizations.append(new_auth)
        return new_auth

    def _get_auth(self, auth_id: str) -> Authorization:
        for a in self.authorizations:
            if a.auth_id == auth_id:
                return a
        raise ContractError(f"授权 {auth_id} 不存在")

    def _active_auth_for(self, scope: str) -> Authorization | None:
        for a in self.authorizations:
            if a.scope == scope and a.status is AuthStatus.ACTIVE:
                return a
        return None

    # ---- 执行尝试 ----

    def dispatch(
        self,
        action_key: str,
        execution_scope: str,
        revision_id: str | None = None,
    ) -> ExecutionAttempt:
        """委托一个动作。缺授权、依赖未决决定都会拒绝。"""
        if not self.policies:
            raise UnauthorizedAction("没有委托策略，不能执行")
        pending = self.pending_decision_blocking(action_key)
        if pending is not None:
            raise BlockedByDecision(
                f"动作 {action_key} 被未回答的请求阻塞（{pending.interaction_id}）；"
                "用户沉默不构成批准"
            )
        if self._active_auth_for(execution_scope) is None:
            raise UnauthorizedAction(
                f"没有覆盖范围 {execution_scope} 的有效授权；"
                "旧授权不能批准新范围"
            )
        attempt = ExecutionAttempt(
            attempt_id=self._next_id("attempt"),
            action_key=action_key,
            brief_version=self.current_brief.version,
            policy_version=self.current_policy.version,
            execution_scope=execution_scope,
            native_task_id=f"native-{self._counters['attempt']:04d}",
            status=AttemptStatus.DISPATCHED,
            created_at=_now(),
        )
        self.attempts.append(attempt)
        if revision_id is not None:
            for rev in self.revisions:
                if rev.revision_id == revision_id:
                    rev.to_attempt_id = attempt.attempt_id
        return attempt

    def mark_running(self, attempt_id: str) -> ExecutionAttempt:
        attempt = self._get_attempt(attempt_id)
        if attempt.status not in (AttemptStatus.DISPATCHED,):
            raise InvalidState(f"{attempt.status} 不能转为 running")
        attempt.status = AttemptStatus.RUNNING
        return attempt

    def complete_attempt(
        self,
        attempt_id: str,
        artifact_path: str | None = None,
        artifact_producer: str = "executor:simulated",
    ) -> ExecutionAttempt:
        attempt = self._get_attempt(attempt_id)
        if attempt.status not in (AttemptStatus.DISPATCHED, AttemptStatus.RUNNING):
            raise InvalidState(f"{attempt.status} 不能直接标记完成")
        attempt.status = AttemptStatus.COMPLETED
        if artifact_path is not None:
            artifact = self.add_artifact(attempt_id, artifact_path, artifact_producer)
            attempt.artifact_id = artifact.artifact_id
        return attempt

    def fail_attempt(self, attempt_id: str) -> ExecutionAttempt:
        attempt = self._get_attempt(attempt_id)
        if attempt.status not in (AttemptStatus.DISPATCHED, AttemptStatus.RUNNING):
            raise InvalidState(f"{attempt.status} 不能转为 failed")
        attempt.status = AttemptStatus.FAILED
        return attempt

    def mark_unknown(self, attempt_id: str) -> ExecutionAttempt:
        """提交超时等场景：进入 unknown，不自动重发。"""
        attempt = self._get_attempt(attempt_id)
        if attempt.status not in (AttemptStatus.DISPATCHED, AttemptStatus.RUNNING):
            raise InvalidState(f"{attempt.status} 不能转为 unknown")
        attempt.status = AttemptStatus.UNKNOWN
        return attempt

    def confirm_outcome(self, attempt_id: str, final_status: AttemptStatus) -> ExecutionAttempt:
        """外部核对后显式确认结局；unknown 只能经此转换。

        FAILED 也允许经人工核对确认——用于"无终态事件被记为失败，但产物可独立
        核对"的情形；调用者必须先核实产物与执行器状态并留痕。
        """
        attempt = self._get_attempt(attempt_id)
        if attempt.status not in (AttemptStatus.UNKNOWN, AttemptStatus.FAILED):
            raise InvalidState("confirm_outcome 只适用于 unknown/failed 状态")
        if final_status not in (AttemptStatus.COMPLETED, AttemptStatus.FAILED, AttemptStatus.INTERRUPTED):
            raise InvalidState(f"非法的最终状态: {final_status}")
        attempt.status = final_status
        return attempt

    def _get_attempt(self, attempt_id: str) -> ExecutionAttempt:
        for a in self.attempts:
            if a.attempt_id == attempt_id:
                return a
        raise ContractError(f"尝试 {attempt_id} 不存在")

    # ---- 产物、审核、接受 ----

    def add_artifact(self, attempt_id: str, path: str, producer: str) -> Artifact:
        artifact = Artifact(
            artifact_id=self._next_id("artifact"),
            path=path,
            sha256=_sha256_of_file(path),
            producer=producer,
            attempt_id=attempt_id,
            created_at=_now(),
        )
        self.artifacts.append(artifact)
        return artifact

    def record_review(
        self,
        attempt_id: str,
        findings: list,
        passed: bool,
        disposition: Disposition,
    ) -> Review:
        attempt = self._get_attempt(attempt_id)
        review = Review(
            review_id=self._next_id("review"),
            attempt_id=attempt_id,
            brief_version=attempt.brief_version,
            findings=list(findings),
            passed=passed,
            disposition=disposition,
            created_at=_now(),
        )
        self.reviews.append(review)
        return review

    def accept(self, attempt_id: str) -> ExecutionAttempt:
        """接受 = 执行已完成 + 该尝试的检查通过 + 绑定当前 brief 版本。
        与用户满意无关——后者单独记录。"""
        attempt = self._get_attempt(attempt_id)
        if attempt.status is not AttemptStatus.COMPLETED:
            raise InvalidState("执行未完成（completed），不能接受；执行结束不等于验收通过")
        passed = any(
            r.attempt_id == attempt_id and r.passed and r.disposition is Disposition.ACCEPT
            for r in self.reviews
        )
        if not passed:
            raise InvalidState("该尝试没有通过的独立检查（review accept），不能接受")
        if attempt.brief_version != self.current_brief.version:
            raise StaleVersion(
                f"尝试绑定 brief v{attempt.brief_version}，当前为 v{self.current_brief.version}；"
                "过期版本的结果不能接受"
            )
        attempt.accepted = True
        return attempt

    def record_user_feedback(self, attempt_id: str, satisfied: bool, note: str = "") -> dict:
        """用户满意单独记录，不与执行状态、检查结论混用。"""
        if self._get_attempt(attempt_id) is None:  # pragma: no cover - _get_attempt 已抛错
            raise ContractError("尝试不存在")
        entry = {"satisfied": satisfied, "note": note, "at": _now()}
        self.user_feedback[attempt_id] = entry
        return entry

    # ---- 修订链 ----

    def record_revision(
        self,
        feedback_interaction_id: str,
        from_attempt_id: str,
        instruction: str,
        round_no: int = 1,
    ) -> RevisionRecord:
        self._get_interaction(feedback_interaction_id)
        self._get_attempt(from_attempt_id)
        revision = RevisionRecord(
            revision_id=self._next_id("revision"),
            feedback_interaction_id=feedback_interaction_id,
            from_attempt_id=from_attempt_id,
            instruction=instruction,
            round_no=round_no,
            created_at=_now(),
        )
        self.revisions.append(revision)
        return revision

    def provenance_items(self, provenance: Provenance) -> list:
        found = []
        for b in self.briefs:
            for item in b.constraints + b.preferences:
                if item.provenance is provenance:
                    found.append(item)
        return found
