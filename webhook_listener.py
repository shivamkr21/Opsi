"""
Minimal GitHub webhook receiver (stdlib only, no extra dependencies).

Listens for `push` events on the tracked branch, verifies the payload
signature against a shared secret, and triggers deploy.py in the
background. Meant to run continuously (see DEPLOY_SETUP.md for how to
register it as a Task Scheduler job that starts at boot).

Required environment variables:
  WEBHOOK_SECRET   - shared secret configured on the GitHub webhook

Optional environment variables:
  WEBHOOK_PORT      - port to listen on (default 8443)
  WEBHOOK_PATH      - URL path GitHub posts to (default /deploy-hook)
  TARGET_REF        - git ref that triggers a deploy (default refs/heads/main)
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OPSI_DIR = Path(__file__).resolve().parent
DEPLOY_SCRIPT = OPSI_DIR / "deploy.py"
LOG_DIR = OPSI_DIR / "logs"
LOG_FILE = LOG_DIR / "webhook.log"
MANIFEST_FILE = OPSI_DIR / "deploy_manifest.txt"

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8443"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/deploy-hook")
TARGET_REF = os.environ.get("TARGET_REF", "refs/heads/main")

_deploy_lock = threading.Lock()


def log(message):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_manifest_paths():
    if not MANIFEST_FILE.exists():
        return []
    paths = []
    for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            paths.append(line)
    return paths


def touches_manifest(payload, manifest_paths):
    if not manifest_paths:
        return True
    commits = payload.get("commits", [])
    if not commits:
        # No file-level info available (e.g. some merge payloads) - deploy to be safe.
        return True
    changed_files = set()
    for commit in commits:
        changed_files.update(commit.get("added", []))
        changed_files.update(commit.get("removed", []))
        changed_files.update(commit.get("modified", []))
    return any(
        changed_file == prefix or changed_file.startswith(prefix.rstrip("/") + "/")
        for changed_file in changed_files
        for prefix in manifest_paths
    )


def run_deploy_in_background():
    def _run():
        acquired = _deploy_lock.acquire(blocking=False)
        if not acquired:
            log("deploy already in progress, skipping this trigger")
            return
        try:
            log("deploy started")
            result = subprocess.run(
                [sys.executable, str(DEPLOY_SCRIPT)],
                capture_output=True,
                text=True,
            )
            log(f"deploy.py exit code: {result.returncode}")
            if result.stdout.strip():
                log(f"deploy.py stdout tail: {result.stdout.strip()[-1000:]}")
            if result.stderr.strip():
                log(f"deploy.py stderr tail: {result.stderr.strip()[-1000:]}")
        finally:
            _deploy_lock.release()

    threading.Thread(target=_run, daemon=True).start()


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log("%s - %s" % (self.address_string(), format % args))

    def _respond(self, status, message):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode("utf-8"))

    def do_POST(self):
        if self.path != WEBHOOK_PATH:
            self._respond(404, "not found")
            return

        if not WEBHOOK_SECRET:
            log("WEBHOOK_SECRET is not set; refusing all requests")
            self._respond(500, "server misconfigured")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        signature = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            log("rejected request: invalid or missing signature")
            self._respond(401, "invalid signature")
            return

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            self._respond(200, "pong")
            return
        if event != "push":
            self._respond(200, f"ignored event: {event}")
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            self._respond(400, "invalid JSON")
            return

        if payload.get("ref") != TARGET_REF:
            self._respond(200, f"ignored ref: {payload.get('ref')}")
            return

        manifest_paths = read_manifest_paths()
        if not touches_manifest(payload, manifest_paths):
            log("push did not touch any manifest path, skipping deploy")
            self._respond(200, "ignored: no manifest paths changed")
            return

        log(f"valid push to {TARGET_REF}, triggering deploy")
        run_deploy_in_background()
        self._respond(200, "deploy triggered")


def main():
    if not WEBHOOK_SECRET:
        print("ERROR: WEBHOOK_SECRET environment variable must be set", file=sys.stderr)
        sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    log(f"listening on 0.0.0.0:{WEBHOOK_PORT}{WEBHOOK_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
