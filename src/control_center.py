import os
import sys
import cv2
import time
import threading
import subprocess

os.environ["QT_LOGGING_RULES"] = "qt.pointer.dispatch=false"

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
GESTURE_SRC_DIR = os.path.abspath(
    os.path.join(CURRENT_DIR, "..", "..", "demo-gesture-remote-control", "src")
)
if GESTURE_SRC_DIR not in sys.path:
    sys.path.append(GESTURE_SRC_DIR)

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QFileDialog,
    QMessageBox,
    QGroupBox,
    QCheckBox,
    QGridLayout,
    QSlider,
    QSizePolicy,
    QSplitter,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QPropertyAnimation
from PySide6.QtGui import QImage, QPixmap, QKeyEvent, QMouseEvent

from eye_detector import MediaPipeEyeDetector
from gesture_recognizer import MediaPipeGestureRecognizer
from video_player import VideoPlayerThread
from log import error


class HybridCaptureThread(QThread):
    frame_ready = Signal(object)
    detection_status = Signal(dict)
    fps_updated = Signal(float)
    command_detected = Signal(str)
    finished = Signal()

    def __init__(self, mode="gesture"):
        super().__init__()
        self.cap = None
        self.running = False
        self.detecting = True
        self.show_landmarks = True
        self.exiting = False
        self._closed = True
        self._lock = threading.RLock()
        self._detect_lock = threading.Lock()  # guards detector lifecycle vs usage
        self.camera_id = None
        self.reconnect_interval = 2.0
        self._last_reconnect_attempt = 0.0

        self.mode = mode
        self.gesture_detector = None
        self.eye_detector = None

        self.frame_count = 0
        self.fps = 0.0
        self.last_fps_time = time.time()
        self.last_command = None
        self.last_command_time = 0.0
        self.last_face_detected_time = time.time()

        self.command_repeat_interval = 0.35
        self.command_repeat_intervals = {
            "seek_forward": 0.45,
            "seek_back": 0.45,
            "play": 0.50,
            "pause": 0.50,
            "toggle": 0.50,
            "vol_up": 0.45,
            "vol_down": 0.45,
        }
        self.gesture_proc_width = 640
        self.eye_proc_width = 960
        self.gesture_detection_fps = 15.0
        self._last_detect_time = 0.0
        # HUD state for gesture mode (mirrors gesture VideoCaptureThread)
        self.hud_frame_remain = 0
        self.hud_command_remain = None

    def _ensure_backend(self, mode):
        if mode == "gesture" and self.gesture_detector is None:
            self.gesture_detector = MediaPipeGestureRecognizer()
        if mode == "eye" and self.eye_detector is None:
            self.eye_detector = MediaPipeEyeDetector()

    def _close_gesture_backend(self):
        with self._detect_lock:
            if self.gesture_detector is not None:
                try:
                    self.gesture_detector.close()
                except Exception as exc:
                    error(f"Error closing gesture detector: {exc}")
                finally:
                    self.gesture_detector = None

    def _close_eye_backend(self):
        with self._detect_lock:
            if self.eye_detector is not None:
                try:
                    self.eye_detector.close()
                except Exception as exc:
                    error(f"Error closing eye detector: {exc}")
                finally:
                    self.eye_detector = None

    def close_backends(self):
        self._close_gesture_backend()
        self._close_eye_backend()

    def set_mode(self, mode):
        with self._lock:
            if mode not in ("gesture", "eye"):
                return
            if self.mode == mode:
                return
            self.mode = mode
            self.last_command = None
            self.last_command_time = 0.0
            self.last_face_detected_time = time.time()
            self._last_detect_time = 0.0

        if mode == "gesture":
            self._close_eye_backend()
        else:
            self._close_gesture_backend()

    def find_available_camera(self):
        for index in range(10):
            temp_cap = None
            try:
                temp_cap = cv2.VideoCapture(index)
                if temp_cap is not None and temp_cap.isOpened():
                    ret, frame = temp_cap.read()
                    if ret and frame is not None:
                        temp_cap.release()
                        return index
            except Exception as exc:
                error(f"Error checking camera {index}: {exc}")
            finally:
                if temp_cap is not None:
                    try:
                        temp_cap.release()
                    except Exception:
                        pass
        return None

    def start_capture(self, camera_id=None):
        if camera_id is None:
            camera_id = self.find_available_camera()

        self._release_capture_only()

        with self._lock:
            self.camera_id = camera_id
            self.exiting = False
            self.running = True
            self.frame_count = 0
            self.fps = 0.0
            self.last_fps_time = time.time()
            self._last_detect_time = 0.0

        if camera_id is not None:
            self._open_capture(camera_id)

        if not self.isRunning():
            self.start()

    def _open_capture(self, camera_id):
        cap = cv2.VideoCapture(camera_id)
        if not (cap and cap.isOpened()):
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            return False

        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            cap.set(cv2.CAP_PROP_FPS, 30)
        except Exception:
            pass

        with self._lock:
            self._release_capture_only()
            self.cap = cap
            self._closed = False
            self.camera_id = camera_id
        return True

    def _release_capture_only(self):
        with self._lock:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception as exc:
                    error(f"Error releasing camera capture: {exc}")
                finally:
                    self.cap = None
                    self._closed = True
            else:
                self._closed = True

    def stop_capture(self):
        with self._lock:
            self.running = False
            self.exiting = True

        if self.isRunning():
            self.wait(2000)

        self._release_capture_only()

    def shutdown(self):
        self.stop_capture()
        self.close_backends()

    def toggle_detection(self, detecting):
        with self._lock:
            self.detecting = detecting

    def toggle_landmarks(self, show):
        with self._lock:
            self.show_landmarks = show

    def _emit_command_if_needed(self, mode, command):
        if not command:
            if mode == "gesture":
                with self._lock:
                    self.last_command = None
            return

        now_time = time.time()
        if mode == "eye":
            if command != self.last_command:
                self.command_detected.emit(command)
                with self._lock:
                    self.last_command = command
                    self.last_command_time = now_time
            return

        repeat_interval = self.command_repeat_intervals.get(command, self.command_repeat_interval)
        if command != self.last_command or (now_time - self.last_command_time) >= repeat_interval:
            self.command_detected.emit(command)
            with self._lock:
                self.last_command = command
                self.last_command_time = now_time

    def _prepare_frame(self, frame, proc_width):
        height, width = frame.shape[:2]
        if width <= proc_width:
            return frame

        scale = proc_width / float(width)
        new_height = max(1, int(height * scale))
        try:
            return cv2.resize(frame, (proc_width, new_height), interpolation=cv2.INTER_LINEAR)
        except Exception:
            return frame

    def _process_gesture_frame(self, frame, show_landmarks):
        processed_frame = self._prepare_frame(frame, self.gesture_proc_width)
        detection_result = {}
        mp_result = None
        command = None

        with self._detect_lock:
            self._ensure_backend("gesture")
            detector = self.gesture_detector
            if detector is None:
                return cv2.flip(processed_frame, 1), {}, None
            try:
                result = detector.process_frame(processed_frame)
                if isinstance(result, tuple) and len(result) >= 1:
                    detection_result = result[0] or {}
                    mp_result = result[1] if len(result) > 1 else None
                elif isinstance(result, dict):
                    detection_result = result or {}
                if show_landmarks and mp_result is not None:
                    detector.draw_landmarks(processed_frame, mp_result)
                command = detection_result.get("cmd")
            except Exception as exc:
                error(f"Gesture process_frame error (reinit next frame): {exc}")
                self.gesture_detector = None

        display_frame = cv2.flip(processed_frame, 1)

        # Draw HUD text on display frame (mirrors gesture VideoCaptureThread)
        _cmd_labels = {
            'play': 'Play', 'pause': 'Pause', 'toggle': 'Play/Pause',
            'seek_forward': 'Seek +5s', 'seek_back': 'Seek -5s',
            'vol_up': 'Volume +5%', 'vol_down': 'Volume -5%',
        }
        if command is None:
            if self.hud_frame_remain > 0:
                self.hud_frame_remain -= 1
                hud_cmd_label = _cmd_labels.get(self.hud_command_remain, 'Waiting')
                hud_state = 'Engaged'
            else:
                hud_cmd_label = 'Waiting'
                hud_state = 'Disengaged'
                self.hud_command_remain = None
        else:
            self.hud_frame_remain = 10
            self.hud_command_remain = command
            hud_cmd_label = _cmd_labels.get(command, command)
            hud_state = 'Engaged'
        hud_text = f" {hud_state} | {hud_cmd_label}"
        font = cv2.FONT_HERSHEY_SIMPLEX
        (tw, th), bl = cv2.getTextSize(hud_text, font, 0.7, 2)
        cv2.rectangle(display_frame, (8, 8), (tw + 16, th + bl + 14), (0, 0, 0), -1)
        cv2.putText(display_frame, hud_text, (12, th + 12), font, 0.7, (0, 230, 118), 2, cv2.LINE_AA)

        return display_frame, detection_result, command

    def _process_eye_frame(self, frame, show_landmarks):
        processed_frame = self._prepare_frame(frame, self.eye_proc_width)
        detection_result = {}
        command = None

        with self._detect_lock:
            self._ensure_backend("eye")
            detector = self.eye_detector
            if detector is None:
                display_frame = cv2.flip(processed_frame, 1)
                return display_frame, {}, None
            try:
                detection_result = detector.detect_eyes_state(processed_frame)
                face_detected = detection_result.get("face_detected", False)
                current_time = time.time()
                if face_detected:
                    with self._lock:
                        self.last_face_detected_time = current_time
                    eyes_closed = detection_result.get("eyes_closed", False)
                    is_gazing = detection_result.get("is_gazing", False)
                    command = "pause" if eyes_closed or not is_gazing else "play"
                else:
                    last_face_time = self.last_face_detected_time
                    if time.time() - last_face_time > 0.5:
                        command = "pause"
            except Exception as exc:
                error(f"Eye detect_eyes_state error (reinit next frame): {exc}")
                self.eye_detector = None

        display_frame = cv2.flip(processed_frame, 1)
        if show_landmarks and detection_result.get("face_detected", False) and detector is not None:
            flipped_result = detection_result.copy()
            eye_center = detection_result.get("eye_center")
            if eye_center is not None:
                flipped_result["eye_center"] = (display_frame.shape[1] - 1 - eye_center[0], eye_center[1])
            detector.draw_landmarks(display_frame, flipped_result)
        return display_frame, detection_result, command

    def run(self):
        read_failures = 0
        while True:
            with self._lock:
                should_continue = self.running and not self.exiting
                cap_ready = self.cap is not None and not self._closed
                camera_id = self.camera_id
                mode = self.mode
                detecting_enabled = self.detecting
                show_landmarks = self.show_landmarks

            if not should_continue:
                break

            if not cap_ready:
                current_time = time.time()
                if current_time - self._last_reconnect_attempt >= self.reconnect_interval:
                    self._last_reconnect_attempt = current_time
                    next_camera_id = camera_id if camera_id is not None else self.find_available_camera()
                    if next_camera_id is not None:
                        self._open_capture(next_camera_id)
                time.sleep(0.1)
                continue

            ret = False
            frame = None
            try:
                ret, frame = self.cap.read()
            except Exception as exc:
                error(f"Error reading frame: {exc}")

            if not ret or frame is None:
                read_failures += 1
                if read_failures >= 30:
                    self._release_capture_only()
                    read_failures = 0
                time.sleep(0.03)
                continue
            read_failures = 0

            self.frame_count += 1
            now = time.time()
            if now - self.last_fps_time >= 1.0:
                self.fps = self.frame_count / (now - self.last_fps_time)
                self.frame_count = 0
                self.last_fps_time = now
                self.fps_updated.emit(self.fps)

            display_frame = frame.copy()
            detection_result = {}
            command = None

            if detecting_enabled:
                try:
                    if mode == "gesture":
                        if now - self._last_detect_time >= (1.0 / self.gesture_detection_fps):
                            self._last_detect_time = now
                            display_frame, detection_result, command = self._process_gesture_frame(frame, show_landmarks)
                            self.detection_status.emit(detection_result or {})
                            self._emit_command_if_needed(mode, command)
                        else:
                            display_frame = cv2.flip(self._prepare_frame(frame, self.gesture_proc_width), 1)
                    else:
                        display_frame, detection_result, command = self._process_eye_frame(frame, show_landmarks)
                        self.detection_status.emit(detection_result or {})
                        self._emit_command_if_needed(mode, command)
                except Exception as exc:
                    error(f"Detection error: {exc}")
                    self.detection_status.emit({})
            else:
                if mode == "gesture":
                    display_frame = cv2.flip(self._prepare_frame(frame, self.gesture_proc_width), 1)
                else:
                    display_frame = self._prepare_frame(frame, self.eye_proc_width)
                self.detection_status.emit({})

            self.frame_ready.emit(display_frame)
            time.sleep(0.01)

        self._release_capture_only()
        self.finished.emit()


