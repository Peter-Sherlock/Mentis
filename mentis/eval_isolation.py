"""E1 运行端答案读隔离：Windows deny ACL 隔离窗口 + 双端真实探测 + 准入校验。

诚实边界：
- 机制是操作系统级 deny ACE（icacls），对同用户子进程（受测 Mentis 环境）和
  受测 Codex 子进程一并生效；junction/绝对路径/父目录遍历由真实探测验证。
- deny 不防对抗性绕过：同用户属主始终保留 WRITE_DAC，可自行解除 deny
  （这是本方案与"独立运行身份"的差距，作为限制如实记录）。
- 探测通过只证明"窗口开启时受保护文件读不到"；不证明提示词服从、
  不证明模型不试图读取，也不构成任何效果结论。
- 离线夹具只证明本模块逻辑；准入判定永远要求真实探测证据 + 当前
  活动窗口（受保护路径此刻实际读不到）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

# 探测证据的保鲜期：过期后准入必须重新探测，不能拿旧窗口的证据开新运行。
DEFAULT_TTL_S = 12 * 3600
# 本机 Codex CLI Windows 沙箱使用的本地账户/组；存在则一并 deny（不存在则跳过并记录）。
# 仅 deny CodexSandboxUsers 不够：其成员身份（Offline/Online）可能不在令牌中携带父组 SID。
EXTRA_DENY_ACCOUNTS = ("CodexSandboxUsers", "CodexSandboxOffline", "CodexSandboxOnline")
ISO_SCHEMA = "mentis-eval-isolation/1"


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
    """所有账户的 deny 写进同一次 icacls 调用：deny 生效后再开第二次
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


def _canary_check(parent: Path, accounts: list[str]) -> None:
    """开窗预检：在目标同父目录（同一 ACL 世系）上验证 deny 可生效且可解除。

    Python 3.14 tempfile 根带 OWNER RIGHTS ACE，其下 deny 生效后会同时
    压掉属主隐式 WRITE_DAC——同用户无法解除。金丝雀在任何真实路径
    被改动之前暴露这种环境，避免把仓库或评测目录锁死。
    """
    canary = parent / f"_iso-canary-{uuid4().hex[:8]}"
    stuck = False
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
            stuck = True
            raise IsolationError(
                f"金丝雀 deny 无法解除（该目录 ACL 世系会在 deny 后锁死属主，"
                f"典型如 Python tempfile 的 OWNER RIGHTS），已放弃开窗；"
                f"可能残留金丝雀目录需手工清理: {canary}")
    finally:
        shutil.rmtree(canary, ignore_errors=True)


def protected_manifest(suite_path: Path, prepared_dir: Path, target_arm: str) -> list[Path]:
    """从 prepare-report 推导受保护清单：套件目录 + 身份映射 + 除目标组外的全部输入包。

    必须在窗口开启前调用（需要读取 prepare-report.json）。
    目标组目录本身不保护（运行组需要读写它）。
    """
    suite_path, prepared_dir = Path(suite_path), Path(prepared_dir)
    report = json.loads((prepared_dir / "prepare-report.json").read_text(encoding="utf-8"))
    arms = {arm["path"] for row in report["packages"] for arm in row["arms"]}
    if target_arm not in arms:
        raise IsolationError(f"target_arm 不在 prepare-report 中: {target_arm}")
    protected = [suite_path.parent.resolve(), (prepared_dir / "prepare-report.json").resolve()]
    for row in report["packages"]:
        for arm in row["arms"]:
            if arm["path"] == target_arm:
                continue
            protected.append((prepared_dir / arm["path"]).resolve())
    return protected


_PREFERRED_PROBES = ("reviewer.json", "suite.json", "prepare-report.json",
                     "input.json", "prompt.md")


def verification_targets(protected: list[Path]) -> list[dict]:
    """deny 之前记录每个受保护路径的"可读性验证点"（文件本身；目录取代表性文件）。

    deny 之后无法再列举目录内容，验证点必须在窗口开启前固定下来；
    它们同时是双端探测的反向目标。目录代表文件优先取敏感文件
    （reviewer.json/suite.json/input.json…），使证据直接针对答案载体。
    """
    targets = []
    for path in protected:
        path = Path(path)
        if path.is_file():
            targets.append({"protected": str(path), "probe": str(path),
                            "sha256_at_apply": _sha_file(path), "kind": "file"})
        elif path.is_dir():
            children = sorted(p for p in path.rglob("*") if p.is_file())
            child = next((p for p in children
                          if p.name.casefold() in _PREFERRED_PROBES), None)
            child = child or (children[0] if children else None)
            if child is None:
                raise IsolationError(f"受保护目录没有可验证的文件: {path}")
            targets.append({"protected": str(path), "probe": str(child),
                            "sha256_at_apply": _sha_file(child), "kind": "dir"})
        else:
            raise IsolationError(f"受保护路径不存在: {path}")
    return targets


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


def _release_best_effort(protected: list[Path], accounts: list[str]) -> list[str]:
    """失败路径的兜底解除；不做可读性断言，只报告每条 icacls 结果。"""
    notes = []
    for path in protected:
        proc = subprocess.run(["icacls", str(path), *_remove_switches(accounts)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
        notes.append(f"{path}: rc={proc.returncode}")
    return notes


def apply_window(protected: list[Path]) -> dict:
    """开启隔离窗口：金丝雀预检 → 单次调用 deny → 立即实测读不到。

    任一受保护路径仍可读即失败，并尽力解除 deny，不留下半开窗口。
    """
    protected = [Path(p).resolve() for p in protected]
    for path in protected:
        if not _readable(path):
            raise IsolationError(f"路径已不可读（窗口可能已开启，先 release 再操作）: {path}")
    targets = verification_targets(protected)
    accounts = _deny_accounts()
    for parent in sorted({p.parent for p in protected}):
        _canary_check(parent, accounts)
    icacls_outputs = {}
    for path in protected:
        try:
            proc = _icacls([path, *_deny_switches(accounts, path.is_dir())])
        except IsolationError:
            # 半开状态兜底：已 deny 的路径尽力解除，不让仓库留在锁死状态
            _release_best_effort([p for p in protected[:protected.index(path)]], accounts)
            raise
        icacls_outputs[str(path)] = proc.stdout.strip()[-400:]
    live = _expect_denied(targets)
    failures = [row for row in live if not row["denied"]]
    if failures:
        notes = _release_best_effort(protected, accounts)
        raise IsolationError("deny 后仍有受保护路径可读，隔离窗口无效（已尽力解除）: "
                             + json.dumps(failures, ensure_ascii=False)
                             + "；解除结果: " + "；".join(notes))
    return {"accounts": accounts, "protected": [str(p) for p in protected],
            "verification_targets": targets, "icacls_outputs": icacls_outputs,
            "live_denial_check": live}


def release_window(protected: list[Path], accounts: list[str]) -> dict:
    """关闭隔离窗口（单次调用移除全部账户 deny）并实测恢复可读。"""
    protected = [Path(p).resolve() for p in protected]
    removed = {}
    for path in protected:
        proc = _icacls([path, *_remove_switches(accounts)])
        removed[str(path)] = proc.stdout.strip()[-200:]
    restored = [{"probe": t["probe"], "readable": _readable(Path(t["probe"]))}
                for t in verification_targets(protected)]
    if not all(row["readable"] for row in restored):
        raise IsolationError("release 后仍有受保护路径拒读，窗口未干净关闭，需人工核对 ACL: "
                             + json.dumps([r for r in restored if not r["readable"]],
                                          ensure_ascii=False))
    return {"removed": removed, "restored_check": restored}


# ---------------------------------------------------------------- 探测

_MENTIS_PROBE_SCRIPT = r"""
import hashlib, json, os, subprocess, sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
targets = [Path(p) for p in sys.argv[2:]]

def attempt(form, path):
    try:
        raw = path.read_bytes()
    except PermissionError:
        return {"form": form, "path": str(path), "result": "denied", "error": "PermissionError"}
    except OSError as exc:
        return {"form": form, "path": str(path), "result": "other_error",
                "error": f"{type(exc).__name__}: {exc}"}
    # 意外读到内容时只记录指纹，绝不让内容进入证据。
    return {"form": form, "path": str(path), "result": "READ",
            "sha256_12": hashlib.sha256(raw).hexdigest()[:12], "bytes": len(raw)}

results = []
positive = {}
try:
    for name in ["input.json", "prompt.md"]:
        raw = (workspace / name).read_bytes()
        positive[name] = {"readable": True, "bytes": len(raw)}
except OSError as exc:
    positive["error"] = f"{type(exc).__name__}: {exc}"
write_path = workspace / ".isolation-probe-write.txt"
try:
    write_path.write_text("probe-write-ok", encoding="utf-8")
    positive["workspace_write"] = write_path.read_text(encoding="utf-8")
except OSError as exc:
    positive["workspace_write"] = f"FAILED {type(exc).__name__}: {exc}"

for target in targets:
    results.append(attempt("absolute", target))
    # 父目录遍历：从工作区出发的 .. 相对形式（路径含 .. 也由目标 ACL 判定）。
    try:
        rel = os.path.relpath(target, workspace)
    except ValueError:
        rel = None
    if rel is not None:
        results.append(attempt("parent_traversal", workspace / rel))

link = workspace / ".isolation-junction"
for target in targets:
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target.parent)],
                       capture_output=True, text=True, timeout=20)
        # junction 指向被拒目标时 stat 会穿透失败，必须用 lexists 判断链接本体
        if os.path.lexists(str(link)):
            results.append(attempt("junction", link / target.name))
        else:
            results.append({"form": "junction", "path": str(target.parent), "result": "denied",
                            "error": "junction 创建失败（更严，不视为漏洞）"})
    finally:
        if os.path.lexists(str(link)):
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, timeout=20)

print(json.dumps({"positive": positive, "negative": results,
                  "cwd": str(workspace), "user_env": os.environ.get("USERNAME", "")},
                 ensure_ascii=False))
"""


def probe_mentis_env(workspace: Path, probe_files: list[Path], out_dir: Path,
                     python_exe: str = sys.executable) -> dict:
    """在实际 Mentis 运行环境（同一解释器/用户/cwd/env）探测正反向权限。

    原始 stdout 落盘作为证据；意外读到内容时结果只含哈希前缀与长度。
    """
    workspace = Path(workspace).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [python_exe, "-I", "-c", _MENTIS_PROBE_SCRIPT, str(workspace),
         *[str(p) for p in probe_files]],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    (out_dir / "mentis-probe-stdout.txt").write_text(
        f"### exit={proc.returncode}\n### cwd={workspace}\n"
        f"### cmd={python_exe} -I -c <probe> {workspace}\n"
        + proc.stdout + ("\n### stderr:\n" + proc.stderr if proc.stderr else ""),
        encoding="utf-8")
    if proc.returncode != 0:
        raise IsolationError(f"Mentis 环境探测脚本失败（退出码 {proc.returncode}），"
                             "原始输出见 mentis-probe-stdout.txt")
    lines = [line for line in proc.stdout.strip().splitlines() if line.startswith("{")]
    if not lines:
        raise IsolationError("Mentis 环境探测未产出 JSON 结果")
    payload = json.loads(lines[-1])
    (out_dir / "mentis-probe-result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def judge_mentis_probe(payload: dict) -> list[str]:
    """探测结果的保守判定：任何意外可读都算隔离不成立。"""
    problems = []
    positive = payload.get("positive", {})
    if not positive.get("input.json", {}).get("readable"):
        problems.append("正向证据缺失：工作区 input.json 不可读")
    if positive.get("workspace_write") != "probe-write-ok":
        problems.append("正向证据缺失：工作区写入失败")
    for row in payload.get("negative", []):
        if row.get("result") == "READ":
            problems.append(f"反向证据失败：{row.get('form')} 形式读到了受保护内容 "
                            f"({row.get('path')})")
        elif row.get("result") == "other_error":
            problems.append(f"探测出现非预期错误（保守记为未验证）: {row.get('error')}")
    return problems


def _codex_probe_targets(targets: list[dict], cap: int = 4) -> list[Path]:
    """Codex 端探测用代表集：按验证点文件名去重（reviewer.json / prepare-report.json /
    input.json 等各取其一），上限 cap。Mentis 端探测覆盖全部验证点。"""
    seen, picked = set(), []
    for target in targets:
        name = Path(target["probe"]).name
        if name in seen:
            continue
        seen.add(name)
        picked.append(Path(target["probe"]))
        if len(picked) >= cap:
            break
    return picked


def probe_codex_env(adapter, workspace: Path, probe_files: list[Path],
                    out_dir: Path, timeout_s: int = 540) -> dict:
    """在真实 Codex 执行环境（其沙箱配置、其 shell）探测正反向权限。

    判定只依据 Codex 在工作区写下的 denied-*/read-* 标记文件与正向写入；
    自由文本回复仅存档供人工核对，不作为通过依据。
    """
    from mentis.executors.codex_cli import CodexTaskSpec

    workspace = Path(workspace).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, timeout=30)
    probe_dir = workspace / "probe-codex"
    probe_dir.mkdir(exist_ok=True)
    path_block = "\n".join(f"  文件{i} = {path}" for i, path in enumerate(probe_files, 1))
    lines = [
        "立即执行以下权限探测任务（评测基础设施自检，与任何案例内容无关），不要等待确认。",
        "禁止：修改 input.json；删除或清理任何文件；把受保护文件的内容写入文件或回复。",
        "每次读取尝试都用包裹形式执行，使命令本身总以退出码 0 结束，读取结果只用输出表达：",
        '  cmd /c "type \\"<目标路径>\\" >nul 2>&1 && (echo READ) || (echo DENIED)"',
        "待探测文件清单：",
        path_block,
        "步骤：",
        f"1. 依次对文件1—文件{len(probe_files)}各做三次读取尝试，全部用上述包裹形式执行："
        "a) 清单中的绝对路径直接读；b) 从当前工作区出发的相对路径（含 .. 前缀）；"
        "c) mklink /J 建一个指向该文件所在目录的目录连接，再经连接读取该文件。",
        "   每个文件的三次尝试完成后再落盘标记：三次输出都是 DENIED → 创建 "
        "probe-codex/denied-<编号>.txt（内容恰好为 DENIED）；任何一次输出 READ → 创建 "
        "probe-codex/read-<编号>.txt（内容恰好为 READ）。",
        f"{len(probe_files) + 1}. 读取工作区 input.json 的字节数（应当能读到）。",
        f"{len(probe_files) + 2}. 创建 probe-codex/positive-write.txt，内容恰好为 OK。",
        f"{len(probe_files) + 3}. 在回复末尾输出一行总结：文件1—文件{len(probe_files)}各自的"
        " DENIED/READ 结果，以及 input.json 的字节数。",
        "全部完成后停止；保留 probe-codex 目录下的全部文件（它们是核查证据）。",
    ]
    prompt = "\n".join(lines)
    (out_dir / "codex-probe-prompt.md").write_text(prompt, encoding="utf-8")
    spec = CodexTaskSpec(workspace=workspace, prompt=prompt, brief_version=1,
                         sandbox="workspace-write", timeout_s=timeout_s,
                         extra_config=('approval_policy="never"',))
    result = adapter.start_task(spec)
    (out_dir / "codex-probe-response.txt").write_text(
        f"### status={result.status.value}\n### cli_version={result.cli_version}\n"
        f"### error={result.error}\n### usage={json.dumps(result.usage, ensure_ascii=False)}\n"
        + result.final_response, encoding="utf-8")
    (out_dir / "codex-probe-events.json").write_text(json.dumps(
        [{"seq": e.sequence, "kind": e.kind, "item_type": e.item_type,
          "item_status": e.item_status} for e in result.events],
        ensure_ascii=False, indent=2), encoding="utf-8")
    if result.status.name != "COMPLETED":
        raise IsolationError(
            f"Codex 探测任务未正常完成（status={result.status.value}，error={result.error[:120]}）；"
            "隔离不能判成立，原始输出已保存")
    payload = {
        "status": result.status.value, "cli_version": result.cli_version,
        "probe_files": [str(p) for p in probe_files],
        "expected_denied": [f"denied-{i}.txt" for i in range(1, len(probe_files) + 1)],
        "denied_files": sorted(p.name for p in probe_dir.glob("denied-*.txt")),
        "read_files": sorted(p.name for p in probe_dir.glob("read-*.txt")),
        "positive_write": (probe_dir / "positive-write.txt").read_text(
            encoding="utf-8").strip() if (probe_dir / "positive-write.txt").exists() else "",
        "input_json_bytes": (workspace / "input.json").stat().st_size
        if (workspace / "input.json").exists() else 0,
        "usage": dict(result.usage),
    }
    (out_dir / "codex-probe-result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def judge_codex_probe(payload: dict) -> list[str]:
    problems = []
    if sorted(payload.get("denied_files", [])) != sorted(payload.get("expected_denied", [])):
        problems.append("反向证据不完整：denied-N.txt 标记与受保护路径清单不一致")
    if payload.get("read_files"):
        problems.append(f"反向证据失败：Codex 报告读到了受保护内容 {payload['read_files']}")
    if payload.get("positive_write") != "OK":
        problems.append("正向证据缺失：Codex 工作区写入未确认")
    if not payload.get("input_json_bytes"):
        problems.append("正向证据缺失：Codex 未能读取工作区 input.json")
    return problems


# ---------------------------------------------------------------- 准入

def write_evidence(out_path: Path, suite_path: Path, audit: dict, window: dict,
                   mentis_payload: dict, mentis_problems: list[str],
                   codex_payload: dict, codex_problems: list[str],
                   probe_dirs: list[str], ttl_s: int = DEFAULT_TTL_S) -> dict:
    """探测全部通过后写入准入证据。audit 必须在窗口开启前完成（套件目录届时可读）。"""
    frozen = {check["name"]: check for check in audit["checks"]}
    if audit.get("status") == "invalid" or not frozen["frozen_source_hashes"]["passed"]:
        raise IsolationError(f"冻结源核对未通过，拒绝写隔离证据: {audit.get('blocking_reasons')}")
    evidence = {
        "schema": ISO_SCHEMA,
        "created_at": _iso(_utcnow()),
        "valid_until": _iso(_utcnow() + timedelta(seconds=ttl_s)),
        "identity": current_identity(),
        "suite_path": str(Path(suite_path).resolve()),
        "suite_id": audit.get("suite_id", ""),
        "suite_sha256": audit["suite_sha256"],
        "frozen_input_checks": {name: frozen[name] for name in
                                ("schema_and_input_whitelist", "source_paths_and_targets",
                                 "frozen_source_hashes")},
        "window": window,
        "mentis_env_probe": {key: mentis_payload.get(key)
                             for key in ("positive", "negative", "cwd")},
        "mentis_probe_problems": mentis_problems,
        "codex_env_probe": {key: codex_payload.get(key)
                            for key in ("status", "cli_version", "probe_files",
                                        "denied_files", "read_files", "positive_write",
                                        "usage")},
        "codex_probe_problems": codex_problems,
        "probe_dirs": probe_dirs,
        "released_at": "",
        "mechanism_note": "Windows deny ACL（icacls /deny 当前用户与 Codex 沙箱组）；"
                          "绝对路径/父目录遍历/junction 均由双端真实探测覆盖；"
                          "属主保留 WRITE_DAC 可解除 deny，不防对抗性绕过。",
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return evidence


def check_admission(evidence_path: Path, suite_path: Path = Path(""),
                    protected: list[Path] | None = None, expected_suite_sha: str = "",
                    now: datetime | None = None, ttl_s: int = DEFAULT_TTL_S,
                    codex_version: str | None = None) -> dict:
    """运行前准入：核对真实证据 + 当前活动窗口；缺一即保持未就绪。

    判定性地依赖"此刻受保护路径读不到"（live denial check）——证据、
    清单声明或离线测试都不能替代它。窗口开启期间套件目录本身被保护，
    冻结核对引用 apply 时的实测；窗口关闭后用 suite_sha256 与当前
    audit_suite 结果闭环比对。
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
                                    "运行前需重新开启并探测")
        return fail_return()
    add("window_active", True, "证据未记录 release")

    now = now or _utcnow()
    try:
        valid_until = datetime.fromisoformat(evidence["valid_until"])
        created = datetime.fromisoformat(evidence["created_at"])
        fresh = now <= valid_until and now >= created
        add("evidence_fresh", fresh,
            f"created_at={evidence.get('created_at')} valid_until={evidence['valid_until']}")
    except (KeyError, ValueError):
        fresh = False
        add("evidence_fresh", False, "证据缺少或无法解析时间字段")

    identity = current_identity()
    same = (identity["user"] == evidence.get("identity", {}).get("user")
            and identity["host"] == evidence.get("identity", {}).get("host"))
    add("identity_match", same,
        f"当前 {identity['user']}@{identity['host']} vs 证据 "
        f"{evidence.get('identity', {}).get('user')}@{evidence.get('identity', {}).get('host')}")

    if codex_version is None:
        # 与适配器一致地解析可执行文件（npm 的 codex 是 .cmd shim，裸名会 FileNotFoundError）
        from shutil import which
        exe = which("codex") or "codex"
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=15)
            codex_version = proc.stdout.strip() if proc.returncode == 0 else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            codex_version = "unavailable"
    expected_codex = evidence.get("codex_env_probe", {}).get("cli_version", "")
    add("codex_version_match", bool(codex_version) and codex_version == expected_codex,
        f"当前={codex_version or 'unavailable'} 证据={expected_codex}")

    add("probe_results_clean",
        not evidence.get("mentis_probe_problems") and not evidence.get("codex_probe_problems"),
        f"mentis 问题={evidence.get('mentis_probe_problems') or '无'}；"
        f"codex 问题={evidence.get('codex_probe_problems') or '无'}")

    if expected_suite_sha:
        add("suite_sha_match", evidence.get("suite_sha256") == expected_suite_sha,
            f"期望={expected_suite_sha[:12]}… 证据={str(evidence.get('suite_sha256'))[:12]}…")
    else:
        add("suite_sha_match", True,
            f"证据记录={str(evidence.get('suite_sha256'))[:12]}…（apply 时实测冻结源）")

    targets = evidence.get("window", {}).get("verification_targets", [])
    if protected is not None:
        wanted = {str(Path(p).resolve()) for p in protected}
        covered = {t.get("protected") for t in targets}
        add("protected_manifest_covered", wanted <= covered,
            f"证据验证点覆盖 {len(wanted & covered)}/{len(wanted)} 个受保护路径")
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
                                         "evidence": str(evidence_path)}}


def run_probe_flow(suite_path: Path, prepared_dir: Path, target_arm: str,
                   isolation_dir: Path, adapter=None, python_exe: str = sys.executable,
                   codex_timeout_s: int = 540) -> dict:
    """完整探测流程：审计 → 开窗 → 双端真实探测 →（全部通过时）写证据。

    任何一步失败都会先关闭窗口再抛出，避免把仓库留在被锁状态；
    失败不写证据，准入自然保持未就绪。
    """
    suite_path, prepared_dir = Path(suite_path), Path(prepared_dir)
    isolation_dir = Path(isolation_dir)
    isolation_dir.mkdir(parents=True, exist_ok=True)
    from mentis.evaluation import audit_suite
    audit = audit_suite(suite_path)
    frozen = {check["name"]: check for check in audit["checks"]}
    if audit["status"] == "invalid" or not frozen["frozen_source_hashes"]["passed"]:
        raise IsolationError(f"冻结源核对未通过: {audit.get('blocking_reasons')}")
    protected = protected_manifest(suite_path, prepared_dir, target_arm)
    window = apply_window(protected)
    # 窗口记录先于探测落盘：探测中途失败时 release 仍有可靠依据。
    (isolation_dir / "window.json").write_text(
        json.dumps({"protected": window["protected"], "accounts": window["accounts"],
                    "verification_targets": window["verification_targets"],
                    "opened_at": _iso(_utcnow())}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    stamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    probe_dir = isolation_dir / f"probe-{stamp}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    arm_dir = prepared_dir / target_arm
    workspace = probe_dir / "workspace"
    shutil.copytree(arm_dir, workspace)
    evidence_written = False
    try:
        probe_files = [Path(t["probe"]) for t in window["verification_targets"]]
        mentis_payload = probe_mentis_env(workspace, probe_files, probe_dir,
                                          python_exe=python_exe)
        mentis_problems = judge_mentis_probe(mentis_payload)
        codex_payload = {"cli_version": "", "status": "skipped", "denied_files": [],
                         "read_files": [], "positive_write": "", "usage": {}}
        codex_problems = []
        if adapter is not None:
            codex_payload = probe_codex_env(adapter, workspace,
                                            _codex_probe_targets(window["verification_targets"]),
                                            probe_dir, timeout_s=codex_timeout_s)
            codex_problems = judge_codex_probe(codex_payload)
        problems = mentis_problems + codex_problems
        evidence = None
        if not problems:
            evidence = write_evidence(
                isolation_dir / "isolation-evidence.json", suite_path, audit, window,
                mentis_payload, mentis_problems, codex_payload, codex_problems,
                probe_dirs=[str(probe_dir)])
            evidence_written = evidence is not None
        return {"evidence": evidence, "problems": problems, "window": window,
                "probe_dir": str(probe_dir), "mentis_payload": mentis_payload,
                "codex_payload": codex_payload, "audit_suite_sha256": audit["suite_sha256"]}
    finally:
        if not evidence_written:
            # 探测失败或未写证据：不留锁死仓库的窗口。
            release_window(protected, window["accounts"])


def eval_check_with_isolation(suite_path: Path, evidence_path: Path,
                              expected_suite_sha: str = "",
                              now: datetime | None = None,
                              codex_version: str | None = None) -> dict:
    """eval-check 的隔离感知封装：有证据时接入真实准入，无证据保持未就绪。

    - 窗口开启期间套件目录本身受保护（防身份推断），完整冻结审计无法执行；
      此时冻结核对引用 apply 时的实测，并在窗口关闭后用 sha256 闭环。
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
    window_active = bool(evidence.get("schema")) and not evidence.get("released_at")
    suite_accessible = _readable(suite_path)

    if window_active and not suite_accessible:
        admission = check_admission(evidence_path, now=now, codex_version=codex_version)
        passed = admission["runtime_answer_isolation"]["passed"]
        applied = evidence.get("frozen_input_checks", {})
        return {
            "status": "prepared_not_ready",
            "suite_id": evidence.get("suite_id", ""),
            "suite_sha256": evidence.get("suite_sha256", ""),
            "isolation_window": {
                "active": True,
                "note": "套件目录在隔离窗口内不可读（防身份推断）；"
                        "冻结输入核对引用窗口开启（apply）时的实测结果",
                "applied_checks": applied,
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
    admission = check_admission(evidence_path, suite_path=suite_path,
                                expected_suite_sha=expected_suite_sha, now=now,
                                codex_version=codex_version)
    result["isolation_admission"] = admission["runtime_answer_isolation"]
    iso = next((c for c in result["checks"] if c["name"] == "runtime_answer_isolation"), None)
    if iso is not None:
        iso["passed"] = bool(admission["runtime_answer_isolation"]["passed"])
        if not iso["passed"]:
            failed = [f"{c['name']}: {c['detail']}"
                      for c in admission["runtime_answer_isolation"]["checks"] if not c["passed"]]
            iso["note"] = "；".join(failed) or "隔离准入未通过"
    # 窗口关闭后的闭环：证据所指的冻结套件与当前磁盘字节是否一致。
    recorded_sha = evidence.get("suite_sha256")
    fresh_sha = result.get("suite_sha256")
    if recorded_sha and fresh_sha and recorded_sha != fresh_sha:
        result["blocking_reasons"] = list(result.get("blocking_reasons", [])) + [
            "隔离证据的 suite_sha256 与当前磁盘上的套件不一致——证据所指冻结材料已被改动"]
    return result


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
                                 adapter=adapter)
        return {
            "ok": outcome["evidence"] is not None,
            "problems": outcome["problems"],
            "evidence": str(isolation_dir / "isolation-evidence.json")
            if outcome["evidence"] else "",
            "probe_dir": outcome["probe_dir"],
            "window_open": outcome["evidence"] is not None,
            "suite_sha256": outcome["audit_suite_sha256"],
            "codex_status": outcome["codex_payload"].get("status", ""),
            "next": "窗口保持开启；运行前执行 eval-check --isolation-evidence 做准入，"
                    "实验结束后 eval-isolate release 关闭窗口。",
        }
    if args.action == "release":
        window = {}
        window_file = isolation_dir / "window.json"
        if window_file.is_file():
            try:
                window = json.loads(window_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                window = {}
        if not window:
            evidence_file = isolation_dir / "isolation-evidence.json"
            if evidence_file.is_file():
                window = json.loads(evidence_file.read_text(encoding="utf-8")).get("window", {})
        if not window or "protected" not in window:
            raise IsolationError(
                f"找不到窗口记录（{window_file}），无法安全解除；"
                "请人工执行 icacls <路径> /remove:d <用户> 核对")
        outcome = release_window([Path(p) for p in window["protected"]],
                                 window["accounts"])
        evidence_file = isolation_dir / "isolation-evidence.json"
        if evidence_file.is_file():
            evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
            evidence["released_at"] = _iso(_utcnow())
            evidence_file.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        return {"ok": True, "released": True,
                "paths": len(window["protected"]), "accounts": window["accounts"],
                "restored_check": outcome["restored_check"]}
    # check
    return check_admission(isolation_dir / "isolation-evidence.json",
                           suite_path=suite_path)
