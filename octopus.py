#!/usr/bin/env python3
"""Octopus desktop overlay — KPS counter + tentacle visualizer.

Port standalone do octopus-overlay.component.ts (Angular) pra Windows/Linux/Mac.
Janela transparente sempre-no-topo, capturável no OBS via Window Capture.

Run:
    pip install -r requirements.txt
    python octopus.py

    (ou use run.bat / run.sh — faz tudo automático)

Controles:
  - Arrastar: clique-esquerdo + drag em qualquer área
  - Resize: drag no canto inferior-direito (mantém aspect ratio 200:240)
  - Menu: clique-direito
  - Configurar teclas: clique-direito → Configurar Teclas…
  - Config: salva em ~/.jamproject/octopus-desktop.json
"""

from __future__ import annotations

import contextlib
import json
import math
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:
    from pynput import keyboard as pkeyboard
except ImportError:
    print('Install deps: pip install -r requirements.txt', file=sys.stderr)
    sys.exit(1)


LANE_COLORS = [
    '#ff3b6b',
    '#ffa233',
    '#ffd23d',
    '#43e07a',
    '#3aa8ff',
    '#8e6bff',
    '#ff5cd6',
    '#26e6c8',
    '#ff8c42',
]
KPS_WINDOW_MS = 1000
MIN_LANES = 3
MAX_LANES = 9
COMBO_KRAKEN = 100
KRAKEN_HOLD_MS = 12_000
LANE_HEAT_WINDOW_MS = 2500
RECENT_MISS_WINDOW_MS = 3500

VB_W = 200
VB_H = 240
BODY_CX = 100
BODY_CY = 78
BODY_RX = 46
BODY_RY = 42
KEY_AREA_TOP = 176
KEY_AREA_BOTTOM = 224
KEY_AREA_PAD_X = 8

DEFAULT_KEYS: dict[int, list[str]] = {
    3: ['a', 's', 'd'],
    4: ['a', 's', 'd', 'f'],
    5: ['a', 's', 'd', 'f', 'g'],
    6: ['a', 's', 'd', 'j', 'k', 'l'],
    7: ['a', 's', 'd', 'space', 'j', 'k', 'l'],
    8: ['a', 's', 'd', 'f', 'j', 'k', 'l', ';'],
    9: ['a', 's', 'd', 'f', 'space', 'j', 'k', 'l', ';'],
}

CONFIG_PATH = Path.home() / '.jamproject' / 'octopus-desktop.json'

PRESETS_FILENAME = 'presets.toml'
PRESETS_USER_PATH = Path.home() / '.jamproject' / PRESETS_FILENAME

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _script_dir() -> Path:
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _exe_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def find_presets_file() -> Path | None:
    for p in (PRESETS_USER_PATH, _exe_dir() / PRESETS_FILENAME, _script_dir() / PRESETS_FILENAME):
        if p.is_file():
            return p
    return None


def ensure_user_presets_file() -> Path | None:
    if PRESETS_USER_PATH.is_file():
        return PRESETS_USER_PATH
    bundled = _script_dir() / PRESETS_FILENAME
    if not bundled.is_file():
        return None
    try:
        PRESETS_USER_PATH.parent.mkdir(parents=True, exist_ok=True)
        PRESETS_USER_PATH.write_text(bundled.read_text(encoding='utf-8'), encoding='utf-8')
        return PRESETS_USER_PATH
    except OSError:
        return None


def load_presets() -> tuple[dict[str, dict], str | None]:
    path = find_presets_file()
    if path is None:
        return ({}, None)
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'[octopus] presets.toml parse error: {exc}', file=sys.stderr)
        return ({}, None)
    presets_section = data.get('preset', {}) or {}
    presets: dict[str, dict] = {}
    for name, body in presets_section.items():
        if not isinstance(body, dict):
            continue
        keys = body.get('keys')
        if not isinstance(keys, list) or not keys:
            continue
        n = len(keys)
        if n < MIN_LANES or n > MAX_LANES:
            print(f'[octopus] preset "{name}" has {n} keys — must be {MIN_LANES}-{MAX_LANES}', file=sys.stderr)
            continue
        presets[name] = {
            'keys': [str(k).lower() for k in keys],
            'description': str(body.get('description', '')).strip(),
        }
    active = (data.get('active') or {}).get('preset')
    if active is not None:
        active = str(active)
    return (presets, active)


def now_ms() -> float:
    return time.perf_counter() * 1000.0


