import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class RclonePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        this._window = window;
        this._helper = `${this.path}/backend.py`;
        window.set_default_size(660, 650);
        this._render();
    }

    _call(...args) {
        const process = Gio.Subprocess.new(['/usr/bin/python3', this._helper, ...args],
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

    _render() {
        if (this._mountPage)
            this._window.remove(this._mountPage);
        if (this._taskPage)
            this._window.remove(this._taskPage);
        if (this._diagnosticsPage)
            this._window.remove(this._diagnosticsPage);
        this._mountEditor = null;
        this._taskEditor = null;
        this._mountPage = new Adw.PreferencesPage({title: 'Mounts', icon_name: 'folder-remote-symbolic'});
        this._taskPage = new Adw.PreferencesPage({title: 'Tasks', icon_name: 'document-send-symbolic'});
        this._diagnosticsPage = new Adw.PreferencesPage({title: 'Diagnostics', icon_name: 'dialog-information-symbolic'});
        this._window.add(this._mountPage);
        this._window.add(this._taskPage);
        this._window.add(this._diagnosticsPage);
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
            this._mountList.add(this._row(mount.label, `${mount.remote} → ${mount.path}`,
                [this._button('Edit', () => this._mountForm(mount)), remove]));
        }
        this._taskList = new Adw.PreferencesGroup({title: 'Saved manual tasks',
            description: 'Copy preserves destination-only files. Sync deletes them. Reverse swaps source and destination for one run.'});
        this._taskPage.add(this._taskList);
        this._taskList.add(this._row('Add task', 'No transfer starts when saving',
            [this._button('Add', () => this._taskForm())]));
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
            this._taskList.add(this._row(task.name,
                `${task.action.toUpperCase()} · ${task.direction} · ${task.source} → ${task.destinations.join('; ')}`,
                [this._button('Edit', () => this._taskForm(task)), remove]));
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
            subtitle: 'Private, size-limited logs for new mounts and copy/sync runs', active: data.logging});
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
        for (const row of [name, action, direction, source])
            group.add(row);
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
                    source: source.text, destinations: lines}));
            }),
            this._button('Cancel', () => {
                this._taskPage.remove(group);
                this._taskEditor = null;
            }),
        ]));
    }
}
