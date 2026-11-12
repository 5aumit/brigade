"""Small, durable worker control plane for Brigade.

This module owns generic worker records and routes backend-specific operations
through adapters.  It deliberately does not schedule, retry, or clean up work.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = {"active", "blocked", "stopping", "unverifiable"}
TERMINAL_STATUSES = {"completed", "failed", "stopped", "disappeared"}
REASONING_LEVELS = {"low", "medium", "high", "xhigh", "max", "ultra"}
HARNESSES = {"codex", "cursor"}

# Profiles are data, not backend behavior.  Add a model here without changing
# worker lifecycle code.  Models use the identifiers accepted by the Codex CLI.
PROFILES = {
    "codex-luna-medium": {
        "harness": "codex", "model": "gpt-5.6-luna", "reasoning": "medium",
        "description": "Localized work with straightforward verification.",
    },
    "codex-terra-high": {
        "harness": "codex", "model": "gpt-5.6-terra", "reasoning": "high",
        "description": "Higher implementation judgment or verification uncertainty.",
    },
}


class WorkerError(RuntimeError):
    pass


class BackendCallError(WorkerError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload


class LaunchRejected(WorkerError):
    def __init__(self, message: str, *, resources_may_exist: bool):
        super().__init__(message)
        self.resources_may_exist = resources_may_exist


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def state_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    brigade = root / "brigade" / "workers.json"
    legacy = root / "5stack" / "workers.json"
    if brigade.exists() and legacy.exists():
        raise WorkerError(f"Worker state exists in both {brigade} and {legacy}. Resolve the duplicate files before using Brigade.")
    return brigade if brigade.exists() or not legacy.exists() else legacy


def load_state(path: Path | None = None) -> dict[str, Any]:
    path = path or state_path()
    if not path.exists():
        return {"version": 1, "workers": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise WorkerError(f"Worker state is not valid JSON: {path}") from error
    if value.get("version") != 1 or not isinstance(value.get("workers"), list):
        raise WorkerError(f"Worker state has an unsupported shape: {path}")
    return value


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def find_worker(state: dict[str, Any], worker_id: str) -> dict[str, Any]:
    for worker in state["workers"]:
        if worker["id"] == worker_id:
            return worker
    raise WorkerError(f"Unknown worker: {worker_id}")


def event(worker: dict[str, Any], kind: str, detail: str, *, status: str | None = None) -> None:
    worker.setdefault("events", []).append({"at": now(), "kind": kind, "detail": detail})
    worker["updated_at"] = now()
    if status:
        worker["status"] = status


def run(command: list[str], *, cwd: str | None = None) -> Any:
    try:
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def require_ok(result: Any, description: str) -> str:
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorkerError(f"{description} failed: {detail or 'no output'}")
    return result.stdout


def json_output(command: list[str], description: str, *, cwd: str | None = None) -> dict[str, Any]:
    result = run(command, cwd=cwd)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise BackendCallError(f"{description} failed: {detail or 'no output'}") from error
        raise WorkerError(f"{description} did not return JSON: {result.stdout.strip()}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise BackendCallError(f"{description} failed: {detail or 'no output'}", value)
    if value.get("ok") is False:
        raise BackendCallError(f"{description} reported failure: {json.dumps(value)}", value)
    return value


def git_repository(cwd: str | None = None) -> dict[str, str]:
    root = require_ok(run(["git", "rev-parse", "--show-toplevel"], cwd=cwd), "Find repository").strip()
    common_dir = require_ok(run(["git", "rev-parse", "--git-common-dir"], cwd=root), "Find git common directory").strip()
    return {"path": str(Path(root).resolve()), "common_dir": str((Path(root) / common_dir).resolve())}


def git_worktrees(repository: str) -> set[str]:
    output = require_ok(run(["git", "worktree", "list", "--porcelain"], cwd=repository), "List Git worktrees")
    return {str(Path(line[9:]).resolve()) for line in output.splitlines() if line.startswith("worktree ")}


def canonical_worktree_path(path: str) -> str:
    # Orca may return a Windows path through its WSL bridge.  Keep that stable
    # rather than incorrectly treating it as a relative Linux path.
    if len(path) >= 3 and path[1:3] == ":\\":
        return path.lower()
    return str(Path(path).resolve())


def worker_released(worker: dict[str, Any]) -> bool:
    return any(item.get("kind") == "released" for item in worker.get("events", []))


def deliver_follow_up(state: dict[str, Any], worker: dict[str, Any], message: str, persist=None) -> str:
    if worker_released(worker):
        raise WorkerError("Cannot send to a released worker")
    status = worker.get("status")
    if status == "completed" and worker.get("configuration", {}).get("backend") == "orca":
        raise WorkerError("Cannot send to a completed Orca worker. Start a new worker.")
    if status not in ACTIVE_STATUSES and status != "completed":
        raise WorkerError(f"Cannot send to a {status} worker")
    if status == "completed":
        path = (worker.get("worktree") or {}).get("path")
        if not isinstance(path, str) or not path:
            raise WorkerError("Completed worker has no worktree to reclaim")
        assert_available_worktree(state, path, exclude=worker["id"])
        event(worker, "reclaimed", "Reclaimed the worktree for a follow-up.", status="active")
        if persist:
            persist()
    backend = backend_for(worker["configuration"]["backend"])
    try:
        detail = backend.resume(worker, message, persist) if status == "completed" else backend.send(worker, message)
    except Exception as error:
        if status == "completed":
            event(worker, "follow_up_failed", str(error), status="completed")
            if persist:
                persist()
        raise
    event(worker, "follow_up", detail)
    return detail


def occupied_worktrees(state: dict[str, Any]) -> dict[str, str]:
    return {
        canonical_worktree_path(worker["worktree"]["path"]): worker["id"]
        for worker in state["workers"]
        if worker.get("status") in ACTIVE_STATUSES and worker.get("worktree", {}).get("path")
    }


def assert_available_worktree(state: dict[str, Any], path: str, *, exclude: str | None = None) -> None:
    occupier = occupied_worktrees(state).get(canonical_worktree_path(path))
    if occupier and occupier != exclude:
        raise WorkerError(f"Worktree is occupied by active worker {occupier}: {path}")


def new_worktree_path(repository: dict[str, str], worker_id: str) -> Path:
    token = hashlib.sha256(repository["common_dir"].encode()).hexdigest()[:12]
    return state_path().parent / "worktrees" / token / worker_id


def create_worktree(repository: dict[str, str], worker_id: str) -> str:
    path = new_worktree_path(repository, worker_id)
    if path.exists():
        raise WorkerError(f"Refusing to reuse existing worker worktree: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    require_ok(run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=repository["path"]), "Create worker worktree")
    return str(path.resolve())


PROFILE_FACTORS = ("low", "moderate", "high", "straightforward", "hard")


def cursor_new_worktree_warning(harness: str, worktree_mode: str) -> str | None:
    if harness == "cursor" and worktree_mode == "new":
        return "Cursor will not start the agent until the Chef trusts that new folder in Cursor."
    return None


def workers_for_list(workers: list[dict[str, Any]], repository_path: str | None) -> list[dict[str, Any]]:
    if repository_path is None:
        selected = list(workers)
    else:
        current = canonical_worktree_path(repository_path)
        selected = [
            worker for worker in workers
            if worker.get("repository", {}).get("path")
            and canonical_worktree_path(worker["repository"]["path"]) == current
        ]
    return sorted(selected, key=lambda worker: worker.get("created_at") or "", reverse=True)


def choose_profile(
    assurance: str,
    complexity: str = "low",
    uncertainty: str = "low",
    verification: str = "straightforward",
    judgment: str = "low",
) -> dict[str, str]:
    values = {"low": 0, "straightforward": 0, "moderate": 1, "high": 2, "hard": 2}
    assurance_score = {"LIGHT": 0, "STANDARD": 1, "HIGH": 2}.get(assurance.upper())
    if assurance_score is None:
        raise WorkerError("Assurance must be LIGHT, STANDARD, or HIGH")
    factors = [values.get(complexity), values.get(uncertainty), values.get(verification), values.get(judgment)]
    if any(value is None for value in factors):
        raise WorkerError("Complexity, uncertainty, verification, and judgment must be low, moderate, high, straightforward, or hard")
    score = assurance_score + sum(factors)
    profile_name = "codex-terra-high" if score >= 5 or assurance_score == 2 else "codex-luna-medium"
    profile = dict(PROFILES[profile_name])
    profile["name"] = profile_name
    profile["reason"] = profile["description"]
    return profile


def resolved_configuration(backend: str, harness: str, model: str, reasoning: str, profile: str | None) -> dict[str, str | None]:
    if harness not in HARNESSES:
        raise WorkerError(f"Harness is not supported in v0: {harness}")
    if reasoning not in REASONING_LEVELS:
        raise WorkerError(f"Unsupported reasoning level: {reasoning}")
    return {"backend": backend, "harness": harness, "model": model, "reasoning": reasoning, "profile": profile}


def herdr_agent_start_tail(config: dict[str, Any]) -> list[str]:
    harness = config.get("harness") or "codex"
    if harness == "codex":
        return ["--kind", "codex", "--", "--model", config["model"], "-c", f'model_reasoning_effort="{config["reasoning"]}"']
    if harness == "cursor":
        # Cursor model ids already name the variant. composer-2.5 rejects a separate effort.
        return ["--kind", "cursor", "--", "--model", config["model"]]
    raise WorkerError(f"Harness is not supported in v0: {harness}")


def orca_cli_command() -> str | None:
    """Follow Orca's version-matched CLI resolution without using Linux `orca`."""
    configured = os.environ.get("ORCA_CLI_COMMAND")
    if configured:
        return configured
    if os.environ.get("ORCA_DEV_REPO_ROOT") and shutil.which("orca-dev"):
        return "orca-dev"
    if platform.system() == "Linux":
        return "orca-ide" if shutil.which("orca-ide") else None
    return "orca" if shutil.which("orca") else None