@dataclass
class State:
    pressed: set[int] = field(default_factory=set)
    kps_times: deque = field(default_factory=deque)
    lane_times: dict[int, deque] = field(default_factory=lambda: defaultdict(deque))
    pulse_at: dict[int, float] = field(default_factory=dict)
    combo: int = 0
    max_combo: int = 0
    kraken_at: float = 0.0
    miss_active_until: float = 0.0
    recent_miss_times: deque = field(default_factory=deque)

    def kps(self) -> int:
        cutoff = now_ms() - KPS_WINDOW_MS
        while self.kps_times and self.kps_times[0] < cutoff:
            self.kps_times.popleft()
        return len(self.kps_times)

    def lane_heat(self, lane: int) -> float:
        cutoff = now_ms() - LANE_HEAT_WINDOW_MS
        mx = 0
        for dq in self.lane_times.values():
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) > mx:
                mx = len(dq)
        if mx == 0:
            return 0.0
        return len(self.lane_times.get(lane, ())) / mx

    def recent_miss_count(self) -> int:
        cutoff = now_ms() - RECENT_MISS_WINDOW_MS
        while self.recent_miss_times and self.recent_miss_times[0] < cutoff:
            self.recent_miss_times.popleft()
        return len(self.recent_miss_times)

    def kraken_active(self) -> bool:
        return self.kraken_at > 0 and now_ms() - self.kraken_at < KRAKEN_HOLD_MS

    def miss_active(self) -> bool:
        return now_ms() < self.miss_active_until

    def mood(self) -> str:
        if self.miss_active():
            return 'oops'
        if self.kraken_active():
            return 'transcendent'
        k = self.kps()
        streaking = self.recent_miss_count() == 0
        if k >= 14:
            return 'hyped' if streaking else 'despair'
        if k >= 9:
            return 'hyped' if streaking else 'panic'
        if k >= 5:
            return 'focused'
        if self.pressed:
            return 'happy'
        return 'normal'

    def lit_tentacles(self, n: int) -> int:
        c = self.combo
        if c >= COMBO_KRAKEN:
            return n
        if c >= 50:
            return max(1, round(n * 0.75))
        if c >= 25:
            return max(1, round(n * 0.5))
        if c >= 10:
            return 1
        return 0

    def hit(self, lane: int) -> None:
        t = now_ms()
        self.kps_times.append(t)
        self.lane_times[lane].append(t)
        self.pulse_at[lane] = t
        self.combo += 1
        if self.combo > self.max_combo:
            self.max_combo = self.combo
        if self.combo >= COMBO_KRAKEN and (
            self.combo == COMBO_KRAKEN or t - self.kraken_at > KRAKEN_HOLD_MS / 2
        ):
            self.kraken_at = t


@dataclass
class TentacleGeom:
    idx: int
    lane: int
    base_x: float
    base_y: float
    tip_rest_x: float
    tip_rest_y: float
    tip_press_x: float
    tip_press_y: float
    ctrl_rest_x: float
    ctrl_rest_y: float
    ctrl_press_x: float
    ctrl_press_y: float
    color: QColor


@dataclass
class KeyGeom:
    lane: int
    x: float
    y: float
    w: float
    h: float
    cx: float
    cy: float
    label: str
    color: QColor


def _seed_rnd(i: int, salt: float) -> float:
    v = math.sin(i * 12.9898 + salt * 4.731) * 43758.5453
    return v - math.floor(v)


def compute_keys(n: int, labels: list[str]) -> list[KeyGeom]:
    if n == 0:
        return []
    total_w = VB_W - 2 * KEY_AREA_PAD_X
    gap = 6 if n <= 4 else 4 if n <= 6 else 3 if n <= 8 else 2
    key_w = (total_w - gap * (n - 1)) / n
    key_h = KEY_AREA_BOTTOM - KEY_AREA_TOP
    keys: list[KeyGeom] = []
    for i in range(n):
        x = KEY_AREA_PAD_X + i * (key_w + gap)
        keys.append(
            KeyGeom(
                lane=i,
                x=x,
                y=KEY_AREA_TOP,
                w=key_w,
                h=key_h,
                cx=x + key_w / 2,
                cy=KEY_AREA_TOP + key_h / 2,
                label=labels[i] if i < len(labels) else '·',
                color=QColor(LANE_COLORS[i % len(LANE_COLORS)]),
            )
        )
    return keys


def compute_tentacles(keys: list[KeyGeom]) -> list[TentacleGeom]:
    n = len(keys)
    if n == 0:
        return []
    base_y = BODY_CY + BODY_RY - 4
    base_span = min(BODY_RX * 1.7, 78)
    out: list[TentacleGeom] = []
    for i, k in enumerate(keys):
        t_ratio = 0.5 if n == 1 else i / (n - 1)
        base_x = BODY_CX - base_span / 2 + t_ratio * base_span
        tip_x = k.cx
        tip_rest_y = k.y - 30 - _seed_rnd(i, 0) * 6
        tip_press_y = k.y + 6
        mid_y = (base_y + tip_rest_y) / 2
        sway = (-1 if i % 2 == 0 else 1) * (10 + _seed_rnd(i, 1) * 6)
        ctrl_rest_x = (base_x + tip_x) / 2 + sway
        ctrl_press_x = (base_x + tip_x) / 2 + sway * 0.35
        out.append(
            TentacleGeom(
                idx=i,
                lane=k.lane,
                base_x=base_x,
                base_y=base_y,
                tip_rest_x=tip_x,
                tip_rest_y=tip_rest_y,
                tip_press_x=tip_x,
                tip_press_y=tip_press_y,
                ctrl_rest_x=ctrl_rest_x,
                ctrl_rest_y=mid_y - 6,
                ctrl_press_x=ctrl_press_x,
                ctrl_press_y=mid_y + 12,
                color=k.color,
            )
        )
    return out


_KEY_LABEL_MAP = {
    'space': '␣',
    'semicolon': ';',
    "'": "'",
    ',': ',',
    '.': '.',
    '/': '/',
    'shift': '⇧',
    'ctrl': 'C',
    'alt': 'A',
    'left': '←',
    'right': '→',
    'up': '↑',
    'down': '↓',
    'enter': '↵',
    'tab': '⇥',
    'backspace': '⌫',
}


def label_for(key: str) -> str:
    k = key.lower()
    if k in _KEY_LABEL_MAP:
        return _KEY_LABEL_MAP[k]
    return k.upper()[:2]


# ─── Key capture helpers ────────────────────────────────────────────────────────

