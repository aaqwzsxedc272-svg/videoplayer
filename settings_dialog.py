from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QVBoxLayout, QHBoxLayout,
                             QFormLayout, QSlider, QLabel, QCheckBox, QSpinBox,
                             QComboBox, QLineEdit, QPushButton, QScrollArea,
                             QWidget, QFrame)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication

_BUILTIN_FOLDERS = [
    r'C:\GhostToolbox',
    r'F:\\',
    r'D:\Br',
    r'C:\TORRENT\S\ERO',
]

_BUILTIN_PHONE_FOLDERS = ['P']  # phone folder names searched recursively in the library tree


class AnalyseFolderListWidget(QWidget):
    def __init__(self, folders, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(2)
        self._layout.addLayout(self.rows_layout)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("＋  Add Folder")
        self.add_btn.clicked.connect(self.add_row)
        self.add_phone_btn = QPushButton("＋  Add Phone Folder")
        self.add_phone_btn.clicked.connect(self.add_phone_row)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.add_phone_btn)
        btn_row.addStretch()
        self._layout.addLayout(btn_row)

        # Build lookups of user-saved active states
        user_paths = {f.get("path"): f for f in folders if isinstance(f, dict) and "path" in f}
        user_phone = {f.get("phone_folder"): f for f in folders if isinstance(f, dict) and "phone_folder" in f}

        # Built-in local disk folders first (read-only, not deletable)
        for path in _BUILTIN_FOLDERS:
            saved = user_paths.get(path, {})
            self._add_builtin_row(path, saved.get("active", True))

        # Built-in phone folders (read-only, not deletable)
        for name in _BUILTIN_PHONE_FOLDERS:
            saved = user_phone.get(name, {})
            self._add_builtin_phone_row(name, saved.get("active", True))

        # User-added local folders (not in built-in lists)
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            if "phone_folder" not in folder and folder.get("path", "").strip() not in _BUILTIN_FOLDERS:
                self.add_row_ui(folder)

        # User-added phone folders (not in built-in list)
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            if "phone_folder" in folder and folder["phone_folder"] not in _BUILTIN_PHONE_FOLDERS:
                self.add_phone_row_ui(folder)

    # ── Built-in local folder row ─────────────────────────────────────────────

    def _add_builtin_row(self, path, active):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        chk = QCheckBox()
        chk.setChecked(active)
        chk.setToolTip("Enable/disable this folder for analysis")
        path_edit = QLineEdit(path)
        path_edit.setReadOnly(True)
        path_edit.setStyleSheet("color: #888; font-style: italic;")
        badge = QLabel("built-in")
        badge.setStyleSheet(
            "color: #666; font-size: 10px; border: 1px solid #555;"
            " border-radius: 3px; padding: 1px 5px;"
        )
        lay.addWidget(chk)
        lay.addWidget(path_edit, 1)
        lay.addWidget(badge)
        row.get_data = lambda p=path: {"path": p, "active": chk.isChecked(), "builtin": True}
        self.rows_layout.addWidget(row)

    # ── Built-in phone folder row ─────────────────────────────────────────────

    def _add_builtin_phone_row(self, name, active):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        chk = QCheckBox()
        chk.setChecked(active)
        chk.setToolTip("Enable/disable this phone folder for analysis")
        name_edit = QLineEdit(name)
        name_edit.setReadOnly(True)
        name_edit.setStyleSheet("color: #888; font-style: italic;")
        badge_phone = QLabel("📱 phone")
        badge_phone.setStyleSheet(
            "color: #4a9; font-size: 10px; border: 1px solid #4a9;"
            " border-radius: 3px; padding: 1px 5px;"
        )
        badge_bi = QLabel("built-in")
        badge_bi.setStyleSheet(
            "color: #666; font-size: 10px; border: 1px solid #555;"
            " border-radius: 3px; padding: 1px 5px;"
        )
        lay.addWidget(chk)
        lay.addWidget(name_edit, 1)
        lay.addWidget(badge_phone)
        lay.addWidget(badge_bi)
        row.get_data = lambda n=name: {"phone_folder": n, "active": chk.isChecked(), "builtin": True}
        self.rows_layout.addWidget(row)

    # ── User-added local folder row ───────────────────────────────────────────

    def add_row(self):
        self.add_row_ui({"path": "", "active": True})

    def add_row_ui(self, folder_data):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        chk = QCheckBox()
        chk.setChecked(folder_data.get("active", True))
        chk.setToolTip("Enable/disable this folder for analysis")
        path_edit = QLineEdit()
        path_edit.setText(folder_data.get("path", ""))
        path_edit.setPlaceholderText("Local folder path…")
        del_btn = QPushButton("Delete")
        lay.addWidget(chk)
        lay.addWidget(path_edit, 1)
        lay.addWidget(del_btn)

        def on_delete():
            self.rows_layout.removeWidget(row)
            row.deleteLater()

        del_btn.clicked.connect(on_delete)
        row.get_data = lambda: {"path": path_edit.text(), "active": chk.isChecked()}
        self.rows_layout.addWidget(row)

    # ── User-added phone folder row ───────────────────────────────────────────

    def add_phone_row(self):
        self.add_phone_row_ui({"phone_folder": "", "active": True})

    def add_phone_row_ui(self, folder_data):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        chk = QCheckBox()
        chk.setChecked(folder_data.get("active", True))
        chk.setToolTip("Enable/disable this phone folder for analysis")
        name_edit = QLineEdit()
        name_edit.setText(folder_data.get("phone_folder", ""))
        name_edit.setPlaceholderText("Folder name (e.g. M) or FTP path (e.g. /sdcard/snaptube/download)…")
        badge = QLabel("📱 phone")
        badge.setStyleSheet(
            "color: #4a9; font-size: 10px; border: 1px solid #4a9;"
            " border-radius: 3px; padding: 1px 5px;"
        )
        del_btn = QPushButton("Delete")
        lay.addWidget(chk)
        lay.addWidget(name_edit, 1)
        lay.addWidget(badge)
        lay.addWidget(del_btn)

        def on_delete():
            self.rows_layout.removeWidget(row)
            row.deleteLater()

        del_btn.clicked.connect(on_delete)
        row.get_data = lambda: {"phone_folder": name_edit.text().strip(), "active": chk.isChecked()}
        self.rows_layout.addWidget(row)

    # ── Collect all data ──────────────────────────────────────────────────────

    def get_data(self):
        data = []
        for i in range(self.rows_layout.count()):
            item = self.rows_layout.itemAt(i)
            if item and item.widget():
                d = item.widget().get_data()
                # Keep local paths that are non-empty, and phone folders that are non-empty
                if d.get("path", "").strip() or d.get("phone_folder", "").strip():
                    data.append(d)
        return data


