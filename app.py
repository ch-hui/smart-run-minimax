"""MiniMax Anthropic SDK demo HTTP service with MongoDB persistence.

Exposes:
* ``/hello``    — POST (JSON body) or GET (query string) to call MiniMax-M3
* ``/history``  — list recent conversation records stored in MongoDB
* ``/``         — service info
* ``/healthz``  — liveness probe (also reports MongoDB reachability)

Configuration is read from environment variables (see ``.env.example``):

* ``ANTHROPIC_BASE_URL`` — MiniMax Anthropic-compatible base URL
* ``ANTHROPIC_API_KEY``  — your MiniMax subscription key
* ``MINIMAX_MODEL``      — default model (e.g. ``MiniMax-M3``)
* ``MAX_TOKENS``         — default ``max_tokens`` for the request
* ``MONGO_URL``          — full MongoDB connection URI
* ``MONGO_DB``           — database name
* ``MONGO_COLLECTION``   — collection name for conversation history
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pydantic import BaseModel, Field

# Load environment variables from .env (if present) before reading them.
# override=True ensures values in .env take precedence over any shell exports.
load_dotenv(override=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("minimax-hello")

ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.minimax.cn/anthropic")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DEFAULT_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3")
DEFAULT_MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1000"))

# Web search — used to ground /race/batch answers in real, current data so
# the model does not hallucinate dates / season tags. Set TAVILY_API_KEY for
# the most reliable experience; otherwise we fall back to DuckDuckGo HTML
# (no key required, but may rate-limit under load).
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "true").lower() not in ("0", "false", "no")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
WEB_SEARCH_TIMEOUT = float(os.getenv("WEB_SEARCH_TIMEOUT", "8"))
WEB_SEARCH_CACHE_TTL = int(os.getenv("WEB_SEARCH_CACHE_TTL", "1800"))  # 30 min
TAVILY_COOLDOWN_SECONDS = int(os.getenv("TAVILY_COOLDOWN_SECONDS", "3600"))  # 1 h after a quota hit

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "minimax_demo")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "conversations")

if not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill in your key."
    )

# Shared Anthropic SDK client — the underlying httpx client handles pooling.
client = anthropic.Anthropic(
    base_url=ANTHROPIC_BASE_URL,
    api_key=ANTHROPIC_API_KEY,
)


# ---------------------------------------------------------------------------
# MongoDB lifecycle
# ---------------------------------------------------------------------------

class MongoState:
    """Lightweight container for the async MongoDB client."""

    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None
    collection: Optional[AsyncIOMotorCollection] = None
    race_analyses: Optional[AsyncIOMotorCollection] = None
    healthy: bool = False
    last_error: Optional[str] = None


RACE_ANALYSES_COLLECTION = os.getenv("RACE_ANALYSES_COLLECTION", "race_analyses")


mongo_state = MongoState()


async def _connect_mongo() -> None:
    """Open the MongoDB connection. Non-fatal on failure — service still serves /hello."""
    log.info("connecting to MongoDB at %s ...", _redact_mongo_url(MONGO_URL))
    try:
        mongo_state.client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=3000)
        # Trigger an actual round trip so we know the server is reachable.
        await mongo_state.client.admin.command("ping")
        mongo_state.db = mongo_state.client[MONGO_DB]
        mongo_state.collection = mongo_state.db[MONGO_COLLECTION]
        await mongo_state.collection.create_index("created_at")

        # race_analyses — cached structured race info keyed by race_name.
        # Unique index lets us use upsert() without race conditions.
        mongo_state.race_analyses = mongo_state.db[RACE_ANALYSES_COLLECTION]
        await mongo_state.race_analyses.create_index("race_name", unique=True)
        await mongo_state.race_analyses.create_index("updated_at")

        mongo_state.healthy = True
        mongo_state.last_error = None
        log.info(
            "MongoDB connected: db=%s collections=[%s, %s]",
            MONGO_DB, MONGO_COLLECTION, RACE_ANALYSES_COLLECTION,
        )
    except Exception as exc:  # noqa: BLE001 — we want any connection error surfaced
        mongo_state.healthy = False
        mongo_state.last_error = str(exc)
        log.warning("MongoDB unavailable, /history and persistence disabled: %s", exc)


async def _disconnect_mongo() -> None:
    if mongo_state.client is not None:
        mongo_state.client.close()
        log.info("MongoDB client closed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _connect_mongo()
    yield
    await _disconnect_mongo()


def _redact_mongo_url(url: str) -> str:
    """Hide credentials when logging the connection string."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _, host = rest.split("@", 1)
    return f"{scheme}://***@{host}"


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class HelloRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User prompt to send to the model")
    system: Optional[str] = Field(
        default="You are a helpful assistant.",
        description="Optional system prompt override",
    )
    model: Optional[str] = Field(
        default=None,
        description="Optional model override; falls back to MINIMAX_MODEL env / MiniMax-M3",
    )
    max_tokens: Optional[int] = Field(
        default=None,
        ge=1,
        le=32000,
        description="Optional max_tokens override; falls back to MAX_TOKENS env",
    )


