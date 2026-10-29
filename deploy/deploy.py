#!/usr/bin/python3
"""Build and test origin/main before replacing the running bot. Python 3.12+."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone


def run(*args, capture=False, timeout=120, **kwargs):
    result = subprocess.run(
        [str(arg) for arg in args], check=True, timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True, **kwargs,
    )
    return result.stdout.strip() if capture else ""


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


class Deployer:
    def __init__(self):
        self.app = Path(os.environ.get("APP_DIR", "/opt/FreelanceNotification")).resolve()
        self.state = Path(os.environ.get("DEPLOY_STATE_DIR", "/var/lib/freelancenotify-deploy"))
        self.branch = os.environ.get("DEPLOY_BRANCH", "main")
        self.project = "freelancenotifycation"
        self.container = "fh-bots-notifier"
        self.repository = "freelancenotifycation-bot"
        self.override = self.state / "compose-image.json"
        self.failure = self.state / "failed.json"
        self.success = self.state / "deployed.json"
        self.stability = int(os.environ.get("DEPLOY_STABILITY_SECONDS", "20"))
        self.health_timeout = int(os.environ.get("DEPLOY_HEALTH_TIMEOUT", "90"))

    def git(self, *args):
        return run("git", "-C", self.app, *args, capture=True)

    def compose(self, *args, directory=None, override=None):
        return run(
            "docker", "compose", "--project-name", self.project,
            "--project-directory", self.app, "--env-file", self.app / ".env",
            "-f", (directory or self.app) / "docker-compose.yml",
            "-f", override or self.override, *args, timeout=180,
        )

    def inspect(self):
        return json.loads(run("docker", "inspect", self.container, capture=True))[0]

    def start(self):
        self.compose(
            "up", "--detach", "--no-build", "--pull", "never", "--no-deps",
            "--force-recreate", "--wait", "--wait-timeout", "60", "bot",
        )

    def wait_for_bot(self, expected_image):
        deadline = time.monotonic() + self.health_timeout
        ready_since = None
        while time.monotonic() < deadline:
            info = self.inspect()
            if (info["Image"] != expected_image or not info["State"]["Running"]
                    or info["RestartCount"] != 0):
                raise RuntimeError("new container exited, restarted, or has the wrong image")
            logs = run("docker", "logs", "--tail", "1000", self.container, capture=True)
            if "TelegramConflictError" in logs or "TelegramUnauthorizedError" in logs:
                raise RuntimeError("Telegram rejected polling; see the container logs")
            # aiogram emits this only after successfully obtaining the bot identity.
            if "Run polling for bot" in logs:
                if ready_since is None:
                    ready_since = time.monotonic()
                if time.monotonic() - ready_since >= self.stability:
                    return
            time.sleep(2)
        raise RuntimeError("Telegram polling did not become stable before the timeout")

    def prepare(self, target, staging, image):
        archive = staging / "source.tar"
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "-C", str(self.app), "archive", target],
                stdout=output, check=True, timeout=60,
            )
        with tarfile.open(archive) as source:
            source.extractall(staging, filter="data")
        archive.unlink()
        run(
            "docker", "build", "--label", f"org.opencontainers.image.revision={target}",
            "--label", "io.freelancenotify.autodeploy=true", "--tag", image, staging,
            timeout=900,
        )
        # Tests receive neither production secrets nor production data/network access.
        run(
            "docker", "run", "--rm", "--network", "none", "--memory", "256m",
            "--volume", f"{staging / 'tests'}:/tests:ro", "--entrypoint", "python",
            image, "-m", "unittest", "discover", "-s", "/tests", "-q", timeout=300,
        )
        validation = """
from app.config import Settings
from aiogram.utils.token import validate_token
try:
    settings = Settings(_env_file=None)
    validate_token(settings.telegram_bot_token.get_secret_value())
    if not settings.freelancehunt_token.get_secret_value().strip():
        raise ValueError('empty Freelancehunt token')
except Exception:
    raise SystemExit('Invalid bot configuration; check the server .env') from None
