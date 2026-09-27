#!/usr/bin/env python3
"""Local Shopee Affiliate custom-link queue and web application."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.getenv("AFFILIATE_DB", str(DATA_DIR / "affiliate.sqlite3")))
COOKIE_FILE = Path(
    os.getenv(
        "SHOPEE_COOKIE_FILE",
        str(ROOT / "Credential" / "cookies.txt"),
    )
)
REQUEST_HEADERS_FILE = Path(
    os.getenv(
        "SHOPEE_REQUEST_HEADERS_FILE",
        str(ROOT / "Credential" / "request_headers.json"),
    )
)
SHOPEE_ORIGIN = os.getenv("SHOPEE_ORIGIN", "https://affiliate.shopee.co.id")
SHOPEE_GRAPHQL = SHOPEE_ORIGIN + "/api/v3/gql?q=batchCustomLink"
SHOPEE_PROFILE = SHOPEE_ORIGIN + "/api/v3/user/profile"
PORT = int(os.getenv("PORT", "8788"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "0.8"))
USER_AGENT = os.getenv(
    "SHOPEE_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
)
TRANSPORT = os.getenv("SHOPEE_TRANSPORT", "extension").lower()
BRIDGE_WAIT_SECONDS = float(os.getenv("SHOPEE_BRIDGE_WAIT_SECONDS", "180"))

GRAPHQL_QUERY = """
    query batchGetCustomLink($linkParams: [CustomLinkParam!], $sourceCaller: SourceCaller){
      batchCustomLink(linkParams: $linkParams, sourceCaller: $sourceCaller){
        shortLink
        longLink
        failCode
      }
    }
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status TEXT NOT NULL,
  links_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  result_json TEXT,
  error TEXT,
  attempt INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
