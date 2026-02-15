#!/usr/bin/env python3
import json
import os
import sys
import requests
import argparse
import urllib
import logging
import time


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


def get_json_with_retries(session, url, headers, logger, retries=3, timeout=20):
    response = None
    for attempt in range(1, retries + 1):
        response = session.get(url, headers=headers, timeout=timeout)
        if response.ok:
            return response.json()

        retriable = response.status_code in {404, 429, 500, 502, 503, 504}
        if attempt < retries and retriable:
            sleep_s = 0.25 * (2 ** (attempt - 1))
            logger.warning(
                "GET %s returned %s (attempt %s/%s), retrying in %.2fs",
                url,
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
    detail_url = f"https://api.nal.usda.gov/fdc/v1/food/{fdc_id}?api_key={fdc_api_key}"
    try:
        return get_json_with_retries(session, detail_url, headers, logger)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status != 404:
            raise

        logger.warning(
            "Primary detail endpoint returned 404 for fdcId=%s; trying /foods fallback endpoint",
            fdc_id,
        )
        fallback_url = f"https://api.nal.usda.gov/fdc/v1/foods?api_key={fdc_api_key}"
        payload = {"fdcIds": [fdc_id], "format": "full"}
        fallback_response = session.post(fallback_url, headers=headers, json=payload, timeout=20)
        fallback_response.raise_for_status()
        fallback_json = fallback_response.json()
        if isinstance(fallback_json, list) and fallback_json:
            return fallback_json[0]
        if isinstance(fallback_json, dict) and fallback_json.get("foods"):
            return fallback_json["foods"][0]
        raise requests.HTTPError(
            f"Fallback /foods endpoint returned no data for fdcId={fdc_id}",
            response=fallback_response,
        )


def main():
    parser = argparse.ArgumentParser(description='Get calorie data from FDC API')
    parser.add_argument('food_description', type=str, help='Description of the food to search for')
    parser.add_argument('--search-category', type=str, help='Search category for FDC API', choices=["Foundation", "Branded"], required=False, default="Foundation")
    parser.add_argument('--brand-owner', type=str, help='Brand owner to filter by (only for Branded category)', required=False)
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose output')
    args = parser.parse_args()
    food_description = args.food_description
    search_category = args.search_category
    brand_owner = args.brand_owner
    verbose = args.verbose
    debug = os.getenv("DEBUG", "0") != "0"

    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.DEBUG if (debug or verbose) else logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    food_description = urllib.parse.quote(food_description)
    brand_owner = urllib.parse.quote(brand_owner) if brand_owner else None

    fdc_api_key = os.getenv("FDC_API_KEY")
    if not fdc_api_key:
        print("FDC_API_KEY environment variable not set")
        sys.exit(1)
    pagenum = 1
    headers = {'Accept': 'application/json'}
    if brand_owner:
        rstring = f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={fdc_api_key}&query={food_description}&dataType=Branded&brandOwner={brand_owner}&pageSize=50&pageNumber={pagenum}&sortBy=dataType.keyword&sortOrder=asc"
    else:
        rstring = f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={fdc_api_key}&query={food_description}&dataType={search_category}&pageSize=50&pageNumber={pagenum}&sortBy=dataType.keyword&sortOrder=asc"

    session = requests.Session()

    first_response_json = get_json_with_retries(session, rstring, headers, logger)
    totalPages = first_response_json.get("totalPages", 1)
    foods = first_response_json.get("foods", [])
    for page in range(2, totalPages + 1):
        if brand_owner:
            rstring = f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={fdc_api_key}&query={food_description}&dataType=Branded&brandOwner={brand_owner}&pageSize=50&pageNumber={page}&sortBy=dataType.keyword&sortOrder=asc"
        else:
            rstring = f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={fdc_api_key}&query={food_description}&dataType={search_category}&pageSize=50&pageNumber={page}&sortBy=dataType.keyword&sortOrder=asc"
        response_json = get_json_with_retries(session, rstring, headers, logger)
        foods.extend(response_json.get("foods", []))

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
                    details = get_food_details(session, fdc_id, fdc_api_key, headers, logger)
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
    print(json.dumps(calorie_list))

if __name__ == "__main__":
    sys.exit(main())
