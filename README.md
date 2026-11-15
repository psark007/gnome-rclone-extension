# Rclone Manager for GNOME Shell

Manage Rclone mounts, one-way copy/sync tasks, and Bisync folder pairs from the
GNOME Shell panel. New installs start empty; nothing mounts or transfers
automatically.

## Features

### Mounts

Add mounts in **Preferences** with explicit `remote:path` values. Mountpoints
must already exist and be empty. The panel shows transfer activity and upload
status. Active mounts cannot be edited or removed.

New mounts use **Reads and writes** caching (`--vfs-cache-mode full`), a **5G**
size limit, **24h** maximum age since last access, and **10G** minimum free
space target. These are editable safeguards; they do not report current disk
usage. Choose **Writes only** or **No limit** in each mount's **Caching**
section.

The size limit is soft per mount: open files and delayed polling can exceed it.
Minimum free space is a target, not a reservation. Full mode caches accessed
data on **local disk, not RAM**. Cache files for crypt mounts contain decrypted
data, so check disk space, permissions, and backups.

Each new mount has its own cache root under
`.cache/mounts/<mount-id>/` in the selected data directory. Rclone creates its
VFS subdirectories inside that root. Existing mounts are not moved. If an old
definition has no saved cache settings, it keeps Writes only and inherited
Rclone defaults for age, size, and free space. If it has no `cacheDir`, it
keeps Rclone's default cache location (`rclone config paths` shows it).

To move an old mount to an isolated cache root, stop new writes, wait for
uploads, unmount safely, and inspect or back up its original cache for pending
data before removing the definition and creating a new one. Do not move or
delete cache files directly: pending uploads may be the only copy. The
extension never deletes cache files when removing definitions or disabling it.
Cache changes take effect on the next mount.

### Copy and sync tasks

Create tasks in **Preferences** with explicit `remote:path` values. Tasks can
run on demand or on a daily/weekly local-time schedule. Different tasks may run
at once, but each task can run only once at a time. Multiple destinations in a
task run sequentially; a failed destination stops the task. Single-destination
tasks can run in reverse. **Run**, **Reverse**, and **Stop** require
confirmation.

Sync can delete destination-only files, including local files when downloading
or reversing an upload. Check paths before confirming.

Scheduled tasks use persistent `systemd --user` timers while the user systemd
manager is available. A run missed while the system was asleep or off starts
when the manager returns. To disable a schedule, edit the task and turn
**Automatic schedule** off. Scheduled sync runs retain sync's behavior of
deleting destination-only files.

The Tasks tab shows the last two outcomes with local date/time and duration
where known. Task history includes reverse mode and destination position. The
latest five attempts per entry are retained in `.state/runs.json`; an active run does
not erase the previous result. Existing single-run state appears as one
historical result until newer runs replace it. If a service exits unexpectedly,
its end time is unknown and the UI labels the recorded start time accordingly.

### Bisync pairs

Bisync pairs appear in the **Tasks** tab. Both folders must exist, must not
overlap each other, and should not overlap other transfers. Bisync propagates
changes and deletions in both directions.