class SettingsDialog(QDialog):
    """Dialog for application settings"""
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(500)
        
        self.settings = settings.copy()
        
        layout = QVBoxLayout(self)

        # All the settings rows live in a scrollable area so the dialog can
        # never grow taller than the screen. Previously this was a bare
        # QFormLayout dropped straight into the dialog, so once enough rows
        # were added the bottom rows and the OK/Cancel buttons ran off-screen.
        scroll_content = QWidget()
        form = QFormLayout(scroll_content)
        form.setVerticalSpacing(8)
        form.setContentsMargins(12, 12, 4, 12)

        def add_section(title):
            label = QLabel(title)
            label.setStyleSheet("font-weight: bold; color: #cfcfcf; padding-top: 6px;")
            form.addRow(label)

        def add_gap(height=10):
            spacer = QLabel("")
            spacer.setFixedHeight(height)
            form.addRow(spacer)

        add_section("Auto-Hibernate")

        self.late_night_profiles = self.settings.get('late_night_profiles', {}) if isinstance(self.settings.get('late_night_profiles', {}), dict) else {}
        self.active_late_night_profile = str(self.settings.get('late_night_active_profile', 'Default') or 'Default')
        if 'Default' not in self.late_night_profiles:
            self.late_night_profiles['Default'] = {}

        profile_row = QHBoxLayout()
        self.late_night_profile_combo = QComboBox()
        self.late_night_profile_combo.addItems(sorted(self.late_night_profiles.keys()))
        active_index = self.late_night_profile_combo.findText(self.active_late_night_profile)
        self.late_night_profile_combo.setCurrentIndex(active_index if active_index >= 0 else 0)
        self.late_night_profile_combo.currentTextChanged.connect(self._load_selected_profile)
        profile_row.addWidget(self.late_night_profile_combo, 1)
        self.save_profile_btn = QPushButton("Save")
        self.save_profile_btn.clicked.connect(self._save_current_profile)
        profile_row.addWidget(self.save_profile_btn)
        self.delete_profile_btn = QPushButton("Delete")
        self.delete_profile_btn.clicked.connect(self._delete_selected_profile)
        profile_row.addWidget(self.delete_profile_btn)
        form.addRow("Late-Night Profiles:", profile_row)

        self.late_night_profile_name = QLineEdit()
        self.late_night_profile_name.setText(self.active_late_night_profile)
        self.late_night_profile_name.setPlaceholderText("Profile name")
        form.addRow("Profile Name:", self.late_night_profile_name)

        # Auto-hibernate enabled checkbox
        self.hibernate_enabled_check = QCheckBox()
        self.hibernate_enabled_check.setChecked(self.settings.get('auto_hibernate_enabled', True))
        form.addRow("Enable Auto-Hibernate:", self.hibernate_enabled_check)
        
        # Hibernate timeout slider
        timeout_layout = QHBoxLayout()
        self.timeout_slider = QSlider(Qt.Orientation.Horizontal)
        self.timeout_slider.setMinimum(5)  # 5 minutes
        self.timeout_slider.setMaximum(120)  # 120 minutes (2 hours)
        self.timeout_slider.setValue(self.settings.get('hibernate_timeout_minutes', 30))
        self.timeout_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.timeout_slider.setTickInterval(15)
        
        self.timeout_label = QLabel(f"{self.timeout_slider.value()} minutes")
        self.timeout_slider.valueChanged.connect(lambda v: self.timeout_label.setText(f"{v} minutes"))
        
        timeout_layout.addWidget(self.timeout_slider)
        timeout_layout.addWidget(self.timeout_label)
        
        form.addRow("Hibernate Timeout:", timeout_layout)

        # Late-night auto-hibernate
        self.late_night_check = QCheckBox()
        self.late_night_check.setChecked(self.settings.get('late_night_hibernate_enabled', True))
        form.addRow("Late-Night Auto-Hibernate:", self.late_night_check)

        self.late_night_hour_spin = QSpinBox()
        self.late_night_hour_spin.setRange(0, 23)
        self.late_night_hour_spin.setValue(int(self.settings.get('late_night_hibernate_hour', 3) or 3))
        self.late_night_hour_spin.setSuffix(":00")
        self.late_night_hour_spin.setToolTip(
            "Hibernation kicks in after this hour when idle time exceeds the threshold below.\n"
            "Active window: from this hour until the 'Late-Night Active Until' hour."
        )
        form.addRow("Late-Night Activate After:", self.late_night_hour_spin)

        self.late_night_end_hour_spin = QSpinBox()
        self.late_night_end_hour_spin.setRange(0, 23)
        self.late_night_end_hour_spin.setValue(int(self.settings.get('late_night_hibernate_end_hour', 6) or 6))
        self.late_night_end_hour_spin.setSuffix(":00")
        self.late_night_end_hour_spin.setToolTip(
            "Hibernation window ends at this hour.\n"
            "If End ≤ Start, the window wraps overnight (e.g. 03:00 → 06:00 or 23:00 → 05:00)."
        )
        form.addRow("Late-Night Active Until:", self.late_night_end_hour_spin)

        # Idle threshold for late-night hibernate
        idle_layout = QHBoxLayout()
        self.late_night_idle_spin = QSpinBox()
        self.late_night_idle_spin.setRange(1, 60)
        self.late_night_idle_spin.setValue(int(self.settings.get('late_night_idle_minutes', 5) or 5))
        self.late_night_idle_spin.setSuffix(" min")
        self.late_night_idle_spin.setToolTip(
            "How many minutes of inactivity are required after the Late-Night hour\n"
            "before the PC hibernates automatically."
        )
        idle_layout.addWidget(self.late_night_idle_spin)
        idle_layout.addStretch()
        form.addRow("Late-Night Idle Threshold:", idle_layout)

        self.late_night_media_half_check = QCheckBox()
        self.late_night_media_half_check.setChecked(
            self.settings.get('late_night_half_timeout_when_media_open', True)
        )
        self.late_night_media_half_check.setToolTip(
            "Halve the late-night idle threshold while a video is playing or manga/read mode is open.\n"
            "This helps avoid hibernating in the middle of watching or reading."
        )
        form.addRow("Halve Timeout While Watching/Reading:", self.late_night_media_half_check)

        self._apply_profile_to_widgets(self.active_late_night_profile)

        add_gap()

        add_section("Preview and Playback")

        self.preview_trigger_combo = QComboBox()
        self.preview_trigger_combo.addItem("Click", "click")
        self.preview_trigger_combo.addItem("Hover", "hover")
        preview_mode = str(self.settings.get('preview_trigger_mode', 'click')).strip().lower()
        preview_index = self.preview_trigger_combo.findData(preview_mode)
        self.preview_trigger_combo.setCurrentIndex(preview_index if preview_index >= 0 else 0)
        form.addRow("Preview Trigger:", self.preview_trigger_combo)

        self.long_time_format_check = QCheckBox()
        self.long_time_format_check.setChecked(self.settings.get('show_hours_for_long_media', False))
        form.addRow("Show Hours For Long Media:", self.long_time_format_check)

        add_gap()

        add_section("Ongoing Behavior")

        self.restore_last_session_check = QCheckBox()
        self.restore_last_session_check.setChecked(self.settings.get('restore_last_session_enabled', True))
        form.addRow("Restore Last Session:", self.restore_last_session_check)

        self.gofile_extraction_combo = QComboBox()
        self.gofile_extraction_combo.addItem("Browser (Default)", "browser")
        self.gofile_extraction_combo.addItem("Direct API (curl_cffi)", "api")
        gofile_mode = str(self.settings.get('gofile_extraction_method', 'browser')).strip().lower()
        idx = self.gofile_extraction_combo.findData(gofile_mode)
        if idx >= 0:
            self.gofile_extraction_combo.setCurrentIndex(idx)
        form.addRow("GoFile Extraction Method:", self.gofile_extraction_combo)

        self.auto_add_copied_links_check = QCheckBox()
        self.auto_add_copied_links_check.setChecked(self.settings.get('auto_add_copied_links', False))
        form.addRow("Auto-Load Copied Links:", self.auto_add_copied_links_check)
        
        self.enable_pdf_edge_triggers_check = QCheckBox()
        self.enable_pdf_edge_triggers_check.setChecked(self.settings.get('enable_pdf_edge_triggers', True))
        form.addRow("Enable PDF Edge Triggers:", self.enable_pdf_edge_triggers_check)

        add_gap()
        
        add_section("Analyse Folders")
        self.analyse_folders_widget = AnalyseFolderListWidget(self.settings.get('analyse_folders', []))
        form.addRow(self.analyse_folders_widget)

        add_gap()

        # Info label
        info_label = QLabel(
            "💡 Hibernate timeout determines how long the PC waits with no activity before hibernating.\n"
            "Move mouse, click, or press any key to reset the timer.\n\n"
            "Late-Night Auto-Hibernate hibernates the PC when the clock passes the set hour\n"
            "and idle time exceeds the Late-Night Idle Threshold (works even if Auto-Hibernate is off).\n"
            "The countdown starts when the active late-night window begins, not before it.\n"
            "If enabled, the timeout is cut in half while a video is playing or manga/read mode is open.\n\n"
            "Preview Trigger chooses whether the playlist/read preview opens on hover or on one click.\n"
            "Show Hours For Long Media controls whether long videos use HH:MM:SS.\n"
            "Restore Last Session controls whether the previous playlist is offered on app startup.\n"
            "Auto-Load Copied Links watches the clipboard and adds copied video links to the playlist.\n"
            "Enable PDF Edge Triggers shows hidden screen-edge buttons when reading a PDF in the background."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: gray; font-size: 10px; padding: 10px;")
        form.addRow(info_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        # Buttons (kept outside the scroll area so they're always visible/reachable)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # Cap the dialog height to the actual screen (multi-monitor aware) so
        # it can never be taller than the available desktop. Any overflow is
        # handled by the scroll area above instead of running off-screen.
        screen = self.screen() or QGuiApplication.primaryScreen()
        max_height = max(400, int(screen.availableGeometry().height() * 0.85)) if screen else 700
        self.setMaximumHeight(max_height)
        self.resize(540, min(680, max_height))
    
    def get_settings(self):
        """Get the edited settings"""
        profiles = self.late_night_profiles.copy()
        profile_name = str(self.late_night_profile_name.text() or 'Default').strip() or 'Default'
        profiles[profile_name] = self._current_profile_payload()
        return {
            'auto_hibernate_enabled': self.hibernate_enabled_check.isChecked(),
            'hibernate_timeout_minutes': self.timeout_slider.value(),
            'late_night_hibernate_enabled': self.late_night_check.isChecked(),
            'late_night_hibernate_hour': self.late_night_hour_spin.value(),
            'late_night_hibernate_end_hour': self.late_night_end_hour_spin.value(),
            'late_night_idle_minutes': self.late_night_idle_spin.value(),
            'late_night_half_timeout_when_media_open': self.late_night_media_half_check.isChecked(),
            'late_night_profiles': profiles,
            'late_night_active_profile': profile_name,
            'preview_trigger_mode': self.preview_trigger_combo.currentData(),
            'show_hours_for_long_media': self.long_time_format_check.isChecked(),
            'restore_last_session_enabled': self.restore_last_session_check.isChecked(),
            'gofile_extraction_method': self.gofile_extraction_combo.currentData(),
            'auto_add_copied_links': self.auto_add_copied_links_check.isChecked(),
            'enable_pdf_edge_triggers': self.enable_pdf_edge_triggers_check.isChecked(),
            'analyse_folders': self.analyse_folders_widget.get_data(),
        }

    def _apply_profile_to_widgets(self, profile_name):
        profile = self.late_night_profiles.get(str(profile_name or 'Default').strip() or 'Default', {})
        if not isinstance(profile, dict):
            return
        self.late_night_check.setChecked(profile.get('late_night_hibernate_enabled', self.late_night_check.isChecked()))
        self.late_night_hour_spin.setValue(int(profile.get('late_night_hibernate_hour', self.late_night_hour_spin.value()) or self.late_night_hour_spin.value()))
        self.late_night_end_hour_spin.setValue(int(profile.get('late_night_hibernate_end_hour', self.late_night_end_hour_spin.value()) or self.late_night_end_hour_spin.value()))
        self.late_night_idle_spin.setValue(int(profile.get('late_night_idle_minutes', self.late_night_idle_spin.value()) or self.late_night_idle_spin.value()))
        self.late_night_media_half_check.setChecked(profile.get('late_night_half_timeout_when_media_open', self.late_night_media_half_check.isChecked()))

    def _load_selected_profile(self):
        name = self.late_night_profile_combo.currentText()
        if name:
            self.late_night_profile_name.setText(name)
            self._apply_profile_to_widgets(name)

    def _current_profile_payload(self):
        return {
            'late_night_hibernate_enabled': self.late_night_check.isChecked(),
            'late_night_hibernate_hour': self.late_night_hour_spin.value(),
            'late_night_hibernate_end_hour': self.late_night_end_hour_spin.value(),
            'late_night_idle_minutes': self.late_night_idle_spin.value(),
            'late_night_half_timeout_when_media_open': self.late_night_media_half_check.isChecked(),
        }

    def _save_current_profile(self):
        name = str(self.late_night_profile_name.text() or self.late_night_profile_combo.currentText() or 'Default').strip() or 'Default'
        self.late_night_profiles[name] = self._current_profile_payload()
        self.settings['late_night_profiles'] = self.late_night_profiles
        self.settings['late_night_active_profile'] = name
        self.active_late_night_profile = name
        if self.late_night_profile_combo.findText(name) < 0:
            self.late_night_profile_combo.addItem(name)
        self.late_night_profile_combo.setCurrentText(name)
        parent = self.parent()
        if parent is not None and hasattr(parent, 'settings') and hasattr(parent, 'save_settings'):
            try:
                parent.settings['late_night_profiles'] = self.late_night_profiles
                parent.settings['late_night_active_profile'] = name
                parent.save_settings()
            except Exception:
                pass

    def _delete_selected_profile(self):
        name = str(self.late_night_profile_name.text() or self.late_night_profile_combo.currentText() or '').strip()
        if not name or name == 'Default':
            return
        if name in self.late_night_profiles:
            del self.late_night_profiles[name]
        idx = self.late_night_profile_combo.findText(name)
        if idx >= 0:
            self.late_night_profile_combo.removeItem(idx)
        fallback = self.late_night_profile_combo.currentText() or 'Default'
        self.late_night_profile_name.setText(fallback)
        self._apply_profile_to_widgets(fallback)
