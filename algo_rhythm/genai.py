"""The one gateway every Gemini call goes through: key, model, retries, schema check, cache, daily cap, one log line.

    from algo_rhythm import genai
    answer = genai.generate("talk", "talk-v1", [{"text": prompt}], schema)

Auth: env GEMINI_API_KEY, else ~/.config/song-matcher/gemini_key; with no key (or GEMINI_AUTH=adc), Vertex uses the
service account (Application Default Credentials: Cloud Run's account, or `gcloud auth application-default login`),
which needs roles/aiplatform.user. Env GEMINI_SERVICE_ACCOUNT: impersonate that account for every AI call instead (the
caller needs roles/iam.serviceAccountTokenCreator on it). Model: env GEMINI_MODEL (default gemini-3.5-flash-lite).
Backend: env GEMINI_BACKEND = "vertex" (default; an express-mode key on the global endpoint, or the project's endpoint at
VERTEX_LOCATION, default global, for the service account or when VERTEX_LOCATION is set; project: GOOGLE_CLOUD_PROJECT,
else the credentials') or "studio" (an AI Studio key, generativelanguage.googleapis.com).
Daily cap: env GEMINI_DAILY_CAP (default 200), a cost guard on text requests.
Embeddings: genai.embed(texts, task_type=...) with env GEMINI_EMBED_MODEL (default gemini-embedding-001); they do not
count against the cap.
"""
import datetime
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests
import truststore

truststore.inject_into_ssl()  # corporate root certificate; TLS checks stay on

STUDIO = "https://generativelanguage.googleapis.com/v1beta/models/{}"
VERTEX = "https://aiplatform.googleapis.com/v1/publishers/google/models/{}"
VERTEX_PROJECT = "https://{host}/v1/projects/{project}/locations/{loc}/publishers/google/models/{}"
EMBED_DIM = 768        # the embedding models allow 128-3072; 768 keeps a library's vectors small
EMBED_BATCH = 100      # texts per embedding request (Vertex refuses more than 250 instances)
KEY_FILE = Path.home() / ".config/song-matcher/gemini_key"
RETRIES = 3            # extra tries after a transient failure (5xx, timeout, connection drop)
BACKOFF_S = 2          # wait 2, 4, 8 s between them
TRANSIENT = (500, 502, 503, 504)
log = logging.getLogger("algo_rhythm.genai")


class GenAIError(RuntimeError):
    pass


class QuotaError(GenAIError):
    pass


class RateLimited(QuotaError):
    """HTTP 429: Google's per-minute quota, which refills within minutes; a batch job may wait and try again."""


class MemoryCache:
    """Default cache: answers kept for the life of the process. A DB-backed one has the same two methods."""

    def __init__(self):
        self.rows = {}

    def get(self, key):
        return self.rows.get(key)

    def put(self, key, task, model, prompt_version, request, response):
        self.rows[key] = response


CACHE = MemoryCache()
# ponytail: per-process counter, so a restart resets it; keep it in the DB cache if several processes share a key
_used = {"day": None, "n": 0}


def model_name():
    return os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def embed_model():
    return os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")


def backend():
    b = os.environ.get("GEMINI_BACKEND", "vertex")
    if b not in ("vertex", "studio"):
        raise GenAIError(f"GEMINI_BACKEND must be vertex or studio, not {b!r}")
    return b


def model_url(model, method):
    """The endpoint for `model` and `method` (generateContent, predict, batchEmbedContents) on the chosen backend."""
    if backend() == "studio":
        return STUDIO.format(model) + ":" + method
    loc = os.environ.get("VERTEX_LOCATION")
    if not loc and api_key():
        return VERTEX.format(model) + ":" + method
    loc = loc or "global"
    host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    return VERTEX_PROJECT.format(model, host=host, loc=loc, project=project()) + ":" + method


