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
    St: {BoxLayout, Button: Actor, Icon: Actor, Label},
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
