# BUILDPLAN.md — calorie-bot (Go data-plane + dev endpoints + A/B + MCP learning slice)

Date: 2026-02-19
Assumptions:
- Python FastAPI remains the public entrypoint initially.
- A new Go service (`caldata`) runs on localhost:7071.
- Cache + telemetry start in SQLite (file on disk). Redis is optional later.
- Hosted on OCI Free VM; systemd used for process management.
- Goal: visible value in small steps; no LLM calls on cache hit.

## Goals (v0)
1. Deterministic normalization + caching + external lookup (FDC) in Go.
2. Developer introspection endpoints under `/development/*` with admin token.
3. Deterministic A/B assignment and metrics logging for cache/ranking experiments.
4. Optional MCP server layer that maps cleanly onto deterministic tools (learning exercise).

Non-goals (v0)
- Full multi-user auth (use a single admin token for dev endpoints; public endpoints unchanged).
- Activity calories data source integration (stub interface only).
- Moving the full public API from Python to Go (later milestone).

---

## Architecture (v0)

Client (PWA/CLI/Shortcut)
  -> FastAPI (public)
      -> caldata (Go service on localhost)
          - normalize
          - cache get/put
          - FDC search + rank
          - decision tracing + metrics
      -> (optional) LLM fallback ONLY when required
          - only on cache miss / ambiguous FDC results

Key rule:
- Cache hit => return without calling LLM.

---

## Step 1 — Create Go service skeleton: `caldata`

### 1.1 Repo layout
- `services/caldata/`
  - `cmd/caldata/main.go`
  - `internal/normalize/`
  - `internal/cache/`
  - `internal/fdc/`
  - `internal/rank/`
  - `internal/ab/`
  - `internal/trace/`
  - `internal/httpapi/`
  - `migrations/` (SQLite schema)
  - `README.md`

### 1.2 Endpoints (internal + dev)
Public (called by FastAPI):
- `POST /v1/food/resolve`
  - input: `{ "query": "string", "user_id": "optional", "context": {...} }`
  - output: `{ "canonical": "...", "hit": bool, "source": "cache|fdc|llm", "items":[...], "trace_id": "...", "variant": "A|B" }`

Dev-only (require `X-Admin-Token`):
- `GET  /development/health`
- `POST /development/normalize`
- `POST /development/cache/peek`
- `POST /development/fdc/search`
- `POST /development/rank`
- `GET  /development/stats`
- `GET  /development/recent?limit=50`

### 1.3 SQLite schema (v0)
Tables:
- `cache_food (canonical_key TEXT, token_key TEXT, payload_json TEXT, score REAL, source TEXT, created_at, updated_at)`
- `trace (trace_id TEXT PRIMARY KEY, decision_json TEXT, created_at)`
- `metrics (day TEXT, variant TEXT, hits INT, misses INT, llm_calls INT, p95_ms INT, updated_at)`
- `ab_assign (user_key TEXT PRIMARY KEY, variant TEXT, created_at)`

Notes:
- payload_json stores the resolved structured data you return.
- trace stores a compact decision tree for debugging (keys tried, candidates, rank scores).

---

## Step 2 — Deterministic normalization + cache keys

### 2.1 Normalization pipeline (deterministic)
- Unicode normalize (NFKC)
- Casefold
- Strip punctuation except intra-word digits (e.g., "2%")
- Collapse whitespace
- Apply synonym rules:
  - "oatmilk" <-> "oat milk"
  - configurable table: `synonyms.json` (small, versioned)

Outputs:
- `canonical`: normalized string
- `tokens`: token list
- `token_key`: sorted unique tokens joined by space
- `phrases`: bigrams/trigrams for phrase boosting

### 2.2 Cache lookup strategy
Try in order:
1) exact canonical match
2) exact token_key match
3) fuzzy match (bounded) against known canonical keys:
   - token Jaccard similarity
   - edit distance threshold on canonical (short strings only)
Return:
- best payload + metadata + confidence score
If confidence below threshold: treat as miss and proceed to FDC lookup.

---

## Step 3 — FDC (USDA FoodData Central) adapter + ranking

### 3.1 Adapter
- `fdc_search(query, require_terms, page_size)` returns a list of candidates
- `fdc_get(id)` retrieves details
Cache raw FDC responses (short TTL or store minimal fields).

### 3.2 Ranking rules (deterministic)
Score candidate by:
- AND token coverage (required terms must be present if enabled)
- phrase match boost for bigrams/trigrams (e.g. "raisin bran")
- source/type preference (configure)
- simple field heuristics (brand vs generic)
Return top N with score breakdown in dev endpoints.

---

## Step 4 — Wire FastAPI -> caldata

### 4.1 FastAPI changes (minimal)
- On `/estimate`, call `http://127.0.0.1:7071/v1/food/resolve`
- If response is `hit=true` OR `source in {cache, fdc}` with confidence >= threshold:
  - return without LLM
- Else (miss/ambiguous):
  - call existing LLM flow, then `cache_put` via caldata (new endpoint or reuse resolve response)

### 4.2 Timeout budget
- FastAPI sets short timeouts to caldata (connect + read)
- caldata sets timeouts to FDC
- enforce p95 and p99 targets with integration test

---

## Step 5 — A/B testing (v0)

### 5.1 Assignment
- variant = hash(stable_user_key) % 2
  - stable_user_key = user_id if available else anon cookie (PWA) else IP-hash (last resort)
- store assignment in `ab_assign`

### 5.2 What to test first
A: strict AND tokens required
B: phrase boost + softer token matching

Track:
- cache hit rate
- FDC success rate
- LLM call rate
- latency (p95)
- user correction rate (later)

Expose:
- `GET /development/stats` with breakdown by variant and day

---

## Step 6 — MCP learning slice (after Steps 1–5 are stable)

Objective:
- Let an LLM client (Codex/IDE) call deterministic tools for debugging and experiments.

Approach:
- MCP server (can be in Go or Python) that exposes tools mapping to caldata:
  - `normalize`
  - `cache_get`
  - `fdc_search`
  - `rank`
  - `cache_put`
- For safety: tools require admin token and are restricted to dev mode.

---

## Step 7 — OCI deployment notes

### 7.1 systemd units
- `caldata.service`
- `calorie-bot-api.service` (existing)

### 7.2 Resource protection
- log rotation (journald limits or logrotate)
- bound concurrency for outbound FDC calls
- keep dev endpoints behind admin token and optionally local-only firewall rule

---

## Milestones
M0 (1–2 sessions):
- caldata skeleton + /development/health + /development/normalize
M1:
- SQLite cache + resolve endpoint + FastAPI wired
M2:
- FDC adapter + deterministic ranking + dev endpoints for search/rank
M3:
- A/B assignment + metrics endpoint
M4:
- MCP server exposing deterministic tools (dev-only)
M5:
- Activity calories adapter interface + stub endpoints; pick authoritative source later

---

## Definition of Done (v0)
- A request produces a trace_id; `/development/recent` shows decision path.
- Cache hit prevents LLM calls (verified by logs).
- A/B variants are assigned deterministically and measurable in `/development/stats`.
- caldata runs reliably under systemd on OCI and survives restarts.
