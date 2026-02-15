#!/usr/bin/env python3
import json
import os
import sys
import requests
import argparse
import logging
import re
import time
import urllib.parse


SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
DETAIL_URL_TEMPLATE = "https://api.nal.usda.gov/fdc/v1/food/{fdc_id}"
NOTICE_PREFIX = "FDC_NOTICE:"
REQUEST_TIMEOUT_SECONDS = float(os.getenv("FDC_REQUEST_TIMEOUT_SECONDS", "3"))
SEARCH_RETRIES = int(os.getenv("FDC_SEARCH_RETRIES", "2"))
PAGE_SIZE = max(1, int(os.getenv("FDC_PAGE_SIZE", "50")))
MAX_SEARCH_PAGES = max(1, int(os.getenv("FDC_MAX_SEARCH_PAGES", "1")))
MAX_SEARCH_RESULTS = int(os.getenv("FDC_MAX_SEARCH_RESULTS", "16"))
MAX_DETAIL_LOOKUPS = int(os.getenv("FDC_MAX_DETAIL_LOOKUPS", "2"))
LOGFILE_PATH = os.getenv("FDC_LOGFILE_PATH", "./logs/get_fdc_data.log")


def extract_kcal_per_100g(food):
    nutrients = food.get("foodNutrients") or []
    for nutrient in nutrients:
        unit = (nutrient.get("unitName") or "").upper()
        if unit == "KCAL":
            if "value" in nutrient:
                return nutrient.get("value")
            if "amount" in nutrient:
                return nutrient.get("amount")
    return None


def format_household_text(portion):
    amount = portion.get("amount")
    unit = ((portion.get("measureUnit") or {}).get("name") or "").strip()
    if amount is None or not unit:
        return None
    if float(amount).is_integer():
        amount_text = str(int(amount))
    else:
        amount_text = str(amount)
    return f"{amount_text} {unit}"


def choose_fallback_portion(portions):
    valid = [p for p in portions if p.get("gramWeight")]
    if not valid:
        return None

    non_racc = [p for p in valid if ((p.get("measureUnit") or {}).get("name") or "").upper() != "RACC"]
    if non_racc:
        valid = non_racc

    def sort_key(p):
        seq = p.get("sequenceNumber")
        datapoints = p.get("dataPoints", 0)
        seq_sort = seq if seq is not None else 999999
        return (seq_sort, -datapoints)

    return sorted(valid, key=sort_key)[0]


_UNIT_WORDS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
    "oz", "ounce", "ounces", "fl", "floz", "ml", "l", "liter", "liters", "litre", "litres",
    "g", "gram", "grams", "kg", "lb", "lbs", "pound", "pounds",
    "serving", "servings", "slice", "slices", "piece", "pieces",
    "bowl", "bowls", "plate", "plates", "can", "cans", "bottle", "bottles",
    "packet", "packets",
}
_CONTAINER_WORDS = {"bowl", "bowls", "plate", "plates", "serving", "servings"}


def _is_number_token(token):
    return bool(re.fullmatch(r"\d+(?:\.\d+)?|\d+\s*/\s*\d+", token))


def extract_food_query(text):
    normalized = normalize_search_query(text)
    if not normalized:
        return normalized

    raw_tokens = []
    for token in normalized.split():
        cleaned = token.strip(".,;:()[]{}").lstrip("+")
        if cleaned:
            raw_tokens.append(cleaned)

    tokens = raw_tokens
    while tokens:
        first = tokens[0].lower()
        second = tokens[1].lower() if len(tokens) > 1 else ""
        if _is_number_token(first):
            tokens = tokens[1:]
            continue
        if first in {"a", "an", "the"} and len(tokens) > 1:
            tokens = tokens[1:]
            continue
        if first in {"half", "quarter"} and (second in _UNIT_WORDS or second == "of"):
            tokens = tokens[1:]
            continue
        if first in _CONTAINER_WORDS and second == "of":
            tokens = tokens[2:]
            continue
        if first in _UNIT_WORDS:
            tokens = tokens[1:]
            if tokens and tokens[0].lower() == "of":
                tokens = tokens[1:]
            continue
        if first == "of":
            tokens = tokens[1:]
            continue
        break

    if tokens:
        return " ".join(tokens[:8])
    return normalized


def redact_url(url):
    parsed = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted_pairs = [
        (key, "***REDACTED***") if key.lower() == "api_key" else (key, value)
        for key, value in pairs
    ]
    redacted_query = urllib.parse.urlencode(redacted_pairs, doseq=True)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, redacted_query, parsed.fragment)
    )


