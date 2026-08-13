from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fixture_factory import create_synthetic_epub


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BenchmarkScriptTests(unittest.TestCase):
    def test_selection_and_benchmark_harnesses_use_sanitized_copies(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            library = root / "private-library"
            output = root / "selected"
            library.mkdir()
            create_synthetic_epub(library / "private-title-one.epub")
            create_synthetic_epub(library / "private-title-two.epub")

            selection_process = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts/select_benchmark_epubs.py"),
                    "--library",
                    str(library),
                    "--output",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            selection = json.loads(selection_process.stdout)
            self.assertEqual(selection["candidate_count"], 2)
            self.assertEqual(
                {item["label"] for item in selection["copies"]},
                {"normal", "image-heavy"},
            )
            self.assertNotIn("private-title", selection_process.stdout)

            benchmark_process = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts/benchmark_pi.py"),
                    "--normal",
                    str(output / "normal.epub"),
                    "--image-heavy",
                    str(output / "image-heavy.epub"),
                    "--work",
                    str(root / "work"),
                    "--timeout-seconds",
                    "30",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            report = json.loads(benchmark_process.stdout)
            self.assertEqual(len(report["cases"]), 4)
            self.assertEqual(
                {(case["label"], case["profile"]) for case in report["cases"]},
                {
                    ("normal", "x3"),
                    ("normal", "x4"),
                    ("image-heavy", "x3"),
                    ("image-heavy", "x4"),
                },
            )
            for case in report["cases"]:
                self.assertGreater(case["source_bytes"], 0)
                self.assertGreater(case["output_bytes"], 0)
                self.assertGreater(case["peak_rss_mib"], 0)
                self.assertEqual(case["output_bytes"], case["http_output_bytes"])


if __name__ == "__main__":
    unittest.main()
