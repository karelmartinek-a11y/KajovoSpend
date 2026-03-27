from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import sys
from dataclasses import asdict, dataclass
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Iterable, Sequence


def _ensure_offscreen_qt() -> None:
    # Headless Qt musí být nastavený ještě před importem PySide6.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")


_ensure_offscreen_qt()

import yaml
from PIL import Image, ImageFilter, ImageOps
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTabBar, QTableView, QTextEdit, QWidget

ROOT_DIR = Path(__file__).resolve().parents[2]
ASSETS_DIR = ROOT_DIR / "assets"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.yaml"
FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures"
SRC_DIR = ROOT_DIR / "src"

DEFAULT_FIXTURE_LINES = (
    "ICO: 12345678",
    "VS: 2025001",
    "Datum vystaveni: 01.01.2025",
    "Cena celkem 100,00 CZK",
    "Zaokrouhleni 100,00",
)


@dataclass(slots=True)
class SmokeEnvironment:
    repo_root: Path
    workspace: Path
    config_path: Path
    data_dir: Path
    input_dir: Path
    output_dir: Path
    log_dir: Path
    models_dir: Path
    artifacts_dir: Path
    fixture_pdf: Path
    template_pdf: Path


@dataclass(slots=True)
class UiIncident:
    kind: str
    widget: str
    path: str
    text: str
    geometry: tuple[int, int, int, int]
    detail: str


@dataclass(slots=True)
class CaptureArtifact:
    name: str
    image_path: str
    widget_count: int
    incidents: list[UiIncident]


@dataclass(slots=True)
class ImportCaseArtifact:
    name: str
    input_path: str
    status: str
    text_method: str | None
    document_ids: list[int]
    moved_to: str | None
    supported: bool
    detail: str | None = None


@dataclass(slots=True)
class SelectionTruthEvidence:
    scope: str
    tab: str
    row_index: int
    backend_id: int | None
    backend_file_id: int | None
    backend_path: str | None
    ui_source_text: str
    action_enabled: bool
    preview_has_pixmap: bool
    preview_has_scene_items: bool


@dataclass(slots=True)
class TruthFinding:
    scope: str
    kind: str
    detail: str
    backend_path: str | None
    ui_source_text: str
    action_enabled: bool
    preview_has_pixmap: bool


@dataclass(slots=True)
class PopulatedStateArtifact:
    docs: dict[str, Any]
    items: dict[str, Any]
    screenshots: list[str]
    truth_findings: list[TruthFinding]


def ensure_repo_on_syspath() -> None:
    repo = str(ROOT_DIR)
    src = str(SRC_DIR)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    if src not in sys.path:
        sys.path.insert(0, src)


def create_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _load_example_config() -> dict[str, Any]:
    if EXAMPLE_CONFIG_PATH.exists():
        with EXAMPLE_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
            if isinstance(data, dict):
                return data
    return {
        "app": {},
        "paths": {},
        "service": {},
        "ocr": {},
        "openai": {},
        "performance": {},
        "features": {},
    }


def _write_minimal_pdf_bytes(lines: Sequence[str], *, font_size: int = 10, start_y: int = 750, line_step: int = 14) -> bytes:
    escaped_lines = [str(line or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    y = int(start_y)
    text_ops = []
    for line in escaped_lines:
        text_ops.append(f"BT /F1 {int(font_size)} Tf 50 {y} Td ({line}) Tj ET")
        y -= int(line_step)
    content = "\n".join(text_ops) or f"BT /F1 {int(font_size)} Tf 50 {int(start_y)} Td (KajovoSpend) Tj ET"

    def obj(n: int, body: str) -> str:
        return f"{n} 0 obj\n{body}\nendobj\n"

    header = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    objs = [
        obj(1, "<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(
            3,
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >> >>",
        ),
    ]
    stream_len = len(content.encode("latin1"))
    objs.append(f"4 0 obj\n<< /Length {stream_len} >>\nstream\n{content}\nendstream\nendobj\n")
    objs.append(obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))

    offsets = [0]
    cur = len(header.encode("latin1"))
    for block in objs:
        offsets.append(cur)
        cur += len(block.encode("latin1"))

    xref_start = cur
    xref = "xref\n0 6\n"
    xref += "0000000000 65535 f \n"
    for off in offsets[1:]:
        xref += f"{off:010d} 00000 n \n"
    trailer = "trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_start) + "\n%%EOF\n"
    return (header + "".join(objs) + xref + trailer).encode("latin1")


def write_smoke_fixture_pdf(
    target: Path,
    *,
    lines: Sequence[str] = DEFAULT_FIXTURE_LINES,
    font_size: int = 10,
    start_y: int = 750,
    line_step: int = 14,
) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_write_minimal_pdf_bytes(lines, font_size=font_size, start_y=start_y, line_step=line_step))
    return target


def write_smoke_fixture_image(target: Path, *, lines: Sequence[str] = DEFAULT_FIXTURE_LINES) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    from PIL import ImageDraw, ImageFont

    image = Image.new("RGB", (2480, 3508), "white")
    draw = ImageDraw.Draw(image)
    font = None
    font_candidates = [
        Path(os.environ.get("WINDIR", r"C:\\Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", r"C:\\Windows")) / "Fonts" / "segoeui.ttf",
    ]
    for candidate in font_candidates:
        try:
            if candidate.exists():
                font = ImageFont.truetype(str(candidate), 72)
                break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    y = 160
    for line in lines:
        draw.text((160, y), str(line), fill="black", font=font)
        y += 140
    image.save(target)
    return target


def _build_smoke_template_schema_text() -> str:
    schema = {
        "version": 1,
        "fields": {
            "supplier_ico": {"page": 1, "box": [0.04, 0.05, 0.96, 0.14]},
            "doc_number": {"page": 1, "box": [0.04, 0.14, 0.96, 0.24]},
            "issue_date": {"page": 1, "box": [0.04, 0.24, 0.96, 0.34]},
            "total_with_vat": {"page": 1, "box": [0.04, 0.34, 0.96, 0.48]},
            "items_region": {"page": 1, "box": [0.04, 0.48, 0.96, 0.96]},
        },
    }
    return json.dumps(schema, ensure_ascii=False, indent=2)


def _preview_has_pixmap(view: Any) -> bool:
    try:
        pix_item = getattr(view, "_pix_item", None)
        if pix_item is None:
            return False
        pixmap = pix_item.pixmap()
        return bool(pixmap and not pixmap.isNull())
    except Exception:
        return False


