#!/usr/bin/env python3
import argparse
import json
from collections import Counter
from pathlib import Path


def _load(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _bool(x):
    return bool(x) if x is not None else False


def _summ(snap):
    n = len(snap)
    complete = sum(1 for r in snap if _bool(r.get("complete")))
    quarantine = sum(1 for r in snap if (r.get("status") == "QUARANTINE") or _bool(r.get("requires_review")))
    ok_total = sum(1 for r in snap if r.get("total_with_vat") is not None)
    ok_date = sum(1 for r in snap if r.get("issue_date") is not None)
    ok_vendor = sum(1 for r in snap if r.get("supplier_ico"))
    reasons = Counter()
    for r in snap:
        for rr in (r.get("review_reasons") or []):
            if rr:
                reasons[rr] += 1
    return {
        "n": n,
        "complete": complete,
        "quarantine_or_review": quarantine,
        "ok_total": ok_total,
        "ok_date": ok_date,
        "ok_vendor": ok_vendor,
        "reasons": reasons,
    }


def _delta(a, b):
    return b - a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before_json", type=Path)
    ap.add_argument("after_json", type=Path)
    args = ap.parse_args()

    before = _load(args.before_json)
    after = _load(args.after_json)

    sb = _summ(before)
    sa = _summ(after)

    def line(label, k):
        vb = sb[k]
        va = sa[k]
        print(f"{label}: {vb} -> {va} (Δ {_delta(vb, va)})")

    print("=== SUMMARY ===")
    line("Docs", "n")
    line("Complete", "complete")
    line("Quarantine/review", "quarantine_or_review")
    line("Has total", "ok_total")
    line("Has date", "ok_date")
    line("Has vendor", "ok_vendor")
    print()

    print("=== TOP REVIEW REASONS (after) ===")
    for reason, cnt in sa["reasons"].most_common(30):
        print(f"{cnt:>5}  {reason}")
    print()

    print("=== REASON DELTAS (after - before) ===")
    all_keys = set(sb["reasons"].keys()) | set(sa["reasons"].keys())
    deltas = []
    for k in all_keys:
        deltas.append((k, sa["reasons"].get(k, 0) - sb["reasons"].get(k, 0), sb["reasons"].get(k, 0), sa["reasons"].get(k, 0)))
    deltas.sort(key=lambda x: abs(x[1]), reverse=True)
    for k, d, bcnt, acnt in deltas[:40]:
        if d == 0:
            continue
        print(f"{d:>+5}  {bcnt:>5} -> {acnt:>5}  {k}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
