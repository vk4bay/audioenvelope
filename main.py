import sys
import os
import time
import queue
import platform
import ctypes
import numpy as np
import sounddevice as sd
import dearpygui.dearpygui as dpg

# Optional scipy: gives a C-level biquad implementation that does not hold
# the Python GIL inside the audio callback, eliminating a major stutter source
# on Linux/macOS.  Gracefully falls back to the pure-Python loop if absent.
try:
    from scipy.signal import sosfilt as _sosfilt
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False

# Folder that contains this script — used to locate assets (logo, font)
# regardless of which directory Python is launched from.
# When frozen by PyInstaller (--onefile), files are unpacked to sys._MEIPASS.
if getattr(sys, 'frozen', False):
    _HERE = sys._MEIPASS          # PyInstaller bundle unpacks here at runtime
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))

# ── Application identity ────────────────────────────────────────────────────
APP_TITLE = "Real-Time Oscilloscope V7"   # single definition used everywhere

# ── Windows-only: monitor-aware borderless fullscreen via Win32 API ──────────
_IS_WINDOWS = platform.system() == 'Windows'

if _IS_WINDOWS:
    import ctypes.wintypes

    _GWL_STYLE           = -16
    _WS_OVERLAPPEDWINDOW = 0x00CF0000
    _WS_POPUP            = 0x80000000
    _SWP_FRAMECHANGED    = 0x0020
    _SWP_SHOWWINDOW      = 0x0040
    _MONITOR_NEAREST     = 0x00000002

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize",    ctypes.wintypes.DWORD),
            ("rcMonitor", ctypes.wintypes.RECT),
            ("rcWork",    ctypes.wintypes.RECT),
            ("dwFlags",   ctypes.wintypes.DWORD),
        ]

# Seconds of audio held in the pitch-shift ring buffer.
PITCH_BUF_SECS = 4.0

# =============================================================================
# SPECTROGRAPH CONSTANTS
# =============================================================================
# FFT window size — 4096 gives 10.8 Hz/bin resolution at 44100 Hz.
# Hop is half the window (50 % overlap) for smooth temporal resolution.
SPEC_FFT_SIZE = 4096
SPEC_HOP      = SPEC_FFT_SIZE // 2          # 2048 samples ≈ 46 ms per row
SPEC_TEX_W    = 1024                        # texture pixel columns (frequency axis)
SPEC_TEX_H    = 400                         # texture pixel rows    (time axis, history)
SPEC_DB_MIN   = -80.0                       # dB floor  (maps to colour 0)
SPEC_DB_MAX   =  20.0                       # dB ceiling (maps to colour 1)
                                            # +20 dB headroom above 0 dBFS gives
                                            # ~5× less colour sensitivity, keeping
                                            # normal speech in the blue-green range.