"""


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    except BaseException:
        if conn is not None:
            conn.rollback()
        raise
    else:
        if conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # A daemon restart must not leave a lease permanently stuck in running.
        conn.execute(
            "UPDATE jobs SET status='queued', updated_at=? WHERE status='running'",
            (now(),),
        )


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_cookie_file() -> str:
    if not COOKIE_FILE.exists():
        raise RuntimeError(f"Cookie file tidak ditemukan: {COOKIE_FILE}")
    value = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("Cookie file kosong")
    # The supplied export is a browser Cookie header, not a Netscape cookie jar.
    if value.startswith("#"):
        value = "".join(line.strip() for line in value.splitlines() if not line.startswith("#"))
    return value


def captured_headers() -> dict[str, str]:
    if not REQUEST_HEADERS_FILE.exists():
        return {}
    try:
        raw = json.loads(REQUEST_HEADERS_FILE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Header rekaman tidak valid: {exc}") from exc


def request_json(url: str, *, method: str = "GET", body: Any = None, headers: dict[str, str]) -> tuple[int, dict[str, str], Any]:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, dict(response.headers.items()), json.loads(raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"raw": raw[:1000]}
        return exc.code, dict(exc.headers.items()), decoded
    except URLError as exc:
        raise RuntimeError(f"Tidak dapat terhubung ke Shopee: {exc.reason}") from exc


def api_headers(cookie: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; charset=utf-8",
        "Affiliate-Program-Type": "1",
        "Origin": SHOPEE_ORIGIN,
        "Referer": SHOPEE_ORIGIN + "/offer/custom_link",
        "User-Agent": USER_AGENT,
        "Cookie": cookie,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if extra:
        # Header names from Chrome exports are preserved because Shopee's gateway
        # treats several anti-crawler headers as case-insensitive but value-sensitive.
        for key, value in extra.items():
            for existing in list(headers):
                if existing.lower() == key.lower():
                    del headers[existing]
            if key.lower() not in {"cookie", "host", "content-length"}:
                headers[key] = value
    return headers


def refresh_csrf(cookie: str, headers: dict[str, str]) -> str | None:
    status, response_headers, _ = request_json(
        SHOPEE_PROFILE,
        headers=api_headers(cookie, headers),
    )
    if status != 200:
        return None
    for key, value in response_headers.items():
        if key.lower() == "csrf-token":
            return value
    return None


def create_links(links: list[str], tags: list[str], *, job_id: str | None = None) -> list[dict[str, Any]]:
    if TRANSPORT == "extension":
        if not job_id:
            raise RuntimeError("Job ID wajib untuk extension bridge")
        return create_links_via_extension(links, tags, job_id)
    if TRANSPORT == "browser":
        return create_links_in_browser(links, tags)
    if TRANSPORT != "http":
        raise RuntimeError("SHOPEE_TRANSPORT harus browser atau http")
    cookie = parse_cookie_file()
    extra = captured_headers()
    csrf = refresh_csrf(cookie, extra)
    request_headers = api_headers(cookie, extra)
    if csrf:
        request_headers["CSRF-token"] = csrf
    body = {
        "operationName": "batchGetCustomLink",
        "query": GRAPHQL_QUERY,
        "variables": {
            "linkParams": [
                {
                    "originalLink": link,
                    "advancedLinkParams": {
                        f"subId{i + 1}": tag
                        for i, tag in enumerate(tags)
                        if tag
                    },
                }
                for link in links
            ],
            "sourceCaller": "CUSTOM_LINK_CALLER",
        },
    }
    status, _, payload = request_json(SHOPEE_GRAPHQL, method="POST", body=body, headers=request_headers)
    if status in (401, 403) or payload.get("error") == 90309999:
        raise SessionExpired(
            "Shopee menolak sesi atau header anti-bot (90309999). "
            "Segarkan halaman Link Khusus di Chrome agar rekaman header terbaru tersimpan."
        )
    if status < 200 or status >= 300:
        raise RuntimeError(f"Shopee HTTP {status}: {json.dumps(payload)[:500]}")
    rows = payload.get("data", {}).get("batchCustomLink")
    if not isinstance(rows, list):
        raise RuntimeError(f"Respons Shopee tidak berisi batchCustomLink: {json.dumps(payload)[:500]}")
    return rows


def create_links_via_extension(links: list[str], tags: list[str], job_id: str) -> list[dict[str, Any]]:
    global BRIDGE_CURRENT
    event = threading.Event()
    lease_token = secrets.token_urlsafe(24)
    with BRIDGE_CONDITION:
        if BRIDGE_CURRENT is not None:
            raise RuntimeError("Extension bridge sedang menangani job lain")
        BRIDGE_CURRENT = {
            "event": event,
            "job_id": job_id,
            "links": links,
            "tags": tags,
            "lease_token": lease_token,
            "delivered": False,
            "response": None,
        }
        BRIDGE_CONDITION.notify_all()
    try:
        if not event.wait(BRIDGE_WAIT_SECONDS):
            raise SessionExpired(
                "Extension bridge tidak menyelesaikan job. Buka tab Link Khusus Shopee "
                "yang sudah login dan selesaikan verifikasi manual bila diminta."
            )
        with BRIDGE_CONDITION:
            response = dict(BRIDGE_CURRENT.get("response") or {}) if BRIDGE_CURRENT else {}
        if response.get("error"):
            message = response["error"]
            if response.get("error_kind") == "needs_session":
                raise SessionExpired(message)
            raise RuntimeError(message)
        results = response.get("results")
        if not isinstance(results, list) or len(results) != len(links):
            raise RuntimeError("Extension bridge mengembalikan jumlah hasil yang tidak sesuai")
        return results
    finally:
        with BRIDGE_CONDITION:
            if BRIDGE_CURRENT and BRIDGE_CURRENT["job_id"] == job_id:
                BRIDGE_CURRENT = None
                BRIDGE_CONDITION.notify_all()


def create_links_in_browser(links: list[str], tags: list[str]) -> list[dict[str, Any]]:
    """Use Shopee's own page so its JavaScript can produce current anti-bot headers."""
    command = [
        "node",
        str(ROOT / "browser_worker.js"),
        json.dumps(links),
        json.dumps(tags),
    ]
    env = os.environ.copy()
    env["SHOPEE_COOKIE_FILE"] = str(COOKIE_FILE)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=150, env=env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Browser worker melewati batas 150 detik") from exc
    output = completed.stdout.strip().splitlines()
    decoded: dict[str, Any] = {}
    if output:
        try:
            decoded = json.loads(output[-1])
        except json.JSONDecodeError:
            pass
    if completed.returncode != 0 or "error" in decoded:
        message = decoded.get("error") or completed.stderr[-500:] or "Browser worker gagal"
        if "Sesi Shopee" in message or "verifikasi anti-bot" in message or "login" in message.lower():
            raise SessionExpired(message)
        raise RuntimeError(message)
    results = decoded.get("results")
    if not isinstance(results, list):
        raise RuntimeError("Browser worker tidak mengembalikan hasil")
    return results


