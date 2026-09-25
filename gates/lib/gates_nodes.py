"""Trusted pytest reporter for gate 70 (loaded with `-p gates_nodes` while plugin autoload is off).

It records one JSON line per test report — {nodeid, when, outcome} — into $GATE_IO/nodes.jsonl for the
trusted judge, which re-derives the EXPECTED test functions from the source by AST. It runs inside the
candidate's test process, so code imported by a test could still forge it (the README's Residuals).
"""

import json
import os


def pytest_runtest_logreport(report):
    path = os.path.join(os.environ["GATE_IO"], "nodes.jsonl")
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome}) + "\n")
