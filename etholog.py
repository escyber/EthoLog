"""EthoLog - Precision Behavior Scorer

Frame-accurate behavior scoring with a BORIS-compatible event log.

Main parts of this file:
  1. Pure logic (no Qt): ethogram read/write, event sorting, pairing check.
  2. Look and feel: light/dark themes.
  3. Small widgets: video label, info popup, dialogs, table delegates.
  4. The main window.
"""
import sys
import os
import csv
import json
import re
import html
import time
from datetime import datetime

import cv2

from PyQt6.QtCore import Qt, QTimer, QSettings, QPoint, QLocale, QRectF, QPointF, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QToolButton, QTableWidget, QTableWidgetItem, QFileDialog,
    QLabel, QLineEdit, QComboBox, QDialog, QFormLayout, QAbstractItemView,
    QMessageBox, QSlider, QFrame, QCheckBox, QDoubleSpinBox, QStyledItemDelegate,
    QHeaderView, QDialogButtonBox, QSizePolicy, QSplitter, QSplitterHandle, QToolTip,
)
from PyQt6.QtGui import (QImage, QPixmap, QKeyEvent, QFont, QDoubleValidator, QGuiApplication, QColor, QBrush,
                         QPainter, QPen, QPolygonF, QIcon)


# ---------------------------------------------------------------------------
# 1. Pure logic (no Qt)
# ---------------------------------------------------------------------------

LOG_HEADERS = [
    "Observation id", "Observation date", "Description", "Observation duration",
    "Observation type", "Source", "Time offset (s)", "Media duration (s)", "FPS",
    "Subject", "Behavior", "Behavioral category", "Behavior type",
    "Time", "Media file name", "Image index", "Image file path", "Comment",
]  # identical to a BORIS "tabular events" export, so files work in both tools
# Columns shown by default. The others are still saved and exported.
VISIBLE_LOG_COLUMNS = ["Behavior", "Behavior type", "Time", "Image index", "Comment"]


PLAY_SPEEDS = (1.0, 0.5, 0.25)      # click the Speed button to cycle; 1.0 = real time
MIN_FRAME = 1   # scoring starts at frame 1 (frame 0 is never scored)
DEFAULT_ETHOGRAM_VERSION = "example"


def version_from_filename(path):
    """'ethogram_v0.1.1.csv' -> '0.1.1'. Returns '' if the name has no version."""
    m = re.search(r"[vV](\d+(?:\.\d+)+)", os.path.basename(path))
    return m.group(1) if m else ""


def normalize_type(text):
    """'State event', 'state', 'STATE' -> 'State'. 'Point event', 'point' -> 'Point'."""
    return "Point" if str(text).strip().lower().startswith("p") else "State"


def parse_ethogram_rows(rows):
    """Turn csv rows into [(key, name, type, description), ...].

    Accepts files with or without a header row, and with 3 or 4 columns
    (key, behavior, type[, description]). Types may be 'State event' / 'Point event'.
    """
    out = []
    for i, row in enumerate(rows):
        row = [c.strip() for c in row]
        if len(row) < 2 or not any(row):
            continue
        if i == 0 and row[0].lower() in ("key", "shortcut", "shortcut key"):
            continue
        key = row[0][:1].upper()
        name = row[1]
        b_type = normalize_type(row[2]) if len(row) >= 3 and row[2] else "State"
        desc = row[3] if len(row) >= 4 else ""
        if key and name:
            out.append((key, name, b_type, desc))
    return out


def ethogram_to_rows(items):
    """[(key, name, type, desc)] -> csv rows including a header."""
    rows = [["Key", "Behavior", "Type", "Description"]]
    for key, name, b_type, desc in items:
        rows.append([key, name, "Point event" if b_type == "Point" else "State event", desc])
    return rows


def check_ethogram(items):
    """Return a list of problems (empty list = fine)."""
    problems, keys, names = [], set(), set()
    for key, name, b_type, _ in items:
        if len(key) != 1:
            problems.append(f"'{name or '(no name)'}': the shortcut key must be exactly one character.")
        if not name:
            problems.append(f"Key '{key}': the behavior needs a name.")
        if key in keys:
            problems.append(f"Key '{key}' is used twice.")
        if name and name.lower() in names:
            problems.append(f"Behavior '{name}' is listed twice.")
        keys.add(key)
        names.add(name.lower())
    return problems


def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def sort_rows_by_time(rows, time_idx):
    """Stable sort of log rows by their Time column (rows with no time go last)."""
    def k(item):
        pos, row = item
        t = to_float(row[time_idx])
        return (t is None, t if t is not None else 0.0, pos)
    return [r for _, r in sorted(enumerate(rows), key=k)]


def find_unpaired(rows, beh_idx, type_idx, time_idx, known_behaviors=None):
    """Check a time-sorted event log.

    Returns a list of (row_index, message). Problems found:
      - STOP with no START before it
      - START while the same behavior is already running
      - START that never gets a STOP
      - behavior type that is not START / STOP / POINT
      - behavior not in the ethogram (only if known_behaviors is given)
    """
    issues = []
    open_at = {}
    for i, row in enumerate(rows):
        name = row[beh_idx].strip()
        flag = row[type_idx].strip().upper()
        t = to_float(row[time_idx])
        if t is None:
            issues.append((i, f"{name or '(no behavior)'}: missing or invalid time"))
        if known_behaviors is not None and name and name not in known_behaviors:
            issues.append((i, f"{name}: not in the current ethogram"))
        if flag == "START":
            if name in open_at:
                issues.append((i, f"{name}: START at {row[time_idx]} s while it already started at "
                                  f"{rows[open_at[name]][time_idx]} s (missing STOP)"))
            open_at[name] = i
        elif flag == "STOP":
            if name in open_at:
                del open_at[name]
            else:
                issues.append((i, f"{name}: STOP at {row[time_idx]} s has no START before it"))
        elif flag == "POINT":
            pass
        else:
            issues.append((i, f"{name}: behavior type '{row[type_idx]}' should be START, STOP or POINT"))
    for name, i in open_at.items():
        issues.append((i, f"{name}: START at {rows[i][time_idx]} s never stops (missing STOP)"))
    return sorted(issues)


def is_open_at(rows, beh_idx, type_idx, time_idx, behavior, t):
    """Is `behavior` running at time t? True if its latest event up to t is a START."""
    state = False
    for row in rows:
        if row[beh_idx].strip() != behavior:
            continue
        rt = to_float(row[time_idx])
        if rt is None or rt > t:
            continue
        flag = row[type_idx].strip().upper()
        if flag == "START":
            state = True
        elif flag == "STOP":
            state = False
    return state


# ---- Gap check (which frames have no behavior marked) ----
# Colors match the Behavior Agreement viewer. climbing, body_flick, ear_flick and defecation
# are not in the viewer's list yet, so they get their own colors here.
BEHAVIOR_COLORS = {
    "rearing": "#6C63FF", "supported_rearing": "#6C63FF", "unsupported_rearing": "#8E88FF",
    "exploration_locomotive": "#5AC8FA", "exploration_stationary": "#FFD60A",
    "face_swipe": "#FF2D92", "walking": "#34C759", "resting": "#AEAEB2",
    "face_grooming": "#BF5AF2", "body_grooming": "#FF9F0A", "digging": "#30B0C7",
    "push_digging": "#1D8FA3", "unchained_grooming": "#D98BFF", "freezing": "#8E8E93",
    "unclear": "#E5484D", "other": "#C7A27C",
    "climbing": "#8D6E63", "body_flick": "#FF7043", "ear_flick": "#F06292", "defecation": "#795548",
}
FALLBACK_COLORS = ["#4F8EF7", "#F78C4F", "#45B58A", "#B5457F", "#7F9A2E", "#A3702F", "#3B7A8C", "#9B59B6"]
AUTOSAVE_DIR = os.path.join(os.path.expanduser("~"), "EthoLog_autosave")
IMG_COL = LOG_HEADERS.index("Image index")
TIME_COL = LOG_HEADERS.index("Time")


def norm_behavior(name):
    return str(name).strip().lower().replace("-", "_").replace(" ", "_")


def row_frame(row, fps):
    """Frame number of a log row: the Image index if it is a number, otherwise Time x FPS."""
    try:
        return int(float(row[IMG_COL]))
    except (ValueError, IndexError, TypeError):
        t = to_float(row[TIME_COL]) if len(row) > TIME_COL else None
        return int(round(t * fps)) if t is not None else None


def build_intervals(events, total_frames=None):
    """events: [(behavior, type, frame)] in any order -> {behavior: [(start, stop_or_None), ...]}.
    Stop frames are inclusive. A POINT is a one-frame interval. A START with no STOP gives stop=None."""
    ordered = sorted((e for e in events if e[2] is not None),
                     key=lambda e: (e[2], 0 if str(e[1]).upper() == "STOP" else 1))
    open_starts, intervals = {}, {}
    for beh, typ, frame in ordered:
        typ = str(typ).strip().upper()
        if total_frames:
            frame = max(0, min(int(frame), total_frames - 1))
        if typ == "START":
            if beh in open_starts:
                intervals.setdefault(beh, []).append((open_starts.pop(beh), None))
            open_starts[beh] = frame
        elif typ == "STOP":
            if beh in open_starts:
                intervals.setdefault(beh, []).append((open_starts.pop(beh), frame))
        elif typ == "POINT":
            intervals.setdefault(beh, []).append((frame, frame))
    for beh, start in open_starts.items():
        intervals.setdefault(beh, []).append((start, None))
    for beh in intervals:
        intervals[beh].sort(key=lambda iv: iv[0])
    return intervals


def find_unscored(intervals, total_frames, first_frame=0):
    """Stretches of frames (inclusive) with NO behavior marked, from first_frame to the last frame.
    A START with no STOP counts as running to the end of the video (the log check reports it separately)."""
    if not total_frames:
        return []
    diff = [0] * (total_frames + 1)
    for ivs in intervals.values():
        for a, b in ivs:
            b = total_frames - 1 if b is None else b
            if b < a:
                a, b = b, a
            a, b = max(0, a), min(total_frames - 1, b)
            diff[a] += 1
            diff[b + 1] -= 1
    gaps, cover, start = [], 0, None
    for f in range(total_frames):
        cover += diff[f]
        if f < first_frame:
            continue
        if cover <= 0 and start is None:
            start = f
        elif cover > 0 and start is not None:
            gaps.append((start, f - 1))
            start = None
    if start is not None:
        gaps.append((start, total_frames - 1))
    return gaps


def assign_layers(intervals, total_frames):
    """Flat list for ONE strip: [(start, stop, behavior, layer, open_ended)].
    Where events overlap, the longest fills the strip (layer 0), the next sits on the top half
    (layer 1) and the third on the top quarter (layer 2), like the Behavior Agreement viewer."""
    flat = []
    for beh, ivs in intervals.items():
        for a, b in ivs:
            end = max(a, total_frames - 1) if b is None else b
            flat.append([a, end, beh, 0, b is None])
    flat.sort(key=lambda e: (-(e[1] - e[0]), e[0]))
    for i, e in enumerate(flat):
        rank = sum(1 for j in range(i) if flat[j][0] <= e[1] and e[0] <= flat[j][1])
        e[3] = min(rank, 2)
    flat.sort(key=lambda e: (e[3], e[0]))
    return [tuple(e) for e in flat]


