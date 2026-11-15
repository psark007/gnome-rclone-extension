import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class RclonePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        this._window = window;
        this._helper = `${this.path}/backend.py`;
        this._settings = this.getSettings();
        this._installReorderStyle();
        window.set_default_size(660, 650);
        this._render();
    }

    _installReorderStyle() {
        if (this._reorderCssProvider)
            return;
        const provider = new Gtk.CssProvider();
        const css = `
            .rclone-drag-handle {
                background-color: transparent;
                border: none;
                box-shadow: none;
                opacity: 0.45;
                padding: 2px;
            }
            .rclone-drag-handle:hover,
            .rclone-drag-handle:focus {
                background-color: transparent;
                box-shadow: none;
                opacity: 0.8;
            }
            .rclone-reorder-row {
                transition: background-color 180ms ease-out, opacity 120ms ease-out;
            }
            .rclone-reorder-row.rclone-dragging {
                opacity: 0.35;
            }
            .rclone-reorder-row.rclone-reordered {
                background-color: alpha(@accent_bg_color, 0.12);
            }
            .rclone-reorder-row:drop(active) {
                background-color: alpha(@accent_bg_color, 0.18);
            }
        `;
        provider.load_from_data(css, -1);
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION);
        this._reorderCssProvider = provider;
    }

    _call(...args) {
        const process = Gio.Subprocess.new(['/usr/bin/python3', this._helper, '--config-dir', this._settings.get_string('config-dir'), ...args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        const [, output] = process.communicate_utf8(null, null);
        const data = JSON.parse(output);
        if (!process.get_successful() || !data.ok)
            throw new Error(data.error || 'Rclone helper failed');
        return data;
    }

    _notice(text) {
        this._window.add_toast(new Adw.Toast({title: text}));
    }

    _perform(action, ...args) {
        try {
            this._notice(this._call(action, ...args).message);
            this._render();
        } catch (error) {
            this._notice(error.message);
        }
    }

    _button(label, callback, destructive = false) {
        const button = new Gtk.Button({label, valign: Gtk.Align.CENTER});
        if (destructive)
            button.add_css_class('destructive-action');
        button.connect('clicked', callback);
        return button;
    }

    _entry(title, value = '') {
        return new Adw.EntryRow({title, text: value});
    }

    _choice(title, choices, selected) {
        return new Adw.ComboRow({title, model: Gtk.StringList.new(choices), selected});
    }

    _row(title, subtitle, actions) {
        const row = new Adw.ActionRow({title, subtitle});
        for (const button of actions)
            row.add_suffix(button);
        return row;
    }

    _makeReorderable(row, collection, id) {
        const handle = new Gtk.Button({child: new Gtk.Image({
            icon_name: 'list-drag-handle-symbolic', pixel_size: 12,
        })});
        handle.add_css_class('flat');
        handle.add_css_class('rclone-drag-handle');
        handle.set_tooltip_text('Drag to reorder');
        handle.set_cursor_from_name('grab');
        handle.update_property([Gtk.AccessibleProperty.LABEL], ['Drag to reorder']);
        const source = Gtk.DragSource.new();
        source.set_actions(Gdk.DragAction.MOVE);
        source.connect('prepare', () => Gdk.ContentProvider.new_for_value(JSON.stringify({collection, id})));
        source.connect('drag-begin', () => {
            row.add_css_class('rclone-dragging');
            source.set_icon(Gtk.WidgetPaintable.new(row), 0, 0);
        });
        source.connect('drag-end', () => row.remove_css_class('rclone-dragging'));
        source.connect('drag-cancel', () => row.remove_css_class('rclone-dragging'));
        handle.add_controller(source);
        row.add_prefix(handle);
        row.add_css_class('rclone-reorder-row');
        this._reorderRows.set(JSON.stringify([collection, id]), row);

        const target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE);
        target.connect('drop', (_target, value, _x, y) => {
            try {
                const dragged = JSON.parse(value);
                if (dragged.collection !== collection || dragged.id === id)
                    return false;
                const position = y < row.get_height() / 2 ? 'before' : 'after';
                GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                    const moved = this._moveReorderableRow(collection, dragged.id, id, position);
                    this._saveReorder(collection, dragged.id, id, position, moved);
                    return GLib.SOURCE_REMOVE;
                });
                return true;
            } catch (error) {
                return false;
            }
        });
        row.add_controller(target);
    }

    _moveReorderableRow(collection, sourceId, targetId, position) {
        const source = this._reorderRows.get(JSON.stringify([collection, sourceId]));
        const target = this._reorderRows.get(JSON.stringify([collection, targetId]));
        if (!source || !target || source === target || source.get_parent() !== target.get_parent())
            return false;
        const list = source.get_parent();
        if (typeof list.insert !== 'function' || typeof list.remove !== 'function')
            return false;
        const sourceIndex = source.get_index();
        const targetIndex = target.get_index();
        let insertAt = targetIndex + (position === 'after' ? 1 : 0);
        list.remove(source);
        if (sourceIndex < insertAt)
            insertAt--;
        list.insert(source, insertAt);
        source.add_css_class('rclone-reordered');
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 220, () => {
            source.remove_css_class('rclone-reordered');
            return GLib.SOURCE_REMOVE;
        });
        return true;
    }

    _saveReorder(collection, sourceId, targetId, position, movedInPlace) {
        let process;
        try {
            process = Gio.Subprocess.new(['/usr/bin/python3', this._helper,
                '--config-dir', this._settings.get_string('config-dir'),
                'reorder-entry', collection, sourceId, targetId, position],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (error) {
            this._notice(error.message);
            this._render();
            return;
        }
        process.communicate_utf8_async(null, null, (proc, result) => {
            try {
                const [, output] = proc.communicate_utf8_finish(result);
                const data = JSON.parse(output);
                if (!proc.get_successful() || !data.ok)
                    throw new Error(data.error || 'Could not save the new order');
                this._notice(data.message);
                if (!movedInPlace)
                    this._render();
            } catch (error) {
                this._notice(error.message);
                this._render();
            }
        });
    }

    _render() {
        if (this._mountPage)
            this._window.remove(this._mountPage);
        if (this._taskPage)
            this._window.remove(this._taskPage);
        if (this._diagnosticsPage)
            this._window.remove(this._diagnosticsPage);
        if (this._settingsPage)
            this._window.remove(this._settingsPage);
        this._mountEditor = null;
        this._taskEditor = null;
        this._bisyncEditor = null;
        this._reorderRows = new Map();
        this._mountPage = new Adw.PreferencesPage({title: 'Mounts', icon_name: 'folder-remote-symbolic'});
        this._taskPage = new Adw.PreferencesPage({title: 'Tasks', icon_name: 'document-send-symbolic'});
        this._diagnosticsPage = new Adw.PreferencesPage({title: 'Diagnostics', icon_name: 'dialog-information-symbolic'});
        this._settingsPage = new Adw.PreferencesPage({title: 'Settings', icon_name: 'emblem-system-symbolic'});
        this._window.add(this._mountPage);
        this._window.add(this._taskPage);
        this._window.add(this._diagnosticsPage);
        this._window.add(this._settingsPage);
        const settingsGroup = new Adw.PreferencesGroup({title: 'Storage', description: 'Choose where mount, task, and Bisync definitions, state, logs, and cache are stored.'});
        const configDir = new Adw.EntryRow({title: 'Data directory', text: this._settings.get_string('config-dir')});
        configDir.connect('notify::text', () => {
            const value = configDir.text.trim();
            if (value)
                this._settings.set_string('config-dir', value);
        });
        settingsGroup.add(configDir);
        this._settingsPage.add(settingsGroup);
        let data;
        try {
            data = this._call('definitions');
        } catch (error) {
            this._mountPage.add(new Adw.PreferencesGroup({title: 'Cannot load definitions', description: error.message}));
            return;
        }
        this._mountList = new Adw.PreferencesGroup({title: 'Saved mounts',
            description: 'Nothing mounts automatically. Add an explicit remote:path and an existing empty local folder.'});
        this._mountPage.add(this._mountList);
        this._mountList.add(this._row('Add mount', 'Save an entry without starting it',
            [this._button('Add', () => this._mountForm())]));
        for (const mount of data.mounts) {
            let awaiting = false;
            const remove = this._button('Remove', () => {
                if (!awaiting) {
                    awaiting = true;
                    remove.label = 'Confirm Remove';
                    return;
                }
                this._perform('delete-mount', mount.id);
            }, true);
            const row = this._row(mount.label, `${mount.remote} → ${mount.path}`,
                [this._button('Edit', () => this._mountForm(mount)), remove]);
            this._makeReorderable(row, 'mounts', mount.id);
            this._mountList.add(row);
        }
        this._taskList = new Adw.PreferencesGroup({title: 'Transfer tasks',
            description: 'Copy preserves destination-only files. Sync deletes them. Bisync propagates changes both ways and can run on a schedule. Resync stays manual. Reverse swaps source and destination for one run.'});
        this._taskPage.add(this._taskList);
        this._taskList.add(this._row('Add a task', 'Saving a definition does not start a transfer', [
            this._button('Copy / Sync', () => this._taskForm()),
            this._button('Bisync pair', () => this._bisyncForm()),
        ]));
        for (const task of data.tasks) {
            let awaiting = false;
            const remove = this._button('Remove', () => {
                if (!awaiting) {
                    awaiting = true;
                    remove.label = 'Confirm Remove';
                    return;
                }
                this._perform('delete-task', task.id);
            }, true);
            const scheduleText = task.schedule
                ? ` · ${task.schedule.frequency === 'daily' ? 'Daily' : `Weekly ${task.schedule.weekday}`} at ${task.schedule.time}`
                : '';
            const row = this._row(task.name,
                `${task.action.toUpperCase()} · ${task.direction} · ${task.source} → ${task.destinations.join('; ')}${scheduleText}`,
                [this._button('Edit', () => this._taskForm(task)), remove]);
            this._makeReorderable(row, 'tasks', task.id);
            this._taskList.add(row);
        }
        for (const item of data.bisyncs) {
            let awaiting = false;
            const remove = this._button('Remove', () => {
                if (!awaiting) {
                    awaiting = true;
                    remove.label = 'Confirm Remove';
                    return;
                }
                this._perform('delete-bisync', item.id);
            }, true);
            const scheduleText = item.schedule
                ? ` · ${item.schedule.frequency === 'daily' ? 'Daily' : `Weekly ${item.schedule.weekday}`} at ${item.schedule.time}`
                : '';
            const row = this._row(`${item.name} · BISYNC`,
                `${item.path1} ↔ ${item.path2} · Resync prefers ${item.resyncMode}${scheduleText}`,
                [this._button('Edit', () => this._bisyncForm(item)), remove]);
            this._makeReorderable(row, 'bisyncs', item.id);
            this._taskList.add(row);
        }
        this._diagnostics();
    }

    _diagnostics() {
        const group = new Adw.PreferencesGroup({title: 'Diagnostics',
            description: 'Off by default. Logs may contain filenames, paths and provider errors. Review before sharing; no credentials or HTTP dumps are requested.'});
        this._diagnosticsPage.add(group);
        let data;
        try {
            data = this._call('diagnostics');
        } catch (error) {
            group.add(this._row('Summary unavailable', error.message, []));
            return;
        }
        group.add(this._row('Installed rclone', data.version, []));
        const logging = new Adw.SwitchRow({title: 'Enable diagnostic logging',
            subtitle: 'Private, size-limited logs for new mounts, tasks, and Bisync runs', active: data.logging});
        logging.connect('notify::active', () => this._perform('save-diagnostics',
            JSON.stringify({logging: logging.active})));
        group.add(logging);
        group.add(this._row('Logs folder', 'Logs can contain private information; not automatically safe to share',
            [this._button('Open', () => this._perform('open-logs'))]));
        const summary = new Adw.PreferencesGroup({title: 'Performance and activity',
            description: 'Configured transfer counts apply to new runs. Mount caching only affects mounts. Unavailable statistics are not zero.'});
        this._diagnosticsPage.add(summary);
        for (const mount of data.mounts) {
            const cache = mount.cache;
            const limits = cache.maxSize === null ? 'inherited rclone limits' :
                `${cache.maxSize} max, ${cache.maxAge} age, ${cache.minFreeSpace} free target`;
            const stats = mount.stats
                ? `${mount.stats.bytes ?? '?'} bytes, ${mount.stats.speed ?? '?'} B/s, ${mount.stats.errors ?? '?'} errors, ${mount.stats.transfers ?? '?'} transfers`
                : 'transfer statistics unavailable';
            const uploads = mount.uploads?.state === 'unknown' || !mount.uploads
                ? 'upload status unknown' : `${mount.uploads.queued} queued, ${mount.uploads.uploading} uploading, ${mount.uploads.failed} failed, ${mount.uploads.retried} retried`;
            summary.add(this._row(`Mount · ${mount.label}`, `8 transfers / 16 checkers · ${cache.mode} · ${limits} · ${stats} · ${uploads}`,
                mount.log ? [this._button('Open log', () => this._perform('open-mount-log', mount.id))] : []));
            summary.add(this._row('Cache root', mount.cacheDir || 'Inherited rclone default cache root', []));
            if (mount.log)
                summary.add(this._row('Mount log location', mount.log, []));
        }
        for (const task of data.tasks) {
            const stats = task.stats
                ? `${task.stats.bytes ?? '?'} bytes, ${task.stats.speed ?? '?'} B/s, ${task.stats.errors ?? '?'} errors, ${task.stats.transfers ?? '?'} transfers`
                : 'transfer statistics unavailable';
            summary.add(this._row(`Copy/sync · ${task.name}`,
                `${task.transfers} transfers / ${task.checkers} checkers · destination ${task.destination || 0}/${task.total || task.destinations} · ${task.status} · ${stats}`,
                task.log ? [this._button('Open log', () => this._perform('open-task-log', task.id))] : []));
            if (task.log)
                summary.add(this._row('Task log location', task.log, []));
        }
        for (const item of data.bisyncs || []) {
            const stats = item.stats
                ? `${item.stats.bytes ?? '?'} bytes, ${item.stats.speed ?? '?'} B/s, ${item.stats.errors ?? '?'} errors, ${item.stats.transfers ?? '?'} transfers`
                : 'transfer statistics unavailable';
            summary.add(this._row(`Bisync · ${item.name}`,
                `${item.status} · ${item.path1} ↔ ${item.path2} · ${stats}`,
                item.log ? [this._button('Open log', () => this._perform('open-bisync-log', item.id))] : []));
            if (item.log)
                summary.add(this._row('Bisync log location', item.log, []));
        }
    }

    _mountForm(mount = null) {
        if (this._mountEditor)
            this._mountPage.remove(this._mountEditor);
        const group = new Adw.PreferencesGroup({title: mount ? 'Edit mount' : 'Add mount',
            description: 'Editing requires an unmounted entry. Changes take effect on the next mount; nothing is interrupted.'});
        this._mountPage.add(group);
        this._mountEditor = group;
        const label = this._entry('Display name', mount?.label || '');
        const remote = this._entry('Remote path (remote:folder)', mount?.remote || '');
        const path = this._entry('Local mountpoint (absolute or ~/...)', mount?.path || '');
        for (const entry of [label, remote, path])
            group.add(entry);
        const legacy = mount && !mount.cache;
        const cache = mount?.cache || (legacy
            ? {mode: 'writes', maxSize: 'off', maxAge: '1h', minFreeSpace: 'off'}
            : {mode: 'full', maxSize: '5G', maxAge: '24h', minFreeSpace: '10G'});
        const caching = new Adw.PreferencesGroup({title: 'Caching',
            description: legacy
                ? 'Legacy: writes only; size/age/free-space inherit rclone defaults. Editing caching keeps the original rclone cache directory to protect pending uploads.'
                : 'New mount: recommended settings. Cache storage is local disk, not RAM.'});
        this._mountPage.add(caching);
        const mode = this._choice('Cache mode', ['Writes only', 'Reads and writes'], cache.mode === 'full' ? 1 : 0);
        mode.subtitle = 'Full caches downloaded data only as files are accessed; not the entire remote.';
        const noLimit = this._choice('Maximum cache size', ['No limit', 'Custom'], cache.maxSize === 'off' ? 0 : 1);
        noLimit.subtitle = 'Soft limit per mount; no limit disables this safeguard.';
        const maxSize = this._entry('Size when custom (e.g. 5G)', cache.maxSize === 'off' ? '' : cache.maxSize);
        const maxAge = this._entry('Remove unused cache after (e.g. 24h)', cache.maxAge);
        const minFree = this._entry('Minimum free disk space target (e.g. 10G or off)', cache.minFreeSpace);
        let cacheChanged = false;
        mode.connect('notify::selected', () => { cacheChanged = true; });
        noLimit.connect('notify::selected', () => { cacheChanged = true; });
        for (const entry of [maxSize, maxAge, minFree])
            entry.connect('notify::text', () => { cacheChanged = true; });
        for (const row of [mode, noLimit, maxSize, maxAge, minFree])
            caching.add(row);
        caching.add(this._row('Storage limits', 'Soft per-mount size limit; free-space target is not a reservation. Saved changes may still be waiting to upload.', []));
        caching.add(this._row('Cache privacy', 'Local disk, not RAM. Crypt mount caches contain decrypted data.', []));
        caching.add(this._row('Recommended settings', 'Reads and writes · 5G · 24h · 10G', [
            this._button('Restore', () => {
                cacheChanged = true;
                mode.selected = 1;
                noLimit.selected = 1;
                maxSize.text = '5G';
                maxAge.text = '24h';
                minFree.text = '10G';
            }),
        ]));
        group.add(this._row('Save definition', 'This does not mount anything', [
            this._button('Save', () => {
                const payload = {id: mount?.id, label: label.text, remote: remote.text, path: path.text};
                if (!legacy || cacheChanged)
                    payload.cache = {mode: mode.selected === 1 ? 'full' : 'writes',
                        maxSize: noLimit.selected === 0 ? 'off' : maxSize.text.trim(),
                        maxAge: maxAge.text.trim(), minFreeSpace: minFree.text.trim()};
                this._perform('save-mount', JSON.stringify(payload));
            }),
            this._button('Cancel', () => {
                this._mountPage.remove(group);
                this._mountPage.remove(caching);
                this._mountEditor = null;
            }),
        ]));
    }

    _taskForm(task = null) {
        if (this._taskEditor)
            this._taskPage.remove(this._taskEditor);
        const group = new Adw.PreferencesGroup({title: task ? 'Edit task' : 'Add task',
            description: 'Destinations run sequentially in one independent user service. A fresh install starts empty.'});
        this._taskPage.add(group);
        this._taskEditor = group;
        const name = this._entry('Task name', task?.name || '');
        const action = this._choice('Action', ['Copy', 'Sync'], task?.action === 'sync' ? 1 : 0);
        const direction = this._choice('Direction', ['Local → remote (upload)', 'Remote → local (download)'],
            task?.direction === 'download' ? 1 : 0);
        const source = this._entry('Source (local path or remote:path)', task?.source || '');
        const schedule = task?.schedule;
        const frequency = this._choice('Automatic schedule', ['Off', 'Daily', 'Weekly'],
            schedule?.frequency === 'daily' ? 1 : schedule?.frequency === 'weekly' ? 2 : 0);
        frequency.subtitle = 'Runs at local time; a missed run is caught up when the user session returns. Scheduled sync can delete destination-only files.';
        const time = this._entry('Time (24-hour local time, HH:MM)', schedule?.time || '09:00');
        const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
        const weekday = this._choice('Day of week', weekdays,
            Math.max(0, weekdays.findIndex(day => day.toLowerCase() === schedule?.weekday)));
        for (const row of [name, action, direction, source, frequency, time, weekday])
            group.add(row);
        time.visible = frequency.selected !== 0;
        weekday.visible = frequency.selected === 2;
        frequency.connect('notify::selected', () => {
            time.visible = frequency.selected !== 0;
            weekday.visible = frequency.selected === 2;
        });
        const destinations = new Gtk.TextView({wrap_mode: Gtk.WrapMode.NONE, monospace: true,
            top_margin: 8, bottom_margin: 8, left_margin: 8, right_margin: 8});
        destinations.buffer.set_text((task?.destinations || []).join('\n'), -1);
        const scroll = new Gtk.ScrolledWindow({min_content_height: 100, child: destinations});
        group.add(this._row('Destinations · one per line', 'Explicit remote:path or local folder', []));
        group.add(scroll);
        group.add(this._row('Save definition', 'This does not start a transfer', [
            this._button('Save', () => {
                const buffer = destinations.buffer;
                const lines = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), false)
                    .split('\n').map(line => line.trim()).filter(Boolean);
                this._perform('save-task', JSON.stringify({id: task?.id, name: name.text,
                    action: action.selected === 1 ? 'sync' : 'copy',
                    direction: direction.selected === 1 ? 'download' : 'upload',
                    source: source.text, destinations: lines,
                    schedule: frequency.selected === 0 ? null : {
                        frequency: frequency.selected === 1 ? 'daily' : 'weekly',
                        time: time.text.trim(),
                        ...(frequency.selected === 2 ? {weekday: weekdays[weekday.selected].toLowerCase()} : {}),
                    }}));
            }),
            this._button('Cancel', () => {
                this._taskPage.remove(group);
                this._taskEditor = null;
            }),
        ]));
    }

    _bisyncForm(item = null) {
        if (this._bisyncEditor)
            this._taskPage.remove(this._bisyncEditor);
        const group = new Adw.PreferencesGroup({title: item ? 'Edit Bisync pair' : 'Add Bisync pair',
            description: 'Both base folders must already exist. Save does not start a transfer; schedule normal runs or use Resync explicitly to create the initial baseline.'});
        this._taskPage.add(group);
        this._bisyncEditor = group;
        const name = this._entry('Display name', item?.name || '');
        const path1 = this._entry('Path 1 (absolute local folder or remote:path)', item?.path1 || '');
        const path2 = this._entry('Path 2 (absolute local folder or remote:path)', item?.path2 || '');
        const modes = ['Path 1', 'Path 2', 'Newer', 'Older', 'Larger', 'Smaller'];
        const modeValues = ['path1', 'path2', 'newer', 'older', 'larger', 'smaller'];
        const mode = this._choice('Resync conflict preference', modes,
            Math.max(0, modeValues.indexOf(item?.resyncMode || 'path1')));
        mode.subtitle = 'Only applies to Resync. Existing bisync runs use the saved baseline.';
        const schedule = item?.schedule;
        const frequency = this._choice('Automatic schedule', ['Off', 'Daily', 'Weekly'],
            schedule?.frequency === 'daily' ? 1 : schedule?.frequency === 'weekly' ? 2 : 0);
        frequency.subtitle = 'Create the baseline with Resync before the first scheduled run. The timer runs normal Bisync only; Resync is always manual. Changes and deletions propagate both ways.';
        const time = this._entry('Time (24-hour local time, HH:MM)', schedule?.time || '09:00');
        const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
        const weekday = this._choice('Day of week', weekdays,
            Math.max(0, weekdays.findIndex(day => day.toLowerCase() === schedule?.weekday)));
        for (const row of [name, path1, path2, mode, frequency, time, weekday])
            group.add(row);
        time.visible = frequency.selected !== 0;
        weekday.visible = frequency.selected === 2;
        frequency.connect('notify::selected', () => {
            time.visible = frequency.selected !== 0;
            weekday.visible = frequency.selected === 2;
        });
        group.add(this._row('Bisync safety',
            'Regular runs propagate changes and deletions both ways. Resync can overwrite same-named files according to the selected preference. Back up important data first.', []));
        group.add(this._row('Save definition', 'This does not change either folder', [
            this._button('Save', () => this._perform('save-bisync', JSON.stringify({
                id: item?.id,
                name: name.text,
                path1: path1.text,
                path2: path2.text,
                resyncMode: modeValues[mode.selected],
                schedule: frequency.selected === 0 ? null : {
                    frequency: frequency.selected === 1 ? 'daily' : 'weekly',
                    time: time.text.trim(),
                    ...(frequency.selected === 2 ? {weekday: weekdays[weekday.selected].toLowerCase()} : {}),
                },
            }))),
            this._button('Cancel', () => {
                this._taskPage.remove(group);
                this._bisyncEditor = null;
            }),
        ]));
    }
}
