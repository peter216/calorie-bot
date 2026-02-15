# calorie-bot

Calorie estimation API with:
- Backend FoodData Central lookups via `get_fdc_data.py`
- 90-day local cache in `fda-data-cache.json`
- AI estimation/fallback at `POST /estimate`

## Prerequisites

- Python 3.10+
- Installed dependencies for this project (`fastapi`, `openai`, etc.)
- Environment variables:
  - `OPENAI_API_KEY` (required by `app.py`)
  - `ESTIMATE_SHARED_SECRET` (required by `POST /estimate`)
  - `FDC_API_KEY` (required for live FDC script/API tests)

### Load `FDC_API_KEY` from gopass

```bash
source ~/.function/gopass.function
gopassget FDC_API_KEY
echo "$FDC_API_KEY" | wc -c
# expected: 41
```

## Run the API locally

```bash
cd calorie-bot
export OPENAI_API_KEY=...
export ESTIMATE_SHARED_SECRET=...
uvicorn app:app --reload --port 8000
```

Health check:

```bash
curl -s http://127.0.0.1:8000/health | jq
```

## Test Suite Overview

Tests are under `calorie-bot/tests/` and split by concern:

- `tests/test_app_isolated.py`
  - Isolated backend + endpoint tests (mocked model/subprocess)
  - Verifies:
    - cache hit avoids subprocess
    - subprocess output parsing and cache persistence
    - endpoint sets `data_source` and `backend_errors`
    - JSONL includes subprocess call/stdout/stderr
- `tests/test_get_fdc_data_live.py`
  - Live script smoke test (`get_fdc_data.py` against USDA API)
  - Skips automatically if `FDC_API_KEY` is missing

## Run Isolated Tests (No external APIs)

```bash
cd calorie-bot
python -m unittest tests.test_app_isolated -v
```

Notes:
- These tests mock OpenAI and script subprocesses.
- They require importable `fastapi` and `openai` packages.
- If those packages are missing, tests auto-skip with a clear reason.

## Run Live Script Test (`get_fdc_data.py`)

```bash
cd calorie-bot
source ~/.function/gopass.function
gopassget FDC_API_KEY
python -m unittest tests.test_get_fdc_data_live -v
```

## Run Full Local Endpoint Smoke Test (Real model + real script)

Start server:

```bash
cd calorie-bot
source ~/.function/gopass.function
gopassget FDC_API_KEY
export OPENAI_API_KEY=...
export ESTIMATE_SHARED_SECRET=...
uvicorn app:app --port 8000
```

In another shell:

```bash
curl -s \
  -H "Content-Type: application/json" \
  -H "X-Shared-Secret: $ESTIMATE_SHARED_SECRET" \
  -d '{"text":"1 slice wheat bread"}' \
  http://127.0.0.1:8000/estimate | jq
```

Expected:
- Response contains `data_source` and `backend_errors`
- `data_source` is `fdc_script` when food candidates are returned
- `data_source` is `model_fallback` when candidates are empty/fail

## Debug Artifacts

- FDC cache file: `calorie-bot/fda-data-cache.json`
- Request/response log: default `~/calorie-bot/logs/estimates.jsonl`

Each JSONL entry includes:
- request text/timestamp
- response payload
- `subprocess_call`
- `subprocess_return_data.stdout`
- `subprocess_return_data.stderr`
- `subprocess_return_data.returncode`
- `subprocess_return_data.from_cache`