Use **Resync** for initial setup or recovery. Normal **Run** uses the saved
baseline. Both actions require confirmation. Resync can replace conflicting
files using its selected Path 1, Path 2, newer, older, larger, or smaller
preference. Review [Rclone's Bisync manual](https://rclone.org/bisync/) before
use.

Bisync can run on demand or on a daily/weekly local-time schedule. Schedules
use persistent `systemd --user` timers while the user systemd manager is
available; a missed run starts when the manager returns. Turn **Automatic
schedule** off in the pair's Preferences to disable it. Scheduled runs perform
normal Bisync; **Resync** remains manual. Create the baseline with Resync before
the first scheduled run. Bisync is not a real-time watcher and does not
automatically restart after logout. Rclone stores Bisync baselines in its own
cache directory; removing a pair definition does not remove its baseline.

### Panel and services

The panel indicator shows counts such as `2M 1T`; zero values are omitted. Its
icon switches to transfer arrows while a transfer runs. Mounts and Tasks have
separate popup tabs, with Bisync pairs listed under Tasks.

Mounts, tasks, and Bisync runs use transient `systemd --user` services.
Disabling the extension does not stop them. Stop active work and unmount
explicitly.

## Settings and data

Use **Preferences → Settings → Data directory** to choose where definitions,
state, logs, and cache live. Existing installations keep the GNOME default
`~/.config/gnome-shell-rclone`.

Definitions and run history stay outside this repository under the selected
data directory with private permissions. Configure Rclone remotes separately;
the extension does not discover them. Progress uses loopback-only Rclone RC
(a distinct port per task, Bisync pair, and mount, allocated from 5576). Do not
commit `rclone.conf` or saved definitions.

## Mount safety

Unmount starts a cancellable wait; **Cancel** leaves the mount running. The
panel uses Rclone v1.75.0's [`vfs/queue` and `vfs/stats`](https://rclone.org/rc/#vfs-queue)
to distinguish queued, uploading, and failed writes. After two refreshes show
no pending uploads, **Unmount now** becomes available. The backend
rechecks twice one second apart before running `fusermount3 -u`. It stops the
user service only after a successful detach.

A busy FUSE mount is not lazily or forcibly detached. Known pending, failed, or
unknown upload status blocks unmount. Review logs and retry when healthy. There
is no force option or automatic cache deletion.

An empty VFS queue does not prove durability. Open files may not be in the
queue, new writes can arrive between checks and detach, and external unmounts
or shutdowns bypass the extension. Stop applications writing, close their
files, and verify important remote files independently. Preserve the cache
after failures; Rclone can resume unuploaded cached files when restarted with
the same cache settings and remote. When RC is unavailable, the extension will
not claim the mount is safe to detach. Disabling or reloading Shell leaves
services and caches alone.

The RC interface is loopback-only but provides user-level access. Do not expose
its ports to untrusted processes or networks.

## Diagnostics and logs

**Preferences → Diagnostics** shows the installed Rclone version, effective
mount/task transfer and cache settings, upload status, and available transfer
and error statistics. Missing RC statistics are shown as unavailable, not as
zero.

Opt-in logging is off by default. When enabled, new mount, task, and Bisync
runs are captured under `.state/logs/` in the selected data directory. The
directory is mode 0700, logs are mode 0600, each log is limited to 1 MiB, and
retained logs are limited to 16 MiB total. Older output may be omitted to keep
the recent tail. Turn logging on before reproducing an issue. Use **Open logs
folder** in Preferences or **Open log** on a panel entry.

Logs may contain filenames, paths, and provider errors; review them before
sharing. The extension does not request HTTP dumps, headers, auth traces, or
Rclone configuration. With logging off, task failures still show an exit code
or a local setup failure. For service startup failures, check the user journal:

```sh
journalctl --user -u 'gnome-rclone-*'
```

## Transfer definitions between devices

There is no built-in import/export button. Choose a data directory in
**Preferences → Settings → Data directory**; definitions are stored there as
`mounts.json`, `tasks.json`, and `bisyncs.json`. Stop active mounts, tasks, and
Bisync runs before copying these files. Do not copy `.state` unless you also
want run history.

Update device-specific absolute local paths for mountpoints, task sources and
destinations, Bisync folders, and mount `cacheDir` values. Mountpoints must
already exist and be empty. Configure the same Rclone remote names separately
on the destination device. Remote credentials are stored in Rclone's separate
`rclone.conf` and must be transferred or configured securely. Review copied
definitions before starting any mount or task.

## Install on Fedora

Install the required packages:

```sh
sudo dnf install gnome-shell gnome-extensions-app rclone fuse3 util-linux python3 glib2
```

Then, as your regular user with a systemd user session:

```sh
./install.sh
gnome-extensions enable rclone-manager@psark007.github.io
```

Log out and back in if GNOME does not discover the extension, or after updating
its JavaScript. Removing the extension code does not stop services or erase
settings.

## Development

Run the synthetic tests with temporary directories and mocked commands. Node.js
is needed only for the UI test:

```sh
python3 -m unittest discover -s tests -v
node --test tests/tab-ui.test.cjs
```

AI tools assisted with the code and documentation. Synthetic tests do not
replace testing with your own mounts or transfers.

Licensed under MIT; see [LICENSE](LICENSE).
