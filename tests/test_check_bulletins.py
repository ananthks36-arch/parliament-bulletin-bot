from datetime import date
import unittest

from check_bulletins import (
    build_target_dates,
    document_key,
    extract_documents,
    get_upload_message_ts,
    parse_document_date,
    should_summarize,
    text_similarity,
)


class DocumentExtractionTests(unittest.TestCase):
    def test_uses_api_date_and_filters_unwanted_documents(self):
        data = {
            "bulletin1Url": {
                "name": "Bulletin-I",
                "url": "https://example.test/bulletin.pdf",
                "date": "28/07/2026",
            },
            "synopsisUrl": {
                "name": "Synopsis",
                "url": "https://example.test/synopsis.pdf",
                "date": "28/07/2026",
            },
        }

        self.assertEqual(
            extract_documents(data, date(2026, 7, 29)),
            [("Bulletin-I", "https://example.test/bulletin.pdf", date(2026, 7, 28))],
        )

    def test_falls_back_to_queried_sitting_date(self):
        data = {
            "listOfBusinessUrls": [
                {
                    "name": "Revised List of Business",
                    "url": "https://example.test/revised.pdf",
                }
            ]
        }

        self.assertEqual(extract_documents(data, date(2026, 7, 30))[0][2], date(2026, 7, 30))

    def test_accepts_rajya_sabha_timestamp(self):
        self.assertEqual(
            parse_document_date("2026-07-28 00:00:00.0", date(2026, 7, 29)),
            date(2026, 7, 28),
        )

    def test_date_window_includes_previous_sitting_after_midnight(self):
        dates = build_target_dates(date(2026, 7, 29))

        self.assertEqual(dates[0], date(2026, 7, 26))
        self.assertIn(date(2026, 7, 28), dates)
        self.assertEqual(dates[-1], date(2026, 8, 1))

    def test_document_identity_does_not_depend_on_source_url(self):
        self.assertEqual(
            document_key("rs", "Revised List of Business", date(2026, 8, 6)),
            "rs:2026-08-06:revised-list-of-business",
        )

    def test_only_revised_lists_and_bulletins_are_summarized(self):
        self.assertTrue(should_summarize("Revised List of Business"))
        self.assertTrue(should_summarize("Bulletin Part-I"))
        self.assertFalse(should_summarize("List of Business"))
        self.assertFalse(should_summarize("Supplementary List of Business-1"))

    def test_extracts_upload_message_timestamp_for_threaded_summary(self):
        response = {
            "file": {
                "shares": {
                    "public": {"C123": [{"ts": "1234567890.123456"}]}
                }
            }
        }

        self.assertEqual(get_upload_message_ts(response), "1234567890.123456")

    def test_recovers_upload_timestamp_from_channel_history(self):
        class FakeClient:
            def files_info(self, file):
                self.file = file
                return {"file": {"id": file}}

            def conversations_history(self, channel, limit):
                self.channel = channel
                return {
                    "messages": [
                        {"ts": "9876.5432", "files": [{"id": "F123"}]},
                    ]
                }

        client = FakeClient()
        response = {"files": [{"id": "F123"}]}

        self.assertEqual(
            get_upload_message_ts(response, client, "C123"),
            "9876.5432",
        )
        self.assertEqual(client.file, "F123")
        self.assertEqual(client.channel, "C123")

    def test_rehash_with_tiny_text_noise_is_near_identical(self):
        original = " ".join(f"word-{index}" for index in range(3000))
        replacement = original.replace("word-1500", "word-1500 Indian", 1)

        self.assertGreater(text_similarity(original, replacement), 0.99)

    def test_material_revision_is_not_near_identical(self):
        original = " ".join(f"agenda-{index}" for index in range(1000))
        revision = " ".join(f"different-{index}" for index in range(1000))

        self.assertLess(text_similarity(original, revision), 0.99)


if __name__ == "__main__":
    unittest.main()
