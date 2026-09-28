"""P2.2 结构观察器：从真实 HTML 文件提取结构清单（无渲染截图）。

诚实边界：本观察器不产生视觉信息。观察记录必须携带 limitations，
凡依赖画面/交互的结论在本阶段一律"未验证"，不得声称通过。
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser


@dataclass
class ObservationItem:
    item_id: str        # 如 "h:1"、"btn:2"、"input:1"，供证据引用核对
    kind: str
    text: str
    extra: dict = field(default_factory=dict)


@dataclass
class ObservationRecord:
    source_path: str
    sha256: str
    kind: str = "structure"
    items: list = field(default_factory=list)          # list[ObservationItem]
    limitations: list = field(default_factory=list)
    created_at: str = ""

    def item_ids(self) -> set:
        return {i.item_id for i in self.items}

    def render_text(self) -> str:
        """供提示词使用的紧凑文本视图。"""
        lines = [f"[观察记录] 来源 {self.source_path}（sha256 前 12 位 {self.sha256[:12]}）"]
        for item in self.items:
            extra = f"｜{'；'.join(f'{k}={v}' for k, v in item.extra.items())}" if item.extra else ""
            lines.append(f"- ({item.item_id}) {item.kind}: {item.text}{extra}")
        lines.append("[边界] " + "；".join(self.limitations))
        return "\n".join(lines)


class _StructureParser(HTMLParser):
    _HEADINGS = {"h1", "h2", "h3", "h4"}
    _INTERACTIVE = {"button", "a"}
    _FORM = {"input", "textarea", "select"}
    _VOID = {"img", "input", "br", "hr", "meta", "link"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[ObservationItem] = []
        self._counters: dict = {}
        self._stack: list = []
        self._capture_tag = None
        self._capture_buf: list = []
        self._capture_extra: dict = {}

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}:{self._counters[prefix]}"

    def handle_starttag(self, tag, attrs):
        attrs_dict = {k: (v or "") for k, v in attrs}
        if tag in self._HEADINGS:
            self._begin_capture(self._next_id("h"), tag, {"level": tag})
        elif tag in self._INTERACTIVE:
            self._begin_capture(
                self._next_id("btn" if tag == "button" else "link"),
                tag,
                {k: v for k, v in attrs_dict.items() if k in ("href", "type", "id") and v},
            )
        elif tag == "img":
            self.items.append(
                ObservationItem(
                    self._next_id("img"), "img",
                    attrs_dict.get("alt", ""),
                    {"alt_present": "yes" if attrs_dict.get("alt", "").strip() else "no"},
                )
            )
        elif tag in self._FORM:
            self.items.append(
                ObservationItem(
                    self._next_id("input"), tag,
                    attrs_dict.get("name") or attrs_dict.get("id") or "(unnamed)",
                    {
                        "type": attrs_dict.get("type", ""),
                        "required": "yes" if "required" in attrs_dict else "no",
                    },
                )
            )
        elif tag == "form":
            self.items.append(ObservationItem(self._next_id("form"), "form", "", {}))

    def handle_endtag(self, tag):
        if self._capture_tag == tag:
            self._flush_capture()

    def handle_data(self, data):
        if self._capture_tag is not None:
            self._capture_buf.append(data)

    def _begin_capture(self, item_id, kind, extra):
        self._flush_capture()
        self._capture_tag = kind
        self._capture_extra = {"item_id": item_id, "extra": extra}
        self._capture_buf = []

    def _flush_capture(self):
        if self._capture_tag is None:
            return
        text = " ".join("".join(self._capture_buf).split())
        self.items.append(
            ObservationItem(
                self._capture_extra["item_id"],
                self._capture_tag,
                text,
                self._capture_extra["extra"],
            )
        )
        self._capture_tag = None
        self._capture_buf = []
        self._capture_extra = {}

    def close(self):
        super().close()
        self._flush_capture()


def observe_structure(path: str) -> ObservationRecord:
    """读取真实文件并产出结构观察记录；文件必须存在。"""
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    parser = _StructureParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    return ObservationRecord(
        source_path=path,
        sha256=sha,
        items=parser.items,
        limitations=[
            "仅结构记录，无渲染截图：视觉呈现类结论（美观、氛围、布局效果）未验证",
            "无交互执行记录：行为类结论（表单实际行为）未验证，仅能看到声明结构",
            "无法检测横向溢出：需要真实视口渲染（P3.1 观察链）",
        ],
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
