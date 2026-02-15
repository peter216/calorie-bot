import os
import json
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Body
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from openai import OpenAI

app = FastAPI()

# --- config ---
SECRET = os.environ.get("ESTIMATE_SHARED_SECRET", "")
LOG_PATH = os.environ.get("ESTIMATE_LOG_PATH", os.path.expanduser("~/calorie-bot/logs/estimates.jsonl"))
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
FDC_SCRIPT_PATH = os.path.join(APP_DIR, "get_fdc_data.py")
FDC_CACHE_PATH = os.environ.get("FDC_CACHE_PATH", os.path.join(APP_DIR, "fda-data-cache.json"))
FDC_CACHE_TTL_SECONDS = 90 * 24 * 60 * 60
FDC_SUBPROCESS_TIMEOUT_SECONDS = int(os.environ.get("FDC_SUBPROCESS_TIMEOUT_SECONDS", "12"))
FDC_NOTICE_PREFIX = "FDC_NOTICE:"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    # Fail early with a clear error in journald if key is missing
    raise RuntimeError("OPENAI_API_KEY is not set (check /etc/calorie-bot/env and systemd EnvironmentFile).")

client = OpenAI(api_key=OPENAI_API_KEY)

# --- models ---
class EstimateResponse(BaseModel):
    kind: str = Field(..., pattern="^(food|exercise)$")
    kcal: int = Field(..., ge=0, le=5000)
    start: str
    end: str
    notes: str
    data_source: str = Field(..., pattern="^(fdc_script|model_fallback)$")
    backend_errors: list[str] = Field(default_factory=list)

# --- helpers ---
def _coerce_text(payload: Any) -> tuple[str, str | None]:
    """
    Accepts:
      - {"text": "...", "timestamp": "..."}
      - {"": "..."}  (common Shortcut mistake)
      - {"someKey": "..."} (fallback: if exactly one non-empty string value exists)
      - "..." (raw string body)
    Returns (text, timestamp)
    """
    timestamp = None

    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            raise ValueError("Empty request body string")
        return s, None

    if isinstance(payload, dict):
        if isinstance(payload.get("timestamp"), str) and payload["timestamp"].strip():
            timestamp = payload["timestamp"].strip()

        if isinstance(payload.get("text"), str) and payload["text"].strip():
            return payload["text"].strip(), timestamp

        if "" in payload and isinstance(payload[""], str) and payload[""].strip():
            return payload[""].strip(), timestamp

        # fallback: single string value in dict
        str_vals = [v.strip() for v in payload.values() if isinstance(v, str) and v.strip()]
        if len(str_vals) == 1:
            return str_vals[0], timestamp

    raise ValueError(f"Could not find 'text' in payload: {payload!r}")

def _append_jsonl(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # Don't fail the request; log to journald
        print(f"LOGGING_FAILED: {e} LOG_PATH={LOG_PATH}", flush=True)


def _cache_key(query: str, search_category: str, brand_owner: str | None) -> str:
    return json.dumps(
        {
            "query": query,
            "search_category": search_category,
            "brand_owner": brand_owner or "",
        },
        sort_keys=True,
    )


def _load_fdc_cache() -> dict:
    if not os.path.exists(FDC_CACHE_PATH):
        return {"version": 1, "entries": {}}
    try:
        with open(FDC_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            return {"version": 1, "entries": {}}
        if not isinstance(cache.get("entries"), dict):
            cache["entries"] = {}
        if "version" not in cache:
            cache["version"] = 1
        return cache
    except Exception:
        return {"version": 1, "entries": {}}


def _save_fdc_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(FDC_CACHE_PATH), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="fdc-cache-", suffix=".json", dir=os.path.dirname(FDC_CACHE_PATH))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp_path, FDC_CACHE_PATH)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _prune_expired_entries(cache: dict, now_ts: int) -> bool:
    changed = False
    threshold = now_ts - FDC_CACHE_TTL_SECONDS
    entries = cache.get("entries", {})
    keys_to_delete = []
    for key, entry in entries.items():
        created_ts = entry.get("created_ts")
        if not isinstance(created_ts, (int, float)) or created_ts < threshold:
            keys_to_delete.append(key)
    for key in keys_to_delete:
        del entries[key]
        changed = True
    return changed


