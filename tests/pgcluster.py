"""A throw-away PostgreSQL cluster for the tests (or TEST_DATABASE_URL, if set).

The tests need a real PostgreSQL with the pgvector, unaccent and pg_trgm extensions,
because search depends on them. Locally this starts its own cluster from the installed
PostgreSQL binaries (pg_config --bindir); as root it runs them as the 'postgres' user.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def _bindir() -> Path:
    found = shutil.which("pg_config")
    if found:
        out = subprocess.run([found, "--bindir"], capture_output=True, text=True, check=True).stdout.strip()
        if (Path(out) / "initdb").exists():
            return Path(out)
    for candidate in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (candidate / "initdb").exists():
            return candidate
    raise RuntimeError("PostgreSQL server binaries not found; install postgresql or set TEST_DATABASE_URL")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    def __init__(self) -> None:
        self.bindir = _bindir()
        self.root = Path(tempfile.mkdtemp(prefix="dwyt-pg-"))
        self.port = _free_port()
        self.as_postgres = os.geteuid() == 0
        if self.as_postgres:
            shutil.chown(self.root, "postgres")

    def _run(self, *args: str) -> None:
        cmd = [str(self.bindir / args[0]), *args[1:]]
        if self.as_postgres:
            cmd = ["runuser", "-u", "postgres", "--", *cmd]
        subprocess.run(cmd, check=True, capture_output=True)

    def start(self) -> str:
        data = self.root / "data"
        self._run("initdb", "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8",
                  "--locale=C.UTF-8")
        self._run("pg_ctl", "-D", str(data), "-l", str(self.root / "log"), "-w", "-o",
                  f"-p {self.port} -k {self.root} -c listen_addresses=127.0.0.1 -c fsync=off", "start")
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        return f"postgresql+psycopg://postgres@127.0.0.1:{self.port}/postgres"

    def stop(self) -> None:
        try:
            self._run("pg_ctl", "-D", str(self.root / "data"), "-m", "immediate", "stop")
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