class MultiModeFullScreenPlayer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.is_slider_pressed = False
        self.setup_ui()
        self.setup_style()
        self.frame_remain = 0
        self.last_command = ""

    def tr(self, en_text, zh_text):
        is_en = True
        if self.parent_window and hasattr(self.parent_window, "current_language"):
            is_en = self.parent_window.current_language == "en"
        return en_text if is_en else zh_text

    def setup_ui(self):
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.video_label = QLabel(self.tr("Loading video...", "正在加载视频..."))
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            "QLabel { background-color: #000000; color: #ffffff; font-size: 24px; font-weight: bold; }"
        )

        self.detection_overlay = QLabel(self.video_label)
        self.detection_overlay.setAlignment(Qt.AlignCenter)
        self.detection_overlay.setStyleSheet(
            "QLabel { color: #f8fafc; font-size: 24px; font-weight: bold; background-color: rgba(15, 23, 42, 200); border-radius: 10px; padding: 10px; }"
        )
        self.detection_overlay.hide()

        self.playback_status_overlay = QLabel(self.video_label)
        self.playback_status_overlay.setAlignment(Qt.AlignCenter)
        self.playback_status_overlay.setStyleSheet(
            "QLabel { color: #86efac; font-size: 24px; font-weight: bold; background-color: rgba(15, 23, 42, 200); border-radius: 10px; padding: 10px; }"
        )
        self.playback_status_overlay.hide()

        self.status_overlay = QLabel(self.video_label)
        self.status_overlay.setAlignment(Qt.AlignCenter)
        self.status_overlay.setStyleSheet(
            "QLabel { color: #fde68a; font-size: 20px; background-color: rgba(15, 23, 42, 200); border-radius: 10px; padding: 10px; }"
        )
        self.status_overlay.hide()

        self.control_bar = QWidget()
        self.control_bar.setFixedHeight(82)
        control_layout = QHBoxLayout(self.control_bar)
        control_layout.setContentsMargins(20, 0, 20, 18)
        control_layout.setSpacing(14)

        self.back_btn = QPushButton(self.tr("Back", "返回"))
        self.back_btn.setFixedSize(100, 40)
        self.back_btn.clicked.connect(self.exit_fullscreen)

        self.play_pause_btn = QPushButton(self.tr("Pause", "暂停"))
        self.play_pause_btn.setFixedSize(100, 40)
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)

        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.sliderMoved.connect(self.on_progress_slider_moved)
        self.progress_slider.sliderPressed.connect(self.on_progress_slider_pressed)
        self.progress_slider.sliderReleased.connect(self.on_progress_slider_released)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: #ffffff; font-size: 14px;")

        self.status_label = QLabel(self.tr("Detecting...", "检测中..."))
        self.status_label.setStyleSheet(
            "QLabel { color: #e2e8f0; font-size: 14px; padding: 5px 10px; background-color: rgba(15, 23, 42, 170); border-radius: 5px; }"
        )

        control_layout.addWidget(self.back_btn)
        control_layout.addWidget(self.play_pause_btn)
        control_layout.addWidget(self.progress_slider, 1)
        control_layout.addWidget(self.time_label)
        control_layout.addWidget(self.status_label)

        main_layout.addWidget(self.video_label, 1)
        main_layout.addWidget(self.control_bar)

        self.mouse_timer = QTimer()
        self.mouse_timer.timeout.connect(self.hide_controls)
        self.mouse_timer.setSingleShot(True)

        self.control_animation = QPropertyAnimation(self.control_bar, b"windowOpacity")
        self.control_animation.setDuration(300)
        self._hiding_controls = False
        self.control_animation.finished.connect(self._on_control_animation_finished)

        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.hide_status)
        self.status_timer.setSingleShot(True)

        self.overlay_timer = QTimer()
        self.overlay_timer.timeout.connect(self.hide_overlays)
        self.overlay_timer.setSingleShot(True)

    def setup_style(self):
        self.setStyleSheet(
            """
            QWidget {
                background-color: #000000;
            }
            QPushButton {
                background-color: rgba(255, 255, 255, 28);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 52);
                border-radius: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 40);
            }
            QSlider::groove:horizontal {
                border: 1px solid rgba(255, 255, 255, 50);
                height: 6px;
                background: rgba(255, 255, 255, 20);
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                width: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSlider::sub-page:horizontal {
                background: #38bdf8;
                border-radius: 3px;
            }
            """
        )

    def showEvent(self, event):
        super().showEvent(event)
        self.back_btn.setText(self.tr("Back", "返回"))
        if self.parent_window and self.parent_window.video_player_thread.playing and not self.parent_window.video_player_thread.paused:
            self.play_pause_btn.setText(self.tr("Pause", "暂停"))
        else:
            self.play_pause_btn.setText(self.tr("Play", "播放"))
        self.showFullScreen()
        self._hiding_controls = False
        self.control_animation.stop()
        self.control_bar.show()
        self.control_bar.setWindowOpacity(1)
        self.adjust_overlay_positions()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.exit_fullscreen()
        elif event.key() == Qt.Key_Space:
            self.toggle_play_pause()
        elif event.key() == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        super().mouseMoveEvent(event)
        self.show_controls()

    def mousePressEvent(self, event: QMouseEvent):
        super().mousePressEvent(event)
        self.show_controls()

    def show_controls(self):
        self._hiding_controls = False
        self.control_animation.stop()
        if not self.control_bar.isVisible():
            self.control_bar.show()
            self.control_animation.setStartValue(0)
            self.control_animation.setEndValue(1)
            self.control_animation.start()
        else:
            self.control_bar.setWindowOpacity(1)
        self.mouse_timer.stop()

    def hide_controls(self):
        return

    def _on_control_animation_finished(self):
        if self._hiding_controls:
            self.control_bar.hide()

    def show_status(self, message, duration=2000):
        self.status_label.setText(message)
        self.status_label.show()
        self.status_timer.stop()
        self.status_timer.start(duration)

    def hide_status(self):
        self.status_label.hide()

    def show_overlays(self, detection_text="", playback_text="", status_text=""):
        if detection_text:
            self.detection_overlay.setText(detection_text)
            self.detection_overlay.adjustSize()
            self.detection_overlay.show()

        if playback_text:
            self.playback_status_overlay.setText(playback_text)
            self.playback_status_overlay.adjustSize()
            self.playback_status_overlay.show()

        if status_text:
            self.status_overlay.setText(status_text)
            self.status_overlay.adjustSize()
            self.status_overlay.show()

        self.adjust_overlay_positions()
        self.overlay_timer.stop()
        self.overlay_timer.start(2000)

    def hide_overlays(self):
        self.detection_overlay.hide()
        self.playback_status_overlay.hide()
        self.status_overlay.hide()

    def update_detection_status(self, detection_result):
        if not self.parent_window:
            return

        if self.parent_window.current_mode == "gesture":
            gesture_cmd = None
            if detection_result:
                hand_present = detection_result.get("hand_present", False)
                gesture_cmd = detection_result.get("cmd")
                playback_text = self.tr("Gesture Active", "手势激活") if hand_present else self.tr("No Hand", "未检测到手")
            else:
                playback_text = self.tr("Detection Inactive", "检测未激活")

            if gesture_cmd is None:
                gesture_cmd = self.tr("Waiting", "等待中")
                if self.frame_remain >= 0:
                    self.frame_remain -= 1
                    gesture_cmd = self.last_command or gesture_cmd
            else:
                self.frame_remain = 5
                self.last_command = self.parent_window.command_display_text(gesture_cmd)
                gesture_cmd = self.last_command
                mode_text, _ = self.parent_window.control_mode_text(detection_result.get("cmd"))
                playback_text = mode_text

            self.show_overlays(detection_text=gesture_cmd, playback_text=playback_text)
            return

        if detection_result and detection_result.get("face_detected", False):
            eyes_closed = detection_result.get("eyes_closed", False)
            is_gazing = detection_result.get("is_gazing", False)
            eye_text = self.tr("Eyes Closed", "闭眼") if eyes_closed else self.tr("Eyes Open", "睁眼")
            gaze_text = self.tr("Gazing", "注视中") if is_gazing else self.tr("Not Gazing", "未注视")
            playback_text = self.tr("Paused", "已暂停") if eyes_closed or not is_gazing else self.tr("Playing", "播放中")
            self.show_overlays(detection_text=eye_text, playback_text=gaze_text, status_text=playback_text)
        else:
            self.show_overlays(
                detection_text=self.tr("No Face Detected", "未检测到人脸"),
                playback_text=self.tr("Paused", "已暂停"),
            )

    def exit_fullscreen(self):
        self.close()
        if self.parent_window:
            self.parent_window.is_in_fullscreen_mode = False
            self.parent_window.showNormal()
            self.parent_window.show()

    def closeEvent(self, event):
        if self.parent_window:
            self.parent_window.is_in_fullscreen_mode = False
            self.parent_window.is_fullscreen = False
            self.parent_window.fullscreen_btn.setText(self.parent_window.tr("Fullscreen", "全屏"))
        super().closeEvent(event)

    def toggle_play_pause(self):
        if not self.parent_window:
            return
        if self.parent_window.video_player_thread.playing and not self.parent_window.video_player_thread.paused:
            self.parent_window.pause_video()
            self.play_pause_btn.setText(self.tr("Play", "播放"))
        else:
            self.parent_window.play_video()
            self.play_pause_btn.setText(self.tr("Pause", "暂停"))

    def update_video_frame(self, frame):
        if self.parent_window:
            self.parent_window.display_frame(self.video_label, frame)

    def update_progress(self, position, duration):
        if not self.is_slider_pressed and not self.progress_slider.isSliderDown():
            self.progress_slider.setValue(int(position * 1000))

        current_time = position * duration
        current_str = f"{int(current_time // 60):02d}:{int(current_time % 60):02d}"
        total_str = f"{int(duration // 60):02d}:{int(duration % 60):02d}"
        self.time_label.setText(f"{current_str} / {total_str}")

    def on_progress_slider_pressed(self):
        self.is_slider_pressed = True

    def on_progress_slider_moved(self, value):
        if self.parent_window and self.parent_window.video_loaded and self.is_slider_pressed:
            position = value / 1000.0
            duration = self.parent_window.video_duration
            self.parent_window.progress_slider.setValue(value)
            self.parent_window.update_time_label(position * duration, duration)
            self.update_progress(position, duration)

    def on_progress_slider_released(self):
        if self.parent_window and self.parent_window.video_loaded:
            position = self.progress_slider.value() / 1000.0
            duration = self.parent_window.video_duration
            self.parent_window.video_player_thread.seek(
                int(position * self.parent_window.video_player_thread.total_frames)
            )
            self.parent_window.progress_slider.setValue(self.progress_slider.value())
            self.parent_window.update_time_label(position * duration, duration)
            self.update_progress(position, duration)
        self.is_slider_pressed = False

    def adjust_overlay_positions(self):
        video_rect = self.video_label.rect()

        if self.detection_overlay.isVisible():
            self.detection_overlay.adjustSize()
            size = self.detection_overlay.sizeHint()
            self.detection_overlay.setGeometry(20, 20, size.width(), size.height())

        if self.playback_status_overlay.isVisible():
            self.playback_status_overlay.adjustSize()
            size = self.playback_status_overlay.sizeHint()
            self.playback_status_overlay.setGeometry(
                video_rect.width() - size.width() - 20,
                20,
                size.width(),
                size.height(),
            )

        if self.status_overlay.isVisible():
            self.status_overlay.adjustSize()
            size = self.status_overlay.sizeHint()
            self.status_overlay.setGeometry(
                (video_rect.width() - size.width()) // 2,
                video_rect.height() - size.height() - 20,
                size.width(),
                size.height(),
            )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_language = "en"
        self.current_mode = "gesture"

        self.video_player_thread = VideoPlayerThread()
        self.capture_thread = HybridCaptureThread(mode=self.current_mode)

        self.current_video_file = ""
        self.video_loaded = False
        self.camera_active = False
        self.is_fullscreen = False
        self.is_in_fullscreen_mode = False
        self.fullscreen_player = None
        self.video_duration = 0.0
        self.latest_video_frame = None
        self.is_slider_pressed = False
        self.last_control_command = None
        self.last_action_hold_until_ms = 0
        self.gesture_display_hold_ms = 700

        self.capture_thread.frame_ready.connect(self.update_camera_frame)
        self.capture_thread.command_detected.connect(self.handle_command)
        self.capture_thread.detection_status.connect(self.update_detection_status)
        self.capture_thread.fps_updated.connect(self.update_fps_display)
        self.capture_thread.finished.connect(self.on_camera_stopped)

        self.video_player_thread.frame_ready.connect(self.update_video_frame)
        self.video_player_thread.playback_finished.connect(self.on_playback_finished)
        self.video_player_thread.video_info_ready.connect(self.update_video_info)

        self.setup_styles()
        self.init_ui()
        self.apply_mode_ui(reset_status=True)
        self.auto_start_camera()

        self.video_player_thread.start()

        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self.update_progress)
        self.progress_timer.start(100)

    def tr(self, en_text, zh_text):
        return en_text if self.current_language == "en" else zh_text

    def translate_known_text(self, text):
        known_pairs = [
            ("Starting camera...", "正在启动摄像头..."),
            ("Camera Stopped", "摄像头已关闭"),
            ("Click to select a video file", "点击选择视频文件"),
            ("Running", "运行中"),
            ("Failed to Start", "启动失败"),
            ("Stopped", "已停止"),
            ("Detecting", "检测中"),
            ("Disabled", "已禁用"),
            ("No Hand", "未检测到手"),
            ("Gesture Active", "手势激活"),
            ("Inactive", "未激活"),
            ("Waiting", "等待中"),
            ("Play", "播放"),
            ("Pause", "暂停"),
            ("Playing", "播放中"),
            ("Paused", "已暂停"),
            ("Not Loaded", "未加载"),
            ("Loaded", "已加载"),
            ("Load Failed", "加载失败"),
            ("Playback Completed", "播放完成"),
            ("Face Detected", "已检测到人脸"),
            ("Not Detected", "未检测"),
            ("Eyes Closed", "闭眼"),
            ("Eyes Open", "睁眼"),
            ("Gazing", "注视中"),
            ("Not Gazing", "未注视"),
            ("Playback Control", "播放控制"),
            ("Seek Control", "快进快退控制"),
            ("Volume Control", "音量控制"),
        ]
        for en_text, zh_text in known_pairs:
            if text == en_text or text == zh_text:
                return self.tr(en_text, zh_text)
        return text

    def setup_styles(self):
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #060b1a;
            }
            QWidget {
                color: #e2e8f0;
                font-family: "DejaVu Sans";
            }
            QGroupBox {
                color: #f8fafc;
                font-weight: 600;
                border: 1px solid #233152;
                border-radius: 16px;
                margin-top: 12px;
                padding-top: 14px;
                background-color: #0f172a;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 16px;
                padding: 0 8px;
            }
            QLabel#muted {
                color: #94a3b8;
            }
            QLabel#status_value {
                font-weight: 700;
                padding: 4px 10px;
                border-radius: 10px;
                background-color: #1e293b;
            }
            QPushButton {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 12px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #273449;
            }
            QPushButton:pressed {
                background-color: #111827;
            }
            QPushButton:checked {
                background-color: #2563eb;
                border-color: #60a5fa;
                color: #eff6ff;
            }
            QCheckBox {
                color: #cbd5e1;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 1px solid #475569;
                background-color: #0f172a;
            }
            QCheckBox::indicator:checked {
                background-color: #38bdf8;
                border-color: #38bdf8;
            }
            QSlider::groove:horizontal {
                border: 1px solid #334155;
                height: 8px;
                background: #0f172a;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #38bdf8;
                border: 1px solid #0ea5e9;
                width: 18px;
                margin: -6px 0;
                border-radius: 9px;
            }
            QSlider::sub-page:horizontal {
                background: #2563eb;
                border-radius: 4px;
            }
            QSplitter::handle {
                background-color: #0f172a;
                width: 8px;
            }
            """
        )

    def init_ui(self):
        self.setWindowTitle("Remote Control Hub")
        screen = QApplication.primaryScreen()
        geometry = screen.availableGeometry()
        width = int(geometry.width() * 0.9)
        height = int(geometry.height() * 0.9)
        self.setGeometry(
            (geometry.width() - width) // 2,
            (geometry.height() - height) // 2,
            width,
            height,
        )

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(16)

        self.sidebar = self.build_sidebar()
        root_layout.addWidget(self.sidebar, 0)

        self.content_widget = QWidget()
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)

        header = self.build_header()
        content_layout.addWidget(header)

        panels_widget = QWidget()
        panels_layout = QHBoxLayout(panels_widget)
        panels_layout.setContentsMargins(0, 0, 0, 0)
        panels_layout.setSpacing(14)
        control_panel = self.build_control_panel()
        control_panel.setFixedWidth(550)
        panels_layout.addWidget(self.build_display_panel(), 1)
        panels_layout.addWidget(control_panel, 0)
        content_layout.addWidget(panels_widget, 1)

        root_layout.addWidget(self.content_widget, 1)

        self.fullscreen_btn.setShortcut("F11")
        self.apply_language()

    def build_sidebar(self):
        sidebar = QGroupBox()
        sidebar.setFixedWidth(280)
        sidebar.setStyleSheet(
            "QGroupBox { background-color: #091122; border: 1px solid #1d4ed8; border-radius: 22px; margin-top: 0px; }"
        )

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(16)

        title = QLabel("AI Remote Hub")
        title.setStyleSheet("font-size: 22px; font-weight: 800; color: #f8fafc;")
        subtitle = QLabel("Control Modules")
        subtitle.setObjectName("muted")

        self.module_counter = QLabel("01 / 02")
        self.module_counter.setStyleSheet("font-size: 16px; font-weight: 700; color: #dbeafe;")

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.module_counter)

        self.gesture_mode_btn = QPushButton("01\nGesture Remote control")
        self.gesture_mode_btn.setCheckable(True)
        self.gesture_mode_btn.setCursor(Qt.PointingHandCursor)
        self.gesture_mode_btn.setMinimumHeight(96)
        self.gesture_mode_btn.clicked.connect(lambda: self.switch_mode("gesture"))

        self.eye_mode_btn = QPushButton("02\nEye Remote control")
        self.eye_mode_btn.setCheckable(True)
        self.eye_mode_btn.setCursor(Qt.PointingHandCursor)
        self.eye_mode_btn.setMinimumHeight(96)
        self.eye_mode_btn.clicked.connect(lambda: self.switch_mode("eye"))

        note = QLabel(self.tr("Default loads gesture mode.", "默认加载手势模式。"))
        note.setWordWrap(True)
        note.setObjectName("muted")

        layout.addWidget(self.gesture_mode_btn)
        layout.addWidget(self.eye_mode_btn)
        layout.addWidget(note)
        layout.addStretch()
        return sidebar

    def build_header(self):
        header = QGroupBox()
        header.setStyleSheet(
            "QGroupBox { background-color: #0b1328; border: 1px solid #233152; border-radius: 22px; margin-top: 0px; }"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(12)

        title_block = QWidget()
        title_layout = QVBoxLayout(title_block)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(4)

        self.header_title = QLabel()
        self.header_title.setStyleSheet("font-size: 22px; font-weight: 800; color: #f8fafc;")
        self.header_subtitle = QLabel()
        self.header_subtitle.setObjectName("muted")
        self.header_subtitle.setWordWrap(True)
        self.header_subtitle.setMaximumHeight(0)  # hidden to save vertical space

        title_layout.addWidget(self.header_title)
        title_layout.addWidget(self.header_subtitle)

        layout.addWidget(title_block, 1)

        self.mode_badge = QLabel()
        self.mode_badge.setStyleSheet(
            "background-color: #172554; color: #bfdbfe; padding: 8px 14px; border-radius: 16px; font-weight: 700;"
        )

        self.language_btn = QPushButton("中文")
        self.language_btn.setFixedHeight(38)
        self.language_btn.clicked.connect(self.toggle_language)

        self.fullscreen_play_btn = QPushButton()
        self.fullscreen_play_btn.setFixedHeight(38)
        self.fullscreen_play_btn.clicked.connect(self.enter_fullscreen_play_mode)

        self.fullscreen_btn = QPushButton()
        self.fullscreen_btn.setFixedHeight(38)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)

        layout.addWidget(self.mode_badge)
        layout.addWidget(self.language_btn)
        layout.addWidget(self.fullscreen_play_btn)
        layout.addWidget(self.fullscreen_btn)
        return header

    def build_display_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.camera_group = QGroupBox()
        camera_layout = QVBoxLayout(self.camera_group)
        self.camera_display = QLabel(self.tr("Starting camera...", "正在启动摄像头..."))
        self.camera_display.setAlignment(Qt.AlignCenter)
        self.camera_display.setScaledContents(True)
        self.camera_display.setMinimumHeight(320)
        self.camera_display.setStyleSheet(
            "QLabel { background-color: #020617; border-radius: 16px; border: 1px solid #233152; color: #ffffff; font-size: 15px; }"
        )
        camera_layout.addWidget(self.camera_display)

        self.video_group = QGroupBox()
        video_layout = QVBoxLayout(self.video_group)

        self.video_display = QLabel(self.tr("Click to select a video file", "点击选择视频文件"))
        self.video_display.setAlignment(Qt.AlignCenter)
        self.video_display.setScaledContents(True)
        self.video_display.setMinimumHeight(320)
        self.video_display.setCursor(Qt.PointingHandCursor)
        self.video_display.setStyleSheet(
            "QLabel { background-color: #020617; border-radius: 16px; border: 1px solid #233152; color: #ffffff; font-size: 15px; }"
        )
        self.video_display.mousePressEvent = self.open_video_from_display

        video_controls = QWidget()
        controls_layout = QVBoxLayout(video_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)

        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.sliderMoved.connect(self.on_progress_slider_moved)
        self.progress_slider.sliderPressed.connect(self.on_progress_slider_pressed)
        self.progress_slider.sliderReleased.connect(self.on_progress_slider_released)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("muted")

        self.video_play_btn = QPushButton()
        self.video_play_btn.clicked.connect(self.play_video)
        self.video_pause_btn = QPushButton()
        self.video_pause_btn.clicked.connect(self.pause_video)
        self.video_stop_btn = QPushButton()
        self.video_stop_btn.clicked.connect(self.stop_video)

        row_layout.addWidget(self.time_label)
        row_layout.addStretch()
        row_layout.addWidget(self.video_play_btn)
        row_layout.addWidget(self.video_pause_btn)
        row_layout.addWidget(self.video_stop_btn)

        controls_layout.addWidget(self.progress_slider)
        controls_layout.addWidget(row)

        video_layout.addWidget(self.video_display)
        video_layout.addWidget(video_controls)

        layout.addWidget(self.camera_group)
        layout.addWidget(self.video_group)
        return panel

    def build_control_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.status_group = QGroupBox()
        status_layout = QGridLayout(self.status_group)
        status_layout.setHorizontalSpacing(12)
        status_layout.setVerticalSpacing(10)

        def _make_badge():
            lbl = QLabel()
            lbl.setObjectName("status_value")
            lbl.setFixedHeight(28)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            return lbl

        self.cam_status_label = QLabel()
        self.cam_status_label.setObjectName("muted")
        self.cam_status = _make_badge()

        self.fps_label = QLabel()
        self.fps_label.setObjectName("muted")
        self.fps_display = _make_badge()
        self.fps_display.setText("0.0")

        self.detect_status_label = QLabel()
        self.detect_status_label.setObjectName("muted")
        self.detect_status = _make_badge()

        self.video_status_label = QLabel()
        self.video_status_label.setObjectName("muted")
        self.video_status = _make_badge()

        self.aux_status_label_1 = QLabel()
        self.aux_status_label_1.setObjectName("muted")
        self.aux_status_value_1 = _make_badge()

        self.aux_status_label_2 = QLabel()
        self.aux_status_label_2.setObjectName("muted")
        self.aux_status_value_2 = _make_badge()

        status_layout.addWidget(self.cam_status_label, 0, 0)
        status_layout.addWidget(self.cam_status, 0, 1)
        status_layout.addWidget(self.fps_label, 0, 2)
        status_layout.addWidget(self.fps_display, 0, 3)
        status_layout.addWidget(self.detect_status_label, 1, 0)
        status_layout.addWidget(self.detect_status, 1, 1)
        status_layout.addWidget(self.video_status_label, 1, 2)
        status_layout.addWidget(self.video_status, 1, 3)
        status_layout.addWidget(self.aux_status_label_1, 2, 0)
        status_layout.addWidget(self.aux_status_value_1, 2, 1)
        status_layout.addWidget(self.aux_status_label_2, 2, 2)
        status_layout.addWidget(self.aux_status_value_2, 2, 3)
        status_layout.setColumnStretch(1, 1)
        status_layout.setColumnStretch(3, 1)

        self.instruction_group = QGroupBox()
        instruction_layout = QVBoxLayout(self.instruction_group)
        self.instructions_label = QLabel()
        self.instructions_label.setWordWrap(True)
        self.instructions_label.setStyleSheet("color: #cbd5e1; line-height: 1.6; padding: 4px;")
        instruction_layout.addWidget(self.instructions_label)

        self.camera_control_group = QGroupBox()
        camera_control_layout = QVBoxLayout(self.camera_control_group)
        camera_control_layout.setSpacing(8)

        self.camera_toggle_btn = QPushButton()
        self.camera_toggle_btn.clicked.connect(self.toggle_camera)
        self.camera_toggle_btn.setFixedHeight(40)

        self.detect_checkbox = QCheckBox()
        self.detect_checkbox.setChecked(True)
        self.detect_checkbox.stateChanged.connect(self.toggle_detection)

        self.landmarks_checkbox = QCheckBox()
        self.landmarks_checkbox.setChecked(True)
        self.landmarks_checkbox.stateChanged.connect(self.toggle_landmarks)

        camera_control_layout.addWidget(self.camera_toggle_btn)
        camera_control_layout.addWidget(self.detect_checkbox)
        camera_control_layout.addWidget(self.landmarks_checkbox)

        self.file_control_group = QGroupBox()
        file_layout = QVBoxLayout(self.file_control_group)
        self.select_video_btn = QPushButton()
        self.select_video_btn.clicked.connect(self.select_video)
        self.select_video_btn.setFixedHeight(42)
        file_layout.addWidget(self.select_video_btn)

        layout.addWidget(self.status_group)
        layout.addWidget(self.instruction_group)
        layout.addWidget(self.camera_control_group)
        layout.addWidget(self.file_control_group)
        layout.addStretch()
        return panel

    def mode_name(self, mode=None):
        mode = mode or self.current_mode
        if mode == "gesture":
            return self.tr("Gesture Remote", "手势遥控")
        return self.tr("Eye Remote", "眼控遥控")

    def mode_header(self):
        if self.current_mode == "gesture":
            return (
                self.tr("Gesture And Eye Control Center", "手势与眼控控制中心"),
                self.tr(
                    "Gesture mode plays, pauses, fast-forwards, rewinds, and adjusts volume based on hand gestures.",
                    "手势模式会根据手势进行播放、暂停、快进、快退、调节音量。",
                ),
            )
        return (
            self.tr("Gesture And Eye Control Center", "手势与眼控控制中心"),
            self.tr(
                "Eye mode pauses or resumes playback according to eye state and gaze direction.",
                "眼控模式会根据眼睛状态和注视方向自动暂停或恢复播放。",
            ),
        )

    def gesture_instructions(self):
        return self.tr(
            "<b>Gesture Commands</b><br>"
            "• Open palm → Play<br>"
            "• Closed fist → Pause<br>"
            "• Swipe right → Fast forward 5 seconds<br>"
            "• Swipe left → Rewind 5 seconds<br>"
            "• Swipe up → Volume +5%<br>"
            "• Swipe down → Volume -5%<br><br>"
            "<b>Tips</b><br>"
            "• Keep your hand inside the camera frame<br>"
            "• Make gestures clearly and steadily<br>"
            "• Use adequate lighting",
            "<b>手势指令</b><br>"
            "• 张开手掌 → 播放<br>"
            "• 握拳 → 暂停<br>"
            "• 向右滑动 → 快进 5 秒<br>"
            "• 向左滑动 → 快退 5 秒<br>"
            "• 向上滑动 → 音量 +5%<br>"
            "• 向下滑动 → 音量 -5%<br><br>"
            "<b>提示</b><br>"
            "• 保持手在摄像头视野内<br>"
            "• 动作清晰稳定<br>"
            "• 确保环境光线充足",
        )

    def eye_instructions(self):
        return self.tr(
            "<b>Eye Tracking Rules</b><br>"
            "• Gazing at the screen keeps playback running<br>"
            "• Eyes closed pauses playback<br>"
            "• Looking away pauses playback<br>"
            "• Face lost for a short time also pauses playback<br><br>"
            "<b>Tips</b><br>"
            "• Keep your face inside the camera frame<br>"
            "• Avoid strong backlight<br>"
            "• Sit still for more stable gaze detection",
            "<b>眼控规则</b><br>"
            "• 注视屏幕时保持播放<br>"
            "• 闭眼时暂停播放<br>"
            "• 视线离开屏幕时暂停播放<br>"
            "• 短时间丢失人脸也会暂停播放<br><br>"
            "<b>提示</b><br>"
            "• 保持人脸位于摄像头范围内<br>"
            "• 避免强逆光<br>"
            "• 尽量保持头部稳定以获得更稳的注视判断",
        )

    def apply_language(self):
        title, subtitle = self.mode_header()
        self.setWindowTitle(self.tr("Remote Control Hub", "遥控控制中心"))
        self.header_title.setText(title)
        self.header_subtitle.setText(subtitle)
        self.mode_badge.setText(self.mode_name())
        self.language_btn.setText("中文" if self.current_language == "en" else "English")
        self.fullscreen_play_btn.setText(self.tr("Fullscreen Play", "全屏播放"))
        self.fullscreen_btn.setText(self.tr("Exit Fullscreen", "退出全屏") if self.is_fullscreen else self.tr("Fullscreen", "全屏"))

        self.camera_group.setTitle(self.tr("Camera Feed", "摄像头画面"))
        self.video_group.setTitle(self.tr("Video Player", "视频播放器"))
        self.status_group.setTitle(self.tr("System Status", "系统状态"))
        self.instruction_group.setTitle(self.tr("Control Instructions", "控制说明"))
        self.camera_control_group.setTitle(self.tr("Camera Controls", "摄像头控制"))
        self.file_control_group.setTitle(self.tr("Video File Controls", "视频文件控制"))

        self.cam_status_label.setText(self.tr("Camera:", "摄像头："))
        self.fps_label.setText(self.tr("FPS:", "帧率："))
        self.detect_status_label.setText(self.tr("Detection:", "检测："))
        self.video_status_label.setText(self.tr("Video:", "视频："))

        self.video_play_btn.setText(self.tr("Play", "播放"))
        self.video_pause_btn.setText(self.tr("Pause", "暂停"))
        self.video_stop_btn.setText(self.tr("Stop", "停止"))
        self.select_video_btn.setText(self.tr("Select Video File", "选择视频文件"))
        self.landmarks_checkbox.setText(self.tr("Show Landmarks", "显示关键点"))

        self.gesture_mode_btn.setText(
            self.tr(
                "01\nGesture Remote Control",
                "01\n手势遥控\n通过手势控制视频播放",
            )
        )
        self.eye_mode_btn.setText(
            self.tr(
                "02\nEye Remote Control",
                "02\n眼控遥控\n通过注视状态控制播放",
            )
        )

        self.camera_display.setText(self.translate_known_text(self.camera_display.text()))
        self.video_display.setText(self.translate_known_text(self.video_display.text()))
        self.cam_status.setText(self.translate_known_text(self.cam_status.text()))
        self.detect_status.setText(self.translate_known_text(self.detect_status.text()))
        self.video_status.setText(self.translate_known_text(self.video_status.text()))
        self.aux_status_value_1.setText(self.translate_known_text(self.aux_status_value_1.text()))
        self.aux_status_value_2.setText(self.translate_known_text(self.aux_status_value_2.text()))

        self.apply_mode_ui(reset_status=False)

        if self.fullscreen_player and self.is_in_fullscreen_mode:
            self.fullscreen_player.back_btn.setText(self.tr("Back", "返回"))
            if self.video_player_thread.playing and not self.video_player_thread.paused:
                self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))
            else:
                self.fullscreen_player.play_pause_btn.setText(self.tr("Play", "播放"))

    def apply_mode_ui(self, reset_status=True):
        title, subtitle = self.mode_header()
        self.header_title.setText(title)
        self.header_subtitle.setText(subtitle)
        self.mode_badge.setText(self.mode_name())

        is_gesture = self.current_mode == "gesture"
        self.module_counter.setText("01 / 02" if is_gesture else "02 / 02")
        self.gesture_mode_btn.setChecked(is_gesture)
        self.eye_mode_btn.setChecked(not is_gesture)

        if is_gesture:
            self.detect_checkbox.setText(self.tr("Enable Gesture Detection", "启用手势检测"))
            self.instructions_label.setText(self.gesture_instructions())
            self.aux_status_label_1.setText(self.tr("Mode:", "手势模式："))
            self.aux_status_label_2.setText(self.tr("Gesture:", "当前手势："))
            if reset_status:
                self._set_badge(self.detect_status, self.tr("Detecting", "检测中"), "#38bdf8")
                self._set_badge(self.aux_status_value_1, self.tr("Inactive", "未激活"), "#334155", "#f8fafc")
                self._set_badge(self.aux_status_value_2, self.tr("Waiting", "等待中"), "#334155", "#f8fafc")
        else:
            self.detect_checkbox.setText(self.tr("Enable Eye Detection", "启用眼部检测"))
            self.instructions_label.setText(self.eye_instructions())
            self.aux_status_label_1.setText(self.tr("Eye:", "眼睛状态："))
            self.aux_status_label_2.setText(self.tr("Gaze:", "注视状态："))
            if reset_status:
                self._set_badge(self.detect_status, self.tr("Detecting", "检测中"), "#38bdf8")
                self._set_badge(self.aux_status_value_1, self.tr("Not Detected", "未检测"), "#334155", "#f8fafc")
                self._set_badge(self.aux_status_value_2, self.tr("Not Detected", "未检测"), "#334155", "#f8fafc")

        if self.camera_active:
            self.camera_toggle_btn.setText(self.tr("Turn Off Camera", "关闭摄像头"))
        else:
            self.camera_toggle_btn.setText(self.tr("Start Camera", "启动摄像头"))

        if not self.video_loaded:
            self._set_badge(self.video_status, self.tr("Not Loaded", "未加载"), "#7f1d1d", "#fee2e2")

        self._update_camera_ui_state()

    def _camera_is_running(self):
        cap = getattr(self.capture_thread, "cap", None)
        return self.capture_thread.isRunning() or (cap is not None and hasattr(cap, "isOpened") and cap.isOpened())

    def _update_camera_ui_state(self):
        camera_running = self.camera_active or self._camera_is_running()
        if camera_running and not self.camera_active:
            self.camera_active = True

        if camera_running:
            self.camera_toggle_btn.setText(self.tr("Turn Off Camera", "关闭摄像头"))
            self._set_badge(self.cam_status, self.tr("Running", "运行中"), "#22c55e")
            if self.detect_checkbox.isChecked():
                self._set_badge(self.detect_status, self.tr("Detecting", "检测中"), "#38bdf8")
            else:
                self._set_badge(self.detect_status, self.tr("Disabled", "已禁用"), "#7f1d1d", "#fee2e2")
        else:
            self.camera_active = False
            self.camera_toggle_btn.setText(self.tr("Start Camera", "启动摄像头"))
            self._set_badge(self.cam_status, self.tr("Stopped", "已停止"), "#ef4444", "#fee2e2")
            self._set_badge(self.detect_status, self.tr("Disabled", "已禁用"), "#7f1d1d", "#fee2e2")
            if self.current_mode == "gesture":
                self._set_badge(self.aux_status_value_1, self.tr("Inactive", "未激活"), "#334155", "#f8fafc")
                self._set_badge(self.aux_status_value_2, self.tr("Waiting", "等待中"), "#334155", "#f8fafc")
            else:
                self._set_badge(self.aux_status_value_1, self.tr("Not Detected", "未检测"), "#334155", "#f8fafc")
                self._set_badge(self.aux_status_value_2, self.tr("Not Detected", "未检测"), "#334155", "#f8fafc")

    def _set_badge(self, label, text, background, color="#08111f"):
        label.setText(text)
        label.setStyleSheet(
            f"background-color: {background}; color: {color}; font-weight: 700; padding: 4px 10px; border-radius: 10px;"
        )

    def command_display_text(self, command):
        labels = {
            "play": self.tr("Play", "播放"),
            "pause": self.tr("Pause", "暂停"),
            "toggle": self.tr("Play/Pause", "播放/暂停"),
            "seek_forward": self.tr("Fast Forward 5s", "快进 5 秒"),
            "seek_back": self.tr("Rewind 5s", "快退 5 秒"),
            "vol_up": self.tr("Volume +5%", "音量 +5%"),
            "vol_down": self.tr("Volume -5%", "音量 -5%"),
        }
        return labels.get(command, self.tr("Waiting", "等待中"))

    def control_mode_text(self, command):
        if command in ("vol_up", "vol_down"):
            return self.tr("Volume Control", "音量控制"), "#c084fc"
        if command in ("seek_forward", "seek_back"):
            return self.tr("Seek Control", "快进快退控制"), "#38bdf8"
        if command in ("play", "pause", "toggle"):
            return self.tr("Playback Control", "播放控制"), "#60a5fa"
        return self.tr("Waiting", "等待中"), "#334155"

    def toggle_language(self):
        self.current_language = "zh" if self.current_language == "en" else "en"
        self.apply_language()

    def auto_start_camera(self):
        try:
            self.capture_thread.start_capture()
            self.camera_active = True
            self._update_camera_ui_state()
        except Exception as exc:
            self.camera_active = False
            self._update_camera_ui_state()
            self._set_badge(self.cam_status, self.tr("Failed to Start", "启动失败"), "#ef4444", "#fee2e2")
            QMessageBox.critical(
                self,
                self.tr("Error", "错误"),
                f"{self.tr('Cannot auto-start camera', '无法自动启动摄像头')}: {exc}",
            )

    def on_camera_stopped(self):
        if self.capture_thread.isRunning() or self.camera_active:
            return
        self.camera_active = False
        self._update_camera_ui_state()

    def toggle_camera(self):
        if self.camera_active:
            self.stop_camera()
        else:
            self.start_camera()

    def start_camera(self):
        try:
            self.capture_thread.start_capture()
            self.camera_active = True
            self._update_camera_ui_state()
        except Exception as exc:
            self.camera_active = False
            self._update_camera_ui_state()
            self._set_badge(self.cam_status, self.tr("Failed to Start", "启动失败"), "#ef4444", "#fee2e2")
            QMessageBox.critical(
                self,
                self.tr("Error", "错误"),
                f"{self.tr('Cannot start camera', '无法启动摄像头')}: {exc}",
            )

    def stop_camera(self):
        self.capture_thread.stop_capture()
        self.camera_active = False
        self._update_camera_ui_state()
        self.camera_display.setText(self.tr("Camera Stopped", "摄像头已关闭"))
        self.camera_display.setPixmap(QPixmap())

    def switch_mode(self, mode):
        if mode == self.current_mode:
            return

        was_active = self.camera_active
        if was_active:
            self.capture_thread.stop_capture()
            self.camera_active = False

        self.current_mode = mode
        self.capture_thread.set_mode(mode)
        self.last_control_command = None
        self.last_action_hold_until_ms = 0
        self.apply_mode_ui(reset_status=True)

        if was_active:
            self.start_camera()
        else:
            self._set_badge(self.cam_status, self.tr("Stopped", "已停止"), "#ef4444", "#fee2e2")

    def toggle_detection(self, state):
        is_detecting = state == Qt.CheckState.Checked.value
        self.capture_thread.toggle_detection(is_detecting)
        if is_detecting:
            self._set_badge(self.detect_status, self.tr("Detecting", "检测中"), "#38bdf8")
        else:
            self._set_badge(self.detect_status, self.tr("Disabled", "已禁用"), "#7f1d1d", "#fee2e2")

    def toggle_landmarks(self, state):
        self.capture_thread.toggle_landmarks(state == Qt.CheckState.Checked.value)

    def open_video_from_display(self, event):
        if event.button() == Qt.LeftButton:
            self.select_video()

    def select_video(self):
        default_dir = os.path.abspath(os.path.join(CURRENT_DIR, "..", "assets"))
        if self.current_video_file:
            current_dir = os.path.dirname(self.current_video_file)
            if os.path.isdir(current_dir):
                default_dir = current_dir
        if not os.path.isdir(default_dir):
            default_dir = os.getcwd()

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select Video File", "选择视频文件"),
            default_dir,
            "Video Files (*.mp4 *.avi *.mov *.mkv *.flv *.wmv *.MP4 *.AVI *.MOV *.MKV *.FLV *.WMV)",
        )

        if not file_path:
            return

        self.current_video_file = file_path
        if self.video_loaded:
            self.video_player_thread.stop()

        if self.video_player_thread.load_video(file_path):
            self.video_loaded = True
            self._set_badge(self.video_status, self.tr("Loaded", "已加载"), "#22c55e")

            cap = cv2.VideoCapture(file_path)
            try:
                if cap.isOpened():
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        self.display_video_frame(frame)
            finally:
                cap.release()

            self.progress_slider.setValue(0)
            self.update_time_label(0, self.video_duration)
            self.video_display.setToolTip(file_path)
        else:
            self.video_loaded = False
            self._set_badge(self.video_status, self.tr("Load Failed", "加载失败"), "#ef4444", "#fee2e2")
            QMessageBox.warning(
                self,
                self.tr("Failure", "失败"),
                f"{self.tr('Cannot load video', '无法加载视频')}: {os.path.basename(file_path)}",
            )

    def update_video_info(self, video_info):
        self.video_duration = video_info.get("duration", 0)
        self.update_time_label(0, self.video_duration)

    def handle_command(self, command):
        if not command:
            return

        if self.current_mode == "eye":
            if command == "play":
                self.play_video()
            elif command == "pause":
                self.pause_video()
            return

        now_ms = int(time.time() * 1000)
        self.last_control_command = command
        self.last_action_hold_until_ms = now_ms + self.gesture_display_hold_ms

        if command in ("play", "pause", "toggle"):
            if self.video_loaded:
                should_pause = command == "pause" or (
                    command == "toggle" and self.video_player_thread.playing and not self.video_player_thread.paused
                )
                if should_pause:
                    self.pause_video()
                else:
                    self.play_video()
            return

        if command in ("seek_forward", "seek_back") and self.video_loaded:
            try:
                delta = 5.0 if command == "seek_forward" else -5.0
                current_time = self.video_player_thread.get_position() * self.video_duration
                new_time = max(0.0, min(self.video_duration, current_time + delta))
                target_frame = int((new_time / max(self.video_duration, 0.001)) * self.video_player_thread.total_frames)
                self.video_player_thread.seek(target_frame)
                self.update_time_label(new_time, self.video_duration)
            except Exception as exc:
                error(f"Seek command failed: {exc}")
            return

        if command in ("vol_up", "vol_down"):
            volume_delta = "+5%" if command == "vol_up" else "-5%"
            try:
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", volume_delta],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                )
            except Exception as exc:
                error(f"Volume command failed: {exc}")

    def play_video(self):
        if self.video_loaded:
            self.video_player_thread.play()
            self._set_badge(self.video_status, self.tr("Playing", "播放中"), "#60a5fa")
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))

    def pause_video(self):
        if self.video_loaded:
            self.video_player_thread.pause()
            self._set_badge(self.video_status, self.tr("Paused", "已暂停"), "#f59e0b")
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.play_pause_btn.setText(self.tr("Play", "播放"))

    def stop_video(self):
        if self.video_loaded:
            self.video_player_thread.stop()
            self._set_badge(self.video_status, self.tr("Stopped", "已停止"), "#ef4444", "#fee2e2")
            self.progress_slider.setValue(0)
            self.update_time_label(0, self.video_duration)
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.play_pause_btn.setText(self.tr("Play", "播放"))

    def update_camera_frame(self, frame):
        self.display_frame(self.camera_display, frame)
        self._update_camera_ui_state()

    def update_video_frame(self, frame):
        self.display_video_frame(frame)

    def display_frame(self, label, frame):
        if frame is None:
            return
        label.clear()
        rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb_image.shape
        bytes_per_line = channels * width
        qt_image = QImage(rgb_image.data, width, height, bytes_per_line, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qt_image)
        scaled_pixmap = pixmap.scaled(
            label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled_pixmap)

    def display_video_frame(self, frame):
        if frame is not None:
            self.latest_video_frame = frame.copy()
        self.display_frame(self.video_display, frame)
        if self.is_in_fullscreen_mode and self.fullscreen_player and frame is not None:
            self.fullscreen_player.update_video_frame(frame)

    def sync_fullscreen_video_frame(self):
        if not self.fullscreen_player:
            return

        if self.latest_video_frame is not None:
            self.fullscreen_player.update_video_frame(self.latest_video_frame)
            return

        if not self.current_video_file or not os.path.exists(self.current_video_file):
            return

        cap = cv2.VideoCapture(self.current_video_file)
        try:
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    self.fullscreen_player.update_video_frame(frame)
        finally:
            cap.release()

    def update_detection_status(self, detection_result):
        if self.current_mode == "gesture":
            self.update_gesture_detection_status(detection_result)
        else:
            self.update_eye_detection_status(detection_result)

    def update_gesture_detection_status(self, detection_result):
        now_ms = int(time.time() * 1000)
        if not detection_result:
            if self.detect_checkbox.isChecked() and self.camera_active:
                self._set_badge(self.detect_status, self.tr("Detecting", "检测中"), "#38bdf8")
            self._set_badge(self.aux_status_value_1, self.tr("Inactive", "未激活"), "#334155", "#f8fafc")
            if self.last_control_command and now_ms < self.last_action_hold_until_ms:
                mode_text, mode_color = self.control_mode_text(self.last_control_command)
                self._set_badge(self.aux_status_value_1, mode_text, mode_color)
                self._set_badge(self.aux_status_value_2, self.command_display_text(self.last_control_command), "#22c55e")
            else:
                self._set_badge(self.aux_status_value_2, self.tr("Waiting", "等待中"), "#334155", "#f8fafc")
            return

        hand_present = detection_result.get("hand_present", False)
        command = detection_result.get("cmd")

        if not hand_present:
            self._set_badge(self.detect_status, self.tr("No Hand", "未检测到手"), "#ef4444", "#fee2e2")
            self._set_badge(self.aux_status_value_1, self.tr("Inactive", "未激活"), "#334155", "#f8fafc")
            if self.last_control_command and now_ms < self.last_action_hold_until_ms:
                self._set_badge(self.aux_status_value_2, self.command_display_text(self.last_control_command), "#22c55e")
            else:
                self._set_badge(self.aux_status_value_2, self.tr("Waiting", "等待中"), "#334155", "#f8fafc")
            return

        self._set_badge(self.detect_status, self.tr("Gesture Active", "手势激活"), "#22c55e")
        if command:
            mode_text, mode_color = self.control_mode_text(command)
            self.last_control_command = command
            self.last_action_hold_until_ms = now_ms + self.gesture_display_hold_ms
            self._set_badge(self.aux_status_value_1, mode_text, mode_color)
            self._set_badge(self.aux_status_value_2, self.command_display_text(command), "#22c55e")
        elif self.last_control_command and now_ms < self.last_action_hold_until_ms:
            mode_text, mode_color = self.control_mode_text(self.last_control_command)
            self._set_badge(self.aux_status_value_1, mode_text, mode_color)
            self._set_badge(self.aux_status_value_2, self.command_display_text(self.last_control_command), "#22c55e")
        else:
            self._set_badge(self.aux_status_value_1, self.tr("Inactive", "未激活"), "#334155", "#f8fafc")
            self._set_badge(self.aux_status_value_2, self.tr("Waiting", "等待中"), "#334155", "#f8fafc")

    def update_eye_detection_status(self, detection_result):
        if detection_result and detection_result.get("face_detected", False):
            self._set_badge(self.detect_status, self.tr("Face Detected", "已检测到人脸"), "#22c55e")
            if detection_result.get("eyes_closed", False):
                self._set_badge(self.aux_status_value_1, self.tr("Eyes Closed", "闭眼"), "#f59e0b")
            else:
                self._set_badge(self.aux_status_value_1, self.tr("Eyes Open", "睁眼"), "#60a5fa")

            if detection_result.get("is_gazing", False):
                self._set_badge(self.aux_status_value_2, self.tr("Gazing", "注视中"), "#22c55e")
            else:
                self._set_badge(self.aux_status_value_2, self.tr("Not Gazing", "未注视"), "#ef4444", "#fee2e2")
        else:
            self._set_badge(self.detect_status, self.tr("Not Detected", "未检测"), "#7f1d1d", "#fee2e2")
            self._set_badge(self.aux_status_value_1, self.tr("Not Detected", "未检测"), "#334155", "#f8fafc")
            self._set_badge(self.aux_status_value_2, self.tr("Not Detected", "未检测"), "#334155", "#f8fafc")

    def update_fps_display(self, fps):
        self.fps_display.setText(f"{fps:.1f}")

    def update_progress(self):
        if self.video_loaded and self.video_player_thread.playing and not self.video_player_thread.paused:
            position = self.video_player_thread.get_position()
            if not self.is_slider_pressed:
                self.progress_slider.setValue(int(position * 1000))

            current_time = position * self.video_duration
            self.update_time_label(current_time, self.video_duration)

            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.update_progress(position, self.video_duration)

    def update_time_label(self, current_time, total_time):
        current_str = f"{int(current_time // 60):02d}:{int(current_time % 60):02d}"
        total_str = f"{int(total_time // 60):02d}:{int(total_time % 60):02d}"
        self.time_label.setText(f"{current_str} / {total_str}")

    def on_progress_slider_moved(self, value):
        if self.video_loaded and self.is_slider_pressed:
            position = value / 1000.0
            target_frame = int(position * self.video_player_thread.total_frames)
            self.video_player_thread.seek(target_frame)
            self.update_time_label(position * self.video_duration, self.video_duration)
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.update_progress(position, self.video_duration)

    def on_progress_slider_pressed(self):
        self.is_slider_pressed = True

    def on_progress_slider_released(self):
        if self.video_loaded:
            position = self.progress_slider.value() / 1000.0
            self.video_player_thread.seek(int(position * self.video_player_thread.total_frames))
            self.update_time_label(position * self.video_duration, self.video_duration)
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.update_progress(position, self.video_duration)
        self.is_slider_pressed = False

    def on_playback_finished(self):
        if not self.video_loaded:
            return

        # Loop playback automatically when the video finishes.
        self.video_player_thread.seek(0)
        self.video_player_thread._pause_position = 0
        self.video_player_thread.play()
        self._set_badge(self.video_status, self.tr("Playing", "播放中"), "#60a5fa")
        self.progress_slider.setValue(0)
        if self.is_in_fullscreen_mode and self.fullscreen_player:
            self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))
            self.fullscreen_player.update_progress(0.0, self.video_duration)

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.showNormal()
            self.fullscreen_btn.setText(self.tr("Fullscreen", "全屏"))
            self.is_fullscreen = False
        else:
            self.showFullScreen()
            self.fullscreen_btn.setText(self.tr("Exit Fullscreen", "退出全屏"))
            self.is_fullscreen = True

    def enter_fullscreen_play_mode(self):
        if not self.video_loaded:
            QMessageBox.warning(
                self,
                self.tr("Notice", "提示"),
                self.tr("Please select a video file first", "请先选择视频文件"),
            )
            return

        if self.fullscreen_player is None:
            self.fullscreen_player = MultiModeFullScreenPlayer(self)
            self.video_player_thread.frame_ready.connect(self.fullscreen_player.update_video_frame)
            self.capture_thread.detection_status.connect(self.fullscreen_player.update_detection_status)

        if self.video_player_thread.playing and not self.video_player_thread.paused:
            self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))
        else:
            self.fullscreen_player.play_pause_btn.setText(self.tr("Play", "播放"))

        self.hide()
        self.fullscreen_player.show()
        self.is_in_fullscreen_mode = True
        self.sync_fullscreen_video_frame()
        QTimer.singleShot(80, self.sync_fullscreen_video_frame)

        position = self.video_player_thread.get_position() if self.video_loaded else 0.0
        self.fullscreen_player.update_progress(position, self.video_duration)
        self.fullscreen_player.show_status(self.tr("Entered fullscreen play mode", "已进入全屏播放模式"))

    def closeEvent(self, event):
        if self.fullscreen_player:
            try:
                self.fullscreen_player.close()
            except Exception as exc:
                error(f"Error closing fullscreen player: {exc}")
            self.fullscreen_player = None

        try:
            if hasattr(self, "progress_timer"):
                self.progress_timer.stop()
        except Exception as exc:
            error(f"Error stopping timers: {exc}")

        try:
            self.capture_thread.shutdown()
        except Exception as exc:
            error(f"Error stopping capture thread: {exc}")

        try:
            self.video_player_thread.shutdown()
            if self.video_player_thread.isRunning():
                self.video_player_thread.wait(3000)
        except Exception as exc:
            error(f"Error stopping video player thread: {exc}")

        try:
            cv2.destroyAllWindows()
        except Exception as exc:
            error(f"Error releasing OpenCV resources: {exc}")

        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()