def qt_key_to_str(ev) -> str | None:
    """Convert Qt key event to octopus key string. Returns None for Escape (cancel)."""
    key = ev.key()
    text = ev.text()
    K = Qt.Key
    _MAP = {
        K.Key_Space: 'space',
        K.Key_Return: 'enter',
        K.Key_Enter: 'enter',
        K.Key_Left: 'left',
        K.Key_Right: 'right',
        K.Key_Up: 'up',
        K.Key_Down: 'down',
        K.Key_Shift: 'shift',
        K.Key_Control: 'ctrl',
        K.Key_Alt: 'alt',
        K.Key_Tab: 'tab',
        K.Key_Escape: None,
        K.Key_Backspace: 'backspace',
        K.Key_Semicolon: ';',
        K.Key_Comma: ',',
        K.Key_Period: '.',
        K.Key_Slash: '/',
        K.Key_Apostrophe: "'",
        K.Key_BracketLeft: '[',
        K.Key_BracketRight: ']',
        K.Key_Backslash: '\\',
        K.Key_Minus: '-',
        K.Key_Equal: '=',
        K.Key_F1: 'f1', K.Key_F2: 'f2', K.Key_F3: 'f3',
        K.Key_F4: 'f4', K.Key_F5: 'f5', K.Key_F6: 'f6',
        K.Key_F7: 'f7', K.Key_F8: 'f8', K.Key_F9: 'f9',
        K.Key_F10: 'f10', K.Key_F11: 'f11', K.Key_F12: 'f12',
    }
    if key in _MAP:
        return _MAP[key]
    if text and text.strip() and text.isprintable():
        return text.lower()
    return ''  # unknown, skip


# ─── Key capture button ─────────────────────────────────────────────────────────

_BTN_IDLE = (
    'QPushButton {'
    '  background: #1e1e2a; color: #ffffff;'
    '  border: 1px solid #3a3a50; border-radius: 4px;'
    '  padding: 3px 8px; min-width: 68px; font-weight: bold;'
    '}'
    'QPushButton:hover { background: #2a2a3e; }'
)
_BTN_WAIT = (
    'QPushButton {'
    '  background: #2a2a1e; color: #ffd23d;'
    '  border: 1px solid #ffd23d; border-radius: 4px;'
    '  padding: 3px 8px; min-width: 68px; font-weight: bold;'
    '}'
)
_BTN_EMPTY = (
    'QPushButton {'
    '  background: #111118; color: #555566;'
    '  border: 1px dashed #3a3a50; border-radius: 4px;'
    '  padding: 3px 8px; min-width: 68px;'
    '}'
    'QPushButton:hover { background: #1a1a28; color: #888899; }'
)


class KeyCaptureButton(QPushButton):
    key_captured = pyqtSignal(str)

    def __init__(self, key: str = '', parent=None) -> None:
        super().__init__(parent)
        self._key = key.lower() if key else ''
        self._capturing = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._refresh()
        self.clicked.connect(self._start_capture)

    def sizeHint(self) -> QSize:
        return QSize(72, 28)

    def current_key(self) -> str:
        return self._key

    def set_key(self, key: str) -> None:
        self._key = key.lower() if key else ''
        self._capturing = False
        with contextlib.suppress(Exception):
            self.releaseKeyboard()
        self._refresh()

    def _refresh(self) -> None:
        if self._capturing:
            self.setText('⌨ pressione…')
            self.setStyleSheet(_BTN_WAIT)
        elif self._key:
            self.setText(label_for(self._key))
            self.setStyleSheet(_BTN_IDLE)
        else:
            self.setText('— clique —')
            self.setStyleSheet(_BTN_EMPTY)

    def _start_capture(self) -> None:
        self._capturing = True
        self._refresh()
        self.grabKeyboard()

    def keyPressEvent(self, ev) -> None:
        if not self._capturing:
            super().keyPressEvent(ev)
            return
        result = qt_key_to_str(ev)
        if result is None:
            # Escape — cancel capture, keep old key
            self._capturing = False
            with contextlib.suppress(Exception):
                self.releaseKeyboard()
            self._refresh()
            return
        if result:
            self._key = result
            self._capturing = False
            with contextlib.suppress(Exception):
                self.releaseKeyboard()
            self._refresh()
            self.key_captured.emit(result)

    def focusOutEvent(self, ev) -> None:
        if self._capturing:
            self._capturing = False
            with contextlib.suppress(Exception):
                self.releaseKeyboard()
            self._refresh()
        super().focusOutEvent(ev)


# ─── Key setup dialog ───────────────────────────────────────────────────────────

_DIALOG_CSS = """
QDialog {
    background: #0e0e18;
    color: #e0e0f0;
}
QLabel {
    color: #c0c0d8;
    font-size: 12px;
}
QLabel#header {
    color: #ffffff;
    font-size: 14px;
    font-weight: bold;
}
QLabel#tip {
    color: #888899;
    font-size: 11px;
}
QCheckBox {
    color: #c0c0d8;
    spacing: 6px;
}
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #3a3a50;
    border-radius: 3px;
    background: #1e1e2a;
}
QCheckBox::indicator:checked {
    background: #3aa8ff;
    border-color: #3aa8ff;
}
QLineEdit {
    background: #1e1e2a;
    color: #ffffff;
    border: 1px solid #3a3a50;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}
QLineEdit:focus { border-color: #3aa8ff; }
QSpinBox {
    background: #1e1e2a;
    color: #ffffff;
    border: 1px solid #3a3a50;
    border-radius: 4px;
    padding: 3px 6px;
    font-size: 13px;
    min-width: 50px;
}
QSpinBox:focus { border-color: #3aa8ff; }
QSpinBox::up-button, QSpinBox::down-button { width: 18px; }
QPushButton#savePresetBtn {
    background: #1e3a1e;
    color: #43e07a;
    border: 1px solid #43e07a;
    border-radius: 4px;
    padding: 4px 14px;
    font-size: 12px;
}
QPushButton#savePresetBtn:hover { background: #274a27; }
QDialogButtonBox QPushButton {
    background: #2a2a3e;
    color: #ffffff;
    border: 1px solid #3a3a50;
    border-radius: 4px;
    padding: 5px 20px;
    font-size: 13px;
    min-width: 80px;
}
QDialogButtonBox QPushButton:hover { background: #3a3a50; }
QScrollArea { border: none; background: transparent; }
QFrame#sep { color: #2a2a3e; }
"""