def api_key():
    """The API key, or None: then Vertex uses the service account. GEMINI_AUTH=adc ignores any key."""
    if os.environ.get("GEMINI_AUTH") == "adc":
        return None
    return os.environ.get("GEMINI_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else None)


_adc = {"creds": None, "project": None, "lock": threading.Lock()}


def _credentials():
    """Application Default Credentials, refreshed when the token is about to run out. GenAIError when there are none."""
    with _adc["lock"]:
        try:
            import google.auth
            import google.auth.transport.requests
            if _adc["creds"] is None:
                scopes = ["https://www.googleapis.com/auth/cloud-platform"]
                creds, _adc["project"] = google.auth.default(scopes=scopes)
                sa = os.environ.get("GEMINI_SERVICE_ACCOUNT")
                if sa:
                    from google.auth import impersonated_credentials
                    creds = impersonated_credentials.Credentials(source_credentials=creds, target_principal=sa,
                                                                 target_scopes=scopes)
                    # name@PROJECT.iam.gserviceaccount.com: bill the account's own project
                    if sa.endswith(".iam.gserviceaccount.com"):
                        _adc["project"] = sa.split("@")[1].split(".")[0]
                _adc["creds"] = creds
            if not _adc["creds"].valid:
                _adc["creds"].refresh(google.auth.transport.requests.Request())
        except Exception as e:
            raise GenAIError(f"No key and no service account ({type(e).__name__}): set GEMINI_API_KEY, put it in "
                             f"{KEY_FILE}, or run `gcloud auth application-default login`") from None
        return _adc["creds"]


def project():
    p = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not p:
        _credentials()
        p = _adc["project"]
    if not p:
        raise GenAIError("Vertex with the service account needs GOOGLE_CLOUD_PROJECT")
    return p


def auth_headers():
    """{"x-goog-api-key": key}, or the service account's bearer token on Vertex. GenAIError when neither is there."""
    key = api_key()
    if key:
        return {"x-goog-api-key": key}
    if backend() == "studio":
        raise GenAIError("No key: AI Studio needs GEMINI_API_KEY or the key in " + str(KEY_FILE))
    return {"Authorization": "Bearer " + _credentials().token}


def cache_key(model, task, prompt_version, parts, schema):
    blob = json.dumps([model, task, prompt_version, parts, schema], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _spend_one():
    """Count one HTTP request against today's cap; QuotaError (before calling) when it is used up."""
    today, cap = datetime.date.today().isoformat(), int(os.environ.get("GEMINI_DAILY_CAP", "200"))
    if _used["day"] != today:
        _used.update(day=today, n=0)
    if _used["n"] >= cap:
        raise QuotaError(f"daily Gemini cap reached ({cap} requests); raise GEMINI_DAILY_CAP or wait until tomorrow")
    _used["n"] += 1


def validate(value, schema, path="answer"):
    """Raise GenAIError if `value` does not match the Gemini `schema` (types, required fields, enum values)."""
    t = schema.get("type", "").upper()
    ok = {"STRING": lambda v: isinstance(v, str), "BOOLEAN": lambda v: isinstance(v, bool),
          "INTEGER": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "NUMBER": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
          "ARRAY": lambda v: isinstance(v, list), "OBJECT": lambda v: isinstance(v, dict)}.get(t, lambda v: True)
    if not ok(value):
        raise GenAIError(f"{path}: expected {t}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise GenAIError(f"{path}: {value!r} is not one of {schema['enum']}")
    if t == "ARRAY":
        for i, v in enumerate(value):
            validate(v, schema.get("items", {}), f"{path}[{i}]")
    if t == "OBJECT":
        for k in schema.get("required", []):
            if k not in value:
                raise GenAIError(f"{path}: missing {k}")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                validate(value[k], sub, f"{path}.{k}")


def _send(url, body, timeout, spend=True):
    """POST with retries on transient errors; QuotaError on 429 at once. Returns the response JSON.
    spend=False: not counted against GEMINI_DAILY_CAP (embeddings use another model and allowance)."""
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(BACKOFF_S * 2 ** (attempt - 1))
        if spend:
            _spend_one()
        try:
            r = requests.post(url, headers=auth_headers(), json=body, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = f"{type(e).__name__}"
            continue
        if r.status_code == 429:  # quota: retrying only burns more of it
            try:
                msg = r.json()["error"]["message"].split(". ")[0]
            except Exception:
                msg = "429"
            raise RateLimited("Gemini quota used up: " + msg + ". Retry later.")
        if r.status_code in TRANSIENT:
            last = f"HTTP {r.status_code}"
            continue
        if r.status_code != 200:
            try:
                msg = ": " + r.json()["error"]["message"][:200]
            except Exception:
                msg = ""
            raise GenAIError(f"Gemini answered HTTP {r.status_code}{msg}")
        try:
            return r.json()
        except ValueError:
            raise GenAIError("Gemini answer is not JSON") from None
    raise GenAIError(f"Gemini kept failing ({last}) after {RETRIES} retries")


def _post(model, parts, schema, timeout):
    """One generateContent request; returns the parsed JSON answer."""
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0.2}}
    out = _send(model_url(model, "generateContent"), body, timeout)
    try:
        return json.loads(out["candidates"][0]["content"]["parts"][0]["text"])
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise GenAIError(f"Gemini answer is not the expected JSON: {type(e).__name__}") from None


def generate(task, prompt_version, parts, schema, *, cache=None, on_call=None, timeout=30, model=None):
    """One Gemini call that must answer JSON matching `schema`; returns the parsed answer.
    Raises QuotaError (429 or daily cap) or GenAIError (bad answer, HTTP error, transient errors that outlast retries)."""
    cache = CACHE if cache is None else cache
    model = model or model_name()
    key = cache_key(model, task, prompt_version, parts, schema)
    t0, cached, answer, err = time.monotonic(), False, None, None
    try:
        answer = cache.get(key)
        cached = answer is not None
        if not cached:
            answer = _post(model, parts, schema, timeout)
            validate(answer, schema)
            cache.put(key, task, model, prompt_version, {"parts": parts, "schema": schema}, answer)
        return answer
    except Exception as e:
        err = e
        raise
    finally:
        ms = int((time.monotonic() - t0) * 1000)
        ename = type(err).__name__ if err else None
        log.info("genai task=%s model=%s prompt_version=%s cached=%s ok=%s latency_ms=%d error=%s%s",
                 task, model, prompt_version, cached, err is None, ms, ename, f" ({err})" if err else "")
        if on_call:
            on_call(task, model, prompt_version, cached, err is None, ms, ename)


def embed(texts, *, task_type, cache=None, on_call=None, timeout=60, model=None):
    """Embedding vectors (lists of EMBED_DIM floats) for `texts`, in order. task_type: RETRIEVAL_DOCUMENT for
    what is searched, RETRIEVAL_QUERY for what the user typed. Cached per text; one batchEmbedContents request per
    EMBED_BATCH uncached texts (Vertex :predict, or AI Studio batchEmbedContents). Raises QuotaError or GenAIError
    like generate()."""
    cache = CACHE if cache is None else cache
    model = model or embed_model()
    keys = [cache_key(model, "embed", task_type, t, EMBED_DIM) for t in texts]
    hits = {k: v["vector"] for k in set(keys) if (v := cache.get(k))}
    todo = list(dict.fromkeys(t for t, k in zip(texts, keys) if k not in hits))
    for i in range(0, len(todo), EMBED_BATCH):
        part = todo[i:i + EMBED_BATCH]
        t0, err = time.monotonic(), None
        try:
            if backend() == "studio":
                body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                                      "taskType": task_type, "outputDimensionality": EMBED_DIM} for t in part]}
                out = _send(model_url(model, "batchEmbedContents"), body, timeout, spend=False)
            else:
                body = {"instances": [{"content": t, "task_type": task_type} for t in part],
                        "parameters": {"outputDimensionality": EMBED_DIM}}
                out = _send(model_url(model, "predict"), body, timeout, spend=False)
            try:
                vecs = ([e["values"] for e in out["embeddings"]] if "embeddings" in out
                        else [p["embeddings"]["values"] for p in out["predictions"]])
            except (KeyError, TypeError):
                raise GenAIError("Gemini embedding answer has no embeddings") from None
            if len(vecs) != len(part) or any(len(v) != EMBED_DIM for v in vecs):
                raise GenAIError("Gemini embedding answer has the wrong shape")
            for t, v in zip(part, vecs):
                k = cache_key(model, "embed", task_type, t, EMBED_DIM)
                cache.put(k, "embed", model, task_type, {"text": t}, {"vector": v})
                hits[k] = v
        except Exception as e:
            err = e
            raise
        finally:
            ms, ename = int((time.monotonic() - t0) * 1000), type(err).__name__ if err else None
            log.info("genai task=embed model=%s task_type=%s texts=%d ok=%s latency_ms=%d error=%s",
                     model, task_type, len(part), err is None, ms, ename)
            if on_call:
                on_call("embed", model, task_type, False, err is None, ms, ename)
    return [hits[k] for k in keys]