def _preview_has_items(view: Any) -> bool:
    try:
        scene = view.scene()
        if scene is None:
            return False
        return bool(scene.items())
    except Exception:
        return False


def evaluate_selection_truth(evidence: SelectionTruthEvidence) -> list[TruthFinding]:
    findings: list[TruthFinding] = []
    backend_path = (evidence.backend_path or "").strip()
    ui_source = (evidence.ui_source_text or "").strip()
    ui_claims_path = _is_truthy_path_text(ui_source)

    if backend_path:
        if not ui_claims_path:
            findings.append(
                TruthFinding(
                    scope=evidence.scope,
                    kind="source_line_empty",
                    detail="UI neukazuje zdroj, i když backend má skutečný soubor.",
                    backend_path=backend_path,
                    ui_source_text=ui_source,
                    action_enabled=evidence.action_enabled,
                    preview_has_pixmap=evidence.preview_has_pixmap,
                )
            )
        elif ui_source != backend_path:
            findings.append(
                TruthFinding(
                    scope=evidence.scope,
                    kind="source_line_mismatch",
                    detail="UI source line neodpovídá backendové cestě.",
                    backend_path=backend_path,
                    ui_source_text=ui_source,
                    action_enabled=evidence.action_enabled,
                    preview_has_pixmap=evidence.preview_has_pixmap,
                )
            )
        if not evidence.action_enabled:
            findings.append(
                TruthFinding(
                    scope=evidence.scope,
                    kind="action_disabled_despite_backend_path",
                    detail="Akce je disabled, přesto backend poskytuje použitelný soubor.",
                    backend_path=backend_path,
                    ui_source_text=ui_source,
                    action_enabled=evidence.action_enabled,
                    preview_has_pixmap=evidence.preview_has_pixmap,
                )
            )
        if not evidence.preview_has_pixmap:
            findings.append(
                TruthFinding(
                    scope=evidence.scope,
                    kind="preview_empty_despite_backend_path",
                    detail="Preview je prázdný, ačkoli backend vrací skutečnou cestu.",
                    backend_path=backend_path,
                    ui_source_text=ui_source,
                    action_enabled=evidence.action_enabled,
                    preview_has_pixmap=evidence.preview_has_pixmap,
                )
            )
    else:
        if evidence.action_enabled:
            findings.append(
                TruthFinding(
                    scope=evidence.scope,
                    kind="action_enabled_without_backend_path",
                    detail="Akce působí aktivně, ale backendová cesta chybí.",
                    backend_path=backend_path or None,
                    ui_source_text=ui_source,
                    action_enabled=evidence.action_enabled,
                    preview_has_pixmap=evidence.preview_has_pixmap,
                )
            )
        if ui_claims_path:
            findings.append(
                TruthFinding(
                    scope=evidence.scope,
                    kind="source_line_claims_path_without_backend",
                    detail="UI ukazuje zdroj, ale backendovou cestu nemá.",
                    backend_path=backend_path or None,
                    ui_source_text=ui_source,
                    action_enabled=evidence.action_enabled,
                    preview_has_pixmap=evidence.preview_has_pixmap,
                )
            )
        if evidence.preview_has_pixmap:
            findings.append(
                TruthFinding(
                    scope=evidence.scope,
                    kind="preview_shows_without_backend_path",
                    detail="Preview ukazuje obsah bez backendové cesty.",
                    backend_path=backend_path or None,
                    ui_source_text=ui_source,
                    action_enabled=evidence.action_enabled,
                    preview_has_pixmap=evidence.preview_has_pixmap,
                )
            )

    return findings


def _is_truthy_path_text(text: str | None) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if lowered.startswith("zdroj není dostupný"):
        return False
    if lowered.startswith("vyberte "):
        return False
    return True


def _relpath_text(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except Exception:
        return path.as_posix()


def _safe_file_stem(text: str) -> str:
    cleaned = []
    for ch in str(text or ""):
        if ch.isalnum():
            cleaned.append(ch)
        else:
            cleaned.append("_")
    stem = "".join(cleaned).strip("_")
    return stem or "item"


def build_smoke_environment(repo_root: Path | None = None, *, workspace_name: str = "kajovospend-smoke") -> SmokeEnvironment:
    repo_root = Path(repo_root or ROOT_DIR)
    workspace = Path(tempfile.mkdtemp(prefix=f"{workspace_name}-"))
    data_dir = workspace / "data"
    input_dir = workspace / "input"
    output_dir = workspace / "output"
    log_dir = workspace / "logs"
    models_dir = workspace / "models"
    artifacts_dir = workspace / "artifacts"
    for path in (data_dir, input_dir, output_dir, log_dir, models_dir, artifacts_dir):
        path.mkdir(parents=True, exist_ok=True)

    fixture_pdf = workspace / "fixture.pdf"
    repo_fixture_pdf = FIXTURE_DIR / "smoke_receipt.pdf"
    if repo_fixture_pdf.exists():
        shutil.copy2(repo_fixture_pdf, fixture_pdf)
    else:
        write_smoke_fixture_pdf(fixture_pdf)

    template_pdf = data_dir / "templates" / "gui_smoke" / "sample.pdf"
    template_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixture_pdf, template_pdf)

    cfg = _load_example_config()
    cfg.setdefault("app", {})
    cfg.setdefault("paths", {})
    cfg.setdefault("ocr", {})
    cfg.setdefault("openai", {})
    cfg.setdefault("service", {})
    cfg.setdefault("performance", {})
    cfg.setdefault("features", {})

    cfg["app"]["data_dir"] = str(data_dir)
    cfg["app"]["db_path"] = str(data_dir / "kajovospend.sqlite")
    cfg["app"]["working_db_path"] = str(data_dir / "kajovospend.sqlite")
    cfg["app"]["production_db_path"] = str(data_dir / "kajovospend-production.sqlite")
    cfg["app"]["log_dir"] = str(log_dir)
    cfg["paths"]["input_dir"] = str(input_dir)
    cfg["paths"]["output_dir"] = str(output_dir)
    cfg["paths"]["processing_db"] = str(workspace / "processing.sqlite")
    cfg["ocr"]["models_dir"] = str(models_dir)
    cfg["ocr"]["pdf_dpi"] = 200
    cfg["ocr"]["force_ocr_on_parse_failure"] = False
    cfg["ocr"]["ensemble"] = {
        "enabled": False,
        "dpis": [200],
        "rotations": [0],
        "include_reconstructed": False,
        "max_runtime_sec": 1,
        "max_candidates": 1,
    }
    cfg["openai"]["enabled"] = False
    cfg["openai"]["api_key"] = ""
    cfg["openai"]["auto_enable"] = False
    cfg["features"]["openai_fallback"] = {"enabled": False}

    config_path = workspace / "config.yaml"
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)

    return SmokeEnvironment(
        repo_root=repo_root,
        workspace=workspace,
        config_path=config_path,
        data_dir=data_dir,
        input_dir=input_dir,
        output_dir=output_dir,
        log_dir=log_dir,
        models_dir=models_dir,
        artifacts_dir=artifacts_dir,
        fixture_pdf=fixture_pdf,
        template_pdf=template_pdf,
    )