# ---------------------------------------------------------------------------
# Anthropic call
# ---------------------------------------------------------------------------


def _call_model(
    text: str,
    system: Optional[str],
    model: Optional[str],
    max_tokens: Optional[int],
) -> dict:
    """Call MiniMax through the Anthropic SDK and return a normalised dict."""
    chosen_model = model or DEFAULT_MODEL
    chosen_max_tokens = max_tokens or DEFAULT_MAX_TOKENS

    log.info(
        "calling model=%s max_tokens=%s text_len=%d", chosen_model, chosen_max_tokens, len(text)
    )

    try:
        message = client.messages.create(
            model=chosen_model,
            max_tokens=chosen_max_tokens,
            system=system or "You are a helpful assistant.",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        )
    except anthropic.APIStatusError as exc:
        log.error("upstream API error: %s", exc)
        raise HTTPException(
            status_code=exc.status_code,
            detail=f"MiniMax API error: {exc.message}",
        ) from exc
    except anthropic.APIConnectionError as exc:
        log.error("connection error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Connection error: {exc}") from exc
    except anthropic.APIError as exc:
        log.error("anthropic SDK error: %s", exc)
        raise HTTPException(status_code=500, detail=f"SDK error: {exc}") from exc

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in message.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "thinking":
            thinking_parts.append(getattr(block, "thinking", ""))

    return {
        "model": message.model,
        "stop_reason": message.stop_reason,
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        },
        "thinking": "\n".join(thinking_parts) if thinking_parts else None,
        "text": "\n".join(text_parts),
    }


async def _persist_conversation(
    *,
    text: str,
    system: Optional[str],
    result: dict[str, Any],
    model_override: Optional[str],
    max_tokens_override: Optional[int],
) -> Optional[str]:
    """Insert a conversation record into MongoDB. Returns the inserted id, or None if disabled."""
    if not mongo_state.healthy or mongo_state.collection is None:
        return None
    try:
        doc = {
            "created_at": datetime.now(timezone.utc),
            "request": {
                "text": text,
                "system": system,
                "model_override": model_override,
                "max_tokens_override": max_tokens_override,
            },
            "response": result,
        }
        inserted = await mongo_state.collection.insert_one(doc)
        return str(inserted.inserted_id)
    except Exception as exc:  # noqa: BLE001 — persistence must never break the main flow
        log.warning("failed to persist conversation: %s", exc)
        return None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MiniMax Anthropic Demo",
    description="Tiny HTTP service that proxies text to MiniMax models via the Anthropic SDK, with MongoDB-backed conversation history.",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/")
def root() -> dict:
    return {
        "service": "minimax-anthropic-demo",
        "model": DEFAULT_MODEL,
        "base_url": ANTHROPIC_BASE_URL,
        "mongo": {
            "db": MONGO_DB,
            "collection": MONGO_COLLECTION,
            "healthy": mongo_state.healthy,
        },
        "search": search_backend_status(),
        "endpoints": {
            "POST /hello": '{"text": "your prompt"}',
            "GET  /hello": "?text=your+prompt",
            "GET  /history": "?limit=20",
            "POST /race/batch": '{"race_names": ["2026南京马拉松", "2026上海半马"]}',
            "GET  /race/batch": "?race_names=A&race_names=B",
            "GET  /healthz": "liveness probe (also pings MongoDB)",
        },
    }


@app.get("/healthz")
async def healthz() -> dict:
    mongo_ok = False
    mongo_detail = mongo_state.last_error or "not connected"
    if mongo_state.healthy and mongo_state.client is not None:
        try:
            await mongo_state.client.admin.command("ping")
            mongo_ok = True
            mongo_detail = "ok"
        except Exception as exc:  # noqa: BLE001
            mongo_detail = str(exc)
    return {
        "status": "ok" if mongo_ok or not MONGO_URL else "degraded",
        "mongo": {
            "enabled": bool(MONGO_URL),
            "healthy": mongo_ok,
            "detail": mongo_detail,
        },
    }


@app.post("/hello")
async def hello_post(req: HelloRequest) -> JSONResponse:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="'text' must not be empty")
    result = _call_model(
        text=req.text,
        system=req.system,
        model=req.model,
        max_tokens=req.max_tokens,
    )
    record_id = await _persist_conversation(
        text=req.text,
        system=req.system,
        result=result,
        model_override=req.model,
        max_tokens_override=req.max_tokens,
    )
    payload = {**result, "persisted_id": record_id, "mongo_healthy": mongo_state.healthy}
    return JSONResponse(payload)


