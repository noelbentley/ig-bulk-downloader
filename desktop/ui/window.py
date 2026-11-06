from dataclasses import asdict
from pathlib import Path
from functools import wraps

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLayout, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import instaloader
from desktop.models import DownloadOptions, Job, parse_profile
from desktop.services.queue import QueueManager
from desktop.services.pacing import PacingConfig
from desktop.services.performance import PerformanceConfig, preset_pacing, summary_text
from desktop.services.performance import diagnostic_report
from desktop.services.pipeline import PipelineConfig
from desktop.storage.local import atomic_json
from desktop.storage.sessions import SessionStore
from desktop.ui.theme import stylesheet
from desktop.ui.authentication import AuthenticationDialog
from desktop.workers.tasks import AccountWorker


def button(label, callback, primary=False):
    widget = QPushButton(label)
    widget.clicked.connect(callback)
    if primary:
        widget.setObjectName("primary")
    return widget


def timed_gui(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        statistics = self.manager.statistics
        if statistics is not None and statistics.ended is None:
            with statistics.measure("gui_seconds"):
                return function(self, *args, **kwargs)
        return function(self, *args, **kwargs)
    return call


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setShowGrid(False)
    widget.setWordWrap(False)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setMinimumSectionSize(105)
    widget.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def fill_table(widget, rows):
    selected = widget.currentRow()
    if widget.rowCount() != len(rows):
        widget.setRowCount(len(rows))
    widget.setUpdatesEnabled(False)
    for row, values in enumerate(rows):
        for col, value in enumerate(values):
            value = str(value)
            cell = widget.item(row, col)
            if cell is None:
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                widget.setItem(row, col, cell)
            elif cell.text() != value:
                cell.setText(value)
                cell.setToolTip(value)
    widget.setUpdatesEnabled(True)
    if 0 <= selected < len(rows):
        widget.selectRow(selected)


class MainWindow(QMainWindow):
    def __init__(self, store):
        super().__init__()
        self._initializing = True
        self.store = store
        self.sessions = SessionStore(store.root)
        self.manager = QueueManager(store, self.sessions, parent=self)
        self.account_worker = None
        self.auth_dialog = None
        self.pending_login_request = None
        self.closing = False
        self.setWindowTitle("IG Bulk Downloader")
        self.resize(1180, 860)
        self.setMinimumSize(920, 700)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 22, 28, 16)
        layout.setSpacing(14)
        title_row = QHBoxLayout()
        title = QLabel("IG Bulk Downloader")
        title.setObjectName("title")
        title_row.addWidget(title)
        title_row.addStretch()
        self.theme = QComboBox()
        self.theme.addItems(["Dark", "Light"])
        self.theme.setCurrentText(store.settings["theme"])
        self.theme.currentTextChanged.connect(self.apply_theme)
        title_row.addWidget(self.theme)
        title_row.addWidget(button("Accent", self.choose_accent))
        layout.addLayout(title_row)
        subtitle = QLabel("Your Instagram archive, on your computer.  •  Powered by Instaloader " + instaloader.__version__)
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        self.auth_status = QLabel("Public Mode — No Login")
        self.auth_status.setObjectName("muted")
        layout.addWidget(self.auth_status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._dashboard()
        self._options()
        self._accounts()
        self._history()
        self._settings()
        self.manager.changed.connect(self.refresh)
        self.manager.activity.connect(self.log_activity)
        self.manager.idle.connect(self.on_idle)
        self.manager.authentication_required.connect(self.request_authentication)
        self.manager.mode_changed.connect(self.auth_status.setText)
        self.manager.performance_updated.connect(self.update_performance)
        self.manager.performance_finished.connect(self.finish_performance)
        self.diagnostics_timer = QTimer(self)
        self.diagnostics_timer.setInterval(1000)
        self.diagnostics_timer.timeout.connect(self.update_safety_status)
        self.refresh_accounts("")  # Saved sessions are listed, never activated at startup.
        self._initializing = False
        self.apply_theme()
        self.refresh()
        self.load_history()
        self.statusBar().showMessage(store.warning or "Ready • One Instagram profile at a time • All settings stay on this PC")

    def _dashboard(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        entry = QHBoxLayout()
        self.profile = QLineEdit()
        self.profile.setPlaceholderText("Instagram username or profile URL")
        self.profile.setAccessibleName("Instagram profile")
        self.profile.returnPressed.connect(self.add_profile)
        entry.addWidget(self.profile)
        entry.addWidget(button("Add to Queue", self.add_profile))
        self.start = button("Start Downloads", self.start_downloads, True)
        entry.addWidget(self.start)
        layout.addLayout(entry)
        destination = QHBoxLayout()
        destination.addWidget(QLabel("Save to"))
        self.destination = QLineEdit(self.store.settings["destination"])
        self.destination.setAccessibleName("Download destination")
        self.destination.editingFinished.connect(self.save_preferences)
        destination.addWidget(self.destination)
        destination.addWidget(button("Browse…", self.choose_destination))
        layout.addLayout(destination)
        hint = QLabel("Each queued profile keeps its own folder and options. Change media selection in Download Options.")
        hint.setObjectName("muted")
        layout.addWidget(hint)
        metrics = QHBoxLayout()
        self.counts = QLabel()
        self.counts.setObjectName("metric")
        metrics.addWidget(self.counts)
        self.file_count = QLabel()
        self.file_count.setObjectName("metric")
        metrics.addWidget(self.file_count)
        layout.addLayout(metrics)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.queue_table = table(["Profile", "Status", "Items checked", "New media files", "Current activity / details"])
        self.queue_table.setMinimumHeight(160)
        layout.addWidget(self.queue_table, 2)
        controls = QHBoxLayout()
        self.pause = button("Pause", self.manager.pause)
        controls.addWidget(self.pause)
        controls.addWidget(button("Cancel selected", self.cancel_selected))
        controls.addWidget(button("Retry selected", self.retry_selected))
        controls.addWidget(button("Open folder", self.open_selected))
        controls.addStretch()
        controls.addWidget(button("Clear finished", self.clear_finished))
        layout.addLayout(controls)
        self.current_activity = QLabel("Ready to download")
        self.current_activity.setWordWrap(True)
        layout.addWidget(self.current_activity)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setMaximumHeight(105)
        self.log.setPlaceholderText("Download activity appears here. No network requests are made until you start.")
        layout.addWidget(self.log, 1)
        self.tabs.addTab(page, "Dashboard && Queue")

    def _options(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("Download selection")
        grid = QGridLayout(group)
        self.options = {}
        supported = {"reels": hasattr(instaloader.Profile, "get_reels"),
                     "stories": hasattr(instaloader.Instaloader, "get_stories"),
                     "highlights": hasattr(instaloader.Instaloader, "get_highlights")}
        labels = [("photos", "Photos in posts"), ("videos", "Videos in posts"),
                  ("reels", "Reels tab"), ("stories", "Stories · login required"),
                  ("highlights", "Highlights · login required"), ("captions", "Captions (.txt)"),
                  ("profile_picture", "Profile picture")]
        saved = self.store.settings["options"]
        for i, (name, label) in enumerate(labels):
            check = QCheckBox(label)
            check.setChecked(bool(saved.get(name, False)) and supported.get(name, True))
            check.setEnabled(supported.get(name, True))
            check.toggled.connect(self.save_preferences)
            self.options[name] = check
            grid.addWidget(check, i // 2, i % 2)
        layout.addWidget(group)
        note = QLabel("Public photos, videos, Reels and carousels are attempted without login. Stories, Highlights and private profiles require your explicit choice of login or skip.\n\n"
                      "Original available files are saved without conversion. Existing media is skipped by Instaloader. Captions are optional; JSON metadata exports are disabled.\n\n"
                      "Incremental runs scan the selected collections again and skip existing files; they do not stop at the first existing post.\n\n"
                      "Photos/videos filter feed media, including mixed carousels. Reels, stories and highlights select their own media. "
                      "Videos visible in the feed can overlap the Reels tab; duplicate posts are skipped in a run.\n\n"
                      "Progress shows finished profiles, checked items and newly written media files. Total files and byte percentages are unknown.\n\n"
                      "Pause and cancellation take effect at safe boundaries, including after each saved carousel file. "
                      "An active request or file completes first (network timeout: 30 seconds). Retry safely skips files already present. "
                      "Configure randomized file pacing in Settings.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.tabs.addTab(page, "Download Options")

    def _accounts(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("Local Instagram sessions")
        form = QFormLayout(group)
        self.account = QComboBox()
        self.account.currentIndexChanged.connect(self.save_preferences)
        form.addRow("Saved session", self.account)
        self.use_session = QCheckBox("Use selected saved session for every download in this queue run")
        self.use_session.setChecked(False)  # Authorization is never restored from preferences.
        form.addRow(self.use_session)
        self.login_user = QLineEdit()
        self.login_user.setPlaceholderText("Instagram username")
        form.addRow("Username", self.login_user)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.password)
        actions = QHBoxLayout()
        self.login_button = button("Sign in && save session", self.login)
        self.verify_button = button("Verify saved session", self.verify_session)
        self.logout_button = button("Logout / delete session", self.logout)
        for action in (self.login_button, self.verify_button, self.logout_button):
            actions.addWidget(action)
        form.addRow(actions)
        self.session_status = QLabel("Public Mode — No Login · signing in saves a session; it does not automatically use it for downloads")
        self.session_status.setWordWrap(True)
        form.addRow(self.session_status)
        self.skip_auth_button = button("Skip requested content and continue", self.skip_auth_content)
        self.skip_auth_button.hide()
        form.addRow(self.skip_auth_button)
        layout.addWidget(group)
        note = QLabel("Passwords are used in memory for login and are never saved. Session cookies are encrypted with Windows DPAPI for your Windows account.\n\n"
                      "A saved session is loaded only after you approve its use for specific content or explicitly enable authenticated operation above. "
                      "Public Mode uses a separate context without saved cookies. Two-factor codes are supported. Complete checkpoints or security challenges in Instagram itself.\n\n"
                      "Logout deletes this app’s local session. It does not revoke other Instagram logins.\n\n"
                      "No cloud services, telemetry, web server or external database. Sessions and settings are stored in:\n" + str(self.store.root))
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.tabs.addTab(page, "Accounts")

    def _history(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.history_filter = QComboBox()
        self.history_filter.addItems(["All", "Completed", "Failed", "Cancelled", "Skipped"])
        self.history_filter.currentTextChanged.connect(self.load_history)
        layout.addWidget(self.history_filter)
        self.history_table = table(["Date (UTC)", "Profile", "Status", "Items checked", "New files", "Destination", "Details"])
        layout.addWidget(self.history_table)
        actions = QHBoxLayout()
        actions.addWidget(button("Retry from history", self.retry_history))
        actions.addWidget(button("Open download folder", self.open_history))
        actions.addStretch()
        layout.addLayout(actions)
        self.tabs.addTab(page, "History")

    def _settings(self):
        page = QWidget()
        page.setObjectName("settingsContent")
        layout = QVBoxLayout(page)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.viewport().setObjectName("settingsViewport")
        scroll.setWidget(page)
        self.performance_group = QGroupBox("Performance")
        form = QFormLayout(self.performance_group)
        form.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.performance_profile = QComboBox()
        self.performance_profile.addItems(["Balanced", "Conservative", "Custom"])
        self.performance_profile.setCurrentText(self.store.settings["performance"]["profile"])
        form.addRow("Performance profile", self.performance_profile)
        self.media_workers = QComboBox()
        self.media_workers.addItem("1 (supported)")
        self.media_workers.setEnabled(False)
        self.media_workers.setToolTip("Instaloader uses one transfer. Experimental public media workers are configured below.")
        form.addRow("Maximum simultaneous transfers", self.media_workers)
        self.protection_status = QLabel("Rate-limit protection: Enabled · Restriction cooldown: Automatic")
        self.protection_status.setWordWrap(True)
        form.addRow(self.protection_status)
        self.live_performance = QLabel("0 active transfers · 0.0 files/min")
        form.addRow(self.live_performance)
        self.last_performance = QLabel(summary_text(self.store.settings.get("last_performance")))
        self.last_performance.setWordWrap(True)
        self.last_performance.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.last_performance.setFixedHeight((self.last_performance.text().count("\n") + 1) * self.fontMetrics().lineSpacing() + 8)
        form.addRow("Last run", self.last_performance)
        self.export_report = button("Export diagnostic report…", self.export_diagnostic_report)
        form.addRow(self.export_report)
        self.review_button = button("I reviewed restrictions — continue after cooldown", self.manager.review_restrictions)
        self.review_button.setVisible(self.manager.safety.review)
        form.addRow(self.review_button)
        layout.addWidget(self.performance_group)
        self.optimized_group = QGroupBox("Optimized Public Downloads — Experimental")
        optimized_form = QFormLayout(self.optimized_group)
        pipeline = PipelineConfig(**self.store.settings["pipeline"])
        self.optimized_enabled = QCheckBox("Use optimized public-feed pipeline")
        self.optimized_enabled.setChecked(pipeline.enabled)
        optimized_form.addRow(self.optimized_enabled)
        self.optimized_workers = QSpinBox()
        self.optimized_workers.setRange(1, 5)
        self.optimized_workers.setValue(pipeline.workers)
        self.optimized_workers.setAccessibleName("Optimized public media workers")
        optimized_form.addRow("Simultaneous media transfers", self.optimized_workers)
        optimized_note = QLabel("Public feed photos/videos with captions off use the experimental pipeline. "
            "Instagram discovery stays sequential. Captions, other content and authenticated downloads use Instaloader. "
            "Existing rate rules and restrictions apply to both pipelines. Changes apply to the next queue run.")
        optimized_note.setWordWrap(True)
        optimized_form.addRow(optimized_note)
        layout.addWidget(self.optimized_group)
        self.optimized_enabled.toggled.connect(self.pipeline_toggled)
        self.optimized_workers.valueChanged.connect(self.pipeline_toggled)
        self.pipeline_toggled()
        self.pacing_group = QGroupBox("Randomized Download Pacing")
        group_layout = QVBoxLayout(self.pacing_group)
        config = PacingConfig(**self.store.settings["pacing"])
        self.pacing_enabled = QCheckBox("Enable randomized pacing")
        self.pacing_enabled.setChecked(config.enabled)
        group_layout.addWidget(self.pacing_enabled)
        grid = QGridLayout()
        grid.addWidget(QLabel("Minimum"), 0, 1)
        grid.addWidget(QLabel("Maximum"), 0, 2)
        self.pacing_inputs = {}
        pairs = [("batch_min", "batch_max", "Batch size", " files", 100000),
                 ("pause_min", "pause_max", "Batch pause", " seconds", 86400),
                 ("long_interval_min", "long_interval_max", "Long-rest interval", " files", 100000),
                 ("long_pause_min", "long_pause_max", "Long-rest duration", " seconds", 86400)]
        for row, (low, high, label, suffix, maximum) in enumerate(pairs, 1):
            grid.addWidget(QLabel(label), row, 0)
            for col, name in enumerate((low, high), 1):
                spin = QSpinBox()
                spin.setRange(1, maximum)
                spin.setSuffix(suffix)
                spin.setValue(getattr(config, name))
                spin.setAccessibleName(label + (" minimum" if col == 1 else " maximum"))
                self.pacing_inputs[name] = spin
                grid.addWidget(spin, row, col)
        group_layout.addLayout(grid)
        self.long_rest_enabled = QCheckBox("Enable longer periodic rests")
        self.long_rest_enabled.setChecked(config.long_rest_enabled)
        group_layout.addWidget(self.long_rest_enabled)
        for low, high, *_ in pairs:
            self.pacing_inputs[low].valueChanged.connect(
                lambda value, low=low, high=high: self.pacing_range_changed(low, high, True))
            self.pacing_inputs[high].valueChanged.connect(
                lambda value, low=low, high=high: self.pacing_range_changed(low, high, False))
        self.pacing_enabled.toggled.connect(self.pacing_toggled)
        self.long_rest_enabled.toggled.connect(self.pacing_toggled)
        self.pacing_toggled()
        self.performance_profile.currentTextChanged.connect(self.select_performance_profile)
        layout.addWidget(self.pacing_group)
        note = QLabel("Only successfully saved JPG/JPEG and MP4 files count, including individual carousel files. "
                      "Skipped files, failed transfers, captions and metadata do not count.\n\n"
                      "Balanced removes extra pacing waits. Conservative uses randomized breaks. Edit values to use Custom. "
                      "Manual pause freezes pacing countdowns. Changes apply to the next queue run. "
                      "One Instagram profile runs at a time. Instaloader uses one media worker; the experimental public-feed pipeline uses its configured limit. "
                      "Instagram restrictions always take priority.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.tabs.addTab(scroll, "Settings")

    def select_performance_profile(self, profile):
        custom = PacingConfig(**self.store.settings["custom_pacing"])
        config = preset_pacing(profile, custom)
        for widget, value in ((self.pacing_enabled, config.enabled), (self.long_rest_enabled, config.long_rest_enabled)):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        for key, spin in self.pacing_inputs.items():
            spin.blockSignals(True)
            spin.setValue(getattr(config, key))
            spin.blockSignals(False)
        self.pacing_toggled()

    def use_custom_performance(self):
        self.performance_profile.blockSignals(True)
        self.performance_profile.setCurrentText("Custom")
        self.performance_profile.blockSignals(False)

    def pipeline_toggled(self, *_):
        enabled = self.optimized_enabled.isChecked()
        self.optimized_workers.setEnabled(enabled)
        self.media_workers.setItemText(0, f"{self.optimized_workers.value()} (optimized public feed)" if enabled else "1 (supported)")
        self.save_preferences()

    @timed_gui
    def update_performance(self, data):
        self.live_performance.setText(f"{data['active_transfers']} active transfers · {data['files_per_minute']:.1f} files/min")

    def finish_performance(self, data):
        self.last_performance.setText(summary_text(data))
        self.last_performance.setFixedHeight((self.last_performance.text().count("\n") + 1) * self.fontMetrics().lineSpacing() + 8)
        self.update_performance(data)

    def export_diagnostic_report(self):
        source = self.manager.statistics.snapshot() if self.manager.statistics else self.store.settings.get("last_performance", {})
        report = diagnostic_report(source)
        if not report:
            self.statusBar().showMessage("Run one ordinary download to collect diagnostics first.")
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export numeric diagnostic report",
            str(self.store.root / "diagnostics" / "latest.json"), "Diagnostic JSON (*.json)")
        if filename:
            try:
                atomic_json(Path(filename), report)
                self.statusBar().showMessage("Diagnostic report exported; credentials and media URLs are excluded.")
            except OSError as error:
                self.show_error(error)

    def get_pacing(self):
        return PacingConfig(enabled=self.pacing_enabled.isChecked(),
                            long_rest_enabled=self.long_rest_enabled.isChecked(),
                            **{key: spin.value() for key, spin in self.pacing_inputs.items()})

    def pacing_range_changed(self, low, high, minimum_changed):
        self.use_custom_performance()
        minimum, maximum = self.pacing_inputs[low], self.pacing_inputs[high]
        if minimum.value() > maximum.value():
            target, value = (maximum, minimum.value()) if minimum_changed else (minimum, maximum.value())
            target.blockSignals(True)
            target.setValue(value)
            target.blockSignals(False)
        self.save_preferences()

    def pacing_toggled(self, *_):
        if self.sender() in (self.pacing_enabled, self.long_rest_enabled):
            self.use_custom_performance()
        enabled = self.pacing_enabled.isChecked()
        self.long_rest_enabled.setEnabled(enabled)
        for name, spin in self.pacing_inputs.items():
            spin.setEnabled(enabled and (not name.startswith("long_") or self.long_rest_enabled.isChecked()))
        self.save_preferences()

    def show_error(self, error):
        QMessageBox.warning(self, "IG Bulk Downloader", str(error))

    def selected_job(self):
        row = self.queue_table.currentRow()
        if 0 <= row < len(self.manager.jobs):
            return self.manager.jobs[row]
        raise ValueError("Select a profile in the queue first.")

    def get_options(self):
        return DownloadOptions(**{key: check.isChecked() for key, check in self.options.items()})

    def save_preferences(self, *_):
        if self._initializing or not hasattr(self, "account") or not hasattr(self, "pacing_inputs"):
            return
        self.store.settings.update(destination=self.destination.text().strip(), theme=self.theme.currentText(),
                                   options=asdict(self.get_options()), account=self.account.currentData() or "",
                                   pacing=asdict(self.get_pacing()),
                                   pipeline=asdict(PipelineConfig(self.optimized_enabled.isChecked(), self.optimized_workers.value())),
                                   performance=asdict(PerformanceConfig(self.performance_profile.currentText())))
        if self.performance_profile.currentText() == "Custom":
            self.store.settings["custom_pacing"] = asdict(self.get_pacing())
        self.store.save_settings()

    def apply_theme(self, *_):
        self.setStyleSheet(stylesheet(self.theme.currentText(), self.store.settings["accent"]))
        self.save_preferences()

    def choose_accent(self):
        color = QColorDialog.getColor(QColor(self.store.settings["accent"]), self, "Choose accent colour")
        if color.isValid():
            self.store.settings["accent"] = color.name()
            self.apply_theme()

    def choose_destination(self):
        folder = QFileDialog.getExistingDirectory(self, "Download destination", self.destination.text())
        if folder:
            self.destination.setText(folder)
            self.save_preferences()

    def add_profile(self):
        try:
            username = parse_profile(self.profile.text())
            raw = self.destination.text().strip()
            if not raw or not Path(raw).is_absolute():
                raise ValueError("Choose an absolute destination folder.")
            self.manager.add(username, str(Path(raw)), self.get_options())
            self.profile.clear()
            self.save_preferences()
        except (ValueError, OSError) as error:
            self.show_error(error)

    def start_downloads(self):
        if self.account_worker:
            return
        if any(j.status == "Queued" for j in self.manager.jobs):
            account = self.account.currentData() if self.use_session.isChecked() else ""
            if self.use_session.isChecked() and not account:
                self.show_error("Select a saved session, or turn off authenticated operation to use Public Mode.")
                return
            self.manager.start(account or "")
        else:
            self.statusBar().showMessage("Add a profile or retry an item first.")

    def cancel_selected(self):
        try:
            self.manager.cancel(self.selected_job())
        except ValueError as error:
            self.show_error(error)

    def retry_selected(self):
        try:
            self.manager.retry(self.selected_job())
        except ValueError as error:
            self.show_error(error)

    def open_folder(self, folder):
        if not Path(folder).is_dir():
            self.show_error("This download folder has not been created yet.")
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_selected(self):
        try:
            self.open_folder(self.selected_job().folder)
        except ValueError as error:
            self.show_error(error)

    def clear_finished(self):
        self.manager.jobs[:] = [j for j in self.manager.jobs if j.status not in ("Completed", "Failed", "Cancelled", "Skipped")]
        self.manager.persist()

    @timed_gui
    def refresh(self):
        jobs = self.manager.jobs
        completed = sum(j.status == "Completed" for j in jobs)
        failed = sum(j.status == "Failed" for j in jobs)
        terminal = sum(j.status in ("Completed", "Failed", "Cancelled", "Skipped") for j in jobs)
        self.counts.setText(f"{completed} completed   /   {failed} failed   /   {len(jobs)} profiles")
        self.file_count.setText(f"{sum(j.files for j in jobs)} new media files")
        self.progress.setRange(0, max(1, len(jobs)))
        self.progress.setValue(terminal)
        self.progress.setFormat(f"{terminal} / {len(jobs)} profiles finished")
        fill_table(self.queue_table, [("@" + j.username, j.status, j.items, j.files, j.detail) for j in jobs])
        busy = self.manager.running or self.manager.worker is not None
        self.start.setEnabled(not busy and self.account_worker is None)
        self.pause.setEnabled(busy)
        self.pause.setText("Resume" if self.manager.paused else "Pause")
        self.pacing_group.setEnabled(not busy)
        self.optimized_group.setEnabled(not busy)
        self.performance_profile.setEnabled(not busy)
        self.review_button.setEnabled(not busy)
        self.update_safety_status()

        login_requested = self.pending_login_request is not None and self.pending_login_request is self.manager.auth_pending
        for widget in (self.account, self.login_button, self.verify_button):
            widget.setEnabled((not busy or login_requested) and self.account_worker is None)
        self.logout_button.setEnabled(not busy and self.account_worker is None)
        self.use_session.setEnabled(not busy and self.account_worker is None)
        self.skip_auth_button.setVisible(login_requested)
        self.skip_auth_button.setEnabled(self.account_worker is None)
        if self.manager.auth_pending is None:
            self.pending_login_request = None
            if self.auth_dialog:
                self.auth_dialog.dismiss()
                self.auth_dialog = None

    def update_safety_status(self):
        self.review_button.setVisible(self.manager.safety.review)
        remaining = self.manager.safety.remaining()
        self.protection_status.setText("Manual restriction review required" if self.manager.safety.review else
            f"Rate-limit protection: Enabled · Cooldown {remaining:.0f}s remaining" if remaining > 0 else
            "Rate-limit protection: Enabled · Restriction cooldown: Automatic")
        if self.manager.worker is not None and self.manager.statistics:
            self.update_performance(self.manager.statistics.snapshot())
        if self.manager.worker is not None or remaining > 0 or self.manager.safety.review:
            if not self.diagnostics_timer.isActive():
                self.diagnostics_timer.start()
        else:
            self.diagnostics_timer.stop()

    @timed_gui
    def log_activity(self, message):
        self.current_activity.setText(message[-220:])
        self.log.appendPlainText(message)

    def on_idle(self):
        self.use_session.setChecked(False)
        self.auth_status.setText("Public Mode — No Login")
        self.refresh()
        self.load_history()
        if self.closing:
            QTimer.singleShot(0, self.close)

    def refresh_accounts(self, selected=None):
        selected = selected if selected is not None else self.store.settings.get("account", "")
        self.account.blockSignals(True)
        self.account.clear()
        self.account.addItem("Public / No Login", "")
        for name in self.sessions.accounts():
            self.account.addItem("@" + name + " · saved locally", name)
        index = self.account.findData(selected)
        self.account.setCurrentIndex(max(0, index))
        self.account.blockSignals(False)

    def launch_account_task(self, username, password=None):
        allowed = self.pending_login_request is not None and self.pending_login_request is self.manager.auth_pending
        if (self.manager.worker is not None and not allowed) or self.account_worker:
            return
        try:
            self.account_worker = AccountWorker(self.sessions, username, password, self)
        except ValueError as error:
            self.show_error(error)
            return
        self.session_status.setText("Contacting Instagram…")
        self.account_worker.two_factor.connect(self.two_factor)
        self.account_worker.finished.connect(self.account_finished)
        self.refresh()
        self.account_worker.start()

    def login(self):
        password = self.password.text()
        self.password.clear()
        if not password:
            self.show_error("Enter your Instagram password.")
            return
        self.launch_account_task(self.login_user.text(), password)

    def verify_session(self):
        name = self.account.currentData()
        if name:
            self.launch_account_task(name)
        else:
            self.show_error("Select a saved session first.")

    def two_factor(self):
        if self.closing:
            self.account_worker.submit_code(None)
            return
        code, accepted = QInputDialog.getText(self, "Instagram two-factor login", "Enter the code from your authenticator or SMS:")
        self.account_worker.submit_code(code.strip() if accepted else None)

    def account_finished(self):
        worker = self.account_worker
        self.session_status.setText(worker.message)
        self.refresh_accounts(worker.username if worker.success else None)
        self.account_worker = None
        worker.deleteLater()
        if worker.success and self.pending_login_request is not None:
            self.manager.respond_auth(worker.username, self.pending_login_request)
            self.pending_login_request = None
        self.save_preferences()
        self.refresh()
        if self.closing:
            QTimer.singleShot(0, self.close)

    def logout(self):
        name = self.account.currentData()
        if name:
            try:
                self.sessions.delete(name)
                self.refresh_accounts("")
                self.save_preferences()
                self.session_status.setText(f"@{name}: local session deleted")
            except OSError as error:
                self.show_error(error)

    def request_authentication(self, request):
        if self.closing:
            self.manager.respond_auth(None, request)
            return
        if self.auth_dialog:
            self.auth_dialog.dismiss()
        self.auth_dialog = AuthenticationDialog(request, self.sessions.accounts(), self)
        self.auth_dialog.chosen.connect(lambda action, account: self.auth_choice(request, action, account))
        self.auth_dialog.show()  # Nonblocking; download worker waits without blocking Qt.

    def auth_choice(self, request, action, account):
        self.auth_dialog = None
        if request is not self.manager.auth_pending:
            return
        if action == "login":
            self.pending_login_request = request
            self.tabs.setCurrentIndex(2)
            self.session_status.setText(f"Sign in to continue @{request['username']} · {request['content']}, or skip this content.")
            self.refresh()
            self.login_user.setFocus()
        else:
            self.manager.respond_auth(account if action == "saved" else None, request)

    def skip_auth_content(self):
        self.manager.respond_auth(None, self.pending_login_request)
        self.pending_login_request = None

    def load_history(self, *_):
        selected = self.history_filter.currentText()
        self.history_rows = [row for row in self.store.history() if selected == "All" or row[3] == selected]
        fill_table(self.history_table, [row[1:8] for row in self.history_rows])

    def history_row(self):
        index = self.history_table.currentRow()
        if not 0 <= index < len(self.history_rows):
            raise ValueError("Select a history entry first.")
        return self.history_rows[index]

    def retry_history(self):
        import json
        try:
            job = Job.from_dict(json.loads(self.history_row()[8]))
            existing = next((item for item in self.manager.jobs if item.id == job.id), None)
            if existing:
                self.manager.retry(existing)
            else:
                self.manager.add(job.username, job.destination, job.options)
            self.tabs.setCurrentIndex(0)
        except (ValueError, OSError) as error:
            self.show_error(error)

    def open_history(self):
        try:
            self.open_folder(self.history_row()[6])
        except ValueError as error:
            self.show_error(error)

    def closeEvent(self, event):
        if self.manager.worker is not None or self.account_worker is not None:
            self.closing = True
            self.manager.stop()
            if self.account_worker:
                self.account_worker.submit_code(None)
            self.statusBar().showMessage("Finishing the active request before closing safely…")
            event.ignore()
            return
        self.save_preferences()
        self.manager.persist()
        event.accept()
