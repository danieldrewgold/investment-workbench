#!/usr/bin/env python3
"""Run all test suites."""
import subprocess, sys

suites = [
    ("Schema (12)", "tests/test_canonical_schema.py"),
    ("Golden (7)", "tests/golden/test_golden.py"),
    ("Unit: loaders (3)", "tests/unit/test_loaders.py"),
    ("Unit: consensus (2)", "tests/unit/test_consensus_loader.py"),
    ("Integration (6)", "tests/integration/test_research_pipeline.py"),
    ("End-to-end (14)", "tests/test_end_to_end.py"),
    ("Priorities (9)", "tests/test_priorities.py"),
    ("Schema Selection (9)", "tests/test_schema_selection.py"),
]

total_pass = total_fail = 0
for name, path in suites:
    print(f"\n{'='*60}\n{name}\n{'='*60}")
    result = subprocess.run([sys.executable, path], cwd=str(__import__('pathlib').Path(__file__).parent))
    if result.returncode != 0:
        total_fail += 1
    else:
        total_pass += 1

print(f"\n{'='*60}")
print(f"SUITES: {total_pass} passed, {total_fail} failed out of {len(suites)}")
sys.exit(0 if total_fail == 0 else 1)