# ---------------------------------------------------------------------------
# 2. Look and feel
# ---------------------------------------------------------------------------

THEMES = {
    "light": dict(
        bg="#F5F5F7", card="#FFFFFF", text="#1D1D1F", sub="#6E6E73", border="#D9D9DE",
        accent="#007AFF", accent_hover="#0A6CFF", accent_text="#FFFFFF",
        soft="#F0F0F3", soft_hover="#E6E6EA", sel="rgba(0,122,255,0.16)",
        danger="#D70015", ok="#1E8E3E", warn="#B25E00", bad_row="#FDE9E7", gap_row="#FFF1B8", head="#3C3C43", head_line="#C7C7CC", run="#007AFF",
    ),
    "dark": dict(
        bg="#1C1C1E", card="#2C2C2E", text="#F5F5F7", sub="#9A9AA0", border="#3D3D40",
        accent="#0A84FF", accent_hover="#3395FF", accent_text="#FFFFFF",
        soft="#3A3A3C", soft_hover="#48484A", sel="rgba(10,132,255,0.30)",
        danger="#FF6961", ok="#30D158", warn="#FFB340", bad_row="#3A2528", gap_row="#4A431F", head="#FF9F0A", head_line="#FF9F0A", run="#FF9F0A",
    ),
}


def build_qss(p):
    return f"""
    QMainWindow, QDialog {{ background: {p['bg']}; }}
    QWidget {{ color: {p['text']}; font-size: 15px; }}
    QWidget#central {{ background: {p['bg']}; }}
    QFrame#card {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 14px; }}
    QLabel {{ background: transparent; }}
    QLabel#title {{ font-size: 24px; font-weight: 700; }}
    QLabel#sub {{ color: {p['sub']}; font-size: 14px; }}
    QLabel#section {{ font-size: 16px; font-weight: 600; }}
    QLabel#badgeOk {{ color: {p['ok']}; font-weight: 600; }}
    QLabel#badgeWarn {{ color: {p['warn']}; font-weight: 600; }}
    QPushButton {{
        background: {p['soft']}; border: none; border-radius: 9px; padding: 8px 16px; font-weight: 500;
    }}
    QPushButton:hover {{ background: {p['soft_hover']}; }}
    QPushButton:disabled {{ color: {p['sub']}; }}
    QPushButton:checked {{ background: {p['sel']}; color: {p['accent']}; }}
    QPushButton[variant="primary"] {{ background: {p['accent']}; color: {p['accent_text']}; }}
    QPushButton[variant="primary"]:hover {{ background: {p['accent_hover']}; }}
    QPushButton[variant="danger"]:hover {{ color: {p['danger']}; }}
    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:hover {{ background: {p['sel']}; border-radius: 3px; }}
    QToolButton#info {{
        background: transparent; color: {p['sub']}; border: 1px solid {p['border']};
        border-radius: 11px; min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
        font-size: 12px; font-weight: 700; font-style: italic;
    }}
    QToolButton#info:hover {{ color: {p['accent']}; border-color: {p['accent']}; }}
    QToolButton#theme {{
        background: {p['soft']}; border: none; border-radius: 16px; font-size: 16px;
        min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px;
    }}
    QToolButton#theme:hover {{ background: {p['soft_hover']}; }}
    QFrame#infoPopup {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 10px; }}
    QTableWidget {{
        background: {p['card']}; border: none; gridline-color: transparent;
        selection-background-color: {p['sel']}; selection-color: {p['text']};
        alternate-background-color: {p['card']};
    }}
    QTableWidget::item {{ padding: 4px 6px; border-bottom: 1px solid {p['border']}; }}
    QHeaderView {{ background: {p['card']}; border: none; }}
    QHeaderView::section {{
        background: {p['card']}; color: {p['head']}; border: none;
        border-bottom: 2px solid {p['head_line']}; padding: 9px 8px; font-weight: 700; font-size: 14px;
    }}
    QTableCornerButton::section {{ background: {p['card']}; border: none; }}
    QLineEdit, QComboBox, QDoubleSpinBox {{
        background: {p['soft']}; border: 1px solid transparent; border-radius: 8px; padding: 5px 8px;
    }}
    QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {p['accent']}; }}
    QComboBox QAbstractItemView {{
        background: {p['card']}; selection-background-color: {p['sel']}; selection-color: {p['text']};
        border: 1px solid {p['border']};
    }}
    QSlider::groove:horizontal {{ height: 4px; background: {p['soft_hover']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: #FFFFFF; border: 1px solid {p['border']}; width: 16px; height: 16px;
        margin: -7px 0; border-radius: 8px;
    }}
    QCheckBox {{ spacing: 6px; }}
    QMessageBox {{ background: {p['card']}; }}
    QToolTip {{ background: {p['card']}; color: {p['text']}; border: 1px solid {p['border']}; padding: 4px; }}
    """


# ---------------------------------------------------------------------------
# 3. Small widgets
# ---------------------------------------------------------------------------

class VideoLabel(QLabel):
    """Shows a video frame scaled to fit, keeping the original aspect ratio."""

    double_clicked = pyqtSignal()

    def __init__(self, text="", min_h=200):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(160, min_h)
        self.setStyleSheet("background-color: #000000; color: #9A9AA0; border-radius: 10px;")
        self._img = None

    def set_image(self, qimg):
        self._img = qimg
        self._rescale()

    def clear_image(self, text=""):
        self._img = None
        self.clear()
        self.setText(text)

    def _rescale(self):
        if self._img is None:
            return
        pix = QPixmap.fromImage(self._img).scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pix)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()


class InfoPopup(QFrame):
    """A small explanation bubble that stays open until you click elsewhere.
    It always stays fully inside the app window / screen."""

    def __init__(self, text, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("infoPopup")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.TextFormat.RichText)
        lab.setFixedWidth(320)
        lay.addWidget(lab)

    def show_below(self, widget):
        self.layout().activate()
        self.adjustSize()
        w, h = self.width(), self.height()
        below = widget.mapToGlobal(QPoint(0, widget.height() + 6))
        above = widget.mapToGlobal(QPoint(0, -h - 6))
        screen = QGuiApplication.screenAt(below) or QGuiApplication.primaryScreen()
        bounds = screen.availableGeometry()
        parent = self.parent()
        if parent is not None:
            win = parent.frameGeometry().intersected(bounds)
            if win.width() >= w + 16 and win.height() >= h + 16:
                bounds = win
        x = min(max(below.x(), bounds.left() + 8), bounds.right() - w - 8)
        y = below.y()
        if y + h > bounds.bottom() - 8:      # no room below -> open above the button
            y = above.y()
        y = min(max(y, bounds.top() + 8), bounds.bottom() - h - 8)
        self.move(x, y)
        self.show()


def info_button(text, owner):
    """The small 'i' button. Click shows the explanation; hover shows it as a tooltip."""
    btn = QToolButton()
    btn.setObjectName("info")
    btn.setText("i")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setToolTip(text.replace("<br>", "\n").replace("<b>", "").replace("</b>", ""))

    def show():
        pop = InfoPopup(text, owner)
        owner._popups = [pop]  # keep a reference so it is not garbage collected
        pop.show_below(btn)
    btn.clicked.connect(show)
    return btn


