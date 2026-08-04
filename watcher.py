import argparse
import json
import logging
import logging.handlers
import os
import sys
import time

import requests
import yaml
from dotenv import load_dotenv

from naming import AUDIO_EXTENSIONS, sanitize_filename

load_dotenv()

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "whisper-watcher")
STATE_PATH = os.path.join(APP_DIR, "state.json")
LOG_PATH = os.path.join(APP_DIR, "watcher.log")
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch-config.yaml")

# Bind-mounted into the whisper-api container at WATCHER_CONTROL_DIR (see docker-compose.yml)
# so POST /watcher/trigger (api.py) and this process agree on the same physical folder.
DEFAULT_CONTROL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher-control")
TRIGGER_FILENAME = "trigger.request"

DEFAULT_API = {
    "base_url": "http://127.0.0.1:8000",
    "enabled": True,               # automatic scheduled scanning; manual triggers work either way
    "poll_interval_seconds": 3600,  # how often the configured roots are rescanned automatically
    "control_check_seconds": 5,     # how often to check for a manual-trigger signal file
    "stability_seconds": 60,
    "job_poll_seconds": 10,
    "job_timeout_seconds": 14400,
    "max_inflight": 1,
    "max_files_per_cycle": 5,
    "order": "newest_first",
    "max_attempts": 5,
    "backoff_base_seconds": 60,
    "backoff_max_seconds": 3600,
}

logger = logging.getLogger("watcher")

# path -> bool, tracks last-seen availability so we log only on state transitions
_root_available = {}


def setup_logging():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    os.makedirs(APP_DIR, exist_ok=True)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)


# --- Config ---

def load_config(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Config not found: {path}. Copy watch-config.example.yaml to watch-config.yaml and edit it."
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    api_cfg = {**DEFAULT_API, **(raw.get("api") or {})}
    watch_roots = raw.get("watch") or []
    if not watch_roots:
        raise ValueError(f"No 'watch' roots configured in {path}")
    for root_cfg in watch_roots:
        if not root_cfg.get("path"):
            raise ValueError(f"A 'watch' entry is missing 'path' in {path}")

    return api_cfg, watch_roots


# --- State (outside the watched tree — see plan section 6) ---

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("State file unreadable (%s), starting fresh: %s", STATE_PATH, e)
        return {}


def save_state(state):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_PATH)


def write_transcript(txt_path, formatted):
    tmp_path = txt_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(formatted)
    os.replace(tmp_path, txt_path)


def consume_trigger(control_dir):
    """Return True and delete the signal file if a manual trigger (POST /watcher/trigger) is pending."""
    trigger_path = os.path.join(control_dir, TRIGGER_FILENAME)
    if not os.path.exists(trigger_path):
        return False
    try:
        os.remove(trigger_path)
    except OSError as e:
        logger.warning("Could not consume trigger file %s: %s", trigger_path, e)
        return False
    return True


def check_root_available(path):
    available = os.path.isdir(path)
    was_available = _root_available.get(path)
    if available and was_available is False:
        logger.info("Root recovered: %s", path)
    elif not available and was_available is not False:
        logger.warning("Root unavailable (Google Drive not mounted, or still starting up?): %s", path)
    _root_available[path] = available
    return available


# --- Discovery ---

def discover_candidates(root_cfg):
    """Walk a root, return (audio_path, txt_path, size, mtime) for every audio file missing its transcript."""
    path = root_cfg["path"]
    recursive = root_cfg.get("recursive", True)
    candidates = []

    if recursive:
        walk_iter = os.walk(path, onerror=lambda e: logger.warning("Walk error under %s: %s", path, e))
    else:
        try:
            walk_iter = [(path, [], os.listdir(path))]
        except OSError as e:
            logger.warning("Could not list %s: %s", path, e)
            return []

    for dirpath, _dirnames, filenames in walk_iter:
        seen_targets = {}
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in AUDIO_EXTENSIONS:
                continue

            file_path = os.path.join(dirpath, filename)
            if not os.path.isfile(file_path):
                continue

            try:
                stat = os.stat(file_path)
            except OSError:
                continue
            if stat.st_size == 0:
                continue

            base_name = os.path.splitext(filename)[0]
            sanitized_txt = sanitize_filename(base_name + ".txt")
            txt_path = os.path.join(dirpath, sanitized_txt)

            if sanitized_txt in seen_targets and seen_targets[sanitized_txt] != filename:
                logger.warning(
                    "Filename collision in %s: '%s' and '%s' both map to '%s' — whichever transcribes first wins.",
                    dirpath, seen_targets[sanitized_txt], filename, sanitized_txt,
                )
            seen_targets[sanitized_txt] = filename

            if os.path.exists(txt_path):
                continue

            candidates.append((file_path, txt_path, stat.st_size, stat.st_mtime))

    return candidates


