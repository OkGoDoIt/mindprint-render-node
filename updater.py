#!/usr/bin/env python3
"""The render node's pull updater (D294). Installed by the NixOS module as the bootstrap copy and
run every few minutes by a systemd timer as the service user. Standard library only.

Every release also ships this file (nixos/updater.py): once one is live, the bootstrap copy hands
over to the shipped one when they differ, so the update logic itself moves with a server deploy
and the machine's owner never has to bump a flake input for it. A shipped updater that fails is
not trusted — the bootstrap copy then runs as if it were not there.

    ask the server which release it advertises
    → same as `current`?  done
    → download to releases/<v>.part, verify SHA-256, extract, check VERSION
    → the new release's own `prepare` (venvs) and `selftest` — refused releases are remembered
    → ask the agent to drain (state/drain-requested), wait until it is not mid-render
    → previous ← current; current ← new; ask the agent to restart (state/restart-requested);
      systemd starts it from `current`
    → wait for the new agent's first healthy heartbeat; otherwise flip back and remember the
      version as bad
    → keep current, previous and one more; delete older releases

No root: every path is under the service user's own state directory, the agent restarts itself
by exiting, and systemd's Restart=always does the rest. `systemctl stop mindprint-render-node`
still works exactly as the owner expects — the updater then flips without a health wait and the
next start runs the new release.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

HOME = pathlib.Path(os.environ.get("MINDPRINT_RENDER_HOME", "/var/lib/mindprint-render"))
STATE = pathlib.Path(os.environ.get("MINDPRINT_RENDER_STATE", HOME / "state"))
RELEASES = HOME / "releases"
CURRENT = HOME / "current"
PREVIOUS = HOME / "previous"
SERVER = os.environ.get("MINDPRINT_RENDER_SERVER", "https://beta.mindprint.ai").rstrip("/")
BASE = SERVER + "/internal/render-nodes"
TOKEN_FILE = pathlib.Path(os.environ.get("MINDPRINT_RENDER_TOKEN_FILE", STATE / "token"))
SERVICE = os.environ.get("MINDPRINT_RENDER_SERVICE", "mindprint-render-node.service")
DRAIN_TIMEOUT = float(os.environ.get("MINDPRINT_RENDER_DRAIN_TIMEOUT", "1500"))
HEALTH_TIMEOUT = float(os.environ.get("MINDPRINT_RENDER_HEALTH_TIMEOUT", "300"))
KEEP_RELEASES = 3
BAD = STATE / "bad-versions.json"
DELEGATE_TIMEOUT = float(os.environ.get("MINDPRINT_RENDER_DELEGATE_TIMEOUT", "7000"))


def log(message: str) -> None:
    print(f"[updater] {message}", flush=True)


# ── the server ─────────────────────────────────────────────────────────────────────────────

def token() -> str | None:
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def request(path: str, tok: str, stream_to=None, timeout: float = 60.0):
    # A bare route ("/release") is relative to the node API; the advert's download URL arrives
    # as a server-absolute path ("/internal/render-nodes/release/…").
    url = path if path.startswith("http") else (SERVER + path if path.startswith("/internal/") else BASE + path)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + tok,
        "X-Mindprint-Render-Protocol": "1",
        "User-Agent": "mindprint-render-node/updater",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - our own server
        if stream_to is None:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            stream_to.write(chunk)
            digest.update(chunk)
            total += len(chunk)
        return response.status, (digest.hexdigest(), total)


# ── local state ────────────────────────────────────────────────────────────────────────────

def current_version() -> str | None:
    try:
        return (CURRENT / "VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def bad_versions() -> dict:
    try:
        return json.loads(BAD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark_bad(version: str, why: str) -> None:
    bad = bad_versions()
    bad[version] = {"why": why[:500], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    STATE.mkdir(parents=True, exist_ok=True)
    BAD.write_text(json.dumps(bad, indent=1), encoding="utf-8")


def agent_status() -> dict | None:
    try:
        data = json.loads((STATE / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def service_active() -> bool:
    try:
        completed = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=False, timeout=15)
        return completed.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def status_fresh(status: dict | None, within: float) -> bool:
    return status is not None and isinstance(status.get("wall"), (int, float)) and time.time() - status["wall"] < within


def flip(target: pathlib.Path) -> None:
    """Atomically repoint `current` (and remember the old target as `previous`)."""
    old = CURRENT.resolve() if CURRENT.is_symlink() else None
    tmp = HOME / "current.new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target, target_is_directory=True)
    os.replace(tmp, CURRENT)
    if old is not None and old != target:
        ptmp = HOME / "previous.new"
        if ptmp.is_symlink() or ptmp.exists():
            ptmp.unlink()
        ptmp.symlink_to(old, target_is_directory=True)
        os.replace(ptmp, PREVIOUS)


def ask_restart() -> None:
    (STATE / "restart-requested").touch()


# ── the steps ──────────────────────────────────────────────────────────────────────────────

def fetch(advert: dict, tok: str) -> pathlib.Path:
    version, file, expected, size = advert["version"], advert["file"] if "file" in advert else advert["url"].rsplit("/", 1)[-1], advert["sha256"].lower(), int(advert.get("size") or 0)
    target = RELEASES / version
    if (target / ".ready").is_file():
        return target
    RELEASES.mkdir(parents=True, exist_ok=True)
    part = RELEASES / f"{version}.part"
    if part.exists():
        shutil.rmtree(part)
    part.mkdir()
    tarball = part / file
    log(f"downloading {version} ({size:,} bytes)")
    with tarball.open("wb") as handle:
        _status, (digest, total) = request(advert["url"], tok, stream_to=handle, timeout=600.0)
    if digest != expected:
        shutil.rmtree(part, ignore_errors=True)
        raise RuntimeError(f"checksum mismatch for {version}: got {digest[:12]}, expected {expected[:12]}")
    if size and total != size:
        shutil.rmtree(part, ignore_errors=True)
        raise RuntimeError(f"size mismatch for {version}: got {total}, expected {size}")
    log(f"verified {version} sha256 {digest[:12]}")

    extract = part / "extract"
    extract.mkdir()
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            if name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts or member.issym() or member.islnk():
                raise RuntimeError(f"refusing archive member {name!r}")
        archive.extractall(extract)  # noqa: S202 - members were just checked
    inner = extract / "mindprint-render-node"
    if not inner.is_dir():
        raise RuntimeError("archive does not contain mindprint-render-node/")
    found = (inner / "VERSION").read_text(encoding="utf-8").strip() if (inner / "VERSION").is_file() else ""
    if found != version:
        raise RuntimeError(f"archive VERSION {found!r} does not match advertised {version!r}")
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(inner), str(target))
    shutil.rmtree(part, ignore_errors=True)
    for script in (target / "bin").glob("*"):
        script.chmod(0o755)
    (target / ".ready").touch()
    return target


def run_release_tool(release: pathlib.Path, *args: str, timeout: float) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MINDPRINT_RENDER_RELEASE"] = str(release)
    env["PYTHONPATH"] = str(release) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "render_node", *args], env=env, cwd=str(release),
                          capture_output=True, text=True, timeout=timeout, check=False)


def prepare_and_test(release: pathlib.Path) -> None:
    log("preparing environments")
    prepared = run_release_tool(release, "prepare", "--no-weights", timeout=3600)
    if prepared.returncode != 0:
        raise RuntimeError("prepare failed: " + (prepared.stderr or prepared.stdout).strip()[-800:])
    log("running the self-test")
    tested = run_release_tool(release, "selftest", timeout=900)
    for line in (tested.stdout or "").strip().splitlines():
        log("  " + line)
    if tested.returncode != 0:
        raise RuntimeError("self-test failed: " + (tested.stdout or tested.stderr).strip()[-800:])


def drain() -> bool:
    """Ask the agent to stop claiming; return once it is not mid-render (or is not running)."""
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "drain-requested").touch()
    deadline = time.monotonic() + DRAIN_TIMEOUT
    log("draining the running agent")
    while time.monotonic() < deadline:
        status = agent_status()
        if not service_active() or not status_fresh(status, 120):
            log("agent is not running; no drain needed")
            return True
        if not status.get("busy"):
            return True
        time.sleep(5)
    log("drain timed out; the agent is still mid-render — leaving the update for the next run")
    (STATE / "drain-requested").unlink(missing_ok=True)
    return False


def wait_healthy(version: str) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        status = agent_status()
        if status_fresh(status, 90) and status.get("version") == version and status.get("heartbeatOkAt") \
                and status.get("status") not in ("error",):
            return True
        time.sleep(5)
    return False


def prune() -> None:
    keep = set()
    for link in (CURRENT, PREVIOUS):
        if link.is_symlink():
            keep.add(link.resolve())
    if not RELEASES.is_dir():
        return
    releases = sorted((p for p in RELEASES.iterdir() if p.is_dir() and (p / ".ready").is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for index, release in enumerate(releases):
        if release.resolve() in keep or index < KEEP_RELEASES:
            continue
        log(f"pruning old release {release.name}")
        shutil.rmtree(release, ignore_errors=True)
    for part in RELEASES.glob("*.part"):
        shutil.rmtree(part, ignore_errors=True)


def rollback(why: str, bad_version: str) -> int:
    mark_bad(bad_version, why)
    if PREVIOUS.is_symlink() and PREVIOUS.resolve().is_dir():
        log(f"rolling back to {PREVIOUS.resolve().name}: {why}")
        flip(PREVIOUS.resolve())
        ask_restart()
        return 1
    log(f"no previous release to roll back to: {why}")
    return 1


def check_crash_loop(advertised: str | None) -> int | None:
    """A release that flipped fine but keeps dying: current == advertised, service active, no fresh status."""
    current = current_version()
    if current is None or not service_active():
        return None
    status = agent_status()
    if status_fresh(status, 600):
        return None
    if PREVIOUS.is_symlink() and not (STATE / "restart-requested").exists():
        stale_for = "never" if status is None else f"{time.time() - status.get('wall', 0):.0f}s"
        return rollback(f"agent has written no status for {stale_for} while active", current)
    return None


def delegate() -> int | None:
    """Run the updater the current release ships, when it differs from this copy. None = not delegated."""
    if os.environ.get("MINDPRINT_RENDER_UPDATER_BOOTSTRAP") == "1":
        return None
    shipped = CURRENT / "nixos" / "updater.py"
    try:
        if not shipped.is_file() or shipped.read_bytes() == pathlib.Path(__file__).read_bytes():
            return None
    except OSError:
        return None
    log(f"handing over to the updater shipped with {current_version() or 'the current release'}")
    try:
        completed = subprocess.run(
            [sys.executable, str(shipped)],
            env={**os.environ, "MINDPRINT_RENDER_UPDATER_BOOTSTRAP": "1"},
            timeout=DELEGATE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"the shipped updater could not run ({error}); continuing with the bootstrap copy")
        return None
    if completed.returncode == 0:
        return 0
    log(f"the shipped updater exited {completed.returncode}; continuing with the bootstrap copy")
    return None


def main() -> int:
    delegated = delegate()
    if delegated is not None:
        return delegated
    tok = token()
    if not tok:
        log(f"no credential at {TOKEN_FILE}; nothing to do")
        return 0
    try:
        status, advert = request("/release", tok)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            log("credential rejected (401); nothing to do until it is fixed")
            return 0
        log(f"release query failed: HTTP {error.code}")
        return 1
    except (urllib.error.URLError, OSError) as error:
        log(f"server unreachable: {error}")
        return 1

    if status == 204 or not advert:
        log("server advertises no release")
        return check_crash_loop(None) or 0

    version = advert.get("version")
    if not version:
        log("advertised release has no version")
        return 1
    if version == current_version():
        prune()
        return check_crash_loop(version) or 0
    if version in bad_versions():
        log(f"{version} was refused earlier ({bad_versions()[version]['why'][:80]}); waiting for a newer one")
        return 0

    try:
        release = fetch(advert, tok)
        prepare_and_test(release)
    except Exception as error:  # noqa: BLE001
        log(f"refusing {version}: {error}")
        mark_bad(version, str(error))
        return 1

    if not drain():
        return 1
    was_running = service_active() and status_fresh(agent_status(), 120)
    flip(release)
    (STATE / "drain-requested").unlink(missing_ok=True)
    ask_restart()
    log(f"switched current → {version}; agent asked to restart")

    if not was_running:
        log("agent was not running; the new release starts with the service")
        prune()
        return 0
    if wait_healthy(version):
        log(f"{version} is healthy")
        prune()
        return 0
    return rollback("the new release did not report a healthy heartbeat in time", version)


if __name__ == "__main__":
    sys.exit(main())
