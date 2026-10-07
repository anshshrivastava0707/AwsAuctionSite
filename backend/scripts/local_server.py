"""Run the whole backend on one machine, with no AWS account, for clicking through the UI.

  python scripts/local_server.py [--host 0.0.0.0] [--http-port 8000] [--ws-port 8001]
                                 [--admin-email admin@local.test]

The real Lambda handlers run unchanged. Only the AWS pieces around them are stood in for:
  - DynamoDB              -> moto, in memory (state is lost on restart)
  - HTTP API              -> a small http.server that builds API Gateway v2 events
  - WebSocket API         -> a `websockets` server; postToConnection writes to the local socket
  - DynamoDB Stream       -> a poller that diffs the Auctions table and feeds stream.handler
  - EventBridge Scheduler -> the same poller closes auctions once endsAt has passed
  - S3 (images)           -> a temp directory; uploads are signed PUTs to /local-upload/<key>
  - Login                 -> AUTH_MODE=dev. Log in as --admin-email to approve other accounts.

This is for demos only. moto is not atomic across threads, so (as in the tests) every backend
operation is serialised. Concurrency is proven by scripts/concurrency_test.py against AWS.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "local",
    "AWS_SECRET_ACCESS_KEY": "local",
    "WS_ENDPOINT": "https://local/dev",
    "AUTH_MODE": "dev",
    # Fresh per run: the in-memory data is gone on restart, so old logins should be too.
    "DEV_AUTH_SECRET": secrets.token_hex(32),
})
for var in ("SCHEDULER_ROLE_ARN", "CLOSE_FUNCTION_ARN", "AWS_PROFILE", "IMAGES_BUCKET"):
    os.environ.pop(var, None)

import boto3  # noqa: E402
import websockets  # noqa: E402
from boto3.dynamodb.types import TypeSerializer  # noqa: E402
from moto import mock_aws  # noqa: E402
from moto.dynamodb.models import DynamoDBBackend  # noqa: E402

from auction import config, connections, repository, storage  # noqa: E402
from auction.models import IMAGE_KEY_RE, now_ms  # noqa: E402
from devtools import tables  # noqa: E402
from handlers import http as http_handler  # noqa: E402
from handlers import stream as stream_handler  # noqa: E402
from handlers import ws as ws_handler  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("local")
logging.getLogger("handlers").setLevel(logging.WARNING)
_ser = TypeSerializer()


# ------------------------------------------------------------------ DynamoDB (moto)

def start_dynamodb() -> None:
    mock_aws().start()
    config.reset_clients()
    # Emulate DynamoDB's per-request atomicity (see tests/test_bidding.py::atomic_moto).
    lock = threading.RLock()
    for name in ("get_item", "put_item", "update_item", "delete_item", "query", "scan",
                 "transact_write_items", "transact_get_items", "batch_get_item", "batch_write_item"):
        fn = getattr(DynamoDBBackend, name)

        def locked(*a, _fn=fn, **kw):
            with lock:
                return _fn(*a, **kw)
        setattr(DynamoDBBackend, name, locked)
    os.environ.update(tables.create_all(boto3.client("dynamodb")))


# ------------------------------------------------------------------ S3 stand-in

class LocalStorage:
    """Implements storage.backend with files on disk and HMAC-signed upload URLs."""

    def __init__(self, root: Path):
        self.root = root
        self.secret = secrets.token_bytes(32)

    def _sig(self, key: str, content_type: str, exp: int) -> str:
        return hmac.new(self.secret, f"{key}|{content_type}|{exp}".encode(), hashlib.sha256).hexdigest()

    def presign_upload(self, key: str, content_type: str) -> dict:
        exp = int(time.time()) + storage.UPLOAD_TTL_SECONDS
        sig = self._sig(key, content_type, exp)
        # Relative URL: the browser resolves it against the API base (e.g. /api).
        return {"method": "PUT", "url": f"/local-upload/{key}?exp={exp}&sig={sig}",
                "headers": {"content-type": content_type}}

    def path(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def download_url(self, key: str) -> str:
        return f"/images/{key}"

    def accept_upload(self, key: str, query: dict, content_type: str, body: bytes) -> tuple[int, str]:
        try:
            exp = int(query.get("exp", "0"))
        except ValueError:
            return 400, "bad expiry"
        if not IMAGE_KEY_RE.match(key) or exp < time.time():
            return 403, "upload URL expired or invalid"
        if not hmac.compare_digest(query.get("sig", ""), self._sig(key, content_type, exp)):
            return 403, "bad signature (content type must match the one requested)"
        if not 0 < len(body) <= storage.MAX_IMAGE_BYTES:
            return 400, "image too large"
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return 200, "ok"


# ------------------------------------------------------------------ WebSocket API

class Sockets:
    """connectionId -> live socket. Stands in for API Gateway's postToConnection."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.by_id: dict[str, websockets.ServerConnection] = {}

    def send(self, _endpoint: str, connection_id: str, payload: dict) -> bool:
        sock = self.by_id.get(connection_id)
        if sock is None:
            connections.remove(connection_id)  # what a 410 Gone does in AWS
            return False
        data = json.dumps(payload)
        asyncio.run_coroutine_threadsafe(_safe_send(sock, data), self.loop)
        return True