class AudioOscilloscopeDPG:
    def __init__(self):
        print("Initializing Audio System (V7 - Effects Edition)...")
        self.stream = None
        self.is_running = False
        self.input_gain = 500.0   # 100 % on the 0-100 slider = slider × 5 = 500× gain
        self.output_gain = 0.0    # 0 % default -- no speaker feedback at start

        # --- Oscilloscope ring buffer ---
        self.max_points = 500000
        self.audio_data = np.zeros(self.max_points, dtype=np.float32)
        self.write_pos = 0

        # Time-interpolation for smooth display scrolling
        self.stream_samplerate = 44100.0
        self.active_blocksize = 128
        self.total_samples_written = 0
        self.last_chunk_wall_time = None

        # Cached plot axis arrays
        self.last_visible_points = -1
        self.x_data_cache = None
        self.zero_line_cache = None

        self.audio_queue = queue.Queue(maxsize=100)

        # =================================================================
        # EFFECTS STATE
        # 0=Normal  1=Chipmunk  2=Voice Pitch  3=Echo  4=Robot
        # =================================================================
        self.effect_mode = 0

        # Pitch-shift ring buffer (sized properly when stream opens)
        self.pitch_ring_buf = np.zeros(0, dtype=np.float32)
        self.pitch_write_pos = 0
        self.pitch_read_frac = 0.0

        # Delay/echo ring buffer (sized at stream start for 11 s)
        self.delay_buf = np.zeros(int(44100 * 11), dtype=np.float32)
        self.delay_write_pos = 0

        # Universal output delay buffer (separate from echo buffer, always active)
        self.delay_buf2 = np.zeros(int(44100 * 11), dtype=np.float32)
        self.delay_write_pos2 = 0

        # Robot effect: carrier phase accumulator + comb-filter ring buffer
        self.robot_phase = 0.0
        self.robot_comb_buf = np.zeros(0, dtype=np.float32)
        self.robot_comb_pos = 0

        # Monitor-aware fullscreen state
        self._is_fullscreen  = False
        self._windowed_hwnd  = 0
        self._windowed_style = 0
        self._windowed_rect  = None

        # =================================================================
        # SPECTROGRAPH STATE
        # display_mode: 'scope' | 'spec'
        # =================================================================
        self.display_mode   = 'scope'
        self._spec_label_freqs = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
        self._spec_label_texts = ["50Hz","100","200","500","1k","2k","5k","10k","20k"]
        self._spec_accum    = np.zeros(0, dtype=np.float32)  # sample accumulator
        self._spec_accum_n  = 0                               # samples in accumulator
        self._hann          = np.hanning(SPEC_FFT_SIZE).astype(np.float32)

        # RGBA texture: shape (SPEC_TEX_H, SPEC_TEX_W, 4), float32 in [0,1]
        self._spec_tex      = np.zeros((SPEC_TEX_H, SPEC_TEX_W, 4), dtype=np.float32)
        self._spec_tex[:, :, 3] = 1.0   # alpha always 1

        # Flat RGBA list uploaded to DPG dynamic texture each frame
        self._spec_flat     = self._spec_tex.ravel().tolist()

        # Log-spaced frequency axis: pixel column → FFT bin index (float for interp)
        # Rebuilt when zoom sliders change.
        self._spec_freq_axis      = None   # shape (SPEC_TEX_W,) FFT bin indices
        self._spec_zoom_cache_key = None   # (freq_min, freq_max) when last built

        # =================================================================
        # HIGH-PASS FILTER STATE (2nd-order Butterworth biquad, always active)
        # Coefficients rebuilt whenever the cutoff slider changes.
        # =================================================================
        self._hpf_cutoff = 80.0          # Hz — cached for change detection
        self._hpf_coeffs = None          # (b0,b1,b2,a1,a2) normalised by a0
        self._hpf_sos    = None          # shape (1,6) SOS matrix for sosfilt (scipy)
        self._hpf_z1     = 0.0           # biquad delay element 1
        self._hpf_z2     = 0.0           # biquad delay element 2

        # Pre-allocated index array reused by delay/robot effects inside the
        # audio callback — avoids 7× np.arange heap allocations per callback.
        self._frame_indices = np.arange(128, dtype=np.intp)

        # Detect once whether DPG set_value accepts a numpy array directly
        # (avoids 15–25 ms ravel().tolist() GIL hold every texture upload).
        self._dpg_tex_as_array = None   # None = not yet detected

        # ----------------------------------------------------------------
        # Cached slider values for safe access from the audio C-thread.
        # dpg.get_value() grabs the Python GIL; calling it from PortAudio's
        # real-time thread causes priority inversion / stutters on Linux.
        # These are refreshed each render frame on the main thread instead.
        # ----------------------------------------------------------------
        self._cached_echo_delay    = 300.0
        self._cached_echo_feedback = 0.40
        self._cached_robot_freq    = 100.0
        self._cached_deep_pitch    = 0.45
        self._cached_delay_time    = 0.0

        # Limit spectrograph texture uploads to ≤30 fps to avoid spending
        # tens of milliseconds on .tolist() every render frame.
        self._spec_last_upload = 0.0

        # Fetch audio devices
        self.devices = sd.query_devices()
        self.input_devices = [
            f"{i}: {d['name']}" for i, d in enumerate(self.devices)
            if d['max_input_channels'] > 0
        ]
        self.output_devices = [
            f"{i}: {d['name']}" for i, d in enumerate(self.devices)
            if d['max_output_channels'] > 0
        ]

    # =================================================================
    # EFFECT IMPLEMENTATIONS  (NumPy only -- called from audio C-thread)
    # =================================================================

    def _apply_pitch_shift(self, signal, factor):
        frames = len(signal)
        buf = self.pitch_ring_buf
        BUF = len(buf)
        if BUF == 0:
            return signal.copy()

        wp = self.pitch_write_pos
        end = wp + frames
        if end <= BUF:
            buf[wp:end] = signal
        else:
            split = BUF - wp
            buf[wp:] = signal[:split]
            buf[:end - BUF] = signal[split:]
        self.pitch_write_pos = end % BUF

        rpos = (self.pitch_read_frac +
                np.arange(frames, dtype=np.float64) * factor) % BUF
        idx  = rpos.astype(np.int32)
        nxt  = (idx + 1) % BUF
        frac = (rpos - idx).astype(np.float32)
        out  = (buf[idx] * (1.0 - frac) + buf[nxt] * frac).astype(np.float32)

        self.pitch_read_frac = (self.pitch_read_frac + frames * factor) % BUF

        target_lag = int(self.stream_samplerate * 0.10)
        cur_lag    = int(self.pitch_write_pos - int(self.pitch_read_frac)) % BUF
        lag_min    = int(self.stream_samplerate * 0.02)
        lag_max    = int(BUF * 0.80)
        if not (lag_min <= cur_lag <= lag_max):
            self.pitch_read_frac = float(
                (self.pitch_write_pos - target_lag) % BUF)

        return out

    def _apply_echo(self, signal):
        frames = len(signal)
        buf = self.delay_buf
        buf_size = len(buf)

        delay_ms = self._cached_echo_delay
        feedback = min(self._cached_echo_feedback, 0.90)

        delay_samps = max(1, int(delay_ms * self.stream_samplerate / 1000.0))
        delay_samps = min(delay_samps, buf_size - frames - 1)

        rpos = (self.delay_write_pos - delay_samps + self._frame_indices[:frames]) % buf_size
        delayed = buf[rpos]
        output = signal + delayed * feedback

        wpos = (self.delay_write_pos + self._frame_indices[:frames]) % buf_size
        buf[wpos] = output
        self.delay_write_pos = (self.delay_write_pos + frames) % buf_size

        return np.clip(output, -1.0, 1.0)

    def _apply_pure_delay(self, signal):
        frames = len(signal)

        delay_sec = self._cached_delay_time
        if delay_sec <= 0.0:
            buf = self.delay_buf2
            buf_size = len(buf)
            wpos = (self.delay_write_pos2 + self._frame_indices[:frames]) % buf_size
            buf[wpos] = signal
            self.delay_write_pos2 = (self.delay_write_pos2 + frames) % buf_size
            return signal

        buf = self.delay_buf2
        buf_size = len(buf)
        delay_samps = min(int(delay_sec * self.stream_samplerate), buf_size - frames - 1)
        delay_samps = max(1, delay_samps)

        rpos = (self.delay_write_pos2 - delay_samps + self._frame_indices[:frames]) % buf_size
        delayed = buf[rpos]

        wpos = (self.delay_write_pos2 + self._frame_indices[:frames]) % buf_size
        buf[wpos] = signal
        self.delay_write_pos2 = (self.delay_write_pos2 + frames) % buf_size

        return delayed

    def _apply_robot(self, signal):
        frames = len(signal)
        freq = self._cached_robot_freq

        t = self._frame_indices[:frames].astype(np.float64) / self.stream_samplerate
        phase_inc = 2.0 * np.pi * freq * frames / self.stream_samplerate

        carrier1 = np.sign(np.sin(2.0 * np.pi * freq * t + self.robot_phase)).astype(np.float32)
        carrier2 = np.sign(np.sin(4.0 * np.pi * freq * t + self.robot_phase * 2.0)).astype(np.float32)
        self.robot_phase = (self.robot_phase + phase_inc) % (2.0 * np.pi)

        modulated = signal * (carrier1 + 0.30 * carrier2)

        if len(self.robot_comb_buf) > 0:
            buf  = self.robot_comb_buf
            BUF  = len(buf)
            comb_d = min(max(1, int(self.stream_samplerate / freq)), BUF - frames - 1)
            rpos   = (self.robot_comb_pos - comb_d + self._frame_indices[:frames]) % BUF
            delayed = buf[rpos]
            combed  = modulated + 0.55 * delayed
            wpos    = (self.robot_comb_pos + self._frame_indices[:frames]) % BUF
            buf[wpos] = combed
            self.robot_comb_pos = (self.robot_comb_pos + frames) % BUF
        else:
            combed = modulated

        return np.tanh(combed * 1.8).astype(np.float32) * 0.65

    # =================================================================
    # HIGH-PASS FILTER  (2nd-order Butterworth biquad)
    # =================================================================

    def _build_hpf_coeffs(self, cutoff_hz):
        """
        Compute and cache biquad HPF coefficients for the current sample rate.
        Uses the Audio EQ Cookbook formula (Butterworth, Q = 1/√2 ≈ 0.7071).
        Roll-off: 12 dB/octave — gentle enough to retain vocal fundamentals
        while attenuating sub-bass rumble and handling noise.
        Resets the filter delay elements so there is no click on update.
        """
        sr  = max(self.stream_samplerate, 8000.0)
        fc  = max(10.0, min(float(cutoff_hz), sr * 0.45))
        w0  = 2.0 * np.pi * fc / sr
        Q   = 0.7071           # Butterworth maximally-flat
        sin_w0 = np.sin(w0)
        cos_w0 = np.cos(w0)
        alpha  = sin_w0 / (2.0 * Q)

        b0 =  (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 =  (1.0 + cos_w0) / 2.0
        a0 =   1.0 + alpha
        a1 =  -2.0 * cos_w0
        a2 =   1.0 - alpha

        # Store normalised coefficients (divide through by a0)
        self._hpf_coeffs = (b0/a0, b1/a0, b2/a0, a1/a0, a2/a0)
        # Pre-build SOS matrix [b0,b1,b2,1,a1,a2] for scipy sosfilt
        self._hpf_sos    = np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]],
                                     dtype=np.float64)
        self._hpf_cutoff = cutoff_hz
        self._hpf_z1     = 0.0
        self._hpf_z2     = 0.0

    def _apply_hpf(self, signal):
        """
        Filter `signal` through the cached biquad.
        Uses scipy sosfilt (C-level, no GIL in audio callback) when available,
        otherwise falls back to a pure-Python transposed Direct Form II loop.
        """
        if self._hpf_coeffs is None:
            return signal

        if _HAVE_SCIPY and self._hpf_sos is not None:
            # sosfilt state: shape (n_sections=1, 2)
            zi = np.array([[self._hpf_z1, self._hpf_z2]], dtype=np.float64)
            out, zf = _sosfilt(self._hpf_sos, signal.astype(np.float64), zi=zi)
            self._hpf_z1 = float(zf[0, 0])
            self._hpf_z2 = float(zf[0, 1])
            return out.astype(np.float32)

        # Pure-Python fallback (no scipy)
        b0, b1, b2, a1, a2 = self._hpf_coeffs
        n   = len(signal)
        out = np.empty(n, dtype=np.float32)
        z1, z2 = self._hpf_z1, self._hpf_z2
        for i in range(n):
            x    = float(signal[i])
            y    = b0 * x + z1
            z1   = b1 * x - a1 * y + z2
            z2   = b2 * x - a2 * y
            out[i] = y
        self._hpf_z1 = z1
        self._hpf_z2 = z2
        return out

    # =================================================================
    # AUDIO CALLBACK  (hardware C-thread -- keep allocations minimal)
    # =================================================================

    def audio_callback(self, indata, outdata, frames, time, status):
        raw_in = indata[:, 0]
        processed_in = (raw_in * self.input_gain).astype(np.float32)

        # High-pass filter — always active, removes low-freq rumble/noise
        processed_in = self._apply_hpf(processed_in)

        mode = self.effect_mode
        if mode == 1:
            fx_out = self._apply_pitch_shift(processed_in, 2.0)
        elif mode == 2:
            fx_out = self._apply_pitch_shift(processed_in, self._cached_deep_pitch)
        elif mode == 3:
            fx_out = self._apply_echo(processed_in)
        elif mode == 4:
            fx_out = self._apply_robot(processed_in)
        else:
            fx_out = processed_in

        fx_out = self._apply_pure_delay(fx_out)
        outdata[:, 0] = np.clip(fx_out * self.output_gain, -1.0, 1.0)

        if self.is_running:
            try:
                self.audio_queue.put_nowait(processed_in.copy())
            except queue.Full:
                pass

    # =================================================================
    # CONTROL HELPERS
    # =================================================================

    def toggle_audio(self, sender, app_data, user_data):
        if self.stream is not None and self.stream.active:
            self.stream.stop()
            self.stream.close()
            self.stream = None
            self.is_running = False
            dpg.set_item_label("start_btn", "Start")
        else:
            in_str = dpg.get_value("in_device")
            out_str = dpg.get_value("out_device")
            if not in_str or not out_str:
                print("Error: No device selected.")
                return

            in_idx = int(in_str.split(":")[0])
            out_idx = int(out_str.split(":")[0])

            while not self.audio_queue.empty():
                self.audio_queue.get_nowait()
            self.audio_data.fill(0.0)
            self.write_pos = 0
            self.total_samples_written = 0
            self.last_chunk_wall_time = None

            # Reset spectrograph accumulator
            self._spec_accum   = np.zeros(0, dtype=np.float32)
            self._spec_accum_n = 0
            self._spec_tex.fill(0.0)
            self._spec_tex[:, :, 3] = 1.0
            self._spec_zoom_cache_key = None   # force freq-axis rebuild

            out_info = sd.query_devices(out_idx, 'output')
            sync_samplerate = out_info['default_samplerate']
            self.stream_samplerate = sync_samplerate

            self.delay_buf = np.zeros(int(sync_samplerate * 11.0), dtype=np.float32)
            self.delay_write_pos = 0
            self.delay_buf2 = np.zeros(int(sync_samplerate * 11.0), dtype=np.float32)
            self.delay_write_pos2 = 0

            buf_size = int(sync_samplerate * PITCH_BUF_SECS)
            self.pitch_ring_buf  = np.zeros(buf_size, dtype=np.float32)
            target_lag           = int(sync_samplerate * 0.10)
            self.pitch_write_pos = target_lag
            self.pitch_read_frac = 0.0

            comb_size = int(sync_samplerate / 20) * 3
            self.robot_comb_buf = np.zeros(comb_size, dtype=np.float32)
            self.robot_comb_pos = 0
            self.robot_phase = 0.0

            # Initialise spectrograph sample accumulator for new sample rate
            self._spec_accum = np.zeros(SPEC_FFT_SIZE, dtype=np.float32)
            self._spec_accum_n = 0

            # Build HPF coefficients for the confirmed sample rate
            hpf_cutoff = float(dpg.get_value("hpf_cutoff")) \
                if dpg.does_item_exist("hpf_cutoff") else self._hpf_cutoff
            self._build_hpf_coeffs(hpf_cutoff)

            try:
                # Linux ALSA needs a larger blocksize to tolerate Python GIL
                # pauses; Windows WASAPI is fine at 128.
                self.active_blocksize = 512 if platform.system() == 'Linux' else 128
                # Pre-allocate frame-index array used inside the audio callback
                # to avoid 7× np.arange() heap allocations per invocation.
                self._frame_indices = np.arange(self.active_blocksize, dtype=np.intp)
                self.stream = sd.Stream(
                    device=(in_idx, out_idx),
                    samplerate=sync_samplerate,
                    channels=1,
                    blocksize=self.active_blocksize,
                    callback=self.audio_callback
                )
                self.stream.start()
                self.is_running = True
                dpg.set_item_label("start_btn", "Stop")
            except Exception as e:
                print(f"Audio Error: {e}")

    def update_gains(self):
        # Slider 0–100; value × 5 is supplied as gain (max 500× at 100 %).
        self.input_gain  = dpg.get_value("in_gain_slider") * 5.0
        self.output_gain = dpg.get_value("out_gain_slider") / 100.0

    _FX_LABELS = ["Normal", "Chipmunk", "Voice Pitch", "Echo", "Robot"]
    _FX_TAGS   = ["fx_btn_normal", "fx_btn_chipmunk", "fx_btn_deep",
                  "fx_btn_echo",   "fx_btn_robot"]

    def set_effect(self, sender, app_data, user_data):
        self.effect_mode = user_data
        self.delay_buf.fill(0.0)
        self.delay_write_pos = 0
        if len(self.pitch_ring_buf) > 0:
            self.pitch_ring_buf.fill(0.0)
            target_lag           = int(self.stream_samplerate * 0.10)
            self.pitch_write_pos = target_lag
            self.pitch_read_frac = 0.0
        self.robot_phase = 0.0
        if len(self.robot_comb_buf) > 0:
            self.robot_comb_buf.fill(0.0)
        self.robot_comb_pos = 0
        for i, tag in enumerate(self._FX_TAGS):
            prefix = ">> " if i == user_data else "   "
            dpg.set_item_label(tag, prefix + self._FX_LABELS[i])

    def toggle_fullscreen(self):
        if not _IS_WINDOWS:
            # Linux / macOS: DearPyGui's built-in viewport fullscreen
            self._is_fullscreen = not self._is_fullscreen
            dpg.toggle_viewport_fullscreen()
            return

        # Windows: Win32 API for true monitor-aware borderless fullscreen
        u32 = ctypes.windll.user32

        if not self._is_fullscreen:
            hwnd = u32.FindWindowW(None, APP_TITLE)
            if not hwnd:
                return

            self._windowed_hwnd  = hwnd
            self._windowed_style = u32.GetWindowLongW(hwnd, _GWL_STYLE)
            rc = ctypes.wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(rc))
            self._windowed_rect = (rc.left, rc.top, rc.right, rc.bottom)

            mon = u32.MonitorFromWindow(hwnd, _MONITOR_NEAREST)
            mi  = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            u32.GetMonitorInfoW(mon, ctypes.byref(mi))
            r = mi.rcMonitor

            u32.SetWindowLongW(hwnd, _GWL_STYLE, _WS_POPUP)
            u32.SetWindowPos(hwnd, None,
                             r.left, r.top,
                             r.right - r.left, r.bottom - r.top,
                             _SWP_FRAMECHANGED | _SWP_SHOWWINDOW)
            self._is_fullscreen = True

        else:
            hwnd = self._windowed_hwnd
            u32.SetWindowLongW(hwnd, _GWL_STYLE, self._windowed_style)
            l, t, r2, b = self._windowed_rect
            u32.SetWindowPos(hwnd, None,
                             l, t, r2 - l, b - t,
                             _SWP_FRAMECHANGED | _SWP_SHOWWINDOW)
            self._is_fullscreen = False

    def _on_escape_key(self, sender, app_data, user_data):
        """Exit fullscreen when Escape is pressed."""
        if self._is_fullscreen:
            self.toggle_fullscreen()

    def _toggle_display_mode(self, sender, app_data, user_data):
        """Switch between oscilloscope and spectrograph panels."""
        if self.display_mode == 'scope':
            self.display_mode = 'spec'
            dpg.hide_item("scope_panel")
            dpg.show_item("spec_panel")
            dpg.set_item_label("disp_mode_btn", "Oscilloscope")
        else:
            self.display_mode = 'scope'
            dpg.hide_item("spec_panel")
            dpg.show_item("scope_panel")
            dpg.set_item_label("disp_mode_btn", "Spectrograph")

    # =================================================================
    # SPECTROGRAPH HELPERS
    # =================================================================

    def _build_spec_freq_axis(self, freq_min, freq_max):
        """
        Build a (SPEC_TEX_W,) array of fractional FFT bin indices corresponding
        to SPEC_TEX_W log-spaced frequencies between freq_min and freq_max Hz.
        Called once at startup and whenever the zoom sliders change.
        """
        sr   = self.stream_samplerate
        n_bins = SPEC_FFT_SIZE // 2 + 1          # rfft output length
        bin_hz = sr / SPEC_FFT_SIZE              # Hz per bin

        freq_min = max(freq_min, bin_hz)         # can't go below 1 bin
        freq_max = min(freq_max, sr / 2.0)       # can't exceed Nyquist

        log_freqs = np.logspace(np.log10(freq_min), np.log10(freq_max),
                                SPEC_TEX_W, dtype=np.float64)
        self._spec_freq_axis = log_freqs / bin_hz   # fractional bin indices
        self._spec_zoom_cache_key = (freq_min, freq_max)

    @staticmethod
    def _db_to_rgba(norm):
        """
        Map normalised amplitude (0=quiet, 1=loud) to an RGBA colour array.
        Colour ramp: black → deep-blue → cyan → green → yellow → red.
        norm: shape (N,), float32 in [0, 1].
        Returns shape (N, 4) float32.
        """
        r = np.clip(norm * 2.5 - 1.0, 0.0, 1.0)
        g = np.clip(np.where(norm < 0.5,
                             norm * 2.0,
                             2.0 - norm * 2.0), 0.0, 1.0)
        b = np.clip(1.0 - norm * 3.0, 0.0, 1.0)
        a = np.ones_like(norm)
        return np.stack([r, g, b, a], axis=-1).astype(np.float32)

    def _update_spectrograph(self, chunks, scroll_step=1):
        """
        Process a list of audio chunks into spectrograph texture rows.
        Called from update_render_loop when display_mode == 'spec'.

        scroll_step: number of texture rows to advance per FFT frame.
                     Linked to the Timebase slider so the scroll speed
                     matches the oscilloscope time window.
        """
        if self._spec_freq_axis is None:
            # Safe default if zoom sliders haven't fired yet
            self._build_spec_freq_axis(20.0, 20000.0)

        # Read persistence blend factor from slider (0=full trails, 1=no trails)
        persist = float(dpg.get_value("spec_persist")) if dpg.does_item_exist("spec_persist") else 0.85

        new_rows = 0

        for chunk in chunks:
            n = len(chunk)
            # Grow accumulator if needed (first call after start)
            if len(self._spec_accum) < SPEC_FFT_SIZE:
                self._spec_accum = np.zeros(SPEC_FFT_SIZE, dtype=np.float32)
                self._spec_accum_n = 0

            # Fill accumulator
            pos = 0
            while pos < n:
                space = SPEC_FFT_SIZE - self._spec_accum_n
                take  = min(space, n - pos)
                self._spec_accum[self._spec_accum_n:self._spec_accum_n + take] = chunk[pos:pos + take]
                self._spec_accum_n += take
                pos += take

                if self._spec_accum_n >= SPEC_FFT_SIZE:
                    # --- FFT on windowed frame ---
                    windowed = self._spec_accum * self._hann
                    spectrum = np.abs(np.fft.rfft(windowed))   # shape: (FFT/2+1,)

                    # --- Log-resample to SPEC_TEX_W columns ---
                    fa = self._spec_freq_axis   # fractional bin indices, shape (W,)
                    i0 = fa.astype(np.int32)
                    i1 = np.clip(i0 + 1, 0, len(spectrum) - 1)
                    fr = (fa - i0).astype(np.float32)
                    mag = spectrum[i0] * (1.0 - fr) + spectrum[i1] * fr

                    # --- dB with floor ---
                    # spec_gain_db shifts the colour scale independently of mic level.
                    # A fixed -20 dB is applied unconditionally so that the slider's
                    # -40 dB stop gives an effective -60 dB reference, making the
                    # spectrograph match the oscilloscope amplitude levels.
                    spec_gain_db = float(dpg.get_value("spec_gain_db")) \
                        if dpg.does_item_exist("spec_gain_db") else -20.0
                    db = 20.0 * np.log10(mag + 1e-9) + spec_gain_db - 20.0
                    norm = np.clip((db - SPEC_DB_MIN) / (SPEC_DB_MAX - SPEC_DB_MIN),
                                   0.0, 1.0).astype(np.float32)

                    # --- RGBA colour row ---
                    new_row = self._db_to_rgba(norm)   # (SPEC_TEX_W, 4)

                    # --- Persistence: blend with current top row ---
                    blended = persist * new_row + (1.0 - persist) * self._spec_tex[0]

                    # --- Scroll texture DOWN by scroll_step rows (newest at top) ---
                    step = max(1, min(scroll_step, SPEC_TEX_H - 1))
                    self._spec_tex[step:] = self._spec_tex[:-step]
                    self._spec_tex[:step] = blended   # fill top `step` rows with new data

                    new_rows += 1

                    # Advance accumulator by HOP (shift left)
                    self._spec_accum[:SPEC_FFT_SIZE - SPEC_HOP] = \
                        self._spec_accum[SPEC_HOP:SPEC_FFT_SIZE]
                    self._spec_accum_n = SPEC_FFT_SIZE - SPEC_HOP

        # Upload texture to GPU only when new rows were computed, capped at ~30 fps.
        # ravel().tolist() on a 400×1024×4 array takes ~20 ms — too expensive every frame.
        if new_rows > 0 and dpg.does_item_exist("spec_texture"):
            _upload_now = time.perf_counter()
            if _upload_now - self._spec_last_upload >= 0.033:   # ≤30 fps
                flat = self._spec_tex.ravel()
                # Detect once whether DPG accepts a numpy array directly.
                # If so we skip the 15–25 ms ravel().tolist() GIL hold entirely.
                if self._dpg_tex_as_array is None:
                    try:
                        dpg.set_value("spec_texture", flat)
                        self._dpg_tex_as_array = True
                    except Exception:
                        self._dpg_tex_as_array = False
                if self._dpg_tex_as_array:
                    dpg.set_value("spec_texture", flat)
                else:
                    self._spec_flat = flat.tolist()
                    dpg.set_value("spec_texture", self._spec_flat)
                self._spec_last_upload = _upload_now

    # =================================================================
    # RENDER LOOP
    # =================================================================

    def update_render_loop(self):
        if not self.is_running:
            return

        now = time.perf_counter()

        # Re-build HPF if the cutoff slider was moved (main-thread safe)
        if dpg.does_item_exist("hpf_cutoff"):
            _hpf_val = float(dpg.get_value("hpf_cutoff"))
            if abs(_hpf_val - self._hpf_cutoff) > 0.5:
                self._build_hpf_coeffs(_hpf_val)

        # Refresh cached slider values so the audio C-thread never calls dpg.get_value()
        if dpg.does_item_exist("fx_echo_delay"):
            self._cached_echo_delay    = float(dpg.get_value("fx_echo_delay"))
        if dpg.does_item_exist("fx_echo_feedback"):
            self._cached_echo_feedback = float(dpg.get_value("fx_echo_feedback"))
        if dpg.does_item_exist("fx_robot_freq"):
            self._cached_robot_freq    = float(dpg.get_value("fx_robot_freq"))
        if dpg.does_item_exist("fx_deep_pitch"):
            self._cached_deep_pitch    = float(dpg.get_value("fx_deep_pitch"))
        if dpg.does_item_exist("fx_delay_time"):
            self._cached_delay_time    = float(dpg.get_value("fx_delay_time"))

        # Drain the audio queue — shared by both display modes
        chunks = []
        while not self.audio_queue.empty():
            try:
                chunk = self.audio_queue.get_nowait()
                chunks.append(chunk)
                self.last_chunk_wall_time = now
            except queue.Empty:
                break

        # --- Write chunks into oscilloscope ring buffer (always, even in spec mode)
        for chunk in chunks:
            chunk_len = len(chunk)
            end = self.write_pos + chunk_len
            if end <= self.max_points:
                self.audio_data[self.write_pos:end] = chunk
            else:
                split = self.max_points - self.write_pos
                self.audio_data[self.write_pos:] = chunk[:split]
                self.audio_data[:end - self.max_points] = chunk[split:]
            self.write_pos = end % self.max_points
            self.total_samples_written += chunk_len

        if self.last_chunk_wall_time is None:
            return

        # ── OSCILLOSCOPE MODE ────────────────────────────────────────────────
        if self.display_mode == 'scope':
            elapsed = now - self.last_chunk_wall_time
            interp_samples = min(int(elapsed * self.stream_samplerate), self.active_blocksize)
            virtual_write_pos = (self.total_samples_written + interp_samples) % self.max_points

            visible_points = int(dpg.get_value("speed_slider"))
            if visible_points <= virtual_write_pos:
                visible_y_np = self.audio_data[virtual_write_pos - visible_points:virtual_write_pos]
            else:
                tail_start = self.max_points - (visible_points - virtual_write_pos)
                visible_y_np = np.concatenate([
                    self.audio_data[tail_start:],
                    self.audio_data[:virtual_write_pos]
                ])

            current_peak = np.max(np.abs(visible_y_np))
            norm_peak = min(1.0, float(current_peak))
            r = int(min(255, norm_peak * 2 * 255))
            g = int(min(255, (1.0 - norm_peak) * 2 * 255))
            b = 50

            dpg.set_value("wave_line_color",  (r, g, b, 255))
            dpg.set_value("wave_shade_color",  (r, g, b, 60))

            if visible_points != self.last_visible_points:
                self.x_data_cache    = np.arange(visible_points, dtype=np.float32)
                self.zero_line_cache = np.zeros(visible_points,  dtype=np.float32)
                self.last_visible_points = visible_points

            dpg.set_value("waveform_shade", [self.x_data_cache, visible_y_np, self.zero_line_cache])
            dpg.set_value("waveform_line",  [self.x_data_cache, visible_y_np])
            dpg.set_axis_limits("x_axis", 0, visible_points)

        # ── SPECTROGRAPH MODE ────────────────────────────────────────────────
        else:
            # Rebuild freq axis if zoom sliders changed
            if dpg.does_item_exist("spec_freq_min") and dpg.does_item_exist("spec_freq_max"):
                fmin = float(dpg.get_value("spec_freq_min"))
                fmax = float(dpg.get_value("spec_freq_max"))
                if (fmin, fmax) != self._spec_zoom_cache_key:
                    self._build_spec_freq_axis(fmin, fmax)

            # Derive scroll speed from Timebase slider:
            # small timebase → fast scroll (zoomed in), large → slow (more history).
            # step = round( (total_texture_samples) / timebase )
            # where total_texture_samples = SPEC_TEX_H * SPEC_HOP (samples to fill screen).
            timebase = float(dpg.get_value("speed_slider")) if dpg.does_item_exist("speed_slider") else SPEC_TEX_H * SPEC_HOP
            scroll_step = max(1, round(SPEC_TEX_H * SPEC_HOP / max(timebase, 1)))

            if chunks:
                self._update_spectrograph(chunks, scroll_step=scroll_step)

    # =================================================================
    # GUI
    # =================================================================

    def build_gui(self):
        print("Building GUI context (V7 - Effects Edition)...")
        dpg.create_context()
        dpg.create_viewport(title=APP_TITLE, width=1400, height=900)
        dpg.setup_dearpygui()

        with dpg.font_registry():
            font_path = os.path.join(_HERE, "NicerFont.ttf")
            try:
                my_nice_font = dpg.add_font(font_path, 20)
                dpg.bind_font(my_nice_font)
                print(f"Custom font '{font_path}' loaded.")
            except Exception as e:
                print(f"Warning: font '{font_path}' not loaded ({e}). Using default.")

        # --- BDARS logo texture --------------------------------------------------
        self._logo_w = 0
        self._logo_h = 0
        logo_path = os.path.join(_HERE, "bdars-logo.png")
        try:
            _result = dpg.load_image(logo_path)
            if _result is None:
                print(f"Warning: bdars-logo.png not found at {logo_path}")
            else:
                raw_w, raw_h, _, raw_data = _result
                max_display_w = 253
                self._logo_w = min(max_display_w, raw_w)
                self._logo_h = int(raw_h * self._logo_w / raw_w)
                with dpg.texture_registry(tag="logo_tex_registry"):
                    dpg.add_static_texture(raw_w, raw_h, raw_data, tag="bdars_logo_tex")
                print(f"BDARS logo loaded ({raw_w}×{raw_h} → displayed {self._logo_w}×{self._logo_h})")
        except Exception as e:
            print(f"Warning: bdars-logo.png could not be loaded ({e})")

        # --- Spectrograph dynamic texture (created before primary window) --------
        _spec_init = self._spec_tex.ravel().tolist()
        with dpg.texture_registry(tag="spec_tex_registry"):
            dpg.add_dynamic_texture(SPEC_TEX_W, SPEC_TEX_H, _spec_init,
                                    tag="spec_texture")

        with dpg.window(label="Oscilloscope V7", tag="Primary Window"):

            # ---- Shared control theme ----------------------------------------
            with dpg.theme() as standout_theme:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_Button,         (180,  70,  70, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,   (230, 100, 100, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,    (130,  40,  40, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_SliderGrab,       (60, 140, 220, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive,(100, 180, 250, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_FrameBg,          (35,  35,  35, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,    (50,  50,  50, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize,  1.0,                 category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_Border,           (80,  80,  80, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_Text,            (220, 220, 220, 255), category=dpg.mvThemeCat_Core)

            with dpg.theme() as start_button_theme:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_Button,         ( 70, 160,  70, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,   (100, 200, 100, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,    ( 50, 130,  50, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize,  1.0,                 category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_Border,           (80,  80,  80, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_Text,            (220, 220, 220, 255), category=dpg.mvThemeCat_Core)

            with dpg.theme() as spec_btn_theme:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_Button,         ( 20, 100, 180, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,   ( 40, 140, 220, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,    ( 10,  70, 140, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize,  1.0,                 category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_Border,           (80,  80,  80, 255), category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_Text,            (220, 220, 220, 255), category=dpg.mvThemeCat_Core)

            # Per-effect button themes
            def _mk_theme(r, g, b):
                with dpg.theme() as t:
                    with dpg.theme_component(dpg.mvAll):
                        dpg.add_theme_color(dpg.mvThemeCol_Button,
                                            (r, g, b, 255), category=dpg.mvThemeCat_Core)
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                            (min(255, r+45), min(255, g+45), min(255, b+45), 255),
                                            category=dpg.mvThemeCat_Core)
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                            (max(0, r-40), max(0, g-40), max(0, b-40), 255),
                                            category=dpg.mvThemeCat_Core)
                        dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0, category=dpg.mvThemeCat_Core)
                        dpg.add_theme_color(dpg.mvThemeCol_Border, (80, 80, 80, 255), category=dpg.mvThemeCat_Core)
                        dpg.add_theme_color(dpg.mvThemeCol_Text,  (230, 230, 230, 255), category=dpg.mvThemeCat_Core)
                return t

            t_normal   = _mk_theme( 60,  60,  60)
            t_chipmunk = _mk_theme(200, 155,  20)
            t_deep     = _mk_theme( 45,  70, 185)
            t_echo     = _mk_theme( 25, 155, 155)
            t_robot    = _mk_theme(155,  35, 155)

            # ---- Row 1: Device selectors + Timebase + HP Filter ----------------
            with dpg.group(horizontal=True, tag="ctrl_grp"):
                dpg.add_combo(self.input_devices,  label="Input Source", tag="in_device",  width=160,
                              default_value=self.input_devices[0]  if self.input_devices  else "")
                dpg.add_combo(self.output_devices, label="Output Sink",  tag="out_device", width=160,
                              default_value=self.output_devices[0] if self.output_devices else "")
                dpg.add_spacer(width=8)
                dpg.add_text("Timebase:")
                dpg.add_slider_float(label="##speed", tag="speed_slider",
                                     default_value=int(self.max_points * 0.9),
                                     min_value=500, max_value=self.max_points, width=175)
                dpg.add_spacer(width=8)
                dpg.add_text("HP Filter:")
                dpg.add_slider_float(label="##hpf_cutoff", tag="hpf_cutoff",
                                     default_value=80.0, min_value=20.0,
                                     max_value=1000.0, width=160)
            dpg.add_spacer(height=5)

            # ---- Row 2: Start | stacked-sliders | Spectrograph | Fullscreen ------
            with dpg.group(horizontal=True, tag="controls_row"):
                btn_start = dpg.add_button(label="Start", tag="start_btn",
                                           callback=self.toggle_audio, width=130, height=40)
                dpg.bind_item_theme(btn_start, start_button_theme)
                dpg.add_spacer(width=8)

                # Vertical column: three slider rows stacked inside the button height
                with dpg.group(horizontal=False):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Mic Level (%):")
                        dpg.add_slider_float(label="##in_gain", tag="in_gain_slider",
                                             default_value=100.0, min_value=0.0,
                                             max_value=100.0,
                                             callback=self.update_gains, width=180)
                        dpg.add_spacer(width=8)
                        dpg.add_text("Output Volume (%):")
                        dpg.add_slider_float(label="##out_gain", tag="out_gain_slider",
                                             default_value=0.0, max_value=100.0,
                                             callback=self.update_gains, width=140)
                    with dpg.group(horizontal=True):
                        dpg.add_text("Out Delay (s):")
                        dpg.add_slider_float(label="##delay_time", tag="fx_delay_time",
                                             default_value=0.0, min_value=0.0,
                                             max_value=10.0, width=280)

                dpg.add_spacer(width=8)
                btn_disp = dpg.add_button(label="Spectrograph", tag="disp_mode_btn",
                                          callback=self._toggle_display_mode, width=115, height=40)
                dpg.bind_item_theme(btn_disp, spec_btn_theme)
                dpg.add_spacer(width=4)
                btn_fs = dpg.add_button(label="Fullscreen", tag="fullscreen_btn",
                                        callback=self.toggle_fullscreen, width=95, height=40)

            dpg.bind_item_theme("ctrl_grp",      standout_theme)
            dpg.bind_item_theme("controls_row",  standout_theme)


            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=5)

            # =================================================================
            # FX PANEL
            # =================================================================
            with dpg.group(horizontal=True):
                dpg.add_text("Voice Effects:")
                dpg.add_spacer(width=6)
                fx_btns = [
                    dpg.add_button(label="   Normal",      tag="fx_btn_normal",
                                   callback=self.set_effect, user_data=0, width=130, height=40),
                    dpg.add_button(label="   Chipmunk",    tag="fx_btn_chipmunk",
                                   callback=self.set_effect, user_data=1, width=130, height=40),
                    dpg.add_button(label="   Voice Pitch", tag="fx_btn_deep",
                                   callback=self.set_effect, user_data=2, width=130, height=40),
                    dpg.add_button(label="   Echo",        tag="fx_btn_echo",
                                   callback=self.set_effect, user_data=3, width=130, height=40),
                    dpg.add_button(label="   Robot",       tag="fx_btn_robot",
                                   callback=self.set_effect, user_data=4, width=130, height=40),
                ]
                for btn, theme in zip(fx_btns,
                                      [t_normal, t_chipmunk, t_deep, t_echo, t_robot]):
                    dpg.bind_item_theme(btn, theme)

            dpg.add_spacer(height=6)

            with dpg.group(horizontal=True):
                dpg.add_text("Voice Pitch:")
                dpg.add_slider_float(label="##deep_pitch", tag="fx_deep_pitch",
                                     default_value=0.45, min_value=0.25, max_value=4.0, width=200)
                dpg.add_spacer(width=14)
                dpg.add_text("Echo Delay (ms):")
                dpg.add_slider_float(label="##echo_delay", tag="fx_echo_delay",
                                     default_value=300.0, min_value=30.0, max_value=1500.0, width=180)
                dpg.add_spacer(width=14)
                dpg.add_text("Echo Feedback:")
                dpg.add_slider_float(label="##echo_feedback", tag="fx_echo_feedback",
                                     default_value=0.45, min_value=0.0, max_value=0.90, width=160)
                dpg.add_spacer(width=14)
                dpg.add_text("Robot Freq (Hz):")
                dpg.add_slider_float(label="##robot_freq", tag="fx_robot_freq",
                                     default_value=100.0, min_value=20.0, max_value=400.0, width=160)


            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=6)

            # =================================================================
            # OSCILLOSCOPE PANEL  (shown by default)
            # =================================================================
            with dpg.group(tag="scope_panel"):
                with dpg.theme() as plot_theme:
                    with dpg.theme_component(dpg.mvPlot):
                        dpg.add_theme_color(dpg.mvPlotCol_PlotBg,  (10, 20, 10, 255), category=dpg.mvThemeCat_Plots)
                        dpg.add_theme_color(dpg.mvThemeCol_Border,  (80, 80, 80, 255), category=dpg.mvThemeCat_Core)
                        dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (20, 20, 20, 255), category=dpg.mvThemeCat_Core)

                with dpg.theme() as wave_shader_theme:
                    with dpg.theme_component(dpg.mvLineSeries):
                        dpg.add_theme_color(dpg.mvPlotCol_Line, (0, 255, 100, 255),
                                            category=dpg.mvThemeCat_Plots, tag="wave_line_color")
                    with dpg.theme_component(dpg.mvShadeSeries):
                        dpg.add_theme_color(dpg.mvPlotCol_Fill, (0, 255, 100, 50),
                                            category=dpg.mvThemeCat_Plots, tag="wave_shade_color")

                with dpg.plot(label="Analog Phosphor Scope", height=-1, width=-1, tag="plot_main"):
                    dpg.add_plot_axis(dpg.mvXAxis, label="Time",      tag="x_axis", no_tick_labels=True)
                    dpg.add_plot_axis(dpg.mvYAxis, label="Amplitude", tag="y_axis")
                    dpg.set_axis_limits("y_axis", -1.0, 1.0)
                    dpg.add_shade_series([], [], tag="waveform_shade", parent="y_axis")
                    dpg.add_line_series( [], [], tag="waveform_line",  parent="y_axis")

                dpg.bind_item_theme("plot_main",      plot_theme)
                dpg.bind_item_theme("waveform_line",  wave_shader_theme)
                dpg.bind_item_theme("waveform_shade", wave_shader_theme)

            # =================================================================
            # SPECTROGRAPH PANEL  (hidden by default)
            # =================================================================
            with dpg.group(tag="spec_panel", show=False):

                # Controls row: freq zoom + persistence + independent gain
                with dpg.group(horizontal=True):
                    dpg.add_text("Freq Min (Hz):")
                    dpg.add_slider_float(label="##spec_fmin", tag="spec_freq_min",
                                         default_value=20.0, min_value=20.0,
                                         max_value=2000.0, width=180,
                                         callback=lambda *_: None)
                    dpg.add_spacer(width=14)
                    dpg.add_text("Freq Max (Hz):")
                    dpg.add_slider_float(label="##spec_fmax", tag="spec_freq_max",
                                         default_value=20000.0, min_value=500.0,
                                         max_value=20000.0, width=180,
                                         callback=lambda *_: None)
                    dpg.add_spacer(width=14)
                    dpg.add_text("Persistence:")
                    dpg.add_slider_float(label="##spec_persist", tag="spec_persist",
                                         default_value=0.85, min_value=0.0,
                                         max_value=1.0, width=120)
                    dpg.add_spacer(width=14)
                    dpg.add_text("Spec Gain (dB):")
                    dpg.add_slider_float(label="##spec_gain_db", tag="spec_gain_db",
                                         default_value=-20.0, min_value=-40.0,
                                         max_value=60.0, width=140)

                dpg.add_spacer(height=6)

                # ── Frequency-axis label bar (drawlist, full-width, updated each frame)
                dpg.add_drawlist(tag="spec_freq_drawlist", width=1380, height=22)
                for _i, _txt in enumerate(self._spec_label_texts):
                    dpg.draw_text([0, 2], _txt,
                                  color=(160, 210, 160, 255), size=14,
                                  parent="spec_freq_drawlist",
                                  tag=f"spec_freq_lbl_{_i}")

                dpg.add_spacer(height=2)

                # ── Waterfall: child_window fills remaining vertical space,
                #    drawlist inside fills the child, draw_image fills the drawlist.
                with dpg.child_window(tag="spec_waterfall_win",
                                      width=-1, height=-1,
                                      border=False, no_scrollbar=True):
                    # Drawlist must have explicit pixel dimensions — auto-sizing (-1)
                    # does not propagate size to drawlists in DPG.
                    # The render loop updates width/height every frame to match the
                    # child_window's actual pixel size.
                    dpg.add_drawlist(tag="spec_drawlist", width=1380, height=600)
                    dpg.draw_image("spec_texture",
                                   pmin=[0, 0], pmax=[1380, 600],
                                   parent="spec_drawlist",
                                   tag="spec_draw_img")

        dpg.set_primary_window("Primary Window", True)

        # --- BDARS logo overlay --------------------------------------------------
        _PAD = 10
        if self._logo_w > 0:
            _init_x = dpg.get_viewport_width() - self._logo_w - _PAD * 2
            with dpg.window(tag="logo_win", no_title_bar=True, no_resize=True,
                            no_move=True, no_scrollbar=True, no_background=True,
                            no_focus_on_appearing=True,
                            pos=[_init_x, _PAD],
                            width=self._logo_w + _PAD,
                            height=self._logo_h + _PAD):
                dpg.add_image("bdars_logo_tex",
                              width=self._logo_w, height=self._logo_h)

        dpg.show_viewport()

        # Register global keyboard handler — Escape cancels fullscreen
        with dpg.handler_registry():
            dpg.add_key_release_handler(dpg.mvKey_Escape,
                                        callback=self._on_escape_key)

        # Build initial freq axis (default full range, sr 44100)
        self._build_spec_freq_axis(20.0, 20000.0)

        _frame_budget = 1.0 / 60.0
        _LOGO_PAD = 10

        print("Entering main render loop...")
        while dpg.is_dearpygui_running():
            _t0 = time.perf_counter()

            if self._logo_w > 0 and dpg.does_item_exist("logo_win"):
                _vp_w = dpg.get_viewport_client_width()
                dpg.set_item_pos("logo_win",
                                 [_vp_w - self._logo_w - _LOGO_PAD * 2, _LOGO_PAD])

            # ── Spectrograph dynamic sizing (runs every frame when panel visible) ──
            if self.display_mode == 'spec':
                # Resize drawlist and draw_image to fill the child_window
                if dpg.does_item_exist("spec_waterfall_win") and dpg.does_item_exist("spec_draw_img"):
                    try:
                        rect = dpg.get_item_rect_size("spec_waterfall_win")
                        dw, dh = int(rect[0]), int(rect[1])
                        if dw > 1 and dh > 1:
                            dpg.configure_item("spec_drawlist", width=dw, height=dh)
                            dpg.configure_item("spec_draw_img", pmax=[dw, dh])
                    except Exception:
                        pass

                # Reposition frequency-axis labels at log-spaced pixel positions
                if dpg.does_item_exist("spec_freq_drawlist"):
                    try:
                        # Use the waterfall child_window width as the reference pixel width
                        if dpg.does_item_exist("spec_waterfall_win"):
                            rect = dpg.get_item_rect_size("spec_waterfall_win")
                        else:
                            rect = dpg.get_item_rect_size("spec_freq_drawlist")
                        lw = int(rect[0]) if rect else 0
                        # Also keep the freq drawlist in sync with actual width
                        if lw > 4:
                            dpg.configure_item("spec_freq_drawlist", width=lw)
                            fmin = float(dpg.get_value("spec_freq_min")) \
                                if dpg.does_item_exist("spec_freq_min") else 20.0
                            fmax = float(dpg.get_value("spec_freq_max")) \
                                if dpg.does_item_exist("spec_freq_max") else 20000.0
                            log_total = np.log10(max(fmax, fmin + 1) / max(fmin, 1.0))
                            for _i, (_freq, _txt) in enumerate(
                                    zip(self._spec_label_freqs, self._spec_label_texts)):
                                _tag = f"spec_freq_lbl_{_i}"
                                if dpg.does_item_exist(_tag):
                                    if fmin <= _freq <= fmax and log_total > 0:
                                        _x = int(np.log10(_freq / fmin) / log_total * lw)
                                        _x = max(0, min(_x, lw - 38))
                                        dpg.configure_item(_tag, pos=[_x, 2],
                                                           color=(160, 210, 160, 255))
                                    else:
                                        dpg.configure_item(_tag, color=(0, 0, 0, 0))
                    except Exception:
                        pass

            self.update_render_loop()
            dpg.render_dearpygui_frame()
            _elapsed = time.perf_counter() - _t0
            if _elapsed < _frame_budget:
                time.sleep(_frame_budget - _elapsed)

        print("Render loop exited. Cleaning up...")
        if self.stream:
            self.stream.stop()
            self.stream.close()
        dpg.destroy_context()


if __name__ == "__main__":
    print("Script started.")
    app = AudioOscilloscopeDPG()
    app.build_gui()