def _unique_errors(errors: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for err in errors:
        if err not in seen:
            output.append(err)
            seen.add(err)
    return output


def _extract_fdc_notices(stderr_text: str) -> list[str]:
    notices: list[str] = []
    if not stderr_text:
        return notices
    for raw_line in stderr_text.splitlines():
        marker = raw_line.find(FDC_NOTICE_PREFIX)
        if marker < 0:
            continue
        message = raw_line[marker + len(FDC_NOTICE_PREFIX):].strip()
        if message:
            notices.append(message)
    return _unique_errors(notices)


def _stderr_without_fdc_notices(stderr_text: str) -> str:
    if not stderr_text:
        return ""
    lines = []
    for line in stderr_text.splitlines():
        if FDC_NOTICE_PREFIX not in line:
            lines.append(line)
    return "\n".join(lines).strip()


def _append_notices_to_notes(notes: str, notices: list[str]) -> str:
    if not notices:
        return notes
    suffix = "Backend warnings: " + "; ".join(notices)
    base = (notes or "").strip()
    if not base:
        return suffix
    if suffix in base:
        return base
    return f"{base} {suffix}"


_UNIT_WORDS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
    "oz", "ounce", "ounces", "fl", "floz", "ml", "l", "liter", "liters", "litre", "litres",
    "g", "gram", "grams", "kg", "lb", "lbs", "pound", "pounds",
    "serving", "servings", "slice", "slices", "piece", "pieces",
    "bowl", "bowls", "plate", "plates", "can", "cans", "bottle", "bottles",
    "packet", "packets",
}
_CONTAINER_WORDS = {"bowl", "bowls", "plate", "plates", "serving", "servings"}


def _is_number_token(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?|\d+\s*/\s*\d+", token))


def _extract_food_tokens(text: str) -> list[str]:
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return []

    raw_tokens = []
    for token in normalized.replace('"', " ").replace("'", " ").split():
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

    return tokens


def _sanitize_fdc_query(text: str) -> str:
    tokens = _extract_food_tokens(text)
    if tokens:
        return " ".join(tokens[:8])
    fallback = " ".join((text or "").replace('"', " ").replace("'", " ").split()).strip()
    return fallback


def _default_fdc_query(text: str) -> str:
    query = _sanitize_fdc_query(text)
    return query if query else text


def _plan_fdc_query(text: str, now: str) -> tuple[dict, list[str]]:
    backend_errors: list[str] = []
    schema = {
        "name": "fdc_query_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "search_category": {"type": "string", "enum": ["Foundation", "Branded"]},
                "brand_owner": {"type": ["string", "null"]},
            },
            "required": ["query", "search_category", "brand_owner"],
        },
    }

    planning_prompt = f"""
You produce a backend query plan for FoodData Central script calls.
Return JSON matching schema exactly.

Guidance:
- Use exactly one food item for each query plan.
- Remove portions/amounts/units from the query (e.g., "1/2 cup of oat milk" -> "oat milk").
- Decide whether to use Foundation or Branded search.
- If query appears brand-specific (unusual non-food token like brand name), choose Branded.
- Return plain text food terms only. Do not use quotes or plus-prefixed tokens.
- Keep query concise and focused on food terms.
- For exercise-like input, still produce the best food-style query from text.

now={now}
text={text}
"""

    try:
        r = client.responses.create(
            model=MODEL,
            input=planning_prompt,
            text={"format": {"type": "json_schema", **schema}},
        )
        plan = json.loads(r.output_text)
        query = _sanitize_fdc_query((plan.get("query") or "").strip())
        search_category = plan.get("search_category")
        brand_owner = plan.get("brand_owner")
        if not query:
            raise ValueError("query is empty")
        if search_category not in {"Foundation", "Branded"}:
            raise ValueError("search_category is invalid")
        if brand_owner is not None and not isinstance(brand_owner, str):
            raise ValueError("brand_owner must be string or null")
        return {
            "query": query,
            "search_category": search_category,
            "brand_owner": brand_owner.strip() if isinstance(brand_owner, str) else None,
        }, backend_errors
    except Exception as exc:
        backend_errors.append(f"query_planning_failed: {exc}")
        return {
            "query": _default_fdc_query(text),
            "search_category": "Foundation",
            "brand_owner": None,
        }, backend_errors


