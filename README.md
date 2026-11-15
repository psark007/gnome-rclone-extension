# Rclone Mounts & Tasks for GNOME Shell

Manual Rclone mounts and copy/sync tasks for Fedora GNOME Shell (including 51). New installs
start empty. Nothing mounts or transfers automatically.

## Use

- Add mounts and tasks in **Preferences**, using explicit `remote:path` values.
  Mountpoints must already exist and be empty. Mounts show transfer activity and upload status;
  active mounts cannot be edited or removed.
- Tasks run on demand; different tasks can run concurrently, but each task can
  run only once at a time. Multiple destinations within a task run sequentially;
  a failed destination stops the task. Single-destination tasks can run in
  reverse. Run, Reverse, and Stop require confirmation.
- Set **Preferences → Settings → Data directory** to choose where definitions,
  state, logs, and cache live. The GNOME default remains
  `~/.config/gnome-shell-rclone` for existing installations.
- To share definitions with the Noctalia widget, set Noctalia's **Data
  directory** to the same path in **Settings → Plugins → Rclone Status**.
- The Tasks tab shows the last two outcomes (success, failure, stop, or
  interruption) with local date/time, duration where known, reverse mode,
  destination position and exit code where available. The latest five attempts
  per task are retained privately in `runs.json`; an active run does not erase
  the previous result. Existing single-run state is shown as one historical
  result until newer runs replace it. The end time of an unexpected service
  exit is unknown; the UI labels the recorded start time accordingly.
- **Sync can delete destination-only files**, including local files when
  downloading or reversing an upload. Check paths before confirming.

The panel indicator shows counts such as `2M 1T`; zero values are omitted and
its icon switches to transfer arrows while a task runs. Mounts and Tasks have
separate popup tabs. Work runs in transient `systemd --user` services, so disabling the
extension does not stop it: stop tasks and unmount explicitly.

## Mount caching

New mounts default to **Reads and writes** (`--vfs-cache-mode full`), **5G**
maximum size, **24h** maximum age since last access, and **10G** minimum free
space target. These are editable safeguards, not a claim that disk space is
currently low. Choose **Writes only** or **No limit** in each mount's **Caching**
section. The size limit is soft **per mount**; open files and delayed polling
can exceed it. Minimum free space is a target, not a reservation. Full mode
caches only data accessed, not the entire remote; it uses **local disk, not
RAM**. Cache files for crypt mounts contain **decrypted** data locally. Check
 space, permissions and backups accordingly. Rclone's directory-cache,
change-polling, write-back and provider-specific concurrency settings are left
 at their existing/default values. Mounts and separate copy/sync tasks use
 8 transfers / 16 checkers. Caching does not affect tasks.

New mounts have their own cache root under
the selected data directory's `.cache/mounts/<mount-id>/`. Rclone creates its own VFS
subdirectories inside that root. **Existing mounts are not moved**: absence
of a saved `cache` block means Writes only and *inherited rclone defaults* for
age/size/free-space; absence of `cacheDir` means the original rclone default
cache root (`rclone config paths` shows its location). Even after
editing an old mount's cache settings, its cache root remains unchanged. To
move a legacy mount to a new isolated root manually, first stop new writes,
wait for uploads, unmount safely, inspect/back up the original cache for
pending data, then remove the old definition and create a new one. Do **not**
just move/delete files or change remote names: rclone keys VFS cache content
by remote/path and pending uploads may be the only copy. Existing definitions
cannot change their remote through the editor for this reason. Cache files are
**never** deleted by the extension when removing definitions or disabling it.
Changes to caching take effect on the **next mount**, not an active mount.

## Diagnostics

**Preferences → Diagnostics** shows installed rclone version, effective
mount/task transfer and cache settings, upload status and any available
transfer/error statistics. Missing RC statistics are shown as unavailable,
not as zero. Opt-in logging (off by default) captures new mount and task runs
under the selected data directory's `.state/logs/`. The directory is 0700, logs are
0600, each is limited to 1 MiB and retained logs to 16 MiB total. Older
output may be omitted to preserve the recent tail. Turn logging on **before**
reproducing an issue; use **Open logs folder** in Preferences or **Open log**
on an entry in the panel. Logs are retained after failure and may contain
filenames, paths and provider errors. **Review before sharing**: they are not
automatically safe to publish. The extension does not request HTTP dumps,
headers, auth traces or rclone configuration. With logging off, task failures
still show an exit code (or a local setup failure). The user service journal
(`journalctl --user -u 'gnome-rclone-*'`) may help with startup failures.

## Unmount safety

The panel's **Unmount** starts a cancellable wait; **Cancel** leaves the mount
running. Upload status uses rclone v1.75.0's [`vfs/queue` and `vfs/stats`](https://rclone.org/rc/#vfs-queue).
The panel distinguishes queued, uploading and failed writes; retry attempts
are included in the diagnostics state. Once two refreshes report no pending
uploads, **Unmount now** is available. The backend rechecks twice one second
apart before `fusermount3 -u`; only after successful detach does it stop the
user service. A busy FUSE mount is not lazily/forcibly detached. Known pending,
failed or **unknown** RC upload status blocks unmount; examine logs and retry
when healthy. No force option or automatic cache deletion is provided.

An empty VFS queue is **not proof of durability**: files still open for writing
may be absent from the upload queue, new writes can arrive between checks and
detach, and external unmounts/shutdowns bypass the extension. Stop applications
writing first, close their files and verify important remote files independently.
Rclone documents that unuploaded cached files can resume when restarted with
the same cache settings and remote; preserve the cache after failures. When
RC is unsupported or unavailable, the extension will not claim safety or
detach on your behalf. Disabling/reloading Shell leaves services and caches
alone. The RC interface is loopback-only but powerful (effectively user-level
access): do not expose its ports to untrusted processes/networks.

Definitions and task history stay outside this repository under the selected
data directory (private permissions). Configure Rclone remotes separately; the extension
does not discover them. Progress uses loopback-only Rclone RC (a distinct port
per task and mount, allocated from 5576). Do not commit `rclone.conf` or saved definitions.

## Transfer definitions between devices

There is no built-in import/export button. Mount and task definitions are stored
in the selected data directory as `mounts.json` and `tasks.json`. Stop active
mounts and tasks, then copy those files to the same directory on the other
device. Do not copy the `.state` directory unless task history is wanted.

Before using the copied definitions, update device-specific absolute local paths
for mountpoints, task sources and destinations, and mount `cacheDir` values.
Mountpoints must already exist and be empty. Configure the same Rclone remote
names separately on the destination device; remote credentials are stored in
Rclone's separate `rclone.conf` and must be transferred or configured securely.
Review copied definitions before starting any mount or task.

## Install on Fedora

```sh
sudo dnf install gnome-shell gnome-extensions-app rclone fuse3 util-linux python3 glib2
```

Then, as your regular user (with a systemd user session):

```sh
./install.sh
gnome-extensions enable rclone-mounts-tasks@psark007.github.io
```

Log out and back in if GNOME does not discover the extension, or after updating
its JavaScript. Uninstalling its code does not stop services or erase settings.

## Test

Synthetic tests use temporary directories and mocked commands; Node.js is
only needed for the UI tests:

```sh
python3 -m unittest discover -s tests -v
node --test tests/tab-ui.test.cjs
```

## AI disclosure

AI tools assisted with the code and documentation. The tests use synthetic
data and do not replace testing with your own mounts or transfers.

Licensed under MIT; see [LICENSE](LICENSE).
