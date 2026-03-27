from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tests.gui.smoke_support import run_gui_audit, write_json_report  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Headless GUI smoke pro KajovoSpend.")
    parser.add_argument("--workspace-name", default="kajovospend-gui-smoke", help="Prefix pro workspace.")
    parser.add_argument("--report", default="", help="Volitelná cesta k JSON reportu.")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = run_gui_audit(workspace_name=args.workspace_name)
    if args.report:
        report_path = Path(args.report)
    else:
        report_path = Path(report["workspace"]) / "artifacts" / "gui_smoke_report.json"
    write_json_report(report_path, report)
    print(f"GUI smoke hotový: {report_path}")
    print(
        f"Taby: {report['summary']['tab_count']}, dialogy: {report['summary']['dialog_count']}, "
        f"truth issues: {report['summary'].get('truth_issue_count', 0)}, "
        f"import cases: {report['summary'].get('import_case_count', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
