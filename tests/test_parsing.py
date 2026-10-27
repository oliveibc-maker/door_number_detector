import unittest

from door_number_detector.core.parsing import parse_ocr_text


class OcrParsingTests(unittest.TestCase):
    def test_extracts_numbers_and_letters(self):
        self.assertEqual(parse_ocr_text("Door 12A and 4"), ["12A", "4"])

    def test_skips_leading_zero_and_blocklisted_fragments(self):
        self.assertEqual(parse_ocr_text("012 2024G 15"), ["15"])

    def test_keyword_hits_preserve_keyword_format(self):
        self.assertEqual(parse_ocr_text("Bloco 12"), ["BLOCO12"])

    def test_blocklisted_fragment_is_rejected_and_logged(self):
        messages = []
        self.assertEqual(parse_ocr_text("LOTE 2024G", messages.append), [])
        self.assertEqual(messages, ["OCR: blocked fragment '2024G' (blocklist)"])

    def test_google_watermark_lines_yield_no_candidates(self):
        for text in (
            "© 2026 Google",
            "2023Go0gle",
            "0-2023Gocqle",
            "2026 GOOGLE",
            "Go0gle",
            "Google",
        ):
            with self.subTest(text=text):
                messages = []
                self.assertEqual(parse_ocr_text(text, messages.append), [])
                self.assertIn("ignored Google watermark", messages[0])

    def test_door_numbers_near_a_year_g_fragment_are_kept(self):
        self.assertEqual(parse_ocr_text("2024G 15"), ["15"])
        self.assertEqual(parse_ocr_text("G12"), ["G12"])

    def test_empty_text_has_no_candidates(self):
        self.assertEqual(parse_ocr_text("no visible number"), [])


if __name__ == "__main__":
    unittest.main()
