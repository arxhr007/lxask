import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from feedback_cases import draft_case  # noqa: E402


def test_draft_case_rules_out_the_reported_command():
    case = draft_case({"question": "list only the directories here", "note": "want ls -d */", "model": "m.gguf",
                       "reported": "2026-10-16T01:00:00", "steps": [{"task": "t", "command": "find . -type d"}]})
    assert case["must_not"] == [r"find\ \.\ \-type\ d"] and case["must_cmds"] == []
    assert "want ls -d */" in case["why"]
