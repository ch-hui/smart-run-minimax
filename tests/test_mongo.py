"""MongoDB connectivity & CRUD smoke test.

Runs against the deployed MongoDB instance to verify a client can:
  1. Open an authenticated connection
  2. Insert a document into a dedicated ``smoke_test`` collection
  3. Read it back
  4. Count documents
  5. Clean up the inserted document (idempotent reruns)

Configuration is read from environment variables (and the local ``.env``
if present, via python-dotenv). Credentials are required — pass them
via env so they stay out of source control:

    export MONGO_URL='mongodb://minimax_admin:YOUR_PASS@47.121.29.106:27017/minimax_demo?authSource=admin'
    python tests/test_mongo.py

A ``MONGO_URL`` default is provided for the deployed demo instance
(IP only, no credentials); you still need to supply ``MONGO_PASS``.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv(override=True)

# Demo instance IP. Override via env if you're testing locally.
DEFAULT_MONGO_URL = "mongodb://minimax_admin:{password}@47.121.29.106:27017/minimax_demo?authSource=admin"
DEMO_HOST = "47.121.29.106"

MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    password = os.getenv("MONGO_PASS")
    if not password:
        print(
            "ERROR: set MONGO_URL (or at least MONGO_PASS) in your environment "
            "or .env file before running this test.",
            file=sys.stderr,
        )
        sys.exit(2)
    MONGO_URL = DEFAULT_MONGO_URL.format(password=password)

DATABASE_NAME = os.getenv("MONGO_DB", "minimax_demo")
SMOKE_COLLECTION = "smoke_test"


def _stamp(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def main() -> int:
    _stamp(f"connecting to {MONGO_URL.replace(password, '***') if 'password' in dir() else MONGO_URL}")
    # Redact credentials before logging
    safe_url = MONGO_URL
    if "@" in safe_url and "://" in safe_url:
        scheme, rest = safe_url.split("://", 1)
        creds, host = rest.split("@", 1)
        safe_url = f"{scheme}://***@{host}"
    _stamp(f"redacted url: {safe_url}")

    client: MongoClient
    try:
        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        ping_result = client.admin.command("ping")
        _stamp(f"ping ok: {ping_result}")
    except PyMongoError as exc:
        _stamp(f"FAIL: connection error: {exc}")
        return 1

    db = client[DATABASE_NAME]
    smoke = db[SMOKE_COLLECTION]

    marker = f"smoke-{uuid.uuid4().hex[:12]}"
    doc = {
        "marker": marker,
        "created_at": datetime.now(timezone.utc),
        "host_target": DEMO_HOST,
        "note": "inserted by tests/test_mongo.py",
    }

    try:
        inserted_id = smoke.insert_one(doc).inserted_id
        _stamp(f"insert ok: _id={inserted_id} marker={marker}")
    except PyMongoError as exc:
        _stamp(f"FAIL: insert error: {exc}")
        return 1

    try:
        found = smoke.find_one({"marker": marker})
        if not found:
            _stamp("FAIL: inserted document not found")
            return 1
        _stamp(f"read ok: marker={found['marker']} created_at={found['created_at'].isoformat()}")
    except PyMongoError as exc:
        _stamp(f"FAIL: find error: {exc}")
        return 1

    try:
        total = smoke.count_documents({})
        _stamp(f"count ok: {SMOKE_COLLECTION} now has {total} docs total")
    except PyMongoError as exc:
        _stamp(f"WARN: count error: {exc}")

    # Cleanup: delete just our marker, leave any other data alone.
    try:
        deleted = smoke.delete_one({"marker": marker}).deleted_count
        _stamp(f"cleanup ok: removed {deleted} doc(s) with marker={marker}")
    except PyMongoError as exc:
        _stamp(f"WARN: cleanup error: {exc}")

    client.close()
    _stamp("ALL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    # Quick connectivity warm-up message so the user sees output even on a hang
    print(f"mongo smoke test starting in {int(time.time())}", flush=True)
    sys.exit(main())