def with_info(button, text, owner):
    """A button with its 'i' next to it."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lay.addWidget(button, 1)
    lay.addWidget(info_button(text, owner))
    return w


def card():
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    return f, lay


def section_header(title, info_text, owner, extra_widget=None):
    row = QHBoxLayout()
    lab = QLabel(title)
    lab.setObjectName("section")
    row.addWidget(lab)
    row.addWidget(info_button(info_text, owner))
    row.addStretch(1)
    if extra_widget is not None:
        row.addWidget(extra_widget)
    return row


class LogTable(QTableWidget):
    """Event log table. Arrow keys move around the table when it has focus.
    Every other key (behavior keys, Space, Cmd+Z ...) is passed to the main window so scoring keeps working."""

    NAV = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right,
           Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.forward = None
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    def keyPressEvent(self, event):
        mods = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        if event.key() in self.NAV and not mods:
            super().keyPressEvent(event)
        elif self.forward:
            self.forward(event)


class ComboDelegate(QStyledItemDelegate):
    """Dropdown editor for a table column. `items` can be a list or a function returning a list."""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self._items = items

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        box.addItems(self._items() if callable(self._items) else self._items)
        return box

    def setEditorData(self, editor, index):
        text = str(index.data())
        i = editor.findText(text)
        if i < 0:
            editor.addItem(text)
            i = editor.findText(text)
        editor.setCurrentIndex(i)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText())


class TimeDelegate(QStyledItemDelegate):
    """Only allows numbers (seconds) in the Time column."""

    def createEditor(self, parent, option, index):
        edit = QLineEdit(parent)
        v = QDoubleValidator(0.0, 1e7, 3, edit)
        v.setNotation(QDoubleValidator.Notation.StandardNotation)
        v.setLocale(QLocale(QLocale.Language.C))
        edit.setValidator(v)
        return edit


class EditEthogramDialog(QDialog):
    """Add, remove, reorder and edit behaviors."""

    COLS = ["Key", "Behavior", "Type", "Description"]

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Ethogram")
        self.resize(820, 520)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        hint = QLabel("Double-click a cell to edit it. Keys are single characters and cannot repeat.")
        hint.setObjectName("sub")
        lay.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 56)
        self.table.setColumnWidth(2, 90)
        lay.addWidget(self.table, 1)
        for key, name, b_type, desc in items:
            self.add_row(key, name, b_type, desc)

        row = QHBoxLayout()
        for text, fn, variant in (
            ("Add behavior", lambda: self.add_row("", "", "State", "", select=True), "primary"),
            ("Remove selected", self.remove_selected, "danger"),
            ("Move up", lambda: self.move_selected(-1), None),
            ("Move down", lambda: self.move_selected(1), None),
        ):
            b = QPushButton(text)
            if variant:
                b.setProperty("variant", variant)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.try_accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def add_row(self, key, name, b_type, desc, select=False):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(key))
        self.table.setItem(r, 1, QTableWidgetItem(name))
        combo = QComboBox()
        combo.addItems(["State", "Point"])
        combo.setCurrentText(b_type)
        self.table.setCellWidget(r, 2, combo)
        self.table.setItem(r, 3, QTableWidgetItem(desc))
        if select:
            self.table.selectRow(r)
            self.table.editItem(self.table.item(r, 0))
            self.table.scrollToBottom()

    def remove_selected(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def move_selected(self, delta):
        r = self.table.currentRow()
        t = r + delta
        if r < 0 or t < 0 or t >= self.table.rowCount():
            return
        a, b = self.get_items()[r], self.get_items()[t]
        self._set_row(r, b)
        self._set_row(t, a)
        self.table.selectRow(t)

    def _set_row(self, r, item):
        key, name, b_type, desc = item
        self.table.item(r, 0).setText(key)
        self.table.item(r, 1).setText(name)
        self.table.cellWidget(r, 2).setCurrentText(b_type)
        self.table.item(r, 3).setText(desc)

    def get_items(self):
        out = []
        for r in range(self.table.rowCount()):
            def txt(c):
                it = self.table.item(r, c)
                return it.text().strip() if it else ""
            out.append((txt(0).upper(), txt(1), self.table.cellWidget(r, 2).currentText(), txt(3)))
        return out

    def try_accept(self):
        problems = check_ethogram(self.get_items())
        if problems:
            QMessageBox.warning(self, "Please fix", "\n".join(problems))
            return
        self.accept()


class AddEventDialog(QDialog):
    """Insert a missed event at any time. The log re-sorts by time automatically."""

    def __init__(self, ethogram, default_time, max_time, fps, is_open_fn, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Event")
        self.ethogram = ethogram          # {key: (name, type, desc)}
        self.fps = fps
        self.is_open_fn = is_open_fn
        self.names = {v[0]: v[1] for v in ethogram.values()}
        form = QFormLayout(self)
        form.setSpacing(10)

        self.beh = QComboBox()
        self.beh.addItems(list(self.names.keys()))
        self.kind = QComboBox()
        self.time = QDoubleSpinBox()
        self.time.setDecimals(3)
        lo = MIN_FRAME / fps if fps else 0.04
        self.time.setRange(lo, max(max_time, lo))
        self.time.setSingleStep(1.0 / fps if fps else 0.04)
        self.time.setSuffix(" s")
        self.time.setValue(max(default_time, lo))
        self.frame_lab = QLabel("")
        self.frame_lab.setObjectName("sub")

        form.addRow("Behavior", self.beh)
        form.addRow("Event", self.kind)
        form.addRow("Time", self.time)
        form.addRow("", self.frame_lab)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

        self.beh.currentTextChanged.connect(self.refresh_kind)
        self.time.valueChanged.connect(self.refresh_kind)
        self.refresh_kind()

    def refresh_kind(self, *_):
        name = self.beh.currentText()
        b_type = self.names.get(name, "State")
        self.kind.clear()
        if b_type == "Point":
            self.kind.addItems(["POINT"])
        else:
            self.kind.addItems(["START", "STOP"])
            if self.is_open_fn(name, self.time.value()):
                self.kind.setCurrentText("STOP")
        self.frame_lab.setText(f"= frame {int(round(self.time.value() * self.fps))}")

    def get_data(self):
        return self.beh.currentText(), self.kind.currentText(), self.time.value()


# ---------------------------------------------------------------------------
# 4. Main window
# ---------------------------------------------------------------------------

INFO = {
    "ethogram": (
        "<b>Ethogram</b><br>The list of behaviors you can score, each with a one-key shortcut. "
        "Press its key, or just <b>click the behavior here</b>, to log it at the current frame. A <b>State</b> behavior (has a start and an end) toggles "
        "START / STOP on each press. A <b>Point</b> behavior logs a single moment."
    ),
    "edit_etho": (
        "<b>Edit Ethogram</b><br>Add, remove, rename or reorder behaviors, change their keys and type, "
        "and edit descriptions."
    ),
    "load_etho": (
        "<b>Load Ethogram</b><br>Replaces the list with one from a file. Reads <b>.csv</b> (or .txt)."
    ),
    "export_etho": (
        "<b>Export Ethogram</b><br>Saves the current list as a <b>.csv</b> file. "
        "It opens in Excel / Google Sheets and can be loaded back here."
    ),
    "add_video": (
        "<b>Add Video</b><br>Choose a video to score. Supports <b>.mp4, .avi, .mov, .mkv</b>."
    ),
    "preview": "<b>Look-ahead preview</b><br>Pick how many seconds to look ahead.",
    "log": (
        "<b>Event Log</b><br>Every key press adds a row (START, STOP or POINT) with its time and frame. "
        "Rows are always kept in time order. <b>Double-click</b> a cell to correct it "
        "(behavior, START/STOP, time, comment). Click a row to jump the video to that moment.<br>"
        "Tick 'Show all columns' to see every column."
    ),
    "add_event": (
        "<b>Add Event</b><br>To add a missed behavior, just move the video to that frame "
        "and press the behavior key. It lands in the right place and picks START or STOP for you. "
        "Use this button only to type an exact time."
    ),
    "delete": "<b>Delete Selected</b><br>Removes the highlighted row from the log.",
    "undo": "<b>Undo</b><br>Takes back the last change to the log (a key press, delete or edit). Shortcut: Cmd+Z.",
    "look_btn": "<b>Look-ahead on/off</b><br>Shows or hides the look-ahead player so the video can use the space.",
    "focus": "<b>Big video</b><br>Double-click the video to hide everything else. Keys, Space and the arrows still work. Double-click again or press Esc to go back.",
    "load_log": "<b>Load Log</b><br>Opens a saved event log: <b>.tsv</b> or <b>.csv</b>.",
    "export_log": "<b>Export Log</b><br>Saves the log as <b>.tsv</b> (tab-separated, recommended) or <b>.csv</b>.",
    "badge": (
        "<b>Pairing check</b><br>Every state behavior needs a START and a STOP. "
        "This shows how many problems the log has right now. The same check runs before export."
    ),
}


def behavior_color(name, _cache={}):
    key = norm_behavior(name)
    if key in BEHAVIOR_COLORS:
        return QColor(BEHAVIOR_COLORS[key])
    if key not in _cache:
        _cache[key] = FALLBACK_COLORS[len(_cache) % len(FALLBACK_COLORS)]
    return QColor(_cache[key])


def color_dot_icon(color, size=12):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


class ArrowHandle(QSplitterHandle):
    """Splitter handle with a small arrow: drag it to resize, or just click it to collapse / reopen the panel."""
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self._press = None
        self._moved = False
        self.setCursor(Qt.CursorShape.SplitVCursor if orientation == Qt.Orientation.Vertical else Qt.CursorShape.SplitHCursor)

    def paintEvent(self, event):
        sp = self.splitter()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QColor(sp.ink)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(c))
        cx, cy, a = self.width() / 2.0, self.height() / 2.0, 5.0
        closed = sp.panel_closed()
        if self.orientation() == Qt.Orientation.Vertical:       # horizontal bar: arrow up/down
            pts = [(-a, -a / 2), (a, -a / 2), (0, a / 2)] if not closed else [(-a, a / 2), (a, a / 2), (0, -a / 2)]
        else:                                                   # vertical bar: arrow left/right
            pts = [(a / 2, -a), (a / 2, a), (-a / 2, 0)] if not closed else [(-a / 2, -a), (-a / 2, a), (a / 2, 0)]
        p.drawPolygon(QPolygonF([QPointF(cx + x, cy + y) for x, y in pts]))
        p.end()

    def mousePressEvent(self, event):
        self._press = event.globalPosition().toPoint()
        self._moved = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is not None and (event.globalPosition().toPoint() - self._press).manhattanLength() > 4:
            self._moved = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self._press is not None and not self._moved:
            self.splitter().toggle_panel()
        self._press = None


class ArrowSplitter(QSplitter):
    """QSplitter whose handle shows an arrow. panel_index is the panel that the click collapses / reopens."""
    def __init__(self, orientation, panel_index, parent=None):
        super().__init__(orientation, parent)
        self.panel_index = panel_index
        self.ink = QColor("#888888")
        self._saved = None
        self.setChildrenCollapsible(False)
        self.setHandleWidth(16)

    def createHandle(self):
        return ArrowHandle(self.orientation(), self)

    def set_ink(self, color):
        self.ink = QColor(color)
        for i in range(1, self.count()):
            self.handle(i).update()

    def panel_closed(self):
        sizes = self.sizes()
        return len(sizes) > self.panel_index and sizes[self.panel_index] == 0

    def toggle_panel(self):
        sizes = self.sizes()
        total = sum(sizes)
        i = self.panel_index
        if len(sizes) < 2 or total <= 0:
            return
        if sizes[i] > 0:
            self._saved = list(sizes)
            new = [0] * len(sizes)
            new[1 - i] = total
        else:
            new = self._saved if self._saved and self._saved[i] > 0 else None
            if new is None:
                new = [0] * len(sizes)
                new[i] = int(total * 0.4) if i == 1 else min(340, total // 3)
                new[1 - i] = total - new[i]
        self.setSizes(new)
        self.splitterMoved.emit(new[0], 1)
        for h in range(1, self.count()):
            self.handle(h).update()


class ClickLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class GapStrip(QWidget):
    """One slim strip for the whole video. Scored stretches are colored by behavior, frames with
    nothing marked are dark (white in dark mode). Click to jump; click a dark marker to find it in the log."""
    frameRequested = pyqtSignal(int)
    gapClicked = pyqtSignal(int, int)

    HEIGHT = 18
    TRI = 7          # room above the strip for the gap triangles
    PAD = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(self.HEIGHT + 2 * self.PAD + self.TRI)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.total_frames = 0
        self.fps = 25.0
        self.current_frame = 0
        self.segments = []
        self.gaps = []
        self._cache = {}
        self._dragging = False
        self.view_start = 0
        self.view_span = 1
        self.set_theme(THEMES["light"])

    def set_theme(self, p):
        self.bg, self.line = QColor(p["soft"]), QColor(p["border"])
        self.ink, self.playhead = QColor(p["text"]), QColor(p["accent"])
        self.update()

    def set_video(self, total_frames, fps):
        self.total_frames, self.fps = max(0, int(total_frames)), fps or 25.0
        self.view_start, self.view_span = 0, max(1, self.total_frames)
        self.update()

    def set_data(self, segments, gaps):
        self.segments, self.gaps = segments, gaps
        self.update()

    def set_current_frame(self, frame):
        self.current_frame = frame
        if (self.view_span < self.total_frames and not self._dragging
                and not (self.view_start <= frame < self.view_start + self.view_span)):
            self.set_view(int(frame - self.view_span * 0.1), self.view_span)     # follow the playhead when zoomed in
        self.update()

    def set_view(self, start, span):
        span = max(1, min(int(span), max(1, self.total_frames)))
        start = max(0, min(int(start), max(0, self.total_frames - span)))
        self.view_start, self.view_span = start, span
        self.update()

    def zoom_seconds(self, seconds):
        """0 = whole video, otherwise show this many seconds around the current frame."""
        span = self.total_frames if not seconds else int(seconds * self.fps)
        centre = self.current_frame if seconds else 0
        self.set_view(centre - span // 2, span)

    def wheelEvent(self, event):
        dy = event.angleDelta().y()
        if not self.total_frames or dy == 0:
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:        # Ctrl + wheel = zoom
            anchor = self._frame_at(event.position().x())
            new_span = max(25, min(self.total_frames, int(self.view_span * (0.8 if dy > 0 else 1.25))))
            ratio = (anchor - self.view_start) / max(1, self.view_span)
            self.set_view(int(anchor - ratio * new_span), new_span)
        else:                                                              # wheel = scroll
            self.set_view(self.view_start - int(self.view_span * 0.15 * (1 if dy > 0 else -1)), self.view_span)
        event.accept()

    def color_for(self, name):
        return behavior_color(name)

    def not_scored_color(self):
        return QColor(self.ink)

    def _x(self, frame):
        return self.PAD + (frame - self.view_start) * (self.width() - 2 * self.PAD) / max(1, self.view_span)

    def _frame_at(self, x):
        f = self.view_start + (x - self.PAD) * max(1, self.view_span) / max(1, self.width() - 2 * self.PAD)
        return int(max(0, min(self.total_frames - 1, f)))

    def paintEvent(self, event):
        try:
            self._paint()
        except Exception:
            import traceback
            traceback.print_exc()

    def _paint(self):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        top, h = self.PAD + self.TRI, self.HEIGHT
        left, right = self.PAD, self.width() - self.PAD
        p.fillRect(left, top, right - left, h, self.bg)
        if not self.total_frames:
            p.end()
            return
        for a, b, name, layer, open_ended in self.segments:
            x0, x1 = self._x(a), self._x(b + 1)
            c = QColor(self.color_for(name))
            if open_ended:
                c.setAlpha(120)                       # started but not stopped yet
            hh = h if layer == 0 else (h // 2 if layer == 1 else h // 4)
            p.fillRect(QRectF(x0, top, max(1.5, x1 - x0), hh), c)
        dark = self.not_scored_color()
        for a, b in self.gaps:
            x0, x1 = self._x(a), self._x(b + 1)
            p.fillRect(QRectF(x0, top, max(3.0, x1 - x0), h), dark)   # never thinner than 3 px
        # orange triangle above every unscored stretch, so even a 1-frame gap is easy to spot
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#FF9F0A")))
        last_x = -99.0
        for a, b in self.gaps:
            gx = (self._x(a) + max(self._x(b + 1), self._x(a) + 3)) / 2.0
            if gx < left or gx > right or gx - last_x < 5:
                continue
            last_x = gx
            p.drawPolygon(QPolygonF([QPointF(gx - 3.5, self.PAD), QPointF(gx + 3.5, self.PAD), QPointF(gx, top - 1)]))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setBrush(Qt.BrushStyle.NoBrush)       # the orange fill must not leak into the outline
        p.setPen(QPen(self.line))
        p.drawRect(left, top, right - left - 1, h - 1)
        cx = self._x(self.current_frame + 0.5)
        p.setPen(QPen(self.playhead, 2))
        p.drawLine(int(cx), 0, int(cx), self.height())
        p.end()

    def gap_near(self, x):
        """The gap marker under (or within 3 px of) x, as (start, stop), or None."""
        for a, b in self.gaps:
            x0, x1 = self._x(a), max(self._x(b + 1), self._x(a) + 3)
            if x0 - 3 <= x <= x1 + 3:      # anywhere on the dark/white stretch counts
                return (a, b)
        return None

    def behaviors_at(self, frame):
        names = []
        for a, b, name, _layer, _open in self.segments:
            if a <= frame <= b and name not in names:
                names.append(name)
        return names

    def _press_at(self, x):
        gap = self.gap_near(x)
        if gap:
            self.gapClicked.emit(gap[0], gap[1])      # the window jumps to the gap start and shows it in the log
        else:
            self.frameRequested.emit(self._frame_at(x))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.total_frames:
            self._dragging = True
            self._press_at(event.position().x())

    def mouseMoveEvent(self, event):
        if not self.total_frames:
            return
        x = event.position().x()
        if self._dragging and not (event.buttons() & Qt.MouseButton.LeftButton):
            self._dragging = False
        if self._dragging:
            self.frameRequested.emit(self._frame_at(x))
            return
        gap = self.gap_near(x)
        f = gap[0] if gap else self._frame_at(x)
        if gap:
            what = "NOT SCORED: frames %d to %d (%d frames)\nClick to find it in the Event Log" % (gap[0], gap[1], gap[1] - gap[0] + 1)
        else:
            names = self.behaviors_at(f)
            what = ", ".join(names) if names else "-"
        QToolTip.showText(event.globalPosition().toPoint(), "Frame %d  |  %.2f s\n%s" % (f, f / self.fps, what), self)

    def mouseReleaseEvent(self, event):
        self._dragging = False


class BehaviorScorer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EthoLog - Precision Behavior Scorer")
        self.resize(1360, 860)

        self.cap = None
        self.prev_cap = None
        self.fps = 25.0
        self.total_frames = 0
        self.current_frame = 0
        self.video_duration = 0.0
        self.is_playing = False
        self.video_path = ""
        self.obs_date = ""
        self._was_playing_before_scrub = False
        self._next_pos = -1        # next frame index the main capture will return
        self._popups = []

        self.ethogram = {}         # key -> (name, type, description); keeps order

        self.settings = QSettings("EthoLog", "BehaviorScorer")
        self.theme = self._initial_theme()

        self.timer = QTimer()
        self.timer.timeout.connect(self.next_frame)

        # look-ahead preview player
        self.prev_timer = QTimer()
        self.prev_timer.timeout.connect(self.preview_tick)
        self.prev_debounce = QTimer()
        self.prev_debounce.setSingleShot(True)
        self.prev_debounce.timeout.connect(self.restart_preview)
        self.prev_start = 0
        self.prev_end = 0
        self.prev_idx = 0
        self.prev_next_pos = -1

        self.play_speed = 1.0
        self.focus_mode = False
        self.etho_version = ""
        self.etho_edited = False
        self._saved_split = None
        self._cache_dirty = True
        self._events = {}
        self._running_names = None
        self._undo = []
        self._prev_rows = []
        self.last_event_text = ""
        self.gap_rows = set()        # log rows next to the gap the user clicked (shown in yellow)
        self._row_frames = []
        self._applying_order = False

        self.beh_col = LOG_HEADERS.index("Behavior")
        self.type_col = LOG_HEADERS.index("Behavior type")
        self.time_col = LOG_HEADERS.index("Time")

        self.init_ui()
        self.load_default_ethogram()
        self.apply_theme()

    # ---------- theme ----------
    def _initial_theme(self):
        saved = self.settings.value("theme", "")
        if saved in THEMES:
            return saved
        try:
            if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark:
                return "dark"
        except Exception:
            pass
        return "light"

    def apply_theme(self):
        QApplication.instance().setStyleSheet(build_qss(THEMES[self.theme]))
        self.theme_btn.setText("☀" if self.theme == "dark" else "☾")  # sun / moon
        self._running_names = None
        self.theme_btn.setToolTip("Switch to light mode" if self.theme == "dark" else "Switch to dark mode")
        if hasattr(self, "gap_strip"):
            self.gap_strip.set_theme(THEMES[self.theme])
        for sp in (getattr(self, 'body_split', None), getattr(self, 'center_split', None)):
            if sp is not None:
                sp.set_ink(THEMES[self.theme]["sub"])
        self.refresh_validation()

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.settings.setValue("theme", self.theme)
        self.apply_theme()
        self.centralWidget().setFocus()

    # ---------- UI ----------
    def init_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 20)
        root.setSpacing(14)

        # top bar
        top = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        t = QLabel("EthoLog")
        t.setObjectName("title")
        s = QLabel("Precision behavior scoring")
        s.setObjectName("sub")
        title_col.addWidget(t)
        title_col.addWidget(s)
        top.addLayout(title_col)
        top.addStretch(1)
        self.theme_btn = QToolButton()
        self.theme_btn.setObjectName("theme")
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.theme_btn.clicked.connect(self.toggle_theme)
        top.addWidget(self.theme_btn)
        root.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        # ----- sidebar: ethogram -----
        side, side_lay = card()
        side.setMinimumWidth(150)
        self.etho_ver_label = QLabel("")
        self.etho_ver_label.setObjectName("sub")
        etho_head = section_header("Ethogram", INFO["ethogram"], self, self.etho_ver_label)
        side_lay.addLayout(etho_head)
        self.ethogram_table = QTableWidget(0, 4)
        self.ethogram_table.setHorizontalHeaderLabels(["Key", "Behavior", "Type", "Description"])
        self.ethogram_table.verticalHeader().setVisible(False)
        self.ethogram_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ethogram_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.ethogram_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ethogram_table.cellClicked.connect(self.on_ethogram_clicked)
        self.ethogram_table.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        hh = self.ethogram_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.ethogram_table.setColumnWidth(0, 44)
        self.ethogram_table.setColumnWidth(2, 60)
        self.ethogram_table.setWordWrap(False)
        self.ethogram_table.setColumnHidden(3, True)
        side_lay.addWidget(self.ethogram_table, 1)

        edit_btn = self._btn("Edit Ethogram", self.edit_ethogram, primary=True)
        load_btn = self._btn("Load Ethogram", self.load_ethogram_csv)
        exp_btn = self._btn("Export Ethogram", self.export_ethogram_csv)
        side_lay.addWidget(with_info(edit_btn, INFO["edit_etho"], self))
        side_lay.addWidget(with_info(load_btn, INFO["load_etho"], self))
        side_lay.addWidget(with_info(exp_btn, INFO["export_etho"], self))
        self.side_card = side
        self.body_split = ArrowSplitter(Qt.Orientation.Horizontal, 0)
        self.body_split.addWidget(side)
        self.center_split = ArrowSplitter(Qt.Orientation.Vertical, 1)
        self.body_split.addWidget(self.center_split)
        self.body_split.setStretchFactor(0, 0)
        self.body_split.setStretchFactor(1, 1)
        self.body_split.setCollapsible(0, True)
        self.body_split.setCollapsible(1, False)
        body.addWidget(self.body_split, 1)

        # video card
        vcard, vlay = card()
        vrow = QHBoxLayout()
        vrow.setSpacing(12)
        self.video_frame_label = VideoLabel("Click 'Add Video' to start", min_h=240)
        self.video_frame_label.double_clicked.connect(self.toggle_focus_mode)
        self.video_frame_label.setToolTip("Double-click for a big video")
        vrow.addWidget(self.video_frame_label, 3)

        self.preview_panel = QWidget()
        pcol = QVBoxLayout(self.preview_panel)
        pcol.setContentsMargins(0, 0, 0, 0)
        pcol.setSpacing(8)
        self.preview_len = QComboBox()
        for sec in (2, 3, 5, 8):
            self.preview_len.addItem(f"{sec} s", sec)
        self.preview_len.setCurrentIndex(1)
        self.preview_len.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview_len.currentIndexChanged.connect(self.schedule_preview)
        hdr = section_header("Look-ahead", INFO["preview"], self)
        hdr.addWidget(self.preview_len)
        pcol.addLayout(hdr)
        self.preview_label = VideoLabel("Pause the video to preview what happens next", min_h=150)
        self.preview_label.setWordWrap(True)
        pcol.addWidget(self.preview_label, 1)
        self.preview_caption = QLabel("")
        self.preview_caption.setObjectName("sub")
        pcol.addWidget(self.preview_caption)
        self.preview_panel.setMinimumWidth(240)
        vrow.addWidget(self.preview_panel, 1)
        vlay.addLayout(vrow, 1)

        self.timeline_label = QLabel("Frame: 0/0  |  Time: 0.000 / 0.000 s")
        self.timeline_label.setTextFormat(Qt.TextFormat.RichText)
        self.timeline_label.setObjectName("sub")
        vlay.addWidget(self.timeline_label)
        self.status_label = QLabel("")
        self.status_label.setObjectName("section")
        vlay.addWidget(self.status_label)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        self.play_pause_btn = self._btn("Play", self.toggle_play_pause)
        self.play_pause_btn.setEnabled(False)
        self.play_pause_btn.setMinimumWidth(80)
        ctrl.addWidget(self.play_pause_btn)
        self.speed_btn = self._btn("Speed 1x", self.cycle_speed)
        self.speed_btn.setToolTip("Playback speed: click to switch between 1x, 0.5x and 0.25x")
        ctrl.addWidget(self.speed_btn)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderPressed.connect(self.on_seek_pressed)
        self.seek_slider.sliderMoved.connect(self.on_seek_moved)
        self.seek_slider.sliderReleased.connect(self.on_seek_released)
        ctrl.addWidget(self.seek_slider, 1)
        self.preview_on = QPushButton("Look-ahead")
        self.preview_on.setCheckable(True)
        self.preview_on.setChecked(str(self.settings.value("lookahead", "1")) != "0")
        self.preview_on.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview_on.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_on.toggled.connect(self.on_preview_toggled)
        self.preview_panel.setVisible(self.preview_on.isChecked())
        ctrl.addWidget(self.preview_on)
        add_video_btn = self._btn("Add Video", self.open_video, primary=True)
        ctrl.addWidget(with_info(add_video_btn, INFO["add_video"], self))
        vlay.addLayout(ctrl)

        # Gap check: one slim strip, scored vs not scored (can be switched off)
        gap_head = QHBoxLayout()
        gap_head.setSpacing(8)
        self.gap_check_cb = QCheckBox("Check for unlabeled frames")
        self.gap_check_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.gap_check_cb.setChecked(str(self.settings.value("gapcheck", "1")) != "0")
        self.gap_check_cb.toggled.connect(self.on_gap_toggled)
        gap_head.addWidget(self.gap_check_cb)
        self.gap_label = QLabel("")
        self.gap_label.setObjectName("sub")
        gap_head.addWidget(self.gap_label, 1)
        self.zoom_btns = []
        for text, secs in (("Whole video", 0), ("2 min", 120), ("30 s", 30), ("10 s", 10)):
            zb = self._btn(text, lambda *_, sec=secs: self.gap_strip.zoom_seconds(sec))
            zb.setToolTip("Zoom the timeline (Ctrl + mouse wheel also zooms, the wheel scrolls)")
            gap_head.addWidget(zb)
            self.zoom_btns.append(zb)
        self.gap_prev_btn = self._btn("◀ Gap", lambda *_: self.jump_to_gap(-1))
        self.gap_next_btn = self._btn("Gap ▶", lambda *_: self.jump_to_gap(1))
        for gb in (self.gap_prev_btn, self.gap_next_btn):
            gb.setToolTip("Jump to the previous / next stretch where no behavior is marked")
            gap_head.addWidget(gb)
        vlay.addLayout(gap_head)
        self.gap_strip = GapStrip()
        self.gap_strip.frameRequested.connect(self.seek_to_frame)
        self.gap_strip.gapClicked.connect(self.on_gap_clicked)
        vlay.addWidget(self.gap_strip)
        self.video_card = vcard
        self.center_split.addWidget(vcard)

        # log card
        lcard, llay = card()
        self.badge = ClickLabel("")
        self.badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.badge.clicked.connect(self.jump_to_issue)
        self._issue_i = -1
        self.badge.setObjectName("badgeOk")
        self.show_all_cols = QCheckBox("Show all columns")
        self.show_all_cols.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.show_all_cols.toggled.connect(self.update_column_visibility)
        head = section_header("Event Log", INFO["log"], self)
        head.addWidget(self.badge)
        head.addSpacing(12)
        head.addWidget(self.show_all_cols)
        head.addSpacing(12)
        self.reset_cols_btn = self._btn("Reset columns", lambda *_: self.reset_col_order())
        self.reset_cols_btn.setToolTip("Put the columns back in BORIS order. Exports always use the BORIS order anyway.")
        head.addWidget(self.reset_cols_btn)
        llay.addLayout(head)

        self.log_table = LogTable(0, len(LOG_HEADERS))
        self.log_table.forward = self.keyPressEvent
        self.log_table.setHorizontalHeaderLabels(LOG_HEADERS)
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        hh = self.log_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(50)
        self._applying_widths = False
        self.col_widths = self.load_col_widths()
        self.apply_col_widths()
        hh.sectionResized.connect(self.on_col_resized)
        hh.setSectionsMovable(True)      # drag column headers to reorder; the export order never changes
        hh.setToolTip("Drag a column header to move it. Exports always use the BORIS column order.")
        hh.sectionMoved.connect(self.on_col_moved)
        self.restore_col_order()
        self.log_table.setItemDelegateForColumn(
            self.beh_col, ComboDelegate(lambda: [v[0] for v in self.ethogram.values()], self.log_table))
        self.log_table.setItemDelegateForColumn(
            self.type_col, ComboDelegate(["START", "STOP", "POINT"], self.log_table))
        self.log_table.setItemDelegateForColumn(self.time_col, TimeDelegate(self.log_table))
        self.log_table.itemSelectionChanged.connect(self.on_log_row_selected)
        self.log_table.itemChanged.connect(self.on_log_item_changed)
        self.log_table.setMinimumHeight(60)
        self.log_body = QWidget()
        lbl = QVBoxLayout(self.log_body)
        lbl.setContentsMargins(0, 0, 0, 0)
        lbl.setSpacing(10)
        lbl.addWidget(self.log_table, 1)
        self.update_column_visibility()

        ops = QHBoxLayout()
        ops.setSpacing(10)
        add_ev = self._btn("Add Event", self.add_event_dialog)
        undo_btn = self._btn("Undo", self.undo_last)
        del_btn = self._btn("Delete Selected", self.delete_selected_row)
        del_btn.setProperty("variant", "danger")
        load_log_btn = self._btn("Load Log", self.load_log_tsv)
        export_btn = self._btn("Export Log", self.export_to_tsv, primary=True)
        ops.addWidget(with_info(add_ev, INFO["add_event"], self))
        ops.addWidget(with_info(undo_btn, INFO["undo"], self))
        ops.addWidget(del_btn)
        ops.addStretch(1)
        recover_btn = self._btn("Recover", self.recover_log)
        recover_btn.setToolTip("Open an autosaved log (saved automatically after every change)")
        ops.addWidget(recover_btn)
        ops.addWidget(with_info(load_log_btn, INFO["load_log"], self))
        ops.addWidget(with_info(export_btn, INFO["export_log"], self))
        lbl.addLayout(ops)
        llay.addWidget(self.log_body, 1)
        self.log_card = lcard
        self.center_split.addWidget(lcard)
        self.center_split.setStretchFactor(0, 3)
        self.center_split.setStretchFactor(1, 2)
        self.center_split.setCollapsible(0, True)
        self.center_split.setCollapsible(1, True)
        saved = self.settings.value("split")
        if saved:
            try:
                self.center_split.restoreState(saved)
            except Exception:
                pass
        self.center_split.splitterMoved.connect(self.save_split)

        self.body_split.setSizes([340, 1400])
        self.body_split.splitterMoved.connect(lambda *_: self.update_etho_columns())
        self.update_etho_columns()
        for sp in (self.body_split, self.center_split):
            sp.set_ink(THEMES[self.theme]["sub"])
        self.apply_gap_visibility()
        central.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        central.setFocus()
        self.refresh_validation()

    def _btn(self, text, slot, primary=False):
        b = QPushButton(text)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:
            b.setProperty("variant", "primary")
        b.clicked.connect(slot)
        return b

    def update_column_visibility(self, *_):
        show_all = self.show_all_cols.isChecked()
        for i, h in enumerate(LOG_HEADERS):
            self.log_table.setColumnHidden(i, not show_all and h not in VISIBLE_LOG_COLUMNS)
        if hasattr(self, "col_widths"):
            self.apply_col_widths()

    # ---------- ethogram ----------
    def default_ethogram(self):
        """A small generic example. Load your own ethogram with "Load Ethogram" (a CSV with Key, Behavior, Type, Description)."""
        return [
            ("A", "walking", "State", "Example: moving across the cage."),
            ("B", "resting", "State", "Example: stationary with minimal movement."),
            ("C", "grooming", "State", "Example: licking or scratching the body or face."),
            ("D", "rearing", "State", "Example: standing on the hind legs."),
        ]

    def load_default_ethogram(self):
        self.set_ethogram(self.default_ethogram(), DEFAULT_ETHOGRAM_VERSION, False)

    def etho_tag(self):
        """Text saved in each log row's Description column, e.g. 'ethogram v0.1.1'."""
        if not self.etho_version:
            return "ethogram version unknown"
        return f"ethogram v{self.etho_version}" + (" (edited)" if self.etho_edited else "")

    def set_ethogram(self, items, version=None, edited=None):
        if version is not None:
            self.etho_version = version
        if edited is not None:
            self.etho_edited = edited
        self.etho_ver_label.setText(
            (f"v{self.etho_version}" + (" · edited" if self.etho_edited else "")) if self.etho_version
            else "version unknown")
        self.ethogram = {k: (n, t, d) for k, n, t, d in items}
        self.ethogram_table.setRowCount(0)
        for key, name, b_type, desc in items:
            r = self.ethogram_table.rowCount()
            self.ethogram_table.insertRow(r)
            for c, text in enumerate((key, name, b_type, desc or "")):
                it = QTableWidgetItem(text)
                it.setToolTip(desc or name)
                if c == 1:
                    it.setIcon(color_dot_icon(behavior_color(name)))
                self.ethogram_table.setItem(r, c, it)
        self._running_names = None
        self.refresh_validation()

    def current_ethogram_items(self):
        return [(k, n, t, d) for k, (n, t, d) in self.ethogram.items()]

    def edit_ethogram(self):
        dlg = EditEthogramDialog(self.current_ethogram_items(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_items = dlg.get_items()
            if new_items != self.current_ethogram_items():
                self.set_ethogram(new_items, None, True)
        self.centralWidget().setFocus()

    def load_ethogram_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Ethogram", "", "CSV Files (*.csv);;Text (*.txt)")
        if path:
            try:
                with open(path, mode="r", encoding="utf-8-sig", newline="") as f:
                    items = parse_ethogram_rows(list(csv.reader(f)))
            except Exception as e:
                QMessageBox.warning(self, "Load Failed", f"Could not read file:\n{e}")
                self.centralWidget().setFocus()
                return
            problems = check_ethogram(items)
            if not items:
                QMessageBox.warning(self, "Load Failed", "No behaviors found in that file.")
            elif problems:
                QMessageBox.warning(self, "Please fix the file", "\n".join(problems))
            else:
                self.set_ethogram(items, version_from_filename(path), False)
        self.centralWidget().setFocus()

    def export_ethogram_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Ethogram",
            (f"ethogram_v{self.etho_version}{'_edited' if self.etho_edited else ''}.csv"
             if self.etho_version else "ethogram.csv"),
            "CSV Files (*.csv)")
        if path:
            if not os.path.splitext(path)[1]:
                path += ".csv"
            with open(path, mode="w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(ethogram_to_rows(self.current_ethogram_items()))
            QMessageBox.information(self, "Export Successful", f"Ethogram saved:\n{path}")
        self.centralWidget().setFocus()

    # ---------- video ----------
    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Add Video", "", "Videos (*.mp4 *.avi *.mov *.mkv);;All Files (*)")
        if not path:
            self.centralWidget().setFocus()
            return
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            QMessageBox.warning(self, "Cannot open video", "This video could not be opened.")
            self.centralWidget().setFocus()
            return

        if self.log_table.rowCount() > 0:
            ans = QMessageBox.question(
                self, "New video",
                "Clear the current event log for the new video?\n(Export it first if you want to keep it.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans == QMessageBox.StandardButton.Yes:
                self.log_table.setRowCount(0)
                self._undo, self._prev_rows = [], []

        self.timer.stop()
        self.prev_timer.stop()
        self.is_playing = False
        if self.cap:
            self.cap.release()
        if self.prev_cap:
            self.prev_cap.release()
        self.cap = cap
        self.prev_cap = cv2.VideoCapture(path)
        self.video_path = path
        self.setWindowTitle("EthoLog - " + os.path.basename(path))
        self.timeline_label.setToolTip(path)
        self.obs_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.video_duration = self.total_frames / self.fps
        self.current_frame = min(MIN_FRAME, max(self.total_frames - 1, 0))
        self._next_pos = -1

        self.gap_strip.set_video(self.total_frames, self.fps)
        self.seek_slider.setEnabled(True)
        self.seek_slider.setRange(min(MIN_FRAME, max(self.total_frames - 1, 0)), max(self.total_frames - 1, 0))
        self.play_pause_btn.setEnabled(True)
        self.play_pause_btn.setText("Play")
        self.update_video_frame()
        self.refresh_validation()
        self.offer_autosave_restore()
        self.centralWidget().setFocus()

    @staticmethod
    def frame_to_qimage(frame, max_w=None):
        if max_w and frame.shape[1] > max_w:
            scale = max_w / frame.shape[1]
            frame = cv2.resize(frame, (max_w, int(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        return QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()

    def update_video_frame(self):
        if not self.cap:
            return
        # Only seek when jumping; during normal playback read frames in order (much smoother).
        if self.current_frame != self._next_pos:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame)
        ret, frame = self.cap.read()
        if not ret:
            self._next_pos = -1
            return
        self._next_pos = self.current_frame + 1

        self.video_frame_label.set_image(self.frame_to_qimage(frame, 1280))
        current_time = self.current_frame / self.fps
        self.timeline_label.setText(
            f'<span style="color:{"#FF9F0A" if self.theme == "dark" else "#D97706"};font-weight:700;">Frame: {self.current_frame}/{self.total_frames}'
            f'&nbsp;&nbsp;|&nbsp;&nbsp;Time: {current_time:.3f} / {self.video_duration:.3f} s</span>'
            f'&nbsp;&nbsp;&nbsp;&nbsp;{html.escape(os.path.basename(self.video_path))}')
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(self.current_frame)
        self.seek_slider.blockSignals(False)
        self.highlight_current_row()
        self.update_running()
        self.gap_strip.set_current_frame(self.current_frame)
        self.schedule_preview()

    def next_frame(self):
        if self.current_frame < self.total_frames - 1:
            self.current_frame += 1
            self.update_video_frame()
        else:
            self.timer.stop()
            self.is_playing = False
            self.play_pause_btn.setText("Play")
            self.schedule_preview()

    def cycle_speed(self, *_):
        i = PLAY_SPEEDS.index(self.play_speed) if self.play_speed in PLAY_SPEEDS else 0
        self.play_speed = PLAY_SPEEDS[(i + 1) % len(PLAY_SPEEDS)]
        self.speed_btn.setText("Speed %gx" % self.play_speed)
        if self.is_playing:
            self.timer.start(int(1000 / (self.fps * self.play_speed)))
        self.centralWidget().setFocus()

    def toggle_play_pause(self):
        if not self.cap or self.total_frames == 0:
            return
        if self.is_playing:
            self.timer.stop()
            self.is_playing = False
            self.play_pause_btn.setText("Play")
            self.schedule_preview()
        else:
            self.timer.start(int(1000 / (self.fps * self.play_speed)))
            self.is_playing = True
            self.play_pause_btn.setText("Pause")
            self.prev_timer.stop()
        self.centralWidget().setFocus()

    def on_seek_pressed(self):
        self._was_playing_before_scrub = self.is_playing
        if self.is_playing:
            self.timer.stop()
            self.is_playing = False
            self.play_pause_btn.setText("Play")

    def on_seek_moved(self, value):
        self.current_frame = value
        self.update_video_frame()

    def on_seek_released(self):
        self.current_frame = self.seek_slider.value()
        self.update_video_frame()
        if self._was_playing_before_scrub:
            self.timer.start(int(1000 / (self.fps * self.play_speed)))
            self.is_playing = True
            self.play_pause_btn.setText("Pause")
        self.centralWidget().setFocus()

    # ---------- look-ahead preview ----------
    def preview_visible(self):
        return self.preview_on.isChecked() and not self.focus_mode

    def on_preview_toggled(self, checked):
        self.settings.setValue("lookahead", "1" if checked else "0")
        self.preview_panel.setVisible(checked and not self.focus_mode)
        self.schedule_preview()
        self.centralWidget().setFocus()

    def schedule_preview(self, *_):
        """Restart the preview shortly after the video stops moving."""
        self.prev_timer.stop()
        if not self.cap or self.is_playing or not self.preview_visible():
            return
        self.prev_debounce.start(200)

    def restart_preview(self):
        if not self.prev_cap or self.is_playing or not self.preview_visible():
            return
        secs = self.preview_len.currentData() or 3
        self.prev_start = self.current_frame
        self.prev_end = min(self.total_frames - 1, self.current_frame + int(secs * self.fps))
        self.prev_idx = self.prev_start
        self.prev_next_pos = -1
        self.preview_caption.setText(
            f"Next {secs} s from {self.prev_start / self.fps:.2f} s (frame {self.prev_start})")
        self.prev_timer.start(int(1000 / self.fps))

    def preview_tick(self):
        if not self.prev_cap or self.is_playing:
            self.prev_timer.stop()
            return
        if self.prev_idx != self.prev_next_pos:
            self.prev_cap.set(cv2.CAP_PROP_POS_FRAMES, self.prev_idx)
        ret, frame = self.prev_cap.read()
        if not ret:
            self.prev_idx = self.prev_start
            self.prev_next_pos = -1
            return
        self.prev_next_pos = self.prev_idx + 1
        self.preview_label.set_image(self.frame_to_qimage(frame, 480))
        self.prev_idx += 1
        if self.prev_idx > self.prev_end:       # loop
            self.prev_idx = self.prev_start

    # ---------- keyboard ----------
    def keyPressEvent(self, event: QKeyEvent):
        v_key = event.key()
        if v_key == Qt.Key.Key_Space:
            self.toggle_play_pause()
            return
        elif v_key == Qt.Key.Key_Left:
            if self.cap and self.current_frame > MIN_FRAME:
                self.current_frame -= 1
                self.update_video_frame()
            return
        elif v_key == Qt.Key.Key_Right:
            if self.cap and self.current_frame < self.total_frames - 1:
                self.current_frame += 1
                self.update_video_frame()
            return

        if v_key == Qt.Key.Key_Escape:
            if self.focus_mode:
                self.toggle_focus_mode()
            else:
                self.centralWidget().setFocus()     # leave the event table, back to frame stepping
            return
        if v_key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.log_table.hasFocus():
                self.delete_selected_row()
            return
        mods = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        if mods:
            if v_key == Qt.Key.Key_Z:
                self.undo_last()
            return
        key_text = event.text().upper().strip()
        if not key_text:
            if Qt.Key.Key_0 <= v_key <= Qt.Key.Key_9:
                key_text = str(v_key - Qt.Key.Key_0)
        self.score_behavior(key_text)

    def score_behavior(self, key_text):
        """Log the behavior with this key at the current frame (START/STOP picked automatically)."""
        if key_text not in self.ethogram:
            return
        if not self.cap:
            self.status_label.setText("Add a video first, then score.")
            return
        if self.current_frame < MIN_FRAME:
            return
        b_name, b_type, b_desc = self.ethogram[key_text]
        t = self.current_frame / self.fps
        if b_type == "Point":
            self.append_log_row(b_name, "POINT", t, b_desc)
        elif self.behavior_open_at(b_name, t):
            self.append_log_row(b_name, "STOP", t, b_desc)
        else:
            self.append_log_row(b_name, "START", t, b_desc)

    def on_ethogram_clicked(self, row, col):
        item = self.ethogram_table.item(row, 0)
        if item:
            self.score_behavior(item.text())
        self.centralWidget().setFocus()

    # ---------- big video / running behaviors / undo ----------
    def save_split(self, *_):
        self.settings.setValue("split", self.center_split.saveState())

    def on_big_toggled(self, on):
        sizes = self.center_split.sizes()
        total = sum(sizes)
        if on:
            self._saved_split = sizes
            top = int(total * 0.8)
            self.center_split.setSizes([top, total - top])
        elif self._saved_split:
            self.center_split.setSizes(self._saved_split)
        self.save_split()
        self.centralWidget().setFocus()

    def toggle_focus_mode(self):
        self.focus_mode = not self.focus_mode
        if self.focus_mode:
            self.side_card.hide()
            self.body_split.handle(1).hide()
            self.log_card.hide()
            self.preview_panel.hide()
        else:
            self.side_card.show()
            self.body_split.handle(1).show()
            self.log_card.show()
            self.preview_panel.setVisible(self.preview_on.isChecked())
        self._running_names = None
        self.update_running()
        self.schedule_preview()
        self.centralWidget().setFocus()

    def update_running(self):
        if not hasattr(self, "status_label"):
            return
        t = self.current_frame / self.fps if self.cap else 0.0
        running = set()
        if self.cap:
            for r in range(self.ethogram_table.rowCount()):
                name_item = self.ethogram_table.item(r, 1)
                type_item = self.ethogram_table.item(r, 2)
                if name_item and type_item and type_item.text() == "State" \
                        and self.behavior_open_at(name_item.text(), t):
                    running.add(name_item.text())
        if running == self._running_names:
            return
        self._running_names = running
        tint = QColor(THEMES[self.theme]["run"])
        tint.setAlpha(80)
        for r in range(self.ethogram_table.rowCount()):
            name_item = self.ethogram_table.item(r, 1)
            on = bool(name_item) and name_item.text() in running
            for c in range(self.ethogram_table.columnCount()):
                it = self.ethogram_table.item(r, c)
                if it:
                    it.setBackground(QBrush(tint) if on else QBrush())
        text = "Running: " + " + ".join(sorted(running)) if running else "Nothing running"
        if self.last_event_text:
            text += "     |     " + self.last_event_text
        if self.focus_mode:
            text += "     |     Esc or double-click to exit big video"
        self.status_label.setText(text)

    def record_change(self):
        rows = self.get_log_rows()
        if rows != self._prev_rows:
            self._undo.append(self._prev_rows)
            del self._undo[:-100]
            self._prev_rows = rows
            self.autosave(rows)

    # ---------- autosave / recovery ----------
    def autosave_path(self):
        name = os.path.splitext(os.path.basename(self.video_path))[0] if self.video_path else "no_video"
        return os.path.join(AUTOSAVE_DIR, name + ".autosave.tsv")

    def autosave(self, rows):
        """After every change, save the log next to a backup of the previous save (BORIS column order)."""
        if not rows:
            return                      # never overwrite a good autosave with an empty log
        try:
            os.makedirs(AUTOSAVE_DIR, exist_ok=True)
            path = self.autosave_path()
            if os.path.exists(path) and time.time() - getattr(self, "_last_bak", 0) > 60:
                os.replace(path, path + ".bak")           # keep an older copy, refreshed once a minute
                self._last_bak = time.time()
            tmp = path + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(LOG_HEADERS)
                w.writerows(rows)
            os.replace(tmp, path)
        except Exception:
            import traceback
            traceback.print_exc()

    @staticmethod
    def read_log_rows(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter="\t")
            header = next(reader, None) or []
            out = []
            for rd in reader:
                new = [""] * len(LOG_HEADERS)
                for i, v in enumerate(rd):
                    if i < len(header) and header[i].strip() in LOG_HEADERS:
                        new[LOG_HEADERS.index(header[i].strip())] = v
                out.append(new)
        return out

    def offer_autosave_restore(self):
        path = self.autosave_path()
        if self.log_table.rowCount() > 0 or not os.path.exists(path):
            return
        try:
            rows = self.read_log_rows(path)
        except Exception:
            return
        if not rows:
            return
        when = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%b %d, %H:%M")
        ans = QMessageBox.question(
            self, "Recover your work?",
            f"An autosaved log for this video was found ({len(rows)} rows, saved {when}).\n\nRestore it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.set_log_rows(sort_rows_by_time(rows, self.time_col))
            self.refresh_validation()

    def recover_log(self, *_):
        """Open the autosave folder to pick an autosaved log (or its .bak older copy)."""
        os.makedirs(AUTOSAVE_DIR, exist_ok=True)
        path, _f = QFileDialog.getOpenFileName(
            self, "Recover autosaved log", AUTOSAVE_DIR, "Autosaved logs (*.tsv *.bak);;All Files (*)")
        if not path:
            self.centralWidget().setFocus()
            return
        try:
            rows = self.read_log_rows(path)
        except Exception as e:
            QMessageBox.warning(self, "Recover failed", f"Could not read file:\n{e}")
            return
        if self.log_table.rowCount() > 0:
            ans = QMessageBox.question(self, "Recover", "Replace the current event log with this autosave?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.set_log_rows(sort_rows_by_time(rows, self.time_col))
        self.refresh_validation()
        self.centralWidget().setFocus()

    def undo_last(self):
        if not self._undo:
            self.status_label.setText("Nothing to undo")
            return
        rows = self._undo.pop()
        self.set_log_rows(rows)
        self._prev_rows = rows
        self.last_event_text = "Undone"
        self.refresh_validation()
        self.centralWidget().setFocus()

    # ---------- column widths ----------
    def load_col_widths(self):
        widths = {h: 130 for h in LOG_HEADERS}
        widths.update({"Behavior": 240, "Behavior type": 120, "Time": 100, "Image index": 110,
                       "Observation id": 260, "Source": 260, "Media file name": 260})
        try:
            saved = json.loads(str(self.settings.value("col_widths", "{}")))
            widths.update({k: int(v) for k, v in saved.items() if k in widths})
        except Exception:
            pass
        return widths

    def apply_col_widths(self):
        self._applying_widths = True
        for i, h in enumerate(LOG_HEADERS):
            self.log_table.setColumnWidth(i, self.col_widths[h])
        self._applying_widths = False

    def on_col_resized(self, idx, old, new):
        if self._applying_widths or idx >= len(LOG_HEADERS) or new < 10:
            return
        self.col_widths[LOG_HEADERS[idx]] = new
        self.settings.setValue("col_widths", json.dumps(self.col_widths))

    # ---------- log table ----------
    def get_log_rows(self):
        rows = []
        for r in range(self.log_table.rowCount()):
            rows.append([(self.log_table.item(r, c).text() if self.log_table.item(r, c) else "")
                         for c in range(self.log_table.columnCount())])
        return rows

    def set_log_rows(self, rows):
        self.log_table.blockSignals(True)
        self.log_table.setRowCount(0)
        for row in rows:
            r = self.log_table.rowCount()
            self.log_table.insertRow(r)
            for c, val in enumerate(row):
                self.log_table.setItem(r, c, QTableWidgetItem(val))
        self.log_table.blockSignals(False)
        self._cache_dirty = True
        if hasattr(self, "col_widths"):
            self.apply_col_widths()

    def resort_log(self):
        self.set_log_rows(sort_rows_by_time(self.get_log_rows(), self.time_col))

    def ensure_event_cache(self):
        """Per-behavior list of (time, START/STOP), rebuilt only after the log changes."""
        if not self._cache_dirty:
            return
        ev = {}
        for r in range(self.log_table.rowCount()):
            def cell(c):
                it = self.log_table.item(r, c)
                return it.text().strip() if it else ""
            t = to_float(cell(self.time_col))
            flag = cell(self.type_col).upper()
            if t is not None and flag in ("START", "STOP"):
                ev.setdefault(cell(self.beh_col), []).append((t, flag))
        for lst in ev.values():
            lst.sort(key=lambda x: x[0])
        self._events = ev
        self._cache_dirty = False

    def behavior_open_at(self, behavior, t):
        self.ensure_event_cache()
        state = False
        for rt, flag in self._events.get(behavior, []):
            if rt > t:
                break
            state = (flag == "START")
        return state

    def append_log_row(self, behavior_name, type_flag, event_time, description=""):
        # Same values BORIS writes. Description holds the ethogram version; Subject, Category and Comment stay empty.
        obs_id = os.path.splitext(os.path.basename(self.video_path))[0] if self.video_path else "video"
        fields = {
            "Observation id": obs_id,
            "Observation date": self.obs_date,
            "Description": self.etho_tag(),
            "Observation duration": f"{self.video_duration:.3f}",
            "Observation type": "Media file(s)",
            "Source": f"player #1:{self.video_path}",
            "Time offset (s)": "0.0",
            "Media duration (s)": f"{self.video_duration:.3f}",
            "FPS": f"{self.fps:.3f}",
            "Behavior": behavior_name,
            "Behavior type": type_flag,
            "Time": f"{event_time:.3f}",
            "Media file name": self.video_path,
            "Image index": str(int(round(event_time * self.fps))),
            "Image file path": "NA",
        }
        rows = self.get_log_rows()
        new = [fields.get(h, "") for h in LOG_HEADERS]
        # new events go after existing rows with the same or earlier time
        idx = len(rows)
        for i, row in enumerate(rows):
            t = to_float(row[self.time_col])
            if t is not None and t > event_time:
                idx = i
                break
        rows.insert(idx, new)
        self.set_log_rows(rows)
        self.last_event_text = f"Last: {type_flag} {behavior_name} at {event_time:.3f} s"
        self.refresh_validation()
        self.log_table.scrollToItem(self.log_table.item(idx, self.beh_col))

    def add_event_dialog(self):
        if not self.cap:
            QMessageBox.information(self, "Add a video first", "Add a video so the tool knows the frame rate.")
            self.centralWidget().setFocus()
            return
        dlg = AddEventDialog(self.ethogram, self.current_frame / self.fps, self.video_duration, self.fps,
                             self.behavior_open_at, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, kind, t = dlg.get_data()
            desc = next((v[2] for v in self.ethogram.values() if v[0] == name), "")
            self.append_log_row(name, kind, t, desc)
        self.centralWidget().setFocus()

    def on_log_item_changed(self, item):
        if item.column() == self.time_col:
            t = to_float(item.text())
            if t is not None:
                self.log_table.blockSignals(True)
                item.setText(f"{t:.3f}")
                img_item = self.log_table.item(item.row(), LOG_HEADERS.index("Image index"))
                if img_item:
                    img_item.setText(str(int(round(t * self.fps))))
                self.log_table.blockSignals(False)
                QTimer.singleShot(0, self.resort_then_refresh)
                return
        self.refresh_validation()
        QTimer.singleShot(0, lambda: self.centralWidget().setFocus())

    def resort_then_refresh(self):
        self.resort_log()
        self.refresh_validation()

    def delete_selected_row(self):
        selected = self.log_table.currentRow()
        if selected >= 0:
            self.log_table.removeRow(selected)
            self.last_event_text = "Row deleted"
            self.refresh_validation()
        self.centralWidget().setFocus()

    def highlight_current_row(self):
        current_time = self.current_frame / self.fps
        closest_row, closest_diff = -1, None
        for r in range(self.log_table.rowCount()):
            item = self.log_table.item(r, self.time_col)
            t = to_float(item.text()) if item else None
            if t is None:
                continue
            diff = abs(t - current_time)
            if closest_diff is None or diff < closest_diff:
                closest_diff, closest_row = diff, r
        if closest_row >= 0:
            self.log_table.blockSignals(True)
            self.log_table.selectRow(closest_row)
            self.log_table.blockSignals(False)
            item = self.log_table.item(closest_row, self.beh_col)
            if item:
                self.log_table.scrollToItem(item)
            self.update_selection_color()

    def on_log_row_selected(self):
        if not self.cap or self.total_frames == 0:
            return
        row = self.log_table.currentRow()
        if row < 0:
            return
        item = self.log_table.item(row, self.time_col)
        t = to_float(item.text()) if item else None
        if t is None:
            return
        frame = max(MIN_FRAME, min(int(round(t * self.fps)), self.total_frames - 1))
        if frame == self.current_frame:
            return
        if self.is_playing:
            self.timer.stop()
            self.is_playing = False
            self.play_pause_btn.setText("Play")
        self.current_frame = frame
        self.update_video_frame()

    def update_etho_columns(self):
        """Narrow panel: Key + Behavior. Medium: + Type. Wide: + Description."""
        w = self.side_card.width()
        show_type, show_desc = w >= 270, w >= 560
        t = self.ethogram_table
        t.setColumnHidden(2, not show_type)
        t.setColumnHidden(3, not show_desc)
        hh = t.horizontalHeader()
        if show_desc:
            hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            t.setColumnWidth(1, 200)
        else:
            hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    # ---------- gap check ----------
    def seek_to_frame(self, frame):
        """Jump from a click or drag on the gap strip."""
        if not self.cap or self.total_frames == 0:
            return
        if self.is_playing:
            self.timer.stop()
            self.is_playing = False
            self.play_pause_btn.setText("Play")
        frame = max(MIN_FRAME, min(int(frame), self.total_frames - 1))
        if frame != self.current_frame:
            self.current_frame = frame
            self.update_video_frame()
        self.centralWidget().setFocus()

    def on_gap_toggled(self, on):
        self.settings.setValue("gapcheck", "1" if on else "0")
        self.gap_rows = set()
        self.apply_gap_visibility()
        self.refresh_row_colors()
        self.centralWidget().setFocus()

    def apply_gap_visibility(self):
        on = self.gap_check_cb.isChecked()
        for w in [self.gap_label, self.gap_prev_btn, self.gap_next_btn, self.gap_strip] + self.zoom_btns:
            w.setVisible(on)
        if on:
            self.refresh_gaps()

    def refresh_gaps(self, rows=None):
        if not hasattr(self, "gap_strip") or not self.gap_check_cb.isChecked():
            return
        rows = rows if rows is not None else self.get_log_rows()
        events, self._row_frames = [], []
        for row in rows:
            fr = row_frame(row, self.fps)
            self._row_frames.append(fr)
            beh, typ = row[self.beh_col].strip(), row[self.type_col].strip()
            if beh and typ and fr is not None:
                events.append((beh, typ, fr))
        total = self.total_frames
        intervals = build_intervals(events, total or None)
        gaps = find_unscored(intervals, total, MIN_FRAME) if total else []
        self.gap_strip.set_data(assign_layers(intervals, total) if total else [], gaps)
        if total:
            n_fr = sum(b - a + 1 for a, b in gaps)
            self.gap_label.setText("%d unlabeled stretch%s, %d frames (%.1f%%)" % (
                len(gaps), "" if len(gaps) == 1 else "es", n_fr, 100.0 * n_fr / total))
        else:
            self.gap_label.setText("Add a video to check for unlabeled frames")

    def row_brush(self, r, bad_rows, p):
        if r in bad_rows:
            return QBrush()             # problem rows: red text only, no fill
        if r in self.gap_rows:
            return QBrush(QColor(p["gap_row"]))
        return QBrush()

    def refresh_row_colors(self):
        p = THEMES[self.theme]
        bad_rows = {i for i, _ in self.current_issues()}
        self.log_table.blockSignals(True)
        for r in range(self.log_table.rowCount()):
            for c in range(self.log_table.columnCount()):
                it = self.log_table.item(r, c)
                if it:
                    it.setBackground(self.row_brush(r, bad_rows, p))
                    it.setForeground(QBrush(QColor(p["danger"])) if r in bad_rows else QBrush())
        self.log_table.blockSignals(False)

    def on_gap_clicked(self, a, b):
        """Jump to the first unlabeled frame of the stretch a..b and show it in the Event Log:
        select the last event before it (and mark the one after it in yellow)."""
        before = after = None
        for i, fr in enumerate(self._row_frames):
            if fr is None:
                continue
            if fr < a:
                before = i
            elif fr > b and after is None:
                after = i
        self.seek_to_frame(a)
        self.gap_rows = {r for r in (before, after) if r is not None}
        self.refresh_row_colors()
        if self.center_split.panel_closed():
            self.center_split.toggle_panel()
        show = before if before is not None else after
        if show is not None:
            self.log_table.blockSignals(True)
            self.log_table.selectRow(show)
            self.log_table.blockSignals(False)
        for r in (after, before):                        # bring both neighbours into view
            if r is not None:
                self.log_table.scrollToItem(self.log_table.item(r, self.beh_col))
        self.gap_label.setText("Unlabeled: frames %d to %d (%d frames)" % (a, b, b - a + 1))
        self.centralWidget().setFocus()

    def jump_to_gap(self, direction):
        gaps = self.gap_strip.gaps
        if not gaps or not self.cap:
            return
        cur = self.current_frame
        if direction > 0:
            g = next((x for x in gaps if x[0] > cur), gaps[0])
        else:
            g = next((x for x in reversed(gaps) if x[0] < cur), gaps[-1])
        self.on_gap_clicked(g[0], g[1])

    # ---------- column order (screen only; the export always uses BORIS order) ----------
    def on_col_moved(self, logical, old_visual, new_visual):
        if self._applying_order:
            return
        hh = self.log_table.horizontalHeader()
        order = [LOG_HEADERS[hh.logicalIndex(v)] for v in range(hh.count())]
        self.settings.setValue("col_order", json.dumps(order))

    def _apply_col_order(self, order):
        hh = self.log_table.horizontalHeader()
        self._applying_order = True
        for target, name in enumerate(order):
            cur = hh.visualIndex(LOG_HEADERS.index(name))
            if cur != target:
                hh.moveSection(cur, target)
        self._applying_order = False

    def restore_col_order(self):
        try:
            saved = json.loads(str(self.settings.value("col_order", "[]")))
            if isinstance(saved, list) and sorted(saved) == sorted(LOG_HEADERS):
                self._apply_col_order(saved)
        except Exception:
            pass

    def reset_col_order(self):
        self._apply_col_order(LOG_HEADERS)
        self.settings.setValue("col_order", json.dumps(LOG_HEADERS))
        self.centralWidget().setFocus()

    # ---------- validation ----------
    def current_issues(self):
        known = {v[0] for v in self.ethogram.values()}
        return find_unpaired(self.get_log_rows(), self.beh_col, self.type_col, self.time_col, known)

    def update_selection_color(self):
        """A selected problem row is shown in red instead of the normal blue selection, so it never hides."""
        row = self.log_table.currentRow()
        bad = row in getattr(self, "_bad_rows", set())
        if bad != getattr(self, "_sel_bad", None):
            self._sel_bad = bad
            col = THEMES[self.theme]["danger"]
            self.log_table.setStyleSheet(
                "QTableWidget { selection-color: %s; }" % col if bad else "")

    def jump_to_issue(self):
        """Click on the unpaired-events notice: go to the next problem row, select it and move the video there."""
        issues = self.current_issues()
        if not issues:
            return
        self._issue_i = (self._issue_i + 1) % len(issues)
        row, msg = issues[self._issue_i]
        if self.center_split.panel_closed():
            self.center_split.toggle_panel()
        self.log_table.selectRow(row)                  # also moves the video to this event's time
        self.log_table.scrollToItem(self.log_table.item(row, self.beh_col))
        self.update_selection_color()
        self.status_label.setText("Problem %d of %d: %s" % (self._issue_i + 1, len(issues), msg))
        self.centralWidget().setFocus()

    def refresh_validation(self):
        if not hasattr(self, "badge"):
            return
        issues = self.current_issues()
        bad_rows = {i for i, _ in issues}
        self.gap_rows = set()      # the log changed, so the yellow gap rows are out of date
        self._bad_rows = bad_rows
        p = THEMES[self.theme]
        self.log_table.blockSignals(True)
        for r in range(self.log_table.rowCount()):
            for c in range(self.log_table.columnCount()):
                it = self.log_table.item(r, c)
                if it:
                    it.setBackground(self.row_brush(r, bad_rows, p))
                    it.setForeground(QBrush(QColor(p["danger"])) if r in bad_rows else QBrush())
        self.log_table.blockSignals(False)
        n = self.log_table.rowCount()
        if issues:
            self.badge.setObjectName("badgeWarn")
            self.badge.setText(f"⚠ {len(issues)} unpaired / problem event(s)")
        else:
            self.badge.setObjectName("badgeOk")
            self.badge.setText(f"✓ {n} event(s), all paired" if n else "")
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        self._cache_dirty = True
        self._running_names = None
        self.record_change()
        self.update_running()
        self.refresh_gaps()
        self.update_selection_color()

    def confirm_export_despite_issues(self):
        """Return True to go ahead and save."""
        issues = self.current_issues()
        if not issues:
            return True
        shown = "\n".join(f"• {m}" for _, m in issues[:12])
        more = f"\n...and {len(issues) - 12} more" if len(issues) > 12 else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unpaired events")
        box.setText(f"{len(issues)} problem(s) found in the event log:")
        box.setInformativeText(shown + more)
        fix = box.addButton("Review problems", QMessageBox.ButtonRole.RejectRole)
        box.addButton("Save anyway", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(fix)
        box.exec()
        if box.clickedButton() is fix:
            first = issues[0][0]
            self.log_table.selectRow(first)
            self.log_table.scrollToItem(self.log_table.item(first, self.beh_col))
            return False
        return True

    # ---------- load / export log ----------
    def load_log_tsv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Event Log", "", "Event logs (*.tsv *.csv);;All Files (*)")
        if not path:
            self.centralWidget().setFocus()
            return
        delimiter = "\t" if path.lower().endswith(".tsv") else ","
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f, delimiter=delimiter)
                header = next(reader, None)
                rows = list(reader)
        except Exception as e:
            QMessageBox.warning(self, "Load Failed", f"Could not read file:\n{e}")
            self.centralWidget().setFocus()
            return

        header_aliases = {"SubjectBehavior": "Behavior"}
        ignored_headers = set()
        col_map, unmatched = [], []
        if header:
            for h in header:
                h = h.strip()
                if h in ignored_headers:
                    col_map.append(None)
                elif h in header_aliases:
                    col_map.append(LOG_HEADERS.index(header_aliases[h]))
                elif h in LOG_HEADERS:
                    col_map.append(LOG_HEADERS.index(h))
                else:
                    col_map.append(None)
                    unmatched.append(h)
        else:
            col_map = list(range(len(LOG_HEADERS)))

        existing = self.get_log_rows()
        replace = True
        if existing:
            box = QMessageBox(self)
            box.setWindowTitle("Load Log")
            box.setText("The event log already has rows.")
            rep = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
            app = box.addButton("Add to existing", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is rep:
                replace = True
            elif box.clickedButton() is app:
                replace = False
            else:
                self.centralWidget().setFocus()
                return

        new_rows = []
        for row_data in rows:
            new = [""] * len(LOG_HEADERS)
            for src_idx, val in enumerate(row_data):
                if src_idx < len(col_map) and col_map[src_idx] is not None:
                    new[col_map[src_idx]] = val
            new_rows.append(new)
        all_rows = new_rows if replace else existing + new_rows
        self.set_log_rows(sort_rows_by_time(all_rows, self.time_col))
        self.refresh_validation()

        msg = f"Loaded {len(rows)} record(s)."
        if unmatched:
            msg += "\n\nColumns not recognized and skipped:\n" + ", ".join(unmatched)
        QMessageBox.information(self, "Load Successful", msg)
        self.centralWidget().setFocus()

    def export_to_tsv(self):
        if not self.confirm_export_despite_issues():
            self.centralWidget().setFocus()
            return
        default = (os.path.splitext(os.path.basename(self.video_path))[0] + ".tsv") if self.video_path else "log.tsv"
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export Log", default, "TSV Files (*.tsv);;CSV Files (*.csv)")
        if path:
            if not os.path.splitext(path)[1]:
                path += ".csv" if "CSV" in chosen else ".tsv"
            delimiter = "\t" if path.lower().endswith(".tsv") else ","
            with open(path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=delimiter)
                # LOG_HEADERS is the BORIS column order, and get_log_rows() reads cells by their real (logical)
                # column, so dragging columns around on screen never changes what is written.
                writer.writerow(LOG_HEADERS)
                writer.writerows(self.get_log_rows())
            QMessageBox.information(self, "Export Successful", f"Log saved:\n{path}")
        self.centralWidget().setFocus()

    def closeEvent(self, event):
        n = self.log_table.rowCount()
        extra = (f"\n\nThe event log has {n} row(s). Export it first if you haven't. "
                 f"An autosave is kept in {AUTOSAVE_DIR}.") if n else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Close EthoLog?")
        box.setText("Do you really want to close EthoLog?" + extra)
        stay = box.addButton("Keep working", QMessageBox.ButtonRole.RejectRole)
        box.addButton("Close", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(stay)
        box.exec()
        if box.clickedButton() is stay:
            event.ignore()
            return
        for c in (self.cap, self.prev_cap):
            if c:
                c.release()
        self.save_split()
        super().closeEvent(event)


def install_debug_log():
    """Write every error and crash to ~/EthoLog_debug.log so a freeze can be diagnosed afterwards."""
    import faulthandler
    import traceback
    path = os.path.join(os.path.expanduser("~"), "EthoLog_debug.log")
    try:
        f = open(path, "a", buffering=1, encoding="utf-8")
        f.write("\n===== EthoLog started %s =====\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        faulthandler.enable(f, all_threads=True)
    except OSError:
        return

    def hook(etype, value, tb):
        f.write("\n--- error %s ---\n" % datetime.now().strftime("%H:%M:%S"))
        f.write("".join(traceback.format_exception(etype, value, tb)))
        sys.__excepthook__(etype, value, tb)
    sys.excepthook = hook


if __name__ == "__main__":
    install_debug_log()
    app = QApplication(sys.argv)
    fam = QFont()
    fam.setFamilies([".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue", "Segoe UI", "Arial"])
    fam.setPointSize(15)
    app.setFont(fam)
    window = BehaviorScorer()
    window.show()
    sys.exit(app.exec())
