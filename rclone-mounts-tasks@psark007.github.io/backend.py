#!/usr/bin/env python3
"""Manual rclone controls. No remote discovery, imports, or scheduled work."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gnome-shell-rclone"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "gnome-shell-rclone"
TASK_PORT = 5576


class UserError(Exception):
    pass


def tool(name):
    executable = shutil.which(name)
    if not executable:
        raise UserError(f"Required tool is missing: {name}")
    return executable


def command(args, timeout=15, check=True):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError("Command unavailable or timed out; check user service logs") from exc
    if check and result.returncode:
        # rclone and systemd errors can contain private paths. Do not return their output to Shell.
        raise UserError("Command failed; check the user service journal")
    return result


def read_list(name):
    path = CONFIG / f"{name}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            raise ValueError("not a list")
        return data
    except (OSError, ValueError) as exc:
        raise UserError(f"Invalid {name} definitions; refusing changes") from exc


def read_state():
    path = STATE / "runs.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (OSError, ValueError) as exc:
        raise UserError("Invalid task history; refusing changes") from exc


def write_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def updated_run(task_id, **fields):
    runs = read_state()
    runs[task_id] = {**runs.get(task_id, {}), **fields}
    write_json(STATE / "runs.json", runs)


def local_path(value):
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute() or str(path) == "/":
        raise UserError("Local path must be absolute (or ~/...) and not the filesystem root")
    return str(path)


def remote_path(value):
    value = str(value).strip()
    if not value or value.startswith(":") or ":" not in value or value.startswith("/"):
        raise UserError("Enter an explicit rclone remote:path")
    return value


def mount_unit(item):
    return f"gnome-rclone-mount-{item['id']}.service"


def task_unit(item):
    return f"gnome-rclone-task-{item['id']}.service"


def mounted(item):
    return command([tool("mountpoint"), "-q", item["path"]], check=False, timeout=4).returncode == 0


def active(unit):
    return command([tool("systemctl"), "--user", "is-active", "--quiet", unit], check=False, timeout=4).returncode == 0


def active_tasks():
    result = command([tool("systemctl"), "--user", "list-units", "--no-legend", "--plain",
                      "--type=service", "--state=active,activating", "gnome-rclone-task-*.service",
                      "rclone-task-*.service"], timeout=5)
    return [line.split()[0] for line in result.stdout.splitlines()
            if line.startswith(("gnome-rclone-task-", "rclone-task-"))]


def guard_mount(item, new_path=None):
    if active(mount_unit(item)) or mounted(item):
        raise UserError("Unmount this entry before changing or removing it")
    if new_path and new_path != item["path"] and command(
            [tool("mountpoint"), "-q", new_path], check=False, timeout=4).returncode == 0:
        raise UserError("The new mountpoint is already in use")


def save_mount(payload):
    mounts = read_list("mounts")
    label = str(payload.get("label", "")).strip()
    remote = remote_path(payload.get("remote", ""))
    path = local_path(payload.get("path", ""))
    if not label:
        raise UserError("Enter a display name")
    old = next((m for m in mounts if m["id"] == payload.get("id")), None)
    if payload.get("id") and not old:
        raise UserError("Mount entry not found")
    if any(m["id"] != payload.get("id") and (m["remote"] == remote or m["path"] == path) for m in mounts):
        raise UserError("That remote or mountpoint is already configured")
    if old:
        guard_mount(old, path)
        entry = {**old, "label": label, "remote": remote, "path": path}
        mounts = [entry if m["id"] == old["id"] else m for m in mounts]
    else:
        used_ports = {m["port"] for m in mounts}
        port = next(p for p in range(5577, 65536) if p not in used_ports)
        entry = {"id": uuid4().hex, "label": label, "remote": remote, "path": path, "port": port}
        mounts.append(entry)
    write_json(CONFIG / "mounts.json", mounts)
    return {"message": "Mount saved"}


def delete_mount(item_id):
    mounts = read_list("mounts")
    item = next((m for m in mounts if m["id"] == item_id), None)
    if not item:
        raise UserError("Mount entry not found")
    guard_mount(item)
    write_json(CONFIG / "mounts.json", [m for m in mounts if m["id"] != item_id])
    return {"message": "Saved entry removed; no remote files were touched"}


def operate_mount(item_id, action):
    item = next((m for m in read_list("mounts") if m["id"] == item_id), None)
    if not item:
        raise UserError("Mount entry not found")
    if action == "mount":
        if active(mount_unit(item)) or mounted(item):
            raise UserError("Mount is already running or this path is occupied")
        path = Path(item["path"])
        if not path.is_dir() or any(path.iterdir()):
            raise UserError("Mountpoint must be an existing empty directory")
        command([tool("systemd-run"), "--user", f"--unit={mount_unit(item)[:-8]}", "--collect",
                 tool("rclone"), "mount", item["remote"], item["path"], "--transfers", "8",
                 "--checkers", "16", "--vfs-cache-mode", "writes", "--rc",
                 "--rc-addr", f"127.0.0.1:{item['port']}"])
        return {"message": "Mount starting; check status in a few seconds"}
    if action == "unmount":
        if not mounted(item):
            raise UserError("Mountpoint is not mounted")
        command([tool("fusermount3"), "-u", item["path"]])
        command([tool("systemctl"), "--user", "stop", mount_unit(item)])
        return {"message": "Unmounted"}
    if action == "open":
        if not mounted(item):
            raise UserError("Mountpoint is not mounted")
        command([tool("gio"), "open", Path(item["path"]).as_uri()], timeout=8)
        return {"message": "Opening folder"}
    raise UserError("Unknown mount operation")


def validate_task(payload):
    name = str(payload.get("name", "")).strip()
    action = payload.get("action")
    direction = payload.get("direction")
    destinations = payload.get("destinations")
    if not name or action not in ("copy", "sync") or direction not in ("upload", "download"):
        raise UserError("Enter a task name, direction, and copy or sync action")
    if not isinstance(destinations, list) or not destinations or any(not isinstance(d, str) for d in destinations):
        raise UserError("Enter at least one destination")
    local = local_path if direction == "upload" else remote_path
    target = remote_path if direction == "upload" else local_path
    source = local(payload.get("source", ""))
    destinations = [target(d) for d in destinations]
    if len(set(destinations)) != len(destinations):
        raise UserError("Duplicate destinations are not allowed")
    return {"name": name, "source": source, "destinations": destinations,
            "direction": direction, "action": action}


def save_task(payload):
    tasks = read_list("tasks")
    data = validate_task(payload)
    old = next((t for t in tasks if t["id"] == payload.get("id")), None)
    if payload.get("id") and not old:
        raise UserError("Task not found")
    if old and task_unit(old) in active_tasks():
        raise UserError("Stop the task before editing it")
    item = {"id": old["id"] if old else uuid4().hex, **data}
    tasks = [item if t["id"] == item["id"] else t for t in tasks] if old else tasks + [item]
    write_json(CONFIG / "tasks.json", tasks)
    return {"message": "Task saved"}


def delete_task(item_id):
    tasks = read_list("tasks")
    item = next((t for t in tasks if t["id"] == item_id), None)
    if not item:
        raise UserError("Task not found")
    if task_unit(item) in active_tasks():
        raise UserError("Stop the task before removing it")
    write_json(CONFIG / "tasks.json", [t for t in tasks if t["id"] != item_id])
    runs = read_state()
    runs.pop(item_id, None)
    write_json(STATE / "runs.json", runs)
    return {"message": "Saved task removed; no files were touched"}


def run_lock():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(STATE / "task.lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(STATE / "task.lock", 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock)
        raise UserError("A task is already starting or running") from exc
    return lock


def start_task(item_id, reverse=False):
    item = next((t for t in read_list("tasks") if t["id"] == item_id), None)
    if not item:
        raise UserError("Task not found")
    if reverse and len(item["destinations"]) != 1:
        raise UserError("Reverse is available for single-destination tasks only")
    lock = run_lock()
    try:
        if active_tasks():
            raise UserError("Another Rclone task is running; wait or stop it first")
        command([tool("systemd-run"), "--user", f"--unit={task_unit(item)[:-8]}", "--collect",
                 sys.executable, str(Path(__file__).resolve()), "worker", item_id,
                 "reverse" if reverse else "forward"])
        updated_run(item_id, status="Starting", reverse=reverse,
                    startedAt=datetime.now(timezone.utc).isoformat(), destination=0)
    finally:
        os.close(lock)
    return {"message": "Task started as an independent user service"}


def worker(item_id, reverse):
    # Wait briefly for the launch helper to release its lock, then hold it for
    # the whole sequence. Reloading/disabling Shell cannot affect this process.
    lock = run_lock_wait()
    try:
        item = next((t for t in read_list("tasks") if t["id"] == item_id), None)
        if not item:
            raise UserError("Task definition missing")
        if reverse and len(item["destinations"]) != 1:
            raise UserError("Reverse requires one destination")
        source = item["destinations"][0] if reverse else item["source"]
        targets = [item["source"]] if reverse else item["destinations"]
        direction = ("download" if item["direction"] == "upload" else "upload") if reverse else item["direction"]
        if direction == "upload" and not Path(source).is_dir():
            raise UserError("Local source folder does not exist")
        for index, target in enumerate(targets):
            updated_run(item_id, status="Running", destination=index + 1, total=len(targets))
            # Do not buffer long-running rclone output in memory or write
            # potentially private filenames to the Shell extension's logs.
            result = subprocess.run([tool("rclone"), item["action"], source, target,
                                     "--transfers", "4", "--checkers", "8", "--stats", "1s",
                                     "--stats-one-line", "--rc", "--rc-addr", f"127.0.0.1:{TASK_PORT}"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                updated_run(item_id, status="Failed", finishedAt=datetime.now(timezone.utc).isoformat())
                return 1
        updated_run(item_id, status="Completed", finishedAt=datetime.now(timezone.utc).isoformat())
        return 0
    except UserError:
        updated_run(item_id, status="Failed", finishedAt=datetime.now(timezone.utc).isoformat())
        return 1
    finally:
        os.close(lock)


def run_lock_wait():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(STATE / "task.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def stop_task(item_id):
    item = next((t for t in read_list("tasks") if t["id"] == item_id), None)
    if not item or task_unit(item) not in active_tasks():
        raise UserError("Task is not running")
    command([tool("systemctl"), "--user", "stop", task_unit(item)], timeout=30)
    updated_run(item_id, status="Stopped", finishedAt=datetime.now(timezone.utc).isoformat())
    return {"message": "Task stopped; incomplete transfers may remain"}


def stats(port):
    result = command([tool("rclone"), "rc", "--url", f"http://127.0.0.1:{port}", "core/stats"],
                     timeout=3, check=False)
    if result.returncode:
        return None
    try:
        data = json.loads(result.stdout)
        return {"bytes": data.get("bytes", 0), "totalBytes": data.get("totalBytes", 0),
                "speed": data.get("speed", 0), "errors": data.get("errors", 0),
                "transfers": data.get("transfers", 0), "totalTransfers": data.get("totalTransfers", 0),
                "eta": data.get("eta")}
    except ValueError:
        return None


def snapshot():
    units = active_tasks()
    runs = read_state()
    mounts = []
    for item in read_list("mounts"):
        is_mounted = mounted(item)
        is_starting = not is_mounted and active(mount_unit(item))
        mounts.append({"id": item["id"], "label": item["label"], "mounted": is_mounted,
                       "status": "Mounted" if is_mounted else "Starting" if is_starting else "Off",
                       "stats": stats(item["port"]) if is_mounted else None})
    tasks = []
    for item in read_list("tasks"):
        running = task_unit(item) in units
        run = runs.get(item["id"], {})
        status = run.get("status", "Ready")
        if status in ("Running", "Starting") and not running:
            status = "Interrupted"  # e.g. killed outside our Stop command
        tasks.append({"id": item["id"], "name": item["name"], "action": item["action"],
                      "direction": item["direction"], "destinations": len(item["destinations"]),
                      "running": running, "status": status if running or status != "Starting" else "Starting",
                      "destination": run.get("destination", 0), "total": run.get("total", 0),
                      "reverse": run.get("reverse", False), "startedAt": run.get("startedAt", ""),
                      "finishedAt": run.get("finishedAt", ""),
                      "stats": stats(TASK_PORT) if running and status == "Running" else None})
    return {"mounts": mounts, "tasks": tasks, "activeTaskUnits": units}


def main(argv):
    if len(argv) < 2:
        raise UserError("Missing command")
    operation = argv[1]
    if operation == "worker":
        return worker(argv[2], argv[3] == "reverse")
    if operation == "snapshot":
        result = snapshot()
    elif operation == "definitions":
        result = {"mounts": read_list("mounts"), "tasks": read_list("tasks")}
    elif operation in ("save-mount", "save-task"):
        data = json.loads(argv[2])
        result = save_mount(data) if operation == "save-mount" else save_task(data)
    elif operation == "delete-mount":
        result = delete_mount(argv[2])
    elif operation == "delete-task":
        result = delete_task(argv[2])
    elif operation in ("mount", "unmount", "open"):
        result = operate_mount(argv[2], operation)
    elif operation in ("run", "reverse"):
        result = start_task(argv[2], operation == "reverse")
    elif operation == "stop":
        result = stop_task(argv[2])
    else:
        raise UserError("Unknown command")
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (UserError, KeyError, IndexError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
