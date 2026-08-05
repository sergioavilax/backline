#!/usr/bin/env python3
"""`make doctor` — verify the local environment can run Backline.

Stdlib-only on purpose: this must run on a fresh clone before any dependency install.
Checks: docker + compose, port availability, env file, and WSL/line-ending sanity
(this repo is developed under WSL2; CRLF or /mnt/c checkouts are the classic traps).

Exit code 0 = all required checks passed (warnings allowed), 1 = at least one failure.
"""

import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PORTS = {"db (Postgres)": 5432, "api (FastAPI)": 8000, "ui (Next.js)": 3000}

failures: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  ✗ {msg}")


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  ! {msg}")


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def check_python() -> None:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    # doctor runs pre-install on whatever python3 exists, so the check is real. noqa: the
    # project targets 3.12, but this script must degrade gracefully on older interpreters.
    if sys.version_info >= (3, 10):  # noqa: UP036
        ok(f"python {version}")
    else:
        fail(f"python {version} — need 3.10+ just to run tooling (project targets 3.12)")


def check_docker() -> None:
    if not shutil.which("docker"):
        fail("docker not found on PATH — install Docker Desktop (WSL2 backend) or docker-ce")
        return
    code, out = run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code == 0:
        ok(f"docker daemon reachable (server {out})")
    else:
        fail("docker CLI present but daemon unreachable — is Docker Desktop running?")
    code, out = run(["docker", "compose", "version", "--short"])
    if code == 0:
        ok(f"docker compose v{out}")
    else:
        fail("docker compose (v2 plugin) not available")


def check_ports() -> None:
    for name, port in PORTS.items():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            in_use = sock.connect_ex(("127.0.0.1", port)) == 0
        if in_use:
            warn(f"port {port} ({name}) already in use — fine if that's the Backline stack")
        else:
            ok(f"port {port} ({name}) free")


def check_env_file() -> None:
    if (REPO_ROOT / ".env").exists():
        ok(".env present")
    elif (REPO_ROOT / ".env.example").exists():
        warn(".env missing — compose runs on defaults; run `cp .env.example .env` to customize")
    else:
        fail(".env.example missing from repo — checkout is incomplete")


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def check_line_endings() -> None:
    gitattributes = REPO_ROOT / ".gitattributes"
    if gitattributes.exists() and "eol=lf" in gitattributes.read_text(encoding="utf-8"):
        ok(".gitattributes forces LF")
    else:
        fail(".gitattributes missing or not forcing LF — CRLF will break scripts in containers")

    code, out = run(["git", "-C", str(REPO_ROOT), "config", "core.autocrlf"])
    if code == 0 and out == "true":
        fail("git core.autocrlf=true — run `git config core.autocrlf input` inside WSL")
    else:
        ok(f"git core.autocrlf={out or 'unset'} (safe)")

    code, out = run(["git", "-C", str(REPO_ROOT), "ls-files", "--eol", "Makefile"])
    if code == 0 and "w/crlf" in out:
        fail("Makefile checked out with CRLF — re-clone after fixing autocrlf")


def check_wsl_location() -> None:
    if not is_wsl():
        ok("not WSL — filesystem-location check not applicable")
        return
    ok("running under WSL2")
    if str(REPO_ROOT).startswith("/mnt/"):
        fail(
            f"repo at {REPO_ROOT} is on the Windows mount — move it to the WSL filesystem "
            "(e.g. ~/code/backline); /mnt/c IO is ~10x slower and breaks file watching"
        )
    else:
        ok(f"repo on native WSL filesystem ({REPO_ROOT})")


def main() -> int:
    print("backline doctor\n")
    print("python:")
    check_python()
    print("docker:")
    check_docker()
    print("ports:")
    check_ports()
    print("env:")
    check_env_file()
    print("line endings / WSL:")
    check_line_endings()
    check_wsl_location()

    print()
    if failures:
        print(f"FAILED — {len(failures)} problem(s), {len(warnings)} warning(s)")
        return 1
    print(f"OK — all checks passed ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
