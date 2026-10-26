// Lightweight Shell-widget mock: verify tab switching without loading private definitions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class Actor {
    constructor(properties = {}) {
        Object.assign(this, properties);
        this.children = [];
        this.signals = {};
    }

    add_child(child) {
        this.children.push(child);
    }

    set_child(child) {
        this.children = [child];
    }

    connect(signal, callback) {
        this.signals[signal] = callback;
    }
}

class Button extends Actor {
    click() {
        // St.Button updates checked before emitting clicked in toggle mode.
        if (this.toggle_mode)
            this.checked = !this.checked;
        this.signals.clicked?.();
    }
}

class BoxLayout extends Actor {
    constructor(properties) {
        assert.ok(!Object.hasOwn(properties, 'vertical'), 'GNOME 51 St.BoxLayout has no vertical property');
        super(properties);
    }
}

class Label extends Actor {
    constructor(properties) {
        super(properties);
        this.clutter_text = {};
    }
}

class PopupItem extends Actor {
    constructor(properties) {
        super(typeof properties === 'object' ? properties : {});
        this.label = new Label({text: typeof properties === 'string' ? properties : ''});
    }

    setSensitive(sensitive) {
        this.sensitive = sensitive;
    }
}

class Menu {
    constructor() {
        this.items = [];
        this.actor = {width: 0, set_width(width) { this.width = width; }};
    }

    removeAll() {
        this.items = [];
    }

    addMenuItem(item) {
        this.items.push(item);
    }
}

const source = fs.readFileSync(path.join(__dirname, '../rclone-mounts-tasks@psark007.github.io/extension.js'), 'utf8')
    .replace(/^import .*;\s*$/gm, '')
    .replace('export default class RcloneExtension', 'class RcloneExtension');
const context = {
    Clutter: {Orientation: {VERTICAL: 1}},
    Extension: class {},
    Gio: {}, GLib: {}, GObject: {registerClass: klass => klass},
    Pango: {EllipsizeMode: {END: 3}},
    St: {BoxLayout, Button, Icon: Actor, Label},
    PanelMenu: {Button: class {}},
    PopupMenu: {PopupBaseMenuItem: PopupItem, PopupMenuItem: PopupItem,
        PopupSeparatorMenuItem: PopupItem},
};
vm.runInNewContext(`${source}\nglobalThis.IndicatorForTest = Indicator;`, context);

function indicator() {
    const instance = Object.create(context.IndicatorForTest.prototype);
    instance.menu = new Menu();
    instance._stopped = false;
    instance._busy = false;
    instance._pending = null;
    instance._tab = 'mounts';
    instance._message = '';
    instance._data = {
        mounts: [{id: 'm1', label: 'Example mount', mounted: true, status: 'Mounted',
            stats: {speed: 0, bytes: 0, errors: 0}}],
        tasks: [{id: 't1', name: 'Example task', action: 'copy', status: 'Ready',
            destinations: 1, running: false}],
        activeTaskUnits: [],
    };
    instance._extension = {openPreferences() {}};
    return instance;
}

function labels(instance) {
    return instance.menu.items.flatMap(item => item.children.flatMap(child =>
        [child.text, child.label, ...child.children.map(grandchild => grandchild.text || grandchild.label)]))
        .filter(Boolean);
}

test('tabs show only their own compact rows and survive redraw', () => {
    const item = indicator();
    item._draw();
    const width = item.menu.actor.width;
    assert.equal(width, 480);
    assert.ok(labels(item).includes('Example mount'));
    assert.ok(!labels(item).some(label => label.includes('Example task')));
    const taskTab = item.menu.items[0].children[1];
    assert.equal(taskTab.style_class, 'rclone-tab');
    taskTab.signals.clicked();
    assert.equal(item._tab, 'tasks');
    assert.equal(item.menu.actor.width, width);
    assert.ok(labels(item).some(label => label.includes('Example task')));
    assert.ok(!labels(item).includes('Example mount'));
    item._draw(); // periodic status refresh
    assert.equal(item._tab, 'tasks');
    assert.equal(item.menu.items[0].children[1].checked, true);
});

test('switching tabs clears an unconfirmed task action', () => {
    const item = indicator();
    item._tab = 'tasks';
    item._pending = {action: 'run', id: 't1'};
    item._draw();
    item.menu.items[0].children[0].signals.clicked();
    assert.equal(item._tab, 'mounts');
    assert.equal(item._pending, null);
});

test('long task labels ellipsize without changing the menu width', () => {
    const item = indicator();
    item._data.tasks[0].name = 'Long task '.repeat(60);
    item._tab = 'tasks';
    item._draw();
    assert.equal(item.menu.actor.width, 480);
    const taskRow = item.menu.items[1];
    assert.equal(taskRow.children[0].children[0].clutter_text.ellipsize, 3);
    item._message = 'A very long status message '.repeat(50);
    item._draw();
    assert.equal(item.menu.actor.width, 480);
    assert.equal(item.menu.items[3].label.clutter_text.ellipsize, 3);
});

test('panel badge counts mounted entries and all running task units', () => {
    const item = indicator();
    item._activity = {text: '', visible: false};
    item._icon = {icon_name: 'folder-remote-symbolic'};
    item._data.activeTaskUnits = ['gnome-rclone-task-example.service', 'rclone-task-external.service'];
    item._updateIndicator();
    assert.equal(item._activity.text, '1M · 2T');
    assert.equal(item._activity.visible, true);
    assert.equal(item._icon.icon_name, 'network-transmit-receive-symbolic');
    assert.equal(item.accessible_name, 'Rclone: 1 mounted, 2 transfers running');
    item._data.mounts = [];
    item._data.activeTaskUnits = [];
    item._updateIndicator();
    assert.equal(item._activity.visible, false);
    assert.equal(item._icon.icon_name, 'folder-remote-symbolic');
});

test('a running task does not disable controls for another task', () => {
    const item = indicator();
    item._tab = 'tasks';
    item._data.activeTaskUnits = ['gnome-rclone-task-other.service'];
    item._data.tasks.unshift({id: 'other', name: 'Other task', action: 'copy',
        status: 'Running', destinations: 1, running: true});
    item._draw();
    const taskRow = item.menu.items[2];
    const buttons = taskRow.children.slice(1);
    assert.deepEqual(buttons.map(button => button.label), ['Run', 'Reverse']);
    assert.ok(buttons.every(button => button.reactive && button.signals.clicked));
    assert.ok(!labels(item).some(label => label.includes('new runs are blocked')));
});

test('waiting to unmount is cancellable without invoking the backend', () => {
    const item = indicator();
    item._data.mounts[0].uploads = {state: 'pending', queued: 2, uploading: 1, failed: 0};
    let calls = 0;
    item._refresh = () => {};
    item._action = () => { calls++; };
    item._draw();
    item.menu.items[1].children[2].signals.clicked(); // Unmount arms a wait, not a detach
    assert.equal(item._waitMount, 'm1');
    assert.equal(calls, 0);
    const buttons = item.menu.items[1].children.slice(1);
    assert.equal(buttons[1].reactive, false);
    assert.equal(buttons[2].label, 'Cancel');
    buttons[2].signals.clicked();
    assert.equal(item._waitMount, null);
    assert.equal(calls, 0);
    item._waitForUnmount('m1');
    item._data.mounts[0].uploads = {state: 'clear', queued: 0, uploading: 0, failed: 0};
    item._clearSamples = 2;
    item._draw();
    item.menu.items[1].children[2].signals.clicked();
    assert.equal(calls, 1);
});

test('failed tasks show exit code and retained log action', () => {
    const item = indicator();
    item._tab = 'tasks';
    Object.assign(item._data.tasks[0], {status: 'Failed', exitCode: 7, log: '/private/log'});
    item._draw();
    assert.ok(labels(item).some(label => label.includes('exit 7')));
    assert.equal(item.menu.items[1].children.at(-1).accessible_name, 'Open Example task log');
});

test('task rows show recent results and dates without widening the menu', () => {
    const item = indicator();
    item._tab = 'tasks';
    item._data.tasks[0].history = [
        {status: 'Completed', startedAt: '2026-10-02T10:00:00Z',
            finishedAt: '2026-10-02T10:04:00Z', destination: 2, total: 2, reverse: true},
        {status: 'Failed', startedAt: '2026-10-01T09:00:00Z',
            finishedAt: '2026-10-01T09:01:00Z', destination: 1, total: 2, exitCode: 7},
    ];
    item._draw();
    const row = item.menu.items[1];
    const lines = row.children[0].children.map(label => label.text);
    assert.equal(lines.length, 4);
    assert.match(lines[1], /Last: Succeeded/);
    assert.ok(lines[1].includes(new Date('2026-10-02T10:04:00Z').toLocaleString(undefined,
        {year: '2-digit', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit'})));
    assert.match(lines[2], /4m · reverse · 2\/2 destinations/);
    assert.match(lines[3], /Before: Failed \(exit 7\)/);
    assert.equal(item.menu.actor.width, 480);
    item._data.tasks[0].running = true;
    item._data.tasks[0].status = 'Running';
    item._data.tasks[0].destination = 1;
    item._data.tasks[0].total = 2;
    item._draw();
    const runningLines = item.menu.items[1].children[0].children.map(label => label.text);
    assert.equal(runningLines.length, 4);
    assert.match(runningLines[1], /destination 1\/2/);
    assert.match(runningLines[2], /Last: Succeeded/);
    assert.match(runningLines[3], /Before: Failed/);
});

test('new tasks show Never run instead of an invented result', () => {
    const item = indicator();
    item._tab = 'tasks';
    item._draw();
    assert.ok(labels(item).includes('Never run'));
});


test('clicking the active tab keeps it selected and preserves pending actions', () => {
    const item = indicator();
    for (const tab of ['mounts', 'tasks']) {
        item._tab = tab;
        const pending = {action: 'run', id: 't1'};
        item._pending = pending;
        item._message = 'Keep this message';
        item._draw();
        const tabs = item.menu.items[0].children;
        const selected = tabs[tab === 'mounts' ? 0 : 1];
        for (let count = 0; count < 3; count++) {
            selected.click();
            assert.equal(selected.checked, true);
            assert.equal(item._tab, tab);
            assert.equal(item._pending, pending);
            assert.equal(item._message, 'Keep this message');
            assert.equal(tabs[tab === 'mounts' ? 1 : 0].checked, false);
        }
        tabs[tab === 'mounts' ? 1 : 0].click();
        assert.equal(item._tab, tab === 'mounts' ? 'tasks' : 'mounts');
        assert.equal(item._pending, null);
        assert.equal(item.menu.items[0].children[tab === 'mounts' ? 1 : 0].checked, true);
    }
});