print('Runtime configuration OK')
"""
        run(
            "docker", "run", "--rm", "--network", "none", "--memory", "256m",
            "--env-file", self.app / ".env", "--entrypoint", "python",
            image, "-c", validation, timeout=60,
        )
        candidate = staging / "compose-image.json"
        write_json(candidate, {"services": {"bot": {"image": image}}})
        self.compose("config", "--quiet", directory=staging, override=candidate)
        return run("docker", "image", "inspect", image, "--format", "{{.Id}}", capture=True)

    def deploy(self, target):
        previous = self.git("rev-parse", "HEAD")
        previous_image = self.inspect()["Image"]
        image = f"{self.repository}:git-{target}"
        rollback_image = f"{self.repository}:rollback"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.state / "backups" / f"{timestamp}-{target[:12]}"
        with tempfile.TemporaryDirectory(prefix="build-", dir=self.state) as directory:
            image_id = self.prepare(target, Path(directory), image)
            run("docker", "image", "tag", previous_image, rollback_image)
            backup.mkdir(parents=True, mode=0o700)
            write_json(backup / "previous.json", {"revision": previous, "image": previous_image})
            try:
                run("docker", "stop", "--time", "30", self.container, timeout=60)
                # Snapshot while the old process is stopped, before the new one writes.
                with tarfile.open(backup / "data-and-env.tar.gz", "w:gz") as snapshot:
                    snapshot.add(self.app / ".env", arcname=".env")
                    snapshot.add(self.app / "data", arcname="data")
                self.git("checkout", "--detach", target)
                write_json(self.override, {"services": {"bot": {"image": image}}})
                self.start()
                self.wait_for_bot(image_id)
                write_json(self.success, {
                    "revision": target, "image": image_id, "previous_revision": previous,
                    "previous_image": previous_image, "backup": str(backup), "deployed_at": timestamp,
                })
            except BaseException:
                print(f"Deployment failed; restoring {previous[:12]}", flush=True)
                self.git("checkout", "--detach", previous)
                write_json(self.override, {"services": {"bot": {"image": rollback_image}}})
                self.start()
                self.wait_for_bot(previous_image)
                print("Previous version restored; production data retained", flush=True)
                raise
        self.failure.unlink(missing_ok=True)
        print(f"Deployed {target}; Telegram polling is stable", flush=True)
        self.cleanup(image_id, previous_image)

    def cleanup(self, current_image, previous_image):
        # Only remove this deployer's old image tags and snapshots, never other apps.
        try:
            tags = run(
                "docker", "image", "ls", self.repository,
                "--format", "{{.Repository}}:{{.Tag}}", capture=True,
            ).splitlines()
            for tag in tags:
                if not tag.startswith(f"{self.repository}:git-"):
                    continue
                image_id = run("docker", "image", "inspect", tag, "--format", "{{.Id}}", capture=True)
                if image_id not in (current_image, previous_image):
                    run("docker", "image", "rm", tag)
            backups = sorted((self.state / "backups").iterdir(), reverse=True)
            for old in backups[5:]:
                shutil.rmtree(old)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Deployment succeeded; cleanup needs attention: {error}", flush=True)

    def check(self, retry=False):
        if self.git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("tracked server files were edited; commit or preserve those changes first")
        self.git(
            "fetch", "--quiet", "--no-tags", "origin",
            f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}",
        )
        target = self.git("rev-parse", f"refs/remotes/origin/{self.branch}")
        previous = self.git("rev-parse", "HEAD")
        if self.failure.exists() and not retry:
            if json.loads(self.failure.read_text())["revision"] == target:
                return
        if self.success.exists() and target == previous:
            if json.loads(self.success.read_text())["revision"] == target:
                return
        # A rewritten main must not silently roll production back.
        self.git("merge-base", "--is-ancestor", previous, target)
        print(f"Updating {previous[:12]} -> {target[:12]} from {self.branch}", flush=True)
        try:
            self.deploy(target)
        except BaseException as error:
            write_json(self.failure, {"revision": target, "error": str(error)})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry", action="store_true", help="retry a previously failed revision")
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        raise RuntimeError("deployment interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    os.umask(0o077)
    deployer = Deployer()
    deployer.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (deployer.state / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        deployer.check(retry=args.retry)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Autodeploy failed: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
