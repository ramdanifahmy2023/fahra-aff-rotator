#!/usr/bin/env python3
"""Local Shopee Affiliate custom-link queue and web application."""

from __future__ import annotations

import json
import hashlib
import hmac
import ipaddress
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
from http.client import HTTPConnection
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
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8790"))
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", f"http://127.0.0.1:{MCP_PORT}/mcp")
MCP_ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(item.strip(), strict=False)
    for item in os.getenv("MCP_ALLOWED_NETWORKS", "127.0.0.1/32,100.64.0.0/10,::1/128").split(",")
    if item.strip()
)
MCP_TOKEN_FILE = Path(
    os.getenv(
        "MCP_TOKEN_FILE",
        str(DB_PATH.parent / "mcp-openclaw.token"),
    )
)
MCP_TOKEN_NAME = os.getenv("MCP_TOKEN_NAME", "openclaw")[:80] or "openclaw"
MCP_MAX_WAIT_SECONDS = max(1, min(300, int(os.getenv("MCP_MAX_WAIT_SECONDS", "180"))))
MCP_RATE_LIMIT_PER_MINUTE = max(1, int(os.getenv("MCP_RATE_LIMIT_PER_MINUTE", "30")))
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
CREATE TABLE IF NOT EXISTS affiliate_accounts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
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
CREATE TABLE IF NOT EXISTS mcp_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  scopes_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT,
  deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS mcp_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_name TEXT NOT NULL,
  method TEXT NOT NULL,
  request_id TEXT,
  job_id TEXT,
  outcome TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_audit_created_at ON mcp_audit(created_at);
CREATE INDEX IF NOT EXISTS mcp_audit_job_id ON mcp_audit(job_id);
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
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_key ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        if "account_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN account_id TEXT")
        if "account_name" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN account_name TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_account_status ON jobs(account_id,status,created_at)")
        conn.execute(
            "UPDATE jobs SET status='needs_session', error=?, updated_at=? "
            "WHERE account_id IS NULL AND status IN ('queued','running')",
            ("Job lama belum memiliki akun affiliate. Pilih akun lalu kirim ulang job.", now()),
        )
        mcp_columns = {row["name"] for row in conn.execute("PRAGMA table_info(mcp_tokens)").fetchall()}
        if "deleted_at" not in mcp_columns:
            conn.execute("ALTER TABLE mcp_tokens ADD COLUMN deleted_at TEXT")
        # A daemon restart must not leave a lease permanently stuck in running.
        conn.execute(
            "UPDATE jobs SET status='queued', updated_at=? WHERE status='running'",
            (now(),),
        )
    ensure_mcp_token()


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


