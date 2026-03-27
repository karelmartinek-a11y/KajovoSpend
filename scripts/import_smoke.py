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

from tests.gui.smoke_support import run_import_smoke, write_json_report  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministický import smoke pro KajovoSpend.")
    parser.add_argument("--workspace-name", default="kajovospend-import-smoke", help="Prefix pro workspace.")
    parser.add_argument("--report", default="", help="Volitelná cesta k JSON reportu.")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = run_import_smoke(workspace_name=args.workspace_name)
    if args.report:
        report_path = Path(args.report)
    else:
        report_path = Path(report["workspace"]) / "artifacts" / "import_smoke_report.json"
    write_json_report(report_path, report)
    print(f"Import smoke hotový: {report_path}")
    print(
        f"Status: {report['status']}, case count: {report['summary']['case_count']}, "
        f"dokumenty: {len(report['document_ids'])}"
    )
    for case in report.get("cases", []):
        print(f"- {case['name']}: {case['status']} ({case.get('text_method') or '-'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
