from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QVBoxLayout, QHBoxLayout,
    QKeySequenceEdit, QPushButton, QMessageBox,
    QScrollArea, QWidget, QLabel, QLineEdit, QFrame,
    QSizePolicy,
)
from PyQt6.QtGui import QKeySequence
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

# ─── Action groups ────────────────────────────────────────────────────────────
# Each entry: (action_key, label, hardcoded_hint_or_empty)
# hardcoded_hint — non-empty means the key shown is always-active and cannot be
# reassigned by the user.  This is the SINGLE source of truth: HARDCODED_KEYS
# is derived from this table below, so there is no duplication to keep in sync.
ACTION_GROUPS = [
    ("Playback", [
        ("play_pause",     "Play / Pause",              "Space"),
        ("stop",           "Stop",                      ""),
        # à = Key_0 on AZERTY (no modifier); shown as à so AZERTY users recognise it
        ("restart_video",  "Restart from Start",        "à / 0"),
        ("seek_forward",   "Seek Forward",              ""),
        ("seek_backward",  "Seek Backward",             ""),
        # ù = Key_Ugrave on AZERTY
        ("frame_forward",  "Next Frame",                ""),
        ("frame_backward", "Previous Frame",            "ù"),
        ("speed_up",       "Speed Up",                  ""),
        ("speed_down",     "Speed Down",                ""),
        ("speed_reset",    "Reset Speed",               ""),
        ("ab_loop_set_a",  "A-B Loop: Set A",          ""),
        ("ab_loop_set_b",  "A-B Loop: Set B",          ""),
        ("ab_loop_clear",  "A-B Loop: Clear",          ""),
        ("cycle_repeat",   "Cycle Repeat Mode",         ""),
    ]),
    ("View & Zoom", [
        ("fullscreen",       "Fullscreen",              "F"),
        ("zoom_in",          "Zoom In",                 ""),
        ("zoom_out",         "Zoom Out",                ""),
        ("reset_zoom",       "Reset Zoom",              ""),
        ("toggle_image_fit", "Toggle Image Fit",        "L"),
        ("toggle_mirror",    "Mirror / Flip Video",     "End"),
        ("cycle_screen_transform", "Cycle Screen Transform", ""),
        ("toggle_vr",        "VR Mode",                 "V  x2"),
        ("toggle_vr_half",   "VR Half (SBS → 2D)",      "^"),
    ]),
    ("Audio", [
        ("volume_up",   "Volume Up",                    ""),
        ("volume_down", "Volume Down",                  ""),
        ("mute",        "Mute / Unmute",                "M"),
    ]),
    ("Subtitles", [
        ("toggle_subtitles",   "Toggle Subtitles",      "S"),
        ("load_subtitle",      "Load Subtitle File",    "Ctrl+L"),
    ]),
    ("Manga / CBZ", [
        ("next_chapter",     "Next Chapter",            "N"),
        ("previous_chapter", "Previous Chapter",        "P"),
        ("next_cbz_page",    "Next Page",               ""),
        ("toggle_compact",   "Page / Continuous Mode",  "C"),
        ("toggle_rtl",       "Toggle RTL Mode",         "H"),
    ]),
    ("Privacy & PDF", [
        ("privacy_lock",       "Privacy Lock",          "PageDown"),
        ("toggle_privacy_lock","Toggle Privacy Lock",   ""),
        ("pdf_search_next",    "PDF Search — Next hit", "Ctrl+G"),
        ("pdf_search_prev",    "PDF Search — Prev hit", "Ctrl+Shift+G"),
        ("pdf_mark_page",      "PDF: Mark Page",        ""),
        ("pdf_switch_app",     "PDF: Return to App",    ""),
        ("close_fullscreen",   "Exit Fullscreen / PDF", "Escape"),
    ]),
    ("Interface", [
        ("open_file",           "Open File",            ""),
        ("toggle_search",       "Search Playlist",      ""),
        ("toggle_mark",         "Mark / Unmark Seen",   ""),
        ("screen_off",          "Screen Off",           ""),
        ("toggle_nocturnal",    "Nocturnal Mode",       ""),
        ("nocturnal_up",        "Nocturnal Brighter",   ""),
        ("nocturnal_down",      "Nocturnal Darker",     ""),
        ("boss_key",            "Boss Key",             "PageUp"),
        ("play_previous_track", "Play Previous Track",  ""),
        ("next_video",          "Next in Playlist",     ""),
        ("previous_video",      "Previous in Playlist", ""),
    ]),
]

