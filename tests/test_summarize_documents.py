import unittest
from unittest.mock import patch

from summarize_documents import (
    build_prompt,
    clean_model_output,
    format_summary_for_slack,
    format_summary_blocks,
    generate_validated_summary,
    post_summary,
    outcome_evidence,
    source_excerpt,
    validate_summary,
    validate_outcome_consistency,
    validate_revised_list_completeness,
)


class LocalSummaryTests(unittest.TestCase):
    def test_prompt_requires_importance_ranking(self):
        job = {"house": "Lok Sabha", "label": "Bulletin-I", "date": "06-08-2026"}
        prompt = build_prompt(job, "[Page 1]\nProceedings")
        self.assertIn("strict descending importance", prompt)
        self.assertIn("binding decisions first", prompt)
        self.assertIn("If a bill passed, say the bill", prompt)
        self.assertIn("reporting deadline was adopted", prompt)

    def test_prompt_surfaces_authoritative_outcome_context(self):
        job = {"house": "Lok Sabha", "label": "Bulletin-I", "date": "06-08-2026"}
        source = "Motion regarding Bill — Extension of Time\nThe motion was put to vote and adopted."
        prompt = build_prompt(job, source)
        self.assertIn("VERIFIED OUTCOME EXCERPTS", prompt)
        self.assertIn("motion was put to vote and adopted", prompt)

    def test_outcome_evidence_keeps_nearby_item_context(self):
        source = "Motion regarding Committee Extension\nMoved by Member\nThe motion was adopted."
        evidence = outcome_evidence(source)
        self.assertIn("Committee Extension", evidence)
        self.assertIn("motion was adopted", evidence)

    def test_viksit_bharat_wrong_outcome_is_blocked(self):
        summary = "- Committee motion negatived: Viksit Bharat Shiksha Adhishthan Bill, 2025."
        source = "Viksit Bharat Shiksha Adhishthan Bill, 2025 – Extension of Time\nThe motion was put to vote and adopted."
        with self.assertRaisesRegex(ValueError, "conflicts with source"):
            validate_outcome_consistency(summary, source)

    def test_viksit_bharat_extension_wording_is_allowed(self):
        summary = "- Committee deadline extended: Time for the report on the Viksit Bharat Shiksha Adhishthan Bill, 2025 was extended."
        source = "Viksit Bharat Shiksha Adhishthan Bill, 2025 – Extension of Time\nThe motion was put to vote and adopted."
        validate_outcome_consistency(summary, source)

    def test_hidden_reasoning_is_removed(self):
        raw = "<think>private reasoning</think>\n- Bill passed. (p. 4)"
        self.assertEqual(clean_model_output(raw), "- Bill passed. (p. 4)")

    def test_standalone_summary_is_refused(self):
        with self.assertRaisesRegex(ValueError, "refusing a standalone"):
            post_summary(object(), {"channel": "C123"}, "- Summary. (p. 1)")

    def test_reasoning_monologue_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "1-6 bullets"):
            validate_summary("Hmm, I need to decide what the user wants.")

    def test_short_bulletin_two_summary_is_allowed(self):
        summary = "- Resolution notice: A member gave notice of a proposed resolution.\n- Committee nominations: Three members were nominated."
        self.assertEqual(validate_summary(summary), summary)

    def test_bulletin_two_prompt_distinguishes_notice_from_outcome(self):
        job = {"house": "Rajya Sabha", "label": "Bulletin Part-II", "date": "06-08-2026"}
        prompt = build_prompt(job, "Dr John Brittas: to move the following resolution")
        self.assertIn("'to move' does not mean", prompt)
        self.assertIn("Never add filler", prompt)

    def test_cleaner_discards_prose_and_normalizes_numbered_bullets(self):
        raw = "Planning text\n1. Bill passed. (p. 4)\n2. Motion defeated. (p. 3)\n3. House adjourned. (p. 8)"
        self.assertEqual(
            clean_model_output(raw),
            "- Bill passed. (p. 4)\n- Motion defeated. (p. 3)\n- House adjourned. (p. 8)",
        )

    def test_only_cited_bullets_are_accepted(self):
        summary = "- Bill passed. (p. 4)\n- Motion defeated. (p. 3)\n- Policy announced. (p. 6)\n- House adjourned. (p. 8)"
        self.assertEqual(validate_summary(summary), summary)

    def test_clean_uncited_bullets_are_allowed(self):
        summary = "- Bill passed.\n- Motion defeated.\n- Policy announced.\n- House adjourned."
        self.assertEqual(validate_summary(summary), summary)

    def test_slack_format_bolds_leads_and_spaces_bullets(self):
        summary = "- Bill passed: Tax law amended. (p. 4)\n- Motion defeated: Ordinance remains. (p. 3)"
        rendered = format_summary_for_slack(summary)
        self.assertIn("Summary — most important first", rendered)
        self.assertIn("• Bill passed: Tax law amended. (p. 4)", rendered)
        self.assertIn("\n\n• Motion defeated:", rendered)
        self.assertNotIn("Why it matters", rendered)
        self.assertNotIn("*", rendered)

    def test_slack_format_does_not_double_bold_existing_lead(self):
        rendered = format_summary_for_slack("- *Bill passed:* Tax law amended.")
        self.assertIn("• Bill passed: Tax law amended.", rendered)
        self.assertNotIn("*", rendered)

    def test_rich_text_blocks_bold_lead_without_asterisks(self):
        blocks = format_summary_blocks("- Bill passed: Tax law amended.")
        lead = blocks[0]["elements"][1]["elements"][0]["elements"][0]
        self.assertEqual(lead["text"], "Bill passed:")
        self.assertEqual(lead["style"], {"bold": True})
        self.assertNotIn("*", str(blocks))

    def test_context_is_removed_from_model_output(self):
        raw = "- Bill passed.\n- Motion defeated.\n- Policy announced.\nContext: Extra interpretation."
        self.assertNotIn("Context:", clean_model_output(raw))

    def test_context_bullet_is_removed_from_model_output(self):
        raw = "- Bill passed.\n- Motion defeated.\n- Policy announced.\n- Context: Extra interpretation."
        self.assertNotIn("Context:", clean_model_output(raw))

    def test_context_bullet_is_removed_from_rich_text(self):
        blocks = format_summary_blocks(
            "- Bill passed: Tax law amended.\n- Context: Generic explanation."
        )
        self.assertNotIn("Context", str(blocks))

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
        self.assertIn("every major section added", prompt)
        self.assertIn("remain unchanged", prompt)

    def test_expanded_revised_list_requires_complete_section_coverage(self):
        original = "PRIVATE MEMBERS’ LEGISLATIVE BUSINESS\n" + ("Bill item. " * 100)
        revised = (
            "REPORTS OF THE DEPARTMENT RELATED PARLIAMENTARY COMMITTEE\n"
            "STATEMENTS BY MINISTERS\nMOTION FOR ELECTION\n"
            "PRIVATE MEMBERS’ LEGISLATIVE BUSINESS\n" + ("Agenda item. " * 300)
        )
        incomplete = (
            "- Schedule expanded: Morning business was added. (p. 1)\n"
            "- Committee reports: Several reports were listed. (p. 2)\n"
            "- Ministerial statements: Ministers will make statements. (p. 5)\n"
            "- Private members' business: The afternoon schedule remains unchanged. (p. 8)"
        )

        with self.assertRaisesRegex(ValueError, "election motions"):
            validate_revised_list_completeness(incomplete, revised, original)

    def test_complete_revised_list_summary_is_allowed(self):
        original = "PRIVATE MEMBERS’ LEGISLATIVE BUSINESS\n" + ("Bill item. " * 100)
        revised = (
            "REPORTS OF THE DEPARTMENT RELATED PARLIAMENTARY COMMITTEE\n"
            "STATEMENTS BY MINISTERS\nMOTION FOR ELECTION\n"
            "PRIVATE MEMBERS’ LEGISLATIVE BUSINESS\n" + ("Agenda item. " * 300)
        )
        summary = (
            "- Schedule expanded: Fifteen morning items were added. (p. 1)\n"
            "- Committee reports: Major reports were scheduled. (p. 2)\n"
            "- Ministerial statements: Implementation statements were listed. (p. 5)\n"
            "- Election motions: Three board elections were scheduled. (p. 6)\n"
            "- Private members' business: The afternoon schedule remains unchanged. (p. 8)"
        )

        validate_revised_list_completeness(summary, revised, original)

    @patch("summarize_documents.generate_summary")
    def test_failed_draft_is_regenerated_with_validation_feedback(self, mock_generate):
        original = "PRIVATE MEMBERS’ LEGISLATIVE BUSINESS\n" + ("Bill item. " * 100)
        revised = (
            "REPORTS OF THE DEPARTMENT RELATED PARLIAMENTARY COMMITTEE\n"
            "STATEMENTS BY MINISTERS\nMOTION FOR ELECTION\n"
            "PRIVATE MEMBERS’ LEGISLATIVE BUSINESS\n" + ("Agenda item. " * 300)
        )
        complete = (
            "- Schedule expanded: Morning business was added. (p. 1)\n"
            "- Committee reports: Reports were scheduled. (p. 2)\n"
            "- Ministerial statements: Statements were listed. (p. 5)\n"
            "- Election motions: Board elections were scheduled. (p. 6)\n"
            "- Private members' business: The afternoon remains unchanged. (p. 8)"
        )
        mock_generate.side_effect = [
            "- Schedule revised: One item changed. (p. 1)",
            complete,
        ]

        result = generate_validated_summary(
            {"house": "Rajya Sabha", "label": "Revised List of Business", "date": "07-08-2026"},
            revised,
            original,
        )

        self.assertEqual(result, complete)
        self.assertEqual(mock_generate.call_count, 2)
        self.assertIn("REJECTED DRAFT FEEDBACK", mock_generate.call_args.args[0])

    def test_long_source_keeps_beginning_and_end(self):
        text = "BEGIN" + ("x" * 80000) + "END"

        excerpt = source_excerpt(text)

        self.assertTrue(excerpt.startswith("BEGIN"))
        self.assertTrue(excerpt.endswith("END"))
        self.assertIn("Middle pages omitted", excerpt)


if __name__ == "__main__":
    unittest.main()
