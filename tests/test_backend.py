import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "rclone-manager@psark007.github.io/backend.py"
spec = importlib.util.spec_from_file_location("rclone_backend", SOURCE)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = mock.patch.object(backend, "CONFIG", root / "config")
        self.state = mock.patch.object(backend, "STATE", root / "state")
        self.cache = mock.patch.object(backend, "CACHE", root / "cache" / "mounts")
        self.config.start()
        self.state.start()
        self.cache.start()
        self.addCleanup(self.config.stop)
        self.addCleanup(self.state.stop)
        self.addCleanup(self.cache.stop)

    def test_fresh_install_does_not_make_config_or_run_commands(self):
        with mock.patch.object(backend, "active_tasks", return_value=[]), \
                mock.patch.object(backend, "command", side_effect=AssertionError("should not execute")):
            self.assertEqual(backend.snapshot(), {"mounts": [], "tasks": [], "activeTaskUnits": []})
        self.assertFalse(backend.CONFIG.exists())
        self.assertFalse(backend.STATE.exists())

    def test_mount_definition_is_private_and_does_not_mount(self):
        folder = Path(self.temp.name) / "mount"
        folder.mkdir()
        with mock.patch.object(backend, "command", side_effect=AssertionError("should not mount")):
            result = backend.save_mount({"label": "Test", "remote": "sample:folder", "path": str(folder)})
        self.assertEqual(result["message"], "Mount saved")
        entry = backend.read_list("mounts")[0]
        self.assertEqual(entry["remote"], "sample:folder")
        self.assertEqual(entry["port"], 5577)
        self.assertEqual(backend.CONFIG.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backend.CONFIG / "mounts.json").stat().st_mode & 0o777, 0o600)
        with mock.patch.object(backend, "active", return_value=False), \
                mock.patch.object(backend, "mounted", return_value=False), \
                mock.patch.object(backend, "command") as run, \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.operate_mount(entry["id"], "mount")
        args = run.call_args.args[0]
        self.assertEqual(args[0], "systemd-run")
        self.assertIn("--rc-addr", args)
        self.assertIn("127.0.0.1:5577", args)

    def test_edit_and_remove_refuse_active_mount(self):
        backend.save_mount({"label": "Test", "remote": "sample:folder", "path": "/tmp/mount-one"})
        entry = backend.read_list("mounts")[0]
        with mock.patch.object(backend, "active", return_value=True):
            with self.assertRaises(backend.UserError):
                backend.save_mount({"id": entry["id"], "label": "New", "remote": "sample:folder", "path": "/tmp/mount-two"})
            with self.assertRaises(backend.UserError):
                backend.delete_mount(entry["id"])
        self.assertEqual(len(backend.read_list("mounts")), 1)

    def test_sync_and_reverse_are_explicit_and_sequential(self):
        source = Path(self.temp.name) / "source"
        source.mkdir()
        result = backend.save_task({"name": "Test", "action": "sync", "direction": "upload",
                                    "source": str(source), "destinations": ["sample:first", "sample:second"]})
        self.assertEqual(result["message"], "Task saved")
        item = backend.read_list("tasks")[0]
        with self.assertRaises(backend.UserError):
            backend.start_task(item["id"], reverse=True)
        commands = []

        def fake_run(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend.subprocess, "run", side_effect=fake_run):
            self.assertEqual(backend.worker(item["id"], reverse=False), 0)
        self.assertEqual([(a[2], a[3]) for a in commands], [(str(source), "sample:first"), (str(source), "sample:second")])
        for args in commands:
            self.assertEqual(args[args.index("--transfers") + 1], "8")
            self.assertEqual(args[args.index("--checkers") + 1], "16")
        self.assertEqual(backend.read_state()[item["id"]]["status"], "Completed")
        self.assertEqual((backend.STATE / "runs.json").stat().st_mode & 0o777, 0o600)

    def test_failure_skips_remaining_destinations(self):
        source = Path(self.temp.name) / "source"
        source.mkdir()
        backend.save_task({"name": "Test", "action": "copy", "direction": "upload",
                           "source": str(source), "destinations": ["sample:first", "sample:second"]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertEqual(backend.worker(item["id"], reverse=False), 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(backend.read_state()[item["id"]]["status"], "Failed")

    def test_other_running_task_service_does_not_block_new_task(self):
        for name in ("Other", "Test"):
            backend.save_task({"name": name, "action": "copy", "direction": "download",
                               "source": "sample:folder", "destinations": [f"/tmp/{name}"]})
        other, item = backend.read_list("tasks")
        with mock.patch.object(backend, "active_tasks", return_value=[backend.task_unit(other),
                                                                       "rclone-task-external.service"]), \
                mock.patch.object(backend, "command") as run, \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.start_task(item["id"])
        self.assertEqual(run.call_args.args[0][0], "systemd-run")
        self.assertEqual(backend.read_state()[item["id"]]["status"], "Starting")

    def test_same_task_cannot_start_twice(self):
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/local"]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "active_tasks", return_value=[backend.task_unit(item)]), \
                mock.patch.object(backend, "command", side_effect=AssertionError("should not start")):
            with self.assertRaisesRegex(backend.UserError, "already running"):
                backend.start_task(item["id"])

    def test_starting_task_is_not_overwritten_before_unit_appears(self):
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/local"]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "active_tasks", return_value=[]), \
                mock.patch.object(backend, "command") as command, \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.start_task(item["id"])
            with self.assertRaisesRegex(backend.UserError, "already starting"):
                backend.start_task(item["id"])
            self.assertEqual(backend.snapshot()["tasks"][0]["status"], "Starting")
        self.assertEqual(command.call_count, 1)
        self.assertEqual(backend.read_state()[item["id"]]["history"], [])

    def test_task_history_records_results_and_survives_new_run(self):
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/one", "/tmp/two"]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "active_tasks", return_value=[]), \
                mock.patch.object(backend, "command"), \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.start_task(item["id"])
            with mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                self.assertEqual(backend.worker(item["id"], False), 0)
            completed = backend.read_state()[item["id"]]["history"][0]
            self.assertEqual((completed["status"], completed["destination"], completed["total"]),
                             ("Completed", 2, 2))
            self.assertTrue(completed["startedAt"])
            self.assertTrue(completed["finishedAt"])
            backend.start_task(item["id"])
            state = backend.read_state()[item["id"]]
            self.assertEqual(state["status"], "Starting")
            self.assertEqual(state["history"], [completed])
            with mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)):
                self.assertEqual(backend.worker(item["id"], False), 1)
        history = backend.read_state()[item["id"]]["history"]
        self.assertEqual([entry["status"] for entry in history], ["Failed", "Completed"])
        self.assertEqual(history[0]["exitCode"], 7)
        self.assertEqual((history[0]["destination"], history[0]["total"]), (1, 2))
        with mock.patch.object(backend, "active_tasks", return_value=[]):
            self.assertEqual(backend.snapshot()["tasks"][0]["history"], history)

    def test_task_history_is_bounded_and_records_stop_and_reverse(self):
        local = Path(self.temp.name) / "one"
        local.mkdir()
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": [str(local)]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "command"), \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            for index in range(7):
                with mock.patch.object(backend, "active_tasks", return_value=[]):
                    backend.start_task(item["id"], reverse=index == 0)
                if index == 1:
                    with mock.patch.object(backend, "active_tasks", return_value=[backend.task_unit(item)]):
                        backend.stop_task(item["id"])
                else:
                    with mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                        self.assertEqual(backend.worker(item["id"], index == 0), 0)
                if index == 0:
                    self.assertTrue(backend.read_state()[item["id"]]["history"][0]["reverse"])
                if index == 1:
                    self.assertEqual(backend.read_state()[item["id"]]["history"][0]["status"], "Stopped")
        history = backend.read_state()[item["id"]]["history"]
        self.assertEqual(len(history), backend.TASK_HISTORY_LIMIT)
        self.assertEqual(history[0]["status"], "Completed")
        self.assertEqual(len({entry["runId"] for entry in history}), len(history))
        self.assertEqual((backend.STATE / "runs.json").stat().st_mode & 0o777, 0o600)

    def test_history_records_interruption_and_legacy_latest_result(self):
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/one"]})
        item = backend.read_list("tasks")[0]
        backend.updated_run(item["id"], status="Completed", startedAt="2026-10-01T10:00:00+00:00",
                            finishedAt="2026-10-01T10:05:00+00:00", exitCode=0)
        with mock.patch.object(backend, "active_tasks", return_value=[]):
            legacy = backend.snapshot()["tasks"][0]["history"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["status"], "Completed")
        with mock.patch.object(backend, "active_tasks", return_value=[]), mock.patch.object(backend, "command"), \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.start_task(item["id"])
            self.assertEqual(len(backend.read_state()[item["id"]]["history"]), 1)
            backend.updated_run(item["id"], status="Running", destination=1)
            interrupted = backend.snapshot()["tasks"][0]
            self.assertEqual(interrupted["status"], "Interrupted")
            self.assertEqual(interrupted["history"][0]["status"], "Interrupted")
            self.assertFalse(interrupted["history"][0]["finishedAt"])
            backend.start_task(item["id"])
        history = backend.read_state()[item["id"]]["history"]
        self.assertEqual([entry["status"] for entry in history], ["Interrupted", "Completed"])
        self.assertFalse(history[0]["finishedAt"])

    def test_service_launch_failure_is_in_history(self):
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/one"]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "active_tasks", return_value=[]), \
                mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend, "command", side_effect=backend.UserError("Launch failed")):
            with self.assertRaises(backend.UserError):
                backend.start_task(item["id"])
        run = backend.read_state()[item["id"]]
        self.assertEqual(run["history"][0]["status"], "Failed")
        self.assertEqual(run["history"][0]["failure"], "Could not start user service")
        self.assertTrue(run["history"][0]["finishedAt"])

    def test_concurrent_workers_keep_independent_progress_and_history(self):
        for name in ("One", "Two"):
            backend.save_task({"name": name, "action": "copy", "direction": "download",
                               "source": "sample:folder", "destinations": [f"/tmp/{name}"]})
        first, second = backend.read_list("tasks")
        self.assertNotEqual(first["port"], second["port"])
        barrier = threading.Barrier(2, timeout=5)
        commands = []

        def fake_run(args, **kwargs):
            commands.append(args)
            barrier.wait()
            return subprocess.CompletedProcess(args, 0)

        results = []
        with mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend.subprocess, "run", side_effect=fake_run):
            threads = [threading.Thread(target=lambda task=item: results.append(backend.worker(task["id"], False)))
                       for item in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results, [0, 0])
        self.assertEqual({args[-1] for args in commands},
                         {f"127.0.0.1:{first['port']}", f"127.0.0.1:{second['port']}"})
        self.assertEqual({run["status"] for run in backend.read_state().values()}, {"Completed"})
        self.assertTrue(all(run["history"][0]["status"] == "Completed"
                            for run in backend.read_state().values()))

    def test_legacy_task_gets_port_on_first_run_and_mounts_avoid_task_ports(self):
        backend.save_task({"name": "Old", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/local"]})
        task = backend.read_list("tasks")[0]
        del task["port"]
        backend.write_json(backend.CONFIG / "tasks.json", [task])
        backend.save_mount({"label": "Mount", "remote": "sample:mount", "path": "/tmp/mount"})
        with mock.patch.object(backend, "active_tasks", return_value=[]), \
                mock.patch.object(backend, "command"), \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.start_task(task["id"])
        task = backend.read_list("tasks")[0]
        self.assertNotEqual(task["port"], backend.read_list("mounts")[0]["port"])
        backend.save_mount({"label": "Other", "remote": "sample:other", "path": "/tmp/other"})
        self.assertNotIn(task["port"], [m["port"] for m in backend.read_list("mounts")])

    def test_snapshot_uses_each_tasks_progress_port(self):
        for name in ("One", "Two"):
            backend.save_task({"name": name, "action": "copy", "direction": "download",
                               "source": "sample:folder", "destinations": [f"/tmp/{name}"]})
        tasks = backend.read_list("tasks")
        for item in tasks:
            backend.updated_run(item["id"], status="Running")
        with mock.patch.object(backend, "active_tasks", return_value=[backend.task_unit(t) for t in tasks]), \
                mock.patch.object(backend, "stats", side_effect=lambda port: {"port": port}):
            snapshot = backend.snapshot()
        self.assertEqual([t["stats"]["port"] for t in snapshot["tasks"]], [t["port"] for t in tasks])

    def test_reverse_swaps_one_destination_without_editing_definition(self):
        source = Path(self.temp.name) / "source"
        source.mkdir()
        backend.save_task({"name": "Test", "action": "copy", "direction": "upload",
                           "source": str(source), "destinations": ["sample:one"]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(backend.worker(item["id"], reverse=True), 0)
        self.assertEqual(run.call_args.args[0][2:4], ["sample:one", str(source)])
        self.assertEqual(backend.read_list("tasks")[0]["source"], str(source))

    def test_new_mount_defaults_and_crypt_cache_isolation(self):
        for name in ("pCloudCrypt", "FilenCrypt"):
            backend.save_mount({"label": name, "remote": f"{name}:folder", "path": f"/tmp/{name}"})
        first, second = backend.read_list("mounts")
        self.assertEqual(first["cache"], backend.RECOMMENDED_CACHE)
        self.assertNotEqual(first["cacheDir"], second["cacheDir"])
        self.assertEqual(first["cacheDir"], str(backend.CACHE / first["id"]))
        for mount in (first, second):
            with mock.patch.object(backend, "active", return_value=False), \
                    mock.patch.object(backend, "mounted", return_value=False), \
                    mock.patch.object(backend, "logging_enabled", return_value=False), \
                    mock.patch.object(backend, "command") as command, \
                    mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                    mock.patch.object(backend.Path, "is_dir", return_value=True), \
                    mock.patch.object(backend.Path, "iterdir", return_value=iter(())):
                backend.operate_mount(mount["id"], "mount")
            args = command.call_args.args[0]
            self.assertEqual(args[args.index("mount") + 1], mount["remote"])
            for flag, value in (("--vfs-cache-mode", "full"), ("--vfs-cache-max-size", "5G"),
                                ("--vfs-cache-max-age", "24h"), ("--vfs-cache-min-free-space", "10G"),
                                ("--cache-dir", mount["cacheDir"])):
                self.assertEqual(args[args.index(flag) + 1], value)
            self.assertEqual(args[args.index("--transfers") + 1], "8")
            self.assertEqual(args[args.index("--checkers") + 1], "16")
            self.assertEqual(Path(mount["cacheDir"]).stat().st_mode & 0o777, 0o700)

    def test_legacy_mount_keeps_inherited_flags_and_cache_location_after_edit(self):
        backend.write_json(backend.CONFIG / "mounts.json", [{"id": "a" * 32, "label": "Old",
                           "remote": "pcloud:folder", "path": "/tmp/old", "port": 5577}])
        item = backend.read_list("mounts")[0]
        with mock.patch.object(backend, "tool", side_effect=lambda name: name):
            args = backend.mount_args(item)
        self.assertEqual(args[args.index("--vfs-cache-mode") + 1], "writes")
        self.assertNotIn("--cache-dir", args)
        self.assertNotIn("--vfs-cache-max-size", args)
        self.assertNotIn("--vfs-cache-max-age", args)
        with mock.patch.object(backend, "active", return_value=False), mock.patch.object(backend, "mounted", return_value=False):
            backend.save_mount({"id": item["id"], "label": "Old renamed", "remote": item["remote"], "path": item["path"]})
            self.assertNotIn("cache", backend.read_list("mounts")[0])
            backend.save_mount({"id": item["id"], "label": "Old", "remote": item["remote"], "path": item["path"],
                                "cache": backend.RECOMMENDED_CACHE})
            self.assertNotIn("cacheDir", backend.read_list("mounts")[0])
            with self.assertRaisesRegex(backend.UserError, "strand"):
                backend.save_mount({"id": item["id"], "label": "Old", "remote": "other:path", "path": item["path"]})
        with mock.patch.object(backend, "tool", side_effect=lambda name: name):
            updated = backend.mount_args(backend.read_list("mounts")[0])
        self.assertIn("--vfs-cache-max-size", updated)
        self.assertNotIn("--cache-dir", updated)

    def test_cache_validation_and_explicit_no_limit(self):
        payload = {"label": "Test", "remote": "filen:folder", "path": "/tmp/test"}
        for field, invalid in (("mode", "off"), ("maxSize", "-2G"), ("maxAge", "0h"),
                               ("minFreeSpace", "--rc"), ("maxSize", "999999999999T"),
                               ("maxAge", "999999999999w")):
            with self.subTest(field=field):
                with self.assertRaises(backend.UserError):
                    backend.save_mount({**payload, "cache": {**backend.RECOMMENDED_CACHE, field: invalid}})
        self.assertEqual(backend.read_list("mounts"), [])
        backend.save_mount({**payload, "cache": {**backend.RECOMMENDED_CACHE, "maxSize": "off"}})
        with mock.patch.object(backend, "tool", side_effect=lambda name: name):
            args = backend.mount_args(backend.read_list("mounts")[0])
        self.assertEqual(args[args.index("--vfs-cache-max-size") + 1], "off")

    def test_deleting_mount_preserves_cache(self):
        backend.save_mount({"label": "Test", "remote": "filen:folder", "path": "/tmp/test"})
        mount = backend.read_list("mounts")[0]
        path = Path(mount["cacheDir"])
        path.mkdir(parents=True)
        (path / "pending").write_text("local only")
        with mock.patch.object(backend, "active", return_value=False), mock.patch.object(backend, "mounted", return_value=False):
            backend.delete_mount(mount["id"])
        self.assertEqual((path / "pending").read_text(), "local only")

    def test_logging_opt_in_private_bounded_and_failure_exit_code(self):
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/one", "/tmp/two"]})
        task = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(backend.worker(task["id"], False), 1)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse((backend.STATE / "logs").exists())
        self.assertEqual(backend.read_state()[task["id"]]["exitCode"], 7)
        backend.save_diagnostics({"logging": True})

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(b"error: path private\n" * 90000)
            def wait(self):
                return 9

        with mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend.subprocess, "Popen", side_effect=FakeProcess) as popen:
            self.assertEqual(backend.worker(task["id"], False), 1)
        self.assertEqual(popen.call_count, 1)
        logged_args = popen.call_args.args[0]
        self.assertEqual(logged_args[logged_args.index("--transfers") + 1], "8")
        self.assertEqual(logged_args[logged_args.index("--checkers") + 1], "16")
        log = Path(backend.read_state()[task["id"]]["log"])
        self.assertEqual(backend.read_state()[task["id"]]["exitCode"], 9)
        self.assertLessEqual(log.stat().st_size, backend.LOG_LIMIT)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)
        for _ in range(20):
            file = backend.log_path(task["id"], "task")
            file.write_bytes(b"x" * backend.LOG_LIMIT)
        backend.prune_logs()
        self.assertLessEqual(sum(p.stat().st_size for p in log.parent.glob("*.log")), backend.LOG_TOTAL)

    def test_mount_logging_is_opt_in_and_uses_private_log_without_changing_flags(self):
        backend.save_mount({"label": "Test", "remote": "filen:folder", "path": "/tmp/test"})
        item = backend.read_list("mounts")[0]
        backend.save_diagnostics({"logging": True})
        with mock.patch.object(backend, "active", return_value=False), \
                mock.patch.object(backend, "mounted", return_value=False), \
                mock.patch.object(backend.Path, "is_dir", return_value=True), \
                mock.patch.object(backend.Path, "iterdir", return_value=iter(())), \
                mock.patch.object(backend, "command") as command, \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.operate_mount(item["id"], "mount")
        args = command.call_args.args[0]
        self.assertIn("mount-worker", args)
        self.assertIn("--property=UMask=0077", args)
        with mock.patch.object(backend, "tool", side_effect=lambda name: name), \
                mock.patch.object(backend, "stream_logged", return_value=(0, "log")) as stream:
            self.assertEqual(backend.mount_worker(item["id"]), 0)
        mount_args = stream.call_args.args[0]
        self.assertEqual(mount_args[mount_args.index("--transfers") + 1], "8")
        self.assertEqual(mount_args[mount_args.index("--checkers") + 1], "16")
        self.assertEqual(stream.call_args.args[2], "mount")

    def test_unmount_requires_clear_vfs_twice_and_never_stops_on_failure(self):
        backend.save_mount({"label": "Test", "remote": "sample:path", "path": "/tmp/test"})
        item = backend.read_list("mounts")[0]
        for state in ("pending", "failed", "unknown"):
            with self.subTest(state=state), mock.patch.object(backend, "mounted", return_value=True), \
                    mock.patch.object(backend, "vfs_status", return_value={"state": state}), \
                    mock.patch.object(backend, "command", side_effect=AssertionError("should not unmount")):
                with self.assertRaisesRegex(backend.UserError, "blocked"):
                    backend.operate_mount(item["id"], "unmount")
        with mock.patch.object(backend, "mounted", side_effect=[True, False]), \
                mock.patch.object(backend, "vfs_status", return_value={"state": "clear"}) as vfs, \
                mock.patch.object(backend.time, "sleep"), \
                mock.patch.object(backend, "command") as commands, \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            backend.operate_mount(item["id"], "unmount")
        self.assertEqual(vfs.call_count, 2)
        self.assertEqual([call.args[0][0] for call in commands.call_args_list], ["fusermount3", "systemctl"])
        with mock.patch.object(backend, "mounted", return_value=True), \
                mock.patch.object(backend, "vfs_status", side_effect=[{"state": "clear"}, {"state": "pending"}]), \
                mock.patch.object(backend.time, "sleep"), \
                mock.patch.object(backend, "command", side_effect=AssertionError("should not detach")):
            with self.assertRaisesRegex(backend.UserError, "blocked"):
                backend.operate_mount(item["id"], "unmount")
        with mock.patch.object(backend, "mounted", return_value=True), \
                mock.patch.object(backend, "vfs_status", return_value={"state": "clear"}), \
                mock.patch.object(backend.time, "sleep"), \
                mock.patch.object(backend, "command", side_effect=backend.UserError("busy")) as commands, \
                mock.patch.object(backend, "tool", side_effect=lambda name: name):
            with self.assertRaises(backend.UserError):
                backend.operate_mount(item["id"], "unmount")
        self.assertEqual(commands.call_count, 1)

    def test_vfs_queue_stats_retries_errors_and_unavailable_rc(self):
        disk = {"uploadsQueued": 1, "uploadsInProgress": 1, "erroredFiles": 0}
        queue = [{"uploading": True, "tries": 2}, {"uploading": False, "tries": 0}]
        with mock.patch.object(backend, "rc_call", side_effect=[{"diskCache": disk}, {"queue": queue}]):
            state = backend.vfs_status(5577)
        self.assertEqual((state["state"], state["queued"], state["uploading"], state["retried"]),
                         ("pending", 1, 1, 1))
        with mock.patch.object(backend, "rc_call", side_effect=[{"diskCache": {**disk, "erroredFiles": 2}}, {"queue": queue}]):
            self.assertEqual(backend.vfs_status(5577)["state"], "failed")
        with mock.patch.object(backend, "rc_call", side_effect=[{"diskCache": {"uploadsQueued": 0,
                    "uploadsInProgress": 0, "erroredFiles": 0}}, {"queue": []}]):
            self.assertEqual(backend.vfs_status(5577)["state"], "clear")
        with mock.patch.object(backend, "rc_call", side_effect=[{"diskCache": {"uploadsQueued": 0}}, {"queue": []}]):
            self.assertEqual(backend.vfs_status(5577)["state"], "unknown")
        with mock.patch.object(backend, "rc_call", return_value=None):
            self.assertEqual(backend.vfs_status(5577)["state"], "unknown")
            self.assertIsNone(backend.stats(5577))

    def test_diagnostics_summary_marks_unavailable_stats(self):
        backend.save_mount({"label": "Test", "remote": "sample:path", "path": "/tmp/test"})
        backend.save_task({"name": "Copy", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/local"]})
        with mock.patch.object(backend, "active_tasks", return_value=[]), \
                mock.patch.object(backend, "mounted", return_value=True), \
                mock.patch.object(backend, "stats", return_value=None), \
                mock.patch.object(backend, "vfs_status", return_value={"state": "unknown"}), \
                mock.patch.object(backend, "rclone_version", return_value="rclone v1.75.0"):
            summary = backend.diagnostics()
        self.assertEqual(summary["version"], "rclone v1.75.0")
        self.assertIsNone(summary["mounts"][0]["stats"])
        self.assertEqual(summary["mounts"][0]["uploads"]["state"], "unknown")
        self.assertEqual((summary["tasks"][0]["transfers"], summary["tasks"][0]["checkers"]), (8, 16))

    def test_rc_post_has_bounded_response_and_handles_unavailable_endpoint(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, length):
                self.length = length
                return b"x" * length

        response = Response()
        with mock.patch.object(backend.RC_OPENER, "open", return_value=response) as call:
            self.assertIsNone(backend.rc_call(5577, "vfs/queue"))
        self.assertEqual(response.length, 1024 * 1024 + 1)
        self.assertEqual(call.call_args.args[0].get_method(), "POST")
        with mock.patch.object(backend.RC_OPENER, "open", side_effect=urllib.error.URLError("offline")):
            self.assertIsNone(backend.rc_call(5577, "vfs/stats"))


if __name__ == "__main__":
    unittest.main()
