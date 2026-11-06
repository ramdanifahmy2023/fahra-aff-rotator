"""Windows watchdog for the local worker and its private reverse SSH tunnel."""

import argparse
import contextlib
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


def read_environment(path):
    result = dict(os.environ)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
            if not match:
                raise ValueError("Invalid .env assignment")
            name, value = match.groups()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[name] = value
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    result["PYTHONUNBUFFERED"] = "1"
    return result


@contextlib.contextmanager
def single_instance(path):
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class ChildJob:
    """Kill only our children if the watchdog is stopped or crashes."""

    def __init__(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill()
            process.wait()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def worker_health(environment):
    port = int(environment.get("PORT", 8788))
    with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
        if not response.headers.get("Server", "").startswith("FahraAffiliateRotator/"):
            raise RuntimeError("Unexpected server on worker port")
        health = json.load(response)
    if health.get("status") != "ok" or not health.get("worker"):
        raise RuntimeError("Worker thread is unavailable")
    host = environment.get("MCP_HOST", "127.0.0.1")
    host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with urlopen(f"http://{host}:{int(environment.get('MCP_PORT', 8790))}/mcp", timeout=3):
            raise RuntimeError("MCP listener accepted an unauthenticated request")
    except HTTPError as error:
        if error.code != 401 or not error.headers.get("Server", "").startswith("FahraAffiliateRotatorMCP/"):
            raise RuntimeError("MCP listener health failed") from None
    return bool(health.get("bridge", {}).get("connected"))


def build_command(mode, config, environment):
    if mode == "worker":
        return [config["python"], "-u", str(ROOT / "app.py")]
    target = config["ssh_target"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", target):
        raise ValueError("Invalid SSH target")
    remote_port = int(config.get("remote_port", 18790))
    local_port = int(environment.get("MCP_PORT", 8790))
    if not all(1024 <= port <= 65535 for port in (remote_port, local_port)):
        raise ValueError("Invalid tunnel port")
    return [config["ssh"], "-N", "-T", "-i", config["ssh_key_file"],
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-o", f"UserKnownHostsFile={config['ssh_known_hosts']}",
            "-R", f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}", target]


def run(mode, config_path):
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    runtime = ROOT / "data/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"fahra.{mode}")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(runtime / f"{mode}.log", maxBytes=1_048_576, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    state = {"mode": mode, "supervisor_pid": os.getpid(), "child_pid": None, "starts": 0}

    def update(status, **details):
        state.update(status=status, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **details)
        temporary = runtime / f"{mode}.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        # Windows readers/antivirus may briefly hold the destination without
        # FILE_SHARE_DELETE. A status reader must never bring down the service.
        for attempt in range(20):
            try:
                os.replace(temporary, runtime / f"{mode}.json")
                return
            except PermissionError:
                time.sleep(0.05)
        logger.warning("Status file is busy; will update on the next health check")

    with single_instance(runtime / f"{mode}.lock") as acquired:
        if not acquired:
            logger.info("Another %s watchdog is already running", mode)
            return
        job = ChildJob()
        process = None
        try:
            logger.info("Watchdog started")
            while True:
                environment = read_environment(ROOT / ".env")
                command = build_command(mode, config, environment)
                if mode == "worker":
                    # Never terminate or duplicate a process we did not start.
                    with socket.socket() as probe:
                        probe.settimeout(0.5)
                        if probe.connect_ex(("127.0.0.1", int(environment.get("PORT", 8788)))) == 0:
                            update("waiting_for_existing_worker", child_pid=None)
                            time.sleep(5)
                            continue
                update("starting", child_pid=None)
                process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding="utf-8", errors="replace",
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign(process)
                state["starts"] += 1
                update("checking", child_pid=process.pid)
                logger.info("Child started (pid %s)", process.pid)

                def capture_output(stream):
                    with stream:
                        for line in stream:
                            logger.info("child: %s", line.rstrip()[:1000])

                reader = threading.Thread(target=capture_output, args=(process.stdout,), daemon=True)
                reader.start()
                failures = 0
                previous_bridge = None
                started_at = time.monotonic()
                while process.poll() is None:
                    time.sleep(5)
                    if process.poll() is not None:
                        break
                    if mode == "worker":
                        try:
                            bridge = worker_health(environment)
                            failures = 0
                            update("healthy", bridge_connected=bridge, consecutive_failures=0)
                            if bridge != previous_bridge:
                                logger.info("Browser bridge %s", "ready" if bridge else "waiting for Chrome/login; worker is healthy")
                                previous_bridge = bridge
                        except (OSError, ValueError, RuntimeError) as error:
                            failures += 1
                            update("unhealthy", consecutive_failures=failures)
                            logger.warning("Health check failed (%s/3): %s", failures, type(error).__name__)
                            if failures >= 3:
                                logger.warning("Restarting unresponsive worker")
                                process.terminate()
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                    else:
                        update("ssh_running", uptime_seconds=int(time.monotonic() - started_at))
                process.wait()
                reader.join(timeout=3)
                logger.warning("Child exited (%s); retrying in 5 seconds", process.returncode)
                update("retrying", child_pid=None, last_exit_code=process.returncode)
                time.sleep(5)
        finally:
            job.close()
            update("stopped", child_pid=None)
            logger.info("Watchdog stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["worker", "tunnel"])
    parser.add_argument("--config", type=Path, default=ROOT / "data/runtime-config.json")
    args = parser.parse_args()
    try:
        run(args.mode, args.config)
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger(f"fahra.{args.mode}").exception("Watchdog stopped unexpectedly")
        sys.exit(1)