def process_root(root_cfg, api_cfg, state):
    """Update stability/backoff tracking for every candidate in this root; return file paths ready to submit."""
    path = root_cfg["path"]
    if not check_root_available(path):
        return []

    now = time.time()
    stability_seconds = api_cfg["stability_seconds"]
    max_attempts = api_cfg["max_attempts"]

    ready = []
    for file_path, txt_path, size, mtime in discover_candidates(root_cfg):
        entry = state.get(file_path, {})

        if entry.get("status") == "submitted" and entry.get("job_id"):
            continue  # in-flight — handled by poll_until_done, not re-queued
        if entry.get("status") == "failed" and entry.get("attempts", 0) >= max_attempts:
            continue
        next_retry_at = entry.get("next_retry_at")
        if next_retry_at and now < next_retry_at:
            continue

        if entry.get("last_size") == size and entry.get("last_mtime") == mtime:
            stable_since = entry.get("stable_since", now)
        else:
            stable_since = now  # size/mtime changed (or first sighting) — restart the stability clock

        entry.update({
            "status": entry.get("status", "pending"),
            "last_size": size,
            "last_mtime": mtime,
            "stable_since": stable_since,
            "first_seen_at": entry.get("first_seen_at", now),
            "txt_path": txt_path,
            "root_path": path,
        })
        state[file_path] = entry

        if now - stable_since >= stability_seconds:
            ready.append(file_path)

    order = api_cfg.get("order", "newest_first")
    ready.sort(key=lambda fp: state[fp]["last_mtime"], reverse=(order == "newest_first"))
    return ready[: api_cfg.get("max_files_per_cycle", 5)]


# --- Submission and job polling ---

def schedule_retry(file_path, state, api_cfg, reason):
    entry = state[file_path]
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error"] = reason
    entry["job_id"] = None

    if entry["attempts"] >= api_cfg["max_attempts"]:
        entry["status"] = "failed"
        entry["next_retry_at"] = None
        logger.warning("Giving up on %s after %d attempt(s): %s", file_path, entry["attempts"], reason)
    else:
        entry["status"] = "pending"
        backoff = min(
            api_cfg["backoff_base_seconds"] * (2 ** (entry["attempts"] - 1)),
            api_cfg["backoff_max_seconds"],
        )
        entry["next_retry_at"] = time.time() + backoff
        logger.info(
            "Will retry %s in %.0fs (attempt %d/%d): %s",
            file_path, backoff, entry["attempts"], api_cfg["max_attempts"], reason,
        )

    state[file_path] = entry
    save_state(state)


def submit_and_track(file_path, root_cfg, api_cfg, state):
    """POST the file to /transcribe (streamed, never loaded fully into memory) and persist the job_id."""
    base_url = api_cfg["base_url"]
    headers = {"X-API-Key": os.environ.get("API_KEY", "")}
    data = {}
    if root_cfg.get("language"):
        data["language"] = root_cfg["language"]
    data["align"] = str(bool(root_cfg.get("align", False))).lower()
    data["diarize"] = str(bool(root_cfg.get("diarize", True))).lower()
    data["enroll_unknown"] = str(bool(root_cfg.get("enroll_unknown", False))).lower()

    logger.info("Submitting: %s", file_path)

    try:
        fh = open(file_path, "rb")
    except OSError as e:
        # Reading a Google Drive cloud-only placeholder can raise transiently while it hydrates.
        logger.warning("Read failed for %s (Drive hydrating?): %s — will retry.", file_path, e)
        schedule_retry(file_path, state, api_cfg, f"read error: {e}")
        return False

    try:
        with fh:
            files = {"file": (os.path.basename(file_path), fh)}
            resp = requests.post(
                f"{base_url}/transcribe", headers=headers, data=data, files=files, timeout=(30, 3600)
            )
    except (OSError, requests.RequestException) as e:
        logger.warning("Submit failed for %s: %s — will retry.", file_path, e)
        schedule_retry(file_path, state, api_cfg, f"submit error: {e}")
        return False

    if resp.status_code != 200:
        logger.warning("Submit rejected for %s: HTTP %d %s", file_path, resp.status_code, resp.text[:300])
        schedule_retry(file_path, state, api_cfg, f"HTTP {resp.status_code}")
        return False

    job_id = resp.json()["job_id"]
    entry = state[file_path]
    entry["status"] = "submitted"
    entry["job_id"] = job_id
    entry["submitted_at"] = time.time()
    entry["next_retry_at"] = None
    state[file_path] = entry
    save_state(state)  # persist job_id before polling, so a restart can resume instead of duplicating
    logger.info("Submitted %s as job %s", file_path, job_id)
    return True


