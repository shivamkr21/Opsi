"""
Deploys the Aggi application: syncs code from the upstream git remote into
a local checkout, installs dependencies, runs migrations, and restarts the
Django dev server. Meant to be invoked by webhook_listener.py on every push
to the tracked branch, or run by hand (optionally with --dry-run).

Layout assumptions (see DEPLOY_SETUP.md):
  - AGGI_DIR is a git clone of the Aggi repo with a remote named GIT_REMOTE
    pointing at 4SightAI/Aggi, and a `myvenv` virtualenv already created.
  - This script's own directory (OPSI_DIR) holds deploy_manifest.txt,
    requirements.txt, and is where logs/ and runtime state files are kept.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

OPSI_DIR = Path(__file__).resolve().parent
AGGI_DIR = Path(os.environ.get("AGGI_DIR", r"C:\Aggi"))

MANIFEST_FILE = OPSI_DIR / "deploy_manifest.txt"
REQUIREMENTS_FILE = OPSI_DIR / "requirements.txt"
REQUIREMENTS_HASH_FILE = OPSI_DIR / "state" / "requirements.sha256"
LOG_DIR = OPSI_DIR / "logs"
DEPLOY_LOG_FILE = LOG_DIR / "deploy.log"
RUNSERVER_LOG_FILE = LOG_DIR / "runserver.log"
PID_FILE = OPSI_DIR / "state" / "runserver.pid"

GIT_REMOTE = os.environ.get("GIT_REMOTE", "upstream")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")
RUNSERVER_BIND = os.environ.get("RUNSERVER_BIND", "0.0.0.0:8000")

VENV_PYTHON = AGGI_DIR / "myvenv" / "Scripts" / "python.exe"
MANAGE_PY = AGGI_DIR / "S4_WebApp" / "manage.py"
WEBAPP_DIR = AGGI_DIR / "S4_WebApp"


class DeployError(RuntimeError):
    pass


def log(message):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, cwd=None, dry_run=False):
    printable = " ".join(str(c) for c in cmd)
    if dry_run:
        log(f"[DRY RUN] would run: {printable}" + (f" (cwd={cwd})" if cwd else ""))
        return ""
    log(f"running: {printable}" + (f" (cwd={cwd})" if cwd else ""))
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, shell=False
    )
    if result.stdout.strip():
        log(f"stdout: {result.stdout.strip()}")
    if result.stderr.strip():
        log(f"stderr: {result.stderr.strip()}")
    if result.returncode != 0:
        raise DeployError(f"command failed ({result.returncode}): {printable}")
    return result.stdout


def read_manifest():
    if not MANIFEST_FILE.exists():
        raise DeployError(f"manifest file not found: {MANIFEST_FILE}")
    paths = []
    for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        paths.append(line)
    if not paths:
        raise DeployError(f"manifest file is empty: {MANIFEST_FILE}")
    return paths


def sync_sparse_checkout(paths, dry_run):
    if not (AGGI_DIR / ".git").exists():
        raise DeployError(f"{AGGI_DIR} is not a git repository")
    run(["git", "-C", str(AGGI_DIR), "sparse-checkout", "init", "--cone"], dry_run=dry_run)
    run(["git", "-C", str(AGGI_DIR), "sparse-checkout", "set", *paths], dry_run=dry_run)


def pull_latest(dry_run):
    run(["git", "-C", str(AGGI_DIR), "fetch", GIT_REMOTE, GIT_BRANCH], dry_run=dry_run)
    run(
        ["git", "-C", str(AGGI_DIR), "reset", "--hard", f"{GIT_REMOTE}/{GIT_BRANCH}"],
        dry_run=dry_run,
    )


def install_requirements(dry_run):
    if not REQUIREMENTS_FILE.exists():
        log(f"no requirements.txt at {REQUIREMENTS_FILE}, skipping pip install")
        return
    current_hash = hashlib.sha256(REQUIREMENTS_FILE.read_bytes()).hexdigest()
    previous_hash = (
        REQUIREMENTS_HASH_FILE.read_text().strip() if REQUIREMENTS_HASH_FILE.exists() else None
    )
    if current_hash == previous_hash and not dry_run:
        log("requirements.txt unchanged, skipping pip install")
        return
    run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)],
        dry_run=dry_run,
    )
    if not dry_run:
        REQUIREMENTS_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
        REQUIREMENTS_HASH_FILE.write_text(current_hash, encoding="utf-8")


def run_migrations(dry_run):
    run(
        [str(VENV_PYTHON), str(MANAGE_PY), "migrate", "--noinput"],
        cwd=str(WEBAPP_DIR),
        dry_run=dry_run,
    )


def stop_runserver(dry_run):
    if not PID_FILE.exists():
        log("no runserver.pid found, nothing to stop")
        return
    pid = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid:
        PID_FILE.unlink(missing_ok=True)
        return
    run(["taskkill", "/PID", pid, "/T", "/F"], dry_run=dry_run)
    if not dry_run:
        PID_FILE.unlink(missing_ok=True)


def start_runserver(dry_run):
    cmd = [str(VENV_PYTHON), str(MANAGE_PY), "runserver", RUNSERVER_BIND]
    if dry_run:
        log(f"[DRY RUN] would start: {' '.join(cmd)} (cwd={WEBAPP_DIR})")
        return
    log(f"starting: {' '.join(cmd)} (cwd={WEBAPP_DIR})")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = open(RUNSERVER_LOG_FILE, "a", encoding="utf-8")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(WEBAPP_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    log(f"runserver started with pid {proc.pid}")


def deploy(dry_run=False):
    log(f"=== deploy started (dry_run={dry_run}) ===")
    try:
        paths = read_manifest()
        log(f"manifest paths: {paths}")
        sync_sparse_checkout(paths, dry_run)
        pull_latest(dry_run)
        install_requirements(dry_run)
        run_migrations(dry_run)
        stop_runserver(dry_run)
        start_runserver(dry_run)
    except DeployError as exc:
        log(f"DEPLOY FAILED: {exc}")
        raise
    log("=== deploy finished successfully ===")


def main():
    parser = argparse.ArgumentParser(description="Deploy Aggi to this machine.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log every step without mutating the repo, venv, or running processes.",
    )
    args = parser.parse_args()
    try:
        deploy(dry_run=args.dry_run)
    except DeployError:
        sys.exit(1)


if __name__ == "__main__":
    main()
