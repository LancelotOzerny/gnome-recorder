#!/usr/bin/env python3
"""Safely add/remove just our GNOME custom keyboard shortcuts."""
import ast
import subprocess
import sys

SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys'
PREFIX = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
NAMES = [('start', 'Запись — начать', '<Control><Alt>r', '--start'),
         ('pause', 'Запись — пауза', '<Control><Alt>p', '--pause'),
         ('stop', 'Запись — остановить', '<Control><Alt>s', '--stop')]


def gs(*args):
    return subprocess.check_output(['gsettings', *args], text=True).strip()


def parse_paths(raw):
    return ast.literal_eval(raw.removeprefix('@as '))


def set_paths(paths):
    entries = ', '.join("'" + p + "'" for p in paths)
    subprocess.check_call(['gsettings', 'set', SCHEMA, 'custom-keybindings', '[' + entries + ']'])


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: shortcuts.py install recorder.py | uninstall')
    action = sys.argv[1]
    paths = parse_paths(gs('get', SCHEMA, 'custom-keybindings'))
    ours = [PREFIX + 'gnome-recorder-' + n + '/' for n, _, _, _ in NAMES]
    if action == 'uninstall':
        set_paths([p for p in paths if p not in ours])
        return
    script = sys.argv[2]
    for (name, label, binding, arg), path in zip(NAMES, ours):
        schema = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:' + path
        subprocess.check_call(['gsettings', 'set', schema, 'name', label])
        subprocess.check_call(['gsettings', 'set', schema, 'command',
                               '/usr/bin/python3 ' + script + ' ' + arg])
        subprocess.check_call(['gsettings', 'set', schema, 'binding', binding])
        if path not in paths:
            paths.append(path)
    set_paths(paths)


if __name__ == '__main__':
    main()