def poll_until_done(file_path, api_cfg, state):
    entry = state.get(file_path)
    if not entry or not entry.get("job_id"):
        return

    job_id = entry["job_id"]
    base_url = api_cfg["base_url"]
    headers = {"X-API-Key": os.environ.get("API_KEY", "")}
    started = entry.get("submitted_at", time.time())
    job_poll_seconds = api_cfg["job_poll_seconds"]
    job_timeout_seconds = api_cfg["job_timeout_seconds"]

    while True:
        if time.time() - started > job_timeout_seconds:
            logger.warning(
                "Job %s timed out after %.0fs for %s — resetting to pending.",
                job_id, time.time() - started, file_path,
            )
            schedule_retry(file_path, state, api_cfg, "job timeout")
            return

        try:
            resp = requests.get(f"{base_url}/jobs/{job_id}", headers=headers, timeout=30)
        except requests.RequestException as e:
            logger.warning(
                "Poll failed for job %s (%s): %s — retrying in %ds", job_id, file_path, e, job_poll_seconds
            )
            time.sleep(job_poll_seconds)
            continue

        if resp.status_code == 404:
            # Watcher was down longer than the server's JOB_TTL_SECONDS — result is gone, re-submit.
            logger.warning("Job %s not found for %s (expired) — will re-submit.", job_id, file_path)
            entry["status"] = "pending"
            entry["job_id"] = None
            state[file_path] = entry
            save_state(state)
            return

        payload = resp.json()
        status = payload.get("status")

        if status == "completed":
            result = payload["result"]
            write_transcript(entry["txt_path"], result["formatted"])
            for name in result.get("enrolled_speakers", []):
                logger.info("Enrolled new speaker: voices/%s.wav (job %s, %s)", name, job_id, file_path)
            logger.info("Completed: %s -> %s", file_path, entry["txt_path"])
            del state[file_path]  # the .txt file is now the sole source of truth for "done"
            save_state(state)
            return

        if status == "failed":
            error = payload.get("error", "unknown error")
            logger.warning("Job %s failed for %s: %s", job_id, file_path, error)
            schedule_retry(file_path, state, api_cfg, error)
            return

        time.sleep(job_poll_seconds)  # queued / processing — keep waiting


# --- Main loop ---

def run_tick(watch_roots, api_cfg, state, source="scheduled"):
    """Run one full discovery+submit pass. `source` ("scheduled"/"manual"/"once") is only
    for the log lines below — it's what makes a directory scan visible in watcher.log even
    when nothing was found to submit, and distinguishes an automatic tick from a trigger.
    """
    logger.info("Scan starting (source=%s, roots=%d).", source, len(watch_roots))

    # Resume anything left in-flight from a previous run before looking for new work.
    for file_path, entry in list(state.items()):
        if entry.get("status") == "submitted" and entry.get("job_id"):
            poll_until_done(file_path, api_cfg, state)

    all_ready = []
    for root_cfg in watch_roots:
        for file_path in process_root(root_cfg, api_cfg, state):
            all_ready.append((root_cfg, file_path))
    save_state(state)

    submitted_count = 0
    max_inflight = api_cfg["max_inflight"]
    for root_cfg, file_path in all_ready:
        # Recomputed fresh each iteration: each submission is polled to completion
        # synchronously before the next one starts, so capacity frees up immediately
        # (max_inflight caps how many submit+poll cycles happen per tick, not true
        # concurrency — the server itself only ever runs one job at a time anyway).
        inflight = sum(1 for e in state.values() if e.get("status") == "submitted")
        if inflight >= max_inflight:
            break
        if submit_and_track(file_path, root_cfg, api_cfg, state):
            submitted_count += 1
            poll_until_done(file_path, api_cfg, state)

    logger.info(
        "Scan finished (source=%s): %d candidate(s) found, %d submitted.",
        source, len(all_ready), submitted_count,
    )