def load_smoke_config(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise TypeError("Konfigurace smoke běhu musí být slovník.")
    return data


def _widget_text(widget: QWidget) -> str:
    if isinstance(widget, QLabel):
        return widget.text()
    if isinstance(widget, QPushButton):
        return widget.text()
    if isinstance(widget, QCheckBox):
        return widget.text()
    if isinstance(widget, QLineEdit):
        if widget.isReadOnly():
            return widget.text() or widget.placeholderText()
        return widget.placeholderText() or widget.text()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, (QSpinBox, QDoubleSpinBox, QDateEdit)):
        return widget.text()
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return widget.toPlainText()
    if isinstance(widget, QTabBar):
        return " | ".join(widget.tabText(i) for i in range(widget.count()))
    return ""


def _widget_label(widget: QWidget) -> str:
    cls = type(widget).__name__
    obj = widget.objectName()
    text = _widget_text(widget).replace("\n", " ").strip()
    if obj:
        base = f"{cls}#{obj}"
    else:
        base = cls
    if text:
        return f"{base} [{text[:80]}]"
    return base


def _widget_geometry(widget: QWidget) -> tuple[int, int, int, int]:
    geo = widget.geometry()
    return int(geo.x()), int(geo.y()), int(geo.width()), int(geo.height())


def _font_metrics(widget: QWidget) -> QFontMetrics:
    return QFontMetrics(widget.font())


def _record(widget: QWidget, kind: str, detail: str) -> UiIncident:
    return UiIncident(
        kind=kind,
        widget=widget.__class__.__name__,
        path=_widget_label(widget),
        text=_widget_text(widget).replace("\n", " ").strip()[:140],
        geometry=_widget_geometry(widget),
        detail=detail,
    )


def _iter_visible_widgets(root: QWidget) -> Iterable[QWidget]:
    for widget in root.findChildren(QWidget):
        if widget is root:
            continue
        if not widget.isVisible():
            continue
        if not widget.isVisibleTo(root):
            continue
        if widget.width() <= 0 or widget.height() <= 0:
            continue
        yield widget


def _is_ancestor(candidate: QWidget, widget: QWidget) -> bool:
    parent = widget.parentWidget()
    while parent is not None:
        if parent is candidate:
            return True
        parent = parent.parentWidget()
    return False


def _text_overflow_incidents(widget: QWidget) -> list[UiIncident]:
    incidents: list[UiIncident] = []
    text = _widget_text(widget).strip()
    if not text:
        return incidents

    fm = _font_metrics(widget)
    rect = widget.contentsRect()
    width = max(1, rect.width())
    height = max(1, rect.height())

    if isinstance(widget, QLabel) and widget.wordWrap():
        wrapped = fm.boundingRect(QRect(0, 0, width, 10_000), Qt.TextWordWrap, text)
        if wrapped.height() > height + 2:
            incidents.append(
                _record(widget, "text_wrap_overflow", f"potřebuje výšku {wrapped.height()} px, má {height} px")
            )
        if fm.horizontalAdvance(text) > width * 2:
            incidents.append(
                _record(widget, "text_density", f"dlouhý text bez dostatečné šířky pro přehledný banner")
            )
        return incidents

    if isinstance(widget, QTabBar):
        for idx in range(widget.count()):
            tab_text = widget.tabText(idx).strip()
            if not tab_text:
                continue
            tab_rect = widget.tabRect(idx)
            needed = fm.horizontalAdvance(tab_text) + 24
            if needed > tab_rect.width() + 8:
                incidents.append(
                    UiIncident(
                        kind="tab_text_overflow",
                        widget=widget.__class__.__name__,
                        path=f"{_widget_label(widget)}::tab[{idx}]",
                        text=tab_text[:140],
                        geometry=(tab_rect.x(), tab_rect.y(), tab_rect.width(), tab_rect.height()),
                        detail=f"potřebuje {needed} px, tab má {tab_rect.width()} px",
                    )
                )
        return incidents

    if isinstance(widget, (QPushButton, QCheckBox)):
        needed = fm.horizontalAdvance(text) + 28
        if widget.width() + 8 < needed:
            incidents.append(_record(widget, "button_overflow", f"potřebuje {needed} px, má {widget.width()} px"))
        return incidents

    if isinstance(widget, QComboBox):
        needed = max(widget.sizeHint().width(), fm.horizontalAdvance(text) + 30)
        if widget.width() + 8 < needed:
            incidents.append(_record(widget, "combo_narrow", f"potřebuje {needed} px, má {widget.width()} px"))
        return incidents

    if isinstance(widget, (QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit)):
        if widget.objectName() == "qt_spinbox_lineedit":
            needed = fm.horizontalAdvance(text) + 24
        elif isinstance(widget, QDateEdit):
            needed = fm.horizontalAdvance(text) + 40
        else:
            needed = widget.sizeHint().width()
        expected_chars = widget.property("audit_expected_chars")
        if expected_chars:
            needed = max(needed, fm.horizontalAdvance("0" * max(1, int(expected_chars))) + 28)
        if widget.width() + 8 < needed:
            incidents.append(_record(widget, "input_narrow", f"potřebuje {needed} px, má {widget.width()} px"))
        return incidents

    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        if widget.width() < widget.sizeHint().width() * 0.8:
            incidents.append(_record(widget, "textedit_narrow", f"šířka {widget.width()} px je pod běžnou rezervou"))
        return incidents

    if isinstance(widget, QLabel):
        needed = fm.horizontalAdvance(text) + 8
        if widget.width() + 12 < needed:
            incidents.append(_record(widget, "label_overflow", f"potřebuje {needed} px, má {widget.width()} px"))
        return incidents

    return incidents


def _audit_table_headers(table: QTableView) -> list[UiIncident]:
    incidents: list[UiIncident] = []
    model = table.model()
    header = table.horizontalHeader()
    if model is None or header is None or not isinstance(header, QHeaderView):
        return incidents

    fm = header.fontMetrics()
    viewport_width = header.viewport().width()
    has_horizontal_scroll = table.horizontalScrollBar().maximum() > 0

    for col in range(model.columnCount()):
        if header.isSectionHidden(col):
            continue
        text = str(model.headerData(col, Qt.Horizontal, Qt.DisplayRole) or "").strip()
        if not text:
            continue

        section_width = header.sectionSize(col)
        needed_width = fm.horizontalAdvance(text) + 24
        if needed_width > section_width + 6:
            incidents.append(
                UiIncident(
                    kind="header_overflow",
                    widget=table.__class__.__name__,
                    path=f"{_widget_label(table)}::header[{col}]",
                    text=text[:140],
                    geometry=(header.sectionPosition(col), 0, section_width, header.height()),
                    detail=f"hlavička potřebuje {needed_width} px, sekce má {section_width} px",
                )
            )

        if not has_horizontal_scroll:
            visible_left = header.sectionViewportPosition(col)
            visible_width = min(section_width, max(0, viewport_width - visible_left))
            if visible_width <= 0:
                incidents.append(
                    UiIncident(
                        kind="header_not_visible",
                        widget=table.__class__.__name__,
                        path=f"{_widget_label(table)}::header[{col}]",
                        text=text[:140],
                        geometry=(visible_left, 0, section_width, header.height()),
                        detail="hlavička nemá žádný viditelný viewport",
                    )
                )
                continue
            if needed_width > visible_width + 6:
                incidents.append(
                    UiIncident(
                        kind="header_viewport_overflow",
                        widget=table.__class__.__name__,
                        path=f"{_widget_label(table)}::header[{col}]",
                        text=text[:140],
                        geometry=(visible_left, 0, visible_width, header.height()),
                        detail=f"hlavička potřebuje {needed_width} px, viditelný viewport má {visible_width} px",
                    )
                )

    return incidents


def audit_widget_geometry(root: QWidget) -> list[UiIncident]:
    incidents: list[UiIncident] = []
    visible_widgets = list(_iter_visible_widgets(root))

    for widget in visible_widgets:
        incidents.extend(_text_overflow_incidents(widget))
        if isinstance(widget, QTableView):
            incidents.extend(_audit_table_headers(widget))

    by_parent: dict[QWidget, list[QWidget]] = {}
    for widget in visible_widgets:
        parent = widget.parentWidget()
        if parent is None:
            continue
        by_parent.setdefault(parent, []).append(widget)

    for parent, children in by_parent.items():
        for left_index, left in enumerate(children):
            if left.width() <= 1 or left.height() <= 1:
                continue
            rect_left = left.geometry()
            for right in children[left_index + 1 :]:
                if right.width() <= 1 or right.height() <= 1:
                    continue
                if _is_ancestor(left, right) or _is_ancestor(right, left):
                    continue
                rect_right = right.geometry()
                inter = rect_left.intersected(rect_right)
                if inter.width() > 4 and inter.height() > 4:
                    incidents.append(
                        UiIncident(
                            kind="widget_overlap",
                            widget=parent.__class__.__name__,
                            path=_widget_label(parent),
                            text="",
                            geometry=_widget_geometry(parent),
                            detail=f"{_widget_label(left)} × {_widget_label(right)}",
                        )
                    )

    return incidents


def _save_widget_capture(widget: QWidget, target: Path) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.grab()
    pixmap.save(str(target))
    return target


def _settle(app: QApplication, *, rounds: int = 4, sleep_s: float = 0.03) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(sleep_s)


def _main_window_instance(env: SmokeEnvironment):
    ensure_repo_on_syspath()
    from kajovospend.ui.main_window import MainWindow

    app = create_app()
    window = MainWindow(config_path=env.config_path, assets_dir=ASSETS_DIR)
    window.resize(1680, 1200)
    window.show()
    _settle(app, rounds=6, sleep_s=0.05)
    return app, window


def _dialog_instances(env: SmokeEnvironment, parent: QWidget):
    ensure_repo_on_syspath()
    from kajovospend.ui.main_window import SupplierDialog
    from kajovospend.ui.progress import ProgressDialog
    from kajovospend.ui.receipt_template_editor import ReceiptTemplateEditorDialog

    supplier = SupplierDialog(
        parent=parent,
        initial={
            "ico": "12345678",
            "name": "Velmi dlouhý testovací dodavatel s rozšířeným názvem a více identifikačními údaji",
            "dic": "CZ12345678",
            "legal_form": "Společnost s ručením omezeným",
            "street": "U Testovací linky",
            "street_number": "42",
            "orientation_number": "A",
            "city": "Praha",
            "zip_code": "11000",
            "address": "U Testovací linky 42/A, Praha, 11000",
            "is_vat_payer": True,
        },
    )
    supplier.resize(720, 520)

    progress = ProgressDialog(parent=parent)
    progress.set_title("IMPORT DOKLADŮ - dlouhý stavový text pro audit")
    progress.set_step("Zpracovávám testovací dávku a generuji podklad pro binární audit UI.")
    progress.set_determinate(100)
    progress.set_value(47)
    progress.set_batch_text("Cyklus 47/100 | PROD: 12 KAR: 2 DUP: 1 CHYBA: 0")
    progress.resize(700, 240)

    template = {
        "name": "Šablona pro headless audit s dlouhým názvem",
        "enabled": True,
        "sample_file_name": "sample.pdf",
        "sample_file_relpath": _relpath_text(env.template_pdf, env.data_dir),
        "schema_json": "",
        "match_texts_json": "",
    }
    editor = ReceiptTemplateEditorDialog(env, {}, template=template, parent=parent)
    editor.resize(1500, 980)

    return [
        ("supplier_dialog", supplier),
        ("progress_dialog", progress),
        ("receipt_template_editor_dialog", editor),
    ]


def _select_first_row(table: QTableView) -> None:
    model = table.model()
    if model is None or model.rowCount() <= 0:
        return
    idx = model.index(0, 0)
    table.setCurrentIndex(idx)
    table.selectRow(0)