class KeySetupDialog(QDialog):
    def __init__(
        self,
        n_lanes: int,
        keys: list[str],
        doubles: dict[int, str],
        presets: dict,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('🐙 Configurar Teclas')
        self.setStyleSheet(_DIALOG_CSS)
        self.setMinimumWidth(500)
        self.setModal(True)

        self._keys = list(keys)
        self._doubles = dict(doubles)
        self._presets = presets
        self._rows: list[tuple[KeyCaptureButton, QCheckBox, KeyCaptureButton]] = []

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 18, 20, 18)

        # Title
        title = QLabel('Configurar Teclas do Polvo')
        title.setObjectName('header')
        root.addWidget(title)

        tip = QLabel('Clique em um botão de tecla e pressione a tecla desejada. ESC cancela.')
        tip.setObjectName('tip')
        root.addWidget(tip)

        # Lane count row
        count_row = QHBoxLayout()
        count_row.addWidget(QLabel('Número de lanes (3–9):'))
        self._spin = QSpinBox()
        self._spin.setRange(MIN_LANES, MAX_LANES)
        self._spin.setValue(n_lanes)
        count_row.addWidget(self._spin)
        count_row.addStretch()
        root.addLayout(count_row)

        self._add_sep(root)

        # Column headers
        hdr = QGridLayout()
        hdr.setSpacing(6)
        for col, txt in enumerate(['Lane', 'Tecla principal', 'Double (alt)', 'Tecla alt']):
            lbl = QLabel(txt)
            lbl.setStyleSheet('font-weight: bold; color: #8888aa; font-size: 11px;')
            hdr.addWidget(lbl, 0, col)
        root.addLayout(hdr)

        # Scrollable lane rows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(320)
        self._lane_container = QWidget()
        self._lane_container.setStyleSheet('background: transparent;')
        self._lane_grid = QGridLayout(self._lane_container)
        self._lane_grid.setSpacing(6)
        self._lane_grid.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self._lane_container)
        root.addWidget(scroll)

        self._rebuild_rows(n_lanes)
        self._spin.valueChanged.connect(self._rebuild_rows)

        self._add_sep(root)

        # Preset save row
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel('Salvar como preset:'))
        self._preset_name = QLineEdit()
        self._preset_name.setPlaceholderText('ex: meu-setup-4k')
        preset_row.addWidget(self._preset_name, 1)
        save_btn = QPushButton('Salvar')
        save_btn.setObjectName('savePresetBtn')
        save_btn.clicked.connect(self._save_preset)
        preset_row.addWidget(save_btn)
        root.addLayout(preset_row)

        self._add_sep(root)

        # OK / Cancel
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText('Aplicar')
        ok_btn.setStyleSheet('background: #1a3a5a; border-color: #3aa8ff; color: #3aa8ff;')
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText('Cancelar')
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _add_sep(self, layout: QVBoxLayout) -> None:
        sep = QFrame()
        sep.setObjectName('sep')
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet('background: #2a2a3e; max-height: 1px;')
        layout.addWidget(sep)

    def _rebuild_rows(self, n: int) -> None:
        while len(self._keys) < n:
            default_n = min(DEFAULT_KEYS.keys(), key=lambda x: abs(x - n))
            fallback = DEFAULT_KEYS[default_n]
            idx = len(self._keys)
            self._keys.append(fallback[idx] if idx < len(fallback) else '')

        self._rows.clear()
        while self._lane_grid.count():
            item = self._lane_grid.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for i in range(n):
            color = LANE_COLORS[i % len(LANE_COLORS)]

            swatch = QLabel(f' {i + 1} ')
            swatch.setAlignment(Qt.AlignmentFlag.AlignCenter)
            swatch.setStyleSheet(
                f'background: {color}; border-radius: 3px;'
                f' color: #000000; font-weight: bold; min-width: 28px; max-width: 28px;'
            )
            swatch.setFixedWidth(28)

            key = self._keys[i] if i < len(self._keys) else ''
            alt_key = self._doubles.get(i, '')

            primary_btn = KeyCaptureButton(key)
            double_chk = QCheckBox('ativo')
            double_chk.setChecked(bool(alt_key))
            alt_btn = KeyCaptureButton(alt_key)
            alt_btn.setEnabled(bool(alt_key))

            double_chk.toggled.connect(
                lambda checked, b=alt_btn: (b.setEnabled(checked), b.set_key(b.current_key()) if checked else b.set_key(''))
            )

            self._lane_grid.addWidget(swatch, i, 0, Qt.AlignmentFlag.AlignVCenter)
            self._lane_grid.addWidget(primary_btn, i, 1)
            self._lane_grid.addWidget(double_chk, i, 2, Qt.AlignmentFlag.AlignCenter)
            self._lane_grid.addWidget(alt_btn, i, 3)

            self._rows.append((primary_btn, double_chk, alt_btn))

    def get_result(self) -> tuple[int, list[str], dict[int, str]]:
        n = self._spin.value()
        keys = [row[0].current_key() for row in self._rows[:n]]
        doubles: dict[int, str] = {}
        for i, (_, chk, alt) in enumerate(self._rows[:n]):
            if chk.isChecked() and alt.current_key():
                doubles[i] = alt.current_key()
        return n, keys, doubles

    def _save_preset(self) -> None:
        name = self._preset_name.text().strip().replace(' ', '-')
        if not name:
            return
        n, keys, _ = self.get_result()
        valid_keys = [k for k in keys if k]
        if len(valid_keys) < MIN_LANES:
            return
        path = PRESETS_USER_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding='utf-8') if path.is_file() else ''
            keys_toml = ', '.join(f'"{k}"' for k in valid_keys)
            block = (
                f'\n[preset.{name}]\n'
                f'description = "Preset personalizado — {len(valid_keys)} lanes"\n'
                f'keys = [{keys_toml}]\n'
            )
            if f'[preset.{name}]' in existing:
                import re
                existing = re.sub(
                    rf'\[preset\.{re.escape(name)}\][^\[]*',
                    block.lstrip('\n'),
                    existing,
                )
                path.write_text(existing, encoding='utf-8')
            else:
                path.write_text(existing + block, encoding='utf-8')
            self._preset_name.setStyleSheet('border-color: #43e07a;')
            QTimer.singleShot(1200, lambda: self._preset_name.setStyleSheet(''))
        except OSError:
            pass


