"""P1.2 最小任务与反馈契约：数据对象与语义枚举。

只实现当前闭环必需的对象；名称是逻辑职责，不要求独立数据库表
（PROJECT_PLAN §12.1）。来源区分 user_selected / agent_selected / provisional。
"""

from dataclasses import dataclass, field
from enum import Enum


class Provenance(str, Enum):
    """信息来源：用户选定 / 系统从用户指令推导 / 暂定（未经确认）。"""

    USER_SELECTED = "user_selected"
    AGENT_SELECTED = "agent_selected"
    PROVISIONAL = "provisional"


PROVENANCE_LABEL = {
    Provenance.USER_SELECTED: "用户选定",
    Provenance.AGENT_SELECTED: "系统推导",
    Provenance.PROVISIONAL: "暂定",
}


class InteractionKind(str, Enum):
    NOTIFY_PROGRESS = "notify_progress"      # 通知：不等待回复
    REQUEST_DECISION = "request_decision"    # 请求决定：只阻塞依赖动作


class AttemptStatus(str, Enum):
    PLANNED = "planned"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"  # 未确认外部执行是否结束；不得自动重发


class Disposition(str, Enum):
    ACCEPT = "accept"
    REVISE = "revise"
    ASK_USER = "ask_user"
    ESCALATE = "escalate"


class AuthStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class ContractError(Exception):
    """契约不变量被违反时的基类。"""


class UnauthorizedAction(ContractError):
    """缺少覆盖该范围的授权；旧授权不能批准新范围。"""


class BlockedByDecision(ContractError):
    """依赖的决定未回答；用户沉默不构成批准。"""


class StaleVersion(ContractError):
    """尝试绑定的是已被替代的 brief 版本，不能据以接受。"""


class InvalidState(ContractError):
    """状态转换不合法（如未通过检查就接受）。"""


class InvalidInteraction(ContractError):
    """交互记录不合法（如通知携带阻塞动作）。"""


@dataclass
class SourcedItem:
    """带来源的约束或偏好条目。"""

    text: str
    provenance: Provenance
    source_ref: str = ""  # 如 "user-msg:1"、"derived:policy-v1"


@dataclass
class Brief:
    """版本化任务定义；旧版本只被标注替代，不原地改写。"""

    brief_id: str
    version: int
    objective: str
    constraints: list = field(default_factory=list)   # list[SourcedItem]
    preferences: list = field(default_factory=list)   # list[SourcedItem]
    superseded_by: int | None = None
    created_at: str = ""


@dataclass
class DelegationPolicy:
    """最小委托策略：从用户现有指令推导，不要求用户填表。"""

    policy_id: str
    version: int
    decided_scope: list = field(default_factory=list)       # 可代决范围
    reserved_decisions: list = field(default_factory=list)  # 必须用户决定
    execution_scopes: list = field(default_factory=list)    # 允许的执行范围标识
    budget: dict = field(default_factory=dict)              # 如 {"max_auto_revisions": 2}
    intervention_triggers: list = field(default_factory=list)
    provenance: Provenance = Provenance.AGENT_SELECTED
    source_ref: str = ""
    created_at: str = ""


@dataclass
class Authorization:
    """与就绪判断独立的授权记录；撤回或被替代后不再生效。"""

    auth_id: str
    scope: str
    granted_by: str = "user"
    granted_at: str = ""
    status: AuthStatus = AuthStatus.ACTIVE


@dataclass
class Interaction:
    """notify_progress 不等待回复；request_decision 只阻塞列出的动作。"""

    interaction_id: str
    kind: InteractionKind
    message: str
    created_at: str = ""
    blocking_action: str | None = None   # notify 必须为 None
    answer_text: str | None = None
    answer_source: str | None = None     # "user" | "existing_authorization"
    answered_at: str | None = None


@dataclass
class ExecutionAttempt:
    """一次执行尝试；completed 只表示执行结束，不表示接受。"""

    attempt_id: str
    action_key: str
    brief_version: int
    policy_version: int
    execution_scope: str
    native_task_id: str
    status: AttemptStatus = AttemptStatus.PLANNED
    artifact_id: str | None = None
    accepted: bool = False
    created_at: str = ""


@dataclass
class Artifact:
    """产物：真实文件路径与哈希；生成者在演示中为 executor:simulated。"""

    artifact_id: str
    path: str
    sha256: str
    producer: str
    attempt_id: str
    created_at: str = ""


@dataclass
class Review:
    """基于证据的检查结论；与执行状态、用户满意互相独立。"""

    review_id: str
    attempt_id: str
    brief_version: int
    findings: list = field(default_factory=list)
    passed: bool = False
    disposition: Disposition = Disposition.REVISE
    created_at: str = ""


@dataclass
class RevisionRecord:
    """修订链：哪条反馈触发、从哪个尝试到哪个尝试。"""

    revision_id: str
    feedback_interaction_id: str
    from_attempt_id: str
    to_attempt_id: str | None = None
    instruction: str = ""
    round_no: int = 1
    created_at: str = ""