def run_dry_run(watch_roots, api_cfg):
    """Preview what would be queued right now. Never submits, never writes state."""
    state = load_state()
    max_attempts = api_cfg["max_attempts"]
    now = time.time()
    found_any = False

    for root_cfg in watch_roots:
        path = root_cfg["path"]
        if not os.path.isdir(path):
            logger.warning("[dry-run] Root unavailable, skipping: %s", path)
            continue

        for file_path, txt_path, _size, _mtime in discover_candidates(root_cfg):
            entry = state.get(file_path, {})
            if entry.get("status") == "failed" and entry.get("attempts", 0) >= max_attempts:
                continue
            next_retry_at = entry.get("next_retry_at")
            if next_retry_at and now < next_retry_at:
                continue
            logger.info("[dry-run] Would queue: %s -> %s", file_path, txt_path)
            found_any = True

    if not found_any:
        logger.info("[dry-run] Nothing to queue.")


def main():
    parser = argparse.ArgumentParser(
        description="Host-side watcher: keeps configured folders in sync with the whisper-api container."
    )
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_PATH, help="Path to watch-config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be queued; submit nothing")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass now, then exit")
    parser.add_argument(
        "--retry-failed", action="store_true", help="Clear backoff/attempt counters and retry everything"
    )
    parser.add_argument(
        "--control-dir", type=str, default=DEFAULT_CONTROL_DIR,
        help="Directory checked for a manual-trigger signal file from POST /watcher/trigger "
             "(must match the API container's WATCHER_CONTROL_DIR bind mount)",
    )
    args = parser.parse_args()

    setup_logging()

    try:
        api_cfg, watch_roots = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Config error: %s", e)
        sys.exit(1)

    if args.dry_run:
        run_dry_run(watch_roots, api_cfg)
        return

    if not os.environ.get("API_KEY"):
        logger.warning("API_KEY not set — requests to whisper-api will likely be rejected (403).")

    os.makedirs(args.control_dir, exist_ok=True)
    consume_trigger(args.control_dir)  # discard any stale signal left over from before this run started

    state = load_state()

    if args.retry_failed:
        cleared = 0
        for entry in state.values():
            if entry.get("status") == "failed":
                entry["status"] = "pending"
                entry["attempts"] = 0
                entry["next_retry_at"] = None
                entry["last_error"] = None
                cleared += 1
        if cleared:
            logger.info("Cleared backoff/attempts for %d file(s).", cleared)
            save_state(state)

    logger.info(
        "Watcher starting. Roots: %d. Config: %s. Enabled: %s. Scan interval: %ds. Control dir: %s",
        len(watch_roots), args.config, api_cfg["enabled"], api_cfg["poll_interval_seconds"], args.control_dir,
    )
    if not api_cfg["enabled"]:
        logger.info("Automatic scanning is disabled (api.enabled: false) — idling, waiting for manual triggers only.")

    next_scan_at = 0.0  # due immediately on startup if enabled

    try:
        while True:
            now = time.time()
            triggered = consume_trigger(args.control_dir)
            due = api_cfg["enabled"] and now >= next_scan_at

            if args.once or due or triggered:
                # A pending trigger is always worth flagging as "manual" in the log, even on
                # the rare tick where the hourly schedule also happened to be due at the same
                # moment — the trigger is the more interesting fact for whoever reads the log.
                source = "manual" if triggered else ("scheduled" if due else "once")
                run_tick(watch_roots, api_cfg, state, source=source)
                next_scan_at = time.time() + api_cfg["poll_interval_seconds"]

            if args.once:
                break
            time.sleep(api_cfg["control_check_seconds"])
    except KeyboardInterrupt:
        pass

    logger.info("Watcher stopped.")


if __name__ == "__main__":
    main()