def ensure_mcp_token() -> None:
    """Create one local OpenClaw token on first run and store only its hash in SQLite."""
    MCP_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        MCP_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    token = ""
    if MCP_TOKEN_FILE.exists():
        token = MCP_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        token = secrets.token_urlsafe(48)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(MCP_TOKEN_FILE, flags, 0o600)
        except FileExistsError:
            token = MCP_TOKEN_FILE.read_text(encoding="utf-8").strip()
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
    if len(token) < 32:
        raise RuntimeError(f"MCP token file tidak valid: {MCP_TOKEN_FILE}")
    hashed = token_hash(token)
    scopes = json.dumps(MCP_SCOPES)
    with db() as conn:
        existing = conn.execute(
            "SELECT id, token_hash, revoked_at, deleted_at FROM mcp_tokens WHERE name=?",
            (MCP_TOKEN_NAME,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO mcp_tokens(name, token_hash, scopes_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (MCP_TOKEN_NAME, hashed, scopes, now()),
            )
        elif existing["token_hash"] != hashed and not existing["deleted_at"]:
            conn.execute(
                """UPDATE mcp_tokens
                   SET token_hash=?, scopes_json=?
                   WHERE id=?""",
                (hashed, scopes, existing["id"]),
            )


def clean_mcp_token_name(name: str) -> str:
    clean_name = str(name or "").strip()[:80]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", clean_name):
        raise ValueError("Nama token hanya boleh berisi huruf, angka, titik, garis bawah, atau tanda minus")
    return clean_name


MCP_SCOPES = ["jobs:create", "jobs:read", "health:read"]


def mcp_token_metadata(row: sqlite3.Row) -> dict[str, Any]:
    if row["deleted_at"]:
        status = "deleted"
    elif row["revoked_at"]:
        status = "revoked"
    else:
        status = "active"
    return {
        "name": row["name"],
        "scopes": json.loads(row["scopes_json"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
        "deleted_at": row["deleted_at"],
        "status": status,
    }


def issue_mcp_token(name: str) -> tuple[str, dict[str, Any]]:
    clean_name = clean_mcp_token_name(name)
    token = secrets.token_urlsafe(48)
    with db() as conn:
        existing = conn.execute("SELECT * FROM mcp_tokens WHERE name=?", (clean_name,)).fetchone()
        if existing and not existing["deleted_at"]:
            raise RuntimeError(f"Token MCP sudah ada dan harus dicabut atau dihapus dahulu: {clean_name}")
        if existing:
            conn.execute(
                """UPDATE mcp_tokens
                   SET token_hash=?, scopes_json=?, created_at=?, last_used_at=NULL,
                       revoked_at=NULL, deleted_at=NULL
                   WHERE id=?""",
                (token_hash(token), json.dumps(MCP_SCOPES), now(), existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO mcp_tokens(name, token_hash, scopes_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (clean_name, token_hash(token), json.dumps(MCP_SCOPES), now()),
            )
        row = conn.execute("SELECT * FROM mcp_tokens WHERE name=?", (clean_name,)).fetchone()
    return token, mcp_token_metadata(row)


def create_mcp_token(name: str) -> None:
    token, _ = issue_mcp_token(name)
    print(token)


def revoke_mcp_token(name: str) -> None:
    clean_name = clean_mcp_token_name(name)
    with db() as conn:
        result = conn.execute(
            """UPDATE mcp_tokens SET revoked_at=?
               WHERE name=? AND revoked_at IS NULL AND deleted_at IS NULL""",
            (now(), clean_name),
        )
        if result.rowcount == 0:
            raise RuntimeError(f"Token MCP tidak ditemukan atau sudah dicabut: {clean_name}")
    print(f"Token MCP dicabut: {clean_name}")


def delete_mcp_token(name: str) -> None:
    clean_name = clean_mcp_token_name(name)
    with db() as conn:
        if clean_name == MCP_TOKEN_NAME:
            deleted_at = now()
            result = conn.execute(
                """UPDATE mcp_tokens
                   SET deleted_at=?, revoked_at=COALESCE(revoked_at, ?)
                   WHERE name=? AND deleted_at IS NULL""",
                (deleted_at, deleted_at, clean_name),
            )
        else:
            result = conn.execute("DELETE FROM mcp_tokens WHERE name=?", (clean_name,))
        if result.rowcount == 0:
            raise RuntimeError(f"Token MCP tidak ditemukan atau sudah dihapus: {clean_name}")
    print(f"Token MCP dihapus: {clean_name}")


def list_mcp_tokens() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM mcp_tokens ORDER BY deleted_at IS NOT NULL, created_at DESC").fetchall()
    return [mcp_token_metadata(row) for row in rows]


def mcp_token_from_request(handler: BaseHTTPRequestHandler) -> sqlite3.Row | None:
    authorization = handler.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    hashed = token_hash(value.strip())
    with db() as conn:
        row = conn.execute(
            """SELECT id, name, token_hash, scopes_json
               FROM mcp_tokens
               WHERE token_hash=? AND revoked_at IS NULL AND deleted_at IS NULL""",
            (hashed,),
        ).fetchone()
        if row:
            conn.execute("UPDATE mcp_tokens SET last_used_at=? WHERE id=?", (now(), row["id"]))
    return row


MCP_RATE_LOCK = threading.Lock()
MCP_RATE_STATE: dict[int, list[float]] = {}


def mcp_rate_limited(token_id: int) -> bool:
    current = time.time()
    with MCP_RATE_LOCK:
        recent = [stamp for stamp in MCP_RATE_STATE.get(token_id, []) if current - stamp < 60]
        limited = len(recent) >= MCP_RATE_LIMIT_PER_MINUTE
        recent.append(current)
        MCP_RATE_STATE[token_id] = recent[-(MCP_RATE_LIMIT_PER_MINUTE + 2):]
        return limited


def mcp_audit(token: sqlite3.Row, method: str, request_id: Any, job_id: str | None, outcome: str) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO mcp_audit(token_name, method, request_id, job_id, outcome, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (token["name"], method[:100], str(request_id)[:100] if request_id is not None else None, job_id, outcome[:100], now()),
        )


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


def create_links(links: list[str], tags: list[str], *, job_id: str | None = None, account_id: str | None = None) -> list[dict[str, Any]]:
    if TRANSPORT == "extension":
        if not job_id:
            raise RuntimeError("Job ID wajib untuk extension bridge")
        return create_links_via_extension(links, tags, job_id, account_id)
    if account_id:
        raise SessionExpired("Job per akun hanya dapat diproses melalui extension. Aktifkan SHOPEE_TRANSPORT=extension.")
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


def create_links_via_extension(links: list[str], tags: list[str], job_id: str, account_id: str | None) -> list[dict[str, Any]]:
    global BRIDGE_CURRENT
    event = threading.Event()
    lease_token = secrets.token_urlsafe(24)
    with BRIDGE_CONDITION:
        if BRIDGE_CURRENT is not None:
            raise RuntimeError("Extension bridge sedang menangani job lain")
        BRIDGE_CURRENT = {
            "event": event,
            "job_id": job_id,
            "account_id": account_id,
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
            raise SessionExpired("Hasil bridge tidak lengkap. Periksa tab akun terpilih lalu kirim ulang bila diperlukan.")
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
        "account_id": row["account_id"],
        "account_name": row["account_name"],
    }


def enqueue_job(body: Any, *, idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
    links, tags = validate_job(body)
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{32}", account_id):
        raise ValueError("Pilih akun affiliate. Atur nama akun pada extension di setiap profil Chrome terlebih dahulu.")
    if TRANSPORT != "extension":
        raise ValueError("Job per akun memerlukan SHOPEE_TRANSPORT=extension")
    clean_key = (idempotency_key or "").strip()[:200] or None
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute("SELECT * FROM affiliate_accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            raise ValueError("Akun affiliate tidak terdaftar. Hubungkan extension profil akun tersebut terlebih dahulu.")
        if clean_key:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?",
                (clean_key,),
            ).fetchone()
            if existing:
                if (existing["account_id"] != account_id or json.loads(existing["links_json"]) != links
                        or json.loads(existing["tags_json"]) != tags):
                    raise ValueError("idempotency_key sudah dipakai untuk akun atau input lain")
                return job_dict(existing), True
        job_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO jobs(id,status,links_json,tags_json,idempotency_key,account_id,account_name) VALUES(?,?,?,?,?,?,?)",
            (job_id, "queued", json.dumps(links), json.dumps(tags), clean_key, account_id, account["name"]),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    WORKER.wake()
    return job_dict(row), False


def get_job(job_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job_dict(row) if row else None


def health_summary() -> dict[str, Any]:
    with BRIDGE_CONDITION:
        last_seen = max((float(state["timestamp"]) for state in BRIDGE_STATES.values()), default=0)
        connected = bridge_is_connected()
        bridge = {
            "connected": connected,
            "ready": connected,
            "last_seen": last_seen or None,
            "busy": BRIDGE_CURRENT is not None,
            "ready_accounts": len(ready_account_ids()),
            "protocol_version": 2,
        }
    return {"status": "ok", "worker": WORKER.is_alive(), "bridge": bridge}


def probe_mcp_listener() -> dict[str, Any]:
    # The client URL can point to the VPS end of a reverse SSH tunnel.
    # A dashboard check verifies this process's listener, not remote reachability.
    hostname = "127.0.0.1" if MCP_HOST in {"0.0.0.0", "::"} else MCP_HOST
    connection = None
    try:
        connection = HTTPConnection(hostname, MCP_PORT, timeout=3)
        connection.request(
            "POST",
            "/mcp",
            body=b'{"jsonrpc":"2.0","id":"dashboard-probe","method":"initialize","params":{}}',
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        response = connection.getresponse()
        response.read(4096)
        if response.status == HTTPStatus.UNAUTHORIZED:
            return {"ok": True, "scope": "local", "status": response.status, "message": "Listener MCP Windows aktif dan meminta Bearer token. Koneksi VPS perlu diuji dari VPS."}
        if response.status == HTTPStatus.OK:
            return {"ok": False, "scope": "local", "status": response.status, "message": "Listener merespons tanpa autentikasi. Periksa konfigurasi MCP."}
        return {"ok": False, "status": response.status, "message": f"Endpoint MCP merespons HTTP {response.status}."}
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": f"MCP tidak terjangkau: {str(exc)[:160]}"}
    finally:
        if connection is not None:
            connection.close()


def wait_for_job(job_id: str, timeout_seconds: int) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(1, min(MCP_MAX_WAIT_SECONDS, timeout_seconds))
    while True:
        job = get_job(job_id)
        if job is None or job["status"] in {"completed", "failed", "needs_session"}:
            return job
        if time.monotonic() >= deadline:
            return job
        time.sleep(0.5)


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
        accounts = ready_account_ids()
        if TRANSPORT == "extension" and not accounts:
            return None
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if TRANSPORT == "extension":
                placeholders = ",".join("?" for _ in accounts)
                row = conn.execute(
                    f"SELECT * FROM jobs WHERE status='queued' AND account_id IN ({placeholders}) "
                    "ORDER BY created_at,id LIMIT 1", accounts,
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
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
                account_id=row["account_id"],
            )
            valid = (len(result) == len(json.loads(row["links_json"])) and all(
                isinstance(item, dict) and item.get("failCode", 0) == 0
                and re.fullmatch(r"https://s\.shopee\.co\.id/[A-Za-z0-9]+", str(item.get("shortLink", "")))
                for item in result
            ))
            status = "completed" if valid else "failed"
            error = None if status == "completed" else "Hasil Shopee tidak lengkap atau salah satu link gagal. Periksa tab akun tersebut lalu kirim ulang."
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
BRIDGE_STATES: dict[str, dict[str, Any]] = {}


def bridge_identity(body: Any) -> str:
    if not isinstance(body, dict) or body.get("protocol_version") != 2:
        raise ValueError("Bridge versi lama. Reload extension dan tab Custom Link, lalu atur nama akun pada extension.")
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{32}", account_id):
        raise ValueError("ID akun bridge tidak valid. Buka pengaturan extension pada profil ini.")
    return account_id


def ready_account_ids() -> list[str]:
    with BRIDGE_CONDITION:
        return [key for key, state in BRIDGE_STATES.items()
                if state.get("ready") and time.monotonic() - state["seen"] < 10]


def bridge_is_connected() -> bool:
    return bool(ready_account_ids())


def list_affiliate_accounts() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM affiliate_accounts ORDER BY name,id").fetchall()
    with BRIDGE_CONDITION:
        ready = set(ready_account_ids())
        return [{"id": row["id"], "name": row["name"], "ready": row["id"] in ready,
                 "busy": bool(BRIDGE_CURRENT and BRIDGE_CURRENT["account_id"] == row["id"]),
                 "last_seen": BRIDGE_STATES.get(row["id"], {}).get("timestamp")}
                for row in rows]


def bridge_ping(body: Any) -> dict[str, Any]:
    account_id = bridge_identity(body)
    name = body.get("account_name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Nama akun wajib diisi, maksimal 80 karakter pada pengaturan extension.")
    url = body.get("url")
    ready = body.get("ready") is True and url == "https://affiliate.shopee.co.id/offer/custom_link"
    with db() as conn:
        conn.execute(
            "INSERT INTO affiliate_accounts(id,name,created_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name WHERE name<>excluded.name",
            (account_id, name.strip(), now()),
        )
    with BRIDGE_CONDITION:
        BRIDGE_STATES[account_id] = {"timestamp": time.time(), "seen": time.monotonic(), "ready": ready}
    WORKER.wake()
    return {"ok": True, "ready": ready, "account_id": account_id}


def bridge_next(body: Any) -> dict[str, Any]:
    account_id = bridge_identity(body)
    with BRIDGE_CONDITION:
        current = BRIDGE_CURRENT
        if (account_id not in ready_account_ids() or not current or current["event"].is_set()
                or current["account_id"] != account_id or current["delivered"]):
            return {"job": None}
        current["delivered"] = True
        return {
            "job": {"id": current["job_id"], "account_id": account_id,
                    "links": current["links"], "tags": current["tags"]},
            "lease_token": current["lease_token"],
        }


def bridge_result(body: Any) -> dict[str, Any]:
    account_id = bridge_identity(body)
    job_id = str(body.get("job_id", ""))
    lease_token = str(body.get("lease_token", ""))
    with BRIDGE_CONDITION:
        current = BRIDGE_CURRENT
        if (not current or current["job_id"] != job_id or not current["delivered"]
                or current["account_id"] != account_id or current["event"].is_set()
                or not secrets.compare_digest(current["lease_token"].encode(), lease_token.encode())):
            raise ValueError("Lease bridge tidak valid, akun tidak cocok, atau hasil sudah diterima")
        current["response"] = {
            "results": body.get("results"),
            "error": str(body.get("error", ""))[:500],
            "error_kind": str(body.get("error_kind", ""))[:40],
        }
        current["event"].set()
        BRIDGE_CONDITION.notify_all()
    return {"ok": True}


MCP_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
MCP_TOOLS = [
    {"name": "shopee_list_accounts", "description": "Daftar akun affiliate dan kesiapan bridge. Pilih id untuk account_id saat generate.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {
        "name": "shopee_generate_short_links",
        "description": "Masukkan 1 sampai 5 URL Shopee ke antrean custom link. Proses dilakukan oleh worker dan tab Shopee yang sudah login.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                "links": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "idempotency_key": {"type": "string", "maxLength": 200},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300},
            },
            "required": ["links", "account_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "shopee_get_job",
        "description": "Baca status dan hasil satu job custom link Shopee.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "minLength": 8, "maxLength": 80}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "shopee_wait_for_job",
        "description": "Tunggu job sampai selesai atau batas waktu tercapai. Sukses hanya jika status completed dan shortLink tersedia.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "minLength": 8, "maxLength": 80},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "shopee_health",
        "description": "Periksa status worker dan koneksi extension bridge tanpa membocorkan secret atau path lokal.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "shopee_list_recent_jobs",
        "description": "Tampilkan job terbaru dengan batas jumlah hasil.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
    },
]


def mcp_text_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": encoded}],
        "structuredContent": value if isinstance(value, dict) else {"value": value},
        "isError": is_error,
    }


def mcp_jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def mcp_call_tool(name: str, arguments: Any) -> tuple[dict[str, Any], str | None]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments harus berupa object JSON")
    if name == "shopee_list_accounts":
        return {"accounts": list_affiliate_accounts()}, None
    if name == "shopee_health":
        return health_summary(), None
    if name == "shopee_generate_short_links":
        body = {"links": arguments.get("links"), "tags": arguments.get("tags", []), "account_id": arguments.get("account_id")}
        idem = str(arguments.get("idempotency_key", "")).strip()[:200] or None
        job, reused = enqueue_job(body, idempotency_key=idem)
        wait_seconds = int(arguments.get("wait_seconds", 0) or 0)
        if wait_seconds:
            job = wait_for_job(job["id"], wait_seconds) or job
        return {
            "job_id": job["id"],
            "status": job["status"],
            "reused": reused,
            "accepted_count": len(job["links"]),
            "job": job,
        }, job["id"]
    if name == "shopee_get_job":
        job_id = str(arguments.get("job_id", "")).strip()
        if not job_id:
            raise ValueError("job_id wajib diisi")
        job = get_job(job_id)
        if job is None:
            raise LookupError("Job tidak ditemukan")
        return job, job_id
    if name == "shopee_wait_for_job":
        job_id = str(arguments.get("job_id", "")).strip()
        if not job_id:
            raise ValueError("job_id wajib diisi")
        timeout = int(arguments.get("timeout_seconds", MCP_MAX_WAIT_SECONDS) or MCP_MAX_WAIT_SECONDS)
        job = wait_for_job(job_id, timeout)
        if job is None:
            raise LookupError("Job tidak ditemukan")
        return job, job_id
    if name == "shopee_list_recent_jobs":
        limit = max(1, min(50, int(arguments.get("limit", 20) or 20)))
        with db() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return {"jobs": [job_dict(row) for row in rows]}, None
    raise LookupError(f"Tool tidak tersedia: {name}")


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "FahraAffiliateRotatorMCP/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[mcp {time.strftime('%H:%M:%S')}] {fmt % args}")

    def send_mcp(self, value: Any, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def authenticate(self) -> sqlite3.Row | None:
        try:
            client_ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            client_ip = None
        if client_ip is None or not any(client_ip in network for network in MCP_ALLOWED_NETWORKS):
            self.send_mcp({"error": "Alamat jaringan tidak diizinkan"}, HTTPStatus.FORBIDDEN)
            return None
        token = mcp_token_from_request(self)
        if not token:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Bearer realm="fahra-affiliate-mcp"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if mcp_rate_limited(int(token["id"])):
            self.send_mcp({"error": "Terlalu banyak request MCP. Coba lagi sebentar."}, HTTPStatus.TOO_MANY_REQUESTS)
            return None
        return token

    def do_GET(self) -> None:
        if self.path != "/mcp":
            self.send_mcp({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.authenticate():
            return
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self.send_mcp({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        token = self.authenticate()
        if not token:
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("Request MCP terlalu besar")
            request = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                self.send_mcp(mcp_jsonrpc_error(None, -32600, "Invalid JSON-RPC request"), HTTPStatus.BAD_REQUEST)
                return
            request_id = request.get("id")
            method = str(request.get("method", ""))
            params = request.get("params") or {}
            if method == "notifications/initialized":
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method.startswith("notifications/"):
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                requested = str(params.get("protocolVersion", ""))
                version = requested if requested in MCP_PROTOCOL_VERSIONS else "2025-03-26"
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fahra-affiliate-rotator", "version": "0.1.0"},
                }
                mcp_audit(token, method, request_id, None, "ok")
                self.send_mcp({"jsonrpc": "2.0", "id": request_id, "result": result})
                return
            if method == "ping":
                mcp_audit(token, method, request_id, None, "ok")
                self.send_mcp({"jsonrpc": "2.0", "id": request_id, "result": {}})
                return
            if method == "tools/list":
                if "tools:read" not in json.loads(token["scopes_json"]) and "jobs:read" not in json.loads(token["scopes_json"]):
                    self.send_mcp(mcp_jsonrpc_error(request_id, -32003, "Scope tidak mencukupi"), HTTPStatus.FORBIDDEN)
                    return
                mcp_audit(token, method, request_id, None, "ok")
                self.send_mcp({"jsonrpc": "2.0", "id": request_id, "result": {"tools": MCP_TOOLS}})
                return
            if method == "tools/call":
                name = str(params.get("name", ""))
                scope = "health:read" if name == "shopee_health" else ("jobs:create" if name == "shopee_generate_short_links" else "jobs:read")
                scopes = set(json.loads(token["scopes_json"]))
                if scope not in scopes:
                    self.send_mcp(mcp_jsonrpc_error(request_id, -32003, "Scope tidak mencukupi"), HTTPStatus.FORBIDDEN)
                    return
                try:
                    result, job_id = mcp_call_tool(name, params.get("arguments") or {})
                    mcp_audit(token, method, request_id, job_id, "ok")
                    self.send_mcp({"jsonrpc": "2.0", "id": request_id, "result": mcp_text_result(result)})
                except (ValueError, LookupError, RuntimeError) as exc:
                    message = str(exc)[:500]
                    mcp_audit(token, method, request_id, None, "error")
                    self.send_mcp({"jsonrpc": "2.0", "id": request_id, "result": mcp_text_result({"error": message}, is_error=True)})
                return
            self.send_mcp(mcp_jsonrpc_error(request_id, -32601, f"Method tidak tersedia: {method}"), HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_mcp(mcp_jsonrpc_error(None, -32700, str(exc)), HTTPStatus.BAD_REQUEST)


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
        if getattr(self, "_body_consumed", False):
            return self._json_body
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("Request terlalu besar")
        raw = self.rfile.read(length) if length else b"{}"
        self._body_consumed = True
        self._json_body = json.loads(raw or b"{}")
        return self._json_body

    def discard_body(self) -> None:
        if getattr(self, "_body_consumed", False):
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2_000_000:
            length = 2_000_000
        if length:
            self.rfile.read(length)
        self._body_consumed = True

    def require_user(self) -> sqlite3.Row | None:
        user = current_user(self)
        if not user:
            # Windows resets a POST connection if the body is still unread when the socket closes.
            self.discard_body()
            self.send_json({"error": "Login diperlukan"}, HTTPStatus.UNAUTHORIZED)
        return user

    def require_mcp_admin(self) -> sqlite3.Row | None:
        user = self.require_user()
        if not user:
            return None
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin:
            parsed_origin = urlparse(origin)
            if parsed_origin.netloc.lower() != host.lower() or parsed_origin.scheme not in {"http", "https"}:
                self.discard_body()
                self.send_json({"error": "Origin request tidak cocok"}, HTTPStatus.FORBIDDEN)
                return None
        elif self.headers.get("Sec-Fetch-Site", "").lower() not in {"same-origin", "none", ""}:
            self.discard_body()
            self.send_json({"error": "Request lintas origin ditolak"}, HTTPStatus.FORBIDDEN)
            return None
        if user["must_change_password"]:
            self.discard_body()
            self.send_json({"error": "Ganti password terlebih dahulu"}, HTTPStatus.FORBIDDEN)
            return None
        return user

    def send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.svg":
            self.send_svg(FAVICON_SVG)
            return
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
            self.send_json(health_summary())
            return
        if parsed.path == "/api/mcp/tokens":
            if not self.require_mcp_admin():
                return
            self.send_json({"mcp_url": MCP_PUBLIC_URL, "tokens": list_mcp_tokens()})
            return
        if parsed.path == "/api/mcp/test":
            if not self.require_mcp_admin():
                return
            self.send_json(probe_mcp_listener())
            return
        if parsed.path == "/api/bridge/next":
            if not self.require_user():
                return
            self.send_json({"error": "Bridge versi lama. Reload extension dan tab Shopee."}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/accounts":
            if not self.require_user():
                return
            self.send_json({"accounts": list_affiliate_accounts()})
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
        if parsed.path == "/api/mcp/test":
            if not self.require_mcp_admin():
                return
            self.send_json(probe_mcp_listener())
            return
        if parsed.path == "/api/mcp/tokens":
            if not self.require_mcp_admin():
                return
            try:
                body = self.read_json()
                name = body.get("name") if isinstance(body, dict) else ""
                token, metadata = issue_mcp_token(name)
                self.send_json(
                    {
                        "token": token,
                        "token_metadata": metadata,
                        "mcp_url": MCP_PUBLIC_URL,
                        "warning": "Token ini hanya ditampilkan sekali. Simpan di secret manager atau environment client.",
                    },
                    HTTPStatus.CREATED,
                )
            except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/mcp/tokens/") and parsed.path.endswith("/revoke"):
            if not self.require_mcp_admin():
                return
            name = parsed.path[len("/api/mcp/tokens/") : -len("/revoke")].strip("/")
            try:
                revoke_mcp_token(name)
                self.send_json({"ok": True, "name": name, "status": "revoked"})
            except (ValueError, RuntimeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/bridge/next":
            if not self.require_user():
                return
            try:
                self.send_json(bridge_next(self.read_json()))
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
            self.discard_body()
            self.send_json({"error": "Ganti password terlebih dahulu"}, HTTPStatus.FORBIDDEN)
            return
        try:
            job, reused = enqueue_job(self.read_json())
            self.send_json({"id": job["id"], "status": job["status"], "account_id": job["account_id"], "reused": reused}, HTTPStatus.ACCEPTED)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/mcp/tokens/"):
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.require_mcp_admin():
            return
        name = parsed.path[len("/api/mcp/tokens/") :].strip("/")
        try:
            delete_mcp_token(name)
            self.send_json({"ok": True, "name": name, "status": "deleted"})
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_svg(self, svg: str) -> None:
        raw = svg.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


FAVICON_SVG = r'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="28" fill="#1a1714"/>
  <rect x="5" y="5" width="118" height="118" rx="24" fill="none" stroke="#3a2c23" stroke-width="2"/>
  <path d="M54 44 42 56a19 19 0 0 0 27 27l12-12" fill="none" stroke="#f5efe4" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="m74 84 12-12a19 19 0 0 0-27-27L47 57" fill="none" stroke="#e98425" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="m52 67 24-6" fill="none" stroke="#ff6b3d" stroke-width="7" stroke-linecap="round"/>
</svg>'''


LOGIN_HTML = r'''<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
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
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <title>Link khusus Shopee</title>
  <script>const storedTheme=localStorage.getItem('fahra-theme');const savedTheme=['system','light','dark'].includes(storedTheme)?storedTheme:'system';document.documentElement.dataset.theme=savedTheme==='system'?(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'):savedTheme;</script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&display=swap');
    :root { color-scheme: dark; --stage:#0e0d0c; --stage-2:#1a1714; --stage-line:rgba(245,239,228,.2); --ink:#1a1714; --light:#f5efe4; --muted:#b5aaa0; --paper:#fffdf8; --line:#ebe6dd; --accent:#e98425; --accent-2:#ff6b3d; --warn:#8a4b16; --error:#9a2d2d; --focus:#a7d8c7; --hover:#302923; --serif:'Instrument Serif',Georgia,serif; --sans:Inter,system-ui,sans-serif; --mono:'IBM Plex Mono',ui-monospace,monospace; }
    [data-theme="light"] { color-scheme: light; --stage:#eee8de; --stage-2:#f8f4ec; --stage-line:rgba(36,31,26,.18); --light:#241f1a; --muted:#685f57; --line:#ded6ca; --accent:#b75b10; --warn:#8a4b16; --hover:#3b3028; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(ellipse 80% 50% at 50% -10%,rgba(233,132,37,.18),transparent 70%),var(--stage); color:var(--light); font:14px/1.5 var(--sans); }
    main { width:min(1360px, calc(100% - 44px)); margin:0 auto 64px; }
    .app-shell { display:grid; grid-template-columns:220px minmax(0,1fr); gap:28px; align-items:start; padding-top:24px; transition:grid-template-columns .18s ease; }
    .app-shell.sidebar-collapsed { grid-template-columns:72px minmax(0,1fr); }
    .sidebar { position:sticky; top:24px; min-height:calc(100vh - 48px); display:flex; flex-direction:column; padding:18px 12px; border:1px solid var(--stage-line); border-radius:18px; background:rgba(26,23,20,.82); color:var(--light); }
    .sidebar-head { display:flex; align-items:flex-start; gap:6px; padding:0 0 22px; }
    .sidebar-brand { min-width:0; flex:1 1 auto; padding:4px 10px 0; font:11px/1.35 var(--mono); letter-spacing:.13em; color:var(--muted); }
    .sidebar-brand strong { display:block; margin-top:7px; color:var(--light); font:italic 700 1.35rem/1 var(--serif); letter-spacing:0; }
    .sidebar-toggle { flex:0 0 38px; width:38px !important; min-height:38px; margin:0; padding:8px !important; border:1px solid var(--stage-line); border-radius:9px; background:transparent; color:var(--muted); }
    .sidebar-toggle:hover { background:var(--hover); color:var(--light); }
    .sidebar-toggle svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .sidebar-collapsed .sidebar-head { display:block; }
    .sidebar-collapsed .sidebar-brand { padding:4px 0 12px; text-align:center; }
    .sidebar-collapsed .sidebar-brand strong, .sidebar-collapsed .sidebar-user, .sidebar-collapsed .sidebar-logout span { display:none; }
    .sidebar-collapsed .sidebar-toggle { display:grid; place-items:center; margin:0 auto; }
    .sidebar-collapsed .nav-item { position:relative; justify-content:center; padding-left:8px; padding-right:8px; }
    .sidebar-collapsed .nav-label { display:none; }
    .sidebar-collapsed .nav-badge:not([hidden]) { position:absolute; top:2px; right:2px; min-width:16px; height:16px; margin:0; padding:0 3px; font-size:.58rem; }
    .sidebar-collapsed .sidebar-footer { padding-left:0; padding-right:0; }
    .sidebar-collapsed .sidebar-logout { justify-content:center; padding-left:8px; padding-right:8px; }
    .sidebar-nav { display:grid; gap:5px; }
    .nav-item { display:flex; align-items:center; gap:10px; width:100%; min-height:44px; margin:0; padding:10px 11px; border:1px solid transparent; border-radius:10px; background:transparent; color:var(--muted); text-align:left; font:600 13px/1.2 var(--sans); cursor:pointer; }
    .nav-item:hover { background:var(--hover); color:var(--light); }
    .nav-item[aria-current="page"] { background:var(--light); color:var(--ink); }
    .nav-item svg { width:17px; height:17px; flex:0 0 auto; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .sidebar-footer { display:grid; gap:10px; margin-top:auto; padding:18px 10px 2px; border-top:1px solid var(--stage-line); }
    .sidebar-user { overflow:hidden; color:var(--muted); font:11px/1.35 var(--mono); text-overflow:ellipsis; white-space:nowrap; }
    .sidebar-logout { display:flex; align-items:center; justify-content:flex-start; gap:9px; min-height:44px; margin:0; padding:9px 10px; border:1px solid var(--stage-line); border-radius:10px; background:transparent; color:var(--light); font:600 12px/1.2 var(--sans); }
    .sidebar-logout:hover { background:var(--hover); }
    .sidebar-logout svg { width:17px; height:17px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .nav-badge { display:inline-grid; place-items:center; min-width:20px; height:20px; margin-left:auto; padding:0 5px; border-radius:999px; background:var(--accent); color:#241f1a; font:700 .68rem/1 var(--mono); }
    .nav-badge[hidden] { display:none; }
    .nav-item[aria-current="page"] .nav-badge { background:var(--ink); color:var(--light); }
    .workspace { min-width:0; }
    header { position:relative; display:flex; justify-content:space-between; gap:24px; align-items:end; margin:0 0 30px; padding:0; }
    header::before { content:'FAHRA AFFILIATE ROTATOR'; position:absolute; top:0; left:0; font:11px/1 var(--mono); letter-spacing:.16em; color:var(--muted); }
    h1 { font:italic 700 clamp(2.4rem,5vw,4.8rem)/.98 var(--serif); letter-spacing:-.02em; margin:44px 0 12px; max-width:11ch; }
    h1::first-line { color:var(--light); }
    .dek { max-width:580px; color:var(--muted); margin:0; font-size:15px; }
    .signal { min-width:0; flex:1 1 160px; padding:12px 14px; border:1px solid var(--stage-line); background:var(--stage-2); color:var(--light); border-radius:10px; font:12px/1.4 var(--mono); }
    .view[hidden] { display:none; }
    .view-heading { margin:0 0 20px; }
    .view-heading h2 { margin-bottom:6px; }
    .view-heading p { max-width:680px; }
    .history-tools { display:grid; grid-template-columns:1.15fr repeat(2,minmax(150px,.8fr)) auto; gap:10px; align-items:end; margin:0 0 18px; padding:14px; border:1px solid var(--line); border-radius:12px; background:#faf6ee; }
    .history-tools label { margin:0; color:var(--muted); font-size:.82rem; }
    .history-tools select, .history-tools input { width:100%; min-height:42px; margin-top:5px; border:1px solid #cfc7bd; border-radius:8px; background:#fff; color:var(--ink); padding:9px 10px; font:inherit; }
    .history-tools button { width:auto; min-height:42px; margin:0; padding:10px 14px; white-space:nowrap; }
    .history-summary { min-height:20px; margin:-8px 0 12px; }
    .layout { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr); gap:22px; align-items:start; }
    .dashboard-stack { max-width:900px; }
    .dashboard-card { max-width:820px; }
    .recent-card { margin-top:22px; padding-top:22px; border-top:1px solid var(--line); }
    .recent-heading { display:flex; align-items:baseline; justify-content:space-between; gap:12px; }
    .recent-heading h2 { margin-bottom:0; }
    .recent-count { color:var(--muted); font:500 .76rem/1.2 var(--mono); }
    section { min-width:0; background:var(--paper); border:0; border-radius:18px; padding:24px; color:var(--ink); box-shadow:0 18px 40px rgba(0,0,0,.18); }
    h2 { font:700 1.15rem/1.2 var(--sans); margin:0 0 15px; }
    label { display:block; font-weight:650; margin:15px 0 6px; }
    textarea,input,#affiliate-account { width:100%; min-height:44px; border:1px solid #cfc7bd; border-radius:8px; background:#fff; color:var(--ink); padding:11px 12px; font:inherit; }
    #affiliate-account { border-color:#887564; color-scheme:light; }
    #affiliate-account:focus-visible { outline-color:#a94013; }
    #affiliate-account-hint { color:#66594d; }
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
    .job-id { font-family:var(--mono); font-size:.78rem; color:#66594d; }
    .status { font:500 .72rem/1 var(--mono); text-transform:uppercase; letter-spacing:.04em; }
    .completed { color:#207044; } .failed,.needs_session { color:var(--error); } .queued,.running { color:var(--warn); }
    .result { min-width:0; margin-top:9px; display:grid; gap:6px; }
    .result > div { min-width:0; }
    .short-link-row { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
    .result a { display:inline-block; max-width:100%; color:var(--accent); overflow-wrap:anywhere; }
    .result a.copied { color:#207044; }
    .copy-link-action { width:auto; min-height:32px; margin:0; padding:7px 10px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); font-size:.78rem; }
    .copy-link-action:hover { background:#f3eee6; }
    .copy-link-action.copied { color:#207044; border-color:#8bb69d; }
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
    .icon-button { width:44px !important; min-height:44px; padding:0 !important; display:inline-grid; place-items:center; border:1px solid var(--stage-line); background:var(--stage-2); color:var(--light); border-radius:10px; }
    .icon-button svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    .password-panel { margin:0; border:1px solid var(--line); border-radius:14px; padding:18px 20px; background:var(--paper); color:var(--ink); }
    .password-panel[hidden] { display:none; }
    .password-panel button { width:100%; }
    .mcp-panel { margin:0 0 22px; }
    .mcp-panel fieldset { border:0; margin:0; padding:0; min-width:0; }
    .mcp-panel fieldset:disabled { opacity:.58; }
    .mcp-intro { max-width:760px; }
    .mcp-heading { display:flex; align-items:center; justify-content:space-between; gap:14px; }
    .mcp-heading h2 { margin-bottom:0; }
    .mcp-heading button { width:auto; margin:0; white-space:nowrap; }
    .mcp-form { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; align-items:end; margin-top:16px; }
    .mcp-form label { margin:0; }
    .mcp-form button { width:auto; margin:0; white-space:nowrap; }
    .mcp-message { min-height:22px; }
    .mcp-reveal { margin-top:18px; padding:16px; border:1px solid #e6b77e; border-radius:12px; background:#fff7e9; }
    .mcp-reveal[hidden] { display:none; }
    .mcp-secret { display:flex; gap:8px; align-items:stretch; margin:10px 0; }
    .mcp-secret code { flex:1 1 auto; min-width:0; overflow:auto; padding:11px 12px; border-radius:8px; background:#241f1a; color:#fff7e9; font:500 .82rem/1.4 var(--mono); overflow-wrap:anywhere; }
    .mcp-secret button, .mcp-copy { width:auto; margin:0; white-space:nowrap; }
    .mcp-configs { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:12px; }
    .mcp-config { min-width:0; border:1px solid #e3d8c9; border-radius:10px; padding:10px; background:#fffdf8; }
    .mcp-config-head { display:flex; justify-content:space-between; gap:8px; align-items:center; font-weight:700; }
    .mcp-config pre { max-height:180px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; margin:8px 0 0; font:500 .73rem/1.45 var(--mono); color:#3a3028; }
    .mcp-config button { min-height:34px; padding:7px 10px; font-size:.78rem; margin:0; }
    .mcp-list { display:grid; gap:9px; margin-top:17px; }
    .mcp-token { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center; border-top:1px solid var(--line); padding-top:12px; }
    .mcp-token:first-child { border-top:0; padding-top:0; }
    .mcp-token-name { font-weight:700; overflow-wrap:anywhere; }
    .mcp-token-meta { color:var(--muted); font-size:.8rem; }
    .mcp-token-actions { display:flex; flex-wrap:wrap; gap:7px; justify-content:flex-end; }
    .mcp-token-actions button { width:auto; min-height:36px; margin:0; padding:8px 11px; font-size:.78rem; }
    .mcp-token-actions .danger { background:var(--error); }
    .mcp-token-actions .quiet { background:transparent; color:var(--ink); border:1px solid var(--line); }
    .mcp-status { display:inline-block; margin-left:6px; font:500 .7rem/1 var(--mono); text-transform:uppercase; letter-spacing:.04em; }
    .mcp-status.active { color:#207044; } .mcp-status.revoked,.mcp-status.deleted { color:var(--error); }
    .settings-grid { display:grid; grid-template-columns:minmax(0,1fr) 280px; gap:16px; align-items:start; }
    .settings-card { min-width:0; }
    .theme-card { min-width:0; }
    .theme-card select { width:100%; min-height:44px; border:1px solid #cfc7bd; border-radius:8px; background:#fff; color:var(--ink); padding:11px 12px; font:inherit; }
    @media (max-width: 980px) {
      .app-shell { grid-template-columns:190px minmax(0,1fr); gap:20px; }
      .settings-grid { grid-template-columns:1fr; }
    }
    @media (max-width: 760px) {
      main { width:min(100% - 32px, 680px); margin-top:0; margin-bottom:48px; }
      .app-shell { display:block; padding-top:12px; }
      .sidebar { position:sticky; top:0; z-index:10; min-height:0; display:grid; grid-template-columns:auto minmax(0,1fr); gap:14px; align-items:center; padding:10px; border-radius:14px; }
      .sidebar-head { display:block; padding:0; }
      .sidebar-brand { padding:4px 8px; white-space:nowrap; }
      .sidebar-brand strong { margin-top:4px; font-size:1.1rem; }
      .sidebar-toggle { display:none; }
      .sidebar-collapsed .sidebar-head { display:block; }
      .sidebar-collapsed .sidebar-brand { padding:4px 8px; text-align:left; }
      .sidebar-collapsed .sidebar-brand strong { display:block; }
      .sidebar-collapsed .nav-item { position:relative; justify-content:center; padding:10px; }
      .sidebar-collapsed .nav-label { display:none; }
      .sidebar-collapsed .nav-badge:not([hidden]) { position:absolute; top:1px; right:1px; min-width:16px; height:16px; margin:0; padding:0 3px; font-size:.58rem; }
      .sidebar-nav { display:flex; min-width:0; gap:4px; overflow-x:auto; scrollbar-width:none; }
      .sidebar-nav::-webkit-scrollbar { display:none; }
      .nav-item { flex:0 0 auto; width:auto; padding:10px; }
      .nav-item .nav-label { display:none; }
      .nav-item svg { width:18px; height:18px; }
      .sidebar-footer { display:none; }
      header { display:block; margin-bottom:26px; padding-top:0; }
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
      .copy-link-action { width:auto; margin:0; }
      .pagination { flex-wrap:wrap; }
      .pagination button { width:auto; margin-top:0; }
      .pagination .pagination-control { flex:1 1 calc(50% - 5px); }
      .pagination-list { order:3; flex-basis:100%; }
      .header-tools { display:flex; flex-wrap:wrap; }
      .signal { flex:1 1 100%; }
      .mcp-form { grid-template-columns:1fr; }
      .mcp-form button { width:100%; }
      .mcp-heading { align-items:flex-start; }
      .mcp-heading button { width:auto; }
      .history-tools { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .history-tools button { width:100%; }
      .mcp-configs { grid-template-columns:1fr; }
      .mcp-secret { display:grid; }
      .mcp-secret button { width:100%; }
      .mcp-token { grid-template-columns:1fr; }
      .mcp-token-actions { justify-content:flex-start; }
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
<main class="app-shell">
  <aside class="sidebar" aria-label="Navigasi utama">
    <div class="sidebar-head"><div class="sidebar-brand">FAHRA<strong>Affiliate Rotator</strong></div><button id="sidebar-toggle" class="sidebar-toggle" type="button" aria-label="Perkecil sidebar" aria-expanded="true" aria-controls="main-navigation" title="Perkecil sidebar"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5 16 12 9 19"/></svg></button></div>
    <nav id="main-navigation" class="sidebar-nav">
      <button class="nav-item" type="button" data-view-target="dashboard" aria-label="Dashboard" aria-current="page"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/></svg><span class="nav-label">Dashboard</span></button>
      <button class="nav-item" type="button" data-view-target="mcp" aria-label="MCP"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 12h8"/><path d="M9.5 7.5 7 10a2.8 2.8 0 0 0 0 4l2.5 2.5"/><path d="m14.5 7.5 2.5 2.5a2.8 2.8 0 0 1 0 4l-2.5 2.5"/></svg><span class="nav-label">MCP</span></button>
      <button class="nav-item" type="button" data-view-target="settings" aria-label="Pengaturan"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z"/><path d="m19 13.5 1.2 1-.9 1.6-1.5-.5a7.5 7.5 0 0 1-1.6 1l-.2 1.6h-2l-.5-1.5a7.4 7.4 0 0 1-2-.1L10.6 19l-1.8-.8.3-1.6a7.5 7.5 0 0 1-1.5-1.4l-1.5.3-.8-1.8 1.3-1a7.4 7.4 0 0 1 0-2l-1.3-1 .8-1.8 1.6.4a7.5 7.5 0 0 1 1.5-1.5L8.9 5l1.8-.8 1 1.3a7.5 7.5 0 0 1 2-.1l.6-1.5h2l.2 1.6a7.5 7.5 0 0 1 1.6 1l1.5-.5.9 1.6-1.2 1a7.4 7.4 0 0 1 .5 1.9l1.5.7-.3 2-1.6.2a7.4 7.4 0 0 1-.4 1.1Z"/></svg><span class="nav-label">Pengaturan</span></button>
      <button id="history-nav" class="nav-item" type="button" data-view-target="history" aria-label="Riwayat"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14M5 12h14M5 18h9"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg><span class="nav-label">Riwayat</span><span id="history-badge" class="nav-badge" hidden></span></button>
    </nav>
    <div class="sidebar-footer"><div id="account-email" class="sidebar-user">Superadmin</div><button id="logout" class="sidebar-logout" type="button" aria-label="Keluar"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"/><path d="m14 8 4 4-4 4"/><path d="M18 12H9"/></svg><span>Keluar</span></button></div>
  </aside>
  <div class="workspace">
  <header>
    <div><h1 id="view-title">Buat link yang bekerja.</h1><p id="view-dek" class="dek">Masukkan sampai lima URL produk atau halaman Shopee, beri tag pelacakan, lalu biarkan worker mengantrekan hasilnya.</p></div>
    <div class="header-tools"><div class="signal" id="health" role="status">Memeriksa worker...</div></div>
  </header>
  <section id="view-dashboard" class="view" data-view="dashboard" aria-labelledby="create-title">
    <div class="dashboard-stack">
    <div class="dashboard-card">
      <h2 id="create-title">Buat batch link</h2>
      <form id="job-form">
        <label for="affiliate-account">Buat link dari akun</label>
        <select id="affiliate-account" name="account_id" required disabled aria-describedby="affiliate-account-hint"><option value="">Memuat akun...</option></select>
        <p id="affiliate-account-hint" class="hint">Atur nama akun lewat ikon extension pada setiap profil Chrome.</p>
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
    </div>
    <div class="recent-card" aria-labelledby="recent-title">
      <div class="recent-heading"><h2 id="recent-title">Hasil terbaru</h2><span id="recent-count" class="recent-count"></span></div>
      <p class="hint">Lima job terakhir tampil di sini agar short link mudah disalin. Riwayat menyimpan daftar lengkap.</p>
      <div id="recent-jobs" class="jobs"><p class="empty">Memuat hasil terbaru...</p></div>
    </div>
    </div>
  </section>
  <section id="view-history" class="view" data-view="history" aria-labelledby="jobs-title" hidden>
    <div class="view-heading"><h2 id="jobs-title">Riwayat job</h2><p class="hint">Pantau status dan salin short link dari pekerjaan yang sudah dikirim.</p></div>
    <form id="history-filters" class="history-tools" aria-label="Filter riwayat job">
      <label for="history-status">Status<select id="history-status"><option value="all">Semua status</option><option value="queued">Queued</option><option value="running">Running</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="needs_session">Needs session</option></select></label>
      <label for="history-from">Dari tanggal<input id="history-from" type="date"></label>
      <label for="history-to">Sampai tanggal<input id="history-to" type="date"></label>
      <button id="history-reset" type="button">Reset filter</button>
    </form>
    <p id="history-summary" class="hint history-summary" role="status"></p>
    <div id="jobs" class="jobs"><p class="empty">Belum ada job.</p></div>
    <nav id="jobs-pagination" class="pagination" aria-label="Navigasi riwayat job"></nav>
  </section>
  <section id="view-mcp" class="view" data-view="mcp" aria-labelledby="mcp-title" hidden>
  <div id="mcp-panel" class="mcp-panel">
    <div class="mcp-heading"><h2 id="mcp-title">Akses MCP</h2><button id="mcp-test" class="quiet" type="button">Tes koneksi</button></div>
    <p class="hint mcp-intro">Buat token terpisah untuk setiap OpenClaw, Codex, Claude, atau Hermes Agent. Plaintext hanya muncul sekali dan tidak disimpan di browser.</p>
    <fieldset id="mcp-fieldset">
      <form id="mcp-form" class="mcp-form">
        <label for="mcp-name">Nama client<input id="mcp-name" name="name" required pattern="[A-Za-z0-9._-]{1,80}" placeholder="misalnya codex-laptop" autocomplete="off"></label>
        <button type="submit">Generate token</button>
      </form>
    </fieldset>
    <p id="mcp-message" class="hint mcp-message" role="status"></p>
    <div id="mcp-reveal" class="mcp-reveal" hidden>
      <strong>Token baru hanya ditampilkan sekarang.</strong>
      <div class="mcp-secret"><code id="mcp-secret-value"></code><button id="mcp-copy-secret" type="button">Salin token</button></div>
      <p class="hint">Simpan ke environment atau secret manager client sebelum menutup panel ini.</p>
      <div id="mcp-configs" class="mcp-configs"></div>
    </div>
    <div id="mcp-tokens" class="mcp-list"><p class="empty">Memuat token...</p></div>
  </div>
  </section>
  <section id="view-settings" class="view" data-view="settings" aria-labelledby="settings-title" hidden>
    <div class="view-heading"><h2 id="settings-title">Pengaturan</h2><p class="hint">Kelola akses akun dan preferensi tampilan dashboard.</p></div>
    <div class="settings-grid">
      <section id="password-panel" class="password-panel" aria-labelledby="password-title">
        <h2 id="password-title">Password superadmin</h2>
        <p id="password-hint" class="hint">Masukkan password saat ini dan password baru.</p>
        <form id="password-form">
          <label for="current-password">Password saat ini</label>
          <input id="current-password" type="password" autocomplete="current-password" required>
          <label for="new-password">Password baru</label>
          <input id="new-password" type="password" autocomplete="new-password" minlength="12" required>
          <label for="new-password-confirmation">Ulangi password baru</label>
          <input id="new-password-confirmation" type="password" autocomplete="new-password" minlength="12" required>
          <button type="submit">Simpan password</button>
          <p id="password-message" class="hint" role="status"></p>
        </form>
      </section>
      <section class="theme-card" aria-labelledby="theme-title">
        <h2 id="theme-title">Tema tampilan</h2>
        <p class="hint">Pilih tampilan yang nyaman untuk sesi ini.</p>
        <label class="sr-only" for="theme-select">Tema tampilan</label>
        <select id="theme-select">
          <option value="system">Sistem</option>
          <option value="light">Terang</option>
          <option value="dark">Gelap</option>
        </select>
      </section>
    </div>
  </section>
</div>
</main>
<script>
const $ = (s) => document.querySelector(s);
const JOBS_PER_PAGE = 5;
let jobsCache = [];
let currentPage = 1;
let currentUser = null;
let mcpUrl = '';
let mcpRevealToken = '';
const SIDEBAR_PREF_KEY = 'fahra-sidebar-collapsed-v2';
if (localStorage.getItem('fahra-sidebar-collapsed') !== null && localStorage.getItem(SIDEBAR_PREF_KEY) === null) localStorage.removeItem('fahra-sidebar-collapsed');
let sidebarCollapsed = localStorage.getItem(SIDEBAR_PREF_KEY) === '1';
const mcpClientLabels = { openclaw: 'OpenClaw', codex: 'Codex CLI', claude: 'Claude / Claude Code', hermes: 'Hermes Agent' };
const viewMeta = {
  dashboard: { title: 'Buat link yang bekerja.', dek: 'Masukkan sampai lima URL produk atau halaman Shopee, beri tag pelacakan, lalu biarkan worker mengantrekan hasilnya.' },
  mcp: { title: 'Akses MCP.', dek: 'Kelola token dan koneksi client untuk OpenClaw, Codex, Claude, dan Hermes.' },
  settings: { title: 'Pengaturan.', dek: 'Atur password superadmin dan tema tampilan dashboard.' },
  history: { title: 'Riwayat job.', dek: 'Pantau status pekerjaan dan salin short link yang sudah selesai.' }
};
function showView(view) {
  if (!viewMeta[view]) view = 'dashboard';
  document.querySelectorAll('[data-view]').forEach((element) => { element.hidden = element.dataset.view !== view; });
  document.querySelectorAll('[data-view-target]').forEach((element) => {
    if (element.dataset.viewTarget === view) element.setAttribute('aria-current', 'page');
    else element.removeAttribute('aria-current');
  });
  $('#view-title').textContent = viewMeta[view].title;
  $('#view-dek').textContent = viewMeta[view].dek;
  if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
}
function applySidebarState() {
  const shell = document.querySelector('.app-shell');
  const toggle = $('#sidebar-toggle');
  shell.classList.toggle('sidebar-collapsed', sidebarCollapsed);
  toggle.setAttribute('aria-expanded', String(!sidebarCollapsed));
  toggle.setAttribute('aria-label', sidebarCollapsed ? 'Perlebar sidebar' : 'Perkecil sidebar');
  toggle.title = sidebarCollapsed ? 'Perlebar sidebar' : 'Perkecil sidebar';
  toggle.innerHTML = sidebarCollapsed
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5-7 7 7 7"/></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>';
}
function updateHistoryBadge() {
  const activeCount = jobsCache.filter((job) => ['queued', 'running'].includes(job.status)).length;
  const badge = $('#history-badge');
  const nav = $('#history-nav');
  badge.hidden = activeCount === 0;
  badge.textContent = activeCount ? (activeCount > 99 ? '99+' : String(activeCount)) : '';
  nav.setAttribute('aria-label', activeCount ? `Riwayat, ${activeCount} job aktif` : 'Riwayat');
}
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
  $('#password-panel').hidden = false;
  $('#password-hint').textContent = locked ? 'Untuk keamanan, ganti password bootstrap sebelum membuat job.' : 'Masukkan password saat ini dan password baru.';
  $('#job-form').querySelectorAll('input,textarea,select,button').forEach(control => { control.disabled = locked; });
  $('#mcp-fieldset').disabled = locked;
  $('#mcp-test').disabled = locked;
  $('#mcp-tokens').querySelectorAll('button').forEach(control => { control.disabled = locked; });
  if (locked) $('#mcp-message').textContent = 'Ganti password awal sebelum membuat atau mengelola token MCP.';
  if (locked) $('#form-message').textContent = 'Ganti password awal di Pengaturan untuk membuka pembuatan job.';
}
function formatMcpDate(value) { return value ? new Date(value).toLocaleString('id-ID') : 'Belum pernah'; }
function mcpSnippet(kind, token) {
  const url = mcpUrl || `${location.protocol}//${location.hostname}:8790/mcp`;
  const env = 'FAHRA_AFF_MCP_TOKEN';
  if (kind === 'openclaw') return JSON.stringify({ mcp: { servers: { 'fahra-aff-rotator': { transport: 'streamable-http', url, headers: { Authorization: 'Bearer ${FAHRA_AFF_MCP_TOKEN}' } } } } }, null, 2);
  if (kind === 'codex') return `# Save the token in your OS secret store, then export it before starting Codex.\nexport ${env}='<paste token>'\ncodex mcp add fahra-aff-rotator --url '${url}' --bearer-token-env-var ${env}`;
  if (kind === 'claude') return JSON.stringify({ mcpServers: { 'fahra-aff-rotator': { type: 'http', url, headers: { Authorization: 'Bearer ${FAHRA_AFF_MCP_TOKEN}' } } } }, null, 2) + `\n\n# Set ${env} in your shell or secret manager before starting Claude Code.`;
  return `# ~/.hermes/config.yaml\nmcp_servers:\n  fahra-aff-rotator:\n    url: '${url}'\n    headers:\n      Authorization: 'Bearer ${'${FAHRA_AFF_MCP_TOKEN}'}'\n\n# Add ${env}=<paste token> to ~/.hermes/.env.`;
}
function renderMcpConfigs(token) {
  $('#mcp-configs').innerHTML = Object.entries(mcpClientLabels).map(([kind, label]) => `<article class="mcp-config"><div class="mcp-config-head"><span>${escapeHtml(label)}</span><button class="mcp-copy" type="button" data-mcp-copy="${escapeHtml(kind)}">Salin</button></div><pre id="mcp-snippet-${escapeHtml(kind)}"></pre></article>`).join('');
  Object.keys(mcpClientLabels).forEach(kind => { $(`#mcp-snippet-${kind}`).textContent = mcpSnippet(kind, token); });
}
function showMcpToken(token) {
  mcpRevealToken = token;
  $('#mcp-secret-value').textContent = token;
  renderMcpConfigs(token);
  $('#mcp-reveal').hidden = false;
  $('#mcp-reveal').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function renderMcpTokens(tokens) {
  const container = $('#mcp-tokens');
  if (!tokens.length) { container.innerHTML = '<p class="empty">Belum ada token MCP.</p>'; return; }
  const lockedAttribute = currentUser?.must_change_password ? ' disabled' : '';
  container.innerHTML = tokens.map(token => {
    const isActive = token.status === 'active';
    const action = isActive ? `<button type="button"${lockedAttribute} data-mcp-action="revoke" data-mcp-name="${escapeHtml(token.name)}">Cabut akses</button>` : '';
    return `<article class="mcp-token"><div><div class="mcp-token-name">${escapeHtml(token.name)}<span class="mcp-status ${escapeHtml(token.status)}">${escapeHtml(token.status)}</span></div><div class="mcp-token-meta">Dibuat ${escapeHtml(formatMcpDate(token.created_at))} · Terakhir dipakai ${escapeHtml(formatMcpDate(token.last_used_at))}</div></div><div class="mcp-token-actions">${action}<button class="danger" type="button"${lockedAttribute} data-mcp-action="delete" data-mcp-name="${escapeHtml(token.name)}">Hapus</button></div></article>`;
  }).join('');
}
async function loadMcpTokens() {
  const data = await api('/api/mcp/tokens');
  mcpUrl = data.mcp_url || mcpUrl;
  renderMcpTokens(Array.isArray(data.tokens) ? data.tokens : []);
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
let accountsSignature = '';
function renderAccounts(accounts) {
  const signature = JSON.stringify(accounts.map(({id, name, ready}) => ({id, name, ready})));
  if (accountsSignature === signature) return;
  accountsSignature = signature;
  const select = $('#affiliate-account');
  const chosen = select.value;
  select.innerHTML = '<option value="">Pilih akun affiliate</option>' + accounts.map(account =>
    `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)} (${escapeHtml(account.id.slice(0, 6))}) · ${account.ready ? 'Siap' : 'Tidak terhubung'}</option>`
  ).join('');
  select.value = accounts.some(account => account.id === chosen) ? chosen : '';
  $('#affiliate-account-hint').textContent = accounts.length
    ? 'Job menunggu profil akun yang dipilih. Biarkan satu tab Custom Link terbuka pada profil itu.'
    : 'Belum ada akun. Klik ikon extension pada setiap profil Chrome, isi nama akun, lalu login dashboard lokal dan buka tab Custom Link.';
}
function renderJob(job) {
  const results = Array.isArray(job.result) ? job.result.map((row, i) => row.shortLink ? `<div class="short-link-row"><span>${i + 1}. </span><a class="copy-link" href="${escapeHtml(row.shortLink)}" data-copy="${escapeHtml(row.shortLink)}" target="_blank" rel="noreferrer" aria-label="Salin short link ${escapeHtml(row.shortLink)}">${escapeHtml(row.shortLink)}</a><button class="copy-link-action" type="button" data-copy="${escapeHtml(row.shortLink)}" aria-label="Salin ${escapeHtml(row.shortLink)}">Salin</button><span class="copy-feedback" role="status" aria-live="polite"></span></div>` : `<div>${i + 1}. Gagal, failCode ${escapeHtml(row.failCode ?? '?')}</div>`).join('') : '';
  const source = String(job.links?.[0] || '');
  return `<article class="job"><div class="job-head"><div><a class="job-source" href="${escapeHtml(source)}" target="_blank" rel="noreferrer" title="${escapeHtml(source)}" aria-label="Buka URL sumber: ${escapeHtml(source)}">${escapeHtml(sourceLabel(source))}</a><div class="job-id">${escapeHtml(job.id)} · ${escapeHtml(job.account_name || 'Akun belum dicatat')}${job.account_id ? ` (${escapeHtml(job.account_id.slice(0, 6))})` : ''}</div></div><span class="status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span></div>${results ? `<div class="result">${results}</div>` : ''}${job.error ? `<p class="error">${escapeHtml(job.error)}</p>` : ''}</article>`;
}
function renderPagination() {
  const pagination = $('#jobs-pagination');
  const pageCount = Math.ceil(jobsCache.length / JOBS_PER_PAGE);
  if (pageCount <= 1) { pagination.innerHTML = ''; return; }
  const pages = Array.from({length: pageCount}, (_, i) => i + 1).map(page => `<button type="button" data-page="${page}"${page === currentPage ? ' aria-current="page"' : ''} aria-label="Halaman ${page}">${page}</button>`).join('');
  pagination.innerHTML = `<button class="pagination-control" type="button" data-page="${currentPage - 1}"${currentPage === 1 ? ' disabled' : ''}>Sebelumnya</button><div class="pagination-list">${pages}</div><button class="pagination-control" type="button" data-page="${currentPage + 1}"${currentPage === pageCount ? ' disabled' : ''}>Berikutnya</button>`;
}
function filteredJobs() {
  const status = $('#history-status').value;
  const from = $('#history-from').value;
  const to = $('#history-to').value;
  return jobsCache.filter((job) => {
    if (status !== 'all' && job.status !== status) return false;
    const date = String(job.created_at || '').slice(0, 10);
    if (from && date < from) return false;
    if (to && date > to) return false;
    return true;
  });
}
function renderJobs() {
  const matchingJobs = filteredJobs();
  const pageCount = Math.ceil(matchingJobs.length / JOBS_PER_PAGE);
  currentPage = Math.min(Math.max(currentPage, 1), Math.max(pageCount, 1));
  const start = (currentPage - 1) * JOBS_PER_PAGE;
  const visibleJobs = matchingJobs.slice(start, start + JOBS_PER_PAGE);
  $('#jobs').innerHTML = visibleJobs.length ? visibleJobs.map(renderJob).join('') : `<p class="empty">${jobsCache.length ? 'Tidak ada job yang cocok dengan filter.' : 'Belum ada job. Kirim URL pertama untuk memulai.'}</p>`;
  $('#history-summary').textContent = matchingJobs.length === jobsCache.length
    ? `${jobsCache.length} job tersedia.`
    : `${matchingJobs.length} dari ${jobsCache.length} job cocok dengan filter.`;
  renderPagination();
}
function renderRecentJobs() {
  const recentJobs = jobsCache.slice(0, 5);
  $('#recent-count').textContent = recentJobs.length ? `${recentJobs.length} terbaru` : '';
  $('#recent-jobs').innerHTML = recentJobs.length
    ? recentJobs.map(renderJob).join('')
    : '<p class="empty">Belum ada hasil. Kirim URL pertama untuk memulai.</p>';
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
  const [health, jobs, accounts] = await Promise.all([api('/api/health'), api('/api/jobs'), api('/api/accounts')]);
  currentUser = me.user;
  $('#account-email').textContent = currentUser.email;
  applyTheme(localStorage.getItem('fahra-theme') || 'system');
  $('#health').textContent = health.worker ? (health.bridge?.connected ? 'Bridge aktif' : 'Menunggu extension') : 'Worker berhenti';
  jobsCache = Array.isArray(jobs.jobs) ? jobs.jobs : [];
  updateHistoryBadge();
  updatePasswordLock();
  renderAccounts(accounts.accounts || []);
  renderJobs();
  renderRecentJobs();
  try { await loadMcpTokens(); } catch (error) { if (!currentUser?.must_change_password) $('#mcp-message').textContent = error.message; }
}
applySidebarState();
$('#sidebar-toggle').addEventListener('click', () => { sidebarCollapsed = !sidebarCollapsed; localStorage.setItem(SIDEBAR_PREF_KEY, sidebarCollapsed ? '1' : '0'); applySidebarState(); });
document.querySelectorAll('[data-view-target]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.viewTarget)));
window.addEventListener('hashchange', () => showView(location.hash.slice(1)));
$('#theme-select').addEventListener('change', () => applyTheme($('#theme-select').value));
applyTheme(localStorage.getItem('fahra-theme') || 'system');
showView(location.hash.slice(1) || 'dashboard');
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
    await refresh();
  } catch (error) { message.textContent = error.message; } finally { button.disabled = false; }
});
$('#mcp-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const message = $('#mcp-message');
  button.disabled = true; message.textContent = 'Membuat token...';
  try {
    const data = await api('/api/mcp/tokens', { method: 'POST', body: JSON.stringify({ name: $('#mcp-name').value.trim() }) });
    $('#mcp-name').value = '';
    message.textContent = 'Token dibuat. Simpan sekarang karena plaintext tidak akan ditampilkan lagi.';
    showMcpToken(data.token);
    await loadMcpTokens();
  } catch (error) { message.textContent = error.message; } finally { button.disabled = false; }
});
$('#mcp-test').addEventListener('click', async () => {
  const button = $('#mcp-test');
  const message = $('#mcp-message');
  button.disabled = true;
  message.textContent = 'Memeriksa koneksi MCP...';
  try {
    const data = await api('/api/mcp/test', { method: 'POST' });
    message.textContent = data.message || 'MCP aktif.';
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = Boolean(currentUser?.must_change_password);
  }
});
$('#mcp-copy-secret').addEventListener('click', async () => {
  const copied = await copyText(mcpRevealToken);
  $('#mcp-message').textContent = copied ? 'Token tersalin.' : 'Salin token dari kotak di atas secara manual.';
});
$('#mcp-configs').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-mcp-copy]');
  if (!button) return;
  const kind = button.dataset.mcpCopy;
  const copied = await copyText(mcpSnippet(kind, mcpRevealToken));
  button.textContent = copied ? 'Tersalin' : 'Salin';
  window.setTimeout(() => { button.textContent = 'Salin'; }, 1800);
});
$('#mcp-tokens').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-mcp-action]');
  if (!button) return;
  const name = button.dataset.mcpName;
  const action = button.dataset.mcpAction;
  const prompt = action === 'revoke' ? `Cabut akses token "${name}" sekarang?` : `Hapus token "${name}" secara permanen dari daftar?`;
  if (!window.confirm(prompt)) return;
  button.disabled = true;
  try {
    await api(`/api/mcp/tokens/${encodeURIComponent(name)}${action === 'revoke' ? '/revoke' : ''}`, { method: action === 'revoke' ? 'POST' : 'DELETE' });
    $('#mcp-message').textContent = action === 'revoke' ? 'Akses token dicabut.' : 'Token dihapus dan aksesnya dicabut.';
    if (mcpRevealToken) { $('#mcp-reveal').hidden = true; mcpRevealToken = ''; }
    await loadMcpTokens();
  } catch (error) { $('#mcp-message').textContent = error.message; button.disabled = false; }
});
$('#jobs-pagination').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-page]');
  if (!button || button.disabled) return;
  currentPage = Number(button.dataset.page);
  renderJobs();
});
$('#history-filters').addEventListener('submit', (event) => event.preventDefault());
['history-status', 'history-from', 'history-to'].forEach((id) => {
  ['input', 'change'].forEach((eventName) => $(`#${id}`).addEventListener(eventName, () => { currentPage = 1; renderJobs(); }));
});
$('#history-reset').addEventListener('click', () => {
  $('#history-status').value = 'all';
  $('#history-from').value = '';
  $('#history-to').value = '';
  currentPage = 1;
  renderJobs();
});
async function handleJobCopy(event) {
  const link = event.target.closest('a.copy-link, button.copy-link-action');
  if (!link) return;
  if (link.tagName === 'A') event.preventDefault();
  const copied = await copyText(link.dataset.copy || link.textContent.trim());
  showCopyFeedback(link, copied ? 'Link tersalin' : 'Salin manual dari link ini', copied);
  if (link.classList.contains('copy-link-action')) {
    link.textContent = copied ? 'Tersalin' : 'Salin';
    window.clearTimeout(link.copyLabelTimer);
    link.copyLabelTimer = window.setTimeout(() => { link.textContent = 'Salin'; }, 2200);
  }
}
$('#jobs').addEventListener('click', handleJobCopy);
$('#recent-jobs').addEventListener('click', handleJobCopy);
$('#job-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = $('#submit'); button.disabled = true; $('#form-message').textContent = 'Menyimpan job...';
  const tags = [1,2,3,4,5].map(i => event.target.elements[`tag${i}`].value.trim());
  try { const data = await api('/api/jobs', {method:'POST', body:JSON.stringify({links:$('#links').value, tags, account_id:$('#affiliate-account').value})}); $('#links').value=''; [1,2,3,4,5].forEach(i => event.target.elements[`tag${i}`].value=''); $('#form-message').textContent = `Job ${data.id} masuk antrean.`; await refresh(); } catch (error) { $('#form-message').textContent = error.message; } finally { button.disabled = false; }
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
    if "--create-mcp-token" in os.sys.argv:
        index = os.sys.argv.index("--create-mcp-token")
        if index + 1 >= len(os.sys.argv):
            raise SystemExit("Gunakan: app.py --create-mcp-token NAMA_AGENT")
        create_mcp_token(os.sys.argv[index + 1])
        return
    if "--revoke-mcp-token" in os.sys.argv:
        index = os.sys.argv.index("--revoke-mcp-token")
        if index + 1 >= len(os.sys.argv):
            raise SystemExit("Gunakan: app.py --revoke-mcp-token NAMA_AGENT")
        revoke_mcp_token(os.sys.argv[index + 1])
        return
    server = ThreadingHTTPServer((os.getenv("HOST", "127.0.0.1"), PORT), Handler)
    mcp_server = ThreadingHTTPServer((MCP_HOST, MCP_PORT), MCPHandler)
    WORKER.start()
    mcp_thread = threading.Thread(target=mcp_server.serve_forever, name="affiliate-mcp", daemon=True)
    mcp_thread.start()
    print(f"Fahra Affiliate Rotator berjalan di http://{server.server_address[0]}:{PORT}")
    print(f"MCP OpenClaw berjalan di http://{mcp_server.server_address[0]}:{MCP_PORT}/mcp")
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
        mcp_server.shutdown()
        mcp_server.server_close()


if __name__ == "__main__":
    main()
