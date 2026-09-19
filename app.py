"""MiniMax Anthropic SDK demo HTTP service.

Exposes a single ``/hello`` endpoint that forwards the text to the
MiniMax-M3 model through the Anthropic-compatible API and returns the
model's reply. The endpoint accepts both JSON (POST) and query string
(GET) inputs so it is easy to smoke-test with ``curl``.

Configuration is read from environment variables (see ``.env.example``):

* ``ANTHROPIC_BASE_URL`` — the MiniMax Anthropic-compatible base URL
* ``ANTHROPIC_API_KEY``  — your MiniMax subscription key
* ``MINIMAX_MODEL``      — model name, defaults to ``MiniMax-M3``
* ``MAX_TOKENS``         — default ``max_tokens`` for the request
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
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

if not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill in your key."
    )

# Single shared client — the underlying httpx client handles connection pooling.
client = anthropic.Anthropic(
    base_url=ANTHROPIC_BASE_URL,
    api_key=ANTHROPIC_API_KEY,
)

app = FastAPI(
    title="MiniMax Anthropic Demo",
    description="Tiny HTTP service that proxies text to MiniMax models via the Anthropic SDK.",
    version="0.1.0",
)


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


def _call_model(
    text: str,
    system: Optional[str],
    model: Optional[str],
    max_tokens: Optional[int],
) -> dict:
    """Call MiniMax through the Anthropic SDK and return a normalised dict."""
    chosen_model = model or DEFAULT_MODEL
    chosen_max_tokens = max_tokens or DEFAULT_MAX_TOKENS

    log.info("calling model=%s max_tokens=%s text_len=%d", chosen_model, chosen_max_tokens, len(text))

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


@app.get("/")
def root() -> dict:
    return {
        "service": "minimax-anthropic-demo",
        "model": DEFAULT_MODEL,
        "base_url": ANTHROPIC_BASE_URL,
        "endpoints": {
            "POST /hello": '{"text": "your prompt"}',
            "GET  /hello": "?text=your+prompt",
            "GET  /healthz": "liveness probe",
        },
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/hello")
def hello_post(req: HelloRequest) -> JSONResponse:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="'text' must not be empty")
    result = _call_model(
        text=req.text,
        system=req.system,
        model=req.model,
        max_tokens=req.max_tokens,
    )
    return JSONResponse(result)


@app.get("/hello")
def hello_get(
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
    return JSONResponse(result)