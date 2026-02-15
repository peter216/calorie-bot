import os
import json
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
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.2")

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
                "fdc_api_query": {"type": "string"},
                "fdc_api_response_code": {"type": "integer"},
                "fdc_api_response": {"type": "object"},
            },
            "required": ["kind", "kcal", "start", "end", "notes"],
        },
    }

    prompt = f"""
You estimate calories from a short description and return JSON matching the provided schema. Use the FoodData Central API, by running the script get_fdc_data.py with the food description as the argument, to find calorie data for food items. For exercises, use your training data to make an estimate. Always include your assumptions and uncertainty in the notes field.

Rules:
- kind=food => kcal is intake (Dietary Energy).
- kind=exercise => kcal is active energy burned (Active Energy).
- If no time given, set start=end=now.
- If duration is given for exercise, set end-start accordingly.
- Put assumptions + uncertainty in notes.
now={now}
text={text}

```bash
# Example script queries and responses:

./get_fdc_data.py '+wheat +bread'
[{"description": "Bread, whole-wheat, commercially prepared", "servingSize": 32.1, "servingSizeUnit": "g", "householdServingFullText": "1 slice", "kcal": 81.53}]

# In this case you would select the option with description "NILLA WAFERS" since it is the best match and calculate based on the number of wafers/cookies if given.

./get_fdc_data.py "+nilla +wafers" --search-category "Branded" --brand-owner "nabisco" | jq -c
[{"description":"NILLA WAFERS","servingSize":30.0,"servingSizeUnit":"g","householdServingFullText":"8 wafers","kcal":467},{"description":"WAFERS","servingSize":30.0,"servingSizeUnit":"GRM","householdServingFullText":"8 wafers","kcal":467},{"description":"MINI WAFERS","servingSize":28.0,"servingSizeUnit":"GRM","householdServingFullText":"1 pack","kcal":464},{"description":"LEMON WAFERS, LEMON","servingSize":30.0,"servingSizeUnit":"g","householdServingFullText":"8 wafers","kcal":467},{"description":"REDUCED FAT WAFERS","servingSize":29.0,"servingSizeUnit":"MG","householdServingFullText":"8 WAFERS","kcal":414}]
```

"""

    r = client.responses.create(
        model=MODEL,
        input=prompt,
        text={"format": {"type": "json_schema", **schema}},
    )

    data = json.loads(r.output_text)

    _append_jsonl(
        {
            "ts": now,
            "request": {"text": text, "timestamp": timestamp},
            "response": data,
        }
    )

    return data