class SessionExpired(RuntimeError):
    pass


def validate_job(body: Any) -> tuple[list[str], list[str]]:
    if not isinstance(body, dict):
        raise ValueError("Body harus berupa object JSON")
    raw_links = body.get("links")
    raw_tags = body.get("tags", [])
    if isinstance(raw_links, str):
        links = [line.strip() for line in raw_links.splitlines() if line.strip()]
    elif isinstance(raw_links, list):
        links = [str(item).strip() for item in raw_links if str(item).strip()]
    else:
        raise ValueError("links wajib berupa URL per baris atau array")
    tags = [str(item).strip() for item in raw_tags] if isinstance(raw_tags, list) else []
    if not links or len(links) > 5:
        raise ValueError("Masukkan 1 sampai 5 URL")
    if len(tags) > 5:
        raise ValueError("Maksimal 5 tag")
    tags += [""] * (5 - len(tags))
    for link in links:
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"URL tidak valid: {link}")
        if "shopee.co.id" not in parsed.netloc.lower():
            raise ValueError("Semua URL harus berasal dari Shopee Indonesia")
    for tag in tags:
        if tag and (len(tag) > 64 or not re.fullmatch(r"[A-Za-z0-9]+", tag)):
            raise ValueError("Tag hanya boleh alfanumerik, maksimal 64 karakter")
    return links, tags


def job_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "links": json.loads(row["links_json"]),
        "tags": json.loads(row["tags_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"],
        "attempt": row["attempt"],
    }


class Worker(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="affiliate-worker")
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()

    def wake(self) -> None:
        self.wake_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                row = self.claim()
            except sqlite3.OperationalError as exc:
                print(f"[worker] database belum tersedia: {exc}", flush=True)
                self.wake_event.wait(POLL_SECONDS)
                self.wake_event.clear()
                continue
            if row is None:
                self.wake_event.wait(POLL_SECONDS)
                self.wake_event.clear()
                continue
            try:
                self.process(row)
            except sqlite3.OperationalError as exc:
                print(f"[worker] gagal menyimpan status job: {exc}", flush=True)

    def claim(self) -> sqlite3.Row | None:
        if TRANSPORT == "extension" and not bridge_is_connected():
            return None
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status='running', attempt=attempt+1, updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
            return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()

    def process(self, row: sqlite3.Row) -> None:
        try:
            result = create_links(
                json.loads(row["links_json"]),
                json.loads(row["tags_json"]),
                job_id=row["id"],
            )
            status = "completed" if all(item.get("failCode", 0) == 0 for item in result) else "failed"
            error = None if status == "completed" else "Shopee mengembalikan failCode pada salah satu link"
            self.finish(row["id"], status, result, error)
        except SessionExpired as exc:
            self.finish(row["id"], "needs_session", None, str(exc))
        except Exception as exc:
            attempts = int(row["attempt"])
            if attempts < MAX_RETRIES:
                self.finish(row["id"], "queued", None, f"Percobaan {attempts} gagal: {exc}")
            else:
                self.finish(row["id"], "failed", None, str(exc))

    @staticmethod
    def finish(job_id: str, status: str, result: Any, error: str | None) -> None:
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, result_json=?, error=?, updated_at=? WHERE id=?",
                (status, json.dumps(result) if result is not None else None, error, now(), job_id),
            )


WORKER = Worker()

BRIDGE_CONDITION = threading.Condition()
BRIDGE_CURRENT: dict[str, Any] | None = None
BRIDGE_LAST_SEEN: dict[str, Any] = {}


def bridge_is_connected() -> bool:
    with BRIDGE_CONDITION:
        seen = float(BRIDGE_LAST_SEEN.get("timestamp", 0))
        return bool(BRIDGE_LAST_SEEN.get("ready")) and time.time() - seen < 10


def bridge_ping(body: Any) -> dict[str, Any]:
    ready = bool(isinstance(body, dict) and body.get("ready"))
    url = str(body.get("url", ""))[:500] if isinstance(body, dict) else ""
    with BRIDGE_CONDITION:
        BRIDGE_LAST_SEEN.update({"timestamp": time.time(), "ready": ready, "url": url})
    WORKER.wake()
    return {"ok": True, "ready": ready}