@app.get("/hello")
async def hello_get(
    text: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> JSONResponse:
    if not text.strip():
        raise HTTPException(status_code=400, detail="query param 'text' must not be empty")
    result = _call_model(
        text=text,
        system=system,
        model=model,
        max_tokens=max_tokens,
    )
    record_id = await _persist_conversation(
        text=text,
        system=system,
        result=result,
        model_override=model,
        max_tokens_override=max_tokens,
    )
    payload = {**result, "persisted_id": record_id, "mongo_healthy": mongo_state.healthy}
    return JSONResponse(payload)


@app.get("/history")
async def history(
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """Return the most recent conversation records, newest first."""
    if not mongo_state.healthy or mongo_state.collection is None:
        raise HTTPException(
            status_code=503,
            detail=f"MongoDB unavailable: {mongo_state.last_error}",
        )
    cursor = mongo_state.collection.find().sort("created_at", -1).limit(limit)
    items: list[dict] = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if isinstance(doc.get("created_at"), datetime):
            doc["created_at"] = doc["created_at"].isoformat()
        items.append(doc)
    return {"count": len(items), "items": items}



# ---------------------------------------------------------------------------
# /race/batch — concurrent structured race info via Anthropic tool use
# ---------------------------------------------------------------------------
#
# The single-race /race/analyze endpoint is gone. Use /race/batch with a
# list of race names; we dispatch them concurrently (asyncio.gather +
# a semaphore-capped thread pool) so N races take ~max(per-call latency)
# instead of N * latency.

import asyncio
import time

RACE_TOOL_NAME = "submit_race_info"

RACE_TOOL_SCHEMA = {
    "name": RACE_TOOL_NAME,
    "description": (
        "Submit structured information about a running race (marathon / half / "
        "trail / etc). Call this tool exactly once per request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "race_name": {
                "type": "string",
                "description": "Official race name as provided by the user (verbatim).",
            },
            "race_date": {
                "type": ["string", "null"],
                "description": "Race day in ISO format YYYY-MM-DD, or null if unknown.",
            },
            "registration_start_date": {
                "type": ["string", "null"],
                "description": "Registration opens on this date (YYYY-MM-DD), or null if unknown.",
            },
            "registration_end_date": {
                "type": ["string", "null"],
                "description": "Registration closes on this date (YYYY-MM-DD), or null if unknown.",
            },
            "location": {
                "type": ["string", "null"],
                "description": "City / venue hosting the race, or null if unknown.",
            },
            "distance_category": {
                "type": "string",
                "enum": ["full_marathon", "half_marathon", "10k", "5k", "trail", "ultra", "other"],
                "description": "Primary distance category of the race.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 12,
                "description": (
                    "Short, lowercase Chinese or English feature tags such as "
                    "'城市马拉松', '山地越野', '田协认证', '春季赛事', '夜间赛', 'pb友好'."
                ),
            },
            "summary": {
                "type": "string",
                "minLength": 10,
                "maxLength": 300,
                "description": "One-sentence race description in Chinese.",
            },
            "elevation_gain_m": {
                "type": ["integer", "null"],
                "description": (
                    "Total cumulative elevation GAIN over the full course, in "
                    "metres. Null if unknown. Typical values: flat city "
                    "marathon ~50-200m, hilly road race ~500-1500m, trail/ultra "
                    "race 2000-10000m+. Use the snippet-grounded value; do "
                    "not invent."
                ),
            },
            "max_elevation_m": {
                "type": ["integer", "null"],
                "description": (
                    "Highest point on the course, in metres above sea level. "
                    "Null if unknown."
                ),
            },
            "min_elevation_m": {
                "type": ["integer", "null"],
                "description": (
                    "Lowest point on the course, in metres above sea level. "
                    "Null if unknown."
                ),
            },
            "elevation_profile": {
                "type": ["string", "null"],
                "maxLength": 200,
                "description": (
                    "Short Chinese description of the elevation trend along "
                    "the course, for example: 整体平缓, 仅 35-38km 有持续缓坡; "
                    "前半程起伏频繁, 后半程持续下降; 全程陡峭爬升, 累计爬升超过 3000m. "
                    "Null if no useful information is available."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "Your confidence in the structured facts. 'high' = widely "
                    "publicised and stable; 'low' = guessed or speculative."
                ),
            },
        },
        "required": [
            "race_name",
            "race_date",
            "registration_start_date",
            "registration_end_date",
            "location",
            "distance_category",
            "tags",
            "summary",
            "elevation_gain_m",
            "max_elevation_m",
            "min_elevation_m",
            "elevation_profile",
            "confidence",
        ],
    },
}


