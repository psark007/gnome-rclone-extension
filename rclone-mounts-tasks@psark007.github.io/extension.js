import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const MENU_WIDTH = 480;

function backend(path, ...args) {
    return new Promise((resolve, reject) => {
        let process;
        try {
            process = Gio.Subprocess.new(['/usr/bin/python3', path, ...args],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (error) {
            reject(error);
            return;
        }
        process.communicate_utf8_async(null, null, (proc, result) => {
            try {
                const [, output] = proc.communicate_utf8_finish(result);
                const data = JSON.parse(output);
                if (!proc.get_successful() || !data.ok)
                    throw new Error(data.error || 'Rclone helper failed');
                resolve(data);
            } catch (error) {
                reject(error);
            }
        });
    });
}

function bytes(value) {
    if (value === null || value === undefined)
        return '?';
    const number = Number(value) || 0;
    if (number >= 1024 ** 3)
        return `${(number / 1024 ** 3).toFixed(1)} GiB`;
    if (number >= 1024 ** 2)
        return `${(number / 1024 ** 2).toFixed(1)} MiB`;
    if (number >= 1024)
        return `${(number / 1024).toFixed(1)} KiB`;
    return `${Math.round(number)} B`;
}

function recentRun(entry, prefix) {
    const outcome = {Completed: 'Succeeded', Failed: 'Failed', Stopped: 'Stopped',
        Interrupted: 'Interrupted'}[entry.status] || entry.status;
    const time = entry.finishedAt || entry.startedAt;
    const date = time ? new Date(time) : null;
    const when = date && !Number.isNaN(date.getTime())
        ? date.toLocaleString(undefined, {year: '2-digit', month: 'numeric', day: 'numeric',
            hour: 'numeric', minute: '2-digit'})
        : 'time unavailable';
    const reason = entry.status === 'Failed' && entry.exitCode !== null && entry.exitCode !== undefined
        ? ` (exit ${entry.exitCode})` : entry.status === 'Failed' && entry.failure
            ? ` (${entry.failure})` : '';
    const summary = `${prefix}: ${outcome}${reason} · ${entry.status === 'Interrupted' && !entry.finishedAt
        ? `started ${when}; end unknown` : when}`;
    const info = [];
    if (entry.startedAt && entry.finishedAt) {
        const seconds = Math.round((Date.parse(entry.finishedAt) - Date.parse(entry.startedAt)) / 1000);
        if (Number.isFinite(seconds) && seconds >= 0)
            info.push(seconds >= 3600 ? `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`
                : seconds >= 60 ? `${Math.floor(seconds / 60)}m` : `${seconds}s`);
    }
    if (entry.reverse)
        info.push('reverse');
    if (entry.total)
        info.push(entry.destination
            ? `${entry.status === 'Completed' ? '' : 'at '}${entry.destination}/${entry.total} destinations`
            : 'before first destination');
    return {summary, info: info.join(' · ')};
}

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Rclone Mounts & Tasks');
        this._extension = extension;
        this._backend = `${extension.path}/backend.py`;
        this._stopped = false;
        this._refreshing = false;
        this._busy = false;
        this._pending = null;
        this._waitMount = null;
        this._clearSamples = 0;
        this._tab = 'mounts';
        this._data = {mounts: [], tasks: [], activeTaskUnits: []};
        this._message = '';
        this._panelBox = new St.BoxLayout({orientation: Clutter.Orientation.HORIZONTAL,
            y_align: Clutter.ActorAlign.CENTER, style_class: 'rclone-indicator'});
        this._icon = new St.Icon({icon_name: 'folder-remote-symbolic', style_class: 'system-status-icon'});
        this._activity = new St.Label({text: '', visible: false, y_align: Clutter.ActorAlign.CENTER,
            style_class: 'rclone-count'});
        this._panelBox.add_child(this._icon);
        this._panelBox.add_child(this._activity);
        this.add_child(this._panelBox);
        this.menu.actor.add_style_class_name('rclone-popup');
        this._draw();
        this._refresh();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    async _refresh() {
        if (this._stopped || this._refreshing)
            return;
        this._refreshing = true;
        try {
            const data = await backend(this._backend, 'snapshot');
            if (this._stopped)
                return;
            this._data = data;
            if (this._waitMount) {
                const mount = data.mounts.find(entry => entry.id === this._waitMount);
                this._clearSamples = mount?.mounted && mount.uploads?.state === 'clear'
                    ? this._clearSamples + 1 : 0;
                if (!mount?.mounted)
                    this._waitMount = null;
            }
            this._updateIndicator();
            this._draw();
        } catch (error) {
            if (!this._stopped) {
                this._message = `Status unavailable: ${error.message}`;
                this._draw();
            }
        } finally {
            this._refreshing = false;
        }
    }

    _updateIndicator() {
        const mounted = this._data.mounts.filter(mount => mount.mounted).length;
        // Include active task services without a saved definition in the count.
        const running = this._data.activeTaskUnits.length;
        const parts = [];
        if (mounted)
            parts.push(`${mounted}M`);
        if (running)
            parts.push(`${running}T`);
        this._activity.text = parts.join(' · ');
        this._activity.visible = parts.length > 0;
        this._icon.icon_name = running
            ? 'network-transmit-receive-symbolic'
            : 'folder-remote-symbolic';
        this.accessible_name = `Rclone: ${mounted} mounted, ${running} transfers running`;
    }

    async _action(action, id) {
        if (this._busy)
            return;
        this._busy = true;
        if (action === 'unmount')
            this._waitMount = null;
        this._pending = null;
        this._message = 'Working…';
        this._draw();
        try {
            const result = await backend(this._backend, action, id);
            if (this._stopped)
                return;
            this._message = result.message || 'Done';
        } catch (error) {
            if (this._stopped)
                return;
            this._message = error.message;
        }
        this._busy = false;
        await this._refresh();
        if (!this._stopped)
            this._draw();
    }

    _item(text, callback, sensitive = true) {
        const item = new PopupMenu.PopupMenuItem(text);
        item.label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        item.setSensitive(sensitive);
        if (callback)
            item.connect('activate', callback);
        this.menu.addMenuItem(item);
    }

    _row(title, subtitle, controls) {
        const row = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const text = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
            style_class: 'rclone-row-text',
        });
        const titleLabel = new St.Label({text: title, x_expand: true});
        titleLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        text.add_child(titleLabel);
        for (const line of Array.isArray(subtitle) ? subtitle : subtitle ? [subtitle] : []) {
            const subtitleLabel = new St.Label({text: line, x_expand: true, style_class: 'dim-label'});
            subtitleLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
            text.add_child(subtitleLabel);
        }
        row.add_child(text);
        for (const control of controls) {
            const button = new St.Button({
                style_class: `button rclone-row-button${control.icon ? ' rclone-icon-button' : ''}${control.kind ? ` rclone-button-${control.kind}` : ''}`,
                ...(control.icon ? {} : {label: control.label}),
                reactive: control.enabled !== false && !this._busy,
                can_focus: control.enabled !== false && !this._busy,
                accessible_name: control.name || control.label,
            });
            if (control.icon)
                button.set_child(new St.Icon({icon_name: control.icon, style_class: 'popup-menu-icon'}));
            if (control.enabled !== false && !this._busy)
                button.connect('clicked', control.activate);
            else
                button.opacity = 128;
            row.add_child(button);
        }
        this.menu.addMenuItem(row);
    }

    _tabs(mounts, tasks) {
        const row = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const mounted = mounts.filter(mount => mount.mounted).length;
        const running = tasks.filter(task => task.running).length;
        for (const [tab, label] of [
            ['mounts', `Mounts · ${mounted}/${mounts.length}`],
            ['tasks', `Tasks · ${tasks.length}${running ? ` (${running} running)` : ''}`],
        ]) {
            const button = new St.Button({
                label,
                style_class: 'rclone-tab',
                x_expand: true,
                can_focus: true,
                // Selection belongs to the current page, not to individual clicks.
                toggle_mode: false,
                checked: this._tab === tab,
            });
            button.connect('clicked', () => {
                if (this._tab === tab)
                    return;
                this._tab = tab;
                this._pending = null;
                this._message = '';
                this._draw();
            });
            row.add_child(button);
        }
        this.menu.addMenuItem(row);
    }

    _confirm(action, id) {
        if (this._pending?.action === action && this._pending.id === id) {
            this._action(action, id);
        } else {
            this._pending = {action, id};
            this._message = '';
            this._draw();
        }
    }

    _waitForUnmount(id) {
        this._waitMount = id;
        this._clearSamples = 0;
        this._message = 'Waiting for uploads. Stop writing to this mount; you can cancel here.';
        this._draw();
        this._refresh();
    }

    _draw() {
        if (this._stopped)
            return;
        this.menu.removeAll();
        this.menu.actor.set_width(MENU_WIDTH);
        const {mounts, tasks} = this._data;
        this._tabs(mounts, tasks);
        if (this._tab === 'mounts') {
            if (!mounts.length)
                this._item('No entries · add one in Preferences', null, false);
            for (const mount of mounts) {
                let details = mount.mounted
                    ? mount.stats
                        ? `Mount transfer: ${bytes(mount.stats.speed)}/s · ${bytes(mount.stats.bytes)} · ${mount.stats.errors ?? '?'} errors`
                        : 'Mount transfer stats unavailable'
                    : mount.status === 'Starting' ? 'Starting…' : null;
                if (mount.mounted) {
                    const uploads = mount.uploads;
                    const status = uploads?.state === 'unknown' || !uploads
                        ? 'Uploads unknown (RC unavailable)'
                        : uploads.state === 'clear' ? 'Uploads: none reported; open/new writes still possible'
                            : `Uploads: ${uploads.queued} queued · ${uploads.uploading} uploading · ${uploads.failed} failed`;
                    details = [details, this._waitMount === mount.id ? `Waiting · ${status}` : status];
                }
                const controls = mount.mounted
                    ? [
                        {icon: 'folder-open-symbolic', name: `Open ${mount.label} in Files`,
                            activate: () => this._action('open', mount.id)},
                        {label: this._waitMount === mount.id
                            ? this._clearSamples >= 2 ? 'Unmount now' : 'Waiting…' : 'Unmount',
                        enabled: this._waitMount !== mount.id || this._clearSamples >= 2,
                        activate: () => this._waitMount === mount.id
                            ? this._action('unmount', mount.id) : this._waitForUnmount(mount.id)},
                        ...(this._waitMount === mount.id
                            ? [{label: 'Cancel', activate: () => {
                                this._waitMount = null;
                                this._message = 'Unmount request cancelled; mount still running.';
                                this._draw();
                            }}] : []),
                        ...(mount.log ? [{icon: 'text-x-generic-symbolic', name: `Open ${mount.label} log`,
                            activate: () => this._action('open-mount-log', mount.id)}] : []),
                    ]
                    : [{label: mount.status === 'Starting' ? 'Starting…' : 'Mount',
                        kind: 'primary',
                        enabled: mount.status !== 'Starting',
                        activate: () => this._action('mount', mount.id)},
                    ...(mount.log ? [{icon: 'text-x-generic-symbolic', name: `Open ${mount.label} log`,
                        activate: () => this._action('open-mount-log', mount.id)}] : [])];
                this._row(mount.label, details, controls);
            }
        } else {
            if (!tasks.length)
                this._item('No tasks · add one in Preferences', null, false);
            for (const task of tasks) {
                const controls = [];
                const details = [];
                const history = task.history || [];
                if (task.running) {
                    const index = task.destination ? ` ${task.destination}/${task.total}` : '';
                    const progress = task.stats
                        ? `${bytes(task.stats.bytes)} / ${bytes(task.stats.totalBytes)} · ${bytes(task.stats.speed)}/s · ${task.stats.errors ?? '?'} errors`
                        : 'Progress unavailable';
                    details.push(task.status === 'Starting' ? 'Starting transfer…'
                        : `Copy/sync destination${index || ' ?'} · ${progress}`);
                    const stopping = this._pending?.action === 'stop' && this._pending.id === task.id;
                    if (stopping)
                        details[0] = 'Stop this transfer? Incomplete files may remain.';
                    controls.push({label: stopping ? 'Confirm Stop' : 'Stop', kind: 'danger',
                        activate: () => this._confirm('stop', task.id)});
                } else {
                    const running = this._pending?.action === 'run' && this._pending.id === task.id;
                    const reversed = this._pending?.action === 'reverse' && this._pending.id === task.id;
                    if (running || reversed)
                        details.push(task.action === 'sync'
                            ? 'Sync can delete destination-only files. Confirm to proceed.'
                            : reversed ? 'Reverse swaps source and destination for this run.'
                                : 'Confirm this manual copy.');
                    controls.push({label: running ? `Confirm ${task.action.toUpperCase()}` : 'Run', kind: 'primary',
                        activate: () => this._confirm('run', task.id)});
                    if (task.destinations === 1) {
                        controls.push({label: reversed ? 'Confirm Reverse' : 'Reverse',
                            activate: () => this._confirm('reverse', task.id)});
                    }
                }
                if (!task.running && task.status === 'Failed' && !history.length)
                    details.push(task.exitCode !== null && task.exitCode !== undefined
                        ? `Copy/sync failed at ${task.destination || '?'}/${task.total || task.destinations} (exit ${task.exitCode})${task.log ? ' · Log available' : ''}`
                        : `Copy/sync failed: ${task.failure || 'see service journal'}${task.log ? ' · Log available' : ''}`);
                if (history.length) {
                    const latest = recentRun(history[0], 'Last');
                    details.push(latest.summary);
                    if (!task.running && this._pending?.id !== task.id && latest.info)
                        details.push(latest.info);
                    if (history.length > 1)
                        details.push(recentRun(history[1], 'Before').summary);
                } else if (!task.running && !details.length) {
                    details.push('Never run');
                }
                if (task.log)
                    controls.push({icon: 'text-x-generic-symbolic', name: `Open ${task.name} log`,
                        activate: () => this._action('open-task-log', task.id)});
                this._row(`${task.name} · ${task.action.toUpperCase()} · ${task.status}`, details, controls);
            }
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        if (this._message)
            this._item(this._message, null, false);
        this._row('', null, [
            {label: 'Preferences…', activate: () => this._extension.openPreferences()},
            {icon: 'view-refresh-symbolic', name: 'Refresh status', activate: () => this._refresh()},
        ]);
    }

    destroy() {
        this._stopped = true;
        if (this._timer) {
            GLib.Source.remove(this._timer);
            this._timer = 0;
        }
        super.destroy();
    }
});

export default class RcloneExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator.destroy();
        this._indicator = null;
    }
}