def normalize_search_query(query):
    normalized = (query or "").strip()
    if not normalized:
        return normalized

    # Trim wrapping quotes from phrase-style queries.
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1].strip()

    # USDA intermittently fails on slash fractions; normalize them to decimal text.
    def _replace_fraction(match):
        numerator = int(match.group("num"))
        denominator = int(match.group("den"))
        if denominator == 0:
            return match.group(0)
        decimal = numerator / denominator
        if decimal.is_integer():
            return str(int(decimal))
        return f"{decimal:.6g}"

    normalized = re.sub(r"(?P<num>\d+)\s*/\s*(?P<den>\d+)", _replace_fraction, normalized)
    normalized = normalized.replace('"', " ").replace("'", " ")
    normalized = " ".join(normalized.split())
    return normalized


def build_search_query_candidates(query):
    food_query = extract_food_query(query)
    if not food_query:
        return []
    return [food_query]


def emit_notice(logger, message):
    logger.warning("%s %s", NOTICE_PREFIX, message)


def get_json_with_retries(session, url, headers, logger, retries=3, timeout=20, params=None):
    response = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout, params=params)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < retries:
                sleep_s = 0.25 * (2 ** (attempt - 1))
                logger.warning(
                    "GET %s raised %s (attempt %s/%s), retrying in %.2fs",
                    redact_url(url),
                    type(exc).__name__,
                    attempt,
                    retries,
                    sleep_s,
                )
                time.sleep(sleep_s)
                continue
            raise
        if response.ok:
            return response.json()

        retriable = response.status_code in {429, 500, 502, 503, 504}
        if attempt < retries and retriable:
            sleep_s = 0.25 * (2 ** (attempt - 1))
            request_url = redact_url(response.url or url)
            logger.warning(
                "GET %s returned %s (attempt %s/%s), retrying in %.2fs",
                request_url,
                response.status_code,
                attempt,
                retries,
                sleep_s,
            )
            time.sleep(sleep_s)
            continue

        response.raise_for_status()

    # Defensive fallback, though loop should always return/raise.
    response.raise_for_status()


