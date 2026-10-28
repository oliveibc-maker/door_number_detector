import unittest

from door_number_detector.core.parsing import parse_ocr_text


class OcrParsingTests(unittest.TestCase):
    def test_extracts_numbers_and_letters(self):
        self.assertEqual(parse_ocr_text("Door 12A and 4"), ["12A", "4"])

    def test_accepts_single_letter_suffix_after_space_or_hyphen(self):
        for text in ("20 A", "20-A", "20A", "20 a"):
            with self.subTest(text=text):
                self.assertEqual(parse_ocr_text(text), ["20A"])

    def test_does_not_take_a_letter_from_a_following_word(self):
        self.assertEqual(parse_ocr_text("12 AV"), ["12"])
        self.assertEqual(parse_ocr_text("3 RUA"), ["3"])
        self.assertEqual(parse_ocr_text("LOTE 12 A"), ["LOTE12"])

    def test_does_not_truncate_numbers_or_take_unicode_word_letters(self):
        self.assertEqual(parse_ocr_text("12AV"), ["12"])
        self.assertEqual(parse_ocr_text("7RUA"), ["7"])
        self.assertEqual(parse_ocr_text("3 SÃO"), ["3"])

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

    def test_truncated_google_watermark_is_ignored(self):
        for text in ("© 2023 Go", "2023Go"):
            with self.subTest(text=text):
                self.assertEqual(parse_ocr_text(text), [])

    def test_address_labels_that_start_with_go_after_a_year_are_kept(self):
        self.assertEqual(parse_ocr_text("LOTE 2000 GONCALVES"), ["LOTE2000"])
        self.assertEqual(parse_ocr_text("BLOCO 1999 GOUVEIA"), ["BLOCO1999"])

    def test_door_numbers_near_a_year_g_fragment_are_kept(self):
        self.assertEqual(parse_ocr_text("2024G 15"), ["15"])
        self.assertEqual(parse_ocr_text("G12"), ["G12"])

    def test_empty_text_has_no_candidates(self):
        self.assertEqual(parse_ocr_text("no visible number"), [])


if __name__ == "__main__":
    unittest.main()
