from datetime import date
import unittest

from check_bulletins import build_target_dates, extract_documents, parse_document_date


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


if __name__ == "__main__":
    unittest.main()
