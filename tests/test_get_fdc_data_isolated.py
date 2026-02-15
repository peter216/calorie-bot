import importlib
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

fdc = importlib.import_module("get_fdc_data")


class TestQueryNormalization(unittest.TestCase):
    def test_build_candidates_strips_wrapping_quotes(self):
        candidates = fdc.build_search_query_candidates('"oat milk"')
        self.assertEqual(candidates, ["oat milk"])

    def test_build_candidates_removes_portion_and_units(self):
        candidates = fdc.build_search_query_candidates("1/2 cup of oat milk")
        self.assertEqual(candidates, ["oat milk"])

    def test_redact_url_hides_api_key(self):
        raw_url = "https://example.test/search?api_key=SECRET&query=oat+milk"
        redacted = fdc.redact_url(raw_url)
        self.assertIn("api_key=%2A%2A%2AREDACTED%2A%2A%2A", redacted)
        self.assertNotIn("SECRET", redacted)


class TestSearchFallback(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test_get_fdc_data_isolated")
        self.logger.setLevel(logging.DEBUG)

    @staticmethod
    def _http_error(status_code: int, body: str = "error") -> requests.HTTPError:
        response = requests.Response()
        response.status_code = status_code
        response.url = "https://api.nal.usda.gov/fdc/v1/foods/search?api_key=SECRET&query=test"
        response._content = body.encode("utf-8")
        return requests.HTTPError(f"HTTP {status_code}", response=response)

    def test_search_foods_uses_food_only_query(self):
        session = mock.Mock()
        with mock.patch.object(fdc, "get_json_with_retries") as mocked_get:
            mocked_get.return_value = {"totalPages": 1, "foods": [{"description": "Oat milk"}]}

            foods = fdc.search_foods(
                session=session,
                food_description="1/2 cup of oat milk",
                search_category="Foundation",
                brand_owner=None,
                fdc_api_key="SECRET",
                headers={"Accept": "application/json"},
                logger=self.logger,
            )

        self.assertEqual(len(foods), 1)
        self.assertEqual(foods[0]["description"], "Oat milk")
        self.assertEqual(mocked_get.call_count, 1)
        first_query = mocked_get.call_args_list[0].kwargs["params"]["query"]
        self.assertEqual(first_query, "oat milk")

    def test_search_foods_respects_max_pages_and_result_limit(self):
        session = mock.Mock()
        with mock.patch.object(fdc, "MAX_SEARCH_PAGES", 2):
            with mock.patch.object(fdc, "MAX_SEARCH_RESULTS", 2):
                with mock.patch.object(fdc, "emit_notice") as mocked_notice:
                    with mock.patch.object(fdc, "get_json_with_retries") as mocked_get:
                        mocked_get.side_effect = [
                            {
                                "totalPages": 3,
                                "totalHits": 120,
                                "foods": [{"description": "A"}, {"description": "B"}],
                            },
                            {
                                "foods": [{"description": "C"}, {"description": "D"}],
                            },
                        ]

                        foods = fdc.search_foods(
                            session=session,
                            food_description="oat milk",
                            search_category="Foundation",
                            brand_owner=None,
                            fdc_api_key="SECRET",
                            headers={"Accept": "application/json"},
                            logger=self.logger,
                        )

        self.assertEqual(mocked_get.call_count, 2)
        self.assertEqual([f["description"] for f in foods], ["A", "B"])
        self.assertGreaterEqual(mocked_notice.call_count, 2)


if __name__ == "__main__":
    unittest.main()