# ─── Hardcoded keys (cannot be reassigned) ───────────────────────────────────
# Built automatically from ACTION_GROUPS hint strings — no manual duplication.
# To mark a key hardcoded: set a non-empty hint in its ACTION_GROUPS entry.
# To add a purely hardcoded key with no rebindable row: it's already shown via
# the 🔒 badge on its row, so no separate section is needed.
HARDCODED_KEYS: dict = {}
for _grp_name, _grp_entries in ACTION_GROUPS:
    for _entry in _grp_entries:
        _ak, _lbl = _entry[0], _entry[1]
        _hc = _entry[2] if len(_entry) > 2 else ""
        if _hc:
            # Strip display-only suffixes like " (CBZ)" or " x2"
            _key = _hc.split()[0]
            HARDCODED_KEYS[_key] = _lbl

_ACTION_LABELS = {ak: lbl for _, grp in ACTION_GROUPS for ak, lbl, *_ in grp}

DEFAULTS = {
    "play_pause": "Space", "stop": "",
    "restart_video": "0",           # also triggered by à (Key_Agrave) on AZERTY
    "next_video": "N", "previous_video": "P",
    "next_chapter": "N", "previous_chapter": "P",
    "next_cbz_page": "",
    "toggle_compact": "C", "toggle_rtl": "H",
    "fullscreen": "F",
    "volume_up": "Up", "volume_down": "Down", "mute": "M",
    "seek_forward": "Right", "seek_backward": "Left",
    "frame_forward": "Period", "frame_backward": "Comma",  # also ù (Key_Ugrave) on AZERTY
    "speed_up": "]", "speed_down": "[", "speed_reset": "Backspace",
    "zoom_in": "Ctrl+Plus", "zoom_out": "Ctrl+Minus", "reset_zoom": "Ctrl+0",
    "toggle_subtitles": "", "toggle_mirror": "H",
    "cycle_screen_transform": "$",
    "toggle_vr": "R",
    "toggle_vr_half": "^",
    "toggle_nocturnal": "Ctrl+N",
    "nocturnal_up": "Shift+Up", "nocturnal_down": "Shift+Down",
    "cycle_repeat": "",
    "open_file": "Ctrl+O", "toggle_search": "Ctrl+F",
    "screen_off": "Ctrl+D", "toggle_image_fit": "I",
    "ab_loop_set_a": "A", "ab_loop_set_b": "B", "ab_loop_clear": "C",
    "boss_key": "PgUp", "toggle_mark": "W",
    "toggle_privacy_lock": "Home",
    "pdf_mark_page": "AltGr",
    "pdf_switch_app": "Shift+AltGr",
    "play_previous_track": "",
}

_CONTEXTUAL_BINDING_GROUPS = {
    QKeySequence("C").toString(): {"toggle_compact", "ab_loop_clear"},
    QKeySequence("N").toString(): {"next_video", "next_chapter"},
    QKeySequence("P").toString(): {"previous_video", "previous_chapter"},
    QKeySequence("H").toString(): {"toggle_mirror", "toggle_rtl"},
}


def _is_contextual_overlap(sequence, action_keys):
    normalized = QKeySequence(sequence).toString().strip()
    allowed = _CONTEXTUAL_BINDING_GROUPS.get(normalized)
    return bool(normalized and allowed and set(action_keys).issubset(allowed))


# ─── Key editor with recording highlight ─────────────────────────────────────
class _KeyEdit(QKeySequenceEdit):
    IDLE = """
        QKeySequenceEdit {
            background:#252525; color:#cccccc;
            border:1px solid #444; border-radius:4px;
            padding:3px 6px; font-size:12px;
            min-width:110px; max-width:150px;
        }
        QKeySequenceEdit:focus {
            border:2px solid #3a8ee6; background:#1e2a3a; color:#fff;
        }
    """
    RECORDING = """
        QKeySequenceEdit {
            background:#0d2010; color:#55ee77;
            border:2px solid #33bb55; border-radius:4px;
            padding:3px 6px; font-size:12px; font-weight:bold;
            min-width:110px; max-width:150px;
        }
    """

    def focusInEvent(self, e):
        self.setStyleSheet(self.RECORDING)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self.setStyleSheet(self.IDLE)
        super().focusOutEvent(e)


_KeyEdit.IDLE = _KeyEdit.IDLE  # keep reference after class creation


