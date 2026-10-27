import tempfile
import unittest
from pathlib import Path

from door_number_detector.web.security import (
    UploadTooLarge,
    is_loopback_host,
    new_upload_path,
    parse_content_length,
    remove_orphaned_uploads,
    upload_suffix,
    valid_csrf_token,
)


class WebSecurityTests(unittest.TestCase):
    def test_content_length_must_be_positive_ascii_integer_within_limit(self):
        self.assertEqual(parse_content_length("12", 20), 12)
        for value in (None, "", "0", "-1", "1.2", "１２"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_content_length(value, 20)
        with self.assertRaises(UploadTooLarge):
            parse_content_length("21", 20)

    def test_upload_uses_generated_name_and_supported_suffix(self):
        upload_dir = Path("data/uploads")
        path = new_upload_path(upload_dir, r"C:\fakepath\doors.XLSX")
        self.assertEqual(path.parent, upload_dir)
        self.assertRegex(path.stem, r"^[0-9a-f]{32}$")
        self.assertEqual(path.suffix, ".xlsx")
        with self.assertRaises(ValueError):
            upload_suffix("payload.py")

    def test_orphan_cleanup_removes_only_generated_spreadsheet_files(self):
        with tempfile.TemporaryDirectory() as directory:
            upload_dir = Path(directory)
            orphan = upload_dir / ("a" * 32 + ".xlsx")
            unrelated = upload_dir / "keep.xlsx"
            orphan.write_bytes(b"orphan")
            unrelated.write_bytes(b"keep")

            self.assertEqual(remove_orphaned_uploads(upload_dir), 1)
            self.assertFalse(orphan.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(remove_orphaned_uploads(upload_dir), 0)

    def test_csrf_token_requires_exact_match(self):
        self.assertTrue(valid_csrf_token("token", "token"))
        self.assertFalse(valid_csrf_token("", "token"))
        self.assertFalse(valid_csrf_token("wrong", "token"))

    def test_only_loopback_addresses_are_allowed(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("192.0.2.1"))


if __name__ == "__main__":
    unittest.main()
