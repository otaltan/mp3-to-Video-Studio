import sys
import subprocess
import threading
import re
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QComboBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class TrackTableWidget(QTableWidget):
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adjust_column_widths()

    def _adjust_column_widths(self) -> None:
        if self.columnCount() < 4:
            return
        duration_width = self.columnWidth(3)
        checkbox_width = 48
        available = max(0, self.viewport().width() - checkbox_width - duration_width)
        song_width = int(available * 2 / 3)
        cover_width = available - song_width
        self.setColumnWidth(0, checkbox_width)
        self.setColumnWidth(1, song_width)
        self.setColumnWidth(2, cover_width)
        self.setColumnWidth(3, duration_width)


@dataclass
class TrackItem:
    audio_path: Path
    cover_path: Path | None = None


class ConverterWorker(QThread):
    progress_changed = Signal(int)
    status_changed = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)
    file_progress_changed = Signal(int)
    file_status_changed = Signal(str)

    def __init__(self, tracks: list[TrackItem], output_dir: Path, quality_preset: str):
        super().__init__()
        self.tracks = tracks
        self.output_dir = output_dir
        self.quality_preset = quality_preset
        self._cancel_requested = False
        self._current_process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()

    def cancel(self) -> None:
        self._cancel_requested = True
        with self._process_lock:
            if self._current_process is not None and self._current_process.poll() is None:
                self._current_process.kill()

    def run(self) -> None:
        try:
            total = max(len(self.tracks), 1)
            for index, track in enumerate(self.tracks, start=1):
                if self._cancel_requested:
                    self.failed.emit("Export canceled.")
                    return
                if track.cover_path is None:
                    raise ValueError(f"Kein Cover zugewiesen: {track.audio_path.name}")
                duration = get_audio_duration(track.audio_path)
                self.status_changed.emit("Exporting, please wait...")
                self.file_status_changed.emit(track.audio_path.name)
                output_file = self.output_dir / f"{track.audio_path.stem}.mp4"
                source_bitrate = self._audio_bitrate_kbps(track.audio_path)
                quality_preset = self._effective_quality_preset(source_bitrate)
                command = build_ffmpeg_command(track.audio_path, track.cover_path, output_file, duration, quality_preset)
                with self._process_lock:
                    self._current_process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                process = self._current_process
                stderr_lines: list[str] = []
                if process is None or process.stderr is None:
                    raise RuntimeError("ffmpeg process could not be started.")
                time_pattern = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
                while True:
                    if self._cancel_requested:
                        process.kill()
                        self.failed.emit("Export canceled.")
                        return
                    line = process.stderr.readline()
                    if not line and process.poll() is not None:
                        break
                    if line:
                        stderr_lines.append(line)
                        match = time_pattern.search(line)
                        if match:
                            hours = int(match.group(1))
                            minutes = int(match.group(2))
                            seconds = float(match.group(3))
                            current_seconds = hours * 3600 + minutes * 60 + seconds
                            file_progress = min(100, int((current_seconds / max(duration, 0.001)) * 100))
                            self.file_progress_changed.emit(file_progress)
                            overall = int(((index - 1) / total) * 100 + (file_progress / total))
                            self.progress_changed.emit(min(99, overall))
                stdout = ""
                stderr = "".join(stderr_lines)
                if self._cancel_requested:
                    self.failed.emit("Export canceled.")
                    return
                if process is not None and process.returncode not in (0, None):
                    raise RuntimeError(stderr.strip() or stdout.strip() or "ffmpeg export failed.")
                self.file_progress_changed.emit(100)
                self.progress_changed.emit(int(index / total * 100))
            self.progress_changed.emit(100)
            self.finished_ok.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

    def _audio_bitrate_kbps(self, audio_path: Path) -> int | None:
        audio = MutagenFile(audio_path)
        bitrate = getattr(getattr(audio, "info", None), "bitrate", None)
        if bitrate is None:
            return None
        return int(round(bitrate / 1000))

    def _effective_quality_preset(self, source_bitrate_kbps: int | None) -> str:
        selected_audio_kbps = self._extract_audio_kbps(self.quality_preset)
        if source_bitrate_kbps is None or selected_audio_kbps is None or source_bitrate_kbps >= selected_audio_kbps:
            return self.quality_preset
        return self._quality_for_audio_bitrate(source_bitrate_kbps) or self.quality_preset

    def _extract_audio_kbps(self, quality_label: str) -> int | None:
        match = re.search(r"Audio:\s*(\d+)\s*kbps", quality_label)
        return int(match.group(1)) if match else None

    def _quality_for_audio_bitrate(self, bitrate_kbps: int) -> str | None:
        if bitrate_kbps <= 128:
            return "Audio: 128 kbps | Video: 720p"
        if bitrate_kbps <= 192:
            return "Audio: 192 kbps | Video: 1080p"
        if bitrate_kbps <= 256:
            return "Audio: 256 kbps | Video: 1440p"
        return "Audio: 320 kbps | Video: 4K"