# ─── One action row ───────────────────────────────────────────────────────────
class _Row(QWidget):
    changed = pyqtSignal()

    def __init__(self, action_key, label, hc_key, current, parent=None):
        super().__init__(parent)
        self.action_key = action_key
        self.setStyleSheet("background:transparent;")
        self.setFixedHeight(32)

        h = QHBoxLayout(self)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(6)

        lbl = QLabel(label)
        lbl.setFixedWidth(158)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl.setStyleSheet("color:#bbbbbb; font-size:12px; background:transparent;")
        h.addWidget(lbl)

        self.editor = _KeyEdit()
        self.editor.setKeySequence(QKeySequence(current))
        self.editor.setStyleSheet(_KeyEdit.IDLE)
        h.addWidget(self.editor)

        clr = QPushButton("✕")
        clr.setFixedSize(22, 22)
        clr.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        clr.setCursor(Qt.CursorShape.PointingHandCursor)
        clr.setToolTip("Clear this binding")
        clr.setStyleSheet("""
            QPushButton{background:transparent;color:#555;border:1px solid #3a3a3a;
                border-radius:3px;font-size:10px;}
            QPushButton:hover{background:#3a1a1a;color:#e05555;border-color:#e05555;}
            QPushButton:pressed{background:#4a2020;}
        """)
        clr.clicked.connect(self._clear)
        h.addWidget(clr)

        if hc_key:
            badge = QLabel(f"🔒 {hc_key}")
            badge.setToolTip("This key is always active and cannot be reassigned")
            badge.setStyleSheet("""
                QLabel{background:#1c1c10;color:#887733;border:1px solid #443322;
                    border-radius:3px;padding:1px 5px;font-size:10px;font-family:monospace;}
            """)
            h.addWidget(badge)

        self.status = QLabel("")
        self.status.setMinimumWidth(10)
        self.status.setStyleSheet("font-size:10px;color:transparent;background:transparent;")
        h.addWidget(self.status, 1)

        self.editor.keySequenceChanged.connect(lambda _: self.changed.emit())

    def _clear(self):
        self.editor.setKeySequence(QKeySequence(""))
        self.changed.emit()

    def value(self):
        return self.editor.keySequence().toString().strip()

    def set_status(self, text, kind):
        color = {
            "conflict": "#e05555",
            "hardcoded": "#e0a020",
            "context": "#5da9ff",
        }.get(kind, "transparent")
        self.status.setText(text)
        self.status.setStyleSheet(f"font-size:10px;color:{color};background:transparent;")


# ─── Section divider ─────────────────────────────────────────────────────────
def _divider(title):
    w = QWidget()
    w.setStyleSheet("background:transparent;")
    w.setFixedHeight(28)
    h = QHBoxLayout(w)
    h.setContentsMargins(6, 4, 6, 0)
    h.setSpacing(8)
    lbl = QLabel(title.upper())
    lbl.setStyleSheet(
        "color:#4477aa;font-size:10px;font-weight:bold;"
        "letter-spacing:1px;background:transparent;"
    )
    h.addWidget(lbl)
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color:#2a2a2a;")
    h.addWidget(line, 1)
    return w