def bridge_next() -> dict[str, Any]:
    with BRIDGE_CONDITION:
        current = BRIDGE_CURRENT
        if not current or current["event"].is_set():
            return {"job": None}
        current["delivered"] = True
        return {
            "job": {
                "id": current["job_id"],
                "links": current["links"],
                "tags": current["tags"],
            },
            "lease_token": current["lease_token"],
        }


def bridge_result(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("Body bridge harus berupa object JSON")
    job_id = str(body.get("job_id", ""))
    lease_token = str(body.get("lease_token", ""))
    with BRIDGE_CONDITION:
        current = BRIDGE_CURRENT
        if not current or current["job_id"] != job_id or current["lease_token"] != lease_token:
            raise ValueError("Lease bridge tidak valid atau sudah kedaluwarsa")
        current["response"] = {
            "results": body.get("results"),
            "error": str(body.get("error", ""))[:500],
            "error_kind": str(body.get("error_kind", ""))[:40],
        }
        current["event"].set()
        BRIDGE_CONDITION.notify_all()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "FahraAffiliateRotator/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}")

    def send_json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if self.path.startswith("/api/bridge/"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("Request terlalu besar")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            with BRIDGE_CONDITION:
                last_seen = float(BRIDGE_LAST_SEEN.get("timestamp", 0))
                connected = bridge_is_connected()
                bridge = {
                    "connected": connected,
                    "ready": connected,
                    "last_seen": last_seen or None,
                    "busy": BRIDGE_CURRENT is not None,
                }
            self.send_json({"status": "ok", "worker": WORKER.is_alive(), "bridge": bridge, "database": str(DB_PATH)})
            return
        if parsed.path == "/api/bridge/next":
            self.send_json(bridge_next())
            return
        if parsed.path == "/api/jobs":
            with db() as conn:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
            self.send_json({"jobs": [job_dict(row) for row in rows]})
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with db() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                self.send_json({"error": "Job tidak ditemukan"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(job_dict(row))
            return
        if parsed.path in {"/", "/index.html"}:
            self.send_html(INDEX_HTML)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        if self.path.startswith("/api/bridge/"):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/bridge/ping":
            try:
                self.send_json(bridge_ping(self.read_json()))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/bridge/result":
            try:
                self.send_json(bridge_result(self.read_json()))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if self.path != "/api/jobs":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            links, tags = validate_job(self.read_json())
            job_id = uuid.uuid4().hex
            with db() as conn:
                conn.execute(
                    "INSERT INTO jobs(id,status,links_json,tags_json) VALUES(?,?,?,?)",
                    (job_id, "queued", json.dumps(links), json.dumps(tags)),
                )
            WORKER.wake()
            self.send_json({"id": job_id, "status": "queued"}, HTTPStatus.ACCEPTED)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


INDEX_HTML = r'''<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Link khusus Shopee</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&display=swap');
    :root { color-scheme: dark; --stage:#0e0d0c; --stage-2:#1a1714; --ink:#1a1714; --light:#f5efe4; --muted:#746d66; --paper:#fffdf8; --line:#ebe6dd; --accent:#e98425; --accent-2:#ff6b3d; --warn:#8a4b16; --error:#9a2d2d; --serif:'Instrument Serif',Georgia,serif; --sans:Inter,system-ui,sans-serif; --mono:'IBM Plex Mono',ui-monospace,monospace; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(ellipse 80% 50% at 50% -10%,rgba(233,132,37,.18),transparent 70%),var(--stage); color:var(--light); font:14px/1.5 var(--sans); }
    main { width:min(1180px, calc(100% - 44px)); margin:0 auto 64px; }
    header { position:relative; display:flex; justify-content:space-between; gap:24px; align-items:end; margin:0 -4px 34px; padding:24px 0 0; }
    header::before { content:'FAHRA AFFILIATE ROTATOR'; position:absolute; top:24px; left:0; font:11px/1 var(--mono); letter-spacing:.16em; color:rgba(245,239,228,.54); }
    h1 { font:italic 700 clamp(2.4rem,5vw,4.8rem)/.98 var(--serif); letter-spacing:-.02em; margin:48px 0 12px; max-width:11ch; }
    h1::first-line { color:var(--light); }
    .dek { max-width:580px; color:rgba(245,239,228,.66); margin:0; font-size:15px; }
    .signal { min-width:0; flex:0 1 220px; padding:12px 14px; border:1px solid rgba(245,239,228,.18); background:var(--stage-2); color:var(--light); border-radius:10px; font:12px/1.4 var(--mono); }
    .layout { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr); gap:22px; align-items:start; }
    section { min-width:0; background:var(--paper); border:0; border-radius:18px; padding:24px; color:var(--ink); box-shadow:0 18px 40px rgba(0,0,0,.18); }
    h2 { font:700 1.15rem/1.2 var(--sans); margin:0 0 15px; }
    label { display:block; font-weight:650; margin:15px 0 6px; }
    textarea,input { width:100%; min-height:44px; border:1px solid #cfc7bd; border-radius:8px; background:#fff; color:var(--ink); padding:11px 12px; font:inherit; }
    textarea { min-height:160px; resize:vertical; }
    input:focus,textarea:focus,button:focus-visible { outline:3px solid #a7d8c7; outline-offset:2px; }
    .tags { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; }
    .tags input { min-width:0; }
    button { min-height:44px; margin-top:18px; border:0; border-radius:999px; background:var(--ink); color:var(--light); padding:13px 18px; font-weight:700; cursor:pointer; }
    button:hover { background:#302923; }
    button:disabled { opacity:.55; cursor:wait; }
    .hint { color:var(--muted); font-size:.88rem; margin:7px 0 0; }
    .jobs { display:grid; gap:11px; }
    .job { min-width:0; border-top:1px solid var(--line); padding-top:12px; }
    .job:first-child { border-top:0; padding-top:0; }
    .job-head { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:start; }
    .job-head > div { min-width:0; }
    .job-source { display:-webkit-box; min-width:0; overflow:hidden; color:var(--ink); font-weight:700; line-height:1.35; overflow-wrap:anywhere; text-decoration:none; -webkit-box-orient:vertical; -webkit-line-clamp:2; }
    .job-source:hover { color:var(--accent); text-decoration:underline; }
    .job-id { font-family:var(--mono); font-size:.78rem; color:var(--muted); }
    .status { font:500 .72rem/1 var(--mono); text-transform:uppercase; letter-spacing:.04em; }
    .completed { color:#207044; } .failed,.needs_session { color:var(--error); } .queued,.running { color:var(--warn); }
    .result { min-width:0; margin-top:9px; display:grid; gap:6px; }
    .result > div { min-width:0; }
    .result a { display:inline-block; max-width:100%; color:var(--accent); overflow-wrap:anywhere; }
    .result a.copied { color:#207044; }
    .copy-feedback { color:#207044; font-size:.8rem; margin-left:6px; }
    .error { color:var(--error); font-size:.9rem; }
    .empty { color:var(--muted); padding:22px 0; }
    .pagination { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:17px; }
    .pagination-list { display:flex; min-width:0; flex:1 1 auto; justify-content:center; flex-wrap:wrap; gap:6px; }
    .pagination button { min-width:44px; min-height:44px; margin:0; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); padding:9px 11px; }
    .pagination button:hover { background:#f3eee6; }
    .pagination button[aria-current="page"] { background:var(--ink); color:var(--light); }
    .pagination button:disabled { opacity:.4; cursor:not-allowed; }
    .pagination .pagination-control { white-space:nowrap; }
    @media (max-width: 860px) {
      main { width:min(100% - 32px, 680px); margin-top:24px; margin-bottom:48px; }
      header { display:block; margin-bottom:26px; }
      .signal { width:100%; margin-top:18px; }
      .layout { grid-template-columns:1fr; gap:16px; }
    }
    @media (max-width: 560px) {
      main { width:calc(100% - 24px); margin-top:16px; }
      header { margin:0 0 22px; padding-top:16px; }
      header::before { top:16px; font-size:10px; letter-spacing:.12em; }
      h1 { font-size:clamp(2.35rem, 13vw, 3.35rem); margin:40px 0 10px; max-width:9ch; }
      .dek { font-size:14px; }
      section { border-radius:14px; padding:18px; }
      textarea { min-height:132px; }
      .tags { grid-template-columns:repeat(2,minmax(0,1fr)); }
      button { width:100%; margin-top:16px; }
      .pagination { flex-wrap:wrap; }
      .pagination button { width:auto; margin-top:0; }
      .pagination .pagination-control { flex:1 1 calc(50% - 5px); }
      .pagination-list { order:3; flex-basis:100%; }
    }
    @media (max-width: 380px) {
      main { width:calc(100% - 20px); }
      section { padding:16px; }
      .tags { grid-template-columns:1fr; }
      .job-head { grid-template-columns:1fr; gap:6px; }
      .status { justify-self:start; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>Buat link yang <span style="color:var(--accent)">bekerja.</span></h1><p class="dek">Masukkan sampai lima URL produk atau halaman Shopee, beri tag pelacakan, lalu biarkan worker mengantrekan hasilnya.</p></div>
    <div class="signal" id="health" role="status">Memeriksa worker...</div>
  </header>
  <div class="layout">
    <section aria-labelledby="create-title">
      <h2 id="create-title">Buat batch link</h2>
      <form id="job-form">
        <label for="links">URL Shopee</label>
        <textarea id="links" name="links" required placeholder="Satu URL per baris"></textarea>
        <p class="hint">URL harus berasal dari shopee.co.id. Maksimal lima baris per job.</p>
        <label>Tag pelacakan</label>
        <div class="tags">
          <input name="tag1" aria-label="Tag ke 1" placeholder="Tag 1">
          <input name="tag2" aria-label="Tag ke 2" placeholder="Tag 2">
          <input name="tag3" aria-label="Tag ke 3" placeholder="Tag 3">
          <input name="tag4" aria-label="Tag ke 4" placeholder="Tag 4">
          <input name="tag5" aria-label="Tag ke 5" placeholder="Tag 5">
        </div>
        <p class="hint">Gunakan huruf dan angka saja. Tag akan masuk ke parameter subId Shopee.</p>
        <button id="submit" type="submit">Masukkan ke worker</button>
        <p id="form-message" class="hint" role="status"></p>
      </form>
    </section>
    <section aria-labelledby="jobs-title">
      <h2 id="jobs-title">Job terakhir</h2>
      <div id="jobs" class="jobs"><p class="empty">Belum ada job.</p></div>
      <nav id="jobs-pagination" class="pagination" aria-label="Navigasi riwayat job"></nav>
    </section>
  </div>
</main>
<script>
const $ = (s) => document.querySelector(s);
const JOBS_PER_PAGE = 5;
let jobsCache = [];
let currentPage = 1;
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function sourceLabel(value) {
  try {
    const url = new URL(value);
    return url.pathname.replace(/^\/+/, '') || url.hostname;
  } catch (_) {
    return value;
  }
}
function renderJob(job) {
  const results = Array.isArray(job.result) ? job.result.map((row, i) => row.shortLink ? `<div><span>${i + 1}. </span><a class="copy-link" href="${escapeHtml(row.shortLink)}" data-copy="${escapeHtml(row.shortLink)}" target="_blank" rel="noreferrer" aria-label="Salin short link ${escapeHtml(row.shortLink)}">${escapeHtml(row.shortLink)}</a><span class="copy-feedback" role="status" aria-live="polite"></span></div>` : `<div>${i + 1}. Gagal, failCode ${escapeHtml(row.failCode ?? '?')}</div>`).join('') : '';
  const source = String(job.links?.[0] || '');
  return `<article class="job"><div class="job-head"><div><a class="job-source" href="${escapeHtml(source)}" target="_blank" rel="noreferrer" title="${escapeHtml(source)}" aria-label="Buka URL sumber: ${escapeHtml(source)}">${escapeHtml(sourceLabel(source))}</a><div class="job-id">${escapeHtml(job.id)}</div></div><span class="status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span></div>${results ? `<div class="result">${results}</div>` : ''}${job.error ? `<p class="error">${escapeHtml(job.error)}</p>` : ''}</article>`;
}
function renderPagination() {
  const pagination = $('#jobs-pagination');
  const pageCount = Math.ceil(jobsCache.length / JOBS_PER_PAGE);
  if (pageCount <= 1) { pagination.innerHTML = ''; return; }
  const pages = Array.from({length: pageCount}, (_, i) => i + 1).map(page => `<button type="button" data-page="${page}"${page === currentPage ? ' aria-current="page"' : ''} aria-label="Halaman ${page}">${page}</button>`).join('');
  pagination.innerHTML = `<button class="pagination-control" type="button" data-page="${currentPage - 1}"${currentPage === 1 ? ' disabled' : ''}>Sebelumnya</button><div class="pagination-list">${pages}</div><button class="pagination-control" type="button" data-page="${currentPage + 1}"${currentPage === pageCount ? ' disabled' : ''}>Berikutnya</button>`;
}
function renderJobs() {
  const pageCount = Math.ceil(jobsCache.length / JOBS_PER_PAGE);
  currentPage = Math.min(Math.max(currentPage, 1), Math.max(pageCount, 1));
  const start = (currentPage - 1) * JOBS_PER_PAGE;
  const visibleJobs = jobsCache.slice(start, start + JOBS_PER_PAGE);
  $('#jobs').innerHTML = visibleJobs.length ? visibleJobs.map(renderJob).join('') : '<p class="empty">Belum ada job. Kirim URL pertama untuk memulai.</p>';
  renderPagination();
}
async function copyText(value) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch (_) {}
  const helper = document.createElement('textarea');
  helper.value = value;
  helper.setAttribute('readonly', '');
  helper.style.position = 'fixed';
  helper.style.opacity = '0';
  document.body.appendChild(helper);
  try {
    helper.select();
    return document.execCommand('copy');
  } catch (_) {
    return false;
  } finally {
    helper.remove();
  }
}
function showCopyFeedback(link, message, copied) {
  const feedback = link.parentElement.querySelector('.copy-feedback');
  if (feedback) feedback.textContent = message;
  link.classList.toggle('copied', copied);
  window.clearTimeout(link.copyFeedbackTimer);
  link.copyFeedbackTimer = window.setTimeout(() => {
    if (feedback) feedback.textContent = '';
    link.classList.remove('copied');
  }, 2200);
}
async function refresh() {
  const [health, jobs] = await Promise.all([fetch('/api/health').then(r => r.json()), fetch('/api/jobs').then(r => r.json())]);
  $('#health').textContent = health.worker ? (health.bridge?.connected ? 'Bridge aktif' : 'Menunggu extension') : 'Worker berhenti';
  jobsCache = Array.isArray(jobs.jobs) ? jobs.jobs : [];
  renderJobs();
}
$('#jobs-pagination').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-page]');
  if (!button || button.disabled) return;
  currentPage = Number(button.dataset.page);
  renderJobs();
});
$('#jobs').addEventListener('click', async (event) => {
  const link = event.target.closest('a.copy-link');
  if (!link) return;
  event.preventDefault();
  const copied = await copyText(link.dataset.copy || link.textContent.trim());
  showCopyFeedback(link, copied ? 'Link tersalin' : 'Salin manual dari link ini', copied);
});
$('#job-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = $('#submit'); button.disabled = true; $('#form-message').textContent = 'Menyimpan job...';
  const tags = [1,2,3,4,5].map(i => event.target.elements[`tag${i}`].value.trim());
  try { const response = await fetch('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({links:$('#links').value, tags})}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Job gagal dibuat'); $('#links').value=''; [1,2,3,4,5].forEach(i => event.target.elements[`tag${i}`].value=''); $('#form-message').textContent = `Job ${data.id} masuk antrean.`; await refresh(); } catch (error) { $('#form-message').textContent = error.message; } finally { button.disabled = false; }
});
refresh().catch(() => { $('#health').textContent = 'Backend tidak merespons'; });
setInterval(() => refresh().catch(() => {}), 2500);
</script>
</body>
</html>'''


def main() -> None:
    init_db()
    WORKER.start()
    server = ThreadingHTTPServer((os.getenv("HOST", "127.0.0.1"), PORT), Handler)
    print(f"Fahra Affiliate Rotator berjalan di http://{server.server_address[0]}:{PORT}")
    print(f"Cookie: {COOKIE_FILE}")
    print(f"Header rekaman hanya untuk mode HTTP diagnosis: {REQUEST_HEADERS_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WORKER.stop_event.set()
        WORKER.wake()
        server.server_close()


if __name__ == "__main__":
    main()
