"""Run the golden question set against a RUNNING API with a real LLM key configured.

Usage:  python scripts/run_golden.py [base_url]     (default http://localhost:8000)
Password comes from SEED_PASSWORD (default demo-password). Exit code 1 if any case fails.
Each case uses a fresh conversation so cases do not influence each other.
"""
import json
import os
import sys
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PASSWORD = os.environ.get("SEED_PASSWORD", "demo-password")
CASES = json.loads((Path(__file__).parent / "golden_set.json").read_text(encoding="utf-8"))


def check(case: dict, body: dict) -> list[str]:
    problems: list[str] = []
    text = body["content"].lower()
    titles = [c["title"] for c in body["citations"]]
    if body.get("intent") != case["intent"]:
        problems.append(f"intent {body.get('intent')!r} != {case['intent']!r}")
    for needle in case.get("contains", []):
        if needle.lower() not in text:
            problems.append(f"answer missing {needle!r}")
    for needle in case.get("not_contains", []):
        if needle.lower() in text:
            problems.append(f"answer should not contain {needle!r}")
    if "cites" in case and case["cites"] not in titles:
        problems.append(f"expected citation {case['cites']!r}, got {titles}")
    if "no_cite_containing" in case and any(case["no_cite_containing"] in t for t in titles):
        problems.append(f"cited forbidden source: {titles}")
    if case.get("refuses") and (body["citations"] or "couldn't find" not in text):
        problems.append("expected a refusal with no citations")
    if "order" in case:
        got = (body.get("order") or {}).get("order_number")
        if got != case["order"]:
            problems.append(f"order {got!r} != {case['order']!r}")
    if "ticket" in case and bool(body.get("ticket")) != case["ticket"]:
        problems.append(f"ticket created = {bool(body.get('ticket'))}, expected {case['ticket']}")
    return problems


def main() -> int:
    tokens: dict[str, str] = {}
    failures = 0
    with httpx.Client(base_url=BASE, timeout=120) as client:
        for i, case in enumerate(CASES, 1):
            email = case["as"]
            if email not in tokens:
                r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
                r.raise_for_status()
                tokens[email] = r.json()["access_token"]
            r = client.post(
                "/chat/messages",
                headers={"Authorization": f"Bearer {tokens[email]}"},
                json={"content": case["q"]},
            )
            if r.status_code != 200:
                problems = [f"HTTP {r.status_code}: {r.text[:120]}"]
            else:
                problems = check(case, r.json())
            failures += bool(problems)
            print(f"{'FAIL' if problems else 'ok  '} {i:2d}. [{email.split('.')[0]}] {case['q']}")
            for p in problems:
                print(f"        - {p}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
