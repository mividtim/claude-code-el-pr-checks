"""PR check suite watcher sidecar plugin for el-sidecar.

Polls GitHub API for check suite / check run status on watched pull requests.
Uses the sidecar's runtime watch API — agents POST /watch with full-qualified
PR URLs, and this plugin polls all watched PRs for check status changes.

Emits events when checks complete (success or failure), allowing agents to
react to CI results without manual polling.

Registers itself with el-sidecar via register(sidecar).

Env vars:
    PR_CHECKS_POLL_INTERVAL  — Polling interval in seconds (default: 30)

Requires: gh CLI (authenticated)
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.environ.get('PR_CHECKS_POLL_INTERVAL', '30'))

# Sidecar reference (set during register())
_sidecar = None

# Watched PRs: {url: {"owner": str, "repo": str, "number": int, "last_status": dict}}
# last_status tracks the most recent state per check run to detect transitions
_watches = {}
_watches_lock = threading.Lock()

# ---------------------------------------------------------------------------
# PR URL parsing
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(
    r'https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)'
)


def _parse_pr_url(url):
    """Parse a full-qualified GitHub PR URL into (owner, repo, number)."""
    m = _PR_URL_RE.match(url)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


# ---------------------------------------------------------------------------
# GitHub API helper
# ---------------------------------------------------------------------------


def _gh_api(endpoint):
    """Call GitHub API via gh CLI. Returns parsed JSON or None."""
    try:
        result = subprocess.run(
            ['gh', 'api', endpoint],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Check status polling for a single PR
# ---------------------------------------------------------------------------


def _poll_pr_checks(owner, repo, number, last_status):
    """Poll a single PR for check suite status changes.
    Returns list of new events and updated status dict."""
    events = []
    full_repo = f"{owner}/{repo}"
    new_status = dict(last_status)

    # Get the PR's head SHA
    pr_data = _gh_api(f"repos/{full_repo}/pulls/{number}")
    if not pr_data:
        return events, new_status

    head_sha = pr_data.get('head', {}).get('sha', '')
    if not head_sha:
        return events, new_status

    # Get check runs for the head commit
    check_data = _gh_api(f"repos/{full_repo}/commits/{head_sha}/check-runs")
    if not check_data:
        return events, new_status

    for run in check_data.get('check_runs', []):
        run_id = str(run.get('id', ''))
        name = run.get('name', '')
        status = run.get('status', '')  # queued, in_progress, completed
        conclusion = run.get('conclusion', '')  # success, failure, etc.
        completed_at = run.get('completed_at', '')

        current_state = f"{status}:{conclusion}"
        prev_state = last_status.get(run_id, '')

        new_status[run_id] = current_state

        # Emit event on state transition to completed
        if status == 'completed' and prev_state != current_state:
            events.append({
                'type': 'check_completed',
                'check_name': name,
                'conclusion': conclusion,
                'completed_at': completed_at,
                'head_sha': head_sha,
                'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                'repo': full_repo,
                'pr_number': number,
                'run_id': run.get('id'),
                'html_url': run.get('html_url', ''),
            })

    # Also check combined status (for status checks that aren't check runs)
    status_data = _gh_api(f"repos/{full_repo}/commits/{head_sha}/status")
    if status_data:
        for s in status_data.get('statuses', []):
            ctx = s.get('context', '')
            state = s.get('state', '')  # pending, success, failure, error
            status_key = f"status:{ctx}"
            prev = last_status.get(status_key, '')

            new_status[status_key] = state

            if state in ('success', 'failure', 'error') and prev != state:
                events.append({
                    'type': 'status_changed',
                    'context': ctx,
                    'state': state,
                    'description': s.get('description', ''),
                    'target_url': s.get('target_url', ''),
                    'head_sha': head_sha,
                    'pr_url': f"https://github.com/{full_repo}/pull/{number}",
                    'repo': full_repo,
                    'pr_number': number,
                })

    return events, new_status


# ---------------------------------------------------------------------------
# Poller (runs in background thread)
# ---------------------------------------------------------------------------


def poll_pr_checks():
    """Background poller: check all watched PRs for CI status changes."""
    sys.stderr.write(f"[el-pr-checks] Polling every {POLL_INTERVAL}s\n")

    while True:
        time.sleep(POLL_INTERVAL)

        with _watches_lock:
            watched = dict(_watches)

        if not watched:
            continue

        for url, info in watched.items():
            try:
                events, new_status = _poll_pr_checks(
                    info['owner'], info['repo'], info['number'],
                    info.get('last_status', {})
                )

                # Update status
                with _watches_lock:
                    if url in _watches:
                        _watches[url]['last_status'] = new_status

                # Insert events into sidecar
                for evt in events:
                    if evt['type'] == 'check_completed':
                        summary = f"[{evt['conclusion']}] {evt['check_name']} on {evt['repo']}#{evt['pr_number']}"
                    else:
                        summary = f"[{evt['state']}] {evt['context']} on {evt['repo']}#{evt['pr_number']}"

                    _sidecar['insert_event'](
                        source='pr-checks',
                        text=json.dumps(evt),
                        summary=summary,
                    )
                    _sidecar['notify_waiters']()

            except Exception as e:
                sys.stderr.write(f"[el-pr-checks] poll error for {url}: {e}\n")


# ---------------------------------------------------------------------------
# Watch handlers
# ---------------------------------------------------------------------------


def add_watch(url):
    """Add a PR to the watch list."""
    parsed = _parse_pr_url(url)
    if not parsed:
        return {"ok": False, "error": f"Invalid PR URL: {url}"}

    owner, repo, number = parsed
    with _watches_lock:
        if url in _watches:
            return {"ok": True, "already_watching": True}
        _watches[url] = {
            'owner': owner,
            'repo': repo,
            'number': number,
            'last_status': {},
        }

    sys.stderr.write(f"[el-pr-checks] Watching {owner}/{repo}#{number}\n")
    return {"ok": True, "watching": url}


def remove_watch(url):
    """Remove a PR from the watch list."""
    with _watches_lock:
        removed = _watches.pop(url, None)
    return {"ok": True, "removed": removed is not None}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(sidecar):
    """Register this PR checks plugin with el-sidecar."""
    global _sidecar
    _sidecar = sidecar

    # Register watch handler (enables POST /watch {"plugin": "pr-checks", ...})
    sidecar['register_watch_handler']('pr-checks', add_watch, remove_watch)

    # Register background poller
    sidecar['register_poller']('pr-checks', poll_pr_checks)

    sys.stderr.write(f"[el-pr-checks] Registered (poll={POLL_INTERVAL}s)\n")
