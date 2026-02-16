import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    TestClient = None

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
OPENAI_AVAILABLE = importlib.util.find_spec("openai") is not None
APP_TEST_DEPS_AVAILABLE = FASTAPI_AVAILABLE and OPENAI_AVAILABLE

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class DummyOpenAIResponse:
    def __init__(self, payload: dict):
        self.output_text = json.dumps(payload)


def import_app_with_test_env(temp_dir: str):
    os.environ["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "test-openai-key")
    os.environ["ESTIMATE_SHARED_SECRET"] = "test-shared-secret"
    os.environ["ESTIMATE_LOG_PATH"] = str(Path(temp_dir) / "estimates.jsonl")
    os.environ["FDC_CACHE_PATH"] = str(Path(temp_dir) / "fda-data-cache.json")

    if "app" in sys.modules:
        del sys.modules["app"]
    return importlib.import_module("app")


@unittest.skipIf(not APP_TEST_DEPS_AVAILABLE, "fastapi/openai dependencies are not available")
class TestFdcLookupIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.appmod = import_app_with_test_env(self.tmp.name)

    def tearDown(self):
        if "app" in sys.modules:
            del sys.modules["app"]
        self.tmp.cleanup()

    def test_cache_hit_skips_subprocess(self):
        query_plan = {
            "query": "+wheat +bread",
            "search_category": "Foundation",
            "brand_owner": None,
        }
        cache_key = self.appmod._cache_key(
            query_plan["query"],
            query_plan["search_category"],
            query_plan["brand_owner"],
        )
        cache_payload = {
            "version": 1,
            "entries": {
                cache_key: {
                    "created_ts": int(datetime.now(timezone.utc).timestamp()),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "stdout": '[{"description":"Bread","kcal":81}]',
                    "stderr": "",
                    "returncode": 0,
                    "data": [{"description": "Bread", "kcal": 81}],
                    "query_plan": query_plan,
                }
            },
        }
        with open(os.environ["FDC_CACHE_PATH"], "w", encoding="utf-8") as f:
            json.dump(cache_payload, f)

        with mock.patch.object(self.appmod, "_plan_fdc_query", return_value=(query_plan, [])):
            with mock.patch.object(self.appmod.subprocess, "run", side_effect=AssertionError("subprocess should not run")):
                result = self.appmod._run_fdc_lookup("wheat bread", datetime.now(timezone.utc).isoformat())

        self.assertTrue(result["from_cache"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["data"][0]["description"], "Bread")

    def test_subprocess_success_parses_and_caches(self):
        query_plan = {
            "query": "+wheat +bread",
            "search_category": "Foundation",
            "brand_owner": None,
        }
        fake_stdout = json.dumps(
            [
                {
                    "description": "Bread, whole-wheat, commercially prepared",
                    "servingSize": 32.1,
                    "servingSizeUnit": "g",
                    "householdServingFullText": "1 slice",
                    "kcal": 81.53,
                }
            ]
        )
        completed = subprocess.CompletedProcess(
            args=["python3", "get_fdc_data.py"],
            returncode=0,
            stdout=fake_stdout,
            stderr="",
        )

        with mock.patch.object(self.appmod, "_plan_fdc_query", return_value=(query_plan, [])):
            with mock.patch.object(self.appmod.subprocess, "run", return_value=completed):
                result = self.appmod._run_fdc_lookup("wheat bread", datetime.now(timezone.utc).isoformat())

        self.assertFalse(result["from_cache"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["description"], "Bread, whole-wheat, commercially prepared")

        with open(os.environ["FDC_CACHE_PATH"], "r", encoding="utf-8") as f:
            cache = json.load(f)

        cache_key = self.appmod._cache_key(
            query_plan["query"],
            query_plan["search_category"],
            query_plan["brand_owner"],
        )
        self.assertIn(cache_key, cache["entries"])
        self.assertEqual(cache["entries"][cache_key]["returncode"], 0)

    def test_default_fdc_query_extracts_food_description(self):
        self.assertEqual(self.appmod._default_fdc_query("1/2 cup of oat milk"), '"oat milk"')
        self.assertEqual(self.appmod._default_fdc_query("bowl of Kellogs raisin bran"), '"Kellogs raisin bran"')
        self.assertEqual(self.appmod._default_fdc_query("raisin bran cereal"), '"raisin bran"')
        self.assertEqual(self.appmod._normalize_planned_query("+raisin +bran +cereal"), "+raisin +bran")

    def test_run_lookup_planning_failure_uses_food_only_query(self):
        completed = subprocess.CompletedProcess(
            args=["python3", "get_fdc_data.py"],
            returncode=0,
            stdout="[]",
            stderr="",
        )

        with mock.patch.object(self.appmod.client.responses, "create", side_effect=RuntimeError("planner down")):
            with mock.patch.object(self.appmod.subprocess, "run", return_value=completed) as mock_run:
                result = self.appmod._run_fdc_lookup("1/2 cup of oat milk", datetime.now(timezone.utc).isoformat())

        self.assertEqual(result["query_plan"]["query"], '"oat milk"')
        self.assertIn("query_planning_failed", " ".join(result["backend_errors"]))
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[2], '"oat milk"')

    def test_extract_notices_from_stderr(self):
        stderr = "\n".join(
            [
                "2026-01-01 00:00:00 - WARNING - FDC_NOTICE: page limit reached",
                "2026-01-01 00:00:01 - WARNING - other warning",
                "FDC_NOTICE: candidate limit reached",
            ]
        )
        notices = self.appmod._extract_fdc_notices(stderr)
        stripped = self.appmod._stderr_without_fdc_notices(stderr)
        self.assertEqual(notices, ["page limit reached", "candidate limit reached"])
        self.assertIn("other warning", stripped)
        self.assertNotIn("FDC_NOTICE:", stripped)


@unittest.skipIf(not APP_TEST_DEPS_AVAILABLE or TestClient is None, "fastapi/openai test dependencies are not available")
class TestEstimateEndpointIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.appmod = import_app_with_test_env(self.tmp.name)
        self.client = TestClient(self.appmod.app)

    def tearDown(self):
        if "app" in sys.modules:
            del sys.modules["app"]
        self.tmp.cleanup()

    def test_estimate_uses_fdc_script_source_when_candidates_exist(self):
        mock_lookup = {
            "subprocess_call": "python get_fdc_data.py +wheat +bread --search-category Foundation",
            "stdout": '[{"description":"Bread","kcal":81}]',
            "stderr": "",
            "returncode": 0,
            "data": [{"description": "Bread", "servingSize": 32.1, "servingSizeUnit": "g", "householdServingFullText": "1 slice", "kcal": 81.53}],
            "query_plan": {"query": "+wheat +bread", "search_category": "Foundation", "brand_owner": None},
            "from_cache": False,
            "backend_errors": [],
        }
        model_payload = {
            "kind": "food",
            "kcal": 82,
            "start": "2026-02-15T13:00:00+00:00",
            "end": "2026-02-15T13:00:00+00:00",
            "notes": "Assume one slice.",
            "data_source": "fdc_script",
            "backend_errors": [],
        }

        with mock.patch.object(self.appmod, "_run_fdc_lookup", return_value=mock_lookup):
            with mock.patch.object(
                self.appmod.client.responses,
                "create",
                return_value=DummyOpenAIResponse(model_payload),
            ):
                response = self.client.post(
                    "/estimate",
                    headers={"X-Shared-Secret": "test-shared-secret"},
                    json={"text": "1 slice wheat bread"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data_source"], "fdc_script")
        self.assertEqual(payload["backend_errors"], [])
        self.assertEqual(payload["kcal"], 82)

        log_path = Path(os.environ["ESTIMATE_LOG_PATH"])
        self.assertTrue(log_path.exists())
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(lines), 1)
        latest = json.loads(lines[-1])
        self.assertIn("subprocess_call", latest)
        self.assertIn("subprocess_return_data", latest)
        self.assertIn("stdout", latest["subprocess_return_data"])
        self.assertIn("stderr", latest["subprocess_return_data"])

    def test_estimate_falls_back_when_no_candidates(self):
        mock_lookup = {
            "subprocess_call": "python get_fdc_data.py +unknown --search-category Foundation",
            "stdout": "[]",
            "stderr": "temporary upstream failure",
            "returncode": 1,
            "data": [],
            "query_plan": {"query": "+unknown", "search_category": "Foundation", "brand_owner": None},
            "from_cache": False,
            "backend_errors": ["subprocess_returncode_1"],
        }
        model_payload = {
            "kind": "food",
            "kcal": 233.6,
            "start": "2026-02-15T13:01:00+00:00",
            "end": "2026-02-15T13:01:00+00:00",
            "notes": "No matching FDC rows; heuristic estimate.",
            "data_source": "model_fallback",
            "backend_errors": [],
        }

        with mock.patch.object(self.appmod, "_run_fdc_lookup", return_value=mock_lookup):
            with mock.patch.object(
                self.appmod.client.responses,
                "create",
                return_value=DummyOpenAIResponse(model_payload),
            ):
                response = self.client.post(
                    "/estimate",
                    headers={"X-Shared-Secret": "test-shared-secret"},
                    json={"text": "mystery snack"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data_source"], "model_fallback")
        self.assertEqual(payload["kcal"], 234)  # rounded to int by backend
        self.assertIn("subprocess_returncode_1", payload["backend_errors"])

    def test_estimate_appends_backend_notices_to_notes(self):
        mock_lookup = {
            "subprocess_call": "python get_fdc_data.py oat milk --search-category Foundation",
            "stdout": '[{"description":"Oat milk","kcal":48.3}]',
            "stderr": "2026-01-01 - WARNING - FDC_NOTICE: search results truncated by page limit",
            "stderr_for_errors": "",
            "returncode": 0,
            "data": [{"description": "Oat milk", "servingSize": None, "servingSizeUnit": None, "householdServingFullText": None, "kcal": 48.3}],
            "query_plan": {"query": "oat milk", "search_category": "Foundation", "brand_owner": None},
            "from_cache": False,
            "backend_errors": [],
            "fdc_notices": ["search results truncated by page limit"],
        }
        model_payload = {
            "kind": "food",
            "kcal": 24,
            "start": "2026-02-15T13:02:00+00:00",
            "end": "2026-02-15T13:02:00+00:00",
            "notes": "Used backend candidate.",
            "data_source": "fdc_script",
            "backend_errors": [],
        }

        with mock.patch.object(self.appmod, "_run_fdc_lookup", return_value=mock_lookup):
            with mock.patch.object(
                self.appmod.client.responses,
                "create",
                return_value=DummyOpenAIResponse(model_payload),
            ):
                response = self.client.post(
                    "/estimate",
                    headers={"X-Shared-Secret": "test-shared-secret"},
                    json={"text": "1/2 cup of oat milk"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("Backend warnings:", payload["notes"])
        self.assertIn("page limit", payload["notes"])

    def test_estimate_does_not_treat_info_stderr_as_backend_error_on_success(self):
        mock_lookup = {
            "subprocess_call": "python get_fdc_data.py '\"raisin bran\"' --search-category Foundation",
            "stdout": "[]",
            "stderr": "2026-02-15 18:54:31,277 - INFO - Starting FDC data retrieval for description='\"raisin bran\"'",
            "stderr_for_errors": "2026-02-15 18:54:31,277 - INFO - Starting FDC data retrieval for description='\"raisin bran\"'",
            "returncode": 0,
            "data": [],
            "query_plan": {"query": '"raisin bran"', "search_category": "Foundation", "brand_owner": None},
            "from_cache": False,
            "backend_errors": [],
            "fdc_notices": [],
        }
        model_payload = {
            "kind": "food",
            "kcal": 320,
            "start": "2026-02-15T13:03:00+00:00",
            "end": "2026-02-15T13:03:00+00:00",
            "notes": "Fallback estimate.",
            "data_source": "model_fallback",
            "backend_errors": [],
        }

        with mock.patch.object(self.appmod, "_run_fdc_lookup", return_value=mock_lookup):
            with mock.patch.object(
                self.appmod.client.responses,
                "create",
                return_value=DummyOpenAIResponse(model_payload),
            ):
                response = self.client.post(
                    "/estimate",
                    headers={"X-Shared-Secret": "test-shared-secret"},
                    json={"text": "bowl of raisin bran"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["backend_errors"], [])


if __name__ == "__main__":
    unittest.main()
