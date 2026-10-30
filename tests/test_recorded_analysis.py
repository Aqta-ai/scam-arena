"""The six authored scripts and three ordinary format responses stay parseable."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import arena

class RecordedAnalysisTests(unittest.TestCase):
    def test_nine_synthetic_responses_pass_full_consumer_without_provider(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/analysis-recorded.json").read_text())
        self.assertEqual(len(fixture["cases"]), 9)
        self.assertEqual(sum(c["kind"] == "authored-scam" for c in fixture["cases"]), 6)
        with patch.object(arena.httpx, "post", side_effect=AssertionError("No provider calls in a regression test")):
            for case in fixture["cases"]:
                with self.subTest(name=case["name"]):
                    obj = {key: case["analysis"][key] for key in arena.ANALYSIS_KEYS}
                    checked = arena.validate_analysis_result(obj, case["text"])
                    self.assertEqual(checked, obj)

if __name__ == "__main__":
    unittest.main()