RACE_SYSTEM_PROMPT = (
    "You are a running-race information analyst. The user will give you a "
    "race name (e.g. '2026南京马拉松') and you may be supplied with real "
    "web search snippets about that race. Your job:\n"
    "  1) Treat the search snippets as the GROUND TRUTH. When they mention "
    "specific dates, locations, distances, registration windows, "
    "certifications, seasons, or elevation numbers, use those exact values.\n"
    "  2) If a fact is NOT in the snippets AND you are not certain about it, "
    "set the field to null and confidence to 'low'. Never guess.\n"
    "  3) For season tags (春季/夏季/秋季/冬季), infer from the race_date: "
    "Mar-May=spring, Jun-Aug=summer, Sep-Nov=autumn, Dec-Feb=winter. Do NOT "
    "default to spring just because the name contains '春' or similar.\n"
    "  4) Use ISO date format YYYY-MM-DD.\n"
    "  5) Tags must be concise (2-6 chars each). Reflect actual features from "
    "the snippets when available (e.g. '田协认证' if a snippet mentions "
    "中国田径协会 / A1类赛事; '金牌赛事' / '银牌赛事' / '铜牌赛事' for IAAF/CAA tiers).\n"
    "  6) Summary must be one Chinese sentence (10-300 chars), factual, "
    "and may quote key details from the snippets.\n"
    "  7) Elevation fields: extract total_gain_m / max / min if the snippets "
    "mention numbers like '累计爬升' / '海拔' / '最高点' / '最低点' / 'total "
    "elevation gain' / 'highest point'. For elevation_profile, write 1-2 "
    "Chinese sentences describing the trend (flat / rolling / mountainous / "
    "net descent / net ascent / where the hard parts are). If snippets give "
    "no useful info, set all four to null.\n"
    "Always call the submit_race_info tool exactly once. When snippets are "
    "present, set confidence to 'high' for fields that match snippets verbatim."
)


class RaceBatchRequest(BaseModel):
    race_names: list[str] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="1-20 race names to analyse concurrently",
    )
    model: Optional[str] = Field(default=None)
    max_tokens: Optional[int] = Field(default=None, ge=256, le=8000)
    concurrency: Optional[int] = Field(
        default=5, ge=1, le=10,
        description="Max parallel upstream calls; clamped to len(race_names)",
    )


class RaceItemResult(BaseModel):
    """Per-race fixed-shape result. Identical to the previous single-race schema."""

    race_name: str
    race_date: Optional[str] = None
    registration_start_date: Optional[str] = None
    registration_end_date: Optional[str] = None
    location: Optional[str] = None
    distance_category: str
    tags: list[str]
    summary: str
    elevation_gain_m: Optional[int] = None
    max_elevation_m: Optional[int] = None
    min_elevation_m: Optional[int] = None
    elevation_profile: Optional[str] = None
    confidence: str
    model: str
    usage: dict
    # Provenance: was this served from the cache or freshly generated?
    # 'cache' = served from mongo, no model call; 'model' = freshly generated
    # (and cached on the way back); 'manual' = served a record created via
    # /race/correction.
    source: str = "model"
    cached_at: Optional[str] = None


class RaceItemError(BaseModel):
    race_name: str
    error: str


class RaceBatchResponse(BaseModel):
    count: int
    success: int
    failed: int
    elapsed_ms: int
    results: list[RaceItemResult | RaceItemError]


_DATE_FIELDS = ("race_date", "registration_start_date", "registration_end_date")


# ---------------------------------------------------------------------------
# Web search — grounds /race/batch answers in real data
# ---------------------------------------------------------------------------
#
# Two backends, in priority order:
#   1) Tavily Search API (TAVILY_API_KEY env var) — purpose-built for LLMs,
#      returns clean snippets. Get a free key at https://tavily.com
#   2) DuckDuckGo HTML — no key, no SDK; we just parse the public HTML page.
#
# Both are wrapped with a TTL cache so repeated batch calls with the same
# race name don't re-hit the network. Failures are non-fatal: _search_web
# returns None and /race/batch falls back to the model's own knowledge
# (which is the legacy behaviour).

_search_cache: dict[str, tuple[float, Optional[str]]] = {}
_search_lock = threading.Lock()

# Tavily quota protection — once we see a 429 / 402 we stop calling Tavily
# for TAVILY_COOLDOWN_SECONDS to avoid burning the rest of the free tier on
# requests that will all be rejected anyway. While disabled, _search_web
# transparently falls back to DuckDuckGo HTML.
_tavily_lock = threading.Lock()
_tavily_disabled_until: float = 0.0
_tavily_last_disabled_reason: str = ""

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15"


def _fetch_url(url: str, headers: dict[str, str], timeout: float) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _tavily_in_cooldown() -> bool:
    return time.time() < _tavily_disabled_until


def _disable_tavily(reason: str) -> None:
    """Mark Tavily as unavailable until now + TAVILY_COOLDOWN_SECONDS."""
    global _tavily_disabled_until, _tavily_last_disabled_reason
    with _tavily_lock:
        _tavily_disabled_until = time.time() + TAVILY_COOLDOWN_SECONDS
        _tavily_last_disabled_reason = reason


