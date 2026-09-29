# Rclone Mounts & Tasks for GNOME Shell

Manual Rclone mounts and copy/sync tasks for Fedora GNOME Shell 51. New installs
start empty. Nothing mounts or transfers automatically.

## Use

- Add mounts and tasks in **Preferences**, using explicit `remote:path` values.
  Mountpoints must already exist and be empty. Mounts show speed and status;
  active mounts cannot be edited or removed.
- Tasks run on demand, one at a time. Multiple destinations run sequentially;
  a failed destination stops the task. Single-destination tasks can run in
  reverse. Run, Reverse, and Stop require confirmation.
- **Sync can delete destination-only files**, including local files when
  downloading or reversing an upload. Check paths before confirming.

The panel shows mounted (`M`) and running-task (`T`) counts; its icon switches
to transfer arrows while a task runs. Mounts and Tasks have separate popup
tabs. Work runs in transient `systemd --user` services, so disabling the
extension does not stop it: stop tasks and unmount explicitly.

Definitions and task history stay outside this repository under
`~/.config/gnome-shell-rclone/` and `~/.local/state/gnome-shell-rclone/`
(private permissions). Configure Rclone remotes separately; the extension
does not discover them. Progress uses loopback-only Rclone RC (task port 5576,
mount ports from 5577). Do not commit `rclone.conf` or saved definitions.

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
