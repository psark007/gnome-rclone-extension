#!/usr/bin/env python3
"""Manual rclone controls. No remote discovery, imports, or scheduled work."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gnome-shell-rclone"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "gnome-shell-rclone"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "gnome-shell-rclone" / "mounts"
FIRST_TASK_PORT = 5576
TASK_HISTORY_LIMIT = 5
RECOMMENDED_CACHE = {"mode": "full", "maxSize": "5G", "maxAge": "24h", "minFreeSpace": "10G"}
LOG_LIMIT = 1024 * 1024
LOG_TOTAL = 16 * 1024 * 1024
RC_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def logging_enabled():
    path = CONFIG / "diagnostics.json"
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text())
        if type(value.get("logging")) is not bool:
            raise ValueError("invalid logging setting")
        return value["logging"]
    except (OSError, ValueError, AttributeError) as exc:
        raise UserError("Invalid diagnostics settings") from exc


def save_diagnostics(payload):
    if type(payload.get("logging")) is not bool:
        raise UserError("Diagnostic logging must be on or off")
    write_json(CONFIG / "diagnostics.json", {"logging": payload["logging"]})
    return {"message": "Logging setting saved; applies to new runs and mounts"}


def validate_cache(cache):
    if not isinstance(cache, dict) or set(cache) != set(RECOMMENDED_CACHE):
        raise UserError("Enter all caching settings")
    if cache["mode"] not in ("writes", "full"):
        raise UserError("Choose Writes only or Reads and writes")
    for key in ("maxSize", "minFreeSpace"):
        value = cache[key]
        if not isinstance(value, str) or not (value == "off" or re.fullmatch(r"[1-9][0-9]*[KMGT]", value)):
            raise UserError(f"Invalid {key}; use e.g. 5G or off")
        if value != "off" and int(value[:-1]) * 1024 ** ("KMGT".index(value[-1]) + 1) >= 2 ** 63:
            raise UserError(f"{key} is too large")
    if not isinstance(cache["maxAge"], str) or not re.fullmatch(r"[1-9][0-9]*[mhdw]", cache["maxAge"]):
        raise UserError("Invalid cache age; use e.g. 24h or 7d")
    if int(cache["maxAge"][:-1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[cache["maxAge"][-1]] > 36500 * 86400:
        raise UserError("Cache age is too large")
    return dict(cache)


def mount_cache(item):
    # An absent cache block denotes the original flags, including inherited
    # rclone size/age/free-space defaults. Never move its implicit cache dir.
    return item.get("cache") or {"mode": "writes", "maxSize": None, "maxAge": None, "minFreeSpace": None}


def mount_args(item):
    cache = mount_cache(item)
    args = [tool("rclone"), "mount", item["remote"], item["path"], "--transfers", "8",
            "--checkers", "16", "--vfs-cache-mode", cache["mode"]]
    for key, flag in (("maxSize", "--vfs-cache-max-size"), ("maxAge", "--vfs-cache-max-age"),
                      ("minFreeSpace", "--vfs-cache-min-free-space")):
        if cache[key] is not None:
            args.extend([flag, cache[key]])
    if item.get("cacheDir"):
        args.extend(["--cache-dir", item["cacheDir"]])
    return args + ["--rc", "--rc-addr", f"127.0.0.1:{item['port']}"]


def logs_dir():
    path = STATE / "logs"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    os.chmod(path, 0o700)
    return path


def log_path(item_id, kind):
    if not re.fullmatch(r"[0-9a-f]{32}", item_id):
        raise UserError("Invalid entry ID for diagnostics")
    return logs_dir() / f"{kind}-{item_id}-{uuid4().hex}.log"


def latest_log(item_id, kind):
    if not re.fullmatch(r"[0-9a-f]{32}", item_id) or not (STATE / "logs").is_dir():
        return None
    matches = sorted((STATE / "logs").glob(f"{kind}-{item_id}-*.log"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    return str(matches[0]) if matches else None


def owned_logs():
    return [path for path in logs_dir().glob("*.log") if re.fullmatch(
        r"(?:task|mount)-[0-9a-f]{32}-[0-9a-f]{32}\.log", path.name)]


def prune_logs(protected=None):
    # A running writer holds a shared flock. Never remove an active log.
    with file_lock(STATE / "logs.lock"):
        paths = sorted(owned_logs(), key=lambda p: p.stat().st_mtime)
        total = sum(path.stat().st_size for path in paths)
        for path in paths:
            if total <= LOG_TOTAL:
                break
            if path == protected:
                continue
            with path.open("rb") as candidate:
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                size = path.stat().st_size
                path.unlink()
                total -= size


def stream_logged(args, item_id, kind, on_start=None):
    path = log_path(item_id, kind)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    with os.fdopen(fd, "wb+") as output:
        fcntl.flock(output, fcntl.LOCK_SH)
        if on_start:
            on_start(str(path))
        # One bounded chunk at a time. Never buffer rclone's entire output.
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        while True:
            chunk = process.stdout.read1(65536)
            if not chunk:
                break
            if output.tell() + len(chunk) > LOG_LIMIT:
                output.seek(-min(output.tell(), LOG_LIMIT // 2), os.SEEK_END)
                tail = output.read(LOG_LIMIT // 2)
                output.seek(0)
                output.truncate()
                output.write(b"[Earlier output omitted; log size limited]\n" + tail)
            prune_logs(path)
            with file_lock(STATE / "logs.lock"):
                other_bytes = sum(p.stat().st_size for p in owned_logs() if p != path)
                room = max(0, min(LOG_LIMIT - output.tell(), LOG_TOTAL - other_bytes - output.tell()))
                output.write(chunk[:room])
            output.flush()
        code = process.wait()
    prune_logs()
    return code, str(path)


def open_logs(item_id=None, kind=None):
    if item_id:
        path = latest_log(item_id, kind)
        if not path:
            raise UserError("No retained log for this entry")
    else:
        path = str(logs_dir())
    command([tool("gio"), "open", Path(path).as_uri()], timeout=8)
    return {"message": f"Opened {path}"}


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


@contextmanager
def file_lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def updated_run(task_id, **fields):
    with file_lock(STATE / "runs.lock"):
        runs = read_state()
        runs[task_id] = {**runs.get(task_id, {}), **fields}
        write_json(STATE / "runs.json", runs)


def history_entry(run):
    if run.get("status") not in ("Completed", "Failed", "Stopped", "Interrupted"):
        return None
    return {key: run.get(key) for key in ("runId", "status", "startedAt", "finishedAt", "reverse",
                                          "destination", "total", "exitCode", "failure", "log")}


def same_history_run(previous, entry):
    if entry["runId"]:
        return previous.get("runId") == entry["runId"]
    return (previous.get("startedAt"), previous.get("finishedAt"), previous.get("status")) == (
        entry["startedAt"], entry["finishedAt"], entry["status"])


def record_history(run):
    entry = history_entry(run)
    history = run.get("history", [])
    if entry and not any(same_history_run(previous, entry) for previous in history):
        history = [entry, *history]
    return history[:TASK_HISTORY_LIMIT]


def recently_starting(run):
    if run.get("status") != "Starting" or not run.get("startedAt"):
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(run["startedAt"])).total_seconds()
        return 0 <= age < 10
    except (TypeError, ValueError):
        return False


def finish_run(task_id, status, **fields):
    with file_lock(STATE / "runs.lock"):
        runs = read_state()
        run = runs.get(task_id, {})
        if run.get("status") in ("Completed", "Failed", "Stopped"):
            return False  # A concurrent stop must not replace a completed result.
        run = {**run, **fields, "status": status, "finishedAt": datetime.now(timezone.utc).isoformat()}
        run["history"] = record_history(run)
        runs[task_id] = run
        write_json(STATE / "runs.json", runs)
        return True


def available_port(mounts, tasks):
    used = {item["port"] for item in mounts + tasks if "port" in item}
    return next(port for port in range(FIRST_TASK_PORT, 65536) if port not in used)


def local_path(value):
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute() or str(path) == "/":
        raise UserError("Local path must be absolute (or ~/...) and not the filesystem root")
    return str(path)


def remote_path(value):
    value = str(value).strip()
    if not value or value.startswith((":", "/", "-")) or ":" not in value:
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
    with file_lock(CONFIG / "definitions.lock"):
        return _save_mount(payload)


def _save_mount(payload):
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
        if remote != old["remote"]:
            raise UserError("Remote changes may strand cached uploads; create a new mount after verifying the old cache")
        entry = {**old, "label": label, "remote": remote, "path": path}
        if "cache" in payload:
            entry["cache"] = validate_cache(payload["cache"])
        mounts = [entry if m["id"] == old["id"] else m for m in mounts]
    else:
        used_ports = {m["port"] for m in mounts}
        used_ports.update(t["port"] for t in read_list("tasks") if "port" in t)
        port = next(p for p in range(5577, 65536) if p not in used_ports)
        item_id = uuid4().hex
        entry = {"id": item_id, "label": label, "remote": remote, "path": path, "port": port,
                 "cache": validate_cache(payload.get("cache", RECOMMENDED_CACHE)),
                 "cacheDir": str(CACHE / item_id)}
        mounts.append(entry)
    write_json(CONFIG / "mounts.json", mounts)
    return {"message": "Mount saved"}


def delete_mount(item_id):
    with file_lock(CONFIG / "definitions.lock"):
        mounts = read_list("mounts")
        item = next((m for m in mounts if m["id"] == item_id), None)
        if not item:
            raise UserError("Mount entry not found")
        guard_mount(item)
        write_json(CONFIG / "mounts.json", [m for m in mounts if m["id"] != item_id])
        return {"message": "Saved entry removed; cache and remote files were left untouched"}


def operate_mount(item_id, action):
    if action == "open":
        return _operate_mount(item_id, action)
    with file_lock(CONFIG / "definitions.lock"):
        return _operate_mount(item_id, action)


def _operate_mount(item_id, action):
    item = next((m for m in read_list("mounts") if m["id"] == item_id), None)
    if not item:
        raise UserError("Mount entry not found")
    if action == "mount":
        if active(mount_unit(item)) or mounted(item):
            raise UserError("Mount is already running or this path is occupied")
        path = Path(item["path"])
        if not path.is_dir() or any(path.iterdir()):
            raise UserError("Mountpoint must be an existing empty directory")
        if item.get("cacheDir"):
            cache_dir = Path(item["cacheDir"])
            cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(cache_dir, 0o700)
        args = mount_args(item)
        if logging_enabled():
            args = [sys.executable, str(Path(__file__).resolve()), "mount-worker", item_id]
        command([tool("systemd-run"), "--user", f"--unit={mount_unit(item)[:-8]}", "--collect",
                 "--property=UMask=0077", *args])
        return {"message": "Mount starting; check status in a few seconds"}
    if action == "unmount":
        if not mounted(item):
            raise UserError("Mountpoint is not mounted")
        for check in range(2):
            status = vfs_status(item["port"])
            if status["state"] != "clear":
                raise UserError(f"Unmount blocked: uploads {status['state']}; wait or inspect logs. Cache untouched")
            if check == 0:
                time.sleep(1)
        # fusermount fails on a busy mount (no lazy/force flag). The service is
        # only stopped after a successful detach; rclone may exit on detach.
        command([tool("fusermount3"), "-u", item["path"]])
        if mounted(item):
            raise UserError("Unmount did not complete; mount service left running")
        command([tool("systemctl"), "--user", "stop", mount_unit(item)])
        return {"message": "Unmounted; cache retained. New writes during checks cannot be ruled out"}
    if action == "open":
        if not mounted(item):
            raise UserError("Mountpoint is not mounted")
        command([tool("gio"), "open", Path(item["path"]).as_uri()], timeout=8)
        return {"message": "Opening folder"}
    raise UserError("Unknown mount operation")


def mount_worker(item_id):
    item = next((m for m in read_list("mounts") if m["id"] == item_id), None)
    if not item:
        raise UserError("Mount definition missing")
    code, _ = stream_logged(mount_args(item), item_id, "mount")
    return code


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
    with file_lock(CONFIG / "definitions.lock"):
        return _save_task(payload)


def _save_task(payload):
    tasks = read_list("tasks")
    data = validate_task(payload)
    old = next((t for t in tasks if t["id"] == payload.get("id")), None)
    if payload.get("id") and not old:
        raise UserError("Task not found")
    if old and task_unit(old) in active_tasks():
        raise UserError("Stop the task before editing it")
    port = old.get("port") if old else None
    if port is None:
        port = available_port(read_list("mounts"), tasks)
    item = {"id": old["id"] if old else uuid4().hex, **data, "port": port}
    tasks = [item if t["id"] == item["id"] else t for t in tasks] if old else tasks + [item]
    write_json(CONFIG / "tasks.json", tasks)
    return {"message": "Task saved"}


def delete_task(item_id):
    with file_lock(CONFIG / "definitions.lock"):
        return _delete_task(item_id)


def _delete_task(item_id):
    tasks = read_list("tasks")
    item = next((t for t in tasks if t["id"] == item_id), None)
    if not item:
        raise UserError("Task not found")
    if task_unit(item) in active_tasks():
        raise UserError("Stop the task before removing it")
    write_json(CONFIG / "tasks.json", [t for t in tasks if t["id"] != item_id])
    with file_lock(STATE / "runs.lock"):
        runs = read_state()
        runs.pop(item_id, None)
        write_json(STATE / "runs.json", runs)
    return {"message": "Saved task removed; no files were touched"}


def start_task(item_id, reverse=False):
    with file_lock(CONFIG / "definitions.lock"):
        return _start_task(item_id, reverse)


def _start_task(item_id, reverse):
    tasks = read_list("tasks")
    item = next((t for t in tasks if t["id"] == item_id), None)
    if not item:
        raise UserError("Task not found")
    if reverse and len(item["destinations"]) != 1:
        raise UserError("Reverse is available for single-destination tasks only")
    if task_unit(item) in active_tasks():
        raise UserError("This task is already running")
    if "port" not in item:  # Definitions saved before per-task ports were introduced.
        item["port"] = available_port(read_list("mounts"), tasks)
        write_json(CONFIG / "tasks.json", tasks)
    with file_lock(STATE / "runs.lock"):
        runs = read_state()
        previous = runs.get(item_id, {})
        if recently_starting(previous):
            raise UserError("This task is already starting; check status in a few seconds")
        if previous.get("status") in ("Running", "Starting"):
            previous = {**previous, "status": "Interrupted",
                        "finishedAt": ""}  # Detection time is not the actual end time.
        history = record_history(previous)
        runs[item_id] = {"history": history, "runId": uuid4().hex, "status": "Starting", "reverse": reverse,
                         "startedAt": datetime.now(timezone.utc).isoformat(), "finishedAt": "",
                         "destination": 0, "total": len(item["destinations"]),
                         "log": None, "exitCode": None, "failure": None}
        write_json(STATE / "runs.json", runs)
    try:
        command([tool("systemd-run"), "--user", f"--unit={task_unit(item)[:-8]}", "--collect",
                 sys.executable, str(Path(__file__).resolve()), "worker", item_id,
                 "reverse" if reverse else "forward"])
    except UserError:
        finish_run(item_id, "Failed", failure="Could not start user service")
        raise
    return {"message": "Task started as an independent user service"}


def worker(item_id, reverse):
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
        log = None
        record_log = logging_enabled()
        for index, target in enumerate(targets):
            updated_run(item_id, status="Running", destination=index + 1, total=len(targets))
            args = [tool("rclone"), item["action"], source, target,
                    "--transfers", "8", "--checkers", "16", "--stats", "1s",
                    "--stats-one-line", "--rc", "--rc-addr", f"127.0.0.1:{item['port']}"]
            if record_log:
                code, log = stream_logged(args, item_id, "task",
                                          on_start=lambda path: updated_run(item_id, log=path))
            else:
                code = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            if code:
                finish_run(item_id, "Failed", exitCode=code)
                return 1
        finish_run(item_id, "Completed", exitCode=0)
        return 0
    except (UserError, OSError) as exc:
        finish_run(item_id, "Failed", failure="Local setup failed" if isinstance(exc, OSError) else str(exc))
        return 1


def stop_task(item_id):
    item = next((t for t in read_list("tasks") if t["id"] == item_id), None)
    if not item or task_unit(item) not in active_tasks():
        raise UserError("Task is not running")
    command([tool("systemctl"), "--user", "stop", task_unit(item)], timeout=30)
    stopped = finish_run(item_id, "Stopped")
    return {"message": "Task stopped; incomplete transfers may remain" if stopped else
            "Task had already finished; check its latest result"}


def rc_call(port, endpoint):
    try:
        # RC uses POST; cap the queue response so a very large mount cannot
        # make each panel refresh buffer an unbounded list of filenames.
        request = urllib.request.Request(f"http://127.0.0.1:{port}/{endpoint}", data=b"{}",
                                         headers={"Content-Type": "application/json"})
        with RC_OPENER.open(request, timeout=3) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None
        data = json.loads(body)
        return data if isinstance(data, dict) and "error" not in data else None
    except (OSError, ValueError):
        return None


def vfs_status(port):
    # v1.75.0: vfs/stats diskCache.{uploadsQueued,uploadsInProgress,erroredFiles}
    # and vfs/queue queue[].{uploading,tries}. Neither includes open writes.
    summary = rc_call(port, "vfs/stats")
    queue = rc_call(port, "vfs/queue")
    disk = summary.get("diskCache") if summary else None
    entries = queue.get("queue") if queue else None
    if not isinstance(disk, dict) or not isinstance(entries, list) or any(
            not isinstance(disk.get(key), int) or isinstance(disk[key], bool) or disk[key] < 0
            for key in ("uploadsQueued", "uploadsInProgress", "erroredFiles")) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("uploading"), bool) for entry in entries):
        return {"state": "unknown", "queued": None, "uploading": None, "failed": None, "retried": None}
    uploading = max(disk["uploadsInProgress"], sum(e["uploading"] for e in entries))
    queued = max(disk["uploadsQueued"], sum(not e["uploading"] for e in entries))
    failed = disk["erroredFiles"]
    retried = sum(isinstance(e.get("tries"), int) and e["tries"] > 1 for e in entries)
    return {"state": "failed" if failed else "pending" if queued or uploading else "clear",
            "queued": queued, "uploading": uploading, "failed": failed, "retried": retried}


def stats(port):
    data = rc_call(port, "core/stats")
    if not data or not any(key in data for key in ("bytes", "speed", "errors", "transfers")):
        return None
    return {key: data.get(key) for key in ("bytes", "totalBytes", "speed", "errors",
                                          "transfers", "totalTransfers", "eta")}


def rclone_version():
    try:
        result = command([tool("rclone"), "version"], timeout=4, check=False)
        return result.stdout.splitlines()[0] if not result.returncode and result.stdout else "Unavailable"
    except UserError:
        return "Unavailable"


def diagnostics():
    data = snapshot()
    mounts = read_list("mounts")
    for entry, item in zip(data["mounts"], mounts):
        entry["cache"] = mount_cache(item)
        entry["cacheDir"] = item.get("cacheDir")  # absent means rclone's original default location
        entry["transfers"] = 8
        entry["checkers"] = 16
    for entry in data["tasks"]:
        entry["transfers"] = 8
        entry["checkers"] = 16
    return {"version": rclone_version(), "logging": logging_enabled(), **data}


def snapshot():
    units = active_tasks()
    runs = read_state()
    mounts = []
    for item in read_list("mounts"):
        is_mounted = mounted(item)
        is_starting = not is_mounted and active(mount_unit(item))
        mounts.append({"id": item["id"], "label": item["label"], "mounted": is_mounted,
                       "status": "Mounted" if is_mounted else "Starting" if is_starting else "Off",
                       "stats": stats(item["port"]) if is_mounted else None,
                       "uploads": vfs_status(item["port"]) if is_mounted else None,
                       "log": latest_log(item["id"], "mount")})
    tasks = []
    for item in read_list("tasks"):
        running = task_unit(item) in units
        run = runs.get(item["id"], {})
        status = run.get("status", "Ready")
        if status in ("Running", "Starting") and not running:
            # systemd-run can return just before its service appears in list-units.
            if not recently_starting(run):
                status = "Interrupted"  # e.g. killed outside our Stop command
        history = record_history({**run, "status": status}) if status in (
            "Completed", "Failed", "Stopped", "Interrupted") else run.get("history", [])
        tasks.append({"id": item["id"], "name": item["name"], "action": item["action"],
                      "direction": item["direction"], "destinations": len(item["destinations"]),
                      "running": running, "status": status if running or status != "Starting" else "Starting",
                      "destination": run.get("destination", 0), "total": run.get("total", 0),
                       "reverse": run.get("reverse", False), "startedAt": run.get("startedAt", ""),
                       "finishedAt": run.get("finishedAt", ""), "exitCode": run.get("exitCode"),
                        "failure": run.get("failure"), "log": run.get("log") if run.get("log") and Path(run["log"]).is_file() else None,
                        "history": history,
                       "stats": stats(item["port"]) if running and status == "Running" and "port" in item else None})
    return {"mounts": mounts, "tasks": tasks, "activeTaskUnits": units}


def main(argv):
    if len(argv) < 2:
        raise UserError("Missing command")
    operation = argv[1]
    if operation == "worker":
        return worker(argv[2], argv[3] == "reverse")
    if operation == "mount-worker":
        return mount_worker(argv[2])
    if operation == "snapshot":
        result = snapshot()
    elif operation == "diagnostics":
        result = diagnostics()
    elif operation == "definitions":
        result = {"mounts": read_list("mounts"), "tasks": read_list("tasks"),
                  "logging": logging_enabled()}
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
    elif operation == "save-diagnostics":
        result = save_diagnostics(json.loads(argv[2]))
    elif operation == "open-logs":
        result = open_logs()
    elif operation in ("open-task-log", "open-mount-log"):
        result = open_logs(argv[2], "task" if operation == "open-task-log" else "mount")
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
