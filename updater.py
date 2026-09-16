#!/usr/bin/env python3
"""The render node's pull updater (D300). Installed by the NixOS module as the bootstrap copy and
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
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
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
# A refused version is tried again after this long, a bounded number of times: the host may have
# been fixed since (a missing library, a full disk), and nobody should need root to say so.
RETRY_BAD_AFTER = float(os.environ.get("MINDPRINT_RENDER_RETRY_BAD_AFTER", str(6 * 3600)))
MAX_BAD_TRIES = int(os.environ.get("MINDPRINT_RENDER_MAX_BAD_TRIES", "6"))
# What a release version or file name may look like: no separators, no dots-only, nothing a path
# could be built from. The server names them; the node still checks.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RELEASE_PATH = "/internal/render-nodes/release/"


class AdvertRefused(RuntimeError):
    """The advert itself is unacceptable (off-server URL, a version that is a path): the node's
    fault to report, not the release's — so it is not remembered as a bad version."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """The bearer token goes to exactly the server configured, never to wherever a 3xx points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl!r} refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirects())


LINES: list = []


def log(message: str) -> None:
    print(f"[updater] {message}", flush=True)
    LINES.append(f"{time.strftime('%H:%M:%S')} {message}"[:400])


def report(tok: str | None, why: str) -> None:
    """Post this run's lines to the server so a refusal or a rollback is read on the page, not in
    a journal the machine's owner has to forward. Best effort: a failure here is only printed."""
    if not tok or not LINES:
        return
    payload = json.dumps({"source": "updater", "lines": LINES[-200:] + [f"{time.strftime('%H:%M:%S')} ({why})"]}).encode("utf-8")
    req = urllib.request.Request(BASE + "/log", data=payload, method="POST", headers={
        "Authorization": "Bearer " + tok,
        "X-Mindprint-Render-Protocol": "1",
        "Content-Type": "application/json",
        "User-Agent": "mindprint-render-node/updater",
    })
    try:
        with _OPENER.open(req, timeout=20) as response:  # noqa: S310 - our own server
            response.read()
    except Exception as error:  # noqa: BLE001
        print(f"[updater] could not post the log to the server: {error}", flush=True)


# ── the server ─────────────────────────────────────────────────────────────────────────────

def token() -> str | None:
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def release_url(advert: dict) -> str:
    """The advertised download, if and only if it is on our own server under the release route.

    The advert is authenticated (it came back over the bearer-token call), but it is still input:
    an absolute URL must have the configured server's scheme and host, and any form must sit under
    the release route. Anything else is refused before the token is attached.
    """
    url = advert.get("url")
    if not isinstance(url, str) or not url:
        raise AdvertRefused("advert has no download url")
    ours = urllib.parse.urlsplit(SERVER)
    if url.startswith("/"):
        target = urllib.parse.urlsplit(url)
        if target.netloc or not target.path.startswith(RELEASE_PATH):
            raise AdvertRefused(f"advert url {url!r} is not under the release route")
        return SERVER + url
    target = urllib.parse.urlsplit(url)
    if (target.scheme, target.netloc) != (ours.scheme, ours.netloc) or not target.path.startswith(RELEASE_PATH):
        raise AdvertRefused(f"advert url {url!r} is not this node's server ({SERVER}) under the release route")
    return url


def request(path: str, tok: str, stream_to=None, timeout: float = 60.0):
    # A bare route ("/release") is relative to the node API; the advert's download URL arrives
    # as a server-absolute path ("/internal/render-nodes/release/…") and is checked by release_url.
    url = path if path.startswith("http") else (SERVER + path if path.startswith("/internal/") else BASE + path)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + tok,
        "X-Mindprint-Render-Protocol": "1",
        "User-Agent": "mindprint-render-node/updater",
        "Accept": "application/json",
    })
    with _OPENER.open(req, timeout=timeout) as response:  # noqa: S310 - our own server, redirects refused
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
    tries = int((bad.get(version) or {}).get("tries") or 0) + 1
    bad[version] = {"why": why[:500], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "epoch": time.time(), "tries": tries}
    STATE.mkdir(parents=True, exist_ok=True)
    BAD.write_text(json.dumps(bad, indent=1), encoding="utf-8")


def still_refused(version: str) -> str | None:
    """Why a refused version stays refused right now, or None when it is due another try."""
    entry = bad_versions().get(version)
    if not entry:
        return None
    tries = int(entry.get("tries") or 1)
    if tries >= MAX_BAD_TRIES:
        return f"refused {tries} times ({entry.get('why', '')[:80]}); waiting for a newer release"
    age = time.time() - float(entry.get("epoch") or 0)
    if age < RETRY_BAD_AFTER:
        return f"refused {age / 3600:.1f} h ago ({entry.get('why', '')[:80]}); trying again after {RETRY_BAD_AFTER / 3600:.0f} h"
    return None


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
    version = advert.get("version")
    file = advert["file"] if "file" in advert else str(advert.get("url", "")).rsplit("/", 1)[-1]
    if not isinstance(version, str) or not SAFE_NAME.match(version):
        raise AdvertRefused(f"advertised version {version!r} is not a plain name")
    if not isinstance(file, str) or not SAFE_NAME.match(file):
        raise AdvertRefused(f"advertised file {file!r} is not a plain name")
    expected = str(advert.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AdvertRefused("advert carries no usable sha256")
    size = int(advert.get("size") or 0)
    url = release_url(advert)
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
        _status, (digest, total) = request(url, tok, stream_to=handle, timeout=600.0)
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
    switched = {"yes": False}
    rc = _main(switched)
    if rc != 0:
        report(token(), f"exit {rc}")
    elif switched["yes"]:
        report(token(), "switched")
    return rc


def _main(switched: dict) -> int:
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
    if not version or not isinstance(version, str):
        log("advertised release has no version")
        return 1
    if version == current_version():
        prune()
        return check_crash_loop(version) or 0
    refused = still_refused(version)
    if refused:
        log(f"{version}: {refused}")
        return 0
    if version in bad_versions():
        log(f"{version} was refused earlier; trying it again (the host may have been fixed since)")

    try:
        release = fetch(advert, tok)
        prepare_and_test(release)
    except AdvertRefused as error:
        log(f"refusing the advert itself: {error}")
        return 1
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
    switched["yes"] = True

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
