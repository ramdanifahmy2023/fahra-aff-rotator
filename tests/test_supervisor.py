"""Exercise real Windows process recovery without touching the live app/database."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]

FAKE_APP = '''
import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    server_version = "FahraAffiliateRotator/test"
    def do_GET(self):
        if self.path == "/fail":
            self.server.failed = True
        body = json.dumps({"status":"ok", "worker":not getattr(self.server,"failed",False), "bridge":{"connected":False}}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass
class MCP(Handler):
    server_version = "FahraAffiliateRotatorMCP/test"
    def do_GET(self): self.send_response(401); self.end_headers()
mcp = ThreadingHTTPServer(("127.0.0.1",int(os.environ["MCP_PORT"])),MCP)
threading.Thread(target=mcp.serve_forever,daemon=True).start()
ThreadingHTTPServer(("127.0.0.1",int(os.environ["PORT"])),Handler).serve_forever()
'''


@unittest.skipUnless(os.name == "nt", "Windows Job Objects and file locks")
class SupervisorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        shutil.copyfile(ROOT / "runtime_supervisor.py", self.root / "runtime_supervisor.py")
        (self.root / "app.py").write_text(FAKE_APP, encoding="utf-8")
        with socket.socket() as first, socket.socket() as second:
            first.bind(("127.0.0.1", 0)); second.bind(("127.0.0.1", 0))
            self.port, mcp_port = first.getsockname()[1], second.getsockname()[1]
        (self.root / ".env").write_text(f"PORT={self.port}\nMCP_PORT={mcp_port}\nMCP_HOST=127.0.0.1\n")
        # Use the real interpreter, not a Windows venv launcher that creates another process.
        python = getattr(sys, "_base_executable", sys.executable)
        config = self.root / "config.json"
        config.write_text(json.dumps({"python": python}))
        self.command = [python, str(self.root / "runtime_supervisor.py"), "worker", "--config", str(config)]
        self.supervisor = subprocess.Popen(self.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_supervisor)

    def stop_supervisor(self):
        if self.supervisor.poll() is None:
            self.supervisor.kill()
        self.supervisor.wait(timeout=5)
        time.sleep(0.5)

    def state(self):
        try:
            return json.loads((self.root / "data/runtime/worker.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.state()
            if predicate(current):
                return current
            if self.supervisor.poll() is not None:
                log = self.root / "data/runtime/worker.log"
                self.fail(log.read_text() if log.exists() else "Watchdog exited before logging")
            time.sleep(0.25)
        self.fail(f"Watchdog did not recover: {self.state()}")

    def test_crash_unhealthy_recovery_single_instance_and_child_cleanup(self):
        healthy = self.wait_for(lambda s: s.get("status") == "healthy")
        self.assertFalse(healthy["bridge_connected"], "No Chrome must not restart a healthy worker")
        duplicate = subprocess.run(self.command, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(duplicate.returncode, 0)
        self.assertEqual(self.state()["child_pid"], healthy["child_pid"])
        subprocess.run(["taskkill", "/F", "/PID", str(healthy["child_pid"])], check=True, stdout=subprocess.DEVNULL)
        recovered = self.wait_for(lambda s: s.get("status") == "healthy" and s.get("starts",0) >= 2)
        self.assertNotEqual(recovered["child_pid"], healthy["child_pid"])
        with urlopen(f"http://127.0.0.1:{self.port}/fail", timeout=2): pass
        restarted = self.wait_for(lambda s: s.get("status") == "healthy" and s.get("starts",0) >= 3)
        self.assertNotEqual(restarted["child_pid"], recovered["child_pid"])
        self.stop_supervisor()
        with socket.socket() as connection:
            self.assertNotEqual(connection.connect_ex(("127.0.0.1", self.port)), 0, "Child survived a killed watchdog")


if __name__ == "__main__":
    unittest.main()