def get_food_details(session, fdc_id, fdc_api_key, headers, logger):
    detail_url = DETAIL_URL_TEMPLATE.format(fdc_id=fdc_id)
    detail_params = {"api_key": fdc_api_key}
    try:
        return get_json_with_retries(
            session,
            detail_url,
            headers,
            logger,
            retries=SEARCH_RETRIES,
            timeout=REQUEST_TIMEOUT_SECONDS,
            params=detail_params,
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            logger.warning("Detail endpoint returned 404 for fdcId=%s; skipping enrichment", fdc_id)
            return {}
        raise


def search_foods(session, food_description, search_category, brand_owner, fdc_api_key, headers, logger):
    query_candidates = build_search_query_candidates(food_description)
    if not query_candidates:
        raise ValueError("food_description is empty")

    last_exc = None
    for idx, candidate in enumerate(query_candidates, start=1):
        search_params = {
            "api_key": fdc_api_key,
            "query": candidate,
            "dataType": "Branded" if brand_owner else search_category,
            "pageSize": PAGE_SIZE,
            "pageNumber": 1,
            "sortBy": "dataType.keyword",
            "sortOrder": "asc",
        }
        if brand_owner:
            search_params["brandOwner"] = brand_owner

        try:
            first_response_json = get_json_with_retries(
                session,
                SEARCH_URL,
                headers,
                logger,
                retries=SEARCH_RETRIES,
                timeout=REQUEST_TIMEOUT_SECONDS,
                params=search_params,
            )
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            error_body = ""
            if exc.response is not None and exc.response.text:
                error_body = " ".join(exc.response.text.strip().split())[:160]
            logger.warning(
                "Search failed for query candidate %r (status=%s, candidate %s/%s)%s",
                candidate,
                status,
                idx,
                len(query_candidates),
                f" body={error_body!r}" if error_body else "",
            )
            if status in {400, 500} and idx < len(query_candidates):
                continue
            raise

        total_pages = int(first_response_json.get("totalPages") or 1)
        total_hits = first_response_json.get("totalHits")
        pages_to_fetch = min(total_pages, MAX_SEARCH_PAGES)
        foods = first_response_json.get("foods", []) or []

        if total_pages > MAX_SEARCH_PAGES:
            emit_notice(
                logger,
                (
                    f"search results truncated by page limit: total_pages={total_pages}, "
                    f"read_pages={pages_to_fetch}, query={candidate!r}"
                ),
            )

        for page in range(2, pages_to_fetch + 1):
            search_params["pageNumber"] = page
            response_json = get_json_with_retries(
                session,
                SEARCH_URL,
                headers,
                logger,
                retries=SEARCH_RETRIES,
                timeout=REQUEST_TIMEOUT_SECONDS,
                params=search_params,
            )
            foods.extend(response_json.get("foods", []) or [])

        if len(foods) > MAX_SEARCH_RESULTS:
            emit_notice(
                logger,
                (
                    f"search results truncated by candidate limit: total_candidates={len(foods)}, "
                    f"returned_candidates={MAX_SEARCH_RESULTS}, query={candidate!r}"
                ),
            )

        if isinstance(total_hits, int) and total_hits > len(foods):
            emit_notice(
                logger,
                (
                    f"not all available matches were read: total_hits={total_hits}, "
                    f"retrieved_candidates={len(foods)}, query={candidate!r}"
                ),
            )

        foods = foods[:MAX_SEARCH_RESULTS]

        if candidate != food_description:
            logger.info("Search succeeded with fallback query candidate %r", candidate)
        return foods

    if last_exc:
        raise last_exc
    return []


def main():
    parser = argparse.ArgumentParser(description='Get calorie data from FDC API')
    parser.add_argument('food_description', type=str, help='Description of the food to search for')
    parser.add_argument('--search-category', type=str, help='Search category for FDC API', choices=["Foundation", "Branded"], required=False, default="Foundation")
    parser.add_argument('--brand-owner', type=str, help='Brand owner to filter by (only for Branded category)', required=False)
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose output')
    args = parser.parse_args()
    food_description = args.food_description.strip()
    search_category = args.search_category
    brand_owner = args.brand_owner.strip() if args.brand_owner else None
    verbose = args.verbose
    debug = os.getenv("DEBUG", "0") != "0"

    os.makedirs(os.path.dirname(LOGFILE_PATH), exist_ok=True)
    logger = logging.getLogger(__name__)
    # logging.basicConfig(level=logging.DEBUG if (debug or verbose) else logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler = logging.FileHandler(LOGFILE_PATH)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.setLevel(logging.DEBUG if (debug or verbose) else logging.INFO)
    if debug or verbose:
        # Prevent urllib3 debug logs from printing full URLs with api_key values.
        logging.getLogger("urllib3").setLevel(logging.INFO)

    logger.info("Starting FDC data retrieval for description=%r, search_category=%r, brand_owner=%r",
                food_description, search_category, brand_owner)
    fdc_api_key = os.getenv("FDC_API_KEY")
    if not fdc_api_key:
        print("FDC_API_KEY environment variable not set")
        return 1
    headers = {'Accept': 'application/json'}

    try:
        session = requests.Session()
        foods = search_foods(
            session,
            food_description,
            search_category,
            brand_owner,
            fdc_api_key,
            headers,
            logger,
        )

        food_details_cache = {}

        calorie_list = []
        for food in foods:
            serving_size = food.get("servingSize")
            serving_size_unit = food.get("servingSizeUnit")
            household_serving_full_text = food.get("householdServingFullText")
            kcal = extract_kcal_per_100g(food)

            missing_serving_meta = (
                serving_size is None
                or serving_size_unit is None
                or household_serving_full_text is None
            )

            if missing_serving_meta and food.get("dataType") == "Foundation":
                fdc_id = food.get("fdcId")
                if fdc_id:
                    details = food_details_cache.get(fdc_id)
                    if details is None:
                        if len(food_details_cache) >= MAX_DETAIL_LOOKUPS:
                            details = {}
                        else:
                            try:
                                details = get_food_details(session, fdc_id, fdc_api_key, headers, logger)
                            except requests.HTTPError as exc:
                                logger.warning("Skipping detail enrichment for fdcId=%s due to error: %s", fdc_id, exc)
                                details = {}
                        food_details_cache[fdc_id] = details

                    portions = details.get("foodPortions") or []
                    fallback_portion = choose_fallback_portion(portions)
                    if fallback_portion:
                        gram_weight = fallback_portion.get("gramWeight")
                        if serving_size is None:
                            serving_size = gram_weight
                        if serving_size_unit is None:
                            serving_size_unit = "g"
                        if household_serving_full_text is None:
                            household_serving_full_text = format_household_text(fallback_portion)
                        if kcal is not None and gram_weight is not None:
                            kcal = round((kcal * gram_weight) / 100, 2)

            calorie_list.append({
                "description": food.get("description"),
                "servingSize": serving_size,
                "servingSizeUnit": serving_size_unit,
                "householdServingFullText": household_serving_full_text,
                "kcal": kcal,
            })
    except Exception as exc:
        logger.error("Error occurred while fetching FDC data: %s", exc, exc_info=True)
        return 1
    print(json.dumps(calorie_list))
    return 0

if __name__ == "__main__":
    sys.exit(main())
