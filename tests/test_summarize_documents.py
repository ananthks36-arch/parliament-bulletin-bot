import unittest

from summarize_documents import build_prompt, source_excerpt


class LocalSummaryTests(unittest.TestCase):
    def test_revised_list_prompt_requires_evidenced_comparison(self):
        job = {
            "house": "Lok Sabha",
            "label": "Revised List of Business",
            "date": "06-08-2026",
        }

        prompt = build_prompt(job, "[Page 1]\nRevised", "[Page 1]\nOriginal")

        self.assertIn("Compare the REVISED document", prompt)
        self.assertIn("ORIGINAL LIST OF BUSINESS", prompt)
        self.assertIn("do not claim it", prompt)
        self.assertIn("supporting PDF page", prompt)

    def test_long_source_keeps_beginning_and_end(self):
        text = "BEGIN" + ("x" * 80000) + "END"

        excerpt = source_excerpt(text)

        self.assertTrue(excerpt.startswith("BEGIN"))
        self.assertTrue(excerpt.endswith("END"))
        self.assertIn("Middle pages omitted", excerpt)


if __name__ == "__main__":
    unittest.main()
