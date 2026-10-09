"""P1.3 任务规范态 SQLite 存储：事务、库版本、迁移、恢复。

职责边界（PROJECT_PLAN §12.3；本包退出条件"摘要不成为第二事实来源"）：
- **本库（state.sqlite3）**：任务规范状态（brief/策略/授权/交互/尝试/产物/评审/修订）
  与关键状态事件的事实记录；**恢复以此为准**。
- **原始证据文件**（trace/、professional-config/、evaluation-plan/、workspace 产物）：
  证据的事实来源；库只存引用与内容哈希，不复制正文。
- **JSON 导出**（session.json / state.json）：给人读与既有只读入口用的导出；
  与库不一致时以库为准并留痕，不反向改写库。
- **报告/页面/状态视图**：投影；缺证据标缺口，不反推事实。

失败语义：
- 保存为单事务（BEGIN IMMEDIATE）；任何一步失败整体回滚——不留半套规范状态。
- 数据库损坏 / 版本高于支持范围：显式抛 StoreError（含恢复办法），不静默新建任务。
- 迁移前自动备份整库文件；迁移失败还原备份。
- unknown 尝试原样恢复；恢复动作不发起任何外部委托。
"""

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from mentis.contracts import ContractError
from mentis.persist import session_from_dict, session_rows, session_to_dict
from mentis.session import CONTRACT_VERSION, TaskSession

STORE_SCHEMA = "mentis-task-store/1"
SUPPORTED_DB_VERSION = 1
DB_FILENAME = "state.sqlite3"
_BUSY_TIMEOUT_MS = 5000
# 摘要计算排除的易变字段（不参与一致性判定）
_VOLATILE_META = {"updated_at", "writer", "task_db"}


class StoreError(ContractError):
    """任务库不可用或校验失败；恢复动作不得静默降级为新建任务。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_path_for(run_dir: str | Path) -> Path:
    return Path(run_dir) / DB_FILENAME


def _canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- schema 与迁移 ----

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS briefs (
  version INTEGER PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
  version INTEGER PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authorizations (
  auth_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interactions (
  interaction_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  answered INTEGER NOT NULL DEFAULT 0,
  brief_version INTEGER,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  action_key TEXT NOT NULL,
  brief_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  native_task_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  attempt_id TEXT,
  sha256 TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  attempt_id TEXT,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
  revision_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_feedback (
  attempt_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_events (
  event_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  brief_version INTEGER,
  recorded_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interactions_pending ON interactions(kind, answered);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);
"""

# db 版本 -> 迁移步骤（在事务内执行）；1 为初始 schema。升级示例：
#   2: lambda conn: conn.execute("ALTER TABLE ...")
_SCHEMA_MIGRATIONS = {}


def _is_db_file(db: Path) -> bool:
    """文件存在且像 SQLite 库（头 16 字节），或尚不存在（将新建）。"""
    if not db.is_file():
        return True
    try:
        with open(db, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _backup_path(db: Path, version: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return db.with_name(f"{db.name}.bak-v{version}-{stamp}")


def _open_with_schema(db: Path, create: bool) -> sqlite3.Connection:
    """打开任务库并确保处于支持版本；返回可用连接。

    - 文件不是 SQLite 库 → StoreCorrupt（含恢复办法）。
    - 版本高于支持范围 → StoreError（不降级、不重建）。
    - 有待执行迁移：先备份整库，逐版本在事务中升级；失败还原备份。
    """
    if db.is_file() and not _is_db_file(db):
        raise StoreError(
            f"任务库文件损坏（不是 SQLite 数据库）: {db}——进入待核对，不静默新建任务；"
            f"恢复办法：从 {db.name}.bak-v* 备份还原，或删除本库后由 JSON 导出重新导入"
            "（JSON 导出不会被本程序改动）")
    conn = sqlite3.connect(db, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not tables:
            if not create:
                raise StoreError(f"任务库为空且不允许创建: {db}")
            conn.executescript(_SCHEMA_V1)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('db_version', ?)",
                         (str(SUPPORTED_DB_VERSION),))
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('store_schema', ?)",
                         (STORE_SCHEMA,))
            conn.commit()
            conn.execute(f"PRAGMA user_version = {SUPPORTED_DB_VERSION}")
            conn.commit()
            return conn
        row = conn.execute("SELECT value FROM meta WHERE key='db_version'").fetchone()
        if row is None:
            conn.close()
            raise StoreError("任务库缺少 db_version（元数据不完整）——按损坏处理，进入待核对；"
                             f"恢复办法：从 {db.name}.bak-v* 备份还原，或由 JSON 导出重新导入")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            conn.close()
            raise StoreError(f"任务库 db_version 非法: {row[0]!r}——进入待核对") from exc
        if version > SUPPORTED_DB_VERSION:
            conn.close()
            raise StoreError(
                f"任务库版本 {version} 高于本程序支持的 {SUPPORTED_DB_VERSION}"
                "（可能由更新版本创建）——不支持降级恢复，进入待核对；"
                "恢复办法：用创建该库的程序版本读取，或从备份核对")
        pending = [v for v in sorted(_SCHEMA_MIGRATIONS) if v > version]
        for target in pending:
            conn.close()
            backup = _backup_path(db, version)
            shutil.copy2(db, backup)
            conn = sqlite3.connect(db, timeout=_BUSY_TIMEOUT_MS / 1000)
            try:
                conn.execute("BEGIN IMMEDIATE")
                _SCHEMA_MIGRATIONS[target](conn)
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('db_version', ?)",
                             (str(target),))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                conn.close()
                shutil.copy2(backup, db)  # 还原备份，不留半升级状态
                raise StoreError(
                    f"任务库迁移 v{version}→v{target} 失败：{exc}；已还原备份 "
                    f"{backup.name}，原库未被改动——进入待核对") from exc
            version = target
        return conn
    except sqlite3.DatabaseError as exc:
        try:
            conn.close()
        except Exception:
            pass
        raise StoreError(
            f"任务库损坏或无法读取（{exc}）——进入待核对，不静默新建任务；"
            f"恢复办法：从 {db.name}.bak-v* 备份还原，或删除本库后由 JSON 导出重新导入") from exc


# ---- 配置/计划绑定事实 ----

def _config_binding_facts(run_dir: Path) -> dict:
    """从运行目录读取专业配置/评估计划当前版本文件的内容哈希（记录事实，供恢复核对）。"""
    facts = {}
    cfg_dir = run_dir / "professional-config"
    if cfg_dir.is_dir():
        versions = sorted(cfg_dir.glob("config-v*.json"))
        if versions:
            latest = versions[-1]
            facts["professional_config_file"] = latest.name
            facts["professional_config_sha256"] = hashlib.sha256(
                latest.read_bytes()).hexdigest()
    plan_dir = run_dir / "evaluation-plan"
    if plan_dir.is_dir():
        plans = sorted(plan_dir.glob("plan-v*.json"))
        if plans:
            latest = plans[-1]
            facts["evaluation_plan_file"] = latest.name
            facts["evaluation_plan_sha256"] = hashlib.sha256(
                latest.read_bytes()).hexdigest()
    return facts


def _binding_problems(run_dir: Path, meta_rows: dict) -> list:
    """恢复时核对库记录的配置/计划哈希与现文件是否一致；不符即问题（不悄悄换最新）。"""
    problems = []
    pairs = [("professional_config_sha256", "professional_config_file",
              "professional-config", "专业配置"),
             ("evaluation_plan_sha256", "evaluation_plan_file",
              "evaluation-plan", "评估计划")]
    for sha_key, file_key, sub, label in pairs:
        recorded = meta_rows.get(sha_key, "")
        if not recorded:
            continue
        name = meta_rows.get(file_key, "")
        target = run_dir / sub / name
        if not target.is_file():
            problems.append(f"{label}文件缺失（库记录 {sub}/{name}，sha {recorded[:12]}）")
        else:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != recorded:
                problems.append(
                    f"{label}内容与恢复记录不符（库记录 sha {recorded[:12]}，"
                    f"现文件 {actual[:12]}）——恢复不悄悄换用最新资源")
    return problems


