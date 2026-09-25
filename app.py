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

import logging
import os
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
    healthy: bool = False
    last_error: Optional[str] = None


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
        mongo_state.healthy = True
        mongo_state.last_error = None
        log.info("MongoDB connected: db=%s collection=%s", MONGO_DB, MONGO_COLLECTION)
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
            "confidence",
        ],
    },
}


RACE_SYSTEM_PROMPT = (
    "You are a running-race information analyst. Given a race name (typically "
    "in Chinese, e.g. '2026南京马拉松'), fill in the structured schema with "
    "what you know. Rules:\n"
    "  - Use ISO date format YYYY-MM-DD.\n"
    "  - For any date or location you are NOT certain about, set the field to "
    "null and set confidence to 'low'.\n"
    "  - Never fabricate registration windows. If you only know the race day, "
    "leave both registration dates as null.\n"
    "  - Tags must be concise (2-6 chars each), lower-case where possible, "
    "and reflect distinguishing features (distance, terrain, certification, "
    "season, time-of-day, course-profile, etc.).\n"
    "  - Summary must be one Chinese sentence (10-300 chars), factual and neutral.\n"
    "Always call the submit_race_info tool exactly once."
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
    confidence: str
    model: str
    usage: dict


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
) -> dict:
    """Synchronous SDK call — invoked via asyncio.to_thread()."""
    message = client.messages.create(
        model=chosen_model,
        max_tokens=chosen_max_tokens,
        system=RACE_SYSTEM_PROMPT,
        tools=[RACE_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": RACE_TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"请分析以下比赛并按 schema 返回结构化信息：\n\n{race_name}\n\n"
                            "如果不确定某项事实，对应字段填 null，并把 confidence 设为 low。"
                        ),
                    }
                ],
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
        try:
            info = await asyncio.to_thread(
                _call_race_one_sync, race_name, chosen_model, chosen_max_tokens
            )
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

    started = time.perf_counter()
    results = await asyncio.gather(
        *[
            _analyze_race_one(name, chosen_model, chosen_max_tokens, sem)
            for name in race_names
        ]
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    success = sum(1 for r in results if isinstance(r, RaceItemResult))
    failed = len(results) - success
    log.info("race batch done: %d success, %d failed in %dms", success, failed, elapsed_ms)

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
