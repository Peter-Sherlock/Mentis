"""P3.1 本地 HTML 初始渲染截图（无点击、无视觉质量自动评分）。"""

import hashlib
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4


VIEWPORTS = {"desktop": (1280, 800), "mobile": (390, 844)}
_WINDOWS_BROWSERS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


@dataclass
class RenderedView:
    name: str
    requested_width: int
    requested_height: int
    screenshot_path: str = ""
    actual_width: int = 0
    actual_height: int = 0
    sha256: str = ""
    error: str = ""


@dataclass
class BrowserObservation:
    source_path: str
    source_sha256: str
    browser: str
    browser_version: str
    views: list[RenderedView] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    created_at: str = ""


def find_browser() -> Path | None:
    for name in ("chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return next((path for path in _WINDOWS_BROWSERS if path.is_file()), None)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as image:
        header = image.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("截图不是有效 PNG")
    return struct.unpack(">II", header[16:24])


def capture_local_html(page: str | Path, output_dir: str | Path,
                       browser: str | Path | None = None, timeout_s: int = 30,
                       runner=None) -> BrowserObservation:
    """用独立浏览器配置采集双视口截图；每个失败单独记账，不虚报渲染。"""
    source = Path(page).resolve()
    if not source.is_file() or source.suffix.lower() not in (".html", ".htm"):
        raise ValueError("输入必须是存在的本地 HTML 文件")
    if not 1 <= timeout_s <= 120:
        raise ValueError("浏览器超时须在 1—120 秒内")
    executable = Path(browser).resolve() if browser else find_browser()
    if executable is None or not executable.is_file():
        raise FileNotFoundError("本机未发现可用的 Chrome/Edge 浏览器")
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    run = runner or subprocess.run
    try:
        version_proc = run([str(executable), "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10)
        version = version_proc.stdout.strip() if version_proc.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        version = "unknown"
    record = BrowserObservation(
        source_path=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        browser=str(executable), browser_version=version,
        limitations=[
            "仅捕获两个视口的初始画面；未执行点击、输入、提交或键盘操作",
            "未自动测量横向溢出、对比度或可访问性；截图需人工复核",
            "本地 file:// 页面不证明部署环境行为",
        ],
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    for name, (width, height) in VIEWPORTS.items():
        view = RenderedView(name=name, requested_width=width, requested_height=height)
        screenshot = destination / f"{name}.png"
        profile = destination / f"profile-{name}-{uuid4().hex[:8]}"
        command = [str(executable), "--headless=new", "--disable-gpu",
                   "--disable-features=Vulkan", "--disable-extensions", "--no-first-run",
                   f"--user-data-dir={profile}", f"--window-size={width},{height}",
                   f"--screenshot={screenshot}", source.as_uri()]
        try:
            completed = run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout_s)
            if completed.returncode != 0:
                view.error = f"浏览器退出码 {completed.returncode}"
            elif not screenshot.is_file():
                view.error = "浏览器未生成截图"
            else:
                view.actual_width, view.actual_height = _png_dimensions(screenshot)
                view.screenshot_path = str(screenshot)
                view.sha256 = hashlib.sha256(screenshot.read_bytes()).hexdigest()
        except subprocess.TimeoutExpired:
            view.error = "浏览器截图超时，渲染状态未知"
        except (OSError, ValueError) as exc:
            view.error = f"截图失败: {type(exc).__name__}"
        record.views.append(view)
    return record
