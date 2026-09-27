#!/usr/bin/env python3
"""Local Shopee Affiliate custom-link queue and web application."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import subprocess
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from http.cookies import SimpleCookie
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
AUTH_COOKIE_NAME = "fahra_session"
AUTH_SESSION_TTL = max(900, int(os.getenv("AUTH_SESSION_TTL_SECONDS", "86400")))
AUTH_EMAIL = os.getenv("SUPERADMIN_EMAIL", "ramdanifahmy2023@gmail.com").strip().lower()
PASSWORD_MIN_LENGTH = 12
AUTH_RATE_LIMIT: dict[str, list[float]] = {}
AUTH_RATE_LOCK = threading.Lock()

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
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'superadmin' CHECK(role='superadmin'),
  password_hash TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);
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


def future_time(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def validate_password(value: Any) -> str:
    password = str(value or "")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password minimal {PASSWORD_MIN_LENGTH} karakter")
    if len(password) > 256:
        raise ValueError("Password terlalu panjang")
    return password


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
    return "scrypt$16384$8$1${}${}".format(salt.hex(), digest.hex())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(raw_salt),
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(bytes.fromhex(raw_digest)),
        )
        return hmac.compare_digest(digest.hex(), raw_digest)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bootstrap_admin() -> None:
    email = normalize_email(os.getenv("SUPERADMIN_EMAIL", AUTH_EMAIL))
    password = validate_password(os.getenv("SUPERADMIN_INITIAL_PASSWORD", ""))
    if not email or "@" not in email:
        raise RuntimeError("SUPERADMIN_EMAIL tidak valid")
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET role='superadmin', password_hash=?, must_change_password=1, updated_at=? WHERE id=?",
                (password_hash(password), now(), existing["id"]),
            )
            conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now(), existing["id"]))
        else:
            conn.execute(
                "INSERT INTO users(email,role,password_hash,must_change_password) VALUES(?,?,?,1)",
                (email, "superadmin", password_hash(password)),
            )
    print(f"Superadmin siap: {email}")


def parse_session_token(cookie_header: str) -> str | None:
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header or "")
    except Exception:
        return None
    morsel = cookies.get(AUTH_COOKIE_NAME)
    return morsel.value if morsel else None


def current_user(handler: "Handler", *, touch: bool = True) -> sqlite3.Row | None:
    token = parse_session_token(handler.headers.get("Cookie", ""))
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.role, u.must_change_password, s.id AS session_id
               FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
            (token_hash(token), now()),
        ).fetchone()
        if row and touch:
            conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now(), row["session_id"]))
    return row


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    current = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,last_seen,expires_at) VALUES(?,?,?,?,?)",
            (token_hash(token), user_id, current, current, future_time(AUTH_SESSION_TTL)),
        )
        conn.execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?", (current, current, user_id))
    return token


def revoke_session(handler: "Handler") -> None:
    token = parse_session_token(handler.headers.get("Cookie", ""))
    if token:
        with db() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (now(), token_hash(token)))


def login_rate_limited(key: str) -> bool:
    current = time.time()
    with AUTH_RATE_LOCK:
        recent = [stamp for stamp in AUTH_RATE_LIMIT.get(key, []) if current - stamp < 60]
        limited = len(recent) >= 8
        recent.append(current)
        AUTH_RATE_LIMIT[key] = recent[-12:]
        return limited


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
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.username or parsed.password or (hostname != "shopee.co.id" and not hostname.endswith(".shopee.co.id")):
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

    def bridge_origin(self) -> str | None:
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            return origin
        if origin in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}:
            return origin
        return None

    def send_json(self, value: Any, status: int = HTTPStatus.OK, *, set_cookie: str | None = None) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if self.path.startswith("/api/bridge/") or self.headers.get("Origin", "").startswith("chrome-extension://"):
            origin = self.bridge_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("Request terlalu besar")
        return json.loads(self.rfile.read(length) or b"{}")

    def require_user(self) -> sqlite3.Row | None:
        user = current_user(self)
        if not user:
            self.send_json({"error": "Login diperlukan"}, HTTPStatus.UNAUTHORIZED)
        return user

    def send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            if current_user(self, touch=False):
                self.send_redirect("/")
            else:
                self.send_html(LOGIN_HTML)
            return
        if parsed.path == "/api/auth/me":
            user = current_user(self)
            if not user:
                self.send_json({"error": "Login diperlukan"}, HTTPStatus.UNAUTHORIZED)
            else:
                self.send_json({"user": {"email": user["email"], "role": user["role"], "must_change_password": bool(user["must_change_password"])}})
            return
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
            self.send_json({"status": "ok", "worker": WORKER.is_alive(), "bridge": bridge})
            return
        if parsed.path == "/api/bridge/next":
            if not self.require_user():
                return
            self.send_json(bridge_next())
            return
        if parsed.path == "/api/jobs":
            if not self.require_user():
                return
            with db() as conn:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
            self.send_json({"jobs": [job_dict(row) for row in rows]})
            return
        if parsed.path.startswith("/api/jobs/"):
            if not self.require_user():
                return
            job_id = parsed.path.rsplit("/", 1)[-1]
            with db() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                self.send_json({"error": "Job tidak ditemukan"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(job_dict(row))
            return
        if parsed.path in {"/", "/index.html"}:
            if not current_user(self, touch=False):
                self.send_redirect("/login")
            else:
                self.send_html(INDEX_HTML)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        if self.path.startswith("/api/bridge/") or self.headers.get("Origin", "").startswith("chrome-extension://"):
            self.send_response(HTTPStatus.NO_CONTENT)
            origin = self.bridge_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            try:
                body = self.read_json()
                email = normalize_email(body.get("email") if isinstance(body, dict) else "")
                password = str(body.get("password", "")) if isinstance(body, dict) else ""
                key = f"{self.client_address[0]}:{email[:120]}"
                if login_rate_limited(key):
                    self.send_json({"error": "Terlalu banyak percobaan. Coba lagi dalam satu menit."}, HTTPStatus.TOO_MANY_REQUESTS)
                    return
                with db() as conn:
                    user = conn.execute("SELECT * FROM users WHERE email=? AND role='superadmin'", (email,)).fetchone()
                if not user or not verify_password(password, user["password_hash"]):
                    self.send_json({"error": "Email atau password salah"}, HTTPStatus.UNAUTHORIZED)
                    return
                token = create_session(int(user["id"]))
                cookie = f"{AUTH_COOKIE_NAME}={token}; HttpOnly; Path=/; Max-Age={AUTH_SESSION_TTL}; SameSite=Lax"
                self.send_json({"user": {"email": user["email"], "role": user["role"]}, "must_change_password": bool(user["must_change_password"])}, set_cookie=cookie)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/auth/logout":
            revoke_session(self)
            expired = f"{AUTH_COOKIE_NAME}=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"
            self.send_json({"ok": True}, set_cookie=expired)
            return
        if parsed.path == "/api/auth/change-password":
            user = self.require_user()
            if not user:
                return
            try:
                body = self.read_json()
                current_password = str(body.get("current_password", "")) if isinstance(body, dict) else ""
                new_password = validate_password(body.get("new_password") if isinstance(body, dict) else "")
                confirmation = str(body.get("new_password_confirmation", "")) if isinstance(body, dict) else ""
                if not hmac.compare_digest(new_password, confirmation):
                    raise ValueError("Konfirmasi password baru tidak cocok")
                with db() as conn:
                    stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
                    if not stored or not verify_password(current_password, stored["password_hash"]):
                        self.send_json({"error": "Password saat ini salah"}, HTTPStatus.UNAUTHORIZED)
                        return
                    conn.execute("UPDATE users SET password_hash=?, must_change_password=0, updated_at=? WHERE id=?", (password_hash(new_password), now(), user["id"]))
                    conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL", (now(), user["id"], user["session_id"]))
                self.send_json({"ok": True, "must_change_password": False})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/bridge/ping":
            if not self.require_user():
                return
            try:
                self.send_json(bridge_ping(self.read_json()))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/bridge/result":
            if not self.require_user():
                return
            try:
                self.send_json(bridge_result(self.read_json()))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if self.path != "/api/jobs":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        user = self.require_user()
        if not user:
            return
        if user["must_change_password"]:
            self.send_json({"error": "Ganti password terlebih dahulu"}, HTTPStatus.FORBIDDEN)
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


LOGIN_HTML = r'''<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Masuk · Fahra Affiliate Rotator</title>
  <script>const storedTheme=localStorage.getItem('fahra-theme');const savedTheme=['system','light','dark'].includes(storedTheme)?storedTheme:'system';document.documentElement.dataset.theme=savedTheme==='system'?(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'):savedTheme;</script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&display=swap');
    :root{color-scheme:dark;--stage:#0e0d0c;--light:#f5efe4;--muted:#b5aaa0;--paper:#fffdf8;--ink:#1a1714;--accent:#e98425;--focus:#a7d8c7;--error:#9a2d2d;--serif:'Instrument Serif',Georgia,serif;--sans:Inter,system-ui,sans-serif;--mono:'IBM Plex Mono',ui-monospace,monospace}
    [data-theme="light"]{color-scheme:light;--stage:#eee8de;--light:#241f1a;--muted:#685f57;--accent:#b75b10}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse 90% 60% at 50% -10%,rgba(233,132,37,.2),transparent 70%),var(--stage);color:var(--light);font:14px/1.5 var(--sans)}.shell{width:min(430px,100%)}.brand{font:11px/1 var(--mono);letter-spacing:.16em;opacity:.7;margin-bottom:34px}.card{background:var(--paper);color:var(--ink);border-radius:18px;padding:28px;box-shadow:0 18px 40px rgba(0,0,0,.2)}h1{font:italic 700 clamp(2.4rem,12vw,4rem)/.98 var(--serif);margin:0 0 12px}.dek{color:var(--muted);margin:0 0 26px}label{display:block;font-weight:650;margin:16px 0 6px}input,button,select{width:100%;min-height:44px;font:inherit}input,select{border:1px solid #cfc7bd;border-radius:8px;background:var(--paper);color:var(--ink);padding:11px 12px}button{border:0;border-radius:999px;background:var(--ink);color:var(--paper);padding:12px 18px;margin-top:20px;font-weight:700;cursor:pointer}button:hover{opacity:.86}button:disabled{opacity:.55;cursor:wait}:focus-visible{outline:3px solid var(--focus);outline-offset:2px}.message{min-height:22px;color:var(--error);margin:12px 0 0}.theme{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:18px;color:var(--muted);font-size:.9rem}.theme select{width:auto;min-height:38px;padding:6px 9px}@media(max-width:480px){body{padding:16px}.card{padding:22px;border-radius:14px}.brand{margin-bottom:24px}}
  </style>
</head>
<body><main class="shell"><div class="brand">FAHRA AFFILIATE ROTATOR</div><section class="card" aria-labelledby="login-title"><h1 id="login-title">Masuk dulu.</h1><p class="dek">Kelola antrean link khusus Shopee dari satu tempat.</p><form id="login-form"><label for="email">Email superadmin</label><input id="email" name="email" type="email" autocomplete="username" required><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required><button id="login-button" type="submit">Masuk ke dashboard</button><p id="message" class="message" role="alert" aria-live="polite"></p></form><div class="theme"><span>Tema tampilan</span><select id="theme-select" aria-label="Tema tampilan"><option value="system">Sistem</option><option value="light">Terang</option><option value="dark">Gelap</option></select></div></section></main>
<script>
const themeSelect=document.querySelector('#theme-select'),themeMedia=matchMedia('(prefers-color-scheme: light)');function normalizeTheme(value){return ['system','light','dark'].includes(value)?value:'system'}function resolveTheme(value){return normalizeTheme(value)==='system'?(themeMedia.matches?'light':'dark'):normalizeTheme(value)}function applyTheme(value){value=normalizeTheme(value);localStorage.setItem('fahra-theme',value);document.documentElement.dataset.theme=resolveTheme(value);themeSelect.value=value}applyTheme(localStorage.getItem('fahra-theme')||'system');themeSelect.addEventListener('change',()=>applyTheme(themeSelect.value));const updateSystemTheme=()=>{if(normalizeTheme(localStorage.getItem('fahra-theme'))==='system')document.documentElement.dataset.theme=resolveTheme('system')};if(themeMedia.addEventListener)themeMedia.addEventListener('change',updateSystemTheme);else themeMedia.addListener(updateSystemTheme);
document.querySelector('#login-form').addEventListener('submit',async(event)=>{event.preventDefault();const button=document.querySelector('#login-button'),message=document.querySelector('#message');button.disabled=true;message.textContent='';try{const response=await fetch('/api/auth/login',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.querySelector('#email').value,password:document.querySelector('#password').value})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Login gagal');location.href='/'}catch(error){message.textContent=error.message}finally{button.disabled=false}});
</script></body></html>'''

INDEX_HTML = r'''<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Link khusus Shopee</title>
  <script>const storedTheme=localStorage.getItem('fahra-theme');const savedTheme=['system','light','dark'].includes(storedTheme)?storedTheme:'system';document.documentElement.dataset.theme=savedTheme==='system'?(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'):savedTheme;</script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&display=swap');
    :root { color-scheme: dark; --stage:#0e0d0c; --stage-2:#1a1714; --stage-line:rgba(245,239,228,.2); --ink:#1a1714; --light:#f5efe4; --muted:#b5aaa0; --paper:#fffdf8; --line:#ebe6dd; --accent:#e98425; --accent-2:#ff6b3d; --warn:#8a4b16; --error:#9a2d2d; --focus:#a7d8c7; --hover:#302923; --serif:'Instrument Serif',Georgia,serif; --sans:Inter,system-ui,sans-serif; --mono:'IBM Plex Mono',ui-monospace,monospace; }
    [data-theme="light"] { color-scheme: light; --stage:#eee8de; --stage-2:#f8f4ec; --stage-line:rgba(36,31,26,.18); --light:#241f1a; --muted:#685f57; --line:#ded6ca; --accent:#b75b10; --warn:#8a4b16; --hover:#3b3028; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(ellipse 80% 50% at 50% -10%,rgba(233,132,37,.18),transparent 70%),var(--stage); color:var(--light); font:14px/1.5 var(--sans); }
    main { width:min(1180px, calc(100% - 44px)); margin:0 auto 64px; }
    header { position:relative; display:flex; justify-content:space-between; gap:24px; align-items:end; margin:0 -4px 34px; padding:24px 0 0; }
    header::before { content:'FAHRA AFFILIATE ROTATOR'; position:absolute; top:24px; left:0; font:11px/1 var(--mono); letter-spacing:.16em; color:var(--muted); }
    h1 { font:italic 700 clamp(2.4rem,5vw,4.8rem)/.98 var(--serif); letter-spacing:-.02em; margin:48px 0 12px; max-width:11ch; }
    h1::first-line { color:var(--light); }
    .dek { max-width:580px; color:var(--muted); margin:0; font-size:15px; }
    .signal { min-width:0; flex:1 1 160px; padding:12px 14px; border:1px solid var(--stage-line); background:var(--stage-2); color:var(--light); border-radius:10px; font:12px/1.4 var(--mono); }
    .layout { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr); gap:22px; align-items:start; }
    section { min-width:0; background:var(--paper); border:0; border-radius:18px; padding:24px; color:var(--ink); box-shadow:0 18px 40px rgba(0,0,0,.18); }
    h2 { font:700 1.15rem/1.2 var(--sans); margin:0 0 15px; }
    label { display:block; font-weight:650; margin:15px 0 6px; }
    textarea,input { width:100%; min-height:44px; border:1px solid #cfc7bd; border-radius:8px; background:#fff; color:var(--ink); padding:11px 12px; font:inherit; }
    textarea { min-height:160px; resize:vertical; }
    input:focus,textarea:focus,button:focus-visible,select:focus-visible { outline:3px solid var(--focus); outline-offset:2px; }
    .tags { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; }
    .tags input { min-width:0; }
    button { min-height:44px; margin-top:18px; border:0; border-radius:999px; background:var(--ink); color:var(--light); padding:13px 18px; font-weight:700; cursor:pointer; }
    button:hover { background:var(--hover); }
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
    .header-tools { display:flex; flex:0 1 430px; align-items:center; gap:10px; }
    .account { display:flex; align-items:center; justify-content:flex-end; flex-wrap:wrap; gap:8px; min-width:0; font-size:12px; color:var(--muted); }
    .account-email { max-width:190px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--light); font-weight:600; }
    .account button { margin:0; width:auto; min-height:40px; padding:8px 12px; }
    .account select { min-height:40px; max-width:112px; border:1px solid var(--stage-line); border-radius:8px; background:var(--stage-2); color:var(--light); padding:6px 8px; }
    .icon-button { width:44px !important; min-height:44px; padding:0 !important; display:inline-grid; place-items:center; border:1px solid var(--stage-line); background:var(--stage-2); color:var(--light); border-radius:10px; }
    .icon-button svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    .password-panel { margin:0 0 22px; border:1px solid var(--line); border-radius:14px; padding:18px 20px; background:var(--paper); color:var(--ink); }
    .password-panel[hidden] { display:none; }
    .password-panel button { width:100%; }
    @media (max-width: 860px) {
      main { width:min(100% - 32px, 680px); margin-top:24px; margin-bottom:48px; }
      header { display:block; margin-bottom:26px; }
      .signal { width:100%; margin-top:18px; }
      .header-tools { width:100%; margin-top:18px; align-items:stretch; }
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
      .header-tools { display:flex; flex-wrap:wrap; }
      .signal { flex:1 1 100%; }
      .account { width:100%; justify-content:space-between; }
      .account-email { flex:1 1 100%; max-width:none; }
      .account select { flex:1 1 100px; max-width:none; }
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
  <div class="header-tools"><div class="signal" id="health" role="status">Memeriksa worker...</div><div class="account"><span id="account-email" class="account-email">Superadmin</span><select id="theme-select" aria-label="Tema tampilan"><option value="system">Sistem</option><option value="light">Terang</option><option value="dark">Gelap</option></select><button id="password-toggle" class="icon-button" type="button" aria-label="Edit password" title="Edit password" aria-expanded="false" aria-controls="password-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3Z"/><path d="m14 7 3 3"/></svg><span class="sr-only">Edit password</span></button><button id="logout" class="icon-button" type="button" aria-label="Keluar" title="Keluar"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"/><path d="m14 8 4 4-4 4"/><path d="M18 12H9"/></svg><span class="sr-only">Keluar</span></button></div></div>
  </header>
  <section id="password-panel" class="password-panel" hidden aria-labelledby="password-title"><h2 id="password-title">Ganti password</h2><p id="password-hint" class="hint">Masukkan password saat ini dan password baru.</p><form id="password-form"><label for="current-password">Password saat ini</label><input id="current-password" type="password" autocomplete="current-password" required><label for="new-password">Password baru</label><input id="new-password" type="password" autocomplete="new-password" minlength="12" required><label for="new-password-confirmation">Ulangi password baru</label><input id="new-password-confirmation" type="password" autocomplete="new-password" minlength="12" required><button type="submit">Simpan password</button><p id="password-message" class="hint" role="status"></p></form></section>
  <div class="layout">
    <section aria-labelledby="create-title">
      <h2 id="create-title">Buat batch link</h2>
      <form id="job-form">
        <label for="links">URL Shopee</label>
        <textarea id="links" name="links" required placeholder="Satu URL per baris" disabled></textarea>
        <p class="hint">URL harus berasal dari shopee.co.id. Maksimal lima baris per job.</p>
        <label>Tag pelacakan</label>
        <div class="tags">
          <input name="tag1" aria-label="Tag ke 1" placeholder="Tag 1" disabled>
          <input name="tag2" aria-label="Tag ke 2" placeholder="Tag 2" disabled>
          <input name="tag3" aria-label="Tag ke 3" placeholder="Tag 3" disabled>
          <input name="tag4" aria-label="Tag ke 4" placeholder="Tag 4" disabled>
          <input name="tag5" aria-label="Tag ke 5" placeholder="Tag 5" disabled>
        </div>
        <p class="hint">Gunakan huruf dan angka saja. Tag akan masuk ke parameter subId Shopee.</p>
        <button id="submit" type="submit" disabled>Masukkan ke worker</button>
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
let currentUser = null;
let passwordPanelOpen = false;
function applyTheme(value) {
  value = normalizeTheme(value);
  localStorage.setItem('fahra-theme', value);
  document.documentElement.dataset.theme = value === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : value;
  $('#theme-select').value = value;
}
function normalizeTheme(value) { return ['system', 'light', 'dark'].includes(value) ? value : 'system'; }
const themeMedia = matchMedia('(prefers-color-scheme: light)');
const updateSystemTheme = () => { if (normalizeTheme(localStorage.getItem('fahra-theme')) === 'system') document.documentElement.dataset.theme = themeMedia.matches ? 'light' : 'dark'; };
if (themeMedia.addEventListener) themeMedia.addEventListener('change', updateSystemTheme); else themeMedia.addListener(updateSystemTheme);
async function api(url, options = {}) {
  const response = await fetch(url, { credentials: 'include', cache: 'no-store', ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (response.status === 401) { location.href = '/login'; throw new Error('Login diperlukan'); }
  if (!response.ok) throw new Error(data.error || `Request gagal (${response.status})`);
  return data;
}
function updatePasswordLock() {
  const locked = Boolean(currentUser?.must_change_password);
  $('#password-panel').hidden = !locked && !passwordPanelOpen;
  const passwordToggle = $('#password-toggle');
  const passwordAction = locked ? 'Password wajib diubah' : (passwordPanelOpen ? 'Tutup password' : 'Edit password');
  passwordToggle.disabled = locked;
  passwordToggle.setAttribute('aria-label', passwordAction);
  passwordToggle.setAttribute('title', passwordAction);
  passwordToggle.setAttribute('aria-expanded', String(locked || passwordPanelOpen));
  $('#password-hint').textContent = locked ? 'Untuk keamanan, ganti password bootstrap sebelum membuat job.' : 'Masukkan password saat ini dan password baru.';
  $('#job-form').querySelectorAll('input,textarea,button').forEach(control => { control.disabled = locked; });
  if (locked) $('#form-message').textContent = 'Ganti password awal di atas untuk membuka pembuatan job.';
}
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
  const me = await api('/api/auth/me');
  const [health, jobs] = await Promise.all([api('/api/health'), api('/api/jobs')]);
  currentUser = me.user;
  $('#account-email').textContent = currentUser.email;
  applyTheme(localStorage.getItem('fahra-theme') || 'system');
  $('#health').textContent = health.worker ? (health.bridge?.connected ? 'Bridge aktif' : 'Menunggu extension') : 'Worker berhenti';
  jobsCache = Array.isArray(jobs.jobs) ? jobs.jobs : [];
  updatePasswordLock();
  renderJobs();
}
$('#theme-select').addEventListener('change', () => applyTheme($('#theme-select').value));
applyTheme(localStorage.getItem('fahra-theme') || 'system');
$('#password-toggle').addEventListener('click', () => { if (currentUser?.must_change_password) return; const wasOpen = passwordPanelOpen; passwordPanelOpen = !passwordPanelOpen; if (wasOpen && !passwordPanelOpen) { $('#current-password').value = ''; $('#new-password').value = ''; $('#new-password-confirmation').value = ''; $('#password-message').textContent = ''; } updatePasswordLock(); if (passwordPanelOpen) $('#current-password').focus(); });
$('#logout').addEventListener('click', async () => { await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }); location.href = '/login'; });
$('#password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const message = $('#password-message');
  button.disabled = true; message.textContent = '';
  try {
    await api('/api/auth/change-password', { method: 'POST', body: JSON.stringify({ current_password: $('#current-password').value, new_password: $('#new-password').value, new_password_confirmation: $('#new-password-confirmation').value }) });
    $('#current-password').value = ''; $('#new-password').value = ''; $('#new-password-confirmation').value = '';
    message.textContent = 'Password tersimpan. Pembuatan job sudah dibuka.';
    passwordPanelOpen = false;
    await refresh();
  } catch (error) { message.textContent = error.message; } finally { button.disabled = false; }
});
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
  try { const data = await api('/api/jobs', {method:'POST', body:JSON.stringify({links:$('#links').value, tags})}); $('#links').value=''; [1,2,3,4,5].forEach(i => event.target.elements[`tag${i}`].value=''); $('#form-message').textContent = `Job ${data.id} masuk antrean.`; await refresh(); } catch (error) { $('#form-message').textContent = error.message; } finally { button.disabled = false; }
});
refresh().catch((error) => { $('#health').textContent = error.message || 'Backend tidak merespons'; });
setInterval(() => refresh().catch(() => {}), 2500);
</script>
</body>
</html>'''


def main() -> None:
    init_db()
    if "--bootstrap-admin" in os.sys.argv:
        bootstrap_admin()
        return
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