async def _safe_send(sock, data: str) -> None:
    try:
        await sock.send(data)
    except websockets.ConnectionClosed:
        pass


def ws_event(route: str, connection_id: str, body: str | None = None, query: dict | None = None) -> dict:
    return {
        "requestContext": {"routeKey": route, "connectionId": connection_id,
                           "domainName": "local", "stage": "dev"},
        "queryStringParameters": query or None,
        "body": body,
    }


async def ws_session(sockets: Sockets, sock) -> None:
    cid = uuid.uuid4().hex
    sockets.by_id[cid] = sock
    query = dict(parse_qsl(urlsplit(sock.request.path).query))
    await asyncio.to_thread(ws_handler.handler, ws_event("$connect", cid, query=query), None)
    try:
        async for raw in sock:
            try:
                action = json.loads(raw).get("action")
            except (json.JSONDecodeError, AttributeError):
                action = None
            route = action if action in ("subscribe", "watch", "placeBid", "ping") else "$default"
            await asyncio.to_thread(ws_handler.handler, ws_event(route, cid, raw), None)
    except websockets.ConnectionClosed:
        pass
    finally:
        sockets.by_id.pop(cid, None)
        await asyncio.to_thread(ws_handler.handler, ws_event("$disconnect", cid), None)


# ------------------------------------------------------------------ Stream + scheduler

def stream_and_scheduler_loop(interval: float = 0.1) -> None:
    """Diffs the Auctions table by version and feeds changes to stream.handler, in version
    order. Two commits inside one interval are coalesced; clients see the version gap and
    re-sync, which is the same path they use after a missed broadcast in AWS."""
    table = config.dynamodb_resource().Table(config.auctions_table_name())
    seen: dict[str, dict] = {}
    while True:
        try:
            items = table.scan(ConsistentRead=True).get("Items", [])
            records = []
            for item in items:
                aid = item["auctionId"]
                old = seen.get(aid)
                if old is None or int(old["version"]) != int(item["version"]):
                    rec = {"eventName": "MODIFY" if old else "INSERT",
                           "dynamodb": {"NewImage": {k: _ser.serialize(v) for k, v in item.items()}}}
                    if old:
                        rec["dynamodb"]["OldImage"] = {k: _ser.serialize(v) for k, v in old.items()}
                    records.append(rec)
                    seen[aid] = item
                if item["status"] == "OPEN" and int(item["endsAt"]) <= now_ms():
                    if repository.close_auction(aid):
                        log.info("closed auction %s", aid)
            if records:
                stream_handler.handler({"Records": records}, None)
        except Exception:
            log.exception("stream poller error")
        time.sleep(interval)


# ------------------------------------------------------------------ HTTP API

_IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}


class HttpApi(BaseHTTPRequestHandler):
    store: LocalStorage

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type, authorization")

    def _body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"")

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self):  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        query = dict(parse_qsl(url.query))

        # S3 stand-in: serve and accept image files directly.
        if method == "GET" and url.path.startswith("/images/"):
            key = url.path[len("/images/"):]
            if not IMAGE_KEY_RE.match(key) or not self.store.exists(key):
                return self._send(404, b"not found")
            return self._send(200, self.store.path(key).read_bytes(),
                              {"Content-Type": _IMAGE_TYPES[key.rsplit(".", 1)[1]],
                               "Cache-Control": "private, max-age=600"})
        if method == "PUT" and url.path.startswith("/local-upload/"):
            key = url.path[len("/local-upload/"):]
            status, msg = self.store.accept_upload(key, query, self.headers.get("content-type", ""), self._body())
            return self._send(status, msg.encode())

        body = self._body().decode() if method in ("POST", "PUT", "PATCH", "DELETE") else ""
        event = {
            "rawPath": url.path,
            "requestContext": {"http": {"method": method, "path": url.path}},
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "queryStringParameters": query or None,
            "body": body or None,
        }
        resp = http_handler.handler(event, None)
        self._send(resp["statusCode"], resp.get("body", "").encode(), resp.get("headers"))

    def log_message(self, fmt, *args):
        log.info("http %s", fmt % args)


# ------------------------------------------------------------------ main

async def main(host: str, http_port: int, ws_port: int, admin_email: str) -> None:
    os.environ["ADMIN_EMAILS"] = admin_email
    start_dynamodb()
    upload_dir = Path(tempfile.mkdtemp(prefix="auction-uploads-"))
    HttpApi.store = storage.backend = LocalStorage(upload_dir)
    sockets = Sockets(asyncio.get_running_loop())
    connections.send = sockets.send  # used by ws._reply and connections.broadcast

    threading.Thread(target=stream_and_scheduler_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host, http_port), HttpApi)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        async with websockets.serve(lambda s: ws_session(sockets, s), host, ws_port):
            log.info("HTTP API  -> http://%s:%d", host, http_port)
            log.info("WebSocket -> ws://%s:%d", host, ws_port)
            log.info("Admin login: %s (approves other accounts at /admin)", admin_email)
            await asyncio.Future()
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=8000)
    p.add_argument("--ws-port", type=int, default=8001)
    p.add_argument("--admin-email", default="admin@local.test")
    a = p.parse_args()
    try:
        asyncio.run(main(a.host, a.http_port, a.ws_port, a.admin_email.strip().lower()))
    except KeyboardInterrupt:
        pass
