import time
import os
import threading
import subprocess
import signal
import av
from PySide6.QtCore import QThread, Signal
from log import debug, error

class VideoPlayerThread(QThread):
    """Stable video player thread using PyAV with proper synchronization"""
    frame_ready = Signal(object)
    playback_finished = Signal()
    video_info_ready = Signal(dict)
    
    def __init__(self):
        super().__init__()
        self.container = None
        self.video_stream = None
        self.current_file = ""
        self.playing = False
        self.paused = False
        self.stopped = True
        self.video_fps = 30
        self.total_frames = 0
        self.video_width = 0
        self.video_height = 0
        self.video_duration = 0
        self.exiting = False
        self.current_frame = 0
        self._lock = threading.RLock()
        
        # Time management
        self.play_start_time = 0  # When playback started
        self.accumulated_pause_time = 0  # Total time spent in pause
        self.last_pause_start = 0  # When current pause started
        self.base_timestamp = 0  # Video timestamp at start of playback
        
        # Frame decoding
        self.codec_context = None
        
        # Audio player process
        self.audio_process = None
        self.audio_process_start_time = 0
        self._pause_position = 0
        
        # Seeking
        self.seek_requested = False
        self.seek_target = 0  # Target frame number
        self.seek_timestamp = 0  # Target timestamp in seconds
        
        # For debugging
        self.last_frame_time = 0
        self.frame_count = 0

    def load_video(self, file_path):
        """Load video file using PyAV"""
        try:
            with self._lock:
                # Clean up existing resources
                self._cleanup_resources()
                
                # Open video file
                self.container = av.open(file_path)
                if not self.container:
                    error(f"Failed to open video file: {file_path}")
                    return False
                
                # Find video stream
                self.video_stream = None
                for stream in self.container.streams:
                    if stream.type == 'video':
                        self.video_stream = stream
                        break
                
                if not self.video_stream:
                    error(f"No video stream found in {file_path}")
                    return False
                
                self.current_file = file_path
                
                # Get video properties
                self.video_fps = float(self.video_stream.average_rate) if self.video_stream.average_rate else 30
                
                # Get duration
                if self.video_stream.duration:
                    time_base = float(self.video_stream.time_base)
                    self.video_duration = self.video_stream.duration * time_base
                elif self.container.duration:
                    self.video_duration = self.container.duration / av.time_base
                else:
                    # Estimate from frames if available
                    self.video_duration = 0
                
                # Estimate total frames
                if self.video_fps > 0 and self.video_duration > 0:
                    self.total_frames = int(self.video_duration * self.video_fps)
                else:
                    self.total_frames = 0
                
                # Get frame dimensions
                self.video_width = self.video_stream.width
                self.video_height = self.video_stream.height
                
                # Get codec context
                self.codec_context = self.video_stream.codec_context
                
                # Reset state
                self.playing = False
                self.paused = False
                self.stopped = True
                self.current_frame = 0
                self._pause_position = 0
                self.play_start_time = 0
                self.accumulated_pause_time = 0
                self.last_pause_start = 0
                self.base_timestamp = 0
                self.frame_count = 0
                self.last_frame_time = 0
                
                #debug(f"Loaded video: {os.path.basename(file_path)}, "
                    #   f"{self.video_width}x{self.video_height}, "
                    #   f"{self.video_fps:.2f} fps, "
                    #   f"{self.video_duration:.2f}s")
                
            # Prepare video information
            video_info = {
                'file_path': file_path,
                'filename': os.path.basename(file_path),
                'width': self.video_width,
                'height': self.video_height,
                'fps': self.video_fps,
                'total_frames': self.total_frames,
                'duration': self.video_duration
            }
            
            self.video_info_ready.emit(video_info)
            return True
            
        except Exception as e:
            error(f"Failed to load video with PyAV: {e}")
            return False
    
    def _cleanup_resources(self):
        """Clean up all video resources"""
        try:
            # Stop audio
            self._stop_audio_process()
            
            # Close container
            if self.container:
                try:
                    self.container.close()
                except Exception as e:
                    error(f"Error closing container: {e}")
                finally:
                    self.container = None
                    self.video_stream = None
                    self.codec_context = None
                    
        except Exception as e:
            error(f"Error in cleanup: {e}")
    
    def _stop_audio_process(self):
        """Safely stop audio process"""
        if self.audio_process:
            try:
                if self.audio_process.poll() is None:
                    os.killpg(os.getpgid(self.audio_process.pid), signal.SIGTERM)
                    try:
                        self.audio_process.wait(timeout=2)  # Increase timeout period
                    except subprocess.TimeoutExpired:
                        os.killpg(os.getpgid(self.audio_process.pid), signal.SIGKILL)
                    #debug("Audio process terminated")
            except Exception as e:
                error(f"Error stopping audio process: {e}")
            finally:
                self.audio_process = None
    
    def _check_audio_device_status(self):
        """Check if audio devices are available"""
        try:
            result = subprocess.run(['pactl', 'list', 'sinks'], 
                                    stdout=subprocess.DEVNULL, 
                                    stderr=subprocess.DEVNULL, 
                                    timeout=2)
            return result.returncode == 0
        except:
            try:
                result = subprocess.run(['pulseaudio', '--check'], 
                                        stdout=subprocess.DEVNULL, 
                                        stderr=subprocess.DEVNULL)
                return result.returncode == 0
            except:
                return False
    
    def _get_current_volume(self):
        """Get current system volume percentage"""
        try:
            result = subprocess.run(['pactl', 'get-sink-volume', '@DEFAULT_SINK@'], 
                                    stdout=subprocess.PIPE, 
                                    stderr=subprocess.DEVNULL, 
                                    text=True, 
                                    timeout=1)
            if result.returncode == 0:
                import re
                match = re.search(r'(\d+)%', result.stdout)
                if match:
                    return int(match.group(1))
        except Exception as e:
            error(f"Failed to get current volume: {e}")
        return 100
    
    def _start_audio(self, start_time=0):
        """Start audio playback"""
        if not self.container:
            return
            
        # Check audio device
        if not self._check_audio_device_status():
            error("Audio device not available")
            return
            
        try:
            # Stop existing audio
            self._stop_audio_process()
            
            # Use ffplay for audio (more reliable than paplay)
            cmd = [
                'ffplay',
                '-nodisp',  # No video display
                '-autoexit',  # Exit when audio ends
                '-loglevel', 'quiet',  # Suppress output
                '-ss', str(start_time),  # Start position
                '-i', self.current_file
            ]
            
            self.audio_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            
            self.audio_process_start_time = time.time()
            #debug(f"Started audio playback at {start_time:.2f}s")
            
        except Exception as e:
            error(f"Failed to start audio: {e}")
    
    def _get_frame_at_time(self, target_time):
        """Get frame at specific time with error handling"""
        if not self.container or not self.video_stream:
            return None
            
        try:
            # Seek to the target time
            self.container.seek(int(target_time * 1000000))
            
            # Decode frames until we reach target time
            for packet in self.container.demux(video=0):
                for frame in packet.decode():
                    if frame.pts is not None:
                        frame_time = frame.pts * self.video_stream.time_base
                        if float(frame_time) >= target_time:
                            # Convert to numpy array
                            rgb_frame = frame.to_ndarray(format='rgb24')
                            # Convert RGB to BGR for OpenCV
                            bgr_frame = rgb_frame[:, :, ::-1]
                            return bgr_frame
            
        except Exception as e:
            error(f"Error getting frame at time {target_time}: {e}")
            
        return None
    
    def _get_next_frame_sequence(self):
        """Get the next frame in sequence (generator)"""
        if not self.container or not self.video_stream:
            return
            
        try:
            for packet in self.container.demux(video=0):
                if packet.stream.type != 'video':
                    continue
                    
                for frame in packet.decode():
                    if frame.pts is not None:
                        # Convert to numpy array
                        rgb_frame = frame.to_ndarray(format='rgb24')
                        # Convert RGB to BGR for OpenCV
                        bgr_frame = rgb_frame[:, :, ::-1]
                        
                        # Calculate frame time
                        frame_time = frame.pts * self.video_stream.time_base
                        
                        yield bgr_frame, float(frame_time)
                        
        except Exception as e:
            error(f"Error in frame sequence: {e}")
    
    def play(self):
        """Start playback"""
        with self._lock:
            if self.stopped:
                # Starting from beginning or paused position
                self.play_start_time = time.time() - self._pause_position
                self.base_timestamp = self._pause_position
            elif self.paused:
                # Resuming from pause
                self.accumulated_pause_time += (time.time() - self.last_pause_start)
                self.play_start_time = time.time() - self.base_timestamp - self.accumulated_pause_time
            
            self.playing = True
            self.paused = False
            self.stopped = False
            
            # Start audio if available
            if self.container:
                has_audio = any(stream.type == 'audio' for stream in self.container.streams)
                if has_audio:
                    self._start_audio(self.base_timestamp)
            
            #debug(f"Play started at position: {self.base_timestamp:.2f}s")
    
    def pause(self):
        """Pause playback"""
        with self._lock:
            if self.playing and not self.stopped:
                # Calculate current position
                current_time = time.time()
                elapsed = current_time - self.play_start_time - self.accumulated_pause_time
                self.base_timestamp = max(0, min(elapsed, self.video_duration))
                
                # Store pause position
                self._pause_position = self.base_timestamp
                self.last_pause_start = current_time
                
                # Stop audio
                self._stop_audio_process()
            
            self.paused = True
            self.playing = False
            #debug(f"Playback paused at position: {self.base_timestamp:.2f}s")
    
    def stop(self):
        """Stop playback"""
        with self._lock:
            self.playing = False
            self.paused = False
            self.stopped = True
            self.current_frame = 0
            self._pause_position = 0
            self.play_start_time = 0
            self.accumulated_pause_time = 0
            self.last_pause_start = 0
            self.base_timestamp = 0
            self.frame_count = 0
            
            self._stop_audio_process()
            #debug("Playback stopped")
    
    def get_position(self):
        """Get current playback position (0.0 to 1.0)"""
        with self._lock:
            if self.video_duration > 0:
                if self.playing:
                    current_time = time.time()
                    elapsed = current_time - self.play_start_time - self.accumulated_pause_time
                    position = min(elapsed / self.video_duration, 1.0)
                    return position
                else:
                    return min(self.base_timestamp / self.video_duration, 1.0)
        return 0.0
    
    def seek(self, frame_number):
        """Seek to specific frame"""
        with self._lock:
            if self.total_frames <= 0:
                return
                
            frame_number = max(0, min(frame_number, self.total_frames - 1))
            self.seek_requested = True
            self.seek_target = frame_number
            
            # Calculate timestamp for seek
            if self.video_fps > 0:
                self.seek_timestamp = frame_number / self.video_fps
            else:
                self.seek_timestamp = 0
            
            # Update current position
            self.base_timestamp = self.seek_timestamp
            self.current_frame = frame_number
            
            # Reset timing
            if self.playing:
                self.play_start_time = time.time() - self.seek_timestamp
                self.accumulated_pause_time = 0
                
                # Restart audio at new position
                self._stop_audio_process()
                self._start_audio(self.seek_timestamp)
            else:
                self._pause_position = self.seek_timestamp
            
            #debug(f"Seek to frame {frame_number}, time: {self.seek_timestamp:.2f}s")
    
    def run(self):
        """Main playback loop with precise timing"""
        frame_generator = None
        current_frame_time = 0
        
        while not self.exiting:
            # Check state
            with self._lock:
                playing = self.playing
                paused = self.paused
                stopped = self.stopped
                seek_requested = self.seek_requested
                seek_timestamp = self.seek_timestamp
                
            if stopped or not playing or paused:
                time.sleep(0.01)
                continue
                
            if not self.container or not self.video_stream:
                time.sleep(0.01)
                continue
            
            # Handle seeking
            if seek_requested:
                with self._lock:
                    self.seek_requested = False
                
                # Reset generator
                frame_generator = None
                
                # Get frame at seek position
                frame = self._get_frame_at_time(seek_timestamp)
                if frame is not None:
                    self.frame_ready.emit(frame)
                    self.frame_count += 1
                
                # Calculate frame time for synchronization
                with self._lock:
                    current_frame_time = self.base_timestamp
                    self.last_frame_time = time.time()
                
                # Continue to normal playback
                continue
            
            # Initialize frame generator if needed
            if frame_generator is None:
                # Seek to current position and start generator
                try:
                    self.container.seek(int(current_frame_time * 1000000))
                    frame_generator = self._get_next_frame_sequence()
                except Exception as e:
                    error(f"Error initializing frame generator: {e}")
                    time.sleep(0.01)
                    continue
            
            # Calculate target time
            current_time = time.time()
            with self._lock:
                target_time = current_time - self.play_start_time - self.accumulated_pause_time
            
            # Limit target time to video duration
            target_time = max(0, min(target_time, self.video_duration))
            
            # Check if we need a new frame
            if target_time > current_frame_time + (1.0 / self.video_fps):
                try:
                    # Get next frame from generator
                    frame, frame_time = next(frame_generator)
                    
                    if frame is not None:
                        # Emit frame
                        self.frame_ready.emit(frame)
                        self.frame_count += 1
                        
                        # Update current position
                        with self._lock:
                            current_frame_time = frame_time
                            self.current_frame = int(frame_time * self.video_fps)
                            self.base_timestamp = frame_time
                        
                        # Check if we've reached the end
                        if frame_time >= self.video_duration - (1.0 / self.video_fps):
                            with self._lock:
                                self.playing = False
                                self.stopped = True
                                self.playback_finished.emit()
                                self._stop_audio_process()
                            #debug("Playback finished")
                            continue
                
                except StopIteration:
                    # End of video
                    with self._lock:
                        self.playing = False
                        self.stopped = True
                        self.playback_finished.emit()
                        self._stop_audio_process()
                    #debug("Playback finished (end of stream)")
                    continue
                except Exception as e:
                    error(f"Error getting frame: {e}")
                    # Reset generator and try again
                    frame_generator = None
                    time.sleep(0.01)
                    continue
            
            # Sleep to maintain frame rate
            if self.video_fps > 0:
                sleep_time = max(0.001, (1.0 / self.video_fps) - 0.005)  # Slightly faster than frame rate
                time.sleep(sleep_time)
            else:
                time.sleep(0.033)
        
        #debug("Video player thread exited")
    
    def shutdown(self):
        """Safely shut down thread"""
        #debug("Shutting down video player thread")
        with self._lock:
            self.exiting = True
            self.playing = False
            self.paused = False
            self.stopped = True
            
            # Ensure thread is stopped before cleaning up
            self._stop_audio_process()
            self._cleanup_resources()
            
            # Wait briefly to allow operations to finish
            time.sleep(0.1)