# ─── Main widget ────────────────────────────────────────────────────────────────

class OctopusWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle('Octopus')
        self.setMinimumSize(180, 216)

        self.cfg = self._load_config()
        self.presets: dict[str, dict] = {}
        self.active_preset: str | None = None
        self._presets_path: Path | None = None
        self._presets_mtime: float = 0.0
        self._load_presets_into_cfg()

        self.state = State()
        self.n_lanes: int = int(self.cfg.get('lanes', 4))
        self.tint = QColor(self.cfg.get('tint', '#ffffff'))
        self.show_kps = bool(self.cfg.get('show_kps', True))

        self.doubles: dict[int, str] = {}
        raw_doubles = self.cfg.get('doubles') or {}
        for k, v in raw_doubles.items():
            with contextlib.suppress(Exception):
                self.doubles[int(k)] = str(v).lower()

        self._drag_start: QPointF | None = None
        self._drag_kind: str | None = None
        self._orig_size = (0, 0)

        self._apply_geometry()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

        self.listener = pkeyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.listener.daemon = True
        self.listener.start()

    def _tick(self) -> None:
        self._maybe_reload_presets()
        self.update()

    def _load_presets_into_cfg(self) -> None:
        ensure_user_presets_file()
        presets, active = load_presets()
        self.presets = presets
        path = find_presets_file()
        self._presets_path = path
        self._presets_mtime = path.stat().st_mtime if path else 0.0
        if not presets:
            return
        chosen = self.cfg.get('preset') if isinstance(self.cfg.get('preset'), str) else None
        if chosen not in presets:
            chosen = active if active in presets else next(iter(presets))
        self.active_preset = chosen
        keys = presets[chosen]['keys']
        self.cfg['preset'] = chosen
        self.cfg['keys'] = keys
        self.cfg['lanes'] = len(keys)

    def _maybe_reload_presets(self) -> None:
        if self._presets_path is None:
            return
        try:
            mtime = self._presets_path.stat().st_mtime
        except OSError:
            return
        if mtime <= self._presets_mtime:
            return
        self._presets_mtime = mtime
        prev_preset = self.active_preset
        presets, active = load_presets()
        if not presets:
            return
        self.presets = presets
        chosen = (
            prev_preset
            if prev_preset in presets
            else (active if active in presets else next(iter(presets)))
        )
        self._apply_preset(chosen, persist=False)

    def _apply_preset(self, name: str, *, persist: bool = True) -> None:
        if name not in self.presets:
            return
        keys = self.presets[name]['keys']
        self.active_preset = name
        self.cfg['preset'] = name
        self.cfg['keys'] = keys
        self.cfg['lanes'] = len(keys)
        self.n_lanes = len(keys)
        self.doubles = {}
        self.state = State()
        if persist:
            self._save_config()
        self.update()

    def _apply_geometry(self) -> None:
        w = int(self.cfg.get('w', 280))
        h = int(w * (VB_H / VB_W))
        self.resize(w, h)
        x = self.cfg.get('x')
        y = self.cfg.get('y')
        if x is not None and y is not None:
            self.move(int(x), int(y))
        else:
            screen = QGuiApplication.primaryScreen().geometry()
            self.move(screen.width() - w - 32, screen.height() - h - 80)

    def _load_config(self) -> dict:
        try:
            return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'lanes': self.n_lanes,
                'tint': self.tint.name(),
                'show_kps': self.show_kps,
                'x': self.x(),
                'y': self.y(),
                'w': self.width(),
                'h': self.height(),
                'keys': self.cfg.get('keys') or DEFAULT_KEYS.get(self.n_lanes, DEFAULT_KEYS[4]),
                'doubles': {str(k): v for k, v in self.doubles.items()},
            }
            if self.cfg.get('preset'):
                data['preset'] = self.cfg['preset']
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _key_list(self) -> list[str]:
        keys = self.cfg.get('keys') or DEFAULT_KEYS.get(self.n_lanes, DEFAULT_KEYS[4])
        return [str(k).lower() for k in keys[: self.n_lanes]]

    def _key_map(self) -> dict[str, int]:
        result = {k: i for i, k in enumerate(self._key_list())}
        for lane, alt_key in self.doubles.items():
            if alt_key and lane < self.n_lanes and alt_key not in result:
                result[alt_key] = lane
        return result

    @staticmethod
    def _key_str(key) -> str | None:
        try:
            if isinstance(key, pkeyboard.Key):
                return key.name
            char = getattr(key, 'char', None)
            return char.lower() if char else None
        except AttributeError:
            return None

    def _on_press(self, key) -> None:
        k = self._key_str(key)
        if k is None:
            return
        lane = self._key_map().get(k)
        if lane is None:
            return
        if lane in self.state.pressed:
            return
        self.state.pressed.add(lane)
        self.state.hit(lane)

    def _on_release(self, key) -> None:
        k = self._key_str(key)
        if k is None:
            return
        lane = self._key_map().get(k)
        if lane is None:
            return
        self.state.pressed.discard(lane)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        scale = min(w / VB_W, h / VB_H)
        offset_x = (w - VB_W * scale) / 2
        offset_y = (h - VB_H * scale) / 2
        p.save()
        p.translate(offset_x, offset_y)
        p.scale(scale, scale)

        labels = [label_for(k) for k in self._key_list()]
        keys = compute_keys(self.n_lanes, labels)
        tentacles = compute_tentacles(keys)
        phase = now_ms() / 1000.0

        for t in tentacles:
            self._paint_tentacle(p, t, phase)

        bob = math.sin(phase * 2.2) * 2
        press_offset = -2 if self.state.pressed else 0
        body_dy = BODY_CY - 70 + bob + press_offset
        p.save()
        p.translate(0, body_dy)
        p.setBrush(QBrush(self.tint))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(100, 70), 46, 42)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawEllipse(QPointF(100, 58), 36, 14)
        self._paint_face(p)
        self._paint_mouth(p)
        p.restore()

        for k in keys:
            self._paint_key(p, k)

        p.restore()

        if self.show_kps:
            self._paint_kps(p)

        self._paint_resize_handle(p)
        p.end()

    def _paint_tentacle(self, p: QPainter, t: TentacleGeom, phase: float) -> None:
        pressed = t.lane in self.state.pressed
        rnd_a = _seed_rnd(t.idx, 0.5)
        rnd_b = _seed_rnd(t.idx, 1.7)
        sway_mag = 2 + rnd_a * 3
        lift_mag = 1.5 + rnd_b * 2.5
        sway_rate = 1.1 + rnd_a * 0.8
        lift_rate = 0.9 + rnd_b * 0.8
        sway = math.sin(phase * sway_rate + rnd_a * 6.28 + t.idx * 0.8) * sway_mag
        lift = math.cos(phase * lift_rate + rnd_b * 6.28 + t.idx * 1.1) * lift_mag
        if pressed:
            tip_x, tip_y = t.tip_press_x, t.tip_press_y
            cx, cy = t.ctrl_press_x, t.ctrl_press_y
        else:
            tip_x = t.tip_rest_x + sway
            tip_y = t.tip_rest_y + lift
            cx = t.ctrl_rest_x + sway * 0.5
            cy = t.ctrl_rest_y + lift * 0.5

        heat = self.state.lane_heat(t.lane)
        lit = t.idx < self.state.lit_tentacles(self.n_lanes)
        kraken = self.state.kraken_active()

        if kraken:
            ph = int(phase * 4)
            stroke = QColor(LANE_COLORS[(t.idx + ph) % len(LANE_COLORS)])
        elif pressed or lit:
            stroke = QColor(t.color)
        else:
            stroke = QColor('#ffffff')
        opacity = 1.0 if (kraken or pressed or lit) else 0.55 + heat * 0.45
        stroke.setAlphaF(max(0.0, min(1.0, opacity)))
        width = (13 if pressed else 9) + heat * 3.5

        path = QPainterPath(QPointF(t.base_x, t.base_y))
        path.quadTo(QPointF(cx, cy), QPointF(tip_x, tip_y))
        pen = QPen(stroke)
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        vfill = QColor(stroke) if (kraken or pressed or lit) else QColor('#cfcfcf')
        vfill.setAlphaF(max(0.0, min(1.0, opacity)))
        p.setBrush(QBrush(vfill))
        p.setPen(Qt.PenStyle.NoPen)
        for u in (0.32, 0.54, 0.74, 0.9):
            inv = 1 - u
            vcx = inv * inv * t.base_x + 2 * inv * u * cx + u * u * tip_x
            vcy = inv * inv * t.base_y + 2 * inv * u * cy + u * u * tip_y
            dxdu = 2 * inv * (cx - t.base_x) + 2 * u * (tip_x - cx)
            dydu = 2 * inv * (cy - t.base_y) + 2 * u * (tip_y - cy)
            angle = math.degrees(math.atan2(dydu, dxdu))
            p.save()
            p.translate(vcx, vcy)
            p.rotate(angle)
            p.drawEllipse(QPointF(0, 0), 2.2, 1.4)
            p.restore()

    def _paint_face(self, p: QPainter) -> None:
        mood = self.state.mood()
        pressed = bool(self.state.pressed)
        eye_dx = 0.0
        if pressed:
            avg = sum(self.state.pressed) / len(self.state.pressed)
            half = (self.n_lanes - 1) / 2
            norm = (avg - half) / max(1, half)
            eye_dx = round(norm * 3)
        eye_dy = 2 if pressed else 0

        white = QColor('#ffffff')
        pupil = QColor('#15151c')
        glint = QColor('#ffffff')

        if mood == 'oops':
            pen = QPen(pupil, 2.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            for cx in (84, 116):
                p.drawLine(QPointF(cx - 6, 67), QPointF(cx + 6, 77))
                p.drawLine(QPointF(cx + 6, 67), QPointF(cx - 6, 77))
        elif mood in ('hyped', 'transcendent'):
            self._draw_star(p, 84, 72, 8, QColor('#ffd23d'))
            self._draw_star(p, 116, 72, 8, QColor('#ffd23d'))
        elif mood == 'focused':
            pen = QPen(pupil, 2.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for cx in (84, 116):
                path = QPainterPath(QPointF(cx - 8, 72))
                path.quadTo(QPointF(cx, 76), QPointF(cx + 8, 72))
                p.drawPath(path)
        elif mood in ('panic', 'despair'):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(white))
            p.drawEllipse(QPointF(84, 72), 9, 9)
            p.drawEllipse(QPointF(116, 72), 9, 9)
            p.setBrush(QBrush(pupil))
            p.drawEllipse(QPointF(84 + eye_dx, 72), 2.4, 2.4)
            p.drawEllipse(QPointF(116 + eye_dx, 72), 2.4, 2.4)
            p.setBrush(QBrush(QColor('#7fc8ff')))
            p.drawEllipse(QPointF(138, 74), 2.2, 4)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(white))
            p.drawEllipse(QPointF(84, 72), 8, 8)
            p.drawEllipse(QPointF(116, 72), 8, 8)
            p.setBrush(QBrush(pupil))
            p.drawEllipse(QPointF(84 + eye_dx, 72 + eye_dy), 4, 4)
            p.drawEllipse(QPointF(116 + eye_dx, 72 + eye_dy), 4, 4)
            p.setBrush(QBrush(glint))
            p.drawEllipse(QPointF(82, 69), 1.4, 1.4)
            p.drawEllipse(QPointF(114, 69), 1.4, 1.4)

        if mood in ('happy', 'hyped', 'transcendent'):
            p.setBrush(QBrush(QColor(255, 120, 150, 140)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(74, 86), 5, 2.5)
            p.drawEllipse(QPointF(126, 86), 5, 2.5)

    def _draw_star(self, p: QPainter, cx: float, cy: float, r: float, color: QColor) -> None:
        path = QPainterPath()
        for i in range(10):
            radius = r if i % 2 == 0 else r / 2.4
            angle = math.radians(-90 + i * 36)
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawPath(path)

    def _paint_mouth(self, p: QPainter) -> None:
        mood = self.state.mood()
        pen = QPen(QColor('#15151c'), 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        if mood in ('hyped', 'transcendent'):
            path.moveTo(88, 93)
            path.quadTo(100, 112, 112, 93)
        elif mood == 'happy':
            path.moveTo(92, 94)
            path.quadTo(100, 106, 108, 94)
        elif mood in ('oops', 'despair'):
            path.moveTo(90, 100)
            path.quadTo(100, 90, 110, 100)
        elif mood == 'panic':
            path.moveTo(94, 96)
            path.quadTo(100, 104, 106, 96)
        elif mood == 'focused':
            path.moveTo(94, 97)
            path.lineTo(106, 97)
        else:
            path.moveTo(94, 95)
            path.quadTo(100, 99, 106, 95)
        p.drawPath(path)

    def _paint_key(self, p: QPainter, k: KeyGeom) -> None:
        pressed = k.lane in self.state.pressed
        fill = QColor(k.color) if pressed else QColor('#15151c')
        y = k.y + 2 if pressed else k.y
        h = k.h - 2 if pressed else k.h
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(QRectF(k.x, y, k.w, h), 3, 3)
        p.setPen(QColor('#ffffff') if pressed else QColor('#9aa0aa'))
        font_size = 10 if self.n_lanes <= 4 else 8 if self.n_lanes <= 6 else 6
        f = QFont('Segoe UI', font_size)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(k.x, y, k.w, h), Qt.AlignmentFlag.AlignCenter, k.label)

    def _paint_kps(self, p: QPainter) -> None:
        p.save()
        p.resetTransform()
        kps = self.state.kps()
        hot = kps >= 8
        bg = QColor('#ff3b6b') if hot else QColor(20, 20, 28, 220)
        p.setBrush(QBrush(bg))
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
        margin = 8
        w_pill = 92
        h_pill = 30
        p.drawRoundedRect(QRectF(margin, margin, w_pill, h_pill), 15, 15)
        p.setPen(QColor('#ffffff'))
        f = QFont('Segoe UI', 13)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(margin, margin, 48, h_pill), Qt.AlignmentFlag.AlignCenter, str(kps))
        f2 = QFont('Segoe UI', 9)
        f2.setBold(True)
        p.setFont(f2)
        p.drawText(
            QRectF(margin + 48, margin, 44, h_pill),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            'KPS',
        )
        p.restore()

    def _paint_resize_handle(self, p: QPainter) -> None:
        s = 14
        x = self.width() - s - 4
        y = self.height() - s - 4
        p.setPen(QPen(QColor(255, 255, 255, 90), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(x + s, y, x, y + s)
        p.drawLine(x + s, y + 5, x + 5, y + s)

    def _corner_rect(self) -> QRectF:
        s = 22
        return QRectF(self.width() - s, self.height() - s, s, s)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            if self._corner_rect().contains(ev.position()):
                self._drag_kind = 'resize'
                self._drag_start = ev.globalPosition()
                self._orig_size = (self.width(), self.height())
            else:
                self._drag_kind = 'move'
                self._drag_start = ev.globalPosition() - QPointF(self.x(), self.y())
        elif ev.button() == Qt.MouseButton.RightButton:
            self._show_menu(ev.globalPosition().toPoint())

    def mouseMoveEvent(self, ev) -> None:
        if self._drag_kind == 'move' and self._drag_start is not None:
            pos = ev.globalPosition() - self._drag_start
            self.move(int(pos.x()), int(pos.y()))
        elif self._drag_kind == 'resize' and self._drag_start is not None:
            delta = ev.globalPosition() - self._drag_start
            new_w = max(self.minimumWidth(), int(self._orig_size[0] + delta.x()))
            new_h = int(new_w * (VB_H / VB_W))
            self.resize(new_w, new_h)

    def mouseReleaseEvent(self, _ev) -> None:
        if self._drag_kind:
            self._drag_kind = None
            self._save_config()

    def _show_menu(self, pos) -> None:
        m = QMenu(self)
        m.setStyleSheet("""
            QMenu {
                background: #12121a; color: #e0e0f0;
                border: 1px solid #2a2a3e;
                font-size: 13px;
            }
            QMenu::item:selected { background: #2a2a4a; }
            QMenu::separator { height: 1px; background: #2a2a3e; margin: 3px 0; }
        """)

        a_setup = QAction('🎹  Configurar Teclas…', self)
        a_setup.triggered.connect(self._open_key_setup)
        m.addAction(a_setup)
        m.addSeparator()

        if self.presets:
            preset_menu = m.addMenu('Preset')
            for name, body in self.presets.items():
                label = name + ('  ✓' if name == self.active_preset else '')
                desc = body.get('description') or ''
                a = QAction(label, self)
                if desc:
                    a.setToolTip(desc)
                a.triggered.connect(lambda _checked=False, nn=name: self._apply_preset(nn))
                preset_menu.addAction(a)
            preset_menu.addSeparator()
            a_open = QAction('Abrir presets.toml…', self)
            a_open.triggered.connect(self._open_presets_file)
            preset_menu.addAction(a_open)
            a_reload = QAction('Recarregar presets', self)
            a_reload.triggered.connect(self._force_reload_presets)
            preset_menu.addAction(a_reload)
            m.addSeparator()

        a_kps = QAction('Mostrar KPS' + ('  ✓' if self.show_kps else ''), self)
        a_kps.triggered.connect(self._toggle_kps)
        m.addAction(a_kps)
        a_reset = QAction('Reset combo/KPS', self)
        a_reset.triggered.connect(self._reset_state)
        m.addAction(a_reset)
        m.addSeparator()
        a_quit = QAction('Fechar', self)
        a_quit.triggered.connect(self.close)
        m.addAction(a_quit)
        m.exec(pos)

    def _open_key_setup(self) -> None:
        dlg = KeySetupDialog(
            n_lanes=self.n_lanes,
            keys=self._key_list(),
            doubles=self.doubles,
            presets=self.presets,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        n, keys, doubles = dlg.get_result()
        valid_keys = [k for k in keys if k]
        if len(valid_keys) < n:
            default_n = min(DEFAULT_KEYS.keys(), key=lambda x: abs(x - n))
            defaults = DEFAULT_KEYS[default_n]
            idx = len(valid_keys)
            for dk in defaults:
                if len(valid_keys) >= n:
                    break
                if dk not in valid_keys:
                    valid_keys.append(dk)
                    idx += 1
        self.n_lanes = n
        self.doubles = doubles
        self.cfg['keys'] = valid_keys[:n]
        self.cfg['lanes'] = n
        self.cfg.pop('preset', None)
        self.active_preset = None
        self.state = State()
        self._save_config()
        self.update()
        self._force_reload_presets()

    def _open_presets_file(self) -> None:
        path = self._presets_path or (_script_dir() / PRESETS_FILENAME)
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fallback = _script_dir() / PRESETS_FILENAME
                if fallback.exists() and fallback != path:
                    path.write_text(fallback.read_text(encoding='utf-8'), encoding='utf-8')
                else:
                    path.touch()
            except OSError:
                return
        with contextlib.suppress(Exception):
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _force_reload_presets(self) -> None:
        self._presets_mtime = 0.0
        self._maybe_reload_presets()

    def _set_lanes(self, n: int) -> None:
        self.n_lanes = max(MIN_LANES, min(MAX_LANES, n))
        self.doubles = {}
        self.cfg['keys'] = DEFAULT_KEYS.get(
            self.n_lanes,
            DEFAULT_KEYS[min(DEFAULT_KEYS.keys(), key=lambda x: abs(x - self.n_lanes))],
        )
        self.cfg.pop('preset', None)
        self.active_preset = None
        self.state = State()
        self._save_config()
        self.update()

    def _toggle_kps(self) -> None:
        self.show_kps = not self.show_kps
        self._save_config()
        self.update()

    def _reset_state(self) -> None:
        self.state = State()
        self.update()

    def closeEvent(self, ev) -> None:
        self._save_config()
        with contextlib.suppress(Exception):
            self.listener.stop()
        super().closeEvent(ev)


def main() -> None:
    app = QApplication(sys.argv)
    w = OctopusWidget()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