def _search_tavily(query: str, max_results: int = 5) -> Optional[str]:
    """Synchronous Tavily Search API call.

    Returns None on any failure (network, quota, parse). If a 429 / 402 is
    observed, additionally marks Tavily as in cooldown so subsequent calls
    skip it for TAVILY_COOLDOWN_SECONDS.
    """
    if not TAVILY_API_KEY or _tavily_in_cooldown():
        return None
    payload = json.dumps({
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=WEB_SEARCH_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Treat 401 / 402 / 403 / 429 as "stop hammering Tavily":
        #   401 = unauthorized (bad/expired key — same outcome either way)
        #   402 = payment required (free-tier quota exhausted)
        #   403 = forbidden (often used for quota on dev keys)
        #   429 = rate limit
        if exc.code in (401, 402, 403, 429):
            retry_after = exc.headers.get("Retry-After")
            try:
                cooldown = int(retry_after) if retry_after else TAVILY_COOLDOWN_SECONDS
            except ValueError:
                cooldown = TAVILY_COOLDOWN_SECONDS
            with _tavily_lock:
                global _tavily_disabled_until, _tavily_last_disabled_reason
                _tavily_disabled_until = time.time() + cooldown
                _tavily_last_disabled_reason = f"HTTP {exc.code}"
            log.warning(
                "tavily quota/rate-limit/auth hit (HTTP %s) for %r; "
                "disabling tavily for %ss, falling back to DDG",
                exc.code, query, cooldown,
            )
        else:
            log.warning("tavily search HTTP %s for %r: %s", exc.code, query, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("tavily search failed for %r: %s", query, exc)
        return None

    parts: list[str] = []
    for item in data.get("results", [])[:max_results]:
        title = item.get("title", "").strip()
        content = item.get("content", "").strip()
        url = item.get("url", "").strip()
        if title or content:
            parts.append(f"[{title}]({url})\n{content}" if url else f"{title}\n{content}")
    return "\n\n".join(parts) if parts else None


_DDG_RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    s = _TAG_STRIP_RE.sub("", s)
    return html.unescape(s).strip()


def _search_duckduckgo(query: str, max_results: int = 5) -> Optional[str]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    try:
        html_text = _fetch_url(url, {"User-Agent": _UA}, WEB_SEARCH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        log.warning("duckduckgo search failed for %r: %s", query, exc)
        return None

    titles = [_strip_html(m.group(2)) for m in _DDG_RESULT_LINK_RE.finditer(html_text)]
    snippets = [_strip_html(m.group(1)) for m in _DDG_SNIPPET_RE.finditer(html_text)]
    pairs = list(zip(titles, snippets))[:max_results]
    parts = [f"{t}\n{s}" for t, s in pairs if t or s]
    return "\n\n".join(parts) if parts else None


def _search_web(query: str) -> Optional[str]:
    """Return concatenated search snippets for ``query``, or None on failure.

    Cached for WEB_SEARCH_CACHE_TTL seconds per query string.
    """
    if not WEB_SEARCH_ENABLED:
        return None
    now = time.time()
    with _search_lock:
        cached = _search_cache.get(query)
        if cached and (now - cached[0]) < WEB_SEARCH_CACHE_TTL:
            return cached[1]

    snippets: Optional[str] = None
    if TAVILY_API_KEY and not _tavily_in_cooldown():
        snippets = _search_tavily(query)
        if not snippets and _tavily_in_cooldown():
            log.info("tavily entered cooldown after a quota hit; using DDG for %r", query)
        elif not snippets:
            log.info("tavily returned no snippets for %r, falling back to DDG", query)
    elif TAVILY_API_KEY and _tavily_in_cooldown():
        log.debug("tavily in cooldown (until +%ds); using DDG for %r",
                  int(_tavily_disabled_until - time.time()), query)
    if not snippets:
        snippets = _search_duckduckgo(query)

    with _search_lock:
        _search_cache[query] = (now, snippets)
    return snippets


def search_backend_status() -> dict[str, Any]:
    """Return current search-backend state for /healthz and /."""
    tavily_active = bool(TAVILY_API_KEY) and not _tavily_in_cooldown()
    in_cooldown = bool(TAVILY_API_KEY) and _tavily_in_cooldown()
    return {
        "enabled": WEB_SEARCH_ENABLED,
        "primary": "tavily" if TAVILY_API_KEY else "duckduckgo",
        "fallback": "duckduckgo",
        "tavily": {
            "configured": bool(TAVILY_API_KEY),
            "active": tavily_active,
            "in_cooldown": in_cooldown,
            "cooldown_remaining_sec": max(0, int(_tavily_disabled_until - time.time())),
            "last_disabled_reason": _tavily_last_disabled_reason,
        },
    }


def _normalise_race_info(raw: dict[str, Any], race_name: str) -> dict[str, Any]:
    """Sanitise the model's tool input into our fixed response shape."""
    out: dict[str, Any] = {
        "race_name": str(raw.get("race_name") or race_name).strip() or race_name,
        "race_date": raw.get("race_date"),
        "registration_start_date": raw.get("registration_start_date"),
        "registration_end_date": raw.get("registration_end_date"),
        "location": raw.get("location"),
        "distance_category": raw.get("distance_category") or "other",
        "tags": [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()][:12],
        "summary": str(raw.get("summary") or "").strip(),
        "confidence": raw.get("confidence") or "low",
    }
    for field in _DATE_FIELDS:
        val = out[field]
        if val is None:
            continue
        if not isinstance(val, str) or len(val) != 10 or val[4] != "-" or val[7] != "-":
            log.warning("model returned malformed %s=%r, normalising to null", field, val)
            out[field] = None

    # Elevation fields: validate as non-negative integers in metres.
    # Valid range covers Dead Sea (~-430m) to Everest summit (8848m),
    # plus trail-race ascent (gain up to ~20000m cumulative).
    for field in ("elevation_gain_m", "max_elevation_m", "min_elevation_m"):
        val = raw.get(field)
        if val is None:
            out[field] = None
            continue
        try:
            num = int(val)
        except (TypeError, ValueError):
            log.warning("model returned non-integer %s=%r, normalising to null", field, val)
            out[field] = None
            continue
        if not (-500 <= num <= 30000):
            log.warning("model returned out-of-range %s=%d, normalising to null", field, num)
            out[field] = None
            continue
        out[field] = num

    profile = raw.get("elevation_profile")
    if isinstance(profile, str) and profile.strip():
        out["elevation_profile"] = profile.strip()[:200]
    else:
        out["elevation_profile"] = None

    if not out["tags"]:
        out["tags"] = ["未分类"]
    if not out["summary"]:
        out["summary"] = f"{out['race_name']} 相关信息有限，建议参考官方公告。"
    if out["confidence"] not in ("high", "medium", "low"):
        out["confidence"] = "low"
    return out


def _call_race_one_sync(
    race_name: str,
    chosen_model: str,
    chosen_max_tokens: int,
    search_snippets: Optional[str] = None,
) -> dict:
    """Synchronous SDK call — invoked via asyncio.to_thread()."""
    user_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"请分析以下比赛并按 schema 返回结构化信息：\n\n{race_name}\n\n"
                "如果某项事实在下面提供的搜索片段里有明确信息，请直接采用；"
                "如果搜索片段与你的知识冲突，以搜索片段为准（更近）；"
                "如果都不确定，对应字段填 null，并把 confidence 设为 low。"
            ),
        }
    ]
    if search_snippets:
        user_blocks.append({
            "type": "text",
            "text": (
                "以下是该比赛的实时搜索片段（视为真实信息源）：\n\n"
                + search_snippets
            ),
        })

    message = client.messages.create(
        model=chosen_model,
        max_tokens=chosen_max_tokens,
        system=RACE_SYSTEM_PROMPT,
        tools=[RACE_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": RACE_TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": user_blocks,
            }
        ],
    )

    tool_input: Optional[dict[str, Any]] = None
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == RACE_TOOL_NAME:
            tool_input = dict(block.input or {})
            break

    if tool_input is None:
        raise RuntimeError("model did not return submit_race_info tool_use")

    info = _normalise_race_info(tool_input, race_name)
    info["model"] = message.model
    info["usage"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }
    return info


async def _analyze_race_one(
    race_name: str,
    chosen_model: str,
    chosen_max_tokens: int,
    sem: asyncio.Semaphore,
) -> RaceItemResult | RaceItemError:
    """Analyse a single race. Catches all SDK errors so one failure does not
    abort the whole batch."""
    async with sem:
        # Step 1: try to fetch real search snippets so the model answers from
        # ground truth rather than stale training data. We do two searches:
        # one for the basic race info and one specifically for elevation /
        # course profile. Both are cached internally so repeat calls in the
        # same batch are cheap.
        snippets: Optional[str] = None
        try:
            base, elev = await asyncio.gather(
                asyncio.to_thread(_search_web, race_name),
                asyncio.to_thread(_search_web, f"{race_name} 累计爬升 海拔 路线 elevation gain profile"),
            )
            parts = [s for s in (base, elev) if s]
            snippets = "\n\n".join(parts) if parts else None
        except Exception as exc:  # noqa: BLE001
            log.warning("search failed for %r: %s", race_name, exc)

        # Step 2: ask the model. SDK call also runs in a worker thread.
        try:
            info = await asyncio.to_thread(
                _call_race_one_sync,
                race_name,
                chosen_model,
                chosen_max_tokens,
                snippets,
            )
            info["source"] = "model"
            info["cached_at"] = None
            return RaceItemResult(**info)
        except anthropic.APIStatusError as exc:
            return RaceItemError(race_name=race_name, error=f"upstream {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError as exc:
            return RaceItemError(race_name=race_name, error=f"connection: {exc}")
        except anthropic.APIError as exc:
            return RaceItemError(race_name=race_name, error=f"sdk: {exc}")
        except Exception as exc:  # noqa: BLE001 — must not break the whole batch
            log.exception("race analyze failed for %r", race_name)
            return RaceItemError(race_name=race_name, error=str(exc))


async def _load_cached_analyses(race_names: list[str]) -> dict[str, dict[str, Any]]:
    """Return race_name -> cached record dict from mongo, or {} if mongo unavailable."""
    if not mongo_state.healthy or mongo_state.race_analyses is None:
        return {}
    try:
        cursor = mongo_state.race_analyses.find({"race_name": {"$in": race_names}})
        out: dict[str, dict[str, Any]] = {}
        async for doc in cursor:
            out[doc["race_name"]] = doc
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("mongo cache load failed: %s", exc)
        return {}


async def _save_race_analysis(race_name: str, info: dict[str, Any], source: str) -> None:
    """Upsert one race analysis to mongo. Silent on failure."""
    if not mongo_state.healthy or mongo_state.race_analyses is None:
        return
    try:
        now = datetime.now(timezone.utc)
        doc = {**info, "race_name": race_name, "source": source, "updated_at": now}
        doc.pop("cached_at", None)
        doc.pop("_id", None)
        await mongo_state.race_analyses.update_one(
            {"race_name": race_name},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("mongo cache save failed for %r: %s", race_name, exc)


def _cached_doc_to_result(doc: dict[str, Any]) -> RaceItemResult:
    """Convert a stored mongo document into a RaceItemResult (drop mongo-only fields).

    ``source`` reflects both provenance and freshness:
      * 'manual'  — record was created via /race/correction (highest priority)
      * 'cache'   — auto-generated model record, served from mongo (cache hit)
    """
    record_source = doc.get("source") or "cache"
    effective_source = "manual" if record_source == "manual" else "cache"
    out = {
        "race_name": doc["race_name"],
        "race_date": doc.get("race_date"),
        "registration_start_date": doc.get("registration_start_date"),
        "registration_end_date": doc.get("registration_end_date"),
        "location": doc.get("location"),
        "distance_category": doc.get("distance_category") or "other",
        "tags": doc.get("tags") or ["未分类"],
        "summary": doc.get("summary") or f"{doc['race_name']} 相关信息有限，建议参考官方公告。",
        "elevation_gain_m": doc.get("elevation_gain_m"),
        "max_elevation_m": doc.get("max_elevation_m"),
        "min_elevation_m": doc.get("min_elevation_m"),
        "elevation_profile": doc.get("elevation_profile"),
        "confidence": doc.get("confidence") or "low",
        "model": doc.get("model") or "cached",
        "usage": doc.get("usage") or {"input_tokens": 0, "output_tokens": 0},
        "source": effective_source,
        "cached_at": doc["updated_at"].isoformat() if isinstance(doc.get("updated_at"), datetime) else None,
    }
    return RaceItemResult(**out)


async def _analyze_race_batch(
    race_names: list[str],
    model_override: Optional[str],
    max_tokens_override: Optional[int],
    concurrency: int,
) -> RaceBatchResponse:
    chosen_model = model_override or DEFAULT_MODEL
    chosen_max_tokens = max_tokens_override or 1500
    concurrency = max(1, min(concurrency, len(race_names)))

    sem = asyncio.Semaphore(concurrency)
    log.info(
        "race batch: %d races, model=%s, concurrency=%d",
        len(race_names), chosen_model, concurrency,
    )

    # Step 1: batch-load any cached records from mongo. Cache hit = zero Tavily
    # calls, zero model calls, zero latency. This is the fast path for any
    # race the user has previously queried (or that someone manually seeded
    # via /race/correction).
    cached = await _load_cached_analyses(race_names)
    miss_names = [n for n in race_names if n not in cached]
    log.info("race batch: %d cached, %d cache-miss (will query model)", len(cached), len(miss_names))

    started = time.perf_counter()

    # Step 2: build cached results (preserving input order) and run the model
    # for the misses in parallel.
    cached_results: list[RaceItemResult | RaceItemError] = []
    miss_results: list[RaceItemResult | RaceItemError] = []
    if miss_names:
        miss_results = await asyncio.gather(
            *[_analyze_race_one(name, chosen_model, chosen_max_tokens, sem) for name in miss_names]
        )

    # Step 3: persist every successful fresh result to mongo so the next call
    # hits the cache. Cache writes are best-effort; failure does not affect
    # the response.
    for name, res in zip(miss_names, miss_results):
        if isinstance(res, RaceItemResult):
            await _save_race_analysis(
                name,
                res.model_dump(exclude={"source", "cached_at"}),
                source="model",
            )

    # Step 4: re-assemble in the original input order.
    results_by_name: dict[str, RaceItemResult | RaceItemError] = {}
    for name in race_names:
        if name in cached:
            try:
                results_by_name[name] = _cached_doc_to_result(cached[name])
            except Exception as exc:  # noqa: BLE001
                log.warning("cached doc decode failed for %r: %s — falling through", name, exc)
                results_by_name[name] = RaceItemError(race_name=name, error=f"cached doc decode failed: {exc}")
    for name, res in zip(miss_names, miss_results):
        results_by_name[name] = res

    results = [results_by_name[n] for n in race_names]
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    success = sum(1 for r in results if isinstance(r, RaceItemResult))
    failed = len(results) - success
    from_cache = sum(1 for r in results if isinstance(r, RaceItemResult) and r.source in ("cache", "manual"))
    log.info(
        "race batch done: %d success (%d from cache), %d failed in %dms",
        success, from_cache, failed, elapsed_ms,
    )

    return RaceBatchResponse(
        count=len(results),
        success=success,
        failed=failed,
        elapsed_ms=elapsed_ms,
        results=list(results),
    )


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


@app.post("/race/batch", response_model=RaceBatchResponse)
async def race_batch_post(req: RaceBatchRequest) -> RaceBatchResponse:
    cleaned = _dedupe_preserve_order([n for n in req.race_names if n and n.strip()])
    if not cleaned:
        raise HTTPException(status_code=400, detail="'race_names' must contain at least one non-empty entry")
    return await _analyze_race_batch(
        race_names=cleaned,
        model_override=req.model,
        max_tokens_override=req.max_tokens,
        concurrency=req.concurrency or 5,
    )


@app.get("/race/batch", response_model=RaceBatchResponse)
async def race_batch_get(
    race_names: list[str] = Query(
        ...,
        description="Repeat ?race_names=A&race_names=B to pass multiple. URL-encode Chinese.",
    ),
    model: Optional[str] = None,
    max_tokens: Optional[int] = Query(default=None, ge=256, le=8000),
    concurrency: Optional[int] = Query(default=5, ge=1, le=10),
) -> RaceBatchResponse:
    cleaned = _dedupe_preserve_order([n for n in race_names if n and n.strip()])
    if not cleaned:
        raise HTTPException(status_code=400, detail="query param 'race_names' must contain at least one non-empty entry")
    return await _analyze_race_batch(
        race_names=cleaned,
        model_override=model,
        max_tokens_override=max_tokens,
        concurrency=concurrency or 5,
    )


# ---------------------------------------------------------------------------
# /race/correction — manually seed or correct a cached analysis
# ---------------------------------------------------------------------------
#
# Use this when the model couldn't find elevation / dates (search snippets
# were thin) and you have authoritative data from the official website /
# a printed brochure / GPX file. Future /race/batch calls will hit this
# record straight from mongo with source='manual'.

class RaceCorrectionRequest(BaseModel):
    race_name: str = Field(..., min_length=1, max_length=200)
    race_date: Optional[str] = None
    registration_start_date: Optional[str] = None
    registration_end_date: Optional[str] = None
    location: Optional[str] = None
    distance_category: Optional[str] = "other"
    tags: Optional[list[str]] = None
    summary: Optional[str] = None
    elevation_gain_m: Optional[int] = Field(default=None, ge=-500, le=30000)
    max_elevation_m: Optional[int] = Field(default=None, ge=-500, le=30000)
    min_elevation_m: Optional[int] = Field(default=None, ge=-500, le=30000)
    elevation_profile: Optional[str] = Field(default=None, max_length=200)
    confidence: Optional[str] = "high"


def _validate_iso_date(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise HTTPException(status_code=400, detail=f"{field} must be ISO YYYY-MM-DD")
    return value


@app.post("/race/correction")
async def race_correction(req: RaceCorrectionRequest) -> dict:
    if not mongo_state.healthy or mongo_state.race_analyses is None:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {mongo_state.last_error}")

    distance_category = req.distance_category or "other"
    if distance_category not in ("full_marathon", "half_marathon", "10k", "5k", "trail", "ultra", "other"):
        raise HTTPException(status_code=400, detail=f"unknown distance_category: {distance_category}")

    confidence = (req.confidence or "high").lower()
    if confidence not in ("high", "medium", "low"):
        raise HTTPException(status_code=400, detail=f"unknown confidence: {confidence}")

    doc = {
        "race_name": req.race_name,
        "race_date": _validate_iso_date(req.race_date, "race_date"),
        "registration_start_date": _validate_iso_date(req.registration_start_date, "registration_start_date"),
        "registration_end_date": _validate_iso_date(req.registration_end_date, "registration_end_date"),
        "location": req.location,
        "distance_category": distance_category,
        "tags": [t.strip() for t in (req.tags or []) if t and t.strip()][:12] or ["未分类"],
        "summary": req.summary or f"{req.race_name} 人工录入数据。",
        "elevation_gain_m": req.elevation_gain_m,
        "max_elevation_m": req.max_elevation_m,
        "min_elevation_m": req.min_elevation_m,
        "elevation_profile": req.elevation_profile,
        "confidence": confidence,
        "model": "manual",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "source": "manual",
    }

    try:
        now = datetime.now(timezone.utc)
        await mongo_state.race_analyses.update_one(
            {"race_name": req.race_name},
            {"$set": {**doc, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"mongo upsert failed: {exc}") from exc

    return {
        "ok": True,
        "race_name": req.race_name,
        "source": "manual",
        "updated_at": now.isoformat(),
    }


@app.get("/race/cache")
async def race_cache_stats() -> dict:
    """Show how many race_analyses are cached (for ops sanity checks)."""
    if not mongo_state.healthy or mongo_state.race_analyses is None:
        return {"healthy": False, "detail": mongo_state.last_error, "count": 0}
    try:
        total = await mongo_state.race_analyses.count_documents({})
        by_source = {}
        async for doc in mongo_state.race_analyses.aggregate([
            {"$group": {"_id": "$source", "n": {"$sum": 1}}}
        ]):
            by_source[doc["_id"] or "unknown"] = doc["n"]
        return {"healthy": True, "total": total, "by_source": by_source}
    except Exception as exc:  # noqa: BLE001
        return {"healthy": False, "detail": str(exc), "count": 0}
