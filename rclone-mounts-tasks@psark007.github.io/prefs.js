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
        this._mountEditor = null;
        this._taskEditor = null;
        this._mountPage = new Adw.PreferencesPage({title: 'Mounts', icon_name: 'folder-remote-symbolic'});
        this._taskPage = new Adw.PreferencesPage({title: 'Tasks', icon_name: 'document-send-symbolic'});
        this._window.add(this._mountPage);
        this._window.add(this._taskPage);
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
    }

    _mountForm(mount = null) {
        if (this._mountEditor)
            this._mountPage.remove(this._mountEditor);
        const group = new Adw.PreferencesGroup({title: mount ? 'Edit mount' : 'Add mount',
            description: 'Editing requires an unmounted entry. No Rclone remotes are discovered.'});
        this._mountPage.add(group);
        this._mountEditor = group;
        const label = this._entry('Display name', mount?.label || '');
        const remote = this._entry('Remote path (remote:folder)', mount?.remote || '');
        const path = this._entry('Local mountpoint (absolute or ~/...)', mount?.path || '');
        for (const entry of [label, remote, path])
            group.add(entry);
        group.add(this._row('Save definition', 'This does not mount anything', [
            this._button('Save', () => this._perform('save-mount', JSON.stringify({
                id: mount?.id, label: label.text, remote: remote.text, path: path.text,
            }))),
            this._button('Cancel', () => {
                this._mountPage.remove(group);
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
