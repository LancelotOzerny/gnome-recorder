#!/usr/bin/env python3
"""GNOME Recorder: GTK3/AppIndicator screen and audio recorder for Wayland."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# A terminal launched by VS Code's Snap can export its private GTK modules,
# schema directory and XDG data directories. They may be incompatible with the
# system GTK/GNOME libraries, causing GTK to abort before our window is created.
snap_original_data_dirs = os.environ.pop('XDG_DATA_DIRS_VSCODE_SNAP_ORIG', None)
if snap_original_data_dirs:
    os.environ['XDG_DATA_DIRS'] = snap_original_data_dirs
for env_name in ('GSETTINGS_SCHEMA_DIR', 'GTK_PATH', 'GTK_EXE_PREFIX',
                 'GIO_MODULE_DIR', 'GIO_EXTRA_MODULES', 'GTK_IM_MODULE_FILE'):
    os.environ.pop(env_name, None)

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gtk, GLib, Gio, Gst

try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ImportError, ValueError):
    try:
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3 as AppIndicator
    except (ImportError, ValueError):
        AppIndicator = None

Gst.init(None)
APP_ID = 'io.github.lancy.GnomeRecorder'
BASE = Path(__file__).resolve().parent
CONFIG = Path.home() / '.config' / 'gnome-recorder' / 'settings.json'
DEFAULTS = {
    'video_dir': str(Path.home() / 'Видео' / 'Записи экрана'),
    'audio_dir': str(Path.home() / 'Музыка' / 'Записи аудио'),
    'cursor': True,
    'audio_in_video': False,
    'merge_audio_video': True,
    'audio_format': 'wav',
    'video_format': 'mp4',
    'open_folder': False,
    'audio_sources': [],
    'shortcut_start': '<Control><Alt>r',
    'shortcut_pause': '<Control><Alt>p',
    'shortcut_stop': '<Control><Alt>s',
    'area_x': 0, 'area_y': 0, 'area_width': 1280, 'area_height': 720,
}


def load_settings():
    data = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG.read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in data})
    except (OSError, ValueError):
        pass
    return data


def save_settings(data):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def audio_devices():
    """Return PulseAudio/PipeWire sources with their user-facing device names."""
    try:
        out = subprocess.check_output(
            ['pactl', '--format=json', 'list', 'sources'], text=True, timeout=5)
        sources = json.loads(out)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return []
    if not isinstance(sources, list):
        return []

    result = []
    for source in sources:
        name = source.get('name')
        if not name:
            continue
        properties = source.get('properties', {})
        # device.description is the name shown by GNOME/PipeWire, e.g. "TDS Core".
        # Keep the technical source name as the value used by pulsesrc only.
        description = (properties.get('device.description')
                       or properties.get('node.description')
                       or properties.get('device.product.name')
                       or name)
        is_monitor = name.endswith('.monitor') or 'monitor_of_sink' in source
        kind = 'Системный звук' if is_monitor else 'Микрофон'
        label = f'{kind}: {description}'
        result.append((name, label))
    return result


def gst_quote(text):
    # GStreamer launch string syntax; paths may include spaces/non-ASCII characters.
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'


class PortalSession:
    BUS = 'org.freedesktop.portal.Desktop'
    PATH = '/org/freedesktop/portal/desktop'
    IFACE = 'org.freedesktop.portal.ScreenCast'

    def __init__(self, done):
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.done = done
        self.session = None
        self.fd = None
        self.pending = {}
        self.subscriptions = []
        self.closed_subscription = None

    def _request(self, method, parameters, callback):
        index = 0 if method == 'CreateSession' else (1 if method == 'SelectSources' else 2)
        token = parameters.get_child_value(index).lookup_value(
            'handle_token', GLib.VariantType.new('s')).get_string()
        sender = self.conn.get_unique_name()[1:].replace('.', '_')
        predicted = '/org/freedesktop/portal/desktop/request/' + sender + '/' + token
        # Subscribe before sending: portals can answer immediately.
        sid = self.conn.signal_subscribe(self.BUS, 'org.freedesktop.portal.Request', 'Response',
                                         predicted, None, Gio.DBusSignalFlags.NONE,
                                         self._on_response, token)
        self.subscriptions.append(sid)
        self.pending[token] = (sid, callback)
        def returned(conn, result, _):
            try:
                reply = conn.call_finish(result)
                actual = reply.unpack()[0]
                if actual != predicted:
                    # Usually identical when handle_token is accepted.
                    self.conn.signal_unsubscribe(sid)
                    if token in self.pending:
                        new_sid = self.conn.signal_subscribe(self.BUS, 'org.freedesktop.portal.Request',
                                                             'Response', actual, None,
                                                             Gio.DBusSignalFlags.NONE,
                                                             self._on_response, token)
                        self.pending[token] = (new_sid, callback)
                        self.subscriptions.append(new_sid)
            except Exception as exc:
                pair = self.pending.pop(token, None)
                if pair:
                    self.conn.signal_unsubscribe(pair[0])
                    callback(None, str(exc))
        self.conn.call(self.BUS, self.PATH, self.IFACE, method, parameters,
                       GLib.VariantType.new('(o)'), Gio.DBusCallFlags.NONE, -1,
                       None, returned, None)

    def _on_response(self, conn, sender, path, interface, signal, parameters, token):
        pair = self.pending.pop(token, None)
        if not pair:
            return
        sid, callback = pair
        self.conn.signal_unsubscribe(sid)
        code, result = parameters.unpack()
        if code != 0:
            callback(None, 'Выбор отменён' if code == 1 else 'Портал отказал в доступе')
        else:
            callback(result, None)

    def start(self, mode, cursor):
        token = 'rec' + uuid.uuid4().hex
        self._request('CreateSession', GLib.Variant('(a{sv})', [{
            'handle_token': GLib.Variant('s', token),
            'session_handle_token': GLib.Variant('s', 'sess' + uuid.uuid4().hex),
        }]), lambda result, error: self._created(result, error, mode, cursor))

    def _created(self, result, error, mode, cursor):
        if error:
            self.done(None, error)
            return
        self.session = result['session_handle']
        self.closed_subscription = self.conn.signal_subscribe(self.BUS, 'org.freedesktop.portal.Session',
            'Closed', self.session, None, Gio.DBusSignalFlags.NONE, self._closed)
        # cursor_mode: 1 hidden, 2 embedded; types: 1 monitors, 2 windows.
        opts = {'handle_token': GLib.Variant('s', 'rec' + uuid.uuid4().hex),
                'types': GLib.Variant('u', 2 if mode == 'window' else 1),
                'multiple': GLib.Variant('b', False),
                'cursor_mode': GLib.Variant('u', 2 if cursor else 1)}
        self._request('SelectSources', GLib.Variant('(oa{sv})', [self.session, opts]),
                      self._selected)

    def _selected(self, result, error):
        if error:
            self.close()
            self.done(None, error)
            return
        self._request('Start', GLib.Variant('(osa{sv})', [self.session, '', {
            'handle_token': GLib.Variant('s', 'rec' + uuid.uuid4().hex)}]), self._started)

    def _started(self, result, error):
        if error:
            self.close()
            self.done(None, error)
            return
        try:
            streams = result['streams']
            if not streams:
                raise RuntimeError('Портал не предоставил видеопоток')
            node = int(streams[0][0])
            size = streams[0][1].get('size')
            params = GLib.Variant('(oa{sv})', [self.session, {}])
            reply, fd_list = self.conn.call_with_unix_fd_list_sync(
                self.BUS, self.PATH, self.IFACE, 'OpenPipeWireRemote', params,
                GLib.VariantType.new('(h)'), Gio.DBusCallFlags.NONE, -1, None, None)
            fd_idx = reply.unpack()[0]
            self.fd = fd_list.get(fd_idx)
            self.done((node, self.fd, size), None)
        except Exception as exc:
            self.close()
            self.done(None, str(exc))

    def _closed(self, *args):
        self.session = None

    def close(self):
        if self.session:
            try:
                self.conn.call(self.BUS, self.session, 'org.freedesktop.portal.Session',
                               'Close', None, None, Gio.DBusCallFlags.NONE, -1, None, None, None)
            except Exception:
                pass
            self.session = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.closed_subscription:
            self.conn.signal_unsubscribe(self.closed_subscription)
            self.closed_subscription = None
        for sid, _ in self.pending.values():
            self.conn.signal_unsubscribe(sid)
        self.pending.clear()


def has_encoder(name):
    return Gst.ElementFactory.find(name) is not None


def audio_mix_chain(sources, encoder):
    """Returns a GStreamer launch string for 1+ audio devices mixed to one stream."""
    if not sources:
        return None
    head = 'audiomixer name=amix ! audioconvert ! audioresample ! ' + encoder
    tails = ' '.join('pulsesrc device=' + gst_quote(src) +
                     ' do-timestamp=true ! queue ! audioconvert ! audioresample ! amix.'
                     for src in sources)
    return head, tails


class Recording:
    def __init__(self, finished, error):
        self.pipelines = []
        self.pending_eos = set()
        self.finished_cb = finished
        self.error_cb = error
        self.paths = []
        self.stopping = False
        self.failed = False
        self.elapsed = 0.0
        self.started_at = None

    def _add(self, description, path):
        pipeline = Gst.parse_launch(description)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._bus_msg, pipeline)
        self.pipelines.append(pipeline)
        self.pending_eos.add(pipeline)
        self.paths.append(path)

    def _start(self):
        for pipe in self.pipelines:
            if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('Не удалось запустить GStreamer')
        self.started_at = time.monotonic()

    def begin_audio(self, settings, sources):
        if not sources:
            raise ValueError('Выберите хотя бы одно аудиоустройство')
        fmt = settings['audio_format']
        encoder = 'wavenc' if fmt == 'wav' else 'lamemp3enc target=quality quality=2'
        if not has_encoder('wavenc' if fmt == 'wav' else 'lamemp3enc'):
            raise RuntimeError('Не найден аудиокодер для ' + fmt)
        directory = Path(settings['audio_dir']).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / ('Аудио ' + datetime.now().strftime('%Y-%m-%d %H-%M-%S') + '.' + fmt)
        chain = audio_mix_chain(sources, encoder)
        self._add(chain[0] + ' ! filesink location=' + gst_quote(dest) + ' ' + chain[1], dest)
        self._start()

    def begin_video(self, settings, sources, node, fd, mode, stream_size=None):
        if not has_encoder('x264enc'):
            raise RuntimeError('Не найден x264enc. Установите gstreamer1.0-plugins-ugly')
        directory = Path(settings['video_dir']).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        base = 'Видео ' + datetime.now().strftime('%Y-%m-%d %H-%M-%S')
        dest = directory / (base + '.mp4')
        video = ('pipewiresrc fd=' + str(fd) + ' path=' + str(node) +
                 ' do-timestamp=true ! queue ! videoconvert ! videorate ! '
                 'video/x-raw,framerate=30/1 ! ')
        if mode == 'area':
            x, y = max(0, int(settings['area_x'])), max(0, int(settings['area_y']))
            w, h = max(2, int(settings['area_width'])), max(2, int(settings['area_height']))
            if not stream_size or len(stream_size) != 2:
                raise RuntimeError('Портал не сообщил размер экрана: обрезка области недоступна')
            screen_w, screen_h = map(int, stream_size)
            if x + w > screen_w or y + h > screen_h:
                raise ValueError(f'Область выходит за пределы выбранного экрана ({screen_w}×{screen_h})')
            right = screen_w - x - w
            bottom = screen_h - y - h
            video += f'videocrop left={x} top={y} right={right} bottom={bottom} ! '
        video += ('x264enc speed-preset=ultrafast tune=zerolatency bitrate=5000 key-int-max=60 ! '
                  'h264parse ! queue ! mux.')
        selected = list(sources) if settings['audio_in_video'] else []
        if selected and settings['merge_audio_video']:
            aac = next((s for s in ('avenc_aac', 'voaacenc', 'fdkaacenc') if has_encoder(s)), None)
            if not aac:
                raise RuntimeError('Для MP4 со звуком нужен AAC-кодер (gstreamer1.0-libav / plugins-bad)')
            chain = audio_mix_chain(selected, aac + ' ! queue ! mux.')
            desc = 'mp4mux name=mux faststart=true ! filesink location=' + gst_quote(dest) + ' ' + video + ' ' + chain[0] + ' ' + chain[1]
        else:
            desc = 'mp4mux name=mux faststart=true ! filesink location=' + gst_quote(dest) + ' ' + video
        self._add(desc, dest)
        if selected and not settings['merge_audio_video']:
            fmt = settings['audio_format']
            encoder = 'wavenc' if fmt == 'wav' else 'lamemp3enc target=quality quality=2'
            if not has_encoder('wavenc' if fmt == 'wav' else 'lamemp3enc'):
                raise RuntimeError('Отсутствует аудиокодер: ' + fmt)
            audio_dest = directory / (base + '.' + fmt)
            chain = audio_mix_chain(selected, encoder)
            self._add(chain[0] + ' ! filesink location=' + gst_quote(audio_dest) + ' ' + chain[1], audio_dest)
        self._start()

    def pause(self):
        if self.stopping:
            return
        if self.started_at is not None:
            self.elapsed += time.monotonic() - self.started_at
            self.started_at = None
            for pipe in self.pipelines:
                pipe.set_state(Gst.State.PAUSED)
        else:
            for pipe in self.pipelines:
                pipe.set_state(Gst.State.PLAYING)
            self.started_at = time.monotonic()

    def seconds(self):
        return self.elapsed + (time.monotonic() - self.started_at if self.started_at else 0)

    def stop(self):
        if self.stopping:
            return
        self.stopping = True
        if self.started_at is None:
            for pipe in self.pipelines:
                pipe.set_state(Gst.State.PLAYING)
        for pipe in self.pipelines:
            pipe.send_event(Gst.Event.new_eos())
        GLib.timeout_add_seconds(12, self._stop_timeout)

    def _stop_timeout(self):
        if self.pending_eos:
            self.failed = True
            self.error_cb('Превышено время ожидания завершения записи')
            self._cleanup()
        return False

    def _bus_msg(self, bus, msg, pipeline):
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            self.failed = True
            self.error_cb(str(err) + ('\n' + debug if debug else ''))
            self._cleanup()
        elif msg.type == Gst.MessageType.EOS:
            self.pending_eos.discard(pipeline)
            pipeline.set_state(Gst.State.NULL)
            if not self.pending_eos:
                self._cleanup()

    def _cleanup(self):
        if not self.pipelines:
            return
        for pipe in self.pipelines:
            pipe.set_state(Gst.State.NULL)
        self.pipelines.clear()
        self.pending_eos.clear()
        self.finished_cb(self.paths if not self.failed else [])


class MainApp:
    def __init__(self):
        self.settings = load_settings()
        self.recording = None
        self.portal = None
        self.mode = 'screen'
        self.status = 'Готово'
        self._build_window()
        self._build_indicator()
        self._make_actions()
        GLib.timeout_add(400, self._tick)

    def _build_window(self):
        self.window = Gtk.Window(title='GNOME Recorder')
        self.window.set_default_size(570, 600)
        self.window.set_border_width(16)
        self.window.connect('delete-event', self._on_close)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.window.add(outer)
        title = Gtk.Label()
        title.set_markup('<span size="x-large" weight="bold">GNOME Recorder</span>')
        title.set_halign(Gtk.Align.START)
        outer.pack_start(title, False, False, 0)
        self.notice = Gtk.Label(label=self.status, xalign=0)
        self.notice.set_line_wrap(True)
        outer.pack_start(self.notice, False, False, 0)
        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 0)

        video_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        notebook.append_page(video_box, Gtk.Label(label='Видео'))
        self.mode_combo = Gtk.ComboBoxText()
        for key, label in [('screen', 'Экран / монитор'), ('window', 'Окно'),
                           ('area', 'Область (координаты)')]:
            self.mode_combo.append(key, label)
        self.mode_combo.set_active_id('screen')
        self._row(video_box, 'Источник изображения', self.mode_combo)
        self.cursor_check = Gtk.CheckButton(label='Показывать курсор')
        self.cursor_check.set_active(self.settings['cursor'])
        video_box.pack_start(self.cursor_check, False, False, 0)
        self.video_audio_check = Gtk.CheckButton(label='Записывать аудио вместе с видео')
        self.video_audio_check.set_active(self.settings['audio_in_video'])
        video_box.pack_start(self.video_audio_check, False, False, 0)
        info = Gtk.Label(label='Выбор монитора или окна выполняется в системном диалоге Wayland.\n'
                               'Для области задайте координаты в настройках ниже.', xalign=0)
        info.set_line_wrap(True)
        video_box.pack_start(info, False, False, 0)

        audio_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        notebook.append_page(audio_box, Gtk.Label(label='Аудио'))
        audio_box.pack_start(Gtk.Label(label='Выбранные источники применяются к видео и аудиозаписи.', xalign=0), False, False, 0)
        self.sources_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(160)
        scroll.add(self.sources_box)
        audio_box.pack_start(scroll, True, True, 0)
        self.source_checks = {}
        self._reload_sources()
        refresh = Gtk.Button(label='Обновить список устройств')
        refresh.connect('clicked', lambda *_: self._reload_sources())
        audio_box.pack_start(refresh, False, False, 0)

        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin=12)
        notebook.append_page(settings_box, Gtk.Label(label='Настройки'))
        self.video_dir = Gtk.FileChooserButton(title='Папка для видео', action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.audio_dir = Gtk.FileChooserButton(title='Папка для аудио', action=Gtk.FileChooserAction.SELECT_FOLDER)
        for field, key in ((self.video_dir, 'video_dir'), (self.audio_dir, 'audio_dir')):
            folder = Path(self.settings[key]).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            field.set_filename(str(folder))
        self._row(settings_box, 'Папка видео', self.video_dir)
        self._row(settings_box, 'Папка аудио', self.audio_dir)
        self.merge_check = Gtk.CheckButton(label='Объединять видео и аудио (в один MP4)')
        self.merge_check.set_active(self.settings['merge_audio_video'])
        settings_box.pack_start(self.merge_check, False, False, 0)
        self.audio_format = Gtk.ComboBoxText()
        for f in ('wav', 'mp3'):
            self.audio_format.append(f, f.upper())
        self.audio_format.set_active_id(self.settings['audio_format'])
        self._row(settings_box, 'Формат аудио', self.audio_format)
        settings_box.pack_start(Gtk.Label(label='Формат видео: MP4 (H.264)', xalign=0), False, False, 0)
        self.open_folder = Gtk.CheckButton(label='Открывать папку после завершения')
        self.open_folder.set_active(self.settings['open_folder'])
        settings_box.pack_start(self.open_folder, False, False, 0)

        expander = Gtk.Expander(label='Область видеозаписи — координаты и размер')
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=8)
        expander.add(grid)
        self.area_spins = {}
        for i, (key, caption) in enumerate([('area_x', 'X'), ('area_y', 'Y'),
                                             ('area_width', 'Ширина'), ('area_height', 'Высота')]):
            spin = Gtk.SpinButton.new_with_range(0 if 'area_' in key and key in ('area_x', 'area_y') else 2,
                                                 16000, 1)
            spin.set_value(int(self.settings[key]))
            grid.attach(Gtk.Label(label=caption, xalign=0), 0, i, 1, 1)
            grid.attach(spin, 1, i, 1, 1)
            self.area_spins[key] = spin
        settings_box.pack_start(expander, False, False, 0)
        settings_box.pack_start(Gtk.Label(label='Горячие клавиши (добавляются при установке):\n'
                                           'Ctrl+Alt+R — начать, Ctrl+Alt+P — пауза, Ctrl+Alt+S — стоп.\n'
                                           'Изменять их можно в настройках клавиатуры GNOME.',
                                           xalign=0), False, False, 0)
        save = Gtk.Button(label='Сохранить настройки')
        save.connect('clicked', self._save_click)
        settings_box.pack_start(save, False, False, 0)

        bar = Gtk.Box(spacing=8)
        outer.pack_end(bar, False, False, 0)
        self.start_video_btn = Gtk.Button(label='● Запись видео')
        self.start_video_btn.connect('clicked', lambda *_: self.start_video())
        self.start_audio_btn = Gtk.Button(label='♫ Запись аудио')
        self.start_audio_btn.connect('clicked', lambda *_: self.start_audio())
        self.pause_btn = Gtk.Button(label='Пауза')
        self.pause_btn.connect('clicked', lambda *_: self.pause())
        self.stop_btn = Gtk.Button(label='■ Стоп')
        self.stop_btn.connect('clicked', lambda *_: self.stop())
        for btn in (self.start_video_btn, self.start_audio_btn, self.pause_btn, self.stop_btn):
            bar.pack_start(btn, True, True, 0)
        self._button_states()

    @staticmethod
    def _row(parent, text, widget):
        label = Gtk.Label(label=text, xalign=0)
        parent.pack_start(label, False, False, 0)
        parent.pack_start(widget, False, False, 0)

    def _reload_sources(self):
        for child in self.sources_box.get_children():
            self.sources_box.remove(child)
        old = self.get_sources() if self.source_checks else self.settings['audio_sources']
        self.source_checks = {}
        for name, label in audio_devices():
            cb = Gtk.CheckButton(label=label)
            cb.set_active(name in old)
            self.sources_box.pack_start(cb, False, False, 0)
            self.source_checks[name] = cb
        if not self.source_checks:
            self.sources_box.pack_start(Gtk.Label(label='Устройства не найдены. Проверьте pactl.'), False, False, 0)
        self.sources_box.show_all()

    def get_sources(self):
        return [k for k, cb in self.source_checks.items() if cb.get_active()]

    def _save(self):
        for k, v in (('cursor', self.cursor_check.get_active()),
                     ('audio_in_video', self.video_audio_check.get_active()),
                     ('merge_audio_video', self.merge_check.get_active()),
                     ('open_folder', self.open_folder.get_active())):
            self.settings[k] = v
        self.settings['audio_sources'] = self.get_sources()
        self.settings['audio_format'] = self.audio_format.get_active_id() or 'wav'
        self.settings['video_dir'] = self.video_dir.get_filename() or DEFAULTS['video_dir']
        self.settings['audio_dir'] = self.audio_dir.get_filename() or DEFAULTS['audio_dir']
        for k, spin in self.area_spins.items():
            self.settings[k] = spin.get_value_as_int()
        save_settings(self.settings)

    def _save_click(self, *_):
        self._save()
        self._status('Настройки сохранены')

    def _build_indicator(self):
        self.indicator = None
        if AppIndicator is not None:
            self.indicator = AppIndicator.Indicator.new(
                APP_ID, 'media-record', AppIndicator.IndicatorCategory.APPLICATION_STATUS)
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            menu = Gtk.Menu()
            items = [
                ('Запись экрана', self.start_screen),
                ('Запись окна', self.start_window),
                ('Запись области', self.start_area),
                ('Запись аудио', self.start_audio),
                ('Пауза / продолжить', self.pause),
                ('Остановить', self.stop),
                ('Настройки / окно', self.show_window),
                ('Выход', self.quit),
            ]
            self.menu_items = {}
            for label, callback in items:
                if label in ('Пауза / продолжить', 'Настройки / окно'):
                    menu.append(Gtk.SeparatorMenuItem())
                item = Gtk.MenuItem(label=label)
                item.connect('activate', lambda _item, func=callback: func())
                menu.append(item)
                self.menu_items[label] = item
            menu.show_all()
            self.indicator.set_menu(menu)
        else:
            self.menu_items = {}

    def _make_actions(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Allow external commands (GNOME shortcuts) without requiring additional packages.
        self.owner_id = Gio.bus_own_name_on_connection(
            self.bus, APP_ID, Gio.BusNameOwnerFlags.NONE, None, None)
        xml = '''<node><interface name="io.github.lancy.GnomeRecorder.Control">
        <method name="Start"/><method name="Pause"/><method name="Stop"/>
        <method name="Show"/></interface></node>'''
        node = Gio.DBusNodeInfo.new_for_xml(xml)
        self.registration_id = self.bus.register_object(
            '/io/github/lancy/GnomeRecorder', node.interfaces[0], self._on_dbus_call, None, None)

    def _on_dbus_call(self, conn, sender, path, iface, method, params, invocation):
        invocation.return_value(GLib.Variant('()', ()))
        if method == 'Start':
            self.start_video()
        elif method == 'Pause':
            self.pause()
        elif method == 'Stop':
            self.stop()
        elif method == 'Show':
            self.show_window()

    def show_window(self):
        self.window.show_all()
        self.window.present()

    def _on_close(self, *_):
        self._save()
        if self.indicator:
            self.window.hide()
            return True
        if self.recording:
            self.window.hide()
            return True
        Gtk.main_quit()
        return True

    def _status(self, message):
        self.status = message
        self.notice.set_text(message)
        print(message, flush=True)

    def _button_states(self):
        active = self.recording is not None or self.portal is not None
        self.start_video_btn.set_sensitive(not active)
        self.start_audio_btn.set_sensitive(not active)
        self.pause_btn.set_sensitive(self.recording is not None and not self.recording.stopping)
        self.stop_btn.set_sensitive(active)
        for label, item in self.menu_items.items() if hasattr(self, 'menu_items') else []:
            if label.startswith('Запись'):
                item.set_sensitive(not active)
            if label == 'Пауза / продолжить':
                item.set_sensitive(self.recording is not None and not self.recording.stopping)
            if label == 'Остановить':
                item.set_sensitive(active)

    def _tick(self):
        if self.recording:
            secs = int(self.recording.seconds())
            text = f'● Запись: {secs // 3600:02}:{(secs // 60) % 60:02}:{secs % 60:02}'
            if self.recording.started_at is None and not self.recording.stopping:
                text += ' (пауза)'
            self.notice.set_text(text)
            if self.indicator:
                self.indicator.set_icon_full('media-playback-stop' if self.recording.stopping else 'media-record', text)
        return True

    def _error(self, msg):
        self._status('Ошибка: ' + msg)
        dialog = Gtk.MessageDialog(transient_for=self.window, flags=0,
            message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
            text='Ошибка записи')
        dialog.format_secondary_text(str(msg))
        dialog.run()
        dialog.destroy()

    def start_screen(self):
        self.start_video('screen')

    def start_window(self):
        self.start_video('window')

    def start_area(self):
        self.start_video('area')

    def start_video(self, mode=None):
        if self.portal or self.recording:
            return
        self._save()
        mode = mode or self.mode_combo.get_active_id() or 'screen'
        self.mode = mode
        sources = self.get_sources() if self.settings['audio_in_video'] else []
        if self.settings['audio_in_video'] and not sources:
            self._error('Включена запись звука, но не выбрано ни одного устройства. Выберите устройство на вкладке «Аудио» или выключите звук.')
            return
        self._status('Выберите экран или окно в системном диалоге…')
        self.portal = PortalSession(lambda info, error: self._portal_done(info, error, sources, mode))
        self._button_states()
        try:
            self.portal.start(mode, self.settings['cursor'])
        except Exception as exc:
            self.portal.close()
            self.portal = None
            self._button_states()
            self._error(str(exc))

    def _portal_done(self, info, error, sources, mode):
        if error:
            if self.portal:
                self.portal.close()
                self.portal = None
            self._button_states()
            if error != 'Выбор отменён':
                self._error(error)
            else:
                self._status('Выбор отменён')
            return
        node, fd, size = info
        self.recording = Recording(self._finished, self._error)
        try:
            self.recording.begin_video(self.settings, sources, node, fd, mode, size)
            self._status('Запись видео запущена')
        except Exception as exc:
            self.recording._cleanup()
            self.recording = None
            self.portal.close()
            self.portal = None
            self._error(str(exc))
        self._button_states()

    def start_audio(self):
        if self.portal or self.recording:
            return
        self._save()
        sources = self.get_sources()
        self.recording = Recording(self._finished, self._error)
        try:
            self.recording.begin_audio(self.settings, sources)
            self._status('Запись аудио запущена')
        except Exception as exc:
            self.recording._cleanup()
            self.recording = None
            self._error(str(exc))
        self._button_states()

    def pause(self):
        if self.recording:
            self.recording.pause()

    def stop(self):
        if self.recording:
            self.recording.stop()
            self._status('Сохраняем файл…')
        elif self.portal:
            self.portal.close()
            self.portal = None
            self._status('Выбор отменён')
            self._button_states()

    def _finished(self, paths):
        self.recording = None
        if self.portal:
            self.portal.close()
            self.portal = None
        self._button_states()
        if self.indicator:
            self.indicator.set_icon_full('media-record', 'GNOME Recorder')
        if paths:
            self._status('Сохранено: ' + ', '.join(str(p) for p in paths))
            if self.settings['open_folder']:
                subprocess.Popen(['xdg-open', str(paths[0].parent)])

    def quit(self):
        if self.recording:
            self.stop()
            GLib.timeout_add_seconds(2, self._exit_after_stop)
        else:
            if self.portal:
                self.portal.close()
            Gtk.main_quit()

    def _exit_after_stop(self):
        if self.recording:
            return True
        Gtk.main_quit()
        return False


def send_command(action):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        bus.call_sync(APP_ID, '/io/github/lancy/GnomeRecorder',
            'io.github.lancy.GnomeRecorder.Control', action,
            None, GLib.VariantType.new('()'), Gio.DBusCallFlags.NONE, 2000, None)
        return 0
    except GLib.Error as exc:
        print('Не удалось связаться с GNOME Recorder:', exc, file=sys.stderr)
        return 1


def main():
    action = sys.argv[1].removeprefix('--').capitalize() if len(sys.argv) > 1 else None
    if action in ('Start', 'Stop', 'Pause', 'Show'):
        return send_command(action)
    app = MainApp()
    app.show_window()
    Gtk.main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