# ─── Dialog ───────────────────────────────────────────────────────────────────
class KeybindingsEditorDialog(QDialog):
    def __init__(self, keybindings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keybindings")
        self.setMinimumSize(700, 600)
        self.resize(760, 700)
        self.setStyleSheet("""
            QDialog{background:#1e1e1e;}
            QScrollArea{background:#1e1e1e;border:none;}
            QScrollBar:vertical{background:#252525;width:8px;border-radius:4px;}
            QScrollBar::handle:vertical{background:#444;border-radius:4px;min-height:20px;}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
        """)

        if parent is not None:
            parent._keybindings_dialog_open = True

        self._rows: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(6)

        # ── Info + search bar ─────────────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(8)
        info = QLabel(
            "  Click a key field, then press your desired key to assign it  ·  "
            "🔒 = hardcoded, cannot be changed"
        )
        info.setStyleSheet(
            "QLabel{background:#1a1f2e;color:#7788aa;border:1px solid #2a3448;"
            "border-radius:4px;padding:5px 10px;font-size:11px;}"
        )
        top.addWidget(info, 1)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Filter…")
        self._search.setFixedWidth(130)
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet("""
            QLineEdit{background:#252525;color:#ccc;border:1px solid #444;
                border-radius:4px;padding:4px 8px;font-size:11px;}
            QLineEdit:focus{border-color:#3a8ee6;}
        """)
        self._search.textChanged.connect(self._filter)
        top.addWidget(self._search)
        root.addLayout(top)

        # ── Scroll area ───────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setStyleSheet("background:#1e1e1e;")
        vbox = QVBoxLayout(inner)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        # Build grouped rows
        for group_name, entries in ACTION_GROUPS:
            vbox.addWidget(_divider(group_name))
            for entry in entries:
                ak, lbl = entry[0], entry[1]
                hc = entry[2] if len(entry) > 2 else ""
                row = _Row(ak, lbl, hc, keybindings.get(ak, ""))
                row.changed.connect(self._recheck)
                self._rows[ak] = row
                vbox.addWidget(row)

        vbox.addStretch()

        # ── Bottom buttons ────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("QFrame{color:#2a2a2a;margin-top:2px;}")
        root.addWidget(sep)

        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(8)

        revert = QPushButton("↺  Revert to Defaults")
        revert.setStyleSheet("""
            QPushButton{background:#252525;color:#888;border:1px solid #3a3a3a;
                border-radius:4px;padding:5px 14px;font-size:11px;}
            QPushButton:hover{background:#2e2e2e;color:#bbb;}
        """)
        revert.clicked.connect(self._revert)
        btn_bar.addWidget(revert)
        btn_bar.addStretch()

        bbox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bbox.setStyleSheet("""
            QPushButton{background:#2a2a2a;color:#ccc;border:1px solid #555;
                border-radius:4px;padding:5px 18px;font-size:12px;min-width:70px;}
            QPushButton:hover{background:#3a8ee6;color:#fff;border-color:#3a8ee6;}
            QPushButton:pressed{background:#2a6ec6;}
        """)
        bbox.accepted.connect(self._on_accept)
        bbox.rejected.connect(self.reject)
        btn_bar.addWidget(bbox)
        root.addLayout(btn_bar)

        QTimer.singleShot(0, self._recheck)

    # ── Conflict check ────────────────────────────────────────────────────────
    def _recheck(self):
        seq_map: dict = {}
        for ak, row in self._rows.items():
            s = row.value()
            if s:
                seq_map.setdefault(s, []).append(ak)

        for ak, row in self._rows.items():
            s = row.value()
            if not s:
                row.set_status("", "")
                continue
            normalized = QKeySequence(s).toString().strip()
            contextual_actions = _CONTEXTUAL_BINDING_GROUPS.get(normalized, set())
            if normalized in HARDCODED_KEYS and ak in contextual_actions:
                row.set_status("Mode-aware shared key", "context")
                continue
            if normalized in HARDCODED_KEYS:
                row.set_status(f"⚠ Hardcoded: {HARDCODED_KEYS[normalized]}", "hardcoded")
                continue
            others = [k for k in seq_map.get(s, []) if k != ak]
            if others:
                if _is_contextual_overlap(s, [ak] + others):
                    row.set_status("Mode-aware shared key", "context")
                    continue
                names = ", ".join(_ACTION_LABELS.get(k, k) for k in others)
                row.set_status(f"⚠ Also: {names}", "conflict")
                continue
            row.set_status("", "")

    # ── Filter ────────────────────────────────────────────────────────────────
    def _filter(self, text):
        text = text.strip().lower()
        for ak, row in self._rows.items():
            match = not text or text in _ACTION_LABELS.get(ak, ak).lower() or text in ak
            row.setVisible(match)

    # ── Accept ────────────────────────────────────────────────────────────────
    def _on_accept(self):
        seq_map: dict = {}
        for ak, row in self._rows.items():
            s = row.value()
            if s:
                seq_map.setdefault(s, []).append(ak)
        dupes = {
            s: ks for s, ks in seq_map.items()
            if len(ks) > 1 and not _is_contextual_overlap(s, ks)
        }
        if dupes:
            lines = ["  • <b>{}</b>  →  {}".format(
                s, ", ".join(_ACTION_LABELS.get(k, k) for k in ks)
            ) for s, ks in dupes.items()]
            msg = QMessageBox(self)
            msg.setWindowTitle("Duplicate Keybindings")
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setText("Some keys are assigned to more than one action.")
            msg.setInformativeText("<br>".join(lines))
            msg.exec()
            return
        self.accept()

    def closeEvent(self, event):
        if self.parent() is not None:
            self.parent()._keybindings_dialog_open = False
        super().closeEvent(event)

    def _revert(self):
        if QMessageBox.question(
            self, "Revert", "Reset all keybindings to defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        for ak, row in self._rows.items():
            row.editor.setKeySequence(QKeySequence(DEFAULTS.get(ak, "")))
        self._recheck()

    def get_keybindings(self):
        return {ak: row.value() for ak, row in self._rows.items()}
