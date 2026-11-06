import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import check_mcp


class QuietMCPHandler(app.MCPHandler):
    def log_message(self, *args):
        pass


class McpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(app, "DB_PATH", Path(self.temp.name) / "test.sqlite3"))
        self.patches.enter_context(patch.object(app, "MCP_TOKEN_FILE", Path(self.temp.name) / "test.token"))
        app.init_db()
        self.token, _ = app.issue_mcp_token("test-client")
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), QuietMCPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_streamable_http_handshake_tools_and_health(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            check_mcp.check(self.url, self.token)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["authentication"], "ok")
        self.assertEqual(len(summary["tools"]), 6)
        self.assertIn("shopee_list_accounts", summary["tools"])
        self.assertNotIn(self.token, output.getvalue())

    def test_revoked_token_cannot_access_tools(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app.revoke_mcp_token("test-client")
        request = Request(self.url, data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 401)

    def test_dashboard_probes_local_listener_when_client_url_is_remote(self):
        with patch.object(app, "MCP_HOST", "127.0.0.1"), patch.object(app, "MCP_PORT", self.server.server_port), patch.object(app, "MCP_PUBLIC_URL", "http://vps.invalid:18790/mcp"):
            result = app.probe_mcp_listener()
        self.assertTrue(result["ok"])
        self.assertEqual(result["scope"], "local")
        self.assertEqual(result["status"], 401)


if __name__ == "__main__":
    unittest.main()
