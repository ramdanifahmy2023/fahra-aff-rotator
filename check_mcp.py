"""Verify a local or tunneled MCP endpoint without printing credentials or jobs."""

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def check(url, token):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    try:
        with urlopen(Request(url, headers=headers), timeout=10):
            raise RuntimeError("Endpoint menerima request tanpa token.")
    except HTTPError as error:
        if error.code != 401:
            raise RuntimeError(f"Endpoint tanpa token: HTTP {error.code}, seharusnya 401.") from None
    headers["Authorization"] = f"Bearer {token}"
    request_id = 0

    def rpc(method, params=None, *, notification=False):
        nonlocal request_id
        request_id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            payload["id"] = request_id
        with urlopen(Request(url, data=json.dumps(payload).encode(), headers=headers), timeout=20) as response:
            if notification:
                if response.status != 202:
                    raise RuntimeError("Notifikasi initialize tidak diterima.")
                return None
            result = json.load(response)
        if result.get("id") != request_id or "error" in result:
            raise RuntimeError(f"RPC gagal: {method}")
        return result["result"]

    initialized = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fahra-check", "version": "1.0"}})
    headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
    rpc("notifications/initialized", notification=True)
    names = sorted(tool["name"] for tool in rpc("tools/list")["tools"])
    expected = {"shopee_generate_short_links", "shopee_get_job", "shopee_wait_for_job", "shopee_health", "shopee_list_recent_jobs"}
    if not expected.issubset(names):
        raise RuntimeError("Daftar tool MCP tidak lengkap.")
    result = rpc("tools/call", {"name": "shopee_health", "arguments": {}})
    if result.get("isError"):
        raise RuntimeError("Tool shopee_health gagal.")
    health = json.loads(result["content"][0]["text"])
    print(json.dumps({"authentication": "ok", "initialize": "ok", "tools": names, "worker": health.get("worker"), "bridge_connected": health.get("bridge", {}).get("connected")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8790/mcp")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else os.getenv("FAHRA_AFF_MCP_TOKEN", "")
    if not token:
        parser.error("Gunakan --token-file atau environment FAHRA_AFF_MCP_TOKEN.")
    try:
        check(args.url, token)
    except HTTPError as error:
        raise SystemExit(f"MCP gagal: HTTP {error.code}.") from None
    except (OSError, URLError, ValueError, KeyError, RuntimeError) as error:
        raise SystemExit(f"MCP gagal: {type(error).__name__}. Periksa listener, tunnel, dan token.") from None
