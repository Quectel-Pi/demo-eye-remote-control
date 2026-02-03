import cv2
import numpy as np
from collections import deque
import time
import mediapipe as mp
from log import error

class MediaPipeEyeDetector:
    def __init__(self):
        # Initialize MediaPipe Face Mesh
        self.mp_face_mesh = mp.solutions.face_mesh
        self.face_mesh = self.mp_face_mesh.FaceMesh(
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5
        )        
        
        # Indices of 6 key points for standard EAR calculation
        self.LEFT_EYE_INDICES = [33, 159, 158, 133, 153, 145]  # Order: p1, p2, p3, p4, p5, p6
        self.RIGHT_EYE_INDICES = [362, 386, 385, 263, 380, 374]  # Order: p1, p2, p3, p4, p5, p6
        
        # Nose key point indices (for head stability detection)
        self.NOSE_INDICES = [1, 4, 6, 168, 197, 195, 5]
        
        # Configuration parameters
        self.GAZING_STABILITY_THRESHOLD = 35  # Gaze stability threshold
        self.GAZING_CONFIRMATION_FRAMES = 12  # Continuous frames required to confirm gaze (lower requirement)
        self.GAZING_BREAK_FRAMES = 15  # Continuous unstable frames required to break gaze (higher requirement)
        
        self.EAR_BLINK_THRESHOLD = 0.18  # Blink threshold
        self.EAR_OPEN_THRESHOLD = 0.25  # Threshold for fully open eyes
        
        # Blink detection parameters
        self.BLINK_FRAME_THRESHOLD = 4  # Blink duration threshold (in frames)
        
        # Data cache
        self.face_position_history = deque(maxlen=25)
        self.left_ear_history = deque(maxlen=40)
        self.right_ear_history = deque(maxlen=40)
        self.eyes_state_history = deque(maxlen=30)
        self.nose_position_history = deque(maxlen=25)
        
        # Eye state tracking
        self.eye_state = "open"
        self.blink_counter = 0
        self.closed_counter = 0
        self.in_blink_phase = False  # Whether in blinking phase
        
        # Gaze state tracking
        self.gazing_state = "not_gazing"  # not_gazing, transitioning, gazing
        self.gazing_confirm_counter = 0
        self.gazing_break_counter = 0
        
        # FPS calculation related
        self.frame_count = 0
        self.start_time = time.time()
        self.fps = 0
        self._closed = False  # Track whether resources have been released
    
    
    def close(self):
        """Release MediaPipe resources"""
        try:
            if self.face_mesh and not self._closed:
                self.face_mesh.close()
                self._closed = True
        except Exception as e:
            error(f"Error releasing MediaPipe resources: {e}")
        finally:
            self._closed = True
            
    def __del__(self):
        """Ensure resources are released when object is destroyed"""
        self.close()
            
    def calculate_ear(self, eye_landmarks):
        """Calculate Eye Aspect Ratio (EAR) using standard 6-point method"""
        p1, p2, p3, p4, p5, p6 = eye_landmarks
        
        A = np.linalg.norm(p2 - p6)
        B = np.linalg.norm(p3 - p5)
        C = np.linalg.norm(p1 - p4)
        
        if C == 0:
            return 0.0
        
        ear = (A + B) / (2.0 * C)
        return ear
    
    def calculate_position_variance(self, position_history):
        """Calculate variance of position history"""
        if len(position_history) < 5:
            return 1000  # Return a large value to indicate instability
        
        positions = [pos for pos, _ in position_history]
        x_variance = np.var([p[0] for p in positions])
        y_variance = np.var([p[1] for p in positions])
        return x_variance + y_variance
    
    def update_eye_state(self, avg_ear):
        """Update eye state machine"""
        # State transition logic
        if self.eye_state == "open":
            if avg_ear < self.EAR_BLINK_THRESHOLD:
                self.eye_state = "closing"
                self.blink_counter = 1
                self.closed_counter = 0
                self.in_blink_phase = True
                
        elif self.eye_state == "closing":
            if avg_ear < self.EAR_BLINK_THRESHOLD:
                self.blink_counter += 1
                if self.blink_counter > self.BLINK_FRAME_THRESHOLD:
                    self.eye_state = "closed"
                    self.closed_counter = self.blink_counter
            else:
                self.eye_state = "open"
                self.blink_counter = 0
                self.in_blink_phase = False
                
        elif self.eye_state == "closed":
            if avg_ear > self.EAR_OPEN_THRESHOLD:
                self.eye_state = "opening"
                self.blink_counter = 0
            else:
                self.closed_counter += 1
                
        elif self.eye_state == "opening":
            if avg_ear > self.EAR_OPEN_THRESHOLD:
                self.blink_counter -= 1
                if self.blink_counter <= 0:
                    self.eye_state = "open"
                    self.blink_counter = 0
                    self.closed_counter = 0
                    self.in_blink_phase = False
            else:
                self.eye_state = "closed"
                
        return self.eye_state
    
    def update_gazing_state(self, position_variance):
        """Update gaze state machine based on position variance"""
        is_stable = position_variance < self.GAZING_STABILITY_THRESHOLD
        
        if self.gazing_state == "not_gazing":
            if is_stable:
                self.gazing_confirm_counter += 1
                self.gazing_break_counter = 0
                
                if self.gazing_confirm_counter >= self.GAZING_CONFIRMATION_FRAMES:
                    self.gazing_state = "gazing"
                    self.gazing_confirm_counter = 0
            else:
                self.gazing_confirm_counter = 0
                self.gazing_state = "not_gazing"
                
        elif self.gazing_state == "gazing":
            if not is_stable:
                self.gazing_break_counter += 1
                self.gazing_confirm_counter = 0
                
                if self.gazing_break_counter >= self.GAZING_BREAK_FRAMES:
                    self.gazing_state = "not_gazing"
                    self.gazing_break_counter = 0
            else:
                self.gazing_break_counter = 0
                self.gazing_state = "gazing"
        
        return self.gazing_state
    
    def detect_eyes_state(self, frame):
        """Detect eye state using MediaPipe"""
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Calculate FPS
        self.frame_count += 1
        if self.frame_count % 10 == 0:
            elapsed_time = time.time() - self.start_time
            self.fps = self.frame_count / elapsed_time if elapsed_time > 0 else 0
            self.start_time = time.time()
            self.frame_count = 0
        
        detection_result = {
            'face_detected': False,
            'eyes_closed': False,
            'is_blinking': False,
            'is_short_blink': False,  # Whether it's a short blink (should be ignored during gaze)
            'eye_state': 'unknown',
            'is_gazing': False,
            'gazing_state': 'not_gazing',
            'left_ear': 0,
            'right_ear': 0,
            'avg_ear': 0,
            'eye_center': None,
            'position_variance': 1000,
            'fps': self.fps
        }
        
        # Process frame
        try:
            results = self.face_mesh.process(rgb_frame)
        except Exception as e:
            error(f"Error processing frame with MediaPipe: {e}")
            return detection_result
        
        if not results.multi_face_landmarks:
            self.eye_state = "open"
            self.blink_counter = 0
            self.closed_counter = 0
            self.in_blink_phase = False
            self.gazing_state = "not_gazing"
            self.gazing_confirm_counter = 0
            self.gazing_break_counter = 0
            
            detection_result['eyes_closed'] = True
            detection_result['eye_state'] = 'no_face'
            detection_result['gazing_state'] = 'not_gazing'
            return detection_result
        
        detection_result['face_detected'] = True
        
        # Get landmarks of the first face
        face_landmarks = results.multi_face_landmarks[0]
        
        # Extract eye landmark coordinates
        h, w = frame.shape[:2]
        left_eye_points = []
        right_eye_points = []
        nose_points = []
        
        # Left eye landmarks
        for idx in self.LEFT_EYE_INDICES:
            landmark = face_landmarks.landmark[idx]
            x, y = int(landmark.x * w), int(landmark.y * h)
            left_eye_points.append(np.array([x, y]))
        
        # Right eye landmarks
        for idx in self.RIGHT_EYE_INDICES:
            landmark = face_landmarks.landmark[idx]
            x, y = int(landmark.x * w), int(landmark.y * h)
            right_eye_points.append(np.array([x, y]))
        
        # Nose landmarks
        for idx in self.NOSE_INDICES:
            landmark = face_landmarks.landmark[idx]
            x, y = int(landmark.x * w), int(landmark.y * h)
            nose_points.append(np.array([x, y]))
        
        # Calculate Eye Aspect Ratio
        left_ear = self.calculate_ear(left_eye_points)
        right_ear = self.calculate_ear(right_eye_points)
        avg_ear = (left_ear + right_ear) / 2.0
        
        detection_result['left_ear'] = left_ear
        detection_result['right_ear'] = right_ear
        detection_result['avg_ear'] = avg_ear
        
        # Record historical EAR values
        self.left_ear_history.append(left_ear)
        self.right_ear_history.append(right_ear)
        
        # Update eye state machine
        eye_state = self.update_eye_state(avg_ear)
        detection_result['eye_state'] = eye_state
        
        # Determine if it's a blink (short-term eye closure)
        is_blinking = False
        is_short_blink = False
        
        if eye_state == "closed":
            # Determine if it's a short blink (duration within threshold)
            if self.closed_counter <= self.BLINK_FRAME_THRESHOLD:
                is_short_blink = True
                is_blinking = True
                # For short blinks, don't consider eyes as closed
                detection_result['eyes_closed'] = False
            else:
                # Long closure, consider eyes as closed
                detection_result['eyes_closed'] = True
        elif eye_state == "closing":
            if (self.blink_counter <= self.BLINK_FRAME_THRESHOLD and 
                self.in_blink_phase):
                is_blinking = True
                if self.blink_counter <= 3:  # Very short eye closure
                    is_short_blink = True
            # During closing phase, eyes are not yet fully closed
            detection_result['eyes_closed'] = False
        else:
            detection_result['eyes_closed'] = False
        
        detection_result['is_blinking'] = is_blinking
        detection_result['is_short_blink'] = is_short_blink
        
        # Record eye state history
        self.eyes_state_history.append(eye_state)
        
        # Calculate eye center position
        left_eye_center = np.mean(left_eye_points, axis=0)
        right_eye_center = np.mean(right_eye_points, axis=0)
        eye_center = ((left_eye_center + right_eye_center) / 2).astype(int)
        detection_result['eye_center'] = (int(eye_center[0]), int(eye_center[1]))
        
        # Calculate nose center position
        nose_center = np.mean(nose_points, axis=0).astype(int)
        
        # Record position history
        current_time = time.time()
        self.face_position_history.append((tuple(eye_center), current_time))
        self.nose_position_history.append((tuple(nose_center), current_time))
        
        # Calculate position variance
        if len(self.face_position_history) >= 5 and len(self.nose_position_history) >= 5:
            eye_variance = self.calculate_position_variance(self.face_position_history)
            nose_variance = self.calculate_position_variance(self.nose_position_history)
            position_variance = (eye_variance + nose_variance) / 2.0
            detection_result['position_variance'] = position_variance
            
            # Update gaze state (simplified version, does not consider blinking)
            gazing_state = self.update_gazing_state(position_variance)
            detection_result['gazing_state'] = gazing_state
            detection_result['is_gazing'] = (gazing_state == "gazing")
        else:
            detection_result['is_gazing'] = False
            detection_result['gazing_state'] = "not_gazing"
        
        return detection_result
    
    def draw_landmarks(self, frame, detection_result):
        """Draw landmarks and information on the frame"""
        if detection_result['eye_center']:
            center_x, center_y = detection_result['eye_center']
            
            # Draw gaze state visualization
            if detection_result['is_gazing']:
                # Green circle indicates gaze state
                cv2.circle(frame, (center_x, center_y), 30, (0, 255, 0), 3)
                cv2.putText(frame, "GAZING", (center_x - 40, center_y - 40),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            else:
                # Red circle indicates not gazing
                cv2.circle(frame, (center_x, center_y), 30, (0, 0, 255), 2)
        
        # Display EAR value and eye state
        if detection_result['left_ear'] > 0 and detection_result['right_ear'] > 0:
            # Display average EAR
            cv2.putText(frame, f"EAR: {detection_result['avg_ear']:.3f}", (10, frame.shape[0] - 150),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            
            # Display eye state
            state = detection_result['eye_state']
            if state == "open":
                status_color = (0, 255, 0)
                status_text = "Open"
            elif state == "closing":
                status_color = (0, 165, 255)
                status_text = "Closing"
            elif state == "closed":
                status_color = (0, 0, 255)
                status_text = "Closed"
            elif state == "opening":
                status_color = (255, 255, 0)
                status_text = "Opening"
            else:
                status_color = (255, 255, 255)
                status_text = state
            
            cv2.putText(frame, f"Eyes: {status_text}", (10, frame.shape[0] - 120),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, status_color, 2)
            
            # Display gaze state
            gazing_state = detection_result['gazing_state']
            if gazing_state == "gazing":
                gaze_color = (0, 255, 0)
                gaze_text = "GAZING"
                # In gaze state, display video playing status
                cv2.putText(frame, "VIDEO: PLAYING", (frame.shape[1] - 200, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                # In gaze state, blinking does not pause video
                if detection_result['is_blinking']:
                    cv2.putText(frame, "BLINK (GAZING)", (frame.shape[1] - 200, 60),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)
            else:
                gaze_color = (0, 0, 255)
                gaze_text = "NOT GAZING"
                # In non-gaze state, display video paused status
                cv2.putText(frame, "VIDEO: PAUSED", (frame.shape[1] - 200, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                # In non-gaze state, blinking causes video to pause
                if detection_result['is_blinking']:
                    cv2.rectangle(frame, (frame.shape[1] - 200, 60), (frame.shape[1] - 10, 100), (0, 0, 255), -1)
                    cv2.putText(frame, "BLINK (PAUSED)", (frame.shape[1] - 190, 90),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            
            cv2.putText(frame, f"Gaze: {gaze_text}", (10, frame.shape[0] - 90),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, gaze_color, 2)
            
            # Display gaze counter (for debugging)
            cv2.putText(frame, f"Gaze Confirm: {self.gazing_confirm_counter}/{self.GAZING_CONFIRMATION_FRAMES}", 
                       (10, frame.shape[0] - 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            
            # Display FPS
            # cv2.putText(frame, f"FPS: {detection_result['fps']:.1f}", (10, 30),
            #            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)