def discover_backend(requested: str, capabilities: dict[str, dict[str, Any]] | None = None) -> str:
    if requested not in {"auto", "orca", "herdr"}:
        raise WorkerError("Backend must be auto, orca, or herdr")
    if capabilities is None:
        # auto still needs both backends; an explicit choice probes only that one.
        capabilities = capability_report() if requested == "auto" else capability_report(requested)
    orca = bool(capabilities.get("orca", {}).get("available"))
    herdr = bool(capabilities.get("herdr", {}).get("available"))
    if requested == "orca":
        if not orca:
            raise WorkerError("Orca is unavailable here. Run inside Orca with its managed CLI available.")
        return "orca"
    if requested == "herdr":
        if not herdr:
            raise WorkerError("Herdr is unavailable here. Run inside a Herdr-managed pane.")
        return "herdr"
    inside_orca = bool(os.environ.get("ORCA_TERMINAL_HANDLE") or os.environ.get("ORCA_PANE_KEY"))
    inside_herdr = os.environ.get("HERDR_ENV") == "1"
    if inside_orca and orca:
        return "orca"
    if inside_herdr and herdr:
        return "herdr"
    if orca:
        return "orca"
    if herdr:
        return "herdr"
    raise WorkerError("No supported worker backend is usable. Start inside Orca or Herdr, or select one explicitly.")


