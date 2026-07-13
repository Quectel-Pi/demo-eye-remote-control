import cv2
import time
import threading
from PySide6.QtCore import QThread, Signal
from eye_detector import MediaPipeEyeDetector
from log import debug,error


class VideoCaptureThread(QThread):
    frame_ready = Signal(object)
    detection_status = Signal(dict)  # Emit detection status
    fps_updated = Signal(float)  # Emit FPS updates
    command_detected = Signal(str)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.cap = None
        self.running = False
        self.detecting = True
        self.show_landmarks = True

        # Add exit flag
        self.exiting = False
        self._closed = True  # Track whether resources have been released
        self._lock = threading.RLock()  # Reentrant lock for resource protection
        self.camera_id = None
        self.reconnect_interval = 2.0
        self._last_reconnect_attempt = 0.0

        # Component initialization
        self.eye_detector = MediaPipeEyeDetector()

        # FPS calculation
        self.frame_count = 0
        self.fps = 0
        self.last_fps_time = time.time()
        self.last_command = None
        self.last_face_detected_time = time.time()

    def find_available_camera(self):
        """Automatically detect available camera"""
        #debug("Searching for available camera devices...")
        # First try the default cameras (0-9)
        for i in range(10):
            temp_cap = None
            try:
                temp_cap = cv2.VideoCapture(i)
                if temp_cap.isOpened():
                    ret, frame = temp_cap.read()
                    if ret:
                        temp_cap.release()
                        #debug(f"Found available camera at device ID: {i}")
                        return i
            except Exception as e:
                error(f"Error checking camera {i}: {e}")
            finally:
                if temp_cap is not None:
                    try:
                        temp_cap.release()
                    except:
                        error(f"Error temp_cap.release(): {e}")
        error("No available camera device found")
        return None

    def start_capture(self, camera_id=None):
        if camera_id is None:
            camera_id = self.find_available_camera()
            if camera_id is None:
                error("No available camera device found, waiting for camera reconnect")

        #debug(f"Starting camera capture on device ID: {camera_id}")

        # Release existing capture if any
        self._release_capture_only()

        with self._lock:
            self.camera_id = camera_id
            self.exiting = False
        if camera_id is not None:
            self._open_capture(camera_id)

        with self._lock:
            self.running = True
            self.frame_count = 0
            self.fps = 0
            self.last_fps_time = time.time()
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

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        with self._lock:
            self._release_capture_only()
            self.cap = cap
            self._closed = False
            self.camera_id = camera_id
        #debug(f"Camera connected on device ID: {camera_id}")
        return True

    def _release_capture_only(self):
        with self._lock:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception as e:
                    error(f"Error releasing camera capture: {e}")
                finally:
                    self.cap = None
                    self._closed = True
            else:
                self._closed = True

    def stop_capture(self):
        #debug("Stopping camera capture...")
        with self._lock:
            self.running = False
            self.exiting = True

        # Wait for thread to finish, but set timeout
        if self.isRunning():
            self.wait(2000)  # Wait up to 2 seconds

        # Release capture resources
        self._safe_release_capture()

    def _safe_release_capture(self):
        """Safely release camera capture resources with multiple safety checks"""
        try:
            with self._lock:
                if self.cap is not None:
                    try:
                        if not self._closed:
                            #debug("Releasing camera capture")
                            self.cap.release()
                    except Exception as e:
                        error(f"Error releasing camera capture: {e}")
                    finally:
                        self.cap = None
                        self._closed = True
                else:
                    # Even if cap is None, mark as closed
                    self._closed = True
        except Exception as e:
            error(f"Error in _safe_release_capture: {e}")
        finally:
            with self._lock:
                self.cap = None
                self._closed = True

    def toggle_detection(self, detecting):
        with self._lock:
            self.detecting = detecting

    def toggle_landmarks(self, show):
        with self._lock:
            self.show_landmarks = show

    def run(self):
        read_failures = 0
        while True:
            # Check exit conditions
            should_continue = False
            cap_ready = False
            with self._lock:
                should_continue = self.running and not self.exiting
                cap_ready = self.cap is not None and not self._closed
                camera_id = self.camera_id

            if not should_continue:
                break

            if not cap_ready:
                current_time = time.time()
                if current_time - self._last_reconnect_attempt >= self.reconnect_interval:
                    self._last_reconnect_attempt = current_time
                    next_camera_id = camera_id
                    if next_camera_id is None:
                        next_camera_id = self.find_available_camera()
                    if next_camera_id is not None:
                        self._open_capture(next_camera_id)
                time.sleep(0.1)
                continue

            try:
                ret, frame = None, None
                cap_valid = False
                with self._lock:
                    cap_valid = self.cap is not None and not self._closed

                if cap_valid:
                    try:
                        ret, frame = self.cap.read()
                    except Exception as e:
                        error(f"Error reading frame: {e}")
                        ret = False

                if ret and frame is not None:
                    read_failures = 0
                    # Calculate FPS
                    with self._lock:
                        self.frame_count += 1
                        current_time = time.time()
                        if current_time - self.last_fps_time >= 1.0:  # Update once per second
                            self.fps = self.frame_count / (current_time - self.last_fps_time)
                            self.frame_count = 0
                            self.last_fps_time = current_time
                    self.fps_updated.emit(self.fps)

                    processed_frame = frame.copy()
                    detection_result = {}

                    # Process frame if detection is enabled
                    detecting_enabled = False
                    with self._lock:
                        detecting_enabled = self.detecting

                    if detecting_enabled:
                        try:
                            # Detect eye state
                            detection_result = self.eye_detector.detect_eyes_state(processed_frame)

                            # Emit detection status
                            self.detection_status.emit(detection_result)

                            # When playing video, continue playing if eyes are gazing at screen,
                            command = None
                            face_detected = detection_result.get('face_detected', False)

                            if face_detected:
                                # Update last face detected time
                                with self._lock:
                                    self.last_face_detected_time = current_time

                                # Check if eyes are closed
                                eyes_closed = detection_result.get('eyes_closed', False)

                                # Check if user is gazing
                                is_gazing = detection_result.get('is_gazing', False)

                                # Pause if eyes are closed or not gazing
                                if eyes_closed or not is_gazing:
                                    command = "pause"
                                else:
                                    command = "play"
                            else:
                                # Pause video if no face detected for over 1 second
                                last_face_time = 0
                                with self._lock:
                                    last_face_time = self.last_face_detected_time
                                if current_time - last_face_time > 1.0:
                                    command = "pause"

                            # Draw landmarks (optional)
                            show_landmarks = False
                            with self._lock:
                                show_landmarks = self.show_landmarks

                            if show_landmarks and face_detected:
                                self.eye_detector.draw_landmarks(processed_frame, detection_result)

                            # Emit command signal
                            if command and command != self.last_command:
                                #debug(f"Command detected: {command}")
                                self.command_detected.emit(command)
                                with self._lock:
                                    self.last_command = command

                        except Exception as e:
                            error(f"Detection error: {e}")
                            # Emit empty status to indicate detection failure
                            self.detection_status.emit({})
                    else:
                        # If detection is disabled, emit empty status
                        self.detection_status.emit({})

                    # Emit frame ready signal
                    self.frame_ready.emit(processed_frame)

                    time.sleep(0.03)  # ~30 FPS
                else:
                    read_failures += 1
                    if read_failures >= 30:
                        error("Cannot read frame from camera, waiting for reconnect")
                        self._release_capture_only()
                        read_failures = 0
                    time.sleep(0.03)
            except Exception as e:
                error(f"Error in camera capture loop: {e}")
                break

        # Release resources when thread exits
        self._safe_release_capture()
        self.finished.emit()

    def __del__(self):
        """Ensure resources are released when object is destroyed"""
        try:
            self._safe_release_capture()
        except Exception as e :
            error(f"Error release resources: {e}")