def _collect_doc_truth(window: Any, app: QApplication) -> tuple[SelectionTruthEvidence, list[TruthFinding]]:
    try:
        window.tabs.setCurrentWidget(window.tab_docs)
    except Exception:
        pass
    try:
        window._docs_new_search_v2()
    except Exception:
        pass
    docs_listing = list(getattr(window, "_docs_listing", []) or [])
    if not docs_listing:
        try:
            window._apply_doc_source_state(None, reason="Vyberte doklad pro zobrazení zdroje.")
        except Exception:
            pass
        _settle(app, rounds=4, sleep_s=0.05)
        evidence = SelectionTruthEvidence(
            scope="docs",
            tab="ÚČTENKY",
            row_index=-1,
            backend_id=None,
            backend_file_id=None,
            backend_path=None,
            ui_source_text="",
            action_enabled=bool(getattr(window, "btn_open_source", None) and window.btn_open_source.isEnabled()),
            preview_has_pixmap=_preview_has_pixmap(getattr(window, "preview_view", None)),
            preview_has_scene_items=_preview_has_items(getattr(window, "preview_view", None)),
        )
        findings = evaluate_selection_truth(evidence)
        return evidence, findings

    try:
        _select_first_row(getattr(window, "docs_table"))
    except Exception:
        pass
    try:
        idx = window.docs_table.currentIndex()
        if idx.isValid():
            window._on_doc_selected_v2(idx)
    except Exception:
        pass
    _settle(app, rounds=4, sleep_s=0.05)
    row = 0
    meta = docs_listing[row]
    evidence = SelectionTruthEvidence(
        scope="docs",
        tab="ÚČTENKY",
        row_index=row,
        backend_id=int(meta.get("doc_id") or 0) or None,
        backend_file_id=meta.get("file_id"),
        backend_path=meta.get("path"),
        ui_source_text=str(getattr(window, "doc_src_line", None).text() if getattr(window, "doc_src_line", None) else ""),
        action_enabled=bool(getattr(window, "btn_open_source", None) and window.btn_open_source.isEnabled()),
        preview_has_pixmap=_preview_has_pixmap(getattr(window, "preview_view", None)),
        preview_has_scene_items=_preview_has_items(getattr(window, "preview_view", None)),
    )
    findings = evaluate_selection_truth(evidence)
    return evidence, findings


def _collect_item_truth(window: Any, app: QApplication) -> tuple[SelectionTruthEvidence, list[TruthFinding]]:
    try:
        window.tabs.setCurrentWidget(window.tab_items)
    except Exception:
        pass
    try:
        window._items_new_search_v2()
    except Exception:
        pass
    _settle(app, rounds=4, sleep_s=0.05)
    items_rows = list(getattr(window, "_items_rows", []) or [])
    if not items_rows:
        try:
            window._apply_items_source_state(None, reason="Vyberte položku pro zobrazení zdroje.")
        except Exception:
            pass
        _settle(app, rounds=4, sleep_s=0.05)
        evidence = SelectionTruthEvidence(
            scope="items",
            tab="POLOŽKY",
            row_index=-1,
            backend_id=None,
            backend_file_id=None,
            backend_path=None,
            ui_source_text="",
            action_enabled=bool(getattr(window, "btn_items_open", None) and window.btn_items_open.isEnabled()),
            preview_has_pixmap=_preview_has_pixmap(getattr(window, "items_preview", None)),
            preview_has_scene_items=_preview_has_items(getattr(window, "items_preview", None)),
        )
        findings = evaluate_selection_truth(evidence)
        return evidence, findings

    try:
        _select_first_row(getattr(window, "items_table"))
    except Exception:
        pass
    try:
        idx = window.items_table.currentIndex()
        if idx.isValid():
            window._items_selection_changed_v2(None, None)
    except Exception:
        pass
    _settle(app, rounds=4, sleep_s=0.05)
    row = 0
    meta = items_rows[row]
    evidence = SelectionTruthEvidence(
        scope="items",
        tab="POLOŽKY",
        row_index=row,
        backend_id=int(meta.get("id_item") or 0) or None,
        backend_file_id=meta.get("file_id"),
        backend_path=meta.get("current_path"),
        ui_source_text=str(getattr(window, "items_src", None).text() if getattr(window, "items_src", None) else ""),
        action_enabled=bool(getattr(window, "btn_items_open", None) and window.btn_items_open.isEnabled()),
        preview_has_pixmap=_preview_has_pixmap(getattr(window, "items_preview", None)),
        preview_has_scene_items=_preview_has_items(getattr(window, "items_preview", None)),
    )
    findings = evaluate_selection_truth(evidence)
    return evidence, findings


