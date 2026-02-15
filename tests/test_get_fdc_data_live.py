import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "get_fdc_data.py"


@unittest.skipUnless(os.environ.get("FDC_API_KEY"), "FDC_API_KEY is not set")
class TestGetFdcDataLive(unittest.TestCase):
    def test_wheat_bread_query_returns_rows(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "+wheat +bread"],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

        self.assertEqual(
            proc.returncode,
            0,
            msg=f"get_fdc_data.py failed\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}",
        )
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, list)
        self.assertGreater(len(payload), 0)

        first = payload[0]
        for field in ["description", "servingSize", "servingSizeUnit", "householdServingFullText", "kcal"]:
            self.assertIn(field, first)


if __name__ == "__main__":
    unittest.main()