# ---- 摘要 ----

def content_digest(session: TaskSession, meta: dict) -> str:
    """规范状态摘要：同状态必同值；meta 中易变字段（更新时刻/写入者/库信息）不参与。"""
    stable_meta = {k: v for k, v in meta.items() if k not in _VOLATILE_META}
    return _sha256_text(_canonical_json(
        {"contract_version": CONTRACT_VERSION,
         "session": session_to_dict(session),
         "meta": stable_meta}))


# ---- 保存 ----

def save_task_state(run_dir: str | Path, session: TaskSession, meta: dict) -> dict:
    """把任务规范状态写入 SQLite（单事务全量同步；任何一步失败整体回滚）。

    返回 {"path", "digest", "db_version", "changed"}；同一状态重复保存幂等。
    库同时存一份 meta 原文（meta_json）——恢复侧基于同一份 meta 重算摘要，
    结构演化不会破坏校验。
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    db = db_path_for(run_dir)
    try:
        digest = content_digest(session, meta)
        meta_json = _canonical_json(meta)
        facts = _config_binding_facts(run_dir)
        rows = session_rows(session)
    except StoreError:
        raise
    except Exception as exc:
        raise StoreError(f"任务状态无法规范化（{type(exc).__name__}: {exc}）——"
                         "库未被改动") from exc
    meta_values = {
        "contract_version": CONTRACT_VERSION,
        "store_schema": STORE_SCHEMA,
        "run_id": str(meta.get("run_id", run_dir.name)),
        "mode": str(meta.get("mode", "")),
        "status": str(meta.get("status", "")),
        "goal": str(meta.get("goal", session.goal)),
        "created_at": str(session.created_at),
        "workspace": str(meta.get("workspace", "")),
        "base_dir": str(meta.get("base_dir", "")),
        "thread_id": str(meta.get("thread_id", "")),
        "repairs_used": str(int(meta.get("repairs_used", 0))),
        "timeout_s": str(int(meta.get("timeout_s", 420))),
        "max_repairs": str(int(meta.get("max_repairs", 1))),
        "applied_answers": "1" if meta.get("applied_answers") else "0",
        "delivered": "1" if meta.get("delivered") else "0",
        "material_text": str(meta.get("material_text", "")),
        "observation_source": str(meta.get("observation_source", "")),
        "currency_cap": str(meta.get("currency_cap", "")),
        "evaluation_plan_version": str(
            (meta.get("evaluation_plan") or {}).get("version", "")),
        "budget_registered": (_canonical_json(meta["budget_registered"])
                              if meta.get("budget_registered") is not None else ""),
        "model_usage": _canonical_json(meta.get("model_usage") or {}),
        "codex_runs": _canonical_json(meta.get("codex_runs") or []),
        "question_routes": _canonical_json(meta.get("question_routes") or []),
        "meta_json": meta_json,
        "state_digest": digest,
        "updated_at": _now(),
        "writer": _canonical_json({"pid": os.getpid()}),
    }
    meta_values.update(facts)
    conn = _open_with_schema(db, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute("SELECT value FROM meta WHERE key='state_digest'").fetchone()
        changed = not (prev and prev[0] == digest)
        if changed:
            for table in ("briefs", "policies", "authorizations", "interactions",
                          "attempts", "artifacts", "reviews", "revisions",
                          "user_feedback"):
                conn.execute(f"DELETE FROM {table}")
            for key, value in meta_values.items():
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                             (key, value))
            for b in rows["briefs"]:
                conn.execute("INSERT OR REPLACE INTO briefs(version, payload) VALUES(?, ?)",
                             (b["version"], _canonical_json(b)))
            for p in rows["policies"]:
                conn.execute("INSERT OR REPLACE INTO policies(version, payload) VALUES(?, ?)",
                             (p["version"], _canonical_json(p)))
            for a in rows["authorizations"]:
                conn.execute(
                    "INSERT OR REPLACE INTO authorizations(auth_id, scope, status, payload) "
                    "VALUES(?, ?, ?, ?)",
                    (a["auth_id"], a["scope"], a["status"], _canonical_json(a)))
            for ix in rows["interactions"]:
                conn.execute(
                    "INSERT OR REPLACE INTO interactions(interaction_id, kind, answered, "
                    "brief_version, payload) VALUES(?, ?, ?, ?, ?)",
                    (ix["interaction_id"], ix["kind"],
                     1 if ix.get("answer_source") is not None else 0,
                     ix.get("brief_version"), _canonical_json(ix)))
            for seq, a in enumerate(rows["attempts"], start=1):
                conn.execute(
                    "INSERT OR REPLACE INTO attempts(attempt_id, seq, action_key, "
                    "brief_version, status, native_task_id, payload) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (a["attempt_id"], seq, a["action_key"], a["brief_version"],
                     a["status"], a["native_task_id"], _canonical_json(a)))
            for x in rows["artifacts"]:
                conn.execute(
                    "INSERT OR REPLACE INTO artifacts(artifact_id, attempt_id, sha256, payload) "
                    "VALUES(?, ?, ?, ?)",
                    (x["artifact_id"], x.get("attempt_id"), x["sha256"],
                     _canonical_json(x)))
            for r in rows["reviews"]:
                conn.execute(
                    "INSERT OR REPLACE INTO reviews(review_id, attempt_id, payload) "
                    "VALUES(?, ?, ?)",
                    (r["review_id"], r["attempt_id"], _canonical_json(r)))
            for r in rows["revisions"]:
                conn.execute(
                    "INSERT OR REPLACE INTO revisions(revision_id, payload) VALUES(?, ?)",
                    (r["revision_id"], _canonical_json(r)))
            for attempt_id, entry in rows["user_feedback"].items():
                conn.execute(
                    "INSERT OR REPLACE INTO user_feedback(attempt_id, payload) VALUES(?, ?)",
                    (attempt_id, _canonical_json(entry)))
        conn.commit()
        return {"path": db, "digest": digest, "db_version": SUPPORTED_DB_VERSION,
                "changed": changed}
    except StoreError:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise StoreError(f"任务库写入失败（{type(exc).__name__}: {exc}）——"
                         "事务已回滚，库保持先前完整状态；JSON 导出不受影响") from exc
    finally:
        conn.close()


# ---- 恢复 ----

def restore_task_state(run_dir: str | Path) -> tuple[TaskSession, dict]:
    """从 SQLite 恢复任务规范状态（跨进程恢复的事实来源）。

    校验：库完整性 quick_check、状态摘要重算一致、配置/计划哈希与文件一致。
    任何不符 → StoreError（进入待核对），不静默换用最新资源或新建任务。
    返回 (session, meta)；meta 为保存时的原文（含 restored_from/state_digest 标注）。
    """
    run_dir = Path(run_dir)
    db = db_path_for(run_dir)
    if not db.is_file():
        raise StoreError(f"任务库不存在: {db}")
    conn = _open_with_schema(db, create=False)
    try:
        (summary,) = conn.execute("PRAGMA quick_check").fetchone()
        if summary != "ok":
            raise StoreError(
                f"任务库完整性检查失败（quick_check={summary}）——进入待核对；"
                f"恢复办法：从 {db.name}.bak-v* 备份还原，或删除本库后由 JSON 导出重新导入")
        meta_rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        stored_digest = meta_rows.get("state_digest", "")
        meta_json = meta_rows.get("meta_json", "")
        if not stored_digest or not meta_json:
            raise StoreError("任务库缺少状态摘要或 meta 原文——不像本程序写出的库，进入待核对")
        payloads = {}
        for table in ("briefs", "policies", "authorizations", "interactions",
                      "attempts", "artifacts", "reviews", "revisions"):
            order = {"briefs": "version", "policies": "version"}.get(table, "rowid")
            payloads[table] = [json.loads(r[0]) for r in conn.execute(
                f"SELECT payload FROM {table} ORDER BY {order}").fetchall()]
        feedback_rows = [(r[0], json.loads(r[1])) for r in conn.execute(
            "SELECT attempt_id, payload FROM user_feedback").fetchall()]
    finally:
        conn.close()
    if not payloads["briefs"]:
        raise StoreError("任务库没有任何 brief 记录——不像本程序写出的库，进入待核对")
    try:
        saved_meta = json.loads(meta_json)
    except ValueError as exc:
        raise StoreError(f"任务库 meta 原文无法解析（{exc}）——进入待核对") from exc
    data = {
        "contract_version": CONTRACT_VERSION,
        "goal": meta_rows.get("goal", saved_meta.get("goal", "")),
        "created_at": meta_rows.get("created_at", ""),
        "briefs": payloads["briefs"],
        "policies": payloads["policies"],
        "authorizations": payloads["authorizations"],
        "interactions": payloads["interactions"],
        "attempts": payloads["attempts"],
        "artifacts": payloads["artifacts"],
        "reviews": payloads["reviews"],
        "revisions": payloads["revisions"],
        "user_feedback": {attempt_id: entry for attempt_id, entry in feedback_rows},
    }
    session = session_from_dict(data)
    # 摘要重算：基于库内存档的同一份 meta 原文——规范状态必须与保存时一致
    recomputed = content_digest(session, saved_meta)
    if recomputed != stored_digest:
        raise StoreError(
            "任务库规范状态与保存时摘要不一致（可能被绕过 API 改动或损坏）——"
            "进入待核对，不据此恢复；恢复办法：从 .bak-v* 备份还原，或删除本库后由"
            " JSON 导出重新导入并人工核对差异")
    problems = _binding_problems(run_dir, meta_rows)
    if problems:
        raise StoreError("恢复校验失败：" + "；".join(problems) +
                         "——进入待核对（不悄悄换用最新资源）；"
                         "恢复办法：还原被改动的文件，或人工核对后修正")
    saved_meta["restored_from"] = "sqlite"
    saved_meta["state_digest"] = stored_digest
    saved_meta.setdefault("task_db", {})
    saved_meta["task_db"]["db_version"] = int(meta_rows.get("db_version", "0") or 0)
    # 绑定事实（保存时从运行目录文件记录的哈希）随恢复返回，供核对与展示
    if meta_rows.get("professional_config_sha256"):
        saved_meta["professional_config_binding"] = {
            "file": meta_rows.get("professional_config_file", ""),
            "sha256": meta_rows["professional_config_sha256"]}
    if meta_rows.get("evaluation_plan_sha256"):
        saved_meta["evaluation_plan_binding"] = {
            "file": meta_rows.get("evaluation_plan_file", ""),
            "sha256": meta_rows["evaluation_plan_sha256"]}
    return session, saved_meta


# ---- 关键状态事件（去重记录；不是 trace 事件流的副本） ----

def record_task_event(run_dir: str | Path, kind: str, payload: dict,
                      brief_version: int | None = None) -> bool:
    """记录一条关键状态事件（任务状态转换的事实记录）。

    同一内容（kind+payload+brief_version）重复记录只入一条，返回是否首次写入；
    调用方结合返回值实现"重复事件不重复推进"。
    任务库不存在时**不创建库**、跳过记录（返回 False）——规范事实仍在会话与
    trace 中；避免遗留 JSON 运行在恢复中途留下空库遮蔽 JSON 导出。
    """
    run_dir = Path(run_dir)
    db = db_path_for(run_dir)
    if not db.is_file():
        return False
    payload_text = _canonical_json(payload)
    event_hash = _sha256_text(_canonical_json(
        {"kind": kind, "payload": payload_text, "brief_version": brief_version}))
    conn = _open_with_schema(db, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO state_events(event_hash, kind, brief_version, "
            "recorded_at, payload) VALUES(?, ?, ?, ?, ?)",
            (event_hash, kind, brief_version, _now(), payload_text))
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise StoreError(f"状态事件记录失败（{exc}）——事务已回滚") from exc
    finally:
        conn.close()


def list_state_events(run_dir: str | Path, limit: int = 50) -> list:
    run_dir = Path(run_dir)
    db = db_path_for(run_dir)
    if not db.is_file():
        return []
    conn = _open_with_schema(db, create=False)
    try:
        rows = conn.execute(
            "SELECT recorded_at, kind, brief_version, payload FROM state_events "
            "ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [{"recorded_at": r[0], "kind": r[1], "brief_version": r[2],
                 "payload": json.loads(r[3])} for r in reversed(rows)]
    finally:
        conn.close()


# ---- 兼容迁移：已有 JSON 运行记录 → 任务库 ----

def import_json_run(run_dir: str | Path, state_path: str | Path | None = None) -> dict:
    """把既有 JSON 运行记录（session.json + state.json）导入任务库。

    幂等：库已存在且与 JSON 导出摘要一致时跳过；库损坏/不完整时以 JSON 导出为源重建。
    JSON 源文件只读不回写；历史运行应先复制到副本目录再验证，不回写原任务。
    """
    run_dir = Path(run_dir)
    sp = Path(state_path) if state_path else run_dir / "state.json"
    meta = json.loads(sp.read_text(encoding="utf-8"))
    sdata = json.loads((sp.parent / "session.json").read_text(encoding="utf-8"))
    if sdata.get("contract_version") != CONTRACT_VERSION:
        raise StoreError(
            f"JSON 导出契约版本不符: {sdata.get('contract_version')} != "
            f"{CONTRACT_VERSION}——历史记录不猜测兼容，进入待核对")
    session = session_from_dict(sdata)
    digest = content_digest(session, meta)
    existing = db_path_for(run_dir)
    if existing.is_file():
        try:
            _, prev = restore_task_state(run_dir)
            if prev.get("state_digest") == digest:
                return {"path": existing, "digest": digest, "imported": False,
                        "note": "任务库已存在且与 JSON 导出一致（幂等跳过）"}
        except StoreError:
            pass  # 库损坏/不完整 → 以 JSON 导出为迁移源重建
    try:
        result = save_task_state(run_dir, session, meta)
    except StoreError as exc:
        if not existing.is_file():
            raise
        # 库文件不可用：先隔离留存（不就地删除证据），再由 JSON 导出重建
        quarantine = existing.with_name(
            f"{DB_FILENAME}.corrupt-{datetime.now().strftime('%Y%m%dT%H%M%S')}")
        try:
            existing.rename(quarantine)
        except OSError:
            raise exc
        result = save_task_state(run_dir, session, meta)
        result["quarantined"] = quarantine.name
        result["note"] = "原任务库损坏，已隔离留存；由 JSON 导出重建"
    result["imported"] = True
    return result


def store_status(run_dir: str | Path) -> dict:
    """只读摘要：库是否存在、版本、与 JSON 导出摘要是否一致（供 status 入口显示）。"""
    run_dir = Path(run_dir)
    db = db_path_for(run_dir)
    if not db.is_file():
        return {"present": False}
    info = {"present": True, "path": db.name}
    try:
        conn = _open_with_schema(db, create=False)
    except StoreError as exc:
        return {"present": True, "error": str(exc)[:200]}
    rows = {}
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        info["db_version"] = int(rows.get("db_version", "0") or 0)
        info["status"] = rows.get("status", "")
        info["state_digest"] = rows.get("state_digest", "")[:12]
        info["updated_at"] = rows.get("updated_at", "")
        (summary,) = conn.execute("PRAGMA quick_check").fetchone()
        if summary != "ok":
            info["error"] = f"quick_check={summary}"
    except sqlite3.DatabaseError as exc:
        info["error"] = f"读取失败（{exc}）"
        return info
    finally:
        try:
            conn.close()
        except Exception:
            pass
    sp = run_dir / "state.json"
    if sp.is_file() and rows.get("state_digest"):
        try:
            exported = json.loads(sp.read_text(encoding="utf-8"))
            exported_digest = (exported.get("task_db") or {}).get("digest", "")
            if exported_digest and exported_digest != rows.get("state_digest"):
                info["export_mismatch"] = True
        except (OSError, ValueError):
            info["export_mismatch"] = True
    return info