def _run_import_suite(env: SmokeEnvironment) -> dict[str, Any]:
    ensure_repo_on_syspath()
    from unittest.mock import patch

    from kajovospend.db.migrate import init_production_db, init_working_db
    from kajovospend.db.production_models import StandardReceiptTemplate
    from kajovospend.db.production_session import create_production_engine
    from kajovospend.db.session import make_session_factory
    from kajovospend.db.working_session import create_working_engine
    from kajovospend.integrations.ares import AresRecord
    from kajovospend.service.processor import Processor
    from kajovospend.utils.paths import resolve_app_paths

    cfg = load_smoke_config(env.config_path)
    paths = resolve_app_paths(
        cfg["app"].get("data_dir"),
        cfg["app"].get("db_path"),
        cfg["app"].get("log_dir"),
        cfg.get("ocr", {}).get("models_dir"),
        working_db=cfg["app"].get("working_db_path"),
        production_db=cfg["app"].get("production_db_path"),
    )
    w_engine = create_working_engine(str(paths.working_db_path))
    p_engine = create_production_engine(str(paths.production_db_path))
    init_working_db(w_engine)
    init_production_db(p_engine)
    sf = make_session_factory(w_engine)
    sf_prod = make_session_factory(p_engine)
    log_path = env.log_dir / "import_smoke.log"
    import logging

    log = logging.getLogger("kajovospend.import_smoke")
    log.setLevel(logging.DEBUG)
    processor = Processor(cfg, paths, log, sf, sf_prod)
    report: dict[str, Any] = {
        "workspace": str(env.workspace),
        "config_path": str(env.config_path),
        "fixture_pdf": str(env.fixture_pdf),
        "log_path": str(log_path),
        "cases": [],
        "document_ids": [],
        "support": {
            "ocr_engine": bool(getattr(processor, "ocr_engine", None)),
        },
    }

    def _record_case(
        name: str,
        source_path: Path,
        *,
        patch_mode: str | None = None,
        ares_record: AresRecord | None = None,
        ares_error: Exception | None = None,
        expected_status: str | None = None,
        supported: bool = True,
        template_payload: dict[str, Any] | None = None,
    ) -> ImportCaseArtifact:
        if not supported:
            return ImportCaseArtifact(
                name=name,
                input_path=str(source_path),
                status="SKIPPED",
                text_method=None,
                document_ids=[],
                moved_to=None,
                supported=False,
                detail="OCR runtime není dostupný, case je pouze evidovaný jako nepodporovaný.",
            )
        if template_payload is not None:
            with sf_prod() as session:
                session.add(StandardReceiptTemplate(**template_payload))
                session.commit()
        if patch_mode == "record" and ares_record is not None:
            ctx = patch("kajovospend.service.processor.fetch_by_ico", return_value=ares_record)
        elif patch_mode == "error" and ares_error is not None:
            ctx = patch("kajovospend.service.processor.fetch_by_ico", side_effect=ares_error)
        else:
            ctx = nullcontext()
        with ctx:
            with sf() as session:
                proc_result = processor.process_path(session, source_path)
                session.commit()
        status = str(proc_result.get("status") or "")
        text_method = proc_result.get("text_method")
        document_ids = [int(x) for x in (proc_result.get("document_ids") or [])]
        moved_to = proc_result.get("moved_to")
        detail = None
        if expected_status and status != expected_status:
            detail = f"Očekáván stav {expected_status!r}, ale procesor vrátil {status!r}."
        return ImportCaseArtifact(
            name=name,
            input_path=str(source_path),
            status=status,
            text_method=str(text_method) if text_method is not None else None,
            document_ids=document_ids,
            moved_to=str(moved_to) if moved_to else None,
            supported=True,
            detail=detail,
        )

    base_ares = AresRecord(
        ico="12345678",
        name="ACME s.r.o.",
        legal_form="společnost s ručením omezeným",
        is_vat_payer=True,
        address="U Testu 1, Praha 1, 11000",
        street="U Testu",
        street_number="1",
        city="Praha",
        zip_code="11000",
    )
    embedded_lines = (
        "ICO: 12345678",
        "VS: 2025001",
        "Datum vystaveni: 01.01.2025",
        "Cena celkem 100,00 CZK",
        "Zaokrouhleni 100,00",
    )
    template_lines = (
        "ICO: 23456789",
        "VS: 2025003",
        "Datum vystaveni: 03.01.2025",
        "Cena celkem 300,00 CZK",
        "Zaokrouhleni 300,00",
    )
    ocr_lines = (
        "ICO: 34567890",
        "VS: 2025002",
        "Datum vystaveni: 02.01.2025",
        "Cena celkem 200,00 CZK",
        "Zaokrouhleni 200,00",
    )
    quarantine_lines = (
        "VS: 2025004",
        "Datum vystaveni: 04.01.2025",
        "Cekam na doplneni",
    )
    ares_fail_lines = (
        "ICO: 45678901",
        "VS: 2025005",
        "Datum vystaveni: 05.01.2025",
        "Cena celkem 500,00 CZK",
        "Zaokrouhleni 500,00",
    )

    embedded_path = env.workspace / "embedded_success.pdf"
    template_path = env.workspace / "template_success.pdf"
    quarantine_path = env.workspace / "quarantine_case.pdf"
    ares_fail_path = env.workspace / "ares_failure_case.pdf"
    duplicate_path = env.workspace / "duplicate_case.pdf"
    ocr_path = env.workspace / "ocr_success.png"

    write_smoke_fixture_pdf(embedded_path, lines=embedded_lines, font_size=18, start_y=700, line_step=24)
    write_smoke_fixture_pdf(template_path, lines=template_lines, font_size=18, start_y=700, line_step=24)
    write_smoke_fixture_pdf(quarantine_path, lines=quarantine_lines, font_size=18, start_y=700, line_step=24)
    write_smoke_fixture_pdf(ares_fail_path, lines=ares_fail_lines, font_size=18, start_y=700, line_step=24)
    write_smoke_fixture_pdf(duplicate_path, lines=embedded_lines, font_size=18, start_y=700, line_step=24)
    write_smoke_fixture_image(ocr_path, lines=ocr_lines)

    try:
        cases: list[ImportCaseArtifact] = []
        cases.append(
            _record_case(
                "embedded_success",
                embedded_path,
                patch_mode="record",
                ares_record=base_ares,
                expected_status="PROCESSED",
            )
        )

        if processor.ocr_engine is not None:
            ocr_ares = AresRecord(
                ico="34567890",
                name="OCR test s.r.o.",
                legal_form="společnost s ručením omezeným",
                is_vat_payer=True,
                address="U OCR 2, Brno, 60200",
                street="U OCR",
                street_number="2",
                city="Brno",
                zip_code="60200",
            )
            cases.append(
                _record_case(
                    "ocr_path",
                    ocr_path,
                    patch_mode="record",
                    ares_record=ocr_ares,
                )
            )
        else:
            cases.append(_record_case("ocr_path", ocr_path, supported=False))

        template_payload = {
            "name": "smoke_template_23456789",
            "enabled": True,
            "match_supplier_ico_norm": "23456789",
            "match_texts_json": json.dumps(["VS: 2025003"], ensure_ascii=False),
            "schema_json": _build_smoke_template_schema_text(),
            "sample_file_name": template_path.name,
            "sample_file_sha256": None,
            "sample_file_relpath": template_path.name,
        }
        if processor.ocr_engine is not None:
            cases.append(
                _record_case(
                    "template_path",
                    template_path,
                    patch_mode="record",
                    ares_record=AresRecord(
                        ico="23456789",
                        name="Template test s.r.o.",
                        legal_form="společnost s ručením omezeným",
                        is_vat_payer=True,
                        address="U Šablony 3, Plzeň, 30100",
                        street="U Šablony",
                        street_number="3",
                        city="Plzeň",
                        zip_code="30100",
                    ),
                    template_payload=template_payload,
                )
            )
        else:
            cases.append(_record_case("template_path", template_path, supported=False))

        cases.append(
            _record_case(
                "quarantine_case",
                quarantine_path,
                expected_status="QUARANTINE",
            )
        )

        cases.append(
            _record_case(
                "duplicate_case",
                duplicate_path,
                patch_mode="record",
                ares_record=base_ares,
                expected_status="DUPLICATE",
            )
        )

        cases.append(
            _record_case(
                "ares_failure_case",
                ares_fail_path,
                patch_mode="error",
                ares_error=ConnectionError("ARES nedostupný"),
                expected_status="QUARANTINE",
            )
        )

        report["cases"] = [asdict(case) for case in cases]
        report["document_ids"] = sorted(
            {
                doc_id
                for case in cases
                for doc_id in case.document_ids
                if doc_id
            }
        )
        required = {"embedded_success", "quarantine_case", "duplicate_case", "ares_failure_case"}
        if processor.ocr_engine is not None:
            required.update({"ocr_path", "template_path"})

        case_map = {case.name: case for case in cases}
        failures: list[str] = []
        for name in required:
            case = case_map.get(name)
            if case is None:
                failures.append(f"Chybí case {name}.")
                continue
            if name == "embedded_success" and case.status != "PROCESSED":
                failures.append(f"embedded_success vrací {case.status!r}.")
            if name == "duplicate_case" and case.status != "DUPLICATE":
                failures.append(f"duplicate_case vrací {case.status!r}.")
            if name == "quarantine_case" and case.status != "QUARANTINE":
                failures.append(f"quarantine_case vrací {case.status!r}.")
            if name == "ares_failure_case" and case.status != "QUARANTINE":
                failures.append(f"ares_failure_case vrací {case.status!r}.")
            if case.detail:
                failures.append(f"{name}: {case.detail}")
        if processor.ocr_engine is not None:
            for name in ("ocr_path", "template_path"):
                case = case_map.get(name)
                if case is None:
                    failures.append(f"Chybí case {name}.")
                elif case.status not in {"PROCESSED", "QUARANTINE"}:
                    failures.append(f"{name} vrací {case.status!r}.")
                if case and case.detail:
                    failures.append(f"{name}: {case.detail}")
        else:
            for name in ("ocr_path", "template_path"):
                case = case_map.get(name)
                if case is None or case.status != "SKIPPED":
                    failures.append(f"{name} měl být SKIPPED při chybějícím OCR runtime.")

        report["summary"] = {
            "case_count": len(cases),
            "processed_count": sum(1 for case in cases if case.status == "PROCESSED"),
            "quarantine_count": sum(1 for case in cases if case.status == "QUARANTINE"),
            "duplicate_count": sum(1 for case in cases if case.status == "DUPLICATE"),
            "skipped_count": sum(1 for case in cases if case.status == "SKIPPED"),
            "document_count": len(report["document_ids"]),
        }
        report["status"] = "PASS" if not failures else "FAIL"
        report["failures"] = failures
        if failures:
            raise RuntimeError("Import smoke hardening selhal: " + "; ".join(failures))
        return report
    finally:
        processor.close()
        w_engine.dispose()
        p_engine.dispose()


