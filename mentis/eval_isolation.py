"""E1 运行端答案读隔离：deny ACL 事务窗口 + 确定性双端探针 + 强绑定准入。

诚实边界：
- 机制是操作系统级 deny ACE（icacls 单次调用写全部账户），对同用户子进程
  （受测 Mentis 环境）和受测 Codex 子进程一并生效；绝对路径/父目录遍历/
  junction/git 历史由确定性探针在双端真实执行环境验证。
- 窗口是可恢复事务：首次修改前原子落盘恢复日志（window_id + 每个目标的
  Get-Acl XML 原档 + 属主 + icacls dump）；release 用原 DACL 恢复并逐项
  核对一致性；恢复失败逐项报告，绝不返回"已恢复"。
- 金丝雀与目标预检只是风险预检（降低锁死概率），不保证永不锁死。
- deny 不防对抗性绕过：同用户属主保留 WRITE_DAC 可自行改 ACL。
- 探针区分权限拒绝、文件不存在、命令错误、链接创建失败四类结果；
  只有真实"权限拒绝"计入隔离通过；意外读到内容只记录指纹。
- 准入绑定 suite/prepared/组/工作区/输入哈希/探针版本/执行器版本与配置/
  window_id/原始记录哈希，并重验双端结果与当前窗口；缺任一即未就绪。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess
import sys
from uuid import uuid4

# 探测证据的保鲜期：过期后准入必须重新探测，不能拿旧窗口的证据开新运行。
DEFAULT_TTL_S = 12 * 3600
# 本机 Codex CLI Windows 沙箱使用的本地账户/组；存在则一并 deny。
EXTRA_DENY_ACCOUNTS = ("CodexSandboxUsers", "CodexSandboxOffline", "CodexSandboxOnline")
ISO_SCHEMA = "mentis-eval-isolation/2"
PROBE_SCHEMA = "mentis-eval-probe/1"
# 探针逻辑版本：变化后旧证据一律失效，必须重新探测。
PROBE_VERSION = "e1-probe/3"
_REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOW_LOCK_DIR = _REPO_ROOT / "runtime" / "evaluation" / "window-locks"


class IsolationError(RuntimeError):
    """隔离窗口操作或验证失败；不得把失败降级为通过。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_identity() -> dict:
    """真实进程令牌身份（whoami），用于证据与准入的身份一致核对。"""
    proc = subprocess.run(["whoami"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=15)
    user = proc.stdout.strip() if proc.returncode == 0 else ""
    if not user:
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    return {"user": user, "host": os.environ.get("COMPUTERNAME", ""), "python": sys.executable}


def _icacls(args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(["icacls", *[str(a) for a in args]], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=30)
    if proc.returncode != 0:
        raise IsolationError(
            f"icacls {' '.join(str(a) for a in args[:2])} 失败（退出码 {proc.returncode}）："
            f"{(proc.stdout + proc.stderr).strip()[:200]}")
    return proc


def _icacls_dump(path: Path) -> str:
    proc = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)
    if proc.returncode != 0:
        raise IsolationError(f"icacls 读取 ACL 失败: {path}: {(proc.stdout + proc.stderr)[:150]}")
    return proc.stdout.replace("\r", "").strip()


def _normalize_dump(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r", "").split("\n")).strip()


def _account_exists(name: str) -> bool:
    for tool in ("localgroup", "user"):
        proc = subprocess.run(["net", tool, name], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20)
        if proc.returncode == 0:
            return True
    return False


def _deny_accounts() -> list[str]:
    """当前用户必须 deny；额外账户（Codex 沙箱组）存在时一并覆盖。"""
    accounts = [current_identity()["user"]]
    accounts += [name for name in EXTRA_DENY_ACCOUNTS if _account_exists(name)]
    return accounts


def _deny_switches(accounts: list[str], is_dir: bool) -> list[str]:
    """所有账户的 deny 写进同一次 icacls 调用：deny 生效后再开第二条
    DACL 写句柄会被自己的 deny 拒绝（实测退出码 5）。"""
    spec = ":(OI)(CI)F" if is_dir else ":F"
    return [switch for account in accounts
            for switch in ("/deny", f"{account}{spec}")]


def _remove_switches(accounts: list[str]) -> list[str]:
    return [switch for account in accounts for switch in ("/remove:d", account)]


def _readable(path: Path) -> bool:
    """预检可读性：文件试读，目录试列举（目录 read_bytes 恒为 PermissionError 不可用）。"""
    try:
        if path.is_dir():
            next(iter(path.iterdir()), None)
        else:
            path.read_bytes()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- ACL 事务

# PowerShell 是本运行依赖：Get-Acl/Export-Clixml 捕获原 DACL，Set-Acl 在 deny
# 生效期间按原档恢复（icacls /restore 需要 SeRestorePrivilege，非提权不可用）。
_CAPTURE_PS = r'''param([string]$ListFile, [string]$OutFile)
$ErrorActionPreference = "Continue"
$items = Get-Content -LiteralPath $ListFile -Encoding UTF8 -Raw | ConvertFrom-Json
$results = @()
foreach ($it in $items) {
  try {
    $a = Get-Acl -LiteralPath $it.path
    $a | Export-Clixml -LiteralPath $it.xml
    $results += [PSCustomObject]@{ path = $it.path; ok = $true; owner = $a.Owner }
  } catch {
    $results += [PSCustomObject]@{ path = $it.path; ok = $false; error = $_.Exception.Message }
  }
}
$results | ConvertTo-Json -Depth 4 | Out-File -LiteralPath $OutFile -Encoding utf8
'''

_RESTORE_PS = r'''param([string]$ListFile, [string]$OutFile)
$ErrorActionPreference = "Continue"
$items = Get-Content -LiteralPath $ListFile -Encoding UTF8 -Raw | ConvertFrom-Json
$results = @()
foreach ($it in $items) {
  try {
    $a = Import-Clixml -LiteralPath $it.xml
    Set-Acl -LiteralPath $it.path -AclObject $a
    $results += [PSCustomObject]@{ path = $it.path; ok = $true; error = "" }
  } catch {
    $results += [PSCustomObject]@{ path = $it.path; ok = $false; error = $_.Exception.Message }
  }
}
$results | ConvertTo-Json -Depth 4 | Out-File -LiteralPath $OutFile -Encoding utf8
'''


def _native_powershell_env() -> dict:
    # Codex 的 PowerShell 7 模块路径不能继承给 Windows PowerShell 5。
    # 后者从自己的 PSHOME/系统配置初始化模块路径。Windows 环境键通常为大写。
    return {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}


def _run_ps_script(script_path: Path, list_file: Path, out_file: Path) -> list[dict]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(script_path), "-ListFile", str(list_file), "-OutFile", str(out_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        env=_native_powershell_env())
    if not out_file.is_file():
        raise IsolationError(
            f"ACL 脚本未产出结果（rc={proc.returncode}）：{(proc.stdout + proc.stderr)[:200]}")
    payload = json.loads(out_file.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, list) else [payload]


def _single_restore_list(wdir: Path, target: dict) -> Path:
    """单目标恢复清单（排练用）。"""
    list_file = wdir / f"rehearse-{uuid4().hex[:8]}.json"
    list_file.write_text(json.dumps(
        [{"path": target["path"], "xml": str(wdir / target["acl_xml"])}],
        ensure_ascii=False, indent=2), encoding="utf-8-sig")
    return list_file


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _canary_check(parent: Path, accounts: list[str]) -> None:
    """风险预检（非保证）：在目标同父目录验证 deny 可生效且可解除。

    Python 3.14 tempfile 根带 OWNER RIGHTS ACE，其下 deny 生效后会同时
    压掉属主 WRITE_DAC——金丝雀提前暴露这种环境，但不能保证真实目标
    一定无锁死风险（属主/权限差异仍以目标预检与恢复日志为准）。
    """
    canary = parent / f"_iso-canary-{uuid4().hex[:8]}"
    try:
        canary.mkdir()
        secret = canary / "secret.txt"
        secret.write_bytes(b"canary")
        _icacls([canary, *_deny_switches(accounts, is_dir=True)])
        try:
            secret.read_bytes()
            raise IsolationError(f"金丝雀 deny 未生效，放弃开窗: {parent}")
        except PermissionError:
            pass
        _icacls([canary, *_remove_switches(accounts)])
        if not _readable(secret):
            raise IsolationError(
                f"金丝雀 deny 无法解除（该目录 ACL 世系存在锁死风险），已放弃开窗；"
                f"可能残留金丝雀目录需手工清理: {canary}")
    finally:
        shutil.rmtree(canary, ignore_errors=True)


def _assert_no_overlapping_window(protected: list[Path]) -> None:
    """共享保护范围互斥（前置检查）：与任何活动窗口的受保护路径相交时拒绝开窗，
    防止并发窗口互相覆盖、一次 release 解除另一次运行的保护。"""
    WINDOW_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    for existing in WINDOW_LOCK_DIR.glob("window-*.json"):
        try:
            other = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        overlap = set(other.get("protected", [])) & {str(p) for p in protected}
        if overlap:
            raise IsolationError(
                f"已有活动隔离窗口覆盖相同受保护路径（{existing.name}，"
                f"交集 {len(overlap)} 项）；先 release 该窗口或人工核对后再开窗")


def _register_lock(window_id: str, protected: list[str], window_dir: Path) -> Path:
    """登记窗口锁（互斥已在开窗前置检查）；锁内记录 window_dir，
    进程崩溃后仍可按恢复日志事务回滚。"""
    WINDOW_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = WINDOW_LOCK_DIR / f"window-{window_id}.json"
    try:
        with lock.open("x", encoding="utf-8") as fh:
            json.dump({"window_id": window_id, "protected": protected,
                       "window_dir": str(window_dir),
                       "created_at": _iso(_utcnow())}, fh, ensure_ascii=False, indent=2)
    except FileExistsError as exc:
        raise IsolationError(f"窗口锁已存在: {lock}") from exc
    return lock


def _release_lock(window_id: str) -> None:
    lock = WINDOW_LOCK_DIR / f"window-{window_id}.json"
    if lock.is_file():
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
            if payload.get("window_id") != window_id:
                raise IsolationError(f"窗口锁 window_id 不匹配，拒绝解除: {lock}")
        except json.JSONDecodeError:
            pass
        lock.unlink(missing_ok=True)


def _prepare_recovery(protected: list[Path], accounts: list[str],
                      isolation_dir: Path, extra: dict) -> dict:
    """首次权限修改前的全部准备：原 DACL 存档、属主、dump、目标预检、原子日志。

    任何一步失败都在改动任何 ACL 之前抛出；返回恢复日志（已含 window_id）。
    """
    protected = [Path(p).resolve() for p in protected]
    for path in protected:
        if not _readable(path):
            raise IsolationError(f"受保护路径当前不可读（窗口可能已开启）: {path}")
    window_id = uuid4().hex
    wdir = isolation_dir / f"window-{window_id}"
    wdir.mkdir(parents=True)
    capture_ps = wdir / "acl-capture.ps1"
    restore_ps = wdir / "acl-restore.ps1"
    capture_ps.write_text(_CAPTURE_PS, encoding="utf-8-sig")
    restore_ps.write_text(_RESTORE_PS, encoding="utf-8-sig")
    list_payload = [{"path": str(p), "xml": str(wdir / f"acl-{i:02d}.xml")}
                    for i, p in enumerate(protected)]
    list_file = wdir / "acl-list.json"
    list_file.write_text(json.dumps(list_payload, ensure_ascii=False, indent=2),
                         encoding="utf-8-sig")
    capture_out = wdir / "acl-capture-result.json"
    captures = {row["path"]: row for row in _run_ps_script(capture_ps, list_file, capture_out)}
    targets = []
    for i, path in enumerate(protected):
        row = captures.get(str(path))
        if row is None or not row.get("ok"):
            raise IsolationError(
                f"目标原 DACL 捕获失败，未做任何权限修改: {path}: "
                f"{(row or {}).get('error', '无记录')}")
        xml = Path(list_payload[i]["xml"])
        if not xml.is_file() or xml.stat().st_size == 0:
            raise IsolationError(f"原 DACL XML 未落盘，未做任何权限修改: {path}")
        targets.append({
            "path": str(path), "kind": "dir" if path.is_dir() else "file",
            "acl_xml": xml.name, "owner": row.get("owner", ""),
            "dump": _icacls_dump(path),
        })
    # 目标恢复能力预检：把刚捕获的原档 Set-Acl 回去（无语义变化），证明
    # 恢复机制在实际目标上成立；随后对每个目标做一次真实 deny 排练
    # （deny→验证拒读→原档恢复→dump 一致），确保正式开窗前 modify+restore
    # 两个方向都被实测过。任何失败都在改动前中止（逐项恢复后抛出）。
    precheck_out = wdir / "acl-precheck-result.json"
    prechecks = {row["path"]: row for row in _run_ps_script(restore_ps, list_file, precheck_out)}
    for target in targets:
        row = prechecks.get(target["path"])
        if row is None or not row.get("ok"):
            raise IsolationError(
                f"目标恢复权限预检失败（Set-Acl 原档回写未通过），未做任何权限修改: "
                f"{target['path']}: {(row or {}).get('error', '无记录')}")
        if _normalize_dump(_icacls_dump(Path(target["path"]))) != _normalize_dump(target["dump"]):
            raise IsolationError(f"目标预检后 ACL 与捕获不一致，未做任何权限修改: {target['path']}")
    rehearsed = []
    probe_of = {t["protected"]: t["probe"] for t in extra.get("verification_targets", [])}
    try:
        for target in targets:
            path = Path(target["path"])
            _icacls([path, *_deny_switches(accounts, target["kind"] == "dir")])
            try:
                Path(probe_of[target["path"]]).read_bytes()
                raise IsolationError(f"deny 排练未生效: {path}")
            except PermissionError:
                pass
            restore_rows = _run_ps_script(
                restore_ps, _single_restore_list(wdir, target), wdir / "acl-rehearse-result.json")
            if not restore_rows or not restore_rows[0].get("ok"):
                raise IsolationError(
                    f"deny 排练后的恢复失败: {path}: "
                    f"{(restore_rows[0].get('error') if restore_rows else '无结果')}")
            if _normalize_dump(_icacls_dump(path)) != _normalize_dump(target["dump"]):
                raise IsolationError(f"deny 排练恢复后 ACL 与原档不一致: {path}")
            rehearsed.append(target["path"])
    except IsolationError as exc:
        # 排练失败：逐项恢复全部目标（原档回写对未改动目标无语义影响）后中止；
        # 未进入正式 deny，未登记锁。
        _run_ps_script(restore_ps, list_file, wdir / "acl-rehearse-rollback.json")
        raise IsolationError(
            f"deny 排练失败，未开启窗口，已按原档恢复全部目标: {exc}") from exc
    log = {
        "schema": ISO_SCHEMA, "window_id": window_id,
        "created_at": _iso(_utcnow()), "identity": current_identity(),
        "accounts": accounts, "targets": targets, "window_dir": str(wdir),
        "capture_script": capture_ps.name, "restore_script": restore_ps.name,
        "note": "原 DACL 以 Get-Acl/Export-Clixml 存档；恢复 = Set-Acl 原档回写 + "
                "icacls dump 逐项一致性核对；/remove:d 仅作恢复失败时的兜底并如实报告。",
        **extra,
    }
    # 原子落盘恢复日志：先写 tmp 再 replace；失败则不开窗。
    try:
        _write_atomic(wdir / "window.json", json.dumps(log, ensure_ascii=False, indent=2))
    except OSError as exc:
        raise IsolationError(f"恢复日志写入失败，未做任何权限修改: {exc}") from exc
    return log


def _restore_targets(log: dict, wdir: Path) -> dict:
    """事务恢复：Set-Acl 原档回写每个目标 → dump 逐项核对；失败逐项报告。"""
    accounts = log["accounts"]
    list_payload = [{"path": t["path"], "xml": str(wdir / t["acl_xml"])}
                    for t in log["targets"]]
    list_file = wdir / "restore-list.json"
    list_file.write_text(json.dumps(list_payload, ensure_ascii=False, indent=2),
                         encoding="utf-8-sig")
    restore_ps = wdir / log.get("restore_script", "acl-restore.ps1")
    out_file = wdir / "acl-restore-result.json"
    results = {row["path"]: row for row in _run_ps_script(restore_ps, list_file, out_file)}
    restored, failed = [], []
    for target in log["targets"]:
        path = target["path"]
        row = results.get(path)
        ok = bool(row and row.get("ok"))
        detail = ""
        if ok:
            if _normalize_dump(_icacls_dump(Path(path))) != _normalize_dump(target["dump"]):
                ok = False
                detail = "Set-Acl 后 ACL 与原档 dump 不一致"
        else:
            detail = (row or {}).get("error", "无 Set-Acl 结果")
        if not ok:
            # 兜底：/remove:d 解除 deny（可能不是精确原档），恢复可读后仍须报告差异
            try:
                _icacls([path, *_remove_switches(accounts)])
                fallback_dump_ok = (_normalize_dump(_icacls_dump(Path(path)))
                                    == _normalize_dump(target["dump"]))
            except IsolationError as exc:
                fallback_dump_ok = False
                detail += f"；/remove:d 兜底也失败: {exc}"
            else:
                detail += "；已用 /remove:d 兜底解除 deny" + (
                    "且 dump 与原档一致" if fallback_dump_ok else "，但 dump 与原档不一致（非精确恢复）")
                ok = fallback_dump_ok
        entry = {"path": path, "restored": ok, "detail": detail}
        (restored if ok else failed).append(entry)
    return {"restored": restored, "failed": failed,
            "verified": not failed and len(restored) == len(log["targets"])}


def open_window(protected: list[Path], isolation_dir: Path,
                extra: dict | None = None) -> dict:
    """开启隔离窗口（事务）：验证点 → 捕获原 DACL → 原子日志 → 锁 → deny → live 验证。

    deny 任一目标失败即按恢复日志做事务恢复并抛出，不留半开窗口。
    """
    isolation_dir = Path(isolation_dir)
    isolation_dir.mkdir(parents=True, exist_ok=True)
    protected = [Path(p).resolve() for p in protected]
    # 互斥前置检查：与活动窗口受保护范围相交时，在触碰任何路径之前拒绝
    _assert_no_overlapping_window(protected)
    accounts = _deny_accounts()
    # 验证点必须在窗口开启前固定（deny 后无法再枚举目录），并随恢复日志原子落盘
    targets = verification_targets(protected)
    for parent in sorted({p.parent for p in protected}):
        _canary_check(parent, accounts)
    log = _prepare_recovery(protected, accounts, isolation_dir,
                            {**(extra or {}), "verification_targets": targets})
    window_id = log["window_id"]
    wdir = isolation_dir / f"window-{window_id}"
    _register_lock(window_id, [str(p) for p in protected], wdir)
    try:
        for target in log["targets"]:
            _icacls([Path(target["path"]),
                     *_deny_switches(accounts, target["kind"] == "dir")])
    except IsolationError as exc:
        outcome = _restore_targets(log, wdir)
        _release_lock(window_id)
        raise IsolationError(
            f"deny 阶段失败（{exc}），已按恢复日志事务回滚"
            f"（失败 {len(outcome['failed'])} 项）："
            + json.dumps(outcome["failed"], ensure_ascii=False))
    live = _expect_denied(log["verification_targets"])
    failures = [row for row in live if not row["denied"]]
    if failures:
        outcome = _restore_targets(log, wdir)
        _release_lock(window_id)
        raise IsolationError("deny 后仍有受保护路径可读，隔离窗口无效（已事务回滚）: "
                             + json.dumps(failures, ensure_ascii=False)
                             + "；回滚结果: " + json.dumps(outcome["failed"], ensure_ascii=False))
    log["window_dir"] = str(wdir)
    return log


def _expect_denied(targets: list[dict]) -> list[dict]:
    """窗口开启后的判定性检查：每个验证点此刻必须读不到。"""
    results = []
    for target in targets:
        try:
            Path(target["probe"]).read_bytes()
        except PermissionError:
            results.append({"probe": target["probe"], "denied": True, "error": "PermissionError"})
        except OSError as exc:
            results.append({"probe": target["probe"], "denied": False,
                            "error": f"{type(exc).__name__}: {exc}"})
        else:
            results.append({"probe": target["probe"], "denied": False,
                            "error": "读取成功——deny 未生效"})
    return results


def release_window(log: dict) -> dict:
    """事务释放：按恢复日志恢复每个目标的原 DACL 并逐项核对一致性。

    恢复失败逐项报告（restored=false + 原因），绝不返回整体"已恢复"。
    """
    wdir = Path(log["window_dir"]) if log.get("window_dir") else None
    if wdir is None or not wdir.is_dir():
        raise IsolationError("恢复日志缺少 window_dir，无法事务恢复；需人工核对 ACL")
    outcome = _restore_targets(log, wdir)
    _release_lock(log["window_id"])
    return outcome


# ---------------------------------------------------------------- 探针

_PROBE_RUNNER = r'''
"""E1 确定性权限探针（stdlib only；在受测进程/执行器环境内运行）。

四类反向结果严格区分：denied（权限拒绝）/ not_found（文件不存在）/
error（其他命令或 IO 错误）/ link_create_failed（链接创建失败）。
只有 denied 计入隔离通过。意外读到内容只记录 sha256 指纹与长度。
"""
import argparse, hashlib, json, os, subprocess, sys, uuid
from pathlib import Path

PROBE_SCHEMA = "mentis-eval-probe/1"


def classify_read(path):
    try:
        raw = Path(path).read_bytes()
    except PermissionError as exc:
        return {"outcome": "denied", "error": "PermissionError winerror=%s" % getattr(exc, "winerror", "")}
    except FileNotFoundError as exc:
        return {"outcome": "not_found", "error": "FileNotFoundError"}
    except OSError as exc:
        return {"outcome": "error", "error": type(exc).__name__ + ": " + str(exc)}
    digest = hashlib.sha256(raw).hexdigest()
    return {"outcome": "READ", "sha256": digest, "bytes": len(raw)}


def classify_git_show(repo, spec):
    try:
        proc = subprocess.run(["git", "-C", str(repo), "show", spec],
                              capture_output=True, timeout=30)
    except OSError as exc:
        return {"outcome": "error", "error": type(exc).__name__ + ": " + str(exc)}
    if proc.returncode == 0:
        digest = hashlib.sha256(proc.stdout).hexdigest()
        return {"outcome": "READ", "sha256": digest, "bytes": len(proc.stdout)}
    err = proc.stderr.decode("utf-8", errors="replace")
    lowered = err.lower()
    if ("denied" in lowered or "拒绝访问" in err or "permission" in lowered
            or "unable to access" in lowered or "could not read" in lowered
            or "not a git repository" in lowered or "不是 git 仓库" in err):
        # 仓库对象库本身不可访问（含 deny 后 git 报 not a git repository）：
        # 历史副本不可读，语义上属于拒绝
        return {"outcome": "denied", "error": err.strip()[:200]}
    if "does not exist in" in lowered or "pathspec" in lowered:
        return {"outcome": "not_found", "error": err.strip()[:200]}
    return {"outcome": "error", "error": ("rc=%d " % proc.returncode) + err.strip()[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workspace", required=True)
    args = ap.parse_args()
    workspace = Path(args.workspace).resolve()
    targets = json.loads(Path(args.targets).read_text(encoding="utf-8-sig"))
    entries = []
    for t in targets:
        if t.get("kind") == "git_history":
            entries.append({"id": t["id"], "form": "git_show",
                            **classify_git_show(t["repo"], t["spec"])})
            continue
        target = Path(t["path"])
        entries.append({"id": t["id"], "form": "absolute", **classify_read(target)})
        try:
            rel = os.path.relpath(target, workspace)
        except ValueError:
            # Windows 相对路径无法跨盘符寻址：该访问形式结构性不可达（如实标注，
            # 不冒充 denied——同一目标的绝对路径与 junction 形式仍真实实测）。
            entries.append({"id": t["id"], "form": "parent_traversal",
                            "outcome": "structurally_unreachable",
                            "error": "目标与工作区在不同盘符，相对路径无法跨盘寻址"})
        else:
            entries.append({"id": t["id"], "form": "parent_traversal",
                            **classify_read(workspace / rel)})
        link = workspace / (".isolation-junction-" + uuid.uuid4().hex[:8])
        try:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target.parent)],
                           capture_output=True, text=True, timeout=20)
            if os.path.lexists(str(link)):
                entries.append({"id": t["id"], "form": "junction",
                                **classify_read(link / target.name)})
            else:
                entries.append({"id": t["id"], "form": "junction",
                                "outcome": "link_create_failed", "error": "mklink 未创建链接"})
        finally:
            if os.path.lexists(str(link)):
                subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, timeout=20)
    positive = {}
    for name in ("input.json", "prompt.md"):
        p = workspace / name
        try:
            raw = p.read_bytes()
            positive[name] = {"readable": True, "bytes": len(raw),
                              "sha256": hashlib.sha256(raw).hexdigest()}
        except OSError as exc:
            positive[name] = {"readable": False, "error": type(exc).__name__ + ": " + str(exc)}
    write_path = workspace / "probe-positive-write.txt"
    try:
        write_path.write_text("probe-write-ok", encoding="utf-8")
        raw = write_path.read_bytes()
        positive["workspace_write"] = {"ok": True,
                                       "sha256": hashlib.sha256(raw).hexdigest()}
    except OSError as exc:
        positive["workspace_write"] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
    result = {"schema": PROBE_SCHEMA, "probe_version": PROBE_VERSION_PLACEHOLDER,
              "workspace": str(workspace), "cwd": os.getcwd(),
              "user_env": os.environ.get("USERNAME", ""),
              "entries": entries, "positive": positive}
    text = json.dumps(result, ensure_ascii=False)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
'''.replace("PROBE_VERSION_PLACEHOLDER", repr(PROBE_VERSION))


def expected_probe_entries(targets: list[dict]) -> set[tuple[str, str]]:
    """探针必须覆盖的 (id, form) 全集。"""
    expected = set()
    for t in targets:
        if t.get("kind") == "git_history":
            expected.add((t["id"], "git_show"))
        else:
            expected.add((t["id"], "absolute"))
            expected.add((t["id"], "parent_traversal"))
            expected.add((t["id"], "junction"))
    return expected


def judge_probe_result(payload: dict, targets: list[dict],
                       expected_input_sha: str = "") -> list[str]:
    """确定性判定：只有全部条目真实 denied 且正向哈希匹配才通过。

    not_found / error / link_create_failed 都不计入通过，逐项报告。
    """
    return judge_probe_entries(payload, sorted(expected_probe_entries(targets)),
                               expected_input_sha)


def judge_probe_entries(payload: dict, expected_entries: list,
                        expected_input_sha: str = "") -> list[str]:
    """确定性判定：只有全部条目真实 denied（或结构性不可达）且正向哈希匹配才通过。

    not_found / error / link_create_failed 都不计入通过，逐项报告。
    structurally_unreachable 仅用于跨盘符目标的 parent_traversal 形式：
    Windows 相对路径无法跨盘寻址，该形式不可能发生（同一目标的绝对路径与
    junction 形式仍实测拒读）；条目原样保留在探针证据中可审计。
    """
    problems = []
    if payload.get("schema") != PROBE_SCHEMA or payload.get("probe_version") != PROBE_VERSION:
        problems.append(f"探针结果 schema/版本不符: {payload.get('schema')}/"
                        f"{payload.get('probe_version')}")
        return problems
    seen = {(e.get("id"), e.get("form")): e for e in payload.get("entries", [])}
    missing = set(map(tuple, expected_entries)) - set(seen)
    if missing:
        problems.append(f"探针条目缺失 {len(missing)} 项: " + ", ".join(
            f"{i}/{f}" for i, f in sorted(missing)[:5]) + ("…" if len(missing) > 5 else ""))
    for (tid, form), entry in sorted(seen.items()):
        outcome = entry.get("outcome")
        if outcome == "READ":
            problems.append(f"反向证据失败：{tid}/{form} 读到受保护内容"
                            f"（指纹 {entry.get('sha256', '')[:16]}，{entry.get('bytes')} 字节）")
        elif outcome in ("denied", "structurally_unreachable"):
            continue
        elif outcome == "not_found":
            problems.append(f"探针异常：{tid}/{form} 文件不存在（清单与磁盘不一致）")
        elif outcome == "link_create_failed":
            problems.append(f"探针未验证：{tid}/{form} 链接创建失败，不能计入拒读通过")
        else:
            problems.append(f"探针错误：{tid}/{form} {entry.get('error', '未知错误')}")
    positive = payload.get("positive", {})
    if not positive.get("input.json", {}).get("readable"):
        problems.append("正向证据缺失：工作区 input.json 不可读")
    elif expected_input_sha and positive["input.json"].get("sha256") != expected_input_sha:
        problems.append("正向证据不匹配：input.json 哈希与准备目录不一致")
    if not positive.get("prompt.md", {}).get("readable"):
        problems.append("正向证据缺失：工作区 prompt.md 不可读")
    if not positive.get("workspace_write", {}).get("ok"):
        problems.append("正向证据缺失：工作区写入未确认")
    return problems


def build_probe_targets(verification_targets: list[dict], suite_path: Path) -> list[dict]:
    """探针目标清单：全部 ACL 验证点 + git 历史中的答案副本 + .git 元数据。

    仓库根由 suite 布局推导；套件不在仓库布局内（测试夹具）时不产生 git 目标。
    """
    targets = []
    for index, t in enumerate(verification_targets, start=1):
        targets.append({"id": f"T{index:02d}", "kind": "file", "path": t["probe"]})
    repo = _repo_root_for(suite_path)
    if repo is None:
        return targets
    reviewer = suite_path.parent / "reviewer.json"
    try:
        spec = "HEAD:" + reviewer.resolve().relative_to(repo).as_posix()
    except ValueError:
        return targets
    targets.append({"id": "GIT-ANSWER-COPY", "kind": "git_history",
                    "repo": str(repo), "spec": spec})
    targets.append({"id": "GIT-METADATA", "kind": "file",
                    "path": str(_git_metadata_roots(repo)[-1] / "config")})
    return targets


def run_deterministic_probe(workspace: Path, targets: list[dict], out_dir: Path,
                            runner_path: Path, python_exe: str,
                            label: str, expected_input_sha: str) -> dict:
    """在指定环境内运行确定性探针；目标清单、原始 stdout 与结果 JSON 落盘。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    targets_file = out_dir / f"{label}-probe-targets.json"
    targets_file.write_text(json.dumps(targets, ensure_ascii=False, indent=2),
                            encoding="utf-8-sig")
    result_path = out_dir / f"{label}-probe-result.json"
    proc = subprocess.run(
        [python_exe, "-I", "-X", "utf8", str(runner_path),
         "--targets", str(targets_file),
         "--out", str(result_path), "--workspace", str(workspace)],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    (out_dir / f"{label}-probe-stdout.txt").write_text(
        f"### exit={proc.returncode}\n### cmd={python_exe} -I -X utf8 <probe_runner> …\n"
        + proc.stdout + ("\n### stderr:\n" + proc.stderr if proc.stderr else ""),
        encoding="utf-8")
    if proc.returncode != 0 or not result_path.is_file():
        raise IsolationError(f"{label} 环境探针失败（退出码 {proc.returncode}），"
                             "原始输出见 probe-stdout")
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    problems = judge_probe_result(payload, targets, expected_input_sha)
    return {"payload": payload, "problems": problems, "result_path": str(result_path)}


# ---------------------------------------------------------------- 清单与准入

def _repo_root_for(suite_path: Path) -> Path | None:
    """从 suite 位置推导所在仓库根（docs/evaluation/e1/suite.json → 仓库根）。

    仅当 suite 相对某含 .git 祖先的路径符合 docs/evaluation/… 冻结布局时认定；
    suite 不在该布局内（如测试夹具）时返回 None，不产生 .git 目标。
    """
    suite_path = Path(suite_path).resolve()
    for parent in suite_path.parents:
        if not (parent / ".git").exists():
            continue
        try:
            relative = suite_path.relative_to(parent).as_posix()
        except ValueError:
            continue
        if relative.startswith("docs/evaluation/"):
            return parent
    return None


def _git_metadata_roots(repo: Path) -> list[Path]:
    """工作树的 .git 是指针文件；共同 Git 数据库也必须拒读，不能只遮住指针。"""
    marker = repo / ".git"
    if marker.is_dir():
        return [marker.resolve()]
    content = marker.read_text(encoding="utf-8").strip()
    if not content.startswith("gitdir: "):
        raise IsolationError("Git 工作树指针无效，拒绝开窗")
    gitdir = (repo / content.removeprefix("gitdir: ")).resolve()
    common_file = gitdir / "commondir"
    common = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve() if common_file.is_file() else gitdir
    return [marker.resolve(), common]


def protected_manifest(suite_path: Path, prepared_dir: Path, target_arm: str) -> list[Path]:
    """受保护清单：套件目录 + 仓库 .git（历史答案副本）+ 当前准备目录的身份
    映射与非目标组 + 其余历史导出目录整体。

    prepare-report 中的路径全部重新校验：仅限安全相对路径、解析后必须仍在
    prepared 根之内；拒绝绝对路径、..、链接逃逸与根外 ACL 目标。
    """
    from mentis.evaluation import _path

    suite_path, prepared_dir = Path(suite_path), Path(prepared_dir).resolve()
    report_path = prepared_dir / "prepare-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    arms = {}
    try:
        for row in report["packages"]:
            neutral = _path(row.get("neutral_id", ""), "prepare-report.neutral_id")
            for arm in row["arms"]:
                arm_rel = _path(arm.get("path", ""), "prepare-report.arm.path")
                if arm_rel == target_arm:
                    arms[arm_rel] = True
                    continue
                resolved = (prepared_dir / arm_rel).resolve()
                try:
                    resolved.relative_to(prepared_dir)
                except ValueError as exc:
                    raise IsolationError(
                        f"prepare-report 路径逃逸出 prepared 根: {arm_rel}") from exc
                arms[arm_rel] = False
    except ValueError as exc:
        raise IsolationError(f"prepare-report 路径校验失败（拒绝开窗）: {exc}") from exc
    if target_arm not in arms:
        raise IsolationError(f"target_arm 不在 prepare-report 中: {target_arm}")
    protected = [suite_path.parent.resolve(), report_path.resolve()]
    repo_root = _repo_root_for(suite_path)
    if repo_root is not None:
        protected.extend(_git_metadata_roots(repo_root))
    for arm_rel, is_target in arms.items():
        if not is_target:
            protected.append(prepared_dir / arm_rel)
    # 历史导出（旧 eval-prepare 输出，含旧身份映射与全部组输入）整体保护；
    # 只识别含 prepare-report.json 的导出目录，不碰无关目录。
    eval_root = prepared_dir.parent
    for child in sorted(eval_root.iterdir()):
        if not child.is_dir() or child == prepared_dir or child.name == "window-locks":
            continue
        if not (child / "prepare-report.json").is_file():
            continue
        protected.append(child.resolve())
    # 既有运行与评分导出含决定回答/样本身份，不能从父目录读回这些答案副本。
    for manifest in eval_root.glob("*/*/run-manifest.json"):
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise IsolationError(f"历史运行记录不可核对，拒绝开窗：{manifest}")
        if str(record.get("schema_version", "")).startswith("mentis-eval-run/"):
            protected.append(manifest.parent.resolve())
    for private_map in eval_root.glob("*/private-map.json"):
        protected.append(private_map.parent.resolve())
    return protected


def verification_targets(protected: list[Path]) -> list[dict]:
    """deny 前记录每个受保护路径的验证点（文件本身；目录取代表性文件，
    优先敏感文件名）。验证点同时是双端探针的反向目标。"""
    _PREFERRED = ("reviewer.json", "suite.json", "prepare-report.json",
                  "input.json", "prompt.md", "config")
    targets = []
    for path in protected:
        path = Path(path)
        if path.is_file():
            targets.append({"protected": str(path), "probe": str(path),
                            "sha256_at_apply": _sha_file(path), "kind": "file"})
        elif path.is_dir():
            children = sorted(p for p in path.rglob("*") if p.is_file())
            child = next((p for p in children if p.name.casefold() in _PREFERRED), None)
            child = child or (children[0] if children else None)
            if child is None:
                raise IsolationError(f"受保护目录没有可验证的文件: {path}")
            targets.append({"protected": str(path), "probe": str(child),
                            "sha256_at_apply": _sha_file(child), "kind": "dir"})
        else:
            raise IsolationError(f"受保护路径不存在: {path}")
    return targets


def _codex_processes_since(since_iso: str, scope: str = "") -> list[dict] | None:
    """核对执行器是否仍存活（PowerShell CIM 按创建时间过滤）。

    返回 None 表示查询失败——调用方必须按"可能存活"处理（保守保留窗口）。
    匹配语义（2026-10-04 精确化，修复 IDE/MCP 辅助进程误报）：
    - codex 名进程：除已知 IDE 常驻（app-server/computer-use/cua_node）外一律视为
      潜在写入者（受测执行器本体是 codex.exe，不得漏判）；
    - node 名进程：仅在命令行引用 scope（本运行的工作区/运行目录）时视为潜在
      写入者；会话工具的无关 MCP server（如 ./mcp/server.mjs）与受测工作区无关，
      不再阻塞释放。scope 为空时保持旧行为（任何 node 都算，保守）。
    """
    try:
        since_iso = datetime.fromisoformat(since_iso).isoformat()
    except ValueError:
        return None
    if scope:
        if "'" in scope:
            return None  # scope 路径含单引号无法安全内插，按"查询失败"保守处理
        node_clause = ("-or ($_.Name -match 'node' "
                       "-and $_.CommandLine -match [regex]::Escape('" + scope + "'))")
    else:
        node_clause = "-or $_.Name -match 'node'"
    script = (f"$since = Get-Date('{since_iso}'); "
              "Get-CimInstance Win32_Process | Where-Object { "
              "$_.ProcessId -ne $PID "
              "-and $_.CreationDate -and $_.CreationDate -gt $since "
              "-and (($_.Name -match 'codex' "
              "-and $_.CommandLine -notmatch 'app-server|computer-use|cua_node') "
              + node_clause + ") } | "
              "Select-Object ProcessId,Name,CreationDate,CommandLine | "
              "ConvertTo-Json -Depth 3")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, env=_native_powershell_env())
        if proc.returncode != 0:
            return None
        payload = json.loads(proc.stdout) if proc.stdout.strip() else []
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None
    rows = payload if isinstance(payload, list) else [payload]
    return [{"pid": row.get("ProcessId"), "name": row.get("Name"),
             "created": str(row.get("CreationDate", "")),
             "cmdline": str(row.get("CommandLine", ""))[:200]} for row in rows]


def run_probe_flow(suite_path: Path, prepared_dir: Path, target_arm: str,
                   isolation_dir: Path, adapter=None, python_exe: str = sys.executable,
                   codex_timeout_s: int = 540, run_spec: Path | None = None) -> dict:
    """完整探测流程：审计 → 事务开窗 → 双端确定性探针 →（全部通过时）写正式证据。

    - adapter=None 只产生单端调试结果（debug-mentis-probe.json），不写正式
      双端准入证据，ok 恒为 False。
    - Codex 状态 unknown/超时/异常时先核对执行器是否仍存活；未确认停止前
      不恢复其对作者资料的读取（窗口与锁保留，release 交由人工核对后执行）。
    """
    suite_path, prepared_dir = Path(suite_path), Path(prepared_dir)
    isolation_dir = Path(isolation_dir)
    isolation_dir.mkdir(parents=True, exist_ok=True)
    from mentis.evaluation import audit_suite
    audit = audit_suite(suite_path)
    frozen = {check["name"]: check for check in audit["checks"]}
    if audit["status"] == "invalid" or not frozen["frozen_source_hashes"]["passed"]:
        raise IsolationError(f"冻结源核对未通过: {audit.get('blocking_reasons')}")
    execution_binding = {}
    from mentis.eval_readiness import workspace_hashes

    package_hashes = workspace_hashes(prepared_dir / target_arm)
    if run_spec:
        from mentis.eval_readiness import binding

        execution_binding = binding(Path(run_spec), audit["suite_sha256"])
    protected = protected_manifest(suite_path, prepared_dir, target_arm)
    targets = verification_targets(protected)
    probe_targets = build_probe_targets(targets, suite_path)
    log = open_window(protected, isolation_dir, extra={
        "suite_path": str(suite_path.resolve()),
        "suite_sha256": audit["suite_sha256"],
        "prepared_dir": str(prepared_dir.resolve()),
        "target_arm": target_arm,
        "verification_targets": targets,
        "probe_version": PROBE_VERSION,
        "execution_binding": execution_binding,
        "package_hashes": package_hashes,
    })
    wdir = isolation_dir / f"window-{log['window_id']}"
    stamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    probe_dir = isolation_dir / f"probe-{stamp}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    arm_dir = prepared_dir / target_arm
    workspace = probe_dir / "workspace"
    try:
        shutil.copytree(arm_dir, workspace)
        # Codex 适配器要求 Git 仓库根；探针与目标文件同样要在工作区内
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, timeout=60)
        runner_path = workspace / "probe_runner.py"
        runner_path.write_text(_PROBE_RUNNER, encoding="utf-8")
        (workspace / "probe-targets.json").write_text(
            json.dumps(probe_targets, ensure_ascii=False, indent=2), encoding="utf-8-sig")
        expected_input_sha = _sha_file(arm_dir / "input.json")
        mentis = run_deterministic_probe(workspace, probe_targets, probe_dir,
                                         runner_path, python_exe, "mentis",
                                         expected_input_sha)
        (probe_dir / "probe_runner.py").write_text(_PROBE_RUNNER, encoding="utf-8")
        codex = {"status": "skipped", "payload": None, "problems": []}
        if adapter is not None:
            codex = _run_codex_probe(adapter, workspace, probe_targets, probe_dir,
                                     runner_path, expected_input_sha, codex_timeout_s)
        problems = mentis["problems"] + codex["problems"]
        outcome = {"problems": problems, "window": log, "probe_dir": str(probe_dir),
                   "mentis_problems": mentis["problems"],
                   "codex_problems": codex["problems"],
                   "codex_status": codex["status"],
                   "audit_suite_sha256": audit["suite_sha256"],
                   "workspace": str(workspace), "probe_version": PROBE_VERSION}
        if problems:
            outcome.update(_post_failure(log, adapter, probe_dir))
            return outcome
        if adapter is None:
            # 单端调试：不生成正式双端证据
            (isolation_dir / "debug-mentis-probe.json").write_text(
                json.dumps({"note": "单端调试结果，不构成双端准入证据",
                            "mentis_problems": mentis["problems"],
                            "probe_dir": str(probe_dir)}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            release_window(log)
            outcome.update({"ok": False, "debug": True, "evidence": None,
                            "window_released": True})
            return outcome
        evidence_path = isolation_dir / "isolation-evidence.json"
        evidence = write_evidence(evidence_path, audit, log, mentis, codex, probe_dir)
        outcome.update({"ok": True, "debug": False, "evidence": evidence,
                        "evidence_path": str(evidence_path),
                        "window_released": False})
        return outcome
    except Exception as exc:  # 探测/写证据异常：先核对执行器存活再决定是否恢复
        alive = _codex_processes_since(log["created_at"], scope=str(workspace))
        if alive is None or alive:
            return {"ok": False, "debug": False, "evidence": None,
                    "problems": [f"探测异常且执行器可能仍存活（{(alive or []) and len(alive)} 进程"
                                 f"{'，存活状态无法确认' if alive is None else ''}），"
                                 "窗口与锁保留，不得恢复作者资料读取",
                                 f"原始错误: {type(exc).__name__}: {exc}"],
                    "window": log, "probe_dir": str(probe_dir),
                    "window_kept": True, "alive_processes": alive or [],
                    "codex_status": "unknown", "probe_version": PROBE_VERSION,
                    "audit_suite_sha256": audit["suite_sha256"],
                    "workspace": str(workspace), "mentis_problems": [],
                    "codex_problems": ["探测异常，Codex 端未取得完整结果"]}
        release_window(log)
        raise


def _run_codex_probe(adapter, workspace: Path, probe_targets: list[dict],
                     probe_dir: Path, runner_path: Path, expected_input_sha: str,
                     timeout_s: int) -> dict:
    """真实 Codex 执行环境探测：委托其原样运行确定性探针脚本。"""
    from mentis.executors.codex_cli import CodexTaskSpec

    probe_out_rel = "probe-codex/result.json"
    (workspace / "probe-codex").mkdir(exist_ok=True)
    prompt = (
        "评测基础设施自检任务。只做一件事：在当前工作目录原样执行下面这条命令并等待它结束：\n"
        "py -3.14 -X utf8 probe_runner.py --targets probe-targets.json --out "
        f"{probe_out_rel} --workspace {workspace}\n"
        "命令结束后：保留全部文件，不要清理；在回复末尾原样粘贴该命令 stdout 的最后一行"
        "（一行 JSON）。不要执行任何其他命令，不要读取任何其他文件。")
    (probe_dir / "codex-probe-prompt.md").write_text(prompt, encoding="utf-8")
    (probe_dir / "codex-probe-targets.json").write_text(
        json.dumps(probe_targets, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    spec = CodexTaskSpec(workspace=workspace, prompt=prompt, brief_version=1,
                         sandbox="workspace-write", timeout_s=timeout_s,
                         extra_config=('approval_policy="never"',))
    result = adapter.start_task(spec)
    (probe_dir / "codex-probe-response.txt").write_text(
        f"### status={result.status.value}\n### cli_version={result.cli_version}\n"
        f"### native_thread_id={result.native_thread_id}\n### stdin_status={result.stdin_status}\n"
        f"### error={result.error}\n### usage={json.dumps(result.usage, ensure_ascii=False)}\n"
        + result.final_response, encoding="utf-8")
    (probe_dir / "codex-probe-events.json").write_text(json.dumps(
        [{"seq": e.sequence, "kind": e.kind, "item_type": e.item_type,
          "item_status": e.item_status} for e in result.events],
        ensure_ascii=False, indent=2), encoding="utf-8")
    result_path = workspace / probe_out_rel
    payload = None
    if result.status.name == "COMPLETED" and result_path.is_file():
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        problems = judge_probe_result(payload, probe_targets, expected_input_sha)
        return {"status": "completed", "payload": payload, "problems": problems,
                "native_thread_id": result.native_thread_id,
                "cli_version": result.cli_version, "usage": dict(result.usage),
                "sandbox": "workspace-write", "model": spec.model or "cli-default",
                "stdin_status": result.stdin_status}
    if result.status.name != "COMPLETED":
        detail = (f"Codex 探测任务未正常完成（status={result.status.value}，"
                  f"stdin={result.stdin_status}，error={result.error[:160]}）")
    else:
        detail = "Codex 报告完成但未产出探针结果文件（未按指令运行或被沙箱拦截）"
    return {"status": result.status.value, "payload": payload,
            "problems": ["Codex 端探测不可用：" + detail],
            "native_thread_id": result.native_thread_id,
            "cli_version": result.cli_version, "usage": dict(result.usage),
            "sandbox": "workspace-write", "model": spec.model or "cli-default",
            "stdin_status": result.stdin_status}


def _post_failure(log: dict, adapter, probe_dir: Path) -> dict:
    """探测失败收尾：先核对执行器存活；未确认停止前不恢复读取权限。

    存活查询失败（None）按"可能存活"保守处理——窗口与锁保留。
    """
    alive = _codex_processes_since(log["created_at"], scope=str(probe_dir))
    if alive is None or alive:
        return {"ok": False, "debug": False, "evidence": None,
                "window_kept": True, "alive_processes": alive or [],
                "window_released": False,
                "note": "执行器可能仍存活（或存活状态无法确认），窗口与锁保留；"
                        "人工核对执行器已停止后执行 eval-isolate release"}
    release_window(log)
    return {"ok": False, "debug": False, "evidence": None, "window_kept": False,
            "alive_processes": [], "window_released": True}


def write_evidence(out_path: Path, audit: dict, log: dict, mentis: dict,
                   codex: dict, probe_dir: Path) -> dict:
    """探测全部通过后写入正式双端准入证据（schema v2，强绑定）。"""
    frozen = {check["name"]: check for check in audit["checks"]}
    if audit.get("status") == "invalid" or not frozen.get("frozen_source_hashes", {}).get("passed"):
        raise IsolationError(f"冻结源核对未通过，拒绝写隔离证据: {audit.get('blocking_reasons')}")
    arm_dir = Path(log["prepared_dir"]) / log["target_arm"]
    workspace = Path(mentis["payload"]["workspace"])
    expected_entries = sorted(expected_probe_entries(
        json.loads((probe_dir / "mentis-probe-targets.json").read_text(encoding="utf-8-sig"))))
    raw_files = {
        "mentis-probe-result.json": probe_dir / "mentis-probe-result.json",
        "mentis-probe-stdout.txt": probe_dir / "mentis-probe-stdout.txt",
        "mentis-probe-targets.json": probe_dir / "mentis-probe-targets.json",
        "codex-probe-prompt.md": probe_dir / "codex-probe-prompt.md",
        "codex-probe-response.txt": probe_dir / "codex-probe-response.txt",
        "codex-probe-events.json": probe_dir / "codex-probe-events.json",
        "codex-probe-targets.json": probe_dir / "codex-probe-targets.json",
        "probe_runner.py": probe_dir / "probe_runner.py",
        "codex-probe-result.json": workspace / "probe-codex" / "result.json",
        "recovery-log.json": Path(log["window_dir"]) / "window.json",
    }
    raw_hashes = {}
    for name, path in raw_files.items():
        if not path.is_file():
            raise IsolationError(f"探针原始记录缺失，拒绝写证据: {name} ({path})")
        raw_hashes[name] = _sha_file(path)
    evidence = {
        "schema": ISO_SCHEMA,
        "window_id": log["window_id"],
        "created_at": _iso(_utcnow()),
        "valid_until": _iso(_utcnow() + timedelta(seconds=DEFAULT_TTL_S)),
        "identity": current_identity(),
        "execution_binding": log.get("execution_binding", {}),
        "package_hashes": log.get("package_hashes", {}),
        "suite_path": log["suite_path"], "suite_sha256": audit["suite_sha256"],
        "prepared_dir": log["prepared_dir"], "target_arm": log["target_arm"],
        "workspace": str(workspace.resolve()),
        "input_hashes": {
            "input.json": _sha_file(arm_dir / "input.json"),
            "prompt.md": _sha_file(arm_dir / "prompt.md"),
        },
        "probe_version": PROBE_VERSION,
        "expected_entries": expected_entries,
        "executor": {"cli_version": codex.get("cli_version", ""),
                     "sandbox": codex.get("sandbox", ""),
                     "model": codex.get("model", ""),
                     "native_thread_id": codex.get("native_thread_id", ""),
                     "stdin_status": codex.get("stdin_status", ""),
                     "usage": codex.get("usage", {})},
        "frozen_input_checks": {name: frozen[name] for name in
                                ("schema_and_input_whitelist", "source_paths_and_targets",
                                 "frozen_source_hashes")},
        "window": {"protected": [t["path"] for t in log["targets"]],
                   "accounts": log["accounts"],
                   "verification_targets": log["verification_targets"],
                   "window_id": log["window_id"],
                   "recovery_log": str(Path(log["window_dir"]) / "window.json")},
        "mentis_env_probe": {"positive": mentis["payload"]["positive"],
                             "entries": mentis["payload"]["entries"],
                             "cwd": mentis["payload"]["cwd"],
                             "problems": mentis["problems"]},
        "codex_env_probe": {"status": codex["status"],
                            "entries": (codex["payload"] or {}).get("entries", []),
                            "positive": (codex["payload"] or {}).get("positive", {}),
                            "cwd": (codex["payload"] or {}).get("cwd", ""),
                            "problems": codex["problems"]},
        "raw_hashes": raw_hashes,
        "probe_dirs": [str(probe_dir)],
        "released_at": "",
        "mechanism_note": "Windows deny ACL（icacls 单次调用 deny 当前用户与 Codex 沙箱账户）；"
                          "原 DACL 以 Get-Acl XML 存档并事务恢复；双端确定性探针区分 "
                          "denied/not_found/error/link_create_failed，仅 denied 计入通过；"
                          "deny 不防对抗性绕过（属主可自行改 ACL）。",
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return evidence


def _load_raw_probes(evidence: dict) -> tuple[dict, dict]:
    """准入重验：从磁盘读取两端原始探针结果并核对哈希（不只看 problems 为空）。"""
    probe_dir = Path((evidence.get("probe_dirs") or [""])[-1])
    workspace = Path(evidence.get("workspace", ""))
    paths = {"mentis": probe_dir / "mentis-probe-result.json",
             "codex": workspace / "probe-codex" / "result.json"}
    results = {}
    for side, path in paths.items():
        if not path.is_file():
            raise IsolationError(f"{side} 端探针原始记录缺失: {path}")
        key = "mentis-probe-result.json" if side == "mentis" else "codex-probe-result.json"
        recorded = evidence.get("raw_hashes", {}).get(key)
        if recorded and recorded != _sha_file(path):
            raise IsolationError(f"{side} 端原始记录哈希与证据不符: {path}")
        results[side] = json.loads(path.read_text(encoding="utf-8-sig"))
    return results["mentis"], results["codex"]


def check_admission(evidence_path: Path, suite_path: Path = Path(""),
                    expected: dict | None = None, now: datetime | None = None,
                    ttl_s: int = DEFAULT_TTL_S, codex_version: str | None = None) -> dict:
    """运行前准入（v2）：强绑定 + 原始记录重验 + 当前 live 拒读。

    以下任一情况必须不通过：缺证据/schema 不符、窗口已释放、证据过期、
    身份/探针版本/执行器版本变化、任一端探测缺失（skipped）、原始记录缺失
    或哈希不符、suite/prepared/组/工作区不匹配、当前受保护路径可读。
    """
    checks: list[dict] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    def fail_return() -> dict:
        return {"runtime_answer_isolation": {"passed": False, "checks": checks}}

    evidence_path = Path(evidence_path)
    if not evidence_path.is_file():
        add("isolation_evidence_present", False, f"证据文件不存在: {evidence_path}")
        return fail_return()
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        add("isolation_evidence_present", False, f"证据不可解析: {exc}")
        return fail_return()
    add("isolation_evidence_present", evidence.get("schema") == ISO_SCHEMA,
        f"schema={evidence.get('schema')}")
    if evidence.get("schema") != ISO_SCHEMA:
        return fail_return()

    if evidence.get("released_at"):
        add("window_active", False, f"窗口已于 {evidence['released_at']} 关闭；"
                                    "运行前需重新开窗探测")
        return fail_return()
    add("window_active", True, "证据未记录 release")

    now = now or _utcnow()
    try:
        valid_until = datetime.fromisoformat(evidence["valid_until"])
        created = datetime.fromisoformat(evidence["created_at"])
        add("evidence_fresh", now <= valid_until and now >= created,
            f"created_at={evidence['created_at']} valid_until={evidence['valid_until']}")
    except (KeyError, ValueError):
        add("evidence_fresh", False, "证据缺少或无法解析时间字段")

    identity = current_identity()
    same = (identity["user"] == evidence.get("identity", {}).get("user")
            and identity["host"] == evidence.get("identity", {}).get("host"))
    add("identity_match", same,
        f"当前 {identity['user']}@{identity['host']} vs 证据 "
        f"{evidence.get('identity', {}).get('user')}@{evidence.get('identity', {}).get('host')}")

    add("probe_version_match", evidence.get("probe_version") == PROBE_VERSION,
        f"当前={PROBE_VERSION} 证据={evidence.get('probe_version')}")

    expected = expected or {}
    if expected.get("execution_binding"):
        add("binding_code_and_protocol", evidence.get("execution_binding") == expected["execution_binding"],
            "代码 commit、方法字节契约和协议版本绑定必须相同")
        try:
            recovery = Path(evidence["window"]["recovery_log"])
            opened = json.loads(recovery.read_text(encoding="utf-8"))
            add("binding_window_record", _sha_file(recovery) == evidence["raw_hashes"].get("recovery-log.json")
                and opened.get("execution_binding") == expected["execution_binding"]
                and opened.get("package_hashes") == evidence.get("package_hashes"),
                "绑定必须与开窗原始记录及其哈希一致")
        except (OSError, ValueError, KeyError) as exc:
            add("binding_window_record", False, f"开窗绑定记录不可核对：{type(exc).__name__}")
        try:
            from mentis.eval_readiness import workspace_hashes

            hashes = workspace_hashes(Path(evidence["prepared_dir"]) / evidence["target_arm"])
            add("package_hashes_match", bool(evidence.get("package_hashes")) and hashes == evidence["package_hashes"],
                "整个冻结运行输入包（含方法提示词、素材、公开资料和观察）须与开窗前一致")
        except (OSError, ValueError, KeyError) as exc:
            add("package_hashes_match", False, f"输入包核对失败：{type(exc).__name__}")
    for key in ("prepared_dir", "target_arm", "workspace", "suite_sha256"):
        if expected.get(key):
            add(f"binding_{key}", str(evidence.get(key)) == str(expected[key]),
                f"期望={expected[key]} 证据={evidence.get(key)}")

    add("probe_results_clean",
        not evidence.get("mentis_env_probe", {}).get("problems")
        and not evidence.get("codex_env_probe", {}).get("problems"),
        f"mentis 问题={evidence.get('mentis_env_probe', {}).get('problems') or '无'}；"
        f"codex 问题={evidence.get('codex_env_probe', {}).get('problems') or '无'}")

    expected_input_sha = evidence.get("input_hashes", {}).get("input.json", "")
    try:
        mentis_raw, codex_raw = _load_raw_probes(evidence)
        add("dual_end_probes_present", True, "双端原始探针结果均存在且哈希一致")
        expected_entries = evidence.get("expected_entries", [])
        mentis_problems = judge_probe_entries(mentis_raw, expected_entries,
                                              expected_input_sha)
        codex_problems = judge_probe_entries(codex_raw, expected_entries,
                                             expected_input_sha)
        add("dual_end_results_reverified", not mentis_problems and not codex_problems,
            "双端原始记录重验通过"
            if not mentis_problems and not codex_problems
            else "mentis: " + "；".join(mentis_problems[:2])
            + " codex: " + "；".join(codex_problems[:2]))
    except (IsolationError, json.JSONDecodeError) as exc:
        add("dual_end_probes_present", False, str(exc))

    if codex_version is None:
        from shutil import which
        exe = which("codex") or "codex"
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=15)
            codex_version = proc.stdout.strip() if proc.returncode == 0 else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            codex_version = "unavailable"
    expected_codex = evidence.get("executor", {}).get("cli_version", "")
    add("codex_version_match", bool(codex_version) and codex_version == expected_codex,
        f"当前={codex_version or 'unavailable'} 证据={expected_codex}")

    # 输入哈希绑定：目标组 input.json 当前字节必须与证据一致
    input_sha_recorded = evidence.get("input_hashes", {}).get("input.json", "")
    arm_input = (Path(evidence.get("prepared_dir", "")) / evidence.get("target_arm", "")
                 / "input.json")
    if input_sha_recorded and arm_input.is_file():
        current = _sha_file(arm_input)
        add("input_hash_match", current == input_sha_recorded,
            f"input.json 当前哈希与证据{'一致' if current == input_sha_recorded else '不一致'}")
    elif input_sha_recorded:
        add("input_hash_match", False, f"目标组 input.json 不可读或不存在: {arm_input}")
    else:
        add("input_hash_match", False, "证据缺少 input.json 哈希绑定")

    targets = evidence.get("window", {}).get("verification_targets", [])
    if not targets:
        add("live_denial_current", False, "证据没有可读性验证点，无法确认窗口当前开启")
        return fail_return()
    live = []
    for target in targets:
        try:
            Path(target["probe"]).read_bytes()
            live.append({"probe": target["probe"], "denied": False, "error": "读取成功"})
        except PermissionError:
            live.append({"probe": target["probe"], "denied": True, "error": "PermissionError"})
        except OSError as exc:
            live.append({"probe": target["probe"], "denied": False,
                         "error": f"{type(exc).__name__}: {exc}"})
    live_ok = all(row["denied"] for row in live)
    add("live_denial_current", live_ok,
        "；".join(f"{Path(r['probe']).name}: {'拒读' if r['denied'] else r['error']}"
                  for r in live[:6]) + ("…" if len(live) > 6 else ""))

    passed = all(check["passed"] for check in checks)
    return {"runtime_answer_isolation": {"passed": passed, "checks": checks,
                                         "evidence": str(evidence_path),
                                         "window_id": evidence.get("window_id", "")}}


def sweep_stale_windows() -> dict:
    """按窗口锁逐个事务回滚遗留窗口（进程崩溃后的恢复路径）。

    逐项报告：恢复成功删除锁；恢复失败保留锁并给出原因，绝不谎报已恢复。
    """
    WINDOW_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for lock in sorted(WINDOW_LOCK_DIR.glob("window-*.json")):
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            results.append({"lock": lock.name, "released": False,
                            "detail": f"锁不可解析: {exc}"})
            continue
        window_dir = payload.get("window_dir", "")
        log_path = Path(window_dir) / "window.json" if window_dir else None
        if log_path is None or not log_path.is_file():
            # 旧格式锁（无 window_dir）：按锁内 protected 清单做 /remove:d 兜底，
            # 全部恢复可读才删锁；否则保留并如实报告。
            protected = payload.get("protected", [])
            if not protected:
                results.append({"lock": lock.name, "released": False,
                                "detail": f"恢复日志缺失: {window_dir}"})
                continue
            leftovers = []
            for p in protected:
                if not Path(p).exists():
                    continue  # 路径已随临时目录清理消失，无需恢复
                try:
                    _icacls([Path(p), *_remove_switches(_deny_accounts())])
                except IsolationError as exc:
                    leftovers.append(f"{p}: {exc}")
                    continue
                if not _readable(Path(p)):
                    leftovers.append(f"{p}: remove 后仍不可读")
            if leftovers:
                results.append({"lock": lock.name, "released": False,
                                "detail": "旧格式锁兜底后仍有残留: " + "；".join(leftovers[:3])})
                continue
            lock.unlink(missing_ok=True)
            results.append({"lock": lock.name, "released": True,
                            "detail": "旧格式锁：按 protected 清单 /remove:d 兜底恢复"})
            continue
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
            log.setdefault("window_dir", window_dir)
            outcome = release_window(log)
            results.append({"lock": lock.name, "released": outcome["verified"],
                            "restored": len(outcome["restored"]),
                            "failed": outcome["failed"]})
        except (OSError, json.JSONDecodeError, IsolationError) as exc:
            results.append({"lock": lock.name, "released": False, "detail": str(exc)[:200]})
    return {"swept": len(results), "results": results}


def eval_isolate_cli(args) -> dict:
    """eval-isolate 三个动作的 CLI 入口；返回可直接 JSON 打印的摘要。"""
    prepared = Path(args.prepared).resolve()
    isolation_dir = prepared / "isolation"
    suite_path = Path(args.suite)
    if args.action == "probe":
        adapter = None
        if not args.no_codex:
            from mentis.executors.codex_cli import CodexCliAdapter
            adapter = CodexCliAdapter()
        outcome = run_probe_flow(suite_path, prepared, args.arm, isolation_dir,
                                 adapter=adapter, run_spec=getattr(args, "run_spec", None))
        return {
            "ok": outcome.get("ok", False),
            "single_end_debug": outcome.get("debug", False),
            "problems": outcome.get("problems", []),
            "evidence": outcome.get("evidence_path") or "",
            "probe_dir": outcome.get("probe_dir", ""),
            "window_kept": outcome.get("window_kept", False),
            "alive_processes": outcome.get("alive_processes", []),
            "window_open": bool(outcome.get("evidence")),
            "suite_sha256": outcome.get("audit_suite_sha256", ""),
            "codex_status": outcome.get("codex_status", ""),
            "probe_version": outcome.get("probe_version", ""),
            "next": "ok=true 时窗口保持开启；运行前执行 eval-check --isolation-evidence 做准入，"
                    "实验结束后 eval-isolate release 事务恢复原权限。",
        }
    if args.action == "release":
        logs = sorted(isolation_dir.glob("window-*/window.json"))
        if not logs:
            # 该准备目录没有恢复日志：按全局窗口锁做崩溃遗留清扫
            return sweep_stale_windows()
        # 优先释放当前证据所指的窗口；否则取 created_at 最新的一个
        evidence_file = isolation_dir / "isolation-evidence.json"
        evidence_window_id = ""
        if evidence_file.is_file():
            try:
                evidence_window_id = json.loads(
                    evidence_file.read_text(encoding="utf-8")).get("window_id", "")
            except (OSError, json.JSONDecodeError):
                pass
        candidates = [json.loads(p.read_text(encoding="utf-8")) for p in logs]
        log = next((c for c in candidates if c.get("window_id") == evidence_window_id), None)             or max(candidates, key=lambda c: c.get("created_at", ""))
        # 旧版日志缺 window_dir：从全局窗口锁补齐
        if not log.get("window_dir"):
            lock = WINDOW_LOCK_DIR / f"window-{log['window_id']}.json"
            if lock.is_file():
                try:
                    log["window_dir"] = json.loads(
                        lock.read_text(encoding="utf-8")).get("window_dir", "")
                except (OSError, json.JSONDecodeError):
                    pass
        alive = _codex_processes_since(log["created_at"], scope=str(isolation_dir))
        if alive is None or alive:
            return {"ok": False, "window_id": log["window_id"], "window_kept": True,
                    "alive_processes": alive or [], "next": "未确认执行器停止；不恢复答案读取权限"}
        outcome = release_window(log)
        evidence_file = isolation_dir / "isolation-evidence.json"
        if evidence_file.is_file():
            evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
            if evidence.get("window_id") == log["window_id"]:
                evidence["released_at"] = _iso(_utcnow())
                evidence["release_restored"] = len(outcome["restored"])
                evidence["release_failed"] = outcome["failed"]
                evidence_file.write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
        return {"ok": not outcome["failed"], "window_id": log["window_id"],
                "restored": len(outcome["restored"]),
                "failed": outcome["failed"],
                "all_verified": outcome["verified"]}
    if args.action == "sweep":
        return sweep_stale_windows()
    # check
    return check_admission(isolation_dir / "isolation-evidence.json",
                           suite_path=suite_path)


def eval_check_with_isolation(suite_path: Path, evidence_path: Path,
                              expected: dict | None = None,
                              now: datetime | None = None,
                              codex_version: str | None = None) -> dict:
    """eval-check 的隔离感知封装：有证据时接入真实准入，无证据保持未就绪。

    - 窗口开启期间套件目录本身受保护（防身份推断），完整冻结审计无法执行；
      此时冻结核对引用 apply 时的实测，并在窗口关闭后用 suite_sha256 闭环。
    - 无证据、证据过期、身份/版本不一致或当前窗口未开启（live 检查可读）
      一律 runtime_answer_isolation.passed=false——清单声明和离线测试不算数。
    """
    suite_path, evidence_path = Path(suite_path), Path(evidence_path)
    evidence = {}
    if evidence_path.is_file():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence = {}
    window_active = (evidence.get("schema") == ISO_SCHEMA
                     and not evidence.get("released_at"))
    suite_accessible = _readable(suite_path)

    if window_active and not suite_accessible:
        admission = check_admission(evidence_path, expected=expected, now=now,
                                    codex_version=codex_version)
        passed = admission["runtime_answer_isolation"]["passed"]
        applied = evidence.get("frozen_input_checks", {})
        return {
            "status": "prepared_not_ready",
            "suite_id": "", "suite_sha256": evidence.get("suite_sha256", ""),
            "isolation_window": {
                "active": True,
                "note": "套件目录在隔离窗口内不可读（防身份推断）；"
                        "冻结输入核对引用窗口开启（apply）时的实测结果",
                "applied_checks": applied,
                "window_id": evidence.get("window_id", ""),
                "probe_dirs": evidence.get("probe_dirs", []),
            },
            "checks": [admission["runtime_answer_isolation"]],
            "exploratory_blocking_reasons": [] if passed else
            ["隔离准入未通过：" + "；".join(
                f"{c['name']}({c['detail']})"
                for c in admission["runtime_answer_isolation"]["checks"] if not c["passed"])],
            "formal_claim_limits": ["正式主指标与数值阈值尚未冻结；隔离准入不构成任何效果结论。"],
            "scope": "隔离窗口开启期间的准入视图；窗口关闭后请再执行 eval-check 做完整冻结审计闭环。",
        }

    from mentis.evaluation import audit_suite
    result = audit_suite(suite_path)
    if not evidence_path.is_file():
        return result
    admission = check_admission(evidence_path, expected=expected, now=now,
                                codex_version=codex_version)
    result["isolation_admission"] = admission["runtime_answer_isolation"]
    iso = next((c for c in result["checks"] if c["name"] == "runtime_answer_isolation"), None)
    if iso is not None:
        iso["passed"] = bool(admission["runtime_answer_isolation"]["passed"])
        if not iso["passed"]:
            failed = [f"{c['name']}: {c['detail']}"
                      for c in admission["runtime_answer_isolation"]["checks"] if not c["passed"]]
            iso["note"] = "；".join(failed) or "隔离准入未通过"
    recorded_sha = evidence.get("suite_sha256")
    fresh_sha = result.get("suite_sha256")
    if recorded_sha and fresh_sha and recorded_sha != fresh_sha:
        result["blocking_reasons"] = list(result.get("blocking_reasons", [])) + [
            "隔离证据的 suite_sha256 与当前磁盘上的套件不一致——证据所指冻结材料已被改动"]
    return result
