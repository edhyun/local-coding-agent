"""Tier 1 tests for chief.py's parse_verdict (see /plan-eng-review, 2026-08-26).

parse_verdict is the safety-relevant boundary between "auto-approved" and
"escalate to human" - a regression here could make a bad review silently
read as an approval, so it gets dedicated attention beyond the rest of
chief.py (which is Tier 3 - requires a live model, see TODOS.md).
"""

import unittest

import chief


def text_event(text: str) -> dict:
    return {"type": "text", "part": {"text": text}}


class TestParseVerdict(unittest.TestCase):
    def test_clean_approve(self):
        events = [text_event("Looks good.\nVERDICT: APPROVE")]
        result = chief.parse_verdict(events)
        self.assertEqual(result["verdict"], "APPROVE")

    def test_clean_request_changes_with_reason(self):
        events = [text_event("VERDICT: REQUEST_CHANGES: missing null check on line 42")]
        result = chief.parse_verdict(events)
        self.assertEqual(result["verdict"], "REQUEST_CHANGES")
        self.assertEqual(result["reason"], "missing null check on line 42")

    def test_no_verdict_line_is_unclear(self):
        events = [text_event("I looked at the diff, seems fine to me.")]
        result = chief.parse_verdict(events)
        self.assertEqual(result["verdict"], "UNCLEAR")

    def test_empty_events_is_unclear(self):
        result = chief.parse_verdict([])
        self.assertEqual(result["verdict"], "UNCLEAR")

    def test_verdict_line_buried_with_trailing_chatter_after_it(self):
        """A VERDICT line is not required to be the literal last line - the
        parser scans backward and must still find it even when the model
        kept talking after emitting it."""
        events = [text_event(
            "Reviewing the diff now.\n"
            "VERDICT: APPROVE\n"
            "Let me know if you want me to look at anything else!"
        )]
        result = chief.parse_verdict(events)
        self.assertEqual(result["verdict"], "APPROVE")

    def test_multiple_verdict_lines_last_one_wins(self):
        events = [text_event(
            "VERDICT: REQUEST_CHANGES: first pass, found an issue\n"
            "Actually, looking again, this is fine.\n"
            "VERDICT: APPROVE"
        )]
        result = chief.parse_verdict(events)
        self.assertEqual(result["verdict"], "APPROVE")

    def test_lowercase_verdict_keyword_still_matches(self):
        events = [text_event("verdict: approve")]
        result = chief.parse_verdict(events)
        self.assertEqual(result["verdict"], "APPROVE")

    def test_non_text_events_are_ignored(self):
        events = [
            {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed"}}},
            text_event("VERDICT: APPROVE"),
        ]
        result = chief.parse_verdict(events)
        self.assertEqual(result["verdict"], "APPROVE")


if __name__ == "__main__":
    unittest.main()
