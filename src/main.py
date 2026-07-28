import os
import sys
import cv2
import time
from datetime import datetime
os.environ["QT_LOGGING_RULES"] = "qt.pointer.dispatch=false"

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, 
    QHBoxLayout, QFileDialog, QMessageBox, QGroupBox, QCheckBox, QFrame,
    QSplitter, QGridLayout, QSlider, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap

# Import existing modules
# sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))  # Add src directory to path

from eye_detector import MediaPipeEyeDetector
from video_capture import VideoCaptureThread
from video_player import VideoPlayerThread
from fullscreen_player_mode import FullScreenPlayer
from log import debug, error

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.video_player_thread = VideoPlayerThread()
        self.video_thread = VideoCaptureThread()
        self.current_video_file = ""
        self.video_loaded = False
        self.camera_active = False
        self.is_fullscreen = False
        self.video_duration = 0
        self.video_position = 0
        self.latest_video_frame = None
        self.is_slider_pressed = False
        self.current_language = "en"
        
         # Fullscreen player window
        self.fullscreen_player = None
        self.is_in_fullscreen_mode = False
        
        # Connect signals
        self.video_thread.frame_ready.connect(self.update_camera_frame)
        self.video_thread.command_detected.connect(self.handle_command)
        self.video_thread.detection_status.connect(self.update_detection_status)
        self.video_thread.fps_updated.connect(self.update_fps_display)
        self.video_thread.finished.connect(self.on_video_stopped)
        
        self.video_player_thread.frame_ready.connect(self.update_video_frame)
        self.video_player_thread.playback_finished.connect(self.on_playback_finished)
        self.video_player_thread.video_info_ready.connect(self.update_video_info)

        # Setup styles
        self.setup_styles()
        
        self.init_ui()
        self.auto_start_camera()
        
        # Start video player thread
        self.video_player_thread.start()
        
        # Video progress update timer
        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self.update_progress)
        self.progress_timer.start(100)

    def tr(self, en_text, zh_text):
        return en_text if self.current_language == "en" else zh_text

    def translate_known_text(self, text):
        known_pairs = [
            ("Starting camera...", "正在启动摄像头..."),
            ("Camera Stopped", "摄像头已关闭"),
            ("Click select a video file", "点击选择视频文件"),
            ("Running", "运行中"),
            ("Failed to Start", "启动失败"),
            ("Stopped", "已停止"),
            ("Detecting...", "检测中"),
            ("Camera Off", "摄像头已关闭"),
            ("Disabled", "已禁用"),
            ("Detected", "已检测"),
            ("Eyes Closed", "闭眼"),
            ("Eyes Open", "睁眼"),
            ("Not Detected", "未检测"),
            ("Gazing", "注视中"),
            ("Not Gazing", "未注视"),
            ("Not Loaded", "未加载"),
            ("Loaded", "已加载"),
            ("Load Failed", "加载失败"),
            ("Playing", "播放中"),
            ("Paused", "已暂停"),
            ("Playback Completed", "播放完成"),
            ("Auto Playing", "自动播放中"),
            ("Auto Play Failed", "自动播放失败"),
        ]
        for en_text, zh_text in known_pairs:
            if text == en_text or text == zh_text:
                return self.tr(en_text, zh_text)
        return text
        
    def setup_styles(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e2e;
            }
            QLabel {
                color: #cdd6f4;
            }
            QGroupBox {
                color: #89b4fa;
                font-weight: bold;
                border: 2px solid #585b70;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 10px;
                background-color: #313244;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 15px;
                padding: 0 5px 0 5px;
            }
            QPushButton {
                background-color: #585b70;
                color: #cdd6f4;
                border: none;
                border-radius: 6px;
                padding: 6px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #6c7086;
            }
            QPushButton:pressed {
                background-color: #45475a;
            }
            QPushButton:disabled {
                background-color: #313244;
                color: #7f849c;
            }
            QCheckBox {
                color: #cdd6f4;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid #585b70;
            }
            QCheckBox::indicator:checked {
                background-color: #89b4fa;
                border-color: #89b4fa;
            }
            QFrame#status_frame {
                background-color: #313244;
                border-radius: 8px;
                border: 1px solid #585b70;
            }
            QLabel#status_value {
                font-weight: bold;
                padding: 2px 8px;
                border-radius: 4px;
            }
            QSlider {
                min-height: 20px;
            }
            QSlider::groove:horizontal {
                border: 1px solid #585b70;
                height: 8px;
                background: #313244;
                margin: 0px;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #89b4fa;
                border: 1px solid #5c81e3;
                width: 18px;
                margin: -5px 0;
                border-radius: 9px;
            }
            QSlider::sub-page:horizontal {
                background: #89b4fa;
                border: 1px solid #5c81e3;
                height: 8px;
                border-radius: 4px;
            }
            QProgressBar {
                border: 1px solid #585b70;
                border-radius: 4px;
                text-align: center;
                background-color: #313244;
            }
            QProgressBar::chunk {
                background-color: #89b4fa;
                border-radius: 4px;
            }
        """)
        
    def init_ui(self):
        self.setWindowTitle('👁️ Eye Remote Control')
        # use adaptive sizing based on screen dimensions
        screen = QApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        screen_width = screen_geometry.width()
        screen_height = screen_geometry.height()
        
        # Set window to 80% width and 85% height of screen size
        window_width = int(screen_width * 0.8)
        window_height = int(screen_height * 0.85)
        self.setGeometry(
            (screen_width - window_width) // 2,
            (screen_height - window_height) // 2,
            window_width,
            window_height
        )
        
        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(8, 8, 8, 8)
        
        # Title bar
        # title_frame = QFrame()
        # title_frame.setFixedHeight(50)
        # title_frame.setStyleSheet("background-color: #313244; border-radius: 8px;")
        
        # title_layout = QHBoxLayout(title_frame)
        
        # title_label = QLabel("👁️ Eye Remote Control")
        # title_label.setStyleSheet("color: #89b4fa; font-size: 18px; font-weight: bold;")
        
        # Fullscreen button
        self.fullscreen_btn = QPushButton("Fullscreen")
        self.fullscreen_btn.setFixedSize(int(window_width * 0.15), 30)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        self.fullscreen_btn.setStyleSheet("""
            QPushButton {
                background-color: #89b4fa;
                color: #1e1e2e;
                font-weight: bold;
                text-align: center;
            }
            QPushButton:hover {
                background-color: #74c7ec;
            }
        """)

        self.language_btn = QPushButton("中文")
        self.language_btn.setFixedSize(int(window_width * 0.1), 30)
        self.language_btn.clicked.connect(self.toggle_language)
        self.language_btn.setStyleSheet("""
            QPushButton {
                background-color: #89b4fa;
                color: #1e1e2e;
                font-weight: bold;
                text-align: center;
            }
            QPushButton:hover {
                background-color: #74c7ec;
            }
        """)

         #  Fullscreen play button
        self.fullscreen_play_btn = QPushButton("Fullscreen Play Mode")
        self.fullscreen_play_btn.setFixedSize(int(window_width * 0.15), 30)
        self.fullscreen_play_btn.clicked.connect(self.enter_fullscreen_play_mode)
        self.fullscreen_play_btn.setStyleSheet("""
            QPushButton {
                background-color: #89b4fa;
                color: #1e1e2e;
                font-weight: bold;
                text-align: center;
            }
            QPushButton:hover {
                background-color: #74c7ec;
            }
        """)
        # title_layout.addWidget(title_label)
        # title_layout.addStretch()
        # title_layout.addWidget(self.fullscreen_play_btn)
        # title_layout.addWidget(self.fullscreen_btn)
        # main_layout.addWidget(title_frame)
        
        # Main content area - horizontal split
        content_splitter = QSplitter(Qt.Horizontal)
        
        # Left side - video display area
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(10)
        
        # Camera display area
        self.camera_group = QGroupBox("📷 Camera Feed")
        camera_layout = QVBoxLayout()
        
        self.camera_display = QLabel("Starting camera...")
        self.camera_display.setAlignment(Qt.AlignCenter)
        self.camera_display.setScaledContents(True)  # Stretch to fill, no black bars
        # Use relative dimensions instead of fixed sizes
        self.camera_display.setMinimumSize(int(window_width * 0.4), int(window_height * 0.26))
        self.camera_display.setStyleSheet("""
            QLabel {
                background-color: #000000;
                border-radius: 8px;
                border: 2px solid #585b70;
                color: #ffffff;
                font-size: 14px;
            }
        """)
        
        camera_layout.addWidget(self.camera_display)
        self.camera_group.setLayout(camera_layout)
        left_layout.addWidget(self.camera_group)
        
        # Video playback area
        self.video_group = QGroupBox("🎬 Video Player")
        video_layout = QVBoxLayout()
        
        self.video_display = QLabel("Click select a video file")
        self.video_display.setAlignment(Qt.AlignCenter)
        self.video_display.setScaledContents(True)  # Stretch to fill, no black bars
        self.video_display.setCursor(Qt.CursorShape.PointingHandCursor)
        self.video_display.mousePressEvent = self.select_video_from_display
        # Use relative dimensions instead of fixed sizes
        self.video_display.setMinimumSize(int(window_width * 0.4), int(window_height * 0.26))
        self.video_display.setStyleSheet("""
            QLabel {
                background-color: #000000;
                border-radius: 8px;
                border: 2px solid #585b70;
                color: #ffffff;
                font-size: 14px;
            }
        """)
        
        # Video controls
        video_controls = QWidget()
        video_controls_layout = QVBoxLayout(video_controls)
        
        # Progress slider
        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.setValue(0)
        self.progress_slider.sliderMoved.connect(self.on_progress_slider_moved)
        self.progress_slider.sliderPressed.connect(self.on_progress_slider_pressed)
        self.progress_slider.sliderReleased.connect(self.on_progress_slider_released)
        
        # Time display and buttons
        control_row = QWidget()
        control_layout = QHBoxLayout(control_row)
        
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: #cdd6f4; font-size: 12px;")
        
        self.video_play_btn = QPushButton("Play")
        self.video_play_btn.clicked.connect(self.play_video)
        self.video_play_btn.setFixedSize(int(window_width * 0.15), 30)
        
        self.video_pause_btn = QPushButton("Pause")
        self.video_pause_btn.clicked.connect(self.pause_video)
        self.video_pause_btn.setFixedSize(int(window_width * 0.15), 30)
        
        self.video_stop_btn = QPushButton("Stop")
        self.video_stop_btn.clicked.connect(self.stop_video)
        self.video_stop_btn.setFixedSize(int(window_width * 0.15), 30)
        
        control_layout.addWidget(self.time_label)
        control_layout.addStretch()
        control_layout.addWidget(self.video_play_btn)
        control_layout.addWidget(self.video_pause_btn)
        control_layout.addWidget(self.video_stop_btn)
        
        video_controls_layout.addWidget(self.progress_slider)
        video_controls_layout.addWidget(control_row)
        
        video_layout.addWidget(self.video_display)
        video_layout.addWidget(video_controls)
        self.video_group.setLayout(video_layout)
        left_layout.addWidget(self.video_group)
        
        # Right side - control panel
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setSpacing(8)

        screen_show_group = QGroupBox()
        screen_show_layout = QGridLayout()
        screen_show_layout.setContentsMargins(6, 4, 6, 4)
        screen_show_layout.setColumnStretch(0, 1)
        screen_show_layout.setColumnStretch(1, 1)
        screen_show_layout.setColumnStretch(2, 1)
        screen_show_layout.addWidget(self.language_btn, 0, 0)
        screen_show_layout.addWidget(self.fullscreen_play_btn, 0, 1)
        screen_show_layout.addWidget(self.fullscreen_btn, 0, 2)
        screen_show_group.setLayout(screen_show_layout)
        right_layout.addWidget(screen_show_group)
        
        # Real-time status display
        self.status_group = QGroupBox("📊 System Status")
        status_layout = QGridLayout()
        status_layout.setVerticalSpacing(4)
        
        # Camera status
        self.cam_status_label = QLabel("📷 Camera:")
        self.cam_status_label.setStyleSheet("color: #a6adc8;")
        
        self.cam_status = QLabel("Running")
        self.cam_status.setObjectName("status_value")
        self.cam_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
        self.cam_status.setFixedSize(int(window_width * 0.1), 25)  # Use relative width
        
        # FPS display
        self.fps_label = QLabel("⚡ Camera FPS:")
        self.fps_label.setStyleSheet("color: #a6adc8;")
        
        self.fps_display = QLabel("0.0")
        self.fps_display.setObjectName("status_value")
        self.fps_display.setStyleSheet("background-color: #cba6f7; color: #000000;")
        self.fps_display.setFixedSize(120, 25)  # Fixed size to prevent layout changes
        
        # Detection status
        self.detect_status_label = QLabel("🔍 Detection Status:")
        self.detect_status_label.setStyleSheet("color: #a6adc8;")
        
        self.detect_status = QLabel("Detecting...")
        self.detect_status.setObjectName("status_value")
        self.detect_status.setStyleSheet("background-color: #f9e2af; color: #000000;")
        self.detect_status.setFixedSize(120, 25)  # Fixed size to prevent layout changes
        
        # Eye status
        self.eye_status_label = QLabel("👁️ Eye Status:")
        self.eye_status_label.setStyleSheet("color: #a6adc8;")
        
        self.eye_status = QLabel("Not Detected")
        self.eye_status.setObjectName("status_value")
        self.eye_status.setStyleSheet("background-color: #f9e2af; color: #000000;")
        self.eye_status.setFixedSize(120, 25)  # Fixed size to prevent layout changes
        
        # Gaze status
        self.gaze_status_label = QLabel("🎯 Gaze Status:")
        self.gaze_status_label.setStyleSheet("color: #a6adc8;")
        
        self.gaze_status = QLabel("Not Detected")
        self.gaze_status.setObjectName("status_value")
        self.gaze_status.setStyleSheet("background-color: #f9e2af; color: #000000;")
        self.gaze_status.setFixedSize(120, 25)  # Fixed size to prevent layout changes
        
        # Video playback status
        self.video_status_label = QLabel("▶️ Video Status:")
        self.video_status_label.setStyleSheet("color: #a6adc8;")
        
        self.video_status = QLabel("Not Loaded")
        self.video_status.setObjectName("status_value")
        self.video_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
        self.video_status.setFixedSize(120, 25)  # Fixed size to prevent layout changes
        
        # Video file info
        # file_info_group = QGroupBox("📁 Video Info")
        # file_info_layout = QVBoxLayout()
        
        # self.file_name_label = QLabel("Filename: Not Selected")
        # self.file_name_label.setStyleSheet("color: #cdd6f4; font-size: 15px;")
        
        # self.file_size_label = QLabel("Resolution: Not Loaded")
        # self.file_size_label.setStyleSheet("color: #cdd6f4; font-size: 15px;")
        
        # self.file_duration_label = QLabel("Duration: Not Loaded")
        # self.file_duration_label.setStyleSheet("color: #cdd6f4; font-size: 15px;")
        
        # self.file_fps_label = QLabel("Frame Rate: Not Loaded")
        # self.file_fps_label.setStyleSheet("color: #cdd6f4; font-size: 15px;")
        
        # file_info_layout.addWidget(self.file_name_label)
        # file_info_layout.addWidget(self.file_size_label)
        # file_info_layout.addWidget(self.file_duration_label)
        # file_info_layout.addWidget(self.file_fps_label)
        # file_info_group.setLayout(file_info_layout)
        
        # Add to grid layout
        status_layout.addWidget(self.cam_status_label, 0, 0)
        status_layout.addWidget(self.cam_status, 0, 1)
        status_layout.addWidget(self.fps_label, 0, 2)
        status_layout.addWidget(self.fps_display, 0, 3)
        
        status_layout.addWidget(self.detect_status_label, 1, 0)
        status_layout.addWidget(self.detect_status, 1, 1)
        status_layout.addWidget(self.eye_status_label, 1, 2)
        status_layout.addWidget(self.eye_status, 1, 3)
        
        status_layout.addWidget(self.gaze_status_label, 2, 0)
        status_layout.addWidget(self.gaze_status, 2, 1)
        status_layout.addWidget(self.video_status_label, 2, 2)
        status_layout.addWidget(self.video_status, 2, 3)
        
        self.status_group.setLayout(status_layout)
        right_layout.addWidget(self.status_group)
        # right_layout.addWidget(file_info_group)
        
        # Control instructions
        self.instruction_group = QGroupBox("📋 Control Instructions")
        instruction_layout = QVBoxLayout()
        instruction_layout.setContentsMargins(6, 4, 6, 4)
        
        self.instructions_label = QLabel(
            "<b>Automatic Control Commands:</b><br>"
            "• While playing video, eyes gazing at screen → Continue playback<br>"
            "• Eyes closed or looking away from screen → Pause playback<br>"
            "• Face not detected → Pause playback<br><br>"
            "<b>Note:</b><br>"
            "• Keep face within camera range<br>"
            "• Ensure adequate lighting<br>"
            "• After video starts playing, gaze at screen to continue playback"
        )
        self.instructions_label.setStyleSheet("color: #cdd6f4; padding: 5px;")
        self.instructions_label.setWordWrap(True)
        
        instruction_layout.addWidget(self.instructions_label)
        self.instruction_group.setLayout(instruction_layout)
        right_layout.addWidget(self.instruction_group)
        
        # Camera controls
        self.camera_control_group = QGroupBox("🎮 Camera Controls")
        camera_control_layout = QVBoxLayout()
        camera_control_layout.setContentsMargins(6, 4, 6, 4)
        camera_control_layout.setSpacing(4)
        
        self.camera_toggle_btn = QPushButton("Turn Off Camera")
        self.camera_toggle_btn.clicked.connect(self.toggle_camera)
        self.camera_toggle_btn.setFixedHeight(35)
        
        self.detect_checkbox = QCheckBox("Enable Eye Detection")
        self.detect_checkbox.setChecked(True)
        self.detect_checkbox.stateChanged.connect(self.toggle_detection)
        
        self.landmarks_checkbox = QCheckBox("Show Landmarks")
        self.landmarks_checkbox.setChecked(True)
        self.landmarks_checkbox.stateChanged.connect(self.toggle_landmarks)
        
        camera_control_layout.addWidget(self.camera_toggle_btn)
        camera_control_layout.addWidget(self.detect_checkbox)
        camera_control_layout.addWidget(self.landmarks_checkbox)
        self.camera_control_group.setLayout(camera_control_layout)
        right_layout.addWidget(self.camera_control_group)
        
        # Video file controls
        self.file_control_group = QGroupBox("📁 Video File Controls")
        file_control_layout = QVBoxLayout()
        file_control_layout.setContentsMargins(6, 4, 6, 4)
        
        self.select_video_btn = QPushButton("Select Video File")
        self.select_video_btn.clicked.connect(self.select_video)
        self.select_video_btn.setFixedHeight(40)
        
        file_control_layout.addWidget(self.select_video_btn)
        self.file_control_group.setLayout(file_control_layout)
        right_layout.addWidget(self.file_control_group)
        
        right_layout.addStretch()
        
        # Add to splitter
        content_splitter.addWidget(left_widget)
        content_splitter.addWidget(right_widget)
        content_splitter.setSizes([900, 500])
        
        main_layout.addWidget(content_splitter)
        
        # Setup fullscreen shortcut
        self.fullscreen_btn.setShortcut("F11")
        self.apply_language()

    def toggle_language(self):
        self.current_language = "zh" if self.current_language == "en" else "en"
        self.apply_language()

    def apply_language(self):
        is_en = self.current_language == "en"

        self.setWindowTitle('👁️ Eye Remote Control' if is_en else '👁️ 眼控遥控器')
        self.language_btn.setText("中文" if is_en else "English")

        if self.is_fullscreen:
            self.fullscreen_btn.setText("Exit Fullscreen" if is_en else "退出全屏")
        else:
            self.fullscreen_btn.setText("Fullscreen" if is_en else "全屏")

        self.fullscreen_play_btn.setText("Fullscreen Play Mode" if is_en else "全屏播放模式")
        self.video_play_btn.setText("Play" if is_en else "播放")
        self.video_pause_btn.setText("Pause" if is_en else "暂停")
        self.video_stop_btn.setText("Stop" if is_en else "停止")
        self.select_video_btn.setText("Select Video File" if is_en else "选择视频文件")

        if self.camera_active:
            self.camera_toggle_btn.setText("Turn Off Camera" if is_en else "关闭摄像头")
        else:
            self.camera_toggle_btn.setText("Start Camera" if is_en else "启动摄像头")

        self.detect_checkbox.setText("Enable Eye Detection" if is_en else "启用眼部检测")
        self.landmarks_checkbox.setText("Show Landmarks" if is_en else "显示关键点")

        self.camera_group.setTitle("📷 Camera Feed" if is_en else "📷 摄像头画面")
        self.video_group.setTitle("🎬 Video Player" if is_en else "🎬 视频播放器")
        self.status_group.setTitle("📊 System Status" if is_en else "📊 系统状态")
        self.instruction_group.setTitle("📋 Control Instructions" if is_en else "📋 控制说明")
        self.camera_control_group.setTitle("🎮 Camera Controls" if is_en else "🎮 摄像头控制")
        self.file_control_group.setTitle("📁 Video File Controls" if is_en else "📁 视频文件控制")

        self.cam_status_label.setText("📷 Camera:" if is_en else "📷 摄像头：")
        self.fps_label.setText("⚡ Camera FPS:" if is_en else "⚡ 摄像头帧率：")
        self.detect_status_label.setText("🔍 Detection Status:" if is_en else "🔍 检测状态：")
        self.eye_status_label.setText("👁️ Eye Status:" if is_en else "👁️ 眼睛状态：")
        self.gaze_status_label.setText("🎯 Gaze Status:" if is_en else "🎯 注视状态：")
        self.video_status_label.setText("▶️ Video Status:" if is_en else "▶️ 视频状态：")

        self.camera_display.setText(self.translate_known_text(self.camera_display.text()))
        self.video_display.setText(self.translate_known_text(self.video_display.text()))
        self.cam_status.setText(self.translate_known_text(self.cam_status.text()))
        self.detect_status.setText(self.translate_known_text(self.detect_status.text()))
        self.eye_status.setText(self.translate_known_text(self.eye_status.text()))
        self.gaze_status.setText(self.translate_known_text(self.gaze_status.text()))
        self.video_status.setText(self.translate_known_text(self.video_status.text()))

        self.instructions_label.setText(
            "<b>Automatic Control Commands:</b><br>"
            "• While playing video, eyes gazing at screen → Continue playback<br>"
            "• Eyes closed or looking away from screen → Pause playback<br>"
            "• Face not detected → Pause playback<br><br>"
            "<b>Note:</b><br>"
            "• Keep face within camera range<br>"
            "• Ensure adequate lighting<br>"
            "• After video starts playing, gaze at screen to continue playback"
            if is_en else
            "<b>自动控制指令：</b><br>"
            "• 播放时眼睛注视屏幕 → 继续播放<br>"
            "• 闭眼或视线离开屏幕 → 暂停播放<br>"
            "• 未检测到人脸 → 暂停播放<br><br>"
            "<b>注意：</b><br>"
            "• 请保持人脸在摄像头范围内<br>"
            "• 请确保环境光线充足<br>"
            "• 视频开始后请注视屏幕以继续播放"
        )
        
    def auto_start_camera(self):
        try:
            self.video_thread.start_capture()
            self.camera_active = True
            self.camera_toggle_btn.setText(self.tr("Turn Off Camera", "关闭摄像头"))
            self.cam_status.setText(self.tr("Running", "运行中"))
            self.cam_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
        except Exception as e:
            self.cam_status.setText(self.tr("Failed to Start", "启动失败"))
            self.cam_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
            QMessageBox.critical(self, self.tr("Error", "错误"), f"{self.tr('Cannot auto-start camera', '无法自动启动摄像头')}: {str(e)}")
            
    def toggle_camera(self):
        if self.camera_active:
            self.stop_camera()
        else:
            self.start_camera()
            
    def start_camera(self):
        try:
            self.video_thread.start_capture()
            self.camera_active = True
            self.camera_toggle_btn.setText(self.tr("Turn Off Camera", "关闭摄像头"))
            self.cam_status.setText(self.tr("Running", "运行中"))
            self.cam_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
            # When camera starts, also update detection status if detection is enabled
            if self.detect_checkbox.isChecked():
                self.detect_status.setText(self.tr("Detecting", "检测中"))
                self.detect_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
        except Exception as e:
            self.cam_status.setText(self.tr("Failed to Start", "启动失败"))
            self.cam_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
            QMessageBox.critical(self, self.tr("Error", "错误"), f"{self.tr('Cannot start camera', '无法启动摄像头')}: {str(e)}")
            
    def stop_camera(self):
        self.video_thread.stop_capture()
        self.camera_active = False
        self.camera_toggle_btn.setText(self.tr("Start Camera", "启动摄像头"))
        self.cam_status.setText(self.tr("Stopped", "已停止"))
        self.cam_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
        self.camera_display.setText(self.tr("Camera Stopped", "摄像头已关闭"))
        self.camera_display.setPixmap(QPixmap())
        # When camera stops, update detection status
        self.detect_status.setText(self.tr("Camera Off", "摄像头已关闭"))
        self.detect_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
        
        # Also stop video playback when camera is turned off
        if self.video_loaded:
            self.video_player_thread.stop()
            self.video_status.setText(self.tr("Stopped", "已停止"))
            self.video_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
            self.progress_slider.setValue(0)
            if hasattr(self, 'video_duration'):
                self.update_time_label(0, self.video_duration)
    def on_video_stopped(self):
        self.camera_active = False
        
    def toggle_detection(self, state):
        is_detecting = state == Qt.CheckState.Checked.value
        self.video_thread.toggle_detection(is_detecting)
        if is_detecting:
            self.detect_status.setText(self.tr("Detecting", "检测中"))
            self.detect_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
        else:
            self.detect_status.setText(self.tr("Disabled", "已禁用"))
            self.detect_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
        
    def toggle_landmarks(self, state):
        self.video_thread.toggle_landmarks(state == Qt.CheckState.Checked.value)
        
    def select_video_from_display(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.select_video()

    def select_video(self):
        assets_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets"))
        initial_dir = assets_dir if os.path.isdir(assets_dir) else ""
        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Video File", "选择视频文件"), initial_dir, "Video Files (*.mp4 *.avi *.mov *.mkv *.flv *.wmv *.MP4 *.AVI *.MOV *.MKV *.FLV *.WMV)")
        
        if file_path:
            self.current_video_file = file_path
            
            # Stop any currently playing video
            if self.video_loaded:
                self.video_player_thread.stop()
                
                # Wait briefly to allow resources to be freed
                time.sleep(0.1)
            
            # Disconnect old thread signals to prevent receiving from old thread
            try:
                self.video_player_thread.frame_ready.disconnect(self.update_video_frame)
                self.video_player_thread.playback_finished.disconnect(self.on_playback_finished)
                self.video_player_thread.video_info_ready.disconnect(self.update_video_info)
            except TypeError:
                # Signals may not be connected, which is fine
                pass
            
            # Delete old thread and create a new one
            old_thread = self.video_player_thread
            self.video_player_thread = VideoPlayerThread()
            
            # Connect new thread signals
            self.video_player_thread.frame_ready.connect(self.update_video_frame)
            self.video_player_thread.playback_finished.connect(self.on_playback_finished)
            self.video_player_thread.video_info_ready.connect(self.update_video_info)
            
            # Shutdown and clean up old thread
            old_thread.shutdown()
            if old_thread.isRunning():
                old_thread.quit()
                old_thread.wait(3000)  # Wait up to 3 seconds for thread to finish
            
            if self.video_player_thread.load_video(file_path):
                self.video_player_thread.start()  # Start the new thread
                self.video_loaded = True
                self.video_status.setText(self.tr("Loaded", "已加载"))
                self.video_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
                # Display first frame
                cap = cv2.VideoCapture(file_path)
                if cap.isOpened():
                    ret, frame = cap.read()
                    if ret:
                        self.display_video_frame(frame)
                    cap.release()
                
                # Reset progress slider and time label to start
                self.progress_slider.setValue(0)
                if hasattr(self, 'video_duration'):
                    self.update_time_label(0, self.video_duration)
            else:
                self.video_loaded = False
                self.video_status.setText(self.tr("Load Failed", "加载失败"))
                self.video_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
                QMessageBox.warning(self, self.tr("Failure", "失败"), f"{self.tr('Cannot load video', '无法加载视频')}: {os.path.basename(file_path)}")
                
    def update_video_info(self, video_info):
        """Update video information display"""
        filename = video_info['filename']
        width = video_info['width']
        height = video_info['height']
        fps = video_info['fps']
        duration = video_info['duration']
        
        # Update labels
        # self.file_name_label.setText(f"Filename: {filename}")
        # self.file_size_label.setText(f"Resolution: {video_info['width']} × {video_info['height']}")
        # self.file_duration_label.setText(f"Duration: {int(video_info['duration'] // 60):02d}:{int(video_info['duration'] % 60):02d}")
        # self.file_fps_label.setText(f"Frame Rate: {video_info['fps']:.1f} FPS")
        
        # Update time display
        self.video_duration = duration
        self.update_time_label(0, duration)
        
    def handle_command(self, command):
        """Handle detection command"""
        if command and self.video_loaded:
            if command == "play":
                self.play_video()
            elif command == "pause":
                self.pause_video()
                
    def play_video(self):
        if self.video_loaded and self.video_player_thread:
            try:
                self.video_player_thread.play()
                self.video_status.setText(self.tr("Playing", "播放中"))
                self.video_status.setStyleSheet("background-color: #89b4fa; color: #000000;")
                # Only update button text in fullscreen, overlays handled by detection_status
                if self.is_in_fullscreen_mode and self.fullscreen_player:
                    self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))
            except RuntimeError as e:
                error(f"Error playing video: {e}")
                QMessageBox.warning(self, self.tr("Playback Error", "播放错误"), self.tr("Failed to start playback", "启动播放失败"))
                
    def pause_video(self):
        if self.video_loaded and self.video_player_thread:
            try:
                self.video_player_thread.pause()
                self.video_status.setText(self.tr("Paused", "已暂停"))
                self.video_status.setStyleSheet("background-color: #f9e2af; color: #000000;")
                # Only update button text in fullscreen, overlays handled by detection_status
                if self.is_in_fullscreen_mode and self.fullscreen_player:
                    self.fullscreen_player.play_pause_btn.setText(self.tr("Play", "播放"))
            except RuntimeError as e:
                error(f"Error pausing video: {e}")
                QMessageBox.warning(self, self.tr("Playback Error", "播放错误"), self.tr("Failed to pause playback", "暂停播放失败"))
            
    def stop_video(self):
        if self.video_loaded and self.video_player_thread:
            try:
                self.video_player_thread.stop()
                self.video_status.setText(self.tr("Stopped", "已停止"))
                self.video_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
                self.progress_slider.setValue(0)
                self.update_time_label(0, self.video_duration)
                # Only update button text in fullscreen
                if self.is_in_fullscreen_mode and self.fullscreen_player:
                    self.fullscreen_player.play_pause_btn.setText(self.tr("Play", "播放"))
                    self.fullscreen_player.update_progress(0.0, self.video_duration)
            except RuntimeError as e:
                error(f"Error stopping video: {e}")
                QMessageBox.warning(self, self.tr("Playback Error", "播放错误"), self.tr("Failed to stop playback", "停止播放失败"))
            
    def update_camera_frame(self, frame):
        self.display_frame(self.camera_display, frame)
        
    def update_video_frame(self, frame):
        self.display_video_frame(frame)
        
    def display_frame(self, label, frame):
        """Display frame to specified label"""
        rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        qt_image = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qt_image)
        
        scaled_pixmap = pixmap.scaled(
            label.size(), 
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        
        label.setPixmap(scaled_pixmap)
        
    def display_video_frame(self, frame):
        """Display video frame"""
        if frame is not None:
            self.latest_video_frame = frame.copy()
        self.display_frame(self.video_display, frame)

    def sync_fullscreen_video_frame(self):
        """Show the currently available video frame in fullscreen immediately."""
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
                if self.video_player_thread.total_frames > 0:
                    current_frame = int(self.video_player_thread.get_position() * self.video_player_thread.total_frames)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, current_frame)
                ret, frame = cap.read()
                if ret:
                    self.latest_video_frame = frame.copy()
                    self.fullscreen_player.update_video_frame(frame)
        finally:
            cap.release()
        
    def update_detection_status(self, detection_result):
        """Update detection status"""
        if detection_result and detection_result.get('face_detected', False):
            self.eye_status.setText(self.tr("Detected", "已检测"))
            
            # Check if eyes are closed
            eyes_closed = detection_result.get('eyes_closed', False)
            if eyes_closed:
                self.eye_status.setText(self.tr("Eyes Closed", "闭眼"))
                self.eye_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
            else:
                self.eye_status.setText(self.tr("Eyes Open", "睁眼"))
                self.eye_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
                
            # Check gaze status
            is_gazing = detection_result.get('is_gazing', False)
            if is_gazing:
                self.gaze_status.setText(self.tr("Gazing", "注视中"))
                self.gaze_status.setStyleSheet("background-color: #89b4fa; color: #000000;")
            else:
                self.gaze_status.setText(self.tr("Not Gazing", "未注视"))
                self.gaze_status.setStyleSheet("background-color: #a6adc8; color: #000000;")
        else:
            self.eye_status.setText(self.tr("Not Detected", "未检测"))
            self.eye_status.setStyleSheet("background-color: #a6adc8; color: #000000;")
            self.gaze_status.setText(self.tr("Not Detected", "未检测"))
            self.gaze_status.setStyleSheet("background-color: #a6adc8; color: #000000;")
        
    def update_fps_display(self, fps):
        """Update FPS display"""
        self.fps_display.setText(f"{fps:.1f}")
        
    def update_progress(self):
        """Update progress bar"""
        if self.video_loaded and hasattr(self, 'video_player_thread') and self.video_player_thread.playing and not self.video_player_thread.paused:
            position = self.video_player_thread.get_position()
            if not self.is_slider_pressed:
                self.progress_slider.setValue(int(position * 1000))
            
            # Update time display
            current_time = position * self.video_duration
            self.update_time_label(current_time, self.video_duration)

            # Keep fullscreen control bar progress/time in sync
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.update_progress(position, self.video_duration)
        
    def update_time_label(self, current_time, total_time):
        """Update time display label"""
        current_str = f"{int(current_time // 60):02d}:{int(current_time % 60):02d}"
        total_str = f"{int(total_time // 60):02d}:{int(total_time % 60):02d}"
        self.time_label.setText(f"{current_str} / {total_str}")
        
    def on_progress_slider_moved(self, value):
        """Progress slider moved event"""
        if self.video_loaded and self.is_slider_pressed:
            position = value / 1000.0
            current_time = position * self.video_duration
            self.update_time_label(current_time, self.video_duration)
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.update_progress(position, self.video_duration)
            
    def on_progress_slider_pressed(self):
        """Progress slider pressed event"""
        debug("Slider pressed: is_slider_pressed = True")
        self.is_slider_pressed = True
        
    def on_progress_slider_released(self):
        """Progress slider released event"""
        if self.video_loaded:
            position = self.progress_slider.value() / 1000.0
            self.video_player_thread.seek(int(position * self.video_player_thread.total_frames))
            current_time = position * self.video_duration
            self.update_time_label(current_time, self.video_duration)
            if self.is_in_fullscreen_mode and self.fullscreen_player:
                self.fullscreen_player.update_progress(position, self.video_duration)
        self.is_slider_pressed = False
        
    def on_playback_finished(self):
        """Video playback finished event"""
        self.video_status.setText(self.tr("Playback Completed", "播放完成"))
        self.video_status.setStyleSheet("background-color: #a6e3a1; color: #000000;")
        self.progress_slider.setValue(1000)
        
        # Automatically find and play the next video file
        started = self.play_next_video()
        if not started and self.current_video_file and os.path.exists(self.current_video_file):
            if self.video_player_thread.load_video(self.current_video_file):
                self.video_loaded = True
                self.video_status.setText(self.tr("Auto Playing", "自动播放中"))
                self.video_status.setStyleSheet("background-color: #89b4fa; color: #000000;")
                if self.is_in_fullscreen_mode and self.fullscreen_player:
                    self.fullscreen_player.update_progress(0.0, self.video_duration)
                self.video_player_thread.play()
                if self.is_in_fullscreen_mode and self.fullscreen_player:
                    self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))
                    self.fullscreen_player.show_status(self.tr("Auto replaying current video", "自动重播当前视频"))
        
    def play_next_video(self):
        """Find and play the next video file"""
        if not self.current_video_file:
            return False
            
        # Get the directory of the current video
        current_dir = os.path.dirname(self.current_video_file)
        if not current_dir:
            current_dir = "."
            
        # If no directory is specified, use the current working directory
        if current_dir == ".":
            current_dir = os.getcwd()
            
        # Find all supported video files in the directory
        try:
            video_files = []
            # Support more video formats
            supported_extensions = ('.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv')
            for file in os.listdir(current_dir):
                if file.lower().endswith(supported_extensions):
                    video_files.append(file)
                    
            # If no video files are found, return
            if not video_files:
                return False
                
            # Sort the files
            video_files.sort()
            
            # Find the position of the currently playing file in the list
            current_filename = os.path.basename(self.current_video_file)
            try:
                current_index = video_files.index(current_filename)
                # Calculate the index of the next file (loop playback)
                next_index = (current_index + 1) % len(video_files)
                next_video = video_files[next_index]
            except ValueError:
                # If the current file is not in the list, play the first file
                next_video = video_files[0]
                
            # Build the full file path
            next_video_path = os.path.join(current_dir, next_video)
            
            # Check if the file exists
            if not os.path.exists(next_video_path):
                self.statusBar().showMessage(
                    f"{self.tr('Next video file not found', '未找到下一个视频文件')}: {next_video}"
                )
                return False
            
            # Reuse existing player thread to avoid reinit stalls and state desync
            if self.video_player_thread:
                self.video_player_thread.stop()
                time.sleep(0.05)
            
            # Load and play the next video
            if self.video_player_thread.load_video(next_video_path):
                self.current_video_file = next_video_path
                self.video_loaded = True
                self.video_status.setText(self.tr("Auto Playing", "自动播放中"))
                self.video_status.setStyleSheet("background-color: #89b4fa; color: #000000;")
                if self.is_in_fullscreen_mode and self.fullscreen_player:
                    self.fullscreen_player.update_progress(0.0, self.video_duration)
                
                # Ensure the video player is in the correct state
                self.video_player_thread.play()
                
                # Show message
                self.statusBar().showMessage(
                    f"{self.tr('Auto-playing next video', '正在自动播放下一个视频')}: {next_video}"
                )
                
                # If in fullscreen mode, update the fullscreen player as well
                if self.is_in_fullscreen_mode and self.fullscreen_player:
                    self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))
                    self.fullscreen_player.show_status(
                        f"{self.tr('Auto-playing', '自动播放')}: {next_video}"
                    )
                return True
            else:
                self.video_loaded = False
                self.video_status.setText(self.tr("Auto Play Failed", "自动播放失败"))
                self.video_status.setStyleSheet("background-color: #f38ba8; color: #000000;")
                self.statusBar().showMessage(self.tr("Failed to auto-play next video", "自动播放下一个视频失败"))
                return False
                
        except Exception as e:
            error(f" Error finding next video: {e}")
            self.statusBar().showMessage(self.tr("Error occurred while finding next video", "查找下一个视频时发生错误"))
            return False
      
        
    def toggle_fullscreen(self):
        """Toggle fullscreen mode"""
        if self.is_fullscreen:
            self.showNormal()
            self.fullscreen_btn.setText("Fullscreen" if self.current_language == "en" else "全屏")
            self.is_fullscreen = False
        else:
            self.showFullScreen()
            self.fullscreen_btn.setText("Exit Fullscreen" if self.current_language == "en" else "退出全屏")
            self.is_fullscreen = True
    def enter_fullscreen_play_mode(self):
        """Enter fullscreen play mode"""
        if not self.video_loaded:
            QMessageBox.warning(
                self,
                "Notice" if self.current_language == "en" else "提示",
                "Please select a video file first" if self.current_language == "en" else "请先选择视频文件"
            )
            return
            
        if self.fullscreen_player is None:
            self.fullscreen_player = FullScreenPlayer(self)
            
            # Connect signals
            self.video_player_thread.frame_ready.connect(self.fullscreen_player.update_video_frame)
            self.video_thread.detection_status.connect(self.fullscreen_player.update_detection_status)
            self.video_player_thread.playback_finished.connect(self.on_playback_finished)
        
        # Set fullscreen play button text based on current playback status
        if self.video_player_thread.playing and not self.video_player_thread.paused:
            self.fullscreen_player.play_pause_btn.setText(self.tr("Pause", "暂停"))
        else:
            self.fullscreen_player.play_pause_btn.setText(self.tr("Play", "播放"))
            
        # Hide main window, show fullscreen player
        self.hide()
        self.fullscreen_player.show()
        self.is_in_fullscreen_mode = True
        self.sync_fullscreen_video_frame()
        QTimer.singleShot(80, self.sync_fullscreen_video_frame)

        # Initialize fullscreen progress/time immediately
        position = self.video_player_thread.get_position() if self.video_loaded else 0.0
        self.fullscreen_player.update_progress(position, self.video_duration)
        
        # Update status
        self.fullscreen_player.show_status(self.tr("Entered fullscreen play mode", "已进入全屏播放模式"))


    def closeEvent(self, event):
        """Window close event """ 
        # Closing application, cleaning up resources
        # If fullscreen player exists, close it first
        if self.fullscreen_player:
            try:
                self.fullscreen_player.close()
            except Exception as e:
                error(f"Error closing fullscreen player: {e}")
            self.fullscreen_player = None
            
        # Stop timers
        try:
            if hasattr(self, 'status_timer'):
                self.status_timer.stop()
            if hasattr(self, 'progress_timer'):
                self.progress_timer.stop()
        except Exception as e:
            error(f"Error Stop timers : {e}")
        
        # Stop camera thread
        try:
            if hasattr(self, 'video_thread'):
                # Stopping camera thread
                self.video_thread.stop_capture()
        except Exception as e:
            error(f"Error stopping camera thread : {e}")
        
        # Stop video player thread
        try:
            if hasattr(self, 'video_player_thread'):
                # Stopping video player thread
                self.video_player_thread.stop()
                self.video_player_thread.shutdown()  # Use new shutdown method
                
                # Wait for thread to finish
                if self.video_player_thread.isRunning():
                    self.video_player_thread.quit()
                    self.video_player_thread.wait(3000)  # Wait up to 3 seconds
        except Exception as e:
            error(f"Error stopping video player thread: {e}")
        
        # Force close MediaPipe related resources (if possible)
        try:
            # If there is a MediaPipe cleanup method, call it
            if (hasattr(self, 'video_thread') and 
                hasattr(self.video_thread, 'eye_detector') and
                hasattr(self.video_thread.eye_detector, 'close')):
                self.video_thread.eye_detector.close()
        except Exception as e:
            error(f"Error closing MediaPipe resources: {e}")
        
        # Ensure all OpenCV resources are released
        try:
            cv2.destroyAllWindows()
        except:
            error(f"Error release OpenCV resources: {e}")
        
        # Resource cleanup completed
        event.accept()

    def resizeEvent(self, event):
        """Handle window resize events"""
        super().resizeEvent(event)
        # Adjust display areas when window size changes
        if hasattr(self, 'camera_display') and hasattr(self, 'video_display'):
            # Adjust display areas according to new window size
            new_width = event.size().width()
            new_height = event.size().height()
            
            # Update minimum size of display areas
            self.camera_display.setMinimumSize(int(new_width * 0.4), int(new_height * 0.26))
            self.video_display.setMinimumSize(int(new_width * 0.4), int(new_height * 0.26))
            
            # Update button sizes
            self.fullscreen_btn.setFixedSize(int(new_width * 0.15), 30)
            self.language_btn.setFixedSize(int(new_width * 0.1), 30)
            self.fullscreen_play_btn.setFixedSize(int(new_width * 0.15), 30)
            self.video_play_btn.setFixedSize(int(new_width * 0.06), 30)
            self.video_pause_btn.setFixedSize(int(new_width * 0.06), 30)
            self.video_stop_btn.setFixedSize(int(new_width * 0.06), 30)
            
            # Update status label size
            if hasattr(self, 'cam_status'):
                self.cam_status.setFixedSize(int(new_width * 0.1), 25)

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()