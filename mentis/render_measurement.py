"""P3.2 浏览器渲染测量：横向溢出数值与表单声明行为（无界面 Chrome，可重复）。

诚实边界：
- 测量在注入脚本的临时副本上执行；原文件不修改（两侧 sha256 均记录）。
- 表单提交测试由测量脚本强制 preventDefault 阻止导航；页面自身的
  submit 处理器是否执行、是否显示确认提示，只能记录"同步可见的 DOM 变化"，
  异步提示与真实网络请求不声称（网络意图仅扫描 action/method/fetch/XHR/beacon 声明）。
- 每个视口、每个环节失败单独记账；测量失败不伪造数据。
"""

import hashlib
import html as html_mod
import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

VIEWPORTS = {"mobile": (390, 844), "desktop": (1280, 800)}
_REPORT_BEGIN = "MENTIS_REPORT_BEGIN"
_REPORT_END = "MENTIS_REPORT_END"

# 注入到临时副本的测量脚本：布局完成后测量溢出与表单，再把报告写回 DOM
_MEASURE_JS = """
(function () {
  function run() {
    var report = { errors: [] };
    try {
      var vw = window.innerWidth, vh = window.innerHeight;
      var doc = document.documentElement;
      report.innerWidth = vw; report.innerHeight = vh;
      report.scrollWidth = doc.scrollWidth; report.scrollHeight = doc.scrollHeight;
      report.overflowPx = Math.max(0, doc.scrollWidth - vw);
      report.pageTitle = document.title;
      report.overflowing = [];
      var offenders = [];
      var all = document.body.querySelectorAll("*");
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > vw + 1) {
          offenders.push({
            tag: el.tagName.toLowerCase(),
            ident: el.id ? ("#" + el.id) : ((el.className && typeof el.className === "string" && el.className.trim()) ? ("." + el.className.trim().split(/\\s+/).join(".")) : ""),
            text: (el.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 40),
            left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width)
          });
        }
      }
      offenders.sort(function (a, b) { return b.right - a.right; });
      report.overflowing = offenders.slice(0, 12);
      // 容器内文字被裁剪（如 overflow:hidden + 定宽）：视觉"文字显示不全"的主要来源
      report.clippedText = [];
      var clipped = [];
      for (var i2 = 0; i2 < all.length; i2++) {
        var el3 = all[i2];
        if (el3.children.length > 0) { continue; }
        var ownText = (el3.textContent || "").trim();
        if (!ownText) { continue; }
        var cs = window.getComputedStyle(el3);
        var clipX = (cs.overflowX === "hidden" || cs.overflowX === "clip" || cs.overflow === "hidden" || cs.overflow === "clip");
        var ellipsis = cs.textOverflow === "ellipsis";
        if ((clipX || ellipsis) && el3.scrollWidth > el3.clientWidth + 2) {
          clipped.push({
            tag: el3.tagName.toLowerCase(),
            ident: el3.id ? ("#" + el3.id) : ((el3.className && typeof el3.className === "string" && el3.className.trim()) ? ("." + el3.className.trim().split(/\\s+/).join(".")) : ""),
            text: ownText.replace(/\\s+/g, " ").slice(0, 40),
            clientWidth: el3.clientWidth, scrollWidth: el3.scrollWidth
          });
        }
      }
      clipped.sort(function (a, b) { return b.scrollWidth - b.clientWidth - (a.scrollWidth - a.clientWidth); });
      report.clippedText = clipped.slice(0, 12);
      report.networkIntent = { actions: [], methods: [], hasFetch: false, hasXHR: false, hasBeacon: false };
      var scriptText = "";
      for (var s = 0; s < document.scripts.length; s++) {
        if (document.scripts[s].id === "mentis-measure-script") { continue; }
        scriptText += "\\n" + (document.scripts[s].textContent || "");
      }
      report.networkIntent.hasFetch = /fetch\\s*\\(/.test(scriptText);
      report.networkIntent.hasXHR = /XMLHttpRequest/.test(scriptText);
      report.networkIntent.hasBeacon = /sendBeacon/.test(scriptText);
      report.forms = [];
      for (var f = 0; f < document.forms.length; f++) {
        var form = document.forms[f];
        var info = { index: f, ident: form.id ? ("#" + form.id) : ("forms[" + f + "]"),
          action: form.getAttribute("action") || "", method: (form.getAttribute("method") || "get"),
          fields: 0, requiredFields: 0,
          emptyCheckValidity: null, emptySubmitFired: null,
          filledCheckValidity: null, filledSubmitFired: null,
          syncDomDelta: "", notes: [] };
        var els = form.querySelectorAll("input, textarea, select");
        info.fields = els.length;
        for (var k = 0; k < els.length; k++) { if (els[k].required || els[k].hasAttribute("required")) info.requiredFields++; }
        try { info.emptyCheckValidity = form.checkValidity(); } catch (e) { info.notes.push("emptyValidity:" + e.message); }
        var fired = false;
        var watcher = function (ev) { fired = true; ev.preventDefault(); };
        window.addEventListener("submit", watcher, true);
        try { form.requestSubmit(); } catch (e) { info.notes.push("requestSubmit:" + e.message); }
        info.emptySubmitFired = fired;
        try {
          var beforeText = document.body.textContent;
          var original = [];
          for (var k2 = 0; k2 < els.length; k2++) {
            var el2 = els[k2];
            if ("value" in el2 && el2.type !== "submit" && el2.type !== "button") {
              original.push([el2, el2.value]);
              if (el2.type === "email") el2.value = "test@example.com";
              else if (el2.type === "tel") el2.value = "13800000000";
              else if (el2.type === "number") el2.value = "1";
              else if (el2.type === "date") el2.value = "2026-11-14";
              else el2.value = "\\u6d4b\\u8bd5\\u503c";
            }
          }
          try { info.filledCheckValidity = form.checkValidity(); } catch (e2) { info.notes.push("filledValidity:" + e2.message); }
          fired = false;
          try { form.requestSubmit(); } catch (e3) { info.notes.push("filledRequestSubmit:" + e3.message); }
          info.filledSubmitFired = fired;
          var afterText = document.body.textContent;
          if (afterText !== beforeText) {
            var common = 0;
            while (common < beforeText.length && common < afterText.length && beforeText[common] === afterText[common]) common++;
            info.syncDomDelta = afterText.slice(common).trim().slice(0, 160);
          }
          for (var k3 = 0; k3 < original.length; k3++) { try { original[k3][0].value = original[k3][1]; } catch (e4) {} }
        } catch (e5) { info.notes.push("filledSubmit:" + e5.message); }
        window.removeEventListener("submit", watcher, true);
        report.forms.push(info);
      }
    } catch (e) { report.errors.push("measure:" + e.message); }
// 报告经 postMessage 回传给外层包装页（wrapper 把它写进 <pre> 供 dump-dom 读取）
    var payload = "%BEGIN%" + JSON.stringify(report) + "%END%";
    try { parent.postMessage(payload, "*"); }
    catch (e) { document.title = "MENTIS_POST_FAIL"; }
  }
  if (document.readyState === "complete") { setTimeout(run, 30); }
  else { window.addEventListener("load", function () { setTimeout(run, 30); }); }
})();
""".replace("%BEGIN%", _REPORT_BEGIN).replace("%END%", _REPORT_END)

# 包装页：iframe 以目标视口宽度布局（绕开 Windows 无界面窗口 500px 最小宽度钳制），
# 接收 iframe 内测量报告并写入 <pre>，供 --dump-dom 序列化读取
_WRAPPER_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<script>
window.addEventListener("message", function (ev) {
  var pre = document.getElementById("mentis-measure-report");
  if (!pre) {
    pre = document.createElement("pre");
    pre.id = "mentis-measure-report";
    document.body.appendChild(pre);
  }
  pre.textContent = ev.data || "";
});
</script></head>
<body style="margin:0;padding:0">
<iframe id="mframe" src="__COPY_NAME__" width="__WIDTH__" height="__HEIGHT__" frameborder="0" scrolling="no" style="border:0;display:block"></iframe>
</body></html>
"""


@dataclass
class OverflowElement:
    item_id: str
    tag: str
    ident: str
    text: str
    left: int
    right: int
    width: int


@dataclass
class FormCheck:
    item_id: str
    index: int
    ident: str
    action: str
    method: str
    fields: int
    required_fields: int
    empty_check_validity: bool | None = None
    empty_submit_fired: bool | None = None
    filled_check_validity: bool | None = None
    filled_submit_fired: bool | None = None
    sync_dom_delta: str = ""
    notes: list = field(default_factory=list)


@dataclass
class ViewportMeasurement:
    name: str
    width: int
    height: int
    inner_width: int = 0
    scroll_width: int = 0
    overflow_px: int = 0
    page_title: str = ""
    overflowing: list = field(default_factory=list)      # list[OverflowElement]
    clipped_text: list = field(default_factory=list)     # list[OverflowElement]（容器内被裁剪）
    forms: list = field(default_factory=list)            # list[FormCheck]
    network_intent: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    measurement_copy: str = ""


@dataclass
class RenderMeasurement:
    source_path: str
    source_sha256: str
    browser: str
    browser_version: str
    viewports: list = field(default_factory=list)        # list[ViewportMeasurement]
    limitations: list = field(default_factory=list)
    created_at: str = ""

    def item_ids(self) -> set:
        ids = set()
        for v in self.viewports:
            ids.add(f"mv:{v.name}")
            ids.update(el.item_id for el in v.overflowing)
            ids.update(el.item_id for el in v.clipped_text)
            ids.update(fk.item_id for fk in v.forms)
        return ids

    def render_text(self) -> str:
        lines = [f"[渲染测量] 来源 {self.source_path}（sha256 前 12 位 {self.source_sha256[:12]}）；"
                 f"浏览器 {Path(self.browser).name}（无界面，可重复执行）"]
        for v in self.viewports:
            head = (f"- (mv:{v.name}) {v.name} {v.width}×{v.height}：innerWidth {v.inner_width}、"
                    f"scrollWidth {v.scroll_width}、**横向溢出 {v.overflow_px}px**、溢出元素 {len(v.overflowing)} 个")
            lines.append(head)
            for el in v.overflowing:
                lines.append(f"  - ({el.item_id}) <{el.tag}{el.ident}>「{el.text}」"
                             f" left {el.left} / right {el.right} / width {el.width}")
            for el in v.clipped_text:
                lines.append(f"  - ({el.item_id}) <{el.tag}{el.ident}>「{el.text}」"
                             f" 容器内被裁剪：内容宽 {el.right} > 可见宽 {el.width}")
            for fk in v.forms:
                lines.append(
                    f"  - ({fk.item_id}) 表单 {fk.ident}：字段 {fk.fields}（必填 {fk.required_fields}）；"
                    f"空表单校验 {fk.empty_check_validity}（提交事件触发={fk.empty_submit_fired}）；"
                    f"填写后校验 {fk.filled_check_validity}（提交事件触发={fk.filled_submit_fired}）；"
                    f"声明 action='{fk.action or '(空)'}' method={fk.method}；"
                    f"提交后同步 DOM 变化：{fk.sync_dom_delta[:80] or '（无）'}"
                    + (f"；备注 {'；'.join(fk.notes)}" if fk.notes else ""))
            ni = v.network_intent or {}
            lines.append(f"  - 网络意图声明：action={ni.get('actions') or ['(无)']}、"
                         f"脚本含 fetch={ni.get('hasFetch')} XHR={ni.get('hasXHR')} sendBeacon={ni.get('hasBeacon')}")
            if v.errors:
                lines.append(f"  - 测量错误：{'；'.join(v.errors)}")
        lines.append("[边界] " + "；".join(self.limitations))
        return "\n".join(lines)


def _inject_measurement_script(raw_html: bytes) -> bytes:
    script = '<script id="mentis-measure-script">' + _MEASURE_JS + "</script>"
    lower = raw_html.lower()
    pos = lower.rfind(b"</body>")
    if pos == -1:
        pos = lower.rfind(b"</html>")
    if pos == -1:
        return raw_html + script.encode("utf-8")
    return raw_html[:pos] + script.encode("utf-8") + raw_html[pos:]


def _parse_report(dump: str) -> dict:
    match = re.search(re.escape(_REPORT_BEGIN) + r"([\s\S]*?)" + re.escape(_REPORT_END), dump)
    if not match:
        raise ValueError("渲染输出中未找到测量报告（脚本可能未执行）")
    return json.loads(html_mod.unescape(match.group(1)))


_VERSION_PATTERN = re.compile(r"\d+(\.\d+)+")


def extract_browser_version(stdout: str, returncode: int) -> str:
    """只在 stdout 确实像版本号时采信，否则 unknown。

    Windows 上 chrome.exe --version 曾打印界面提示（如"已在现有的浏览器会话中
    打开。"）且退出码 0——把这类文本当版本号会污染测量记录（2026-10-03 复现）。
    """
    text = (stdout or "").strip()
    if returncode == 0 and text and _VERSION_PATTERN.search(text):
        return text
    return "unknown"


def measure_viewport(page: Path, name: str, width: int, height: int, out_dir: Path,
                     executable: Path, timeout_s: int, runner) -> ViewportMeasurement:
    result = ViewportMeasurement(name=name, width=width, height=height)
    copy_path = out_dir / f"measure-copy-{name}.html"
    copy_path.write_bytes(_inject_measurement_script(page.read_bytes()))
    result.measurement_copy = str(copy_path)
    # Windows 无界面窗口宽度下限 500px（实测 2026-09-28，Chrome 154）：
    # --window-size=390 在 dump-dom 下被钳制为 500。用 iframe 包装获得真实 390/1280 布局。
    wrapper_path = out_dir / f"measure-wrapper-{name}.html"
    wrapper_path.write_text(
        _WRAPPER_TEMPLATE.replace("__COPY_NAME__", copy_path.name)
        .replace("__WIDTH__", str(width)).replace("__HEIGHT__", str(height)),
        encoding="utf-8")
    profile = out_dir / f"profile-{name}-{uuid4().hex[:8]}"
    command = [str(executable), "--headless=new", "--disable-gpu",
               "--disable-features=Vulkan", "--disable-extensions", "--no-first-run",
               f"--user-data-dir={profile}", "--window-size=800,900",
               "--virtual-time-budget=2500", "--dump-dom", wrapper_path.as_uri()]
    try:
        completed = runner(command, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout_s)
        if completed.returncode != 0:
            result.errors.append(f"浏览器退出码 {completed.returncode}")
            return result
        report = _parse_report(completed.stdout or "")
    except subprocess.TimeoutExpired:
        result.errors.append("浏览器测量超时，结果未知")
        return result
    except (ValueError, json.JSONDecodeError) as exc:
        result.errors.append(f"测量报告解析失败: {exc}")
        return result
    except OSError as exc:
        # 浏览器进程无法创建（权限受限/可执行文件缺失）：逐视口记账，
        # 不让观察链崩溃——下游按"测量不可信"处理，不冒充零溢出通过。
        result.errors.append(f"浏览器无法启动: {type(exc).__name__}: {exc}")
        return result
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    result.inner_width = int(report.get("innerWidth", 0))
    result.scroll_width = int(report.get("scrollWidth", 0))
    result.overflow_px = int(report.get("overflowPx", 0))
    result.page_title = str(report.get("pageTitle", ""))
    for idx, el in enumerate(report.get("overflowing", []), start=1):
        result.overflowing.append(OverflowElement(
            item_id=f"ov:{name}:{idx}", tag=str(el.get("tag", "")), ident=str(el.get("ident", "")),
            text=str(el.get("text", "")), left=int(el.get("left", 0)),
            right=int(el.get("right", 0)), width=int(el.get("width", 0))))
    for idx, el in enumerate(report.get("clippedText", []), start=1):
        result.clipped_text.append(OverflowElement(
            item_id=f"clip:{name}:{idx}", tag=str(el.get("tag", "")), ident=str(el.get("ident", "")),
            text=str(el.get("text", "")), left=0, right=int(el.get("scrollWidth", 0)),
            width=int(el.get("clientWidth", 0))))
    for idx, fk in enumerate(report.get("forms", []), start=1):
        result.forms.append(FormCheck(
            item_id=f"formchk:{name}:{idx}", index=int(fk.get("index", 0)),
            ident=str(fk.get("ident", "")), action=str(fk.get("action", "")),
            method=str(fk.get("method", "")), fields=int(fk.get("fields", 0)),
            required_fields=int(fk.get("requiredFields", 0)),
            empty_check_validity=fk.get("emptyCheckValidity"),
            empty_submit_fired=fk.get("emptySubmitFired"),
            filled_check_validity=fk.get("filledCheckValidity"),
            filled_submit_fired=fk.get("filledSubmitFired"),
            sync_dom_delta=str(fk.get("syncDomDelta", "")),
            notes=[str(n) for n in fk.get("notes", [])]))
    result.network_intent = dict(report.get("networkIntent", {}))
    result.errors = [str(e) for e in report.get("errors", [])]
    return result


def measure_page(page: str | Path, output_dir: str | Path, browser: str | Path | None = None,
                 timeout_s: int = 60, runner=None, viewports: dict | None = None) -> RenderMeasurement:
    """对真实 HTML 文件做双视口渲染测量；原文件不修改，失败逐项记账。"""
    source = Path(page).resolve()
    if not source.is_file() or source.suffix.lower() not in (".html", ".htm"):
        raise ValueError("输入必须是存在的本地 HTML 文件")
    if not 1 <= timeout_s <= 120:
        raise ValueError("测量超时须在 1—120 秒内")
    executable = Path(browser).resolve() if browser else _find_browser()
    if executable is None or not executable.is_file():
        raise FileNotFoundError("本机未发现可用的 Chrome/Edge 浏览器")
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    run = runner or subprocess.run
    try:
        version_proc = run([str(executable), "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10)
        version = extract_browser_version(version_proc.stdout, version_proc.returncode)
    except (OSError, subprocess.TimeoutExpired):
        version = "unknown"
    record = RenderMeasurement(
        source_path=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        browser=str(executable), browser_version=version,
        limitations=[
            "测量在注入脚本的临时副本 + iframe 包装页上执行（原文件不改，sha256 已记录）；"
            "无界面渲染初始状态，绕开 Windows 无头窗口 500px 最小宽度钳制以获得真实视口布局",
            "真实网络请求未验证：提交测试强制阻止导航，仅扫描 action/method/fetch/XHR/sendBeacon 声明",
            "同步 DOM 变化仅捕获提交后立即可见的文本差异；异步提示、点击手感、对比度不在此范围",
            "字体渲染与真实设备可能不同；数值为该浏览器该视口下的一次测量",
        ],
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    for name, (width, height) in (viewports or VIEWPORTS).items():
        record.viewports.append(measure_viewport(
            source, name, width, height, out_dir, executable, timeout_s, run))
    return record


def _find_browser() -> Path | None:
    for name in ("chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    from mentis.browser_observation import _WINDOWS_BROWSERS

    return next((path for path in _WINDOWS_BROWSERS if path.is_file()), None)