def run_gui_audit(*, repo_root: Path | None = None, workspace_name: str = "kajovospend-ui-audit") -> dict[str, Any]:
    env = build_smoke_environment(repo_root, workspace_name=workspace_name)
    report: dict[str, Any] = {
        "workspace": str(env.workspace),
        "config_path": str(env.config_path),
        "fixture_pdf": str(env.fixture_pdf),
        "template_pdf": str(env.template_pdf),
        "screenshots": [],
        "tabs": [],
        "dialogs": [],
        "summary": {},
    }

    app, bootstrap_window = _main_window_instance(env)
    try:
        tab_names = [bootstrap_window.tabs.tabText(idx) for idx in range(bootstrap_window.tabs.count())]
    finally:
        bootstrap_window.close()
        bootstrap_window.deleteLater()
        app.processEvents()

    for idx, tab_text in enumerate(tab_names):
        app, window = _main_window_instance(env)
        try:
            window.tabs.setCurrentIndex(idx)
            window.resize(1680, 1200)
            _settle(app, rounds=6, sleep_s=0.04)
            shot_name = f"tab_{idx:02d}_{_safe_file_stem(tab_text)}.png"
            shot_path = _save_widget_capture(window, env.artifacts_dir / shot_name)
            incidents = audit_widget_geometry(window)
            artifact = CaptureArtifact(
                name=tab_text,
                image_path=str(shot_path),
                widget_count=len(list(_iter_visible_widgets(window))),
                incidents=incidents,
            )
            report["tabs"].append(asdict(artifact))
            report["screenshots"].append(str(shot_path))
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()

    app, parent_window = _main_window_instance(env)
    try:
        for name, dialog in _dialog_instances(env, parent_window):
            dialog.show()
            _settle(app, rounds=6, sleep_s=0.04)
            shot_path = _save_widget_capture(dialog, env.artifacts_dir / f"{name}.png")
            incidents = audit_widget_geometry(dialog)
            artifact = CaptureArtifact(
                name=name,
                image_path=str(shot_path),
                widget_count=len(list(_iter_visible_widgets(dialog))),
                incidents=incidents,
            )
            report["dialogs"].append(asdict(artifact))
            report["screenshots"].append(str(shot_path))
            dialog.close()
            dialog.deleteLater()
    finally:
        parent_window.close()
        parent_window.deleteLater()
        app.processEvents()

    report["summary"] = {
        "tab_count": len(report["tabs"]),
        "dialog_count": len(report["dialogs"]),
        "incident_count": sum(len(item["incidents"]) for item in report["tabs"]) + sum(
            len(item["incidents"]) for item in report["dialogs"]
        ),
    }
    return report


def run_import_smoke(*, repo_root: Path | None = None, workspace_name: str = "kajovospend-import-smoke") -> dict[str, Any]:
    env = build_smoke_environment(repo_root, workspace_name=workspace_name)
    ensure_repo_on_syspath()
    from unittest.mock import patch

    from kajovospend.db.migrate import init_production_db, init_working_db
    from kajovospend.db.production_session import create_production_engine
    from kajovospend.db.session import make_session_factory
    from kajovospend.db.working_session import create_working_engine
    from kajovospend.integrations.ares import AresRecord
    from kajovospend.service.processor import Processor
    from kajovospend.utils.paths import resolve_app_paths

    cfg = load_smoke_config(env.config_path)
    paths = resolve_app_paths(
        cfg["app"].get("data_dir"),
        cfg["app"].get("db_path"),
        cfg["app"].get("log_dir"),
        cfg.get("ocr", {}).get("models_dir"),
        working_db=cfg["app"].get("working_db_path"),
        production_db=cfg["app"].get("production_db_path"),
    )
    w_engine = create_working_engine(str(paths.working_db_path))
    p_engine = create_production_engine(str(paths.production_db_path))
    init_working_db(w_engine)
    init_production_db(p_engine)
    sf = make_session_factory(w_engine)
    sf_prod = make_session_factory(p_engine)
    log_path = env.log_dir / "import_smoke.log"
    import logging

    log = logging.getLogger("kajovospend.import_smoke")
    log.setLevel(logging.DEBUG)
    processor = Processor(cfg, paths, log, sf, sf_prod)
    result: dict[str, Any] = {
        "workspace": str(env.workspace),
        "config_path": str(env.config_path),
        "fixture_pdf": str(env.fixture_pdf),
        "processed_pdf": str(env.input_dir / "smoke_invoice.pdf"),
        "log_path": str(log_path),
    }

    shutil.copy2(env.fixture_pdf, env.input_dir / "smoke_invoice.pdf")
    try:
        with patch(
            "kajovospend.service.processor.fetch_by_ico",
            return_value=AresRecord(
                ico="12345678",
                name="ACME s.r.o.",
                legal_form="společnost s ručením omezeným",
                is_vat_payer=True,
                address="U Testu 1, Praha 1, 11000",
                street="U Testu",
                street_number="1",
                city="Praha",
                zip_code="11000",
            ),
        ):
            with sf() as session:
                proc_result = processor.process_path(session, env.input_dir / "smoke_invoice.pdf")
                session.commit()

        result["processor_result"] = proc_result
        status = str(proc_result.get("status") or "")
        result["status"] = status
        result["document_ids"] = list(proc_result.get("document_ids") or [])
        if status != "PROCESSED":
            raise RuntimeError(f"Import smoke neskončil v PROCESSED, ale v {status!r}.")
        if not result["document_ids"]:
            raise RuntimeError("Import smoke nevytvořil žádný dokument.")
    finally:
        processor.close()
        w_engine.dispose()
        p_engine.dispose()
    return result


def write_json_report(path: Path, data: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def _run_gui_audit_new(*, repo_root: Path | None = None, workspace_name: str = "kajovospend-ui-audit") -> dict[str, Any]:
    env = build_smoke_environment(repo_root, workspace_name=workspace_name)
    import_report = _run_import_suite(env)

    report: dict[str, Any] = {
        "workspace": str(env.workspace),
        "config_path": str(env.config_path),
        "fixture_pdf": str(env.fixture_pdf),
        "template_pdf": str(env.template_pdf),
        "import_smoke": import_report,
        "screenshots": list(import_report.get("screenshots", [])),
        "tabs": [],
        "dialogs": [],
        "populated_state": {},
        "summary": {},
    }

    app, bootstrap_window = _main_window_instance(env)
    try:
        tab_names = [bootstrap_window.tabs.tabText(idx) for idx in range(bootstrap_window.tabs.count())]
    finally:
        bootstrap_window.close()
        bootstrap_window.deleteLater()
        app.processEvents()

    for idx, tab_text in enumerate(tab_names):
        app, window = _main_window_instance(env)
        try:
            window.tabs.setCurrentIndex(idx)
            window.resize(1680, 1200)
            _settle(app, rounds=6, sleep_s=0.04)
            shot_name = f"tab_{idx:02d}_{_safe_file_stem(tab_text)}.png"
            shot_path = _save_widget_capture(window, env.artifacts_dir / shot_name)
            incidents = audit_widget_geometry(window)
            artifact = CaptureArtifact(
                name=tab_text,
                image_path=str(shot_path),
                widget_count=len(list(_iter_visible_widgets(window))),
                incidents=incidents,
            )
            report["tabs"].append(asdict(artifact))
            report["screenshots"].append(str(shot_path))
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()

    app, parent_window = _main_window_instance(env)
    try:
        for name, dialog in _dialog_instances(env, parent_window):
            dialog.show()
            _settle(app, rounds=6, sleep_s=0.04)
            shot_path = _save_widget_capture(dialog, env.artifacts_dir / f"{name}.png")
            incidents = audit_widget_geometry(dialog)
            artifact = CaptureArtifact(
                name=name,
                image_path=str(shot_path),
                widget_count=len(list(_iter_visible_widgets(dialog))),
                incidents=incidents,
            )
            report["dialogs"].append(asdict(artifact))
            report["screenshots"].append(str(shot_path))
            dialog.close()
            dialog.deleteLater()
    finally:
        parent_window.close()
        parent_window.deleteLater()
        app.processEvents()

    app, truth_window = _main_window_instance(env)
    try:
        docs_evidence, docs_findings = _collect_doc_truth(truth_window, app)
        docs_shot = _save_widget_capture(truth_window, env.artifacts_dir / "populated_docs.png")
        report["screenshots"].append(str(docs_shot))
        items_evidence, items_findings = _collect_item_truth(truth_window, app)
        items_shot = _save_widget_capture(truth_window, env.artifacts_dir / "populated_items.png")
        report["screenshots"].append(str(items_shot))
        report["populated_state"] = {
            "docs": asdict(docs_evidence),
            "items": asdict(items_evidence),
            "docs_findings": [asdict(item) for item in docs_findings],
            "items_findings": [asdict(item) for item in items_findings],
            "screenshots": [str(docs_shot), str(items_shot)],
        }
    finally:
        truth_window.close()
        truth_window.deleteLater()
        app.processEvents()

    report["summary"] = {
        "tab_count": len(report["tabs"]),
        "dialog_count": len(report["dialogs"]),
        "incident_count": sum(len(item["incidents"]) for item in report["tabs"]) + sum(
            len(item["incidents"]) for item in report["dialogs"]
        ),
        "truth_issue_count": len(report["populated_state"].get("docs_findings", []))
        + len(report["populated_state"].get("items_findings", [])),
        "import_case_count": int(import_report.get("summary", {}).get("case_count", 0) or 0),
    }
    return report


def _run_import_smoke_new(*, repo_root: Path | None = None, workspace_name: str = "kajovospend-import-smoke") -> dict[str, Any]:
    env = build_smoke_environment(repo_root, workspace_name=workspace_name)
    report = _run_import_suite(env)
    report["processed_pdf"] = str(env.input_dir / "embedded_success.pdf")
    return report


run_gui_audit = _run_gui_audit_new
run_import_smoke = _run_import_smoke_new
