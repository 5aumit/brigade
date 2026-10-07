#!/usr/bin/env python3
"""Command line entry point for the Brigade worker control plane."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import worker_control as control


def fail(error: Exception) -> int:
    print(f"ERROR: {error}", file=sys.stderr)
    return 1


def output(value, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def brief_from(args) -> str:
    text = Path(args.brief_file).read_text(encoding="utf-8") if args.brief_file else args.brief
    if not text or not text.strip():
        raise control.WorkerError("Dispatch requires a non-empty explicit brief")
    return text.strip() + """

Worker operating rules:
- Work only on this bounded work unit. Do not dispatch other workers.
- Handle only MINOR safe adjacent changes needed to complete it.
- Surface MEANINGFUL findings. Stop and escalate DECISION or CRITICAL findings to the primary.
- Do not create commits, branches, pull requests, merges, or remote changes unless the brief names existing authority for that action.
- Finish with a concise handoff: changed files, verification, remaining uncertainty, and any decision needed.
"""


def command_capabilities(args) -> int:
    report = control.capability_report()
    if args.json:
        output(report, True)
    else:
        print("Worker backends:")
        for name, value in report.items():
            print(f"  {name.title()}: {'available' if value['available'] else 'unavailable'} - {value['reason']}")
    return 0


def command_recommend(args) -> int:
    profile = control.choose_profile(args.assurance, args.complexity, args.uncertainty, args.verification, args.judgment)
    backend = control.discover_backend(args.backend) if args.backend != "auto" or any(item["available"] for item in control.capability_report().values()) else "current/default"
    value = {"assurance": args.assurance.upper(), "ownership": args.ownership.upper(), "backend": backend, **profile}
    if args.json:
        output(value, True)
    else:
        print(f"Assurance: {value['assurance']}")
        print(f"Ownership: {value['ownership']}")
        print("\nSuggested worker:")
        print(f"  backend: {backend}")
        print(f"  harness: {profile['harness']}")
        print(f"  model: {profile['model']}")
        print(f"  reasoning: {profile['reasoning']}")
        print(f"\nReason: {profile['reason']}")
    return 0


def command_dispatch(args) -> int:
    if not args.approved:
        raise control.WorkerError("Dispatch requires explicit approval. Ask the user, then rerun with --approved.")
    state = control.load_state()
    backend_name = control.discover_backend(args.backend)
    profile = control.PROFILES.get(args.profile) if args.profile else None
    harness = args.harness or (profile or {}).get("harness", "codex")
    model = args.model or (profile or {}).get("model", "gpt-5.6-luna")
    reasoning = args.reasoning or (profile or {}).get("reasoning", "medium")
    configuration = control.resolved_configuration(backend_name, harness, model, reasoning, args.profile)
    repository = control.git_repository(args.repository)
    worker_id = "w_" + __import__("uuid").uuid4().hex[:12]
    if args.worktree == "current":
        worktree = repository["path"]
        kind = "current"
    elif args.worktree == "new":
        worktree = repository["path"] if backend_name == "orca" else control.create_worktree(repository, worker_id)
        kind = "backend-child" if backend_name == "orca" else "created"
    else:
        worktree = str(Path(args.worktree).resolve())
        if worktree not in control.git_worktrees(repository["path"]):
            raise control.WorkerError(f"Selected worktree is not registered by Git: {worktree}")
        kind = "existing"
    shared_worktree = False
    if not (backend_name == "orca" and args.worktree == "new"):
        try:
            control.assert_available_worktree(state, worktree)
        except control.WorkerError:
            if not args.allow_shared_worktree:
                raise
            shared_worktree = True
    worker = control.new_worker(args.title, brief_from(args), configuration, repository, worktree, kind)
    # Keep the generated ID stable across state and any generated worktree path.
    worker["id"] = worker_id
    worker["task"]["id"] = worker_id
    worker["worktree"]["shared_by_user_override"] = shared_worktree
    state["workers"].append(worker)
    control.save_state(state)

    def persist_progress():
        control.save_state(state)

    notice = control.cursor_new_worktree_warning(harness, args.worktree)
    if notice:
        print(notice)
    try:
        result = control.backend_for(backend_name).launch(worker, args.worktree, persist_progress)
    except Exception as error:
        uncertain = control.launch_failure_is_uncertain(worker, error)
        control.event(worker, "launch_uncertain" if uncertain else "launch_failed", str(error), status="unverifiable" if uncertain else "failed")
        control.save_state(state)
        if isinstance(error, control.WorkerError):
            raise
        raise control.WorkerError(f"Worker launch failed: {error}") from error
    worker["backend_session"] = result.session
    worker["worktree"]["path"] = control.canonical_worktree_path(result.worktree)
    control.event(worker, "launched", f"Launched through {backend_name}.")
    control.save_state(state)
    print(f"Worker {worker['id']} launched.")
    output(worker, args.json)
    return 0


def command_list(args) -> int:
    state = control.load_state()
    repository_path = None if args.all else control.git_repository()["path"]
    workers = control.workers_for_list(state["workers"], repository_path)
    if args.json:
        output(workers, True)
    elif not workers:
        print("No persisted workers.")
    else:
        for worker in workers:
            print(f"{worker['id']}  {worker['status']:11}  {worker['configuration']['backend']:5}  {worker['task']['title']}")
    return 0


def command_status(args) -> int:
    state = control.load_state()
    worker = control.find_worker(state, args.worker_id)
    if args.refresh and worker["status"] in control.ACTIVE_STATUSES:
        control.reconcile_worker(worker)
        control.save_state(state)
    output(worker, args.json)
    return 0


def command_send(args) -> int:
    state = control.load_state()
    worker = control.find_worker(state, args.worker_id)
    detail = control.deliver_follow_up(state, worker, args.message, lambda: control.save_state(state))
    control.save_state(state)
    print(detail)
    return 0


def command_open(args) -> int:
    worker = control.find_worker(control.load_state(), args.worker_id)
    print(control.backend_for(worker["configuration"]["backend"]).open(worker))
    return 0


def command_stop(args) -> int:
    state = control.load_state()
    worker = control.find_worker(state, args.worker_id)
    if worker["status"] in control.TERMINAL_STATUSES:
        print(f"Worker is already {worker['status']}.")
        return 0
    detail = control.backend_for(worker["configuration"]["backend"]).stop(worker)
    control.event(worker, "stop_requested", detail, status="stopping")
    control.save_state(state)
    print(detail)
    return 0


def command_release(args) -> int:
    state = control.load_state()
    worker = control.find_worker(state, args.worker_id)
    if worker["status"] in control.ACTIVE_STATUSES:
        control.reconcile_worker(worker)
    if worker["status"] not in control.TERMINAL_STATUSES:
        control.save_state(state)
        raise control.WorkerError(f"Cannot release a {worker['status']} worker. Stop it or wait for it to settle first.")
    detail = control.backend_for(worker["configuration"]["backend"]).release(worker)
    control.event(worker, "released", detail)
    control.save_state(state)
    print(detail)
    return 0


def command_reconcile(args) -> int:
    state = control.load_state()
    results = []
    for worker in state["workers"]:
        if worker["status"] in control.ACTIVE_STATUSES:
            status, detail = control.reconcile_worker(worker)
            results.append({"id": worker["id"], "status": status, "detail": detail})
    control.save_state(state)
    output(results, args.json)
    return 0


def command_resolve_failed(args) -> int:
    state = control.load_state()
    worker = control.find_worker(state, args.worker_id)
    control.resolve_failed_launch(worker)
    control.save_state(state)
    print(f"Resolved {worker['id']} as a failed pre-launch attempt. Writer occupancy released.")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="brigade", description="Brigade worker control plane")
    groups = root.add_subparsers(dest="group", required=True)
    worker = groups.add_parser("worker", help="dispatch and supervise bounded coding workers")
    commands = worker.add_subparsers(dest="command", required=True)

    capabilities = commands.add_parser("capabilities", help="report optional backend capability")
    capabilities.add_argument("--json", action="store_true")
    capabilities.set_defaults(function=command_capabilities)

    recommend = commands.add_parser("recommend", help="suggest a declarative worker profile")
    recommend.add_argument("--assurance", required=True, choices=["LIGHT", "STANDARD", "HIGH", "light", "standard", "high"])
    recommend.add_argument("--ownership", default="DELEGATE", choices=["DELEGATE", "REVIEW", "UNDERSTAND", "delegate", "review", "understand"])
    recommend.add_argument("--complexity", default="low", choices=control.PROFILE_FACTORS)
    recommend.add_argument("--uncertainty", default="low", choices=control.PROFILE_FACTORS)
    recommend.add_argument("--verification", default="straightforward", choices=control.PROFILE_FACTORS)
    recommend.add_argument("--judgment", default="low", choices=control.PROFILE_FACTORS)
    recommend.add_argument("--backend", default="auto")
    recommend.add_argument("--json", action="store_true")
    recommend.set_defaults(function=command_recommend)

    dispatch = commands.add_parser("dispatch", help="launch one approved bounded worker")
    dispatch.add_argument("--approved", action="store_true", help="records the user's explicit dispatch approval")
    dispatch.add_argument("--title", required=True)
    brief = dispatch.add_mutually_exclusive_group(required=True)
    brief.add_argument("--brief")
    brief.add_argument("--brief-file")
    dispatch.add_argument("--backend", default="auto")
    dispatch.add_argument("--harness")
    dispatch.add_argument("--model")
    dispatch.add_argument("--reasoning")
    dispatch.add_argument("--profile", choices=sorted(control.PROFILES))
    dispatch.add_argument("--repository", default=".")
    dispatch.add_argument("--worktree", default="new", help="new, current, or an existing Git worktree path")
    dispatch.add_argument("--allow-shared-worktree", action="store_true", help="records the User's explicit override of active-writer protection")
    dispatch.add_argument("--json", action="store_true")
    dispatch.set_defaults(function=command_dispatch)

    listing = commands.add_parser("list", help="list persisted worker records")
    listing.add_argument("--all", action="store_true", help="show workers from every repository")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(function=command_list)

    status = commands.add_parser("status", help="show one worker")
    status.add_argument("worker_id")
    status.add_argument("--refresh", action="store_true")
    status.add_argument("--json", action="store_true")
    status.set_defaults(function=command_status)

    send = commands.add_parser("send", help="send a same-work-unit follow-up")
    send.add_argument("worker_id")
    send.add_argument("message")
    send.set_defaults(function=command_send)

    opening = commands.add_parser("open", help="focus a native worker session")
    opening.add_argument("worker_id")
    opening.set_defaults(function=command_open)

    stop = commands.add_parser("stop", help="request worker stop")
    stop.add_argument("worker_id")
    stop.set_defaults(function=command_stop)

    release = commands.add_parser("release", help="close a settled worker terminal while preserving its output")
    release.add_argument("worker_id")
    release.set_defaults(function=command_release)

    reconcile = commands.add_parser("reconcile", help="compare persisted records to live backends")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(function=command_reconcile)

    resolve_failed = commands.add_parser("resolve-failed", help="release an unverifiable record that has no backend worker identifier")
    resolve_failed.add_argument("worker_id")
    resolve_failed.set_defaults(function=command_resolve_failed)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.function(args)
    except control.WorkerError as error:
        return fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
