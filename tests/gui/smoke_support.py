from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


def _ensure_offscreen_qt() -> None:
    # Headless Qt musí být nastavený ještě před importem PySide6.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")


_ensure_offscreen_qt()

import yaml
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox, QLineEdit, QLabel, QPlainTextEdit, QPushButton, QSpinBox, QTabBar, QTextEdit, QWidget

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


def _write_minimal_pdf_bytes(lines: Sequence[str]) -> bytes:
    escaped_lines = [str(line or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    y = 750
    text_ops = []
    for line in escaped_lines:
        text_ops.append(f"BT /F1 10 Tf 50 {y} Td ({line}) Tj ET")
        y -= 14
    content = "\n".join(text_ops) or "BT /F1 10 Tf 50 750 Td (KajovoSpend) Tj ET"

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


def write_smoke_fixture_pdf(target: Path, *, lines: Sequence[str] = DEFAULT_FIXTURE_LINES) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_write_minimal_pdf_bytes(lines))
    return target


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


def audit_widget_geometry(root: QWidget) -> list[UiIncident]:
    incidents: list[UiIncident] = []
    visible_widgets = list(_iter_visible_widgets(root))

    for widget in visible_widgets:
        incidents.extend(_text_overflow_incidents(widget))

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
