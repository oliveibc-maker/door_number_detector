import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from door_number_detector import cli


class CliResumeTests(unittest.TestCase):
    def setUp(self):
        self.data_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_directory.cleanup)
        for target in (
            "door_number_detector.cli.configure_logging",
            "door_number_detector.core.config.load_dotenv",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _environment(self, **values):
        return {"APP_DATA_DIR": self.data_directory.name, **values}

    @patch("door_number_detector.cli.run_excel_batch")
    def test_excel_run_passes_resume_csv(self, run_excel_batch):
        environment = self._environment(
            SOURCE="excel", INPUT_EXCEL="input.xlsx", RESUME_CSV="previous.csv"
        )
        with patch.dict("os.environ", environment, clear=True):
            cli.main()

        self.assertEqual(run_excel_batch.call_args.kwargs["existing_csv"], Path("previous.csv"))

    @patch("door_number_detector.cli.run_sqlserver_batch")
    def test_sqlserver_run_without_resume_starts_fresh(self, run_sqlserver_batch):
        environment = self._environment(SOURCE="sqlserver", FILTER_BY="rua", FILTER_VALUE="1")
        with patch.dict("os.environ", environment, clear=True):
            cli.main()

        self.assertIsNone(run_sqlserver_batch.call_args.kwargs["existing_csv"])


if __name__ == "__main__":
    unittest.main()
