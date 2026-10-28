import unittest

from evaluation.answer_eval import gold_review_summary, source_fingerprints


class AnswerReviewProvenanceTests(unittest.TestCase):
    def test_agent_gold_stays_distinct_from_human_review(self):
        result = gold_review_summary([{"gold_review": "agent_reviewed"}] * 2)
        self.assertEqual(result["status"], "agent_reviewed")
        self.assertEqual(result["counts"], {"agent_reviewed": 2})
        self.assertEqual(result["answer_review_status"], "pending_independent_review")

    def test_mixed_or_missing_gold_provenance_is_not_promoted(self):
        result = gold_review_summary([{"gold_review": "agent_reviewed"}, {},
                                      {"gold_review": "pending_human_review"}])
        self.assertEqual(result["status"], "mixed")
        self.assertEqual(result["counts"], {"agent_reviewed": 1, "pending_human_review": 1, "unspecified": 1})

    def test_source_manifest_captures_shared_parser_and_backend_prompt(self):
        hashes = source_fingerprints()
        self.assertIn("backend/config.py", hashes)
        self.assertIn("evaluation/engine.py", hashes)
        self.assertIn("evaluation/answer_eval.py", hashes)
        self.assertTrue(all(len(value) == 64 for value in hashes.values()))


if __name__ == "__main__":
    unittest.main()
