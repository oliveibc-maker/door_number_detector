import unittest

from core.parsing import parse_ocr_text


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

    def test_empty_text_has_no_candidates(self):
        self.assertEqual(parse_ocr_text("no visible number"), [])


if __name__ == "__main__":
    unittest.main()