def capability_report(only: str | None = None) -> dict[str, dict[str, Any]]:
    """Report backend availability. `only` skips the other probe; omit it for both."""
    report: dict[str, dict[str, Any]] = {}
    if only in (None, "auto", "orca"):
        orca_command = orca_cli_command()
        orca_available = False
        orca_reason = "requires the version-matched Orca CLI and a ready runtime"
        if orca_command:
            result = run([orca_command, "status", "--json"])
            try:
                status = json.loads(result.stdout) if result.returncode == 0 else {}
            except json.JSONDecodeError:
                status = {}
            orca_available = bool(status.get("ok") and nested(status, "reachable") is True)
            orca_reason = "Orca runtime is ready" if orca_available else "Orca CLI is present, but its runtime is not ready"
        report["orca"] = {"available": orca_available, "reason": orca_reason}
    if only in (None, "auto", "herdr"):
        herdr_context = os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr") is not None
        herdr_available = False
        herdr_reason = "requires HERDR_ENV=1 and the herdr CLI"
        if herdr_context:
            result = run(["herdr", "status"])
            herdr_available = result.returncode == 0
            herdr_reason = "Herdr-managed pane and CLI found" if herdr_available else "Herdr context exists, but its session is not ready"
        report["herdr"] = {"available": herdr_available, "reason": herdr_reason}
    return report


def measure_stage(name: str, action, stages: list[dict[str, Any]] | None = None):
    """Time one startup step. Progress is stderr-only; the record stores name, ms, and ok."""
    started = time.monotonic()
    print(f"brigade: {name} start", file=sys.stderr, flush=True)
    try:
        result = action()
    except Exception as error:
        elapsed = int((time.monotonic() - started) * 1000)
        if stages is not None:
            stages.append({"stage": name, "ms": elapsed, "ok": False, "error": type(error).__name__})
        print(f"brigade: {name} failed {elapsed}ms", file=sys.stderr, flush=True)
        raise
    elapsed = int((time.monotonic() - started) * 1000)
    if stages is not None:
        stages.append({"stage": name, "ms": elapsed, "ok": True})
    print(f"brigade: {name} done {elapsed}ms", file=sys.stderr, flush=True)
    return result