def get_audio_duration(audio_path: Path) -> float:
    audio = MutagenFile(audio_path)
    if audio is None or not getattr(audio, "info", None):
            raise ValueError(f"Could not read audio duration: {audio_path.name}")
    return float(audio.info.length)


def build_ffmpeg_command(
    audio_path: Path,
    cover_path: Path,
    output_path: Path,
    duration: float,
    quality_preset: str,
) -> list[str]:
    presets = {
        "Audio: 128 kbps | Video: 720p": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "128k"],
        "Audio: 192 kbps | Video: 1080p": ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "192k"],
        "Audio: 256 kbps | Video: 1440p": ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-c:a", "aac", "-b:a", "256k"],
        "Audio: 320 kbps | Video: 4K": ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-c:a", "aac", "-b:a", "320k"],
    }
    quality_args = presets.get(quality_preset, presets["Audio: 192 kbps | Video: 1080p"])
    return [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(cover_path),
        "-i",
        str(audio_path),
        *quality_args,
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        "-t",
        f"{duration:.3f}",
        str(output_path),
    ]


def export_track_to_mp4(audio_path: Path, cover_path: Path, output_path: Path, duration: float) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(cover_path),
        "-i",
        str(audio_path),
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        "-t",
        f"{duration:.3f}",
        str(output_path),
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate()
        except Exception:
            process.kill()
            raise
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
    except FileNotFoundError as exc:
        raise FileNotFoundError("ffmpeg was not found. Please install ffmpeg and add it to PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise RuntimeError(stderr or "ffmpeg-Export fehlgeschlagen.") from exc


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MP3 to Video Studio")
        icon_path = Path(__file__).parent / "assets" / "mp3tovideo.svg"
        self.app_icon = QIcon(str(icon_path))
        self.setWindowIcon(self.app_icon)
        self.resize(1180, 760)
        self.tracks: list[TrackItem] = []
        self.worker: ConverterWorker | None = None
        self.is_exporting = False
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        logo = QLabel()
        logo.setPixmap(self.app_icon.pixmap(48, 48))
        logo.setFixedSize(48, 48)
        header_layout.addWidget(logo)
        title = QLabel("MP3 to Video Studio")
        title.setObjectName("Title")
        subtitle = QLabel("Select multiple songs, assign covers, and export MP4 videos.")
        subtitle.setObjectName("Subtitle")
        text_box = QVBoxLayout()
        text_box.addWidget(title)
        text_box.addWidget(subtitle)
        header_layout.addLayout(text_box)
        header_layout.addStretch()

        self.add_button = QPushButton("Add songs")
        self.cover_button = QPushButton("Assign cover")
        self.remove_button = QPushButton("Remove")
        self.export_button = QPushButton("Start export")
        self.quality_combo = QComboBox()
        self.quality_combo.addItems([
            "Audio: 128 kbps | Video: 720p",
            "Audio: 192 kbps | Video: 1080p",
            "Audio: 256 kbps | Video: 1440p",
            "Audio: 320 kbps | Video: 4K",
        ])
        self.quality_combo.setCurrentText("Audio: 192 kbps | Video: 1080p")
        for btn in (self.add_button, self.cover_button, self.remove_button, self.export_button):
            btn.setCursor(Qt.PointingHandCursor)

        self.add_button.clicked.connect(self.add_tracks)
        self.cover_button.clicked.connect(self.assign_cover)
        self.remove_button.clicked.connect(self.remove_selected)
        self.export_button.clicked.connect(self.start_export)

        toolbar = QFrame()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.addWidget(self.add_button)
        toolbar_layout.addWidget(self.cover_button)
        toolbar_layout.addWidget(self.remove_button)
        toolbar_layout.addStretch()
        toolbar_layout.addWidget(self.quality_combo)
        toolbar_layout.addWidget(self.export_button)

        self.table = TrackTableWidget(0, 3)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["✓", "Song", "Cover", "Duration"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 48)
        self.table.setColumnWidth(1, 360)
        self.table.setColumnWidth(2, 240)
        self.table.setColumnWidth(3, 90)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeaderItem(1).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.horizontalHeaderItem(2).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.horizontalHeaderItem(3).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.cellClicked.connect(self.toggle_row_checkbox)
        self.table.horizontalHeader().sectionClicked.connect(self.toggle_all_checkboxes)

        self.preview = QLabel("Cover preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(260)
        self.preview.setObjectName("Preview")
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.overall_status = QLabel("Overall progress: 0/0 exported")
        self.overall_status.setObjectName("Status")
        self.file_progress = QProgressBar()
        self.file_progress.setValue(0)
        self.file_progress.setFormat("Current file: %p%")
        self.status = QLabel("Ready")
        self.status.setObjectName("Status")
        self.file_status = QLabel("Current file: —")
        self.file_status.setObjectName("Status")

        content = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self.table)
        left.addWidget(self.overall_status)
        left.addWidget(self.file_progress)
        left.addWidget(self.file_status)
        left.addWidget(self.status)
        right = QVBoxLayout()
        right.addWidget(self.preview)
        content.addLayout(left, 3)
        content.addLayout(right, 2)

        layout.addWidget(header)
        layout.addWidget(toolbar)
        layout.addLayout(content)
        self.setCentralWidget(root)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #0f172a; }
            QFrame { background: transparent; }
            QLabel { color: #e2e8f0; font-size: 14px; }
            QLabel#Title { font-size: 28px; font-weight: 700; }
            QLabel#Subtitle { color: #94a3b8; }
            QLabel#Status { color: #cbd5e1; }
            QLabel#Preview {
                border: 1px solid #334155;
                border-radius: 18px;
                background: #111827;
                color: #64748b;
            }
            QTableWidget {
                background: #111827;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 16px;
                gridline-color: #1f2937;
                selection-background-color: #2563eb;
            }
            QTableWidget::item:selected {
                background: #2563eb;
                color: white;
            }
            QHeaderView::section {
                background: #1e293b;
                color: #cbd5e1;
                padding: 10px;
                border: none;
            }
            QPushButton {
                background: #1d4ed8;
                color: white;
                border: none;
                padding: 12px 18px;
                border-radius: 12px;
                font-weight: 600;
            }
            QPushButton:hover { background: #2563eb; }
            QPushButton:disabled { background: #334155; color: #94a3b8; }
            QPushButton#CancelButton {
                background: #dc2626;
                color: white;
            }
            QPushButton#CancelButton:hover { background: #ef4444; }
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 10px;
                background: #111827;
                color: #e2e8f0;
                text-align: center;
                height: 18px;
            }
            QProgressBar::chunk { background: #22c55e; border-radius: 10px; }
            """
        )

    def add_tracks(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select MP3 files", "", "Audio (*.mp3)")
        for file_name in files:
            path = Path(file_name)
            self.tracks.append(TrackItem(audio_path=path))
        self.refresh_table()

    def assign_cover(self) -> None:
        checked_rows = self.checked_rows()
        if not checked_rows:
            QMessageBox.information(self, "Info", "Please select one or more songs using the checkbox first.")
            return
        file_name, _ = QFileDialog.getOpenFileName(self, "Select cover", "", "Images (*.png *.jpg *.jpeg)")
        if not file_name:
            return
        cover_path = Path(file_name)
        for row in checked_rows:
            self.tracks[row].cover_path = cover_path
        self.refresh_table()
        self.show_preview(cover_path)

    def remove_selected(self) -> None:
        for row in sorted(self.checked_rows(), reverse=True):
            self.tracks.pop(row)
            self.refresh_table()
            self.preview.setText("Cover preview")
            self.preview.setPixmap(QPixmap())

    def refresh_table(self) -> None:
        self.table.setRowCount(len(self.tracks))
        for row, track in enumerate(self.tracks):
            duration = get_audio_duration(track.audio_path) if track.audio_path.exists() else 0
            checkbox_item = QTableWidgetItem("☑" if row in self.checked_rows() else "☐")
            checkbox_item.setTextAlignment(Qt.AlignCenter)
            song_item = QTableWidgetItem(track.audio_path.name)
            song_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            cover_item = QTableWidgetItem(track.cover_path.name if track.cover_path else "—")
            cover_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            duration_item = QTableWidgetItem(f"{duration:.1f} s")
            duration_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, 0, checkbox_item)
            self.table.setItem(row, 1, song_item)
            self.table.setItem(row, 2, cover_item)
            self.table.setItem(row, 3, duration_item)
        self.update_header_checkbox()
        self.table._adjust_column_widths()

    def select_track_cover(self, row: int) -> None:
        if row < 0 or row >= len(self.tracks):
            return
        cover_path = self.tracks[row].cover_path
        if cover_path:
            self.show_preview(cover_path)
        else:
            self.preview.setText("Cover preview")
            self.preview.setPixmap(QPixmap())

    def checked_rows(self) -> list[int]:
        rows: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == "☑":
                rows.append(row)
        return rows

    def all_rows_checked(self) -> bool:
        return self.table.rowCount() > 0 and len(self.checked_rows()) == self.table.rowCount()

    def update_header_checkbox(self) -> None:
        header_item = self.table.horizontalHeaderItem(0)
        if header_item is not None:
            header_item.setText("☑" if self.all_rows_checked() else "☐")

    def toggle_row_checkbox(self, row: int, column: int) -> None:
        if row < 0:
            return
        checkbox_item = self.table.item(row, 0)
        if checkbox_item is None:
            return
        is_checked = checkbox_item.text() == "☑"
        checkbox_item.setText("☐" if is_checked else "☑")
        self.update_header_checkbox()

    def toggle_all_checkboxes(self, column: int) -> None:
        if column != 0:
            return
        target_state = not self.all_rows_checked()
        for row in range(self.table.rowCount()):
            checkbox_item = self.table.item(row, 0)
            if checkbox_item is not None:
                checkbox_item.setText("☑" if target_state else "☐")
        self.update_header_checkbox()

    def show_preview(self, cover_path: Path | None) -> None:
        if cover_path and cover_path.exists():
            pixmap = QPixmap(str(cover_path)).scaled(420, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.preview.setPixmap(pixmap)
        else:
            self.preview.setText("Cover preview")

    def start_export(self) -> None:
        if self.is_exporting:
            self.cancel_export()
            return
        if not self.tracks:
            QMessageBox.information(self, "Info", "Please add songs first.")
            return
        output_dir = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if not output_dir:
            return
        selected_quality = self.quality_combo.currentText()
        if not self._confirm_audio_quality(selected_quality):
            self.status.setText("Export canceled")
            return
        self.worker = ConverterWorker(self.tracks, Path(output_dir), selected_quality)
        self.worker.progress_changed.connect(self._update_overall_progress)
        self.worker.file_progress_changed.connect(self.file_progress.setValue)
        self.worker.file_status_changed.connect(lambda name: self.file_status.setText(f"Current file: {name}"))
        self.worker.status_changed.connect(self.status.setText)
        self.worker.finished_ok.connect(self._export_finished)
        self.worker.failed.connect(self._show_error)
        self.worker.finished.connect(self._export_done)
        self.is_exporting = True
        self._set_export_controls_enabled(False)
        self.overall_status.setText(f"Overall progress: 0/{len(self.tracks)} exported")
        self.file_progress.setValue(0)
        self.file_status.setText("Current file: —")
        self.export_button.setText("Cancel")
        self.export_button.setObjectName("CancelButton")
        self.export_button.style().unpolish(self.export_button)
        self.export_button.style().polish(self.export_button)
        self.worker.start()

    def cancel_export(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.status.setText("Canceling export...")

    def _export_done(self) -> None:
        self.is_exporting = False
        self._set_export_controls_enabled(True)
        self.export_button.setText("Start export")
        self.export_button.setObjectName("")
        self.export_button.style().unpolish(self.export_button)
        self.export_button.style().polish(self.export_button)

    def _set_export_controls_enabled(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        self.cover_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.quality_combo.setEnabled(enabled)
        self.table.setEnabled(enabled)

    def _confirm_audio_quality(self, selected_quality: str) -> bool:
        selected_audio_kbps = self._extract_audio_kbps(selected_quality)
        if selected_audio_kbps is None:
            return True
        lowest_source_kbps = self._lowest_audio_bitrate()
        if lowest_source_kbps is None or lowest_source_kbps >= selected_audio_kbps:
            return True
        message = (
            "Some audio tracks are lower quality than the selected preset. "
            "The quality cannot be increased, so a lower preset will be used instead. "
            "Do you want to continue?"
        )
        result = self._show_question("Adjust quality", message)
        if result == QMessageBox.No:
            return False
        return True

    def _lowest_audio_bitrate(self) -> int | None:
        bitrates: list[int] = []
        for track in self.tracks:
            bitrate = self._audio_bitrate_kbps(track.audio_path)
            if bitrate is not None:
                bitrates.append(bitrate)
        return min(bitrates) if bitrates else None

    def _audio_bitrate_kbps(self, audio_path: Path) -> int | None:
        audio = MutagenFile(audio_path)
        bitrate = getattr(getattr(audio, "info", None), "bitrate", None)
        if bitrate is None:
            return None
        return int(round(bitrate / 1000))

    def _extract_audio_kbps(self, quality_label: str) -> int | None:
        match = re.search(r"Audio:\s*(\d+)\s*kbps", quality_label)
        return int(match.group(1)) if match else None

    def _show_question(self, title: str, message: str) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        box.setStyleSheet(
            """
            QMessageBox { background: #0f172a; }
            QMessageBox QLabel { color: #e2e8f0; }
            QMessageBox QPushButton {
                background: #1d4ed8;
                color: white;
                border: none;
                padding: 8px 14px;
                border-radius: 8px;
                min-width: 80px;
            }
            QMessageBox QPushButton:hover { background: #2563eb; }
            """
        )
        return box.exec()

    def _export_finished(self) -> None:
        self.status.setText("Export completed")
        self.tracks.clear()
        self.refresh_table()
        self.preview.setText("Cover preview")
        self.preview.setPixmap(QPixmap())
        self.overall_status.setText("Overall progress: 0/0 exported")
        self.file_progress.setValue(100)
        self.file_status.setText("Current file: —")

    def _update_overall_progress(self, value: int) -> None:
        total = len(self.tracks)
        exported = 0 if total == 0 else round((value / 100) * total)
        self.overall_status.setText(f"Overall progress: {exported}/{total} exported")

    def _show_error(self, message: str) -> None:
        if message == "Export canceled.":
            self.status.setText(message)
            return
        QMessageBox.critical(self, "Error", message)
        self.status.setText("Export failed")


def main() -> None:
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "mp3tovideo.svg")))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
