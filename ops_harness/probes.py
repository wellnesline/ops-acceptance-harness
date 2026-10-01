"""Fixed probes. Never execute commands supplied by a manifest or a user."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def result(status: str, summary: str, **details) -> dict:
    return {"status": status, "summary": summary, "details": details}


def within(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("Registry paths must stay inside the workspace")
    return path


def registry_paths(root: Path, registry: dict) -> dict:
    missing = []
    count = 0
    for service in registry["services"]:
        for field in ("source_paths", "entrypoints"):
            for relative in service[field]:
                count += 1
                if not within(root, relative).exists():
                    missing.append({"service": service["id"], "path": relative})
        relative = service["runbook"].split("#", 1)[0]
        count += 1
        if not within(root, relative).is_file():
            missing.append({"service": service["id"], "path": relative})
    return result("fail" if missing else "pass", "Registry references checked; existence is not service health",
                  references=count, missing=missing)


def runtime_tools(root: Path, registry: dict) -> dict:
    tools = {name: bool(shutil.which(name)) for name in ("python", "ffmpeg", "node", "codex", "claude")}
    ready = tools["python"] and tools["ffmpeg"] and tools["node"] and (tools["codex"] or tools["claude"])
    return result("pass" if ready else "warn", "Executable availability only; login and quotas not tested", available=tools)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_probe(url: str, kind: str) -> dict:
    # No proxy, redirects, response dumps, credentials or external destinations.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=5) as response:
            body = response.read(65537)
            status_code = response.status
            content_type = response.headers.get_content_type()
        if status_code != 200 or len(body) > 65536:
            return result("fail", "Unexpected or oversized HTTP response", http_status=status_code)
        if kind == "frontend":
            valid = content_type == "text/html" and b"<html" in body.lower()
            return result("pass" if valid else "fail", "Frontend HTML response only; browser workflow not tested", http_status=status_code)
        data = json.loads(body)
        if not isinstance(data, dict) or not data:
            return result("fail", "Health endpoint did not return a nonempty JSON object", http_status=status_code)
        # Unknown schemas cannot become a green health result merely through HTTP 200.
        status = data.get("status")
        if status in ("ok", "healthy", "ready"):
            return result("pass", "Application reports healthy; generation and credentials not tested", http_status=status_code)
        if status in ("error", "failed", "unhealthy", "degraded"):
            return result("fail", "Application reports degraded health", http_status=status_code)
        if data.get("ok") is True:
            return result("pass", "Application reports ok; generation and credentials not tested", http_status=status_code)
        return result("unknown", "JSON reachable, but health schema needs a dedicated verifier", http_status=status_code)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return result("fail", "Local endpoint check failed", error_type=type(exc).__name__)


def _scheduled_task_specs(registry: dict) -> list[dict]:
    """Scheduled tasks to verify are declared in config/ops_harness.json under "scheduled_tasks":
    [{"name": "example-task", "path": "\\", "action_contains": "run_example.ps1"}] (all values are examples)."""
    specs = []
    for item in registry.get("scheduled_tasks", []) if isinstance(registry, dict) else []:
        name, path, needle = item.get("name", ""), item.get("path", "\\"), item.get("action_contains", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) or not re.fullmatch(r"\\([A-Za-z0-9._-]+\\)*", path) \
                or not isinstance(needle, str) or len(needle) > 128:
            raise ValueError("Invalid scheduled task declaration")
        specs.append({"name": name, "path": path, "needle": needle})
    return specs


def windows_tasks(root: Path, registry: dict) -> dict:
    if os.name != "nt":
        return result("unknown", "Windows Task Scheduler is not applicable on this host")
    specs = _scheduled_task_specs(registry)
    if not specs:
        return result("unknown", "No scheduled tasks declared in the registry")
    # Project only the name/state/last result and a Boolean action match; do not emit arguments.
    # The declared specs are passed as validated JSON data, never interpolated as code.
    script = r'''
$ErrorActionPreference = 'Stop'
$expected = @($env:OPS_TASK_SPECS | ConvertFrom-Json)
$rows = @()
foreach ($spec in $expected) {
 $name = [string]$spec.name
 $task = Get-ScheduledTask -TaskName $name -TaskPath ([string]$spec.path) -ErrorAction SilentlyContinue
 if ($null -eq $task) { $rows += @{name=$name;state='Missing';action_match=$null;last_result=$null}; continue }
 $info = Get-ScheduledTaskInfo -InputObject $task
 $actions = (($task.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join ' ')
 $matched = if ($spec.needle) { $actions.Contains([string]$spec.needle) } else { $null }
 $rows += @{name=$name;state=[string]$task.State;action_match=$matched;last_result=[long]$info.LastTaskResult}
}
ConvertTo-Json -InputObject @($rows) -Compress
'''
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, timeout=25, encoding="utf-8", errors="replace",
                              env={**os.environ, "OPS_TASK_SPECS": json.dumps(specs)})
        if proc.returncode:
            return result("unknown", "Task Scheduler query failed", exit_code=proc.returncode)
        rows = json.loads(proc.stdout.lstrip("\ufeff"))
        if not isinstance(rows, list) or not rows:
            return result("unknown", "No Task Scheduler evidence")
        if any(row["state"] == "Missing" or row["action_match"] is False for row in rows):
            severity = "fail"
        elif any(row["state"] not in ("Ready", "Running", "Disabled") for row in rows):
            severity = "unknown"
        elif any(row["state"] == "Disabled" or row["last_result"] not in (0, 267009) for row in rows):
            severity = "warn"
        else:
            severity = "pass"
        return result(severity, "Task registration/state only; business results and schedules not verified", tasks=rows)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        return result("unknown", "Task Scheduler evidence unavailable", error_type=type(exc).__name__)


# Example marker path and thresholds; adapt them to the job you monitor.
EXPORT_MARKER = "scripts/example_job/logs/last_success.txt"


def export_evidence(root: Path, registry: dict) -> dict:
    marker = root / EXPORT_MARKER
    if not marker.is_file():
        return result("unknown", "Export success marker missing")
    age = time.time() - marker.stat().st_mtime
    # Operational warning threshold, not an SLA (example value).
    status = "unknown" if age < -60 else ("warn" if age > 1800 else "pass")
    return result(status, "Export wrapper success marker freshness; destination contents not verified",
                  age_seconds=round(age), warning_after_seconds=1800)


def url_probe(env_name: str, kind: str):
    """Endpoint URLs come only from environment variables (no built-in defaults).
    When the variable is unset the probe reports "unknown" (skipped) instead of guessing an address."""
    def run(root: Path, registry: dict) -> dict:
        url = os.environ.get(env_name, "").strip()
        if not url:
            return result("unknown", f"{env_name} is not set; probe skipped")
        return http_probe(url, kind)
    return run


PROBES = {
    "registry.paths": registry_paths,
    "runtime.tools": runtime_tools,
    "windows.tasks": windows_tasks,
    "local.app_api": url_probe("OPS_APP_HEALTH_URL", "app"),
    "local.frontend": url_probe("OPS_FRONTEND_URL", "frontend"),
    "evidence.export_marker": export_evidence,
}