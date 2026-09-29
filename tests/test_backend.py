import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "rclone-mounts-tasks@psark007.github.io/backend.py"
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
        self.config.start()
        self.state.start()
        self.addCleanup(self.config.stop)
        self.addCleanup(self.state.stop)

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

    def test_other_running_task_service_blocks_new_task(self):
        backend.save_task({"name": "Test", "action": "copy", "direction": "download",
                           "source": "sample:folder", "destinations": ["/tmp/local"]})
        item = backend.read_list("tasks")[0]
        with mock.patch.object(backend, "active_tasks", return_value=["rclone-task-external.service"]), \
                mock.patch.object(backend, "command", side_effect=AssertionError("should not start")):
            with self.assertRaises(backend.UserError):
                backend.start_task(item["id"])

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


if __name__ == "__main__":
    unittest.main()