def _run_fdc_lookup(text: str, now: str) -> dict:
    plan, backend_errors = _plan_fdc_query(text, now)
    query = plan["query"]
    search_category = plan["search_category"]
    brand_owner = plan["brand_owner"]

    cmd = [sys.executable, FDC_SCRIPT_PATH, query, "--search-category", search_category]
    if brand_owner:
        cmd.extend(["--brand-owner", brand_owner])
    cmd_str = shlex.join(cmd)

    cache = _load_fdc_cache()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if _prune_expired_entries(cache, now_ts):
        try:
            _save_fdc_cache(cache)
        except Exception as exc:
            backend_errors.append(f"cache_write_failed: {exc}")

    key = _cache_key(query, search_category, brand_owner)
    cache_entry = cache.get("entries", {}).get(key)
    if isinstance(cache_entry, dict):
        cached_stderr = cache_entry.get("stderr", "")
        cached_notices = cache_entry.get("fdc_notices")
        if not isinstance(cached_notices, list):
            cached_notices = _extract_fdc_notices(cached_stderr)
        return {
            "subprocess_call": cmd_str,
            "stdout": cache_entry.get("stdout", ""),
            "stderr": cached_stderr,
            "stderr_for_errors": _stderr_without_fdc_notices(cached_stderr),
            "returncode": cache_entry.get("returncode", 0),
            "data": cache_entry.get("data", []),
            "query_plan": plan,
            "from_cache": True,
            "backend_errors": backend_errors,
            "fdc_notices": cached_notices,
        }

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FDC_SUBPROCESS_TIMEOUT_SECONDS,
            cwd=APP_DIR,
            check=False,
        )
    except Exception as exc:
        backend_errors.append(f"subprocess_failed: {exc}")
        return {
            "subprocess_call": cmd_str,
            "stdout": "",
            "stderr": str(exc),
            "returncode": 1,
            "data": [],
            "query_plan": plan,
            "from_cache": False,
            "backend_errors": backend_errors,
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    fdc_notices = _extract_fdc_notices(stderr)
    stderr_for_errors = _stderr_without_fdc_notices(stderr)
    returncode = proc.returncode
    parsed_data = []

    if returncode != 0:
        backend_errors.append(f"subprocess_returncode_{returncode}")
    else:
        try:
            parsed = json.loads(stdout) if stdout else []
            if isinstance(parsed, list):
                parsed_data = parsed
            else:
                backend_errors.append("subprocess_stdout_not_json_array")
        except Exception as exc:
            backend_errors.append(f"subprocess_stdout_parse_failed: {exc}")

    if returncode == 0:
        cache.setdefault("entries", {})[key] = {
            "created_ts": now_ts,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
            "data": parsed_data,
            "query_plan": plan,
            "fdc_notices": fdc_notices,
        }
        try:
            _save_fdc_cache(cache)
        except Exception as exc:
            backend_errors.append(f"cache_write_failed: {exc}")

    return {
        "subprocess_call": cmd_str,
        "stdout": stdout,
        "stderr": stderr,
        "stderr_for_errors": stderr_for_errors,
        "returncode": returncode,
        "data": parsed_data,
        "query_plan": plan,
        "from_cache": False,
        "backend_errors": backend_errors,
        "fdc_notices": fdc_notices,
    }

# --- routes ---
@app.get("/", response_class=HTMLResponse)
def home():
    return f"""
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Calorie Bot</title></head>
<body style="font-family: -apple-system, system-ui, sans-serif; max-width: 52rem; margin: 2rem auto;">
  <h1>Calorie Bot</h1>
  <p>Endpoints:</p>
  <ul>
    <li><code>GET /health</code></li>
    <li><code>POST /estimate</code> (requires header <code>X-Shared-Secret</code>)</li>
  </ul>
  <p>Logging to: <code>{LOG_PATH}</code></p>
</body>
</html>
"""

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/estimate", response_model=EstimateResponse)
def estimate(payload: Any = Body(...), x_shared_secret: str = Header(default="")):
    if not SECRET:
        raise HTTPException(status_code=500, detail="Server misconfigured: ESTIMATE_SHARED_SECRET not set.")
    if x_shared_secret != SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(timezone.utc).isoformat()

    try:
        text, timestamp = _coerce_text(payload)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    fdc_lookup = _run_fdc_lookup(text, now)
    fdc_candidates = fdc_lookup.get("data") or []
    fdc_notices = list(fdc_lookup.get("fdc_notices") or [])
    backend_errors = list(fdc_lookup.get("backend_errors") or [])
    stderr_for_errors = fdc_lookup.get("stderr_for_errors")
    if stderr_for_errors:
        backend_errors.append(f"subprocess_stderr: {stderr_for_errors}")
    backend_errors = _unique_errors(backend_errors)

    schema = {
        "name": "calorie_estimate",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["food", "exercise"]},
                "kcal": {"type": "integer", "minimum": 0, "maximum": 5000},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "notes": {"type": "string"},
                "data_source": {"type": "string", "enum": ["fdc_script", "model_fallback"]},
                "backend_errors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["kind", "kcal", "start", "end", "notes", "data_source", "backend_errors"],
        },
    }

    prompt = f"""
You estimate calories from a short description and return JSON matching the provided schema.
The backend has already called get_fdc_data.py and provided candidates.
Use those candidates for food estimates when present.
If candidates are empty, fall back to model estimation and explain uncertainty.
Always include assumptions and uncertainty in notes.

Rules:
- kind=food => kcal is intake (Dietary Energy).
- kind=exercise => kcal is active energy burned (Active Energy).
- If no time given, set start=end=now.
- If duration is given for exercise, set end-start accordingly.
- If food candidates exist: choose the best semantic match to the user text.
  Example: for "8 nilla wafers", choose "NILLA WAFERS" over generic "WAFERS" or "LEMON WAFERS".
- Use servingSize / servingSizeUnit / householdServingFullText and kcal to scale amount when quantity is present.
- If no backend food candidates: set data_source=model_fallback.
- If backend food candidates are used: set data_source=fdc_script.
- Include backend_warnings in notes when they are provided.
- Put assumptions + uncertainty in notes.
backend_errors={json.dumps(backend_errors)}
backend_warnings={json.dumps(fdc_notices)}
backend_query_plan={json.dumps(fdc_lookup.get("query_plan"))}
backend_food_candidates={json.dumps(fdc_candidates)}
now={now}
text={text}
"""

    r = client.responses.create(
        model=MODEL,
        input=prompt,
        text={"format": {"type": "json_schema", **schema}},
    )

    data = json.loads(r.output_text)
    uses_fdc = data.get("kind") == "food" and bool(fdc_candidates)
    data["data_source"] = "fdc_script" if uses_fdc else "model_fallback"
    data["notes"] = _append_notices_to_notes(data.get("notes", ""), fdc_notices)
    data["backend_errors"] = backend_errors

    try:
        data["kcal"] = int(round(float(data.get("kcal", 0))))
    except Exception:
        data["kcal"] = 0
    data["kcal"] = max(0, min(5000, data["kcal"]))

    _append_jsonl(
        {
            "ts": now,
            "request": {"text": text, "timestamp": timestamp},
            "response": data,
            "subprocess_call": fdc_lookup.get("subprocess_call"),
            "subprocess_return_data": {
                "stdout": fdc_lookup.get("stdout"),
                "stderr": fdc_lookup.get("stderr"),
                "returncode": fdc_lookup.get("returncode"),
                "from_cache": fdc_lookup.get("from_cache", False),
            },
        }
    )

    return data
