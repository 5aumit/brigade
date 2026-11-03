from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import worker_control as control


class WorkerControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state" / "workers.json"

    def tearDown(self):
        self.temporary.cleanup()

    def worker(self, worker_id="w_one", path="/repo/task", status="active"):
        return {"id": worker_id, "status": status, "worktree": {"path": path}, "events": [], "updated_at": "now"}

    def test_state_round_trip(self):
        value = {"version": 1, "workers": [self.worker()]}
        control.save_state(value, self.state)
        self.assertEqual(control.load_state(self.state), value)
        self.assertEqual(json.loads(self.state.read_text())["version"], 1)

    def test_state_path_uses_legacy_history_until_brigade_state_exists(self):
        root = Path(self.temporary.name) / "state-root"
        legacy = root / "5stack" / "workers.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"version": 1, "workers": []}\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root)}):
            self.assertEqual(control.state_path(), legacy)
            brigade = root / "brigade" / "workers.json"
            brigade.parent.mkdir()
            brigade.write_text('{"version": 1, "workers": []}\n', encoding="utf-8")
            with self.assertRaisesRegex(control.WorkerError, "both"):
                control.state_path()

    def test_state_path_defaults_to_brigade_for_fresh_installations(self):
        root = Path(self.temporary.name) / "fresh-state-root"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root)}):
            self.assertEqual(control.state_path(), root / "brigade" / "workers.json")

    def test_occupancy_blocks_only_active_writer(self):
        state = {"version": 1, "workers": [self.worker()]}
        with self.assertRaisesRegex(control.WorkerError, "w_one"):
            control.assert_available_worktree(state, "/repo/task")
        state["workers"][0]["status"] = "completed"
        control.assert_available_worktree(state, "/repo/task")

    def test_profile_selection_considers_multiple_factors(self):
        simple = control.choose_profile("STANDARD", "low", "low", "straightforward", "low")
        difficult = control.choose_profile("STANDARD", "high", "high", "hard", "high")
        self.assertEqual(simple["name"], "codex-luna-medium")
        self.assertEqual(difficult["name"], "codex-terra-high")
        self.assertEqual(difficult["reasoning"], "high")

    def test_configuration_preserves_selected_values(self):
        config = control.resolved_configuration("orca", "codex", "gpt-5.6-terra", "high", "codex-terra-high")
        self.assertEqual(config, {"backend": "orca", "harness": "codex", "model": "gpt-5.6-terra", "reasoning": "high", "profile": "codex-terra-high"})
        cursor = control.resolved_configuration("orca", "cursor", "composer-2.5", "low", None)
        self.assertEqual(cursor["harness"], "cursor")
        self.assertEqual(cursor["model"], "composer-2.5")
        with self.assertRaisesRegex(control.WorkerError, "not supported"):
            control.resolved_configuration("orca", "claude", "opus", "medium", None)

    def test_herdr_cursor_start_uses_cursor_kind(self):
        tail = control.herdr_agent_start_tail({"harness": "cursor", "model": "composer-2.5", "reasoning": "low"})
        self.assertEqual(tail, ["--kind", "cursor", "--", "--model", "composer-2.5"])
        codex = control.herdr_agent_start_tail({"model": "gpt-5.6-luna", "reasoning": "medium"})
        self.assertEqual(codex[:2], ["--kind", "codex"])
        self.assertIn("model_reasoning_effort", " ".join(codex))

    def test_capabilities_are_optional(self):
        original = {key: os.environ.get(key) for key in ("ORCA_CLI_COMMAND", "ORCA_TERMINAL_HANDLE", "HERDR_ENV")}
        original_orca_command = control.orca_cli_command
        try:
            for key in original:
                os.environ.pop(key, None)
            control.orca_cli_command = lambda: None
            capabilities = control.capability_report()
            self.assertFalse(capabilities["orca"]["available"])
            self.assertFalse(capabilities["herdr"]["available"])
        finally:
            control.orca_cli_command = original_orca_command
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_reconcile_persists_terminal_change(self):
        worker = self.worker()
        worker["configuration"] = {"backend": "orca"}
        original = control.backend_for

        class Finished:
            def inspect(self, _worker):
                return "completed", "test backend reports completion"

            def read(self, _worker):
                return "worker handoff"

        control.backend_for = lambda _name: Finished()
        try:
            status, detail = control.reconcile_worker(worker)
        finally:
            control.backend_for = original
        self.assertEqual(status, "completed")
        self.assertEqual(detail, "test backend reports completion")
        self.assertEqual(worker["status"], "completed")
        self.assertEqual(worker["handoff"]["output"], "worker handoff")

    def test_windows_orca_worktree_path_stays_stable(self):
        self.assertEqual(
            control.canonical_worktree_path(r"D:\tech_stuff\worker"),
            r"d:\tech_stuff\worker",
        )

    def test_orca_launch_uses_durable_run_and_dispatch_identifier(self):
        worker = {
            "id": "w_test", "brief": "bounded brief", "task": {"title": "Test worker"},
            "configuration": {"harness": "codex", "model": "gpt-5.6-luna", "reasoning": "medium"},
            "worktree": {"path": "/repo", "kind": "current"},
        }
        backend = control.OrcaBackend()
        calls = []
        replies = iter([
            {"id": "request-one", "ok": True, "result": {"run": None}},
            {"id": "request-two", "ok": True, "result": {"run": {"id": "run_123"}}},
            {"id": "request-three", "ok": True, "result": {"dispatch": {"id": "dispatch_123"}, "task": {"id": "task_123"}, "worktree": {"path": "/repo"}, "terminalHandle": "term_123", "launch": {"effective": {"model": "gpt-5.6-luna", "effort": "medium"}}}},
        ])
        backend.call = lambda arguments, _description: calls.append(arguments) or next(replies)
        result = backend.launch(worker, "current")
        self.assertEqual(result.session, {"dispatch_id": "dispatch_123", "task_id": "task_123", "terminal_id": "term_123", "run_id": "run_123", "launch_started": True})
        self.assertIn(["orchestration", "run-create", "--objective", "Brigade worker: Test worker"], calls)
        self.assertIn("--run", calls[-1])
        self.assertIn("run_123", calls[-1])
        self.assertIn("--effort", calls[-1])

    def test_orca_orchestration_calls_request_full_record(self):
        backend = control.OrcaBackend()
        backend.command = lambda: "orca"
        commands = []
        with mock.patch.object(control, "json_output", lambda command, *_args, **_kwargs: commands.append(command) or {}):
            backend.call(["orchestration", "run-current"], "Inspect Orca run")
            backend.call(["worktree", "current"], "Resolve Orca worktree")
        self.assertEqual(commands, [["orca", "orchestration", "run-current", "--json", "--full"], ["orca", "worktree", "current", "--json"]])

    def test_orca_cursor_launch_omits_effort(self):
        worker = {
            "id": "w_cursor", "brief": "read only", "task": {"title": "Read install commands"},
            "configuration": {"harness": "cursor", "model": "composer-2.5", "reasoning": "low"},
            "worktree": {"path": "/repo", "kind": "current"},
        }
        backend = control.OrcaBackend()
        calls = []
        replies = iter([
            {"ok": True, "result": {"run": {"id": "run_123"}}},
            {"ok": True, "result": {"dispatch": {"id": "dispatch_123"}, "task": {"id": "task_123"}, "worktree": {"path": "/repo"}, "launch": {"effective": {"model": "composer-2.5", "effort": None}}}},
        ])
        backend.call = lambda arguments, _description: calls.append(arguments) or next(replies)
        backend.launch(worker, "current")
        self.assertEqual(calls[-1][calls[-1].index("--agent") + 1], "cursor")
        self.assertNotIn("--effort", calls[-1])

    def test_orca_new_child_has_deterministic_name(self):
        worker = {
            "id": "w_abc123", "brief": "bounded brief", "task": {"title": "Test worker"},
            "configuration": {"harness": "codex", "model": "gpt-5.6-luna", "reasoning": "medium"},
            "worktree": {"path": "/repo", "kind": "backend-child"},
        }
        backend = control.OrcaBackend()
        calls = []
        replies = iter([
            {"ok": True, "result": {"run": {"id": "run_123"}}},
            {"ok": True, "result": {"dispatch": {"id": "dispatch_123"}, "launch": {"effective": {"model": "gpt-5.6-luna", "effort": "medium"}}}},
        ])
        backend.call = lambda arguments, _description: calls.append(arguments) or next(replies)
        backend.launch(worker, "new")
        self.assertIn("new-child", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("--name") + 1], "brigade-abc123")

    def test_orca_existing_worktree_uses_identity_from_its_directory(self):
        backend = control.OrcaBackend()
        calls = []

        def call(arguments, _description, *, cwd=None):
            calls.append((arguments, cwd))
            return {"result": {"worktree": {"identity": {"key": "wt2:local:123"}}}}

        backend.call = call
        self.assertEqual(backend.existing_worktree_selector("/mnt/d/repo/task"), "identity:wt2:local:123")
        self.assertEqual(calls, [(["worktree", "current"], "/mnt/d/repo/task")])

    def test_orca_explicit_prelaunch_rejection_is_not_uncertain(self):
        payload = {"ok": False, "error": {"code": "invalid_argument"}, "effects": [], "residualResources": []}
        self.assertFalse(control.orca_failure_may_have_resources(payload))
        error = control.LaunchRejected("rejected", resources_may_exist=False)
        worker = {"backend_session": {"run_id": "run_123"}}
        self.assertFalse(control.launch_failure_is_uncertain(worker, error))

    def test_orca_ambiguous_rejection_remains_uncertain(self):
        self.assertTrue(control.orca_failure_may_have_resources({"ok": False}))
        error = control.LaunchRejected("unknown", resources_may_exist=True)
        self.assertTrue(control.launch_failure_is_uncertain({"backend_session": {}}, error))

    def test_resolve_failed_launch_releases_occupancy(self):
        worker = self.worker(status="unverifiable")
        worker["backend_session"] = {"run_id": "run_123", "launch_started": True}
        state = {"version": 1, "workers": [worker]}
        with self.assertRaises(control.WorkerError):
            control.assert_available_worktree(state, "/repo/task")
        control.resolve_failed_launch(worker)
        control.assert_available_worktree(state, "/repo/task")
        self.assertEqual(worker["status"], "failed")

    def test_resolve_failed_launch_rejects_persisted_dispatch(self):
        worker = self.worker(status="unverifiable")
        worker["backend_session"] = {"dispatch_id": "dispatch_123"}
        with self.assertRaisesRegex(control.WorkerError, "persisted backend resource"):
            control.resolve_failed_launch(worker)

    def test_orca_reconcile_uses_task_lifecycle_not_terminal_state(self):
        backend = control.OrcaBackend()
        worker = {"backend_session": {"dispatch_id": "dispatch_123", "task_id": "task_123", "run_id": "run_123"}}
        replies = iter([
            {"ok": True, "result": {"observation": {"agentWait": None}, "status": "active"}},
            {"ok": True, "result": {"messages": []}},
            {"ok": True, "result": {"workers": [{"dispatch": {"id": "dispatch_123"}, "task": {"status": "succeeded"}}]}},
        ])
        backend.call = lambda _arguments, _description: next(replies)
        self.assertEqual(backend.inspect(worker), ("completed", "Orca reports worker lifecycle succeeded."))

    def test_orca_reconcile_recognizes_operator_stopped_worker(self):
        backend = control.OrcaBackend()
        worker = {"backend_session": {"dispatch_id": "dispatch_123", "run_id": "run_123"}}
        backend.call = lambda _arguments, _description: {
            "result": {
                "dispatch": {"id": "dispatch_123", "status": "failed", "lastFailure": "stopped"},
                "worker": {"state": "stopped"},
            }
        }
        self.assertEqual(backend.inspect(worker), ("stopped", "Orca reports that the worker was stopped."))

    def test_orca_worker_list_fields_are_lifecycle_inputs(self):
        self.assertEqual(control.lifecycle_state({"workerState": "running", "dispatchStatus": "pending"}), "running")

    def test_orca_release_targets_exact_dispatch(self):
        backend = control.OrcaBackend()
        calls = []
        backend.call = lambda arguments, _description: calls.append(arguments) or {"result": {"state": "released"}}
        detail = backend.release({"backend_session": {"dispatch_id": "dispatch_123"}})
        self.assertEqual(calls, [["orchestration", "worker-release", "--dispatch", "dispatch_123"]])
        self.assertIn("child worktree remains", detail)

    def test_orca_release_accepts_user_owned_terminal(self):
        backend = control.OrcaBackend()
        backend.call = lambda _arguments, _description: {"result": {"state": "retained", "reason": "user_takeover"}}
        detail = backend.release({"backend_session": {"dispatch_id": "dispatch_123"}})
        self.assertIn("left the terminal with the user", detail)
        backend.call = lambda _arguments, _description: {"result": {"state": "retained", "reason": "release_pending"}}
        with self.assertRaisesRegex(control.WorkerError, "retained \\(release_pending\\)"):
            backend.release({"backend_session": {"dispatch_id": "dispatch_123"}})

    def test_orca_backend_failure_is_unverifiable_not_disappeared(self):
        backend = control.OrcaBackend()
        backend.call = lambda _arguments, _description: (_ for _ in ()).throw(control.WorkerError("runtime down"))
        worker = {"backend_session": {"dispatch_id": "dispatch_123", "run_id": "run_123"}}
        self.assertEqual(backend.inspect(worker)[0], "unverifiable")

    def test_orca_reconcile_follows_worker_list_pages(self):
        backend = control.OrcaBackend()
        worker = {"backend_session": {"dispatch_id": "dispatch_123", "task_id": "task_123", "run_id": "run_123"}}
        calls = []
        replies = iter([
            {"ok": True, "result": {"observation": {"agentWait": None}}},
            {"ok": True, "result": {"messages": []}},
            {"ok": True, "result": {"workers": [{"dispatch": {"id": "other"}, "task": {"status": "running"}}], "page": {"nextCursor": "next"}}},
            {"ok": True, "result": {"workers": [{"dispatch": {"id": "dispatch_123"}, "task": {"status": "failed"}}], "page": {"nextCursor": None}}},
        ])
        backend.call = lambda arguments, _description: calls.append(arguments) or next(replies)
        self.assertEqual(backend.inspect(worker)[0], "failed")
        self.assertIn(["orchestration", "worker-list", "--run", "run_123", "--cursor", "next"], calls)

    def test_orca_effective_configuration_mismatch_is_persisted_and_reported(self):
        backend = control.OrcaBackend()
        worker = {
            "brief": "bounded brief", "task": {"title": "Test worker"},
            "configuration": {"harness": "codex", "model": "gpt-5.6-terra", "reasoning": "high"},
            "worktree": {"path": "/repo", "kind": "current"},
        }
        replies = iter([
            {"ok": True, "result": {"run": {"id": "run_123"}}},
            {"ok": True, "result": {"dispatch": {"id": "dispatch_123"}, "task": {"id": "task_123"}, "launch": {"effective": {"model": "gpt-5.6-luna", "effort": "medium"}}}},
        ])
        backend.call = lambda _arguments, _description: next(replies)
        snapshots = []
        with self.assertRaisesRegex(control.WorkerError, "different configuration"):
            backend.launch(worker, "current", lambda: snapshots.append(dict(worker["backend_session"])))
        self.assertEqual(worker["effective_configuration"], {"model": "gpt-5.6-luna", "reasoning": "medium"})
        self.assertEqual(snapshots[-1]["dispatch_id"], "dispatch_123")

    def test_orca_missing_effective_configuration_is_unconfirmed(self):
        backend = control.OrcaBackend()
        worker = {
            "brief": "bounded brief", "task": {"title": "Test worker"},
            "configuration": {"harness": "codex", "model": "gpt-5.6-luna", "reasoning": "medium"},
            "worktree": {"path": "/repo", "kind": "current"},
        }
        replies = iter([
            {"ok": True, "result": {"run": {"id": "run_123"}}},
            {"ok": True, "result": {"dispatch": {"id": "dispatch_123"}, "task": {"id": "task_123"}}},
        ])
        backend.call = lambda _arguments, _description: next(replies)
        with self.assertRaisesRegex(control.WorkerError, "did not confirm"):
            backend.launch(worker, "current", lambda: None)
        self.assertIsNone(worker["effective_configuration"])

    def test_herdr_launch_persists_pane_before_agent_start(self):
        backend = control.HerdrBackend()
        worker = {"id": "w_abc", "task": {"title": "Check dispatch"}, "worktree": {"path": "/repo"}, "configuration": {"model": "gpt-5.6-luna", "reasoning": "medium"}}
        snapshots = []
        calls = []
        replies = iter([
            {"id": "cli:tab:list", "result": {"tabs": []}},
            {"id": "cli:tab:create", "result": {"tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p2"}}},
            control.WorkerError("start failed"),
        ])

        def call(arguments, _description):
            calls.append(arguments)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        backend.call = call
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}):
            with self.assertRaisesRegex(control.WorkerError, "start failed"):
                backend.launch(worker, "new", lambda: snapshots.append(dict(worker["backend_session"])))
        self.assertEqual(calls[1], ["tab", "create", "--workspace", "w1", "--label", "Workers", "--cwd", "/repo", "--no-focus"])
        self.assertEqual(snapshots, [
            {"agent_name": "w-check-dispatch-abc", "tab_id": "w1:t2", "pane_id": "w1:p2", "agent_started": False, "agent_start_attempted": False},
            {"agent_name": "w-check-dispatch-abc", "tab_id": "w1:t2", "pane_id": "w1:p2", "agent_started": False, "agent_start_attempted": True},
        ])

    def test_herdr_reuses_workers_tab_and_nested_pane_id(self):
        backend = control.HerdrBackend()
        worker = {"id": "w_def", "task": {"title": "60 second test"}, "brief": "Check", "worktree": {"path": "/repo"}, "configuration": {"model": "gpt-5.6-luna", "reasoning": "medium"}}
        calls = []
        replies = iter([
            {"id": "cli:tab:list", "result": {"tabs": [{"label": "Workers", "tab_id": "w1:t2", "workspace_id": "w1"}]}},
            {"id": "cli:pane:list", "result": {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2"}]}},
            {"id": "cli:pane:split", "result": {"pane": {"pane_id": "w1:p3"}}},
            {"result": {}}, {"result": {}},
        ])
        backend.call = lambda arguments, _description: calls.append(arguments) or next(replies)
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}):
            backend.launch(worker, "current")
        self.assertEqual(calls[2], ["pane", "split", "w1:p2", "--direction", "right", "--cwd", "/repo", "--no-focus"])
        self.assertEqual(calls[3][:7], ["agent", "start", "w-60-second-test-def", "--pane", "w1:p3", "--kind", "codex"])
        self.assertIn("gpt-5.6-luna", calls[3])

    def test_herdr_inspect_nested_status_after_stop(self):
        backend = control.HerdrBackend()
        worker = {"status": "active", "backend_session": {"agent_name": "w-check-abc"}}
        backend.call = lambda _arguments, _description: {"id": "cli:agent:get", "result": {"agent": {"agent_status": "working"}}}
        self.assertEqual(backend.inspect(worker), ("active", "Herdr reports working"))
        backend.call = lambda _arguments, _description: {"id": "cli:agent:get", "result": {"agent": {"agent_status": "idle"}}}
        self.assertEqual(backend.inspect(worker), ("completed", "Herdr reports idle"))
        worker["status"] = "stopping"
        backend.call = lambda _arguments, _description: {"id": "cli:agent:get", "result": {"agent": {"agent_status": "idle"}}}
        self.assertEqual(backend.inspect(worker), ("stopped", "Herdr reports idle after interruption"))

    def test_herdr_read_preserves_plain_text(self):
        backend = control.HerdrBackend()
        output = "Worker finished.\nNo files changed.\n"
        with mock.patch.object(control, "run", return_value=control.subprocess.CompletedProcess([], 0, output, "")) as run:
            self.assertEqual(backend.read({"backend_session": {"agent_name": "w-check-abc"}}), output)
        run.assert_called_once_with(["herdr", "agent", "read", "w-check-abc", "--source", "recent-unwrapped", "--lines", "120"])

    def test_herdr_open_targets_agent_focus(self):
        backend = control.HerdrBackend()
        calls = []
        backend.call = lambda arguments, _description: calls.append(arguments) or {"result": {}}
        backend.open({"backend_session": {"agent_name": "worker-abc", "pane_id": "w1:p2"}})
        self.assertEqual(calls, [["agent", "focus", "worker-abc"]])

    def test_herdr_release_closes_only_worker_pane_when_tab_is_shared(self):
        backend = control.HerdrBackend()
        calls = []
        replies = iter([
            {"result": {"panes": [
                {"pane_id": "w1:p2", "tab_id": "w1:t2"},
                {"pane_id": "w1:p3", "tab_id": "w1:t2"},
            ]}},
            {"result": {"agents": [{"pane_id": "w1:p2", "name": "worker-abc"}]}},
            {"result": {}},
        ])
        backend.call = lambda arguments, _description: calls.append(arguments) or next(replies)
        detail = backend.release({"backend_session": {"agent_name": "worker-abc", "pane_id": "w1:p2", "tab_id": "w1:t2"}})
        self.assertEqual(calls, [["pane", "list", "--workspace", "w1"], ["agent", "list"], ["pane", "close", "w1:p2"]])
        self.assertIn("worker pane", detail)

    def test_herdr_release_closes_last_worker_pane_with_tab(self):
        backend = control.HerdrBackend()
        calls = []
        replies = iter([
            {"result": {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2"}]}},
            {"result": {"agents": [{"pane_id": "w1:p2", "name": "worker-abc"}]}},
            {"result": {"tabs": [{"tab_id": "w1:t2", "label": "Workers"}]}},
            {"result": {}},
        ])
        backend.call = lambda arguments, _description: calls.append(arguments) or next(replies)
        backend.release({"backend_session": {"agent_name": "worker-abc", "pane_id": "w1:p2", "tab_id": "w1:t2"}})
        self.assertEqual(calls[-1], ["tab", "close", "w1:t2"])

    def test_herdr_release_rejects_repurposed_pane(self):
        backend = control.HerdrBackend()
        replies = iter([
            {"result": {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2"}]}},
            {"result": {"agents": [{"pane_id": "w1:p2", "name": "someone-else"}]}},
        ])
        backend.call = lambda _arguments, _description: next(replies)
        with self.assertRaisesRegex(control.WorkerError, "no longer contains"):
            backend.release({"backend_session": {"agent_name": "worker-abc", "pane_id": "w1:p2", "tab_id": "w1:t2"}})

    def test_orca_linux_resolution_avoids_bare_orca(self):
        previous_command = os.environ.pop("ORCA_CLI_COMMAND", None)
        previous_dev = os.environ.pop("ORCA_DEV_REPO_ROOT", None)
        original_which, original_system = control.shutil.which, control.platform.system
        control.shutil.which = lambda name: "/tmp/orca-ide" if name == "orca-ide" else None
        control.platform.system = lambda: "Linux"
        try:
            self.assertEqual(control.orca_cli_command(), "orca-ide")
        finally:
            control.shutil.which, control.platform.system = original_which, original_system
            if previous_command is not None:
                os.environ["ORCA_CLI_COMMAND"] = previous_command
            if previous_dev is not None:
                os.environ["ORCA_DEV_REPO_ROOT"] = previous_dev

    def test_auto_backend_prefers_herdr_inside_herdr(self):
        original_report = control.capability_report
        original_herdr = os.environ.get("HERDR_ENV")
        original_orca = os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        original_orca_pane = os.environ.pop("ORCA_PANE_KEY", None)
        control.capability_report = lambda: {"orca": {"available": True}, "herdr": {"available": True}}
        os.environ["HERDR_ENV"] = "1"
        try:
            self.assertEqual(control.discover_backend("auto"), "herdr")
        finally:
            control.capability_report = original_report
            if original_herdr is None:
                os.environ.pop("HERDR_ENV", None)
            else:
                os.environ["HERDR_ENV"] = original_herdr
            if original_orca is not None:
                os.environ["ORCA_TERMINAL_HANDLE"] = original_orca
            if original_orca_pane is not None:
                os.environ["ORCA_PANE_KEY"] = original_orca_pane

    def test_list_filters_current_repository_and_sorts_newest_first(self):
        older = {"id": "older", "created_at": "2020-01-01T00:00:00+00:00", "repository": {"path": "/repo/current"}}
        newer = {"id": "newer", "created_at": "2024-06-01T00:00:00+00:00", "repository": {"path": "/repo/current"}}
        other = {"id": "other", "created_at": "2025-01-01T00:00:00+00:00", "repository": {"path": "/repo/other"}}
        workers = [older, other, newer]
        current = control.workers_for_list(workers, "/repo/current")
        self.assertEqual([worker["id"] for worker in current], ["newer", "older"])
        everything = control.workers_for_list(workers, None)
        self.assertEqual([worker["id"] for worker in everything], ["other", "newer", "older"])

    def test_recommend_help_lists_profile_factors_and_rejects_medium(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "worker.py"
        spec = importlib.util.spec_from_file_location("brigade_worker_cli", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        buffer = io.StringIO()
        with mock.patch("sys.argv", ["brigade", "worker", "recommend", "--help"]):
            with mock.patch("sys.stdout", buffer):
                with self.assertRaises(SystemExit) as raised:
                    cli.parser().parse_args()
        self.assertEqual(raised.exception.code, 0)
        help_text = buffer.getvalue()
        choices = "{low,moderate,high,straightforward,hard}"
        self.assertEqual(help_text.count(choices), 8)
        self.assertNotIn("medium", help_text)
        self.assertEqual(control.choose_profile("STANDARD", "moderate", "moderate", "moderate", "moderate")["name"], "codex-terra-high")
        with self.assertRaisesRegex(control.WorkerError, "must be"):
            control.choose_profile("STANDARD", "medium", "low", "straightforward", "low")

    def test_cursor_warning_only_for_new_cursor_worktree(self):
        message = control.cursor_new_worktree_warning("cursor", "new")
        self.assertIn("trusts that new folder", message)
        self.assertIsNone(control.cursor_new_worktree_warning("cursor", "current"))
        self.assertIsNone(control.cursor_new_worktree_warning("cursor", "/repo/existing"))
        self.assertIsNone(control.cursor_new_worktree_warning("codex", "new"))

    def follow_up_worker(self, backend, status="completed", path="/repo/task"):
        return {
            "id": "w_follow", "status": status, "task": {"title": "Fix the gate"},
            "configuration": {"backend": backend},
            "worktree": {"path": path, "kind": "current"},
            "backend_session": {"dispatch_id": "dispatch_old", "task_id": "task_old", "terminal_id": "term_1", "run_id": "run_1", "agent_name": "w-fix"},
            "events": [], "updated_at": "now",
        }

    def test_herdr_follow_up_prompts_completed_agent_and_reclaims_worktree(self):
        worker = self.follow_up_worker("herdr")
        state = {"version": 1, "workers": [worker]}
        saved = []
        backend = control.HerdrBackend()
        calls = []
        backend.call = lambda arguments, _description: calls.append(arguments) or {"ok": True}
        original = control.backend_for
        control.backend_for = lambda _name: backend
        try:
            detail = control.deliver_follow_up(state, worker, "check the other caller", lambda: saved.append(worker["status"]))
        finally:
            control.backend_for = original
        self.assertEqual(calls, [["agent", "prompt", "w-fix", "check the other caller"]])
        self.assertEqual(worker["status"], "active")
        self.assertEqual(saved, ["active"])
        self.assertEqual(detail, "Follow-up submitted to the Herdr agent.")
        with self.assertRaisesRegex(control.WorkerError, "w_follow"):
            control.assert_available_worktree(state, "/repo/task")

    def test_follow_up_refuses_released_failed_and_occupied_workers(self):
        released = self.follow_up_worker("herdr")
        released["events"] = [{"kind": "released"}]
        with self.assertRaisesRegex(control.WorkerError, "released"):
            control.deliver_follow_up({"version": 1, "workers": [released]}, released, "again")
        failed = self.follow_up_worker("herdr", status="failed")
        with self.assertRaisesRegex(control.WorkerError, "failed"):
            control.deliver_follow_up({"version": 1, "workers": [failed]}, failed, "again")
        completed = self.follow_up_worker("herdr")
        other = self.worker(worker_id="w_other", path="/repo/task", status="active")
        with self.assertRaisesRegex(control.WorkerError, "w_other"):
            control.deliver_follow_up({"version": 1, "workers": [completed, other]}, completed, "again")
        self.assertEqual(completed["status"], "completed")

    def test_herdr_follow_up_failure_returns_the_worker_to_completed(self):
        worker = self.follow_up_worker("herdr")
        state = {"version": 1, "workers": [worker]}
        saved = []
        backend = control.HerdrBackend()

        def fail(arguments, _description):
            raise control.WorkerError("agent_blocked")

        backend.call = fail
        original = control.backend_for
        control.backend_for = lambda _name: backend
        try:
            with self.assertRaisesRegex(control.WorkerError, "agent_blocked"):
                control.deliver_follow_up(state, worker, "again", lambda: saved.append(worker["status"]))
        finally:
            control.backend_for = original
        self.assertEqual(worker["status"], "completed")
        self.assertEqual(saved, ["active", "completed"])

    def test_orca_completed_follow_up_is_refused(self):
        worker = self.follow_up_worker("orca")
        with self.assertRaisesRegex(control.WorkerError, "completed Orca"):
            control.deliver_follow_up({"version": 1, "workers": [worker]}, worker, "again")
        self.assertEqual(worker["status"], "completed")

    def test_orca_active_follow_up_stays_on_the_open_dispatch(self):
        worker = self.follow_up_worker("orca", status="active")
        backend = control.OrcaBackend()
        calls = []
        backend.call = lambda arguments, _description: calls.append(arguments) or {"ok": True}
        original = control.backend_for
        control.backend_for = lambda _name: backend
        try:
            control.deliver_follow_up({"version": 1, "workers": [worker]}, worker, "steer this attempt")
        finally:
            control.backend_for = original
        self.assertEqual(calls, [[
            "orchestration", "send", "--to", "dispatch:dispatch_old", "--subject", "Brigade follow-up",
            "--body", "steer this attempt", "--type", "status",
        ]])
        self.assertEqual(worker["backend_session"]["dispatch_id"], "dispatch_old")


if __name__ == "__main__":
    unittest.main()
