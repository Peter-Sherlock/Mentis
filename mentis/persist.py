"""P1.2 序列化：会话与 JSON 的无损往返；文件保存与读取。

JSONL 导出等属后续包；本模块只保证同一份数据可保存、可恢复、可导出。
"""

import json

from mentis.contracts import (
    Artifact,
    AttemptStatus,
    AuthStatus,
    Authorization,
    Brief,
    DelegationPolicy,
    Disposition,
    ExecutionAttempt,
    Interaction,
    InteractionKind,
    Provenance,
    RevisionRecord,
    Review,
    SourcedItem,
)
from mentis.session import CONTRACT_VERSION, TaskSession

_ENCRYPT_NONE = None  # 明示：本层不做任何加密或脱敏


def session_to_dict(session: TaskSession) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "goal": session.goal,
        "created_at": session.created_at,
        "briefs": [_brief_to_dict(b) for b in session.briefs],
        "policies": [_policy_to_dict(p) for p in session.policies],
        "authorizations": [_auth_to_dict(a) for a in session.authorizations],
        "interactions": [_interaction_to_dict(ix) for ix in session.interactions],
        "attempts": [_attempt_to_dict(a) for a in session.attempts],
        "artifacts": [_artifact_to_dict(x) for x in session.artifacts],
        "reviews": [_review_to_dict(r) for r in session.reviews],
        "revisions": [_revision_to_dict(r) for r in session.revisions],
        "user_feedback": session.user_feedback,
    }


def session_from_dict(data: dict) -> TaskSession:
    session = TaskSession.__new__(TaskSession)
    session.goal = data["goal"]
    session.created_at = data["created_at"]
    session.briefs = [_brief_from_dict(b) for b in data["briefs"]]
    session.policies = [_policy_from_dict(p) for p in data["policies"]]
    session.authorizations = [_auth_from_dict(a) for a in data["authorizations"]]
    session.interactions = [_interaction_from_dict(ix) for ix in data["interactions"]]
    session.attempts = [_attempt_from_dict(a) for a in data["attempts"]]
    session.artifacts = [_artifact_from_dict(x) for x in data["artifacts"]]
    session.reviews = [_review_from_dict(r) for r in data["reviews"]]
    session.revisions = [_revision_from_dict(r) for r in data["revisions"]]
    session.user_feedback = dict(data["user_feedback"])
    max_ids = {}
    for prefix in ("brief", "policy", "auth", "ix", "attempt", "artifact", "review", "revision"):
        nums = [int(x.split("-")[-1]) for x in _collect_ids(session, prefix)]
        max_ids[prefix] = max(nums, default=0)
    session._counters = max_ids
    return session


def _collect_ids(session: TaskSession, prefix: str) -> list:
    sources = {
        "brief": [b.brief_id for b in session.briefs],
        "policy": [p.policy_id for p in session.policies],
        "auth": [a.auth_id for a in session.authorizations],
        "ix": [ix.interaction_id for ix in session.interactions],
        "attempt": [a.attempt_id for a in session.attempts],
        "artifact": [x.artifact_id for x in session.artifacts],
        "review": [r.review_id for r in session.reviews],
        "revision": [r.revision_id for r in session.revisions],
    }
    return [i for i in sources[prefix] if i.startswith(prefix + "-")]


def save_session(session: TaskSession, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session_to_dict(session), f, ensure_ascii=False, indent=2)
    return path


def load_session(path: str) -> TaskSession:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(f"契约版本不符: {data.get('contract_version')} != {CONTRACT_VERSION}")
    return session_from_dict(data)


# ---- 各对象的双向映射 ----


def _item_to_dict(item: SourcedItem) -> dict:
    return {"text": item.text, "provenance": item.provenance.value, "source_ref": item.source_ref}


def _item_from_dict(d: dict) -> SourcedItem:
    return SourcedItem(d["text"], Provenance(d["provenance"]), d["source_ref"])


def _brief_to_dict(b: Brief) -> dict:
    return {
        "brief_id": b.brief_id,
        "version": b.version,
        "objective": b.objective,
        "constraints": [_item_to_dict(i) for i in b.constraints],
        "preferences": [_item_to_dict(i) for i in b.preferences],
        "superseded_by": b.superseded_by,
        "created_at": b.created_at,
    }


def _brief_from_dict(d: dict) -> Brief:
    b = Brief(d["brief_id"], d["version"], d["objective"], created_at=d["created_at"])
    b.constraints = [_item_from_dict(i) for i in d["constraints"]]
    b.preferences = [_item_from_dict(i) for i in d["preferences"]]
    b.superseded_by = d["superseded_by"]
    return b


def _policy_to_dict(p: DelegationPolicy) -> dict:
    return {
        "policy_id": p.policy_id,
        "version": p.version,
        "decided_scope": p.decided_scope,
        "reserved_decisions": p.reserved_decisions,
        "execution_scopes": p.execution_scopes,
        "budget": p.budget,
        "intervention_triggers": p.intervention_triggers,
        "provenance": p.provenance.value,
        "source_ref": p.source_ref,
        "created_at": p.created_at,
    }


def _policy_from_dict(d: dict) -> DelegationPolicy:
    return DelegationPolicy(
        policy_id=d["policy_id"],
        version=d["version"],
        decided_scope=d["decided_scope"],
        reserved_decisions=d["reserved_decisions"],
        execution_scopes=d["execution_scopes"],
        budget=d["budget"],
        intervention_triggers=d["intervention_triggers"],
        provenance=Provenance(d["provenance"]),
        source_ref=d["source_ref"],
        created_at=d["created_at"],
    )


def _auth_to_dict(a: Authorization) -> dict:
    return {
        "auth_id": a.auth_id,
        "scope": a.scope,
        "granted_by": a.granted_by,
        "granted_at": a.granted_at,
        "status": a.status.value,
    }


def _auth_from_dict(d: dict) -> Authorization:
    a = Authorization(d["auth_id"], d["scope"], d["granted_by"], d["granted_at"])
    a.status = AuthStatus(d["status"])
    return a


def _interaction_to_dict(ix: Interaction) -> dict:
    return {
        "interaction_id": ix.interaction_id,
        "kind": ix.kind.value,
        "message": ix.message,
        "created_at": ix.created_at,
        "blocking_action": ix.blocking_action,
        "answer_text": ix.answer_text,
        "answer_source": ix.answer_source,
        "answered_at": ix.answered_at,
    }


def _interaction_from_dict(d: dict) -> Interaction:
    return Interaction(
        interaction_id=d["interaction_id"],
        kind=InteractionKind(d["kind"]),
        message=d["message"],
        created_at=d["created_at"],
        blocking_action=d["blocking_action"],
        answer_text=d["answer_text"],
        answer_source=d["answer_source"],
        answered_at=d["answered_at"],
    )


def _attempt_to_dict(a: ExecutionAttempt) -> dict:
    return {
        "attempt_id": a.attempt_id,
        "action_key": a.action_key,
        "brief_version": a.brief_version,
        "policy_version": a.policy_version,
        "execution_scope": a.execution_scope,
        "native_task_id": a.native_task_id,
        "status": a.status.value,
        "artifact_id": a.artifact_id,
        "accepted": a.accepted,
        "created_at": a.created_at,
    }


def _attempt_from_dict(d: dict) -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_id=d["attempt_id"],
        action_key=d["action_key"],
        brief_version=d["brief_version"],
        policy_version=d["policy_version"],
        execution_scope=d["execution_scope"],
        native_task_id=d["native_task_id"],
        status=AttemptStatus(d["status"]),
        artifact_id=d["artifact_id"],
        accepted=d["accepted"],
        created_at=d["created_at"],
    )


def _artifact_to_dict(a: Artifact) -> dict:
    return {
        "artifact_id": a.artifact_id,
        "path": a.path,
        "sha256": a.sha256,
        "producer": a.producer,
        "attempt_id": a.attempt_id,
        "created_at": a.created_at,
    }


def _artifact_from_dict(d: dict) -> Artifact:
    return Artifact(
        artifact_id=d["artifact_id"],
        path=d["path"],
        sha256=d["sha256"],
        producer=d["producer"],
        attempt_id=d["attempt_id"],
        created_at=d["created_at"],
    )


def _review_to_dict(r: Review) -> dict:
    return {
        "review_id": r.review_id,
        "attempt_id": r.attempt_id,
        "brief_version": r.brief_version,
        "findings": r.findings,
        "passed": r.passed,
        "disposition": r.disposition.value,
        "created_at": r.created_at,
    }


def _review_from_dict(d: dict) -> Review:
    return Review(
        review_id=d["review_id"],
        attempt_id=d["attempt_id"],
        brief_version=d["brief_version"],
        findings=d["findings"],
        passed=d["passed"],
        disposition=Disposition(d["disposition"]),
        created_at=d["created_at"],
    )


def _revision_to_dict(r: RevisionRecord) -> dict:
    return {
        "revision_id": r.revision_id,
        "feedback_interaction_id": r.feedback_interaction_id,
        "from_attempt_id": r.from_attempt_id,
        "to_attempt_id": r.to_attempt_id,
        "instruction": r.instruction,
        "round_no": r.round_no,
        "created_at": r.created_at,
    }


def _revision_from_dict(d: dict) -> RevisionRecord:
    return RevisionRecord(
        revision_id=d["revision_id"],
        feedback_interaction_id=d["feedback_interaction_id"],
        from_attempt_id=d["from_attempt_id"],
        to_attempt_id=d["to_attempt_id"],
        instruction=d["instruction"],
        round_no=d["round_no"],
        created_at=d["created_at"],
    )