def nested(value: Any, *names: str) -> Any:
    """Find the first matching key in a backend response without coupling to its full schema."""
    if isinstance(value, dict):
        for name in names:
            if value.get(name) is not None:
                return value[name]
        for child in value.values():
            found = nested(child, *names)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = nested(child, *names)
            if found is not None:
                return found
    return None


def text_value(value: Any, *names: str) -> str | None:
    found = nested(value, *names)
    return found if isinstance(found, str) else None


def named_object(value: Any, *names: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, dict):
                return candidate
        for child in value.values():
            found = named_object(child, *names)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = named_object(child, *names)
            if found is not None:
                return found
    return None


def find_key(value: Any, *names: str) -> tuple[bool, Any]:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return True, value[name]
        for child in value.values():
            found, candidate = find_key(child, *names)
            if found:
                return True, candidate
    if isinstance(value, list):
        for child in value:
            found, candidate = find_key(child, *names)
            if found:
                return True, candidate
    return False, None


def orca_failure_may_have_resources(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return True
    if text_value(payload, "dispatchId", "dispatch_id") or named_object(payload, "dispatch"):
        return True
    residual_found, residual = find_key(payload, "residualResources", "residual_resources")
    effects_found, effects = find_key(payload, "effects")
    return not (residual_found and effects_found and not residual and not effects)


def launch_failure_is_uncertain(worker: dict[str, Any], error: Exception) -> bool:
    session = worker.get("backend_session", {})
    return (
        isinstance(error, LaunchRejected) and error.resources_may_exist
    ) or bool(session.get("dispatch_id") or session.get("agent_start_attempted"))


def dispatch_matches(row: Any, dispatch_id: str) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("dispatch_id") == dispatch_id or row.get("dispatchId") == dispatch_id:
        return True
    dispatch = row.get("dispatch")
    return isinstance(dispatch, dict) and dispatch.get("id") == dispatch_id


def lifecycle_state(row: dict[str, Any]) -> str | None:
    task = row.get("task")
    values = [row.get("task_status"), row.get("taskStatus")]
    if isinstance(task, dict):
        values.extend([task.get("status"), task.get("state")])
    values.extend([row.get("workerState"), row.get("worker_state"), row.get("dispatchStatus"), row.get("dispatch_status")])
    for value in values:
        if isinstance(value, str):
            return value.lower()
    return None


def orca_worker_show_lifecycle(response: dict[str, Any]) -> tuple[str, str] | None:
    worker = named_object(response, "worker") or {}
    dispatch = named_object(response, "dispatch") or {}
    worker_state = worker.get("state")
    dispatch_status = dispatch.get("status")
    last_failure = dispatch.get("lastFailure") or dispatch.get("last_failure")
    if worker_state == "stopped" and last_failure == "stopped":
        return "stopped", "Orca reports that the worker was stopped."
    if dispatch_status in {"succeeded", "success", "completed"}:
        return "completed", f"Orca reports dispatch {dispatch_status}."
    if dispatch_status == "failed":
        return "failed", "Orca reports that the dispatch failed."
    return None


def run_has_worker_escalation(value: Any, worker: dict[str, Any]) -> bool:
    encoded = json.dumps(value)
    identifiers = [worker["backend_session"].get("dispatch_id"), worker["backend_session"].get("task_id")]
    return any(identifier and identifier in encoded for identifier in identifiers) and any(
        marker in encoded for marker in ('"type": "escalation"', '"type": "question"', '"type": "decision_gate"')
    )


def effective_launch_configuration(value: Any, *, allow_missing_effort: bool = False) -> dict[str, str] | None:
    launch = named_object(value, "launch")
    effective = (launch or {}).get("effective")
    if not isinstance(effective, dict):
        return None
    model = effective.get("model") or effective.get("modelId")
    reasoning = effective.get("effort")
    if reasoning is None:
        reasoning = effective.get("reasoning")
    if not isinstance(model, str):
        return None
    if not isinstance(reasoning, str):
        if not allow_missing_effort:
            return None
        reasoning = ""
    return {"model": model, "reasoning": reasoning}


@dataclass
class LaunchResult:
    session: dict[str, Any]
    worktree: str


class Backend:
    name: str

    def launch(self, worker: dict[str, Any], worktree_mode: str, persist=None) -> LaunchResult:
        raise NotImplementedError

    def inspect(self, worker: dict[str, Any]) -> tuple[str, str]:
        raise NotImplementedError

    def send(self, worker: dict[str, Any], message: str) -> str:
        raise NotImplementedError

    def resume(self, worker: dict[str, Any], message: str, persist=None) -> str:
        return self.send(worker, message)

    def open(self, worker: dict[str, Any]) -> str:
        raise NotImplementedError

    def stop(self, worker: dict[str, Any]) -> str:
        raise NotImplementedError

    def release(self, worker: dict[str, Any]) -> str:
        raise NotImplementedError

    def read(self, worker: dict[str, Any]) -> str:
        raise NotImplementedError


class OrcaBackend(Backend):
    name = "orca"

    def command(self) -> str:
        command = orca_cli_command()
        if not command:
            raise WorkerError("Orca CLI is not available through its documented resolution")
        return command

    def call(self, arguments: list[str], description: str, *, cwd: str | None = None) -> dict[str, Any]:
        # Orca 1.4.247+ prints a concise orchestration view by default; Brigade parses the full record.
        full = ["--full"] if arguments[0] == "orchestration" else []
        return json_output([self.command(), *arguments, "--json", *full], description, cwd=cwd)

    def existing_worktree_selector(self, path: str) -> str:
        response = self.call(["worktree", "current"], "Resolve Orca worktree", cwd=path)
        worktree = named_object(response, "worktree")
        identity = named_object(worktree, "identity")
        key = (identity or {}).get("key")
        if not isinstance(key, str) or not key:
            raise WorkerError("Orca did not return a stable identity for the selected worktree")
        return f"identity:{key}"

    def launch(self, worker: dict[str, Any], worktree_mode: str, persist=None) -> LaunchResult:
        config = worker["configuration"]
        stages = worker.setdefault("startup_stages", [])

        def resolve_run():
            run_response = self.call(["orchestration", "run-current"], "Inspect Orca run")
            run = named_object(run_response, "run")
            if not run or not isinstance(run.get("id"), str):
                run_response = self.call(["orchestration", "run-create", "--objective", f"Brigade worker: {worker['task']['title']}"], "Create Orca run")
                run = named_object(run_response, "run")
            run_id = run.get("id") if run else None
            if not isinstance(run_id, str):
                raise WorkerError(f"Orca did not return a durable Run identifier. Response: {json.dumps(run_response)[:300]}")
            return run_id

        run_id = measure_stage("resolve_run", resolve_run, stages)
        worker["backend_session"] = {"run_id": run_id}
        if persist:
            persist()
        if worktree_mode == "new":
            selected_worktree = "new-child"
        elif worker["worktree"]["kind"] == "current":
            selected_worktree = "current"
        else:
            selected_worktree = measure_stage("resolve_worktree", lambda: self.existing_worktree_selector(worker["worktree"]["path"]), stages)
        arguments = [
            "orchestration", "worker-start", "--spec", worker["brief"], "--task-title", worker["task"]["title"],
            "--worktree", selected_worktree, "--run", run_id,
            "--agent", config["harness"], "--model", config["model"],
        ]
        if config["harness"] != "cursor":
            arguments.extend(["--effort", config["reasoning"]])
        if worktree_mode == "new":
            arguments.extend(["--name", f"brigade-{worker['id'].removeprefix('w_')}"])
        try:
            response = measure_stage("start_worker", lambda: self.call(arguments, "Launch Orca worker"), stages)
        except BackendCallError as error:
            raise LaunchRejected(str(error), resources_may_exist=orca_failure_may_have_resources(error.payload)) from error
        dispatch = named_object(response, "dispatch")
        dispatch_id = text_value(response, "dispatchId", "dispatch_id") or (dispatch or {}).get("id")
        if not dispatch_id:
            raise WorkerError("Orca did not return a dispatch identifier")
        worktree = named_object(response, "worktree")
        actual_worktree = text_value(response, "worktreePath") or (worktree or {}).get("path") or worker["worktree"]["path"]
        terminal_id = text_value(response, "terminalHandle", "agentTerminalHandle", "terminal_id")
        task = named_object(response, "task")
        task_id = (task or {}).get("id")
        session = {"dispatch_id": dispatch_id, "task_id": task_id, "terminal_id": terminal_id, "run_id": run_id, "launch_started": True}
        worker["backend_session"] = session
        effective = effective_launch_configuration(response, allow_missing_effort=config["harness"] == "cursor")
        if effective:
            worker["effective_configuration"] = effective
        else:
            worker["effective_configuration"] = None
        if persist:
            persist()
        if not effective:
            raise WorkerError("Orca did not confirm the effective model and reasoning configuration")
        model_mismatch = effective["model"] != config["model"]
        reasoning_mismatch = config["harness"] != "cursor" and effective["reasoning"] != config["reasoning"]
        if effective and (model_mismatch or reasoning_mismatch):
            raise WorkerError(f"Orca launched a different configuration: requested {config['model']} / {config['reasoning']}, effective {effective['model']} / {effective['reasoning']}")
        return LaunchResult(session, actual_worktree)

    def inspect(self, worker: dict[str, Any]) -> tuple[str, str]:
        dispatch_id = worker["backend_session"].get("dispatch_id")
        if not isinstance(dispatch_id, str):
            return "unverifiable", "Orca launch began but no Dispatch identifier was persisted."
        try:
            response = self.call(["orchestration", "worker-show", "--dispatch", dispatch_id], "Inspect Orca worker")
        except WorkerError as error:
            return "unverifiable", f"Cannot inspect Orca worker: {error}"
        if nested(response, "agentWait", "agent_wait") is not None:
            return "blocked", "Orca observed the worker waiting for a human answer."
        settled = orca_worker_show_lifecycle(response)
        if settled:
            return settled
        run_id = worker["backend_session"].get("run_id")
        if not run_id:
            return "unverifiable", "Worker has no persisted Orca Run identifier."
        try:
            mailbox = self.call(["orchestration", "check", "--run", run_id, "--peek", "--types", "escalation,question,decision_gate"], "Check Orca worker mailbox")
            if run_has_worker_escalation(mailbox, worker):
                return "blocked", "Orca Run has an escalation or decision request from this worker."
        except WorkerError:
            # Lifecycle truth comes from worker-list. An unreadable mailbox
            # must not turn a healthy worker into a synthetic disappearance.
            pass
        cursor = None
        row = None
        for _ in range(100):
            arguments = ["orchestration", "worker-list", "--run", run_id]
            if cursor:
                arguments.extend(["--cursor", cursor])
            try:
                listing = self.call(arguments, "List Orca worker lifecycle")
            except WorkerError as error:
                return "unverifiable", f"Cannot list Orca worker lifecycle: {error}"
            rows = nested(listing, "workers")
            if not isinstance(rows, list):
                return "unverifiable", "Orca returned no worker lifecycle rows."
            row = next((candidate for candidate in rows if dispatch_matches(candidate, dispatch_id)), None)
            if row is not None:
                break
            page = named_object(listing, "page")
            cursor = (page or {}).get("nextCursor") or text_value(listing, "nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        if row is None:
            return "disappeared", "Orca Run is reachable but no longer lists this Dispatch."
        state = lifecycle_state(row)
        if not state:
            return "unverifiable", "Orca lists the worker but did not expose a task lifecycle state."
        mapping = {"succeeded": "completed", "success": "completed", "done": "completed", "completed": "completed", "failed": "failed", "blocked": "blocked", "stopped": "stopped", "active": "active", "working": "active", "running": "active", "ready": "active", "starting": "active", "pending": "active", "queued": "active", "unverifiable": "unverifiable"}
        return mapping.get(state, "unverifiable"), f"Orca reports worker lifecycle {state}."

    def send(self, worker: dict[str, Any], message: str) -> str:
        dispatch_id = worker["backend_session"]["dispatch_id"]
        self.call(["orchestration", "send", "--to", f"dispatch:{dispatch_id}", "--subject", "Brigade follow-up", "--body", message, "--type", "status"], "Send Orca worker message")
        return "Follow-up delivered through Orca orchestration mail."

    def open(self, worker: dict[str, Any]) -> str:
        terminal = worker["backend_session"].get("terminal_id")
        if not terminal:
            raise WorkerError("Orca did not expose a terminal for this worker. Open it from Orca's worker view.")
        self.call(["terminal", "switch", "--terminal", terminal], "Focus Orca worker")
        return "Focused Orca worker terminal."

    def stop(self, worker: dict[str, Any]) -> str:
        self.call(["orchestration", "worker-stop", "--dispatch", worker["backend_session"]["dispatch_id"]], "Stop Orca worker")
        return "Orca fenced and stopped the supervised worker."

    def release(self, worker: dict[str, Any]) -> str:
        response = self.call(["orchestration", "worker-release", "--dispatch", worker["backend_session"]["dispatch_id"]], "Release Orca worker terminal")
        result = response.get("result", {})
        state = result.get("state")
        if state == "retained" and result.get("reason") == "user_takeover":
            return "Orca left the terminal with the user. The worker is released."
        if state not in {"released", "already_released"}:
            reason = result.get("reason")
            detail = f" ({reason})" if reason else ""
            raise WorkerError(f"Orca did not close the worker terminal: {state or 'unknown state'}{detail}. Inspect the worker tab before closing it directly.")
        return "Orca released the settled worker terminal. The child worktree remains available."

    def read(self, worker: dict[str, Any]) -> str:
        response = self.call(["orchestration", "worker-read", "--dispatch", worker["backend_session"]["dispatch_id"], "--source", "auto", "--limit", "120"], "Read Orca worker handoff")
        return json.dumps(response.get("result", response), ensure_ascii=False)[:12000]


class HerdrBackend(Backend):
    name = "herdr"

    def call(self, arguments: list[str], description: str) -> dict[str, Any]:
        return json_output(["herdr", *arguments], description)

    def launch(self, worker: dict[str, Any], worktree_mode: str, persist=None) -> LaunchResult:
        workspace_id = os.environ.get("HERDR_WORKSPACE_ID")
        if not workspace_id:
            raise WorkerError("Herdr did not provide the current workspace identifier")
        tabs = self.call(["tab", "list", "--workspace", workspace_id], "Find Herdr Workers tab")
        existing = next((tab for tab in tabs.get("result", {}).get("tabs", [])
                         if tab.get("label") == "Workers" and tab.get("workspace_id") == workspace_id), None)
        if existing:
            tab_id = existing["tab_id"]
            panes = self.call(["pane", "list", "--workspace", workspace_id], "Find Workers tab pane")
            anchor = next((pane.get("pane_id") for pane in panes.get("result", {}).get("panes", [])
                           if pane.get("tab_id") == tab_id), None)
            if not anchor:
                raise WorkerError("Herdr Workers tab has no pane")
            pane = self.call(["pane", "split", anchor, "--direction", "right", "--cwd", worker["worktree"]["path"], "--no-focus"], "Create Herdr worker pane")
            pane_id = text_value(pane.get("result", {}).get("pane", {}), "pane_id")
        else:
            created = self.call(["tab", "create", "--workspace", workspace_id, "--label", "Workers", "--cwd", worker["worktree"]["path"], "--no-focus"], "Create Herdr Workers tab")
            tab_id = text_value(created.get("result", {}).get("tab", {}), "tab_id")
            pane_id = text_value(created.get("result", {}).get("root_pane", {}), "pane_id")
        if not tab_id or not pane_id:
            raise WorkerError("Herdr did not return tab and pane identifiers")
        title = re.sub(r"[^a-z0-9]+", "-", worker["task"]["title"].lower()).strip("-") or "task"
        name = f"w-{title[:16].rstrip('-')}-{worker['id'].removeprefix('w_')[:12]}"
        worker["backend_session"] = {"agent_name": name, "tab_id": tab_id, "pane_id": pane_id, "agent_started": False, "agent_start_attempted": False}
        if persist:
            persist()
        config = worker["configuration"]
        worker["backend_session"]["agent_start_attempted"] = True
        if persist:
            persist()
        self.call(["agent", "start", name, "--pane", pane_id, *herdr_agent_start_tail(config)], "Start Herdr worker")
        worker["backend_session"]["agent_started"] = True
        if persist:
            persist()
        self.call(["agent", "prompt", name, worker["brief"]], "Send Herdr worker brief")
        return LaunchResult({"agent_name": name, "tab_id": tab_id, "pane_id": pane_id}, worker["worktree"]["path"])

    def inspect(self, worker: dict[str, Any]) -> tuple[str, str]:
        name = worker["backend_session"]["agent_name"]
        try:
            response = self.call(["agent", "get", name], "Inspect Herdr worker")
        except WorkerError as error:
            return "disappeared", str(error)
        state = (text_value(response.get("result", {}).get("agent", {}), "agent_status") or "unknown").lower()
        if worker.get("status") == "stopping" and state in {"idle", "done"}:
            return "stopped", f"Herdr reports {state} after interruption"
        mapping = {"done": "completed", "idle": "completed", "working": "active", "blocked": "blocked", "unknown": "active"}
        return mapping.get(state, "active"), f"Herdr reports {state}"

    def send(self, worker: dict[str, Any], message: str) -> str:
        self.call(["agent", "prompt", worker["backend_session"]["agent_name"], message], "Send Herdr worker follow-up")
        return "Follow-up submitted to the Herdr agent."

    def open(self, worker: dict[str, Any]) -> str:
        self.call(["agent", "focus", worker["backend_session"]["agent_name"]], "Focus Herdr worker")
        return "Focused Herdr worker pane."

    def stop(self, worker: dict[str, Any]) -> str:
        self.call(["agent", "send-keys", worker["backend_session"]["agent_name"], "ctrl+c"], "Interrupt Herdr worker")
        return "Sent Ctrl+C to the Herdr worker. Reconcile to confirm its settled state."

    def release(self, worker: dict[str, Any]) -> str:
        session = worker["backend_session"]
        pane_id, tab_id = session.get("pane_id"), session.get("tab_id")
        if not pane_id or not tab_id:
            raise WorkerError("Herdr worker has no recorded pane and tab identifiers")
        workspace_id = tab_id.split(":", 1)[0]
        panes = self.call(["pane", "list", "--workspace", workspace_id], "Find Herdr worker pane")
        tab_panes = [pane for pane in panes.get("result", {}).get("panes", []) if pane.get("tab_id") == tab_id]
        target = next((pane for pane in tab_panes if pane.get("pane_id") == pane_id), None)
        if target is None:
            return "Herdr worker pane is already closed."
        agents = self.call(["agent", "list"], "Verify Herdr worker identity")
        occupant = next((agent for agent in agents.get("result", {}).get("agents", []) if agent.get("pane_id") == pane_id), None)
        if occupant and occupant.get("name") != session.get("agent_name"):
            raise WorkerError("Herdr pane no longer contains the recorded worker")
        if len(tab_panes) == 1:
            tabs = self.call(["tab", "list", "--workspace", workspace_id], "Verify Herdr Workers tab")
            tab = next((tab for tab in tabs.get("result", {}).get("tabs", []) if tab.get("tab_id") == tab_id), None)
            if not tab or tab.get("label") != "Workers":
                raise WorkerError("Herdr worker tab is no longer the Workers tab")
            self.call(["tab", "close", tab_id], "Close Herdr Workers tab")
            return "Closed the settled Herdr worker and its empty Workers tab."
        self.call(["pane", "close", pane_id], "Close Herdr worker pane")
        return "Closed the settled Herdr worker pane."

    def read(self, worker: dict[str, Any]) -> str:
        command = ["herdr", "agent", "read", worker["backend_session"]["agent_name"], "--source", "recent-unwrapped", "--lines", "120"]
        return require_ok(run(command), "Read Herdr worker handoff")[:12000]


def backend_for(name: str) -> Backend:
    if name == "orca":
        return OrcaBackend()
    if name == "herdr":
        return HerdrBackend()
    raise WorkerError(f"Unsupported backend: {name}")


def new_worker(title: str, brief: str, configuration: dict[str, str | None], repository: dict[str, str], worktree: str, worktree_kind: str) -> dict[str, Any]:
    worker_id = "w_" + uuid.uuid4().hex[:12]
    created = now()
    return {
        "id": worker_id,
        "task": {"id": worker_id, "title": title},
        "brief": brief,
        "configuration": configuration,
        "repository": repository,
        "worktree": {"path": worktree, "kind": worktree_kind},
        "backend_session": {},
        "status": "active",
        "events": [{"at": created, "kind": "dispatched", "detail": "Worker record created."}],
        "handoff": None,
        "created_at": created,
        "updated_at": created,
    }


def reconcile_worker(worker: dict[str, Any]) -> tuple[str, str]:
    backend = backend_for(worker["configuration"]["backend"])
    status, detail = backend.inspect(worker)
    if status != worker["status"]:
        event(worker, "reconciled", detail, status=status)
    else:
        worker["updated_at"] = now()
    if status in TERMINAL_STATUSES and status != "disappeared":
        try:
            output = backend.read(worker)
        except WorkerError as error:
            output = f"Handoff output unavailable: {error}"
        worker["handoff"] = {"at": now(), "status": status, "summary": detail, "output": output}
    return status, detail


def resolve_failed_launch(worker: dict[str, Any]) -> None:
    if worker.get("status") != "unverifiable":
        raise WorkerError("Only an unverifiable worker can be resolved as a failed launch")
    session = worker.get("backend_session", {})
    resource_keys = ("dispatch_id", "task_id", "terminal_id", "agent_name", "pane_id")
    if any(session.get(key) for key in resource_keys) or session.get("agent_started"):
        raise WorkerError("Worker has a persisted backend resource and cannot be resolved as a pre-launch failure")
    event(worker, "launch_resolved_failed", "No persisted backend worker identifier exists.", status="failed")


def handoff(worker: dict[str, Any]) -> str:
    session = worker.get("backend_session", {})
    return json.dumps({"worker": worker["id"], "status": worker["status"], "worktree": worker["worktree"]["path"], "backend": worker["configuration"]["backend"], "session": session, "events": worker.get("events", [])[-3:]}, indent=2)
