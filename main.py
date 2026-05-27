import sys
import os
import time
import queue
import ctypes
import ctypes.wintypes
import numpy as np
import sounddevice as sd
import dearpygui.dearpygui as dpg

# Folder that contains this script — used to locate assets (logo, font)
# regardless of which directory Python is launched from.
_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Windows API constants for monitor-aware fullscreen ──────────────────────
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
# 4 s gives plenty of headroom for extreme pitch factors before drift correction.
PITCH_BUF_SECS = 4.0


class AudioOscilloscopeDPG:
    def __init__(self):
        print("Initializing Audio System (V5 - Effects Edition)...")
        self.stream = None
        self.is_running = False
        self.input_gain = 10.0    # 1000 % default
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
        self.pitch_write_pos = 0        # integer write position
        self.pitch_read_frac = 0.0      # fractional read position

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
        self._windowed_rect  = None   # (left, top, right, bottom) in screen coords

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
        """
        Tape-speed pitch shift via fractional ring-buffer read rate.

        Incoming audio is written to a ring buffer at the hardware sample rate.
        It is read back at (factor × sample_rate), with sub-sample linear
        interpolation for smooth output.

          factor > 1  →  higher pitch (and slightly faster playback)
          factor < 1  →  lower pitch  (and slightly slower playback)
          factor = 1  →  no change

        Drift correction: the read pointer is kept ~100 ms behind the write
        pointer.  If it drifts outside [20 ms, 80% of buffer] the pointer is
        soft-reset -- inaudible at moderate factors, a brief click at extremes.
        """
        frames = len(signal)
        buf = self.pitch_ring_buf
        BUF = len(buf)
        if BUF == 0:
            return signal.copy()

        # --- Write new samples into ring buffer ---
        wp = self.pitch_write_pos
        end = wp + frames
        if end <= BUF:
            buf[wp:end] = signal
        else:
            split = BUF - wp
            buf[wp:] = signal[:split]
            buf[:end - BUF] = signal[split:]
        self.pitch_write_pos = end % BUF

        # --- Read `frames` output samples at step = factor (vectorised) ---
        rpos = (self.pitch_read_frac +
                np.arange(frames, dtype=np.float64) * factor) % BUF
        idx  = rpos.astype(np.int32)
        nxt  = (idx + 1) % BUF
        frac = (rpos - idx).astype(np.float32)
        out  = (buf[idx] * (1.0 - frac) + buf[nxt] * frac).astype(np.float32)

        # Advance fractional read position
        self.pitch_read_frac = (self.pitch_read_frac + frames * factor) % BUF

        # --- Drift correction: keep read ~100 ms behind write ---
        target_lag = int(self.stream_samplerate * 0.10)
        cur_lag    = int(self.pitch_write_pos - int(self.pitch_read_frac)) % BUF
        lag_min    = int(self.stream_samplerate * 0.02)   # 20 ms floor
        lag_max    = int(BUF * 0.80)                      # 80 % ceiling
        if not (lag_min <= cur_lag <= lag_max):
            self.pitch_read_frac = float(
                (self.pitch_write_pos - target_lag) % BUF)

        return out


    def _apply_echo(self, signal):
        """Echo: delay + feedback -- the delayed signal feeds back into itself."""
        frames = len(signal)
        buf = self.delay_buf
        buf_size = len(buf)

        delay_ms = float(dpg.get_value("fx_echo_delay")) if dpg.does_item_exist("fx_echo_delay") else 300.0
        feedback = float(dpg.get_value("fx_echo_feedback")) if dpg.does_item_exist("fx_echo_feedback") else 0.45
        feedback = min(feedback, 0.90)

        delay_samps = max(1, int(delay_ms * self.stream_samplerate / 1000.0))
        delay_samps = min(delay_samps, buf_size - frames - 1)

        rpos = (self.delay_write_pos - delay_samps + np.arange(frames)) % buf_size
        delayed = buf[rpos]
        output = signal + delayed * feedback

        wpos = (self.delay_write_pos + np.arange(frames)) % buf_size
        buf[wpos] = output   # write mixed signal back -> creates repeating echoes
        self.delay_write_pos = (self.delay_write_pos + frames) % buf_size

        return np.clip(output, -1.0, 1.0)

    def _apply_pure_delay(self, signal):
        """
        Pure delay: hear your voice from N seconds ago with NO feedback.
        The live signal is written; only the delayed signal reaches the speaker.
        Delay time is set by the Output Delay slider (0 -- 10 seconds).
        A value of 0 is a true bypass with no latency added.
        Uses a dedicated second delay buffer (delay_buf2) so the Echo
        effect's delay_buf is never disturbed.
        """
        frames = len(signal)

        delay_sec = float(dpg.get_value("fx_delay_time")) if dpg.does_item_exist("fx_delay_time") else 0.0
        if delay_sec <= 0.0:
            # Zero delay -- pure bypass, also keep delay_buf2 primed with signal
            buf = self.delay_buf2
            buf_size = len(buf)
            wpos = (self.delay_write_pos2 + np.arange(frames)) % buf_size
            buf[wpos] = signal
            self.delay_write_pos2 = (self.delay_write_pos2 + frames) % buf_size
            return signal

        buf = self.delay_buf2
        buf_size = len(buf)
        delay_samps = min(int(delay_sec * self.stream_samplerate), buf_size - frames - 1)
        delay_samps = max(1, delay_samps)

        # Read from delay_samps ago
        rpos = (self.delay_write_pos2 - delay_samps + np.arange(frames)) % buf_size
        delayed = buf[rpos]

        # Write current signal with NO feedback
        wpos = (self.delay_write_pos2 + np.arange(frames)) % buf_size
        buf[wpos] = signal
        self.delay_write_pos2 = (self.delay_write_pos2 + frames) % buf_size

        return delayed

    def _apply_robot(self, signal):
        """
        Robotic voice using dual square-wave ring modulation + comb filter.

        Stage 1 – Dual ring modulation:
          A primary square-wave carrier at `freq` Hz is mixed with a secondary
          carrier at `2×freq` (one octave up, 30 % level).  Square waves produce
          rich harmonic series; the octave carrier fills in gaps between the
          primary harmonics, making the effect far more dramatic than a single
          sine or even a single square wave.

        Stage 2 – Comb filter:
          The modulated signal is fed through a feedback comb filter with a
          delay of exactly one carrier period (1/freq seconds).  This reinforces
          every harmonic of the carrier and adds the metallic, resonant buzz
          characteristic of classic sci-fi robot voices (Daleks, etc.).
          Feedback coefficient = 0.55 (stable; peak gain ≈ +7 dB at resonance).

        Stage 3 – Soft saturation:
          tanh clipping at moderate gain keeps levels under control and adds
          a slight electronic edge while preserving speech intelligibility.
        """
        frames = len(signal)
        freq = float(dpg.get_value("fx_robot_freq")) if dpg.does_item_exist("fx_robot_freq") else 100.0

        # Phase array for this block
        t = np.arange(frames, dtype=np.float64) / self.stream_samplerate
        phase_inc = 2.0 * np.pi * freq * frames / self.stream_samplerate

        # Stage 1: dual square-wave carriers
        carrier1 = np.sign(np.sin(2.0 * np.pi * freq * t + self.robot_phase)).astype(np.float32)
        carrier2 = np.sign(np.sin(4.0 * np.pi * freq * t + self.robot_phase * 2.0)).astype(np.float32)
        self.robot_phase = (self.robot_phase + phase_inc) % (2.0 * np.pi)

        modulated = signal * (carrier1 + 0.30 * carrier2)

        # Stage 2: feedback comb filter (delay = one carrier period)
        if len(self.robot_comb_buf) > 0:
            buf  = self.robot_comb_buf
            BUF  = len(buf)
            comb_d = min(max(1, int(self.stream_samplerate / freq)), BUF - frames - 1)
            rpos   = (self.robot_comb_pos - comb_d + np.arange(frames)) % BUF
            delayed = buf[rpos]
            combed  = modulated + 0.55 * delayed          # feedback at 55 %
            wpos    = (self.robot_comb_pos + np.arange(frames)) % BUF
            buf[wpos] = combed
            self.robot_comb_pos = (self.robot_comb_pos + frames) % BUF
        else:
            combed = modulated

        # Stage 3: soft saturation – restores loudness, adds electronic edge
        return np.tanh(combed * 1.8).astype(np.float32) * 0.65

    # =================================================================
    # AUDIO CALLBACK  (hardware C-thread -- keep allocations minimal)
    # =================================================================

    def audio_callback(self, indata, outdata, frames, time, status):
        raw_in = indata[:, 0]
        processed_in = (raw_in * self.input_gain).astype(np.float32)

        # --- Voice effect (selectable) ---
        mode = self.effect_mode
        if mode == 1:
            fx_out = self._apply_pitch_shift(processed_in, 2.0)    # chipmunk: x2 pitch
        elif mode == 2:
            deep_factor = float(dpg.get_value("fx_deep_pitch")) if dpg.does_item_exist("fx_deep_pitch") else 0.45
            fx_out = self._apply_pitch_shift(processed_in, deep_factor)
        elif mode == 3:
            fx_out = self._apply_echo(processed_in)
        elif mode == 4:
            fx_out = self._apply_robot(processed_in)
        else:
            fx_out = processed_in

        # --- Universal output delay (always applied after the voice effect) ---
        # Breaks acoustic feedback loops; slider 0 s = pass-through.
        fx_out = self._apply_pure_delay(fx_out)

        outdata[:, 0] = np.clip(fx_out * self.output_gain, -1.0, 1.0)

        # Visualiser always receives the DRY (pre-effect) signal
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
            dpg.set_item_label("start_btn", "Start Oscilloscope")
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

            out_info = sd.query_devices(out_idx, 'output')
            sync_samplerate = out_info['default_samplerate']
            self.stream_samplerate = sync_samplerate

            # Size delay buffer for 11 seconds at the actual sample rate
            self.delay_buf = np.zeros(int(sync_samplerate * 11.0), dtype=np.float32)
            self.delay_write_pos = 0

            # Size universal output delay buffer the same way
            self.delay_buf2 = np.zeros(int(sync_samplerate * 11.0), dtype=np.float32)
            self.delay_write_pos2 = 0

            # Reset pitch shift state
            buf_size = int(sync_samplerate * PITCH_BUF_SECS)
            self.pitch_ring_buf  = np.zeros(buf_size, dtype=np.float32)
            target_lag           = int(sync_samplerate * 0.10)   # 100 ms
            self.pitch_write_pos = target_lag   # write starts 100 ms ahead of read
            self.pitch_read_frac = 0.0

            # Robot comb buffer: large enough for lowest carrier freq (20 Hz)
            comb_size = int(sync_samplerate / 20) * 3
            self.robot_comb_buf = np.zeros(comb_size, dtype=np.float32)
            self.robot_comb_pos = 0
            self.robot_phase = 0.0

            try:
                self.active_blocksize = 128
                self.stream = sd.Stream(
                    device=(in_idx, out_idx),
                    samplerate=sync_samplerate,
                    channels=1,
                    blocksize=self.active_blocksize,
                    callback=self.audio_callback
                )
                self.stream.start()
                self.is_running = True
                dpg.set_item_label("start_btn", "Freeze / Stop")
            except Exception as e:
                print(f"Audio Error: {e}")

    def update_gains(self):
        self.input_gain = dpg.get_value("in_gain_slider") / 100.0
        self.output_gain = dpg.get_value("out_gain_slider") / 100.0

    _FX_LABELS = ["Normal", "Chipmunk", "Voice Pitch", "Echo", "Robot"]
    _FX_TAGS   = ["fx_btn_normal", "fx_btn_chipmunk", "fx_btn_deep",
                  "fx_btn_echo",   "fx_btn_robot"]

    def set_effect(self, sender, app_data, user_data):
        """Switch effect mode and reset all effect buffers cleanly."""
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
        """
        True fullscreen on whichever monitor the DPG window currently occupies.

        DearPyGui's built-in toggle_viewport_fullscreen() always targets the
        primary monitor, which is wrong in a dual-monitor setup where the user
        has dragged the window to the secondary display.

        This implementation uses the Windows API directly:
          1.  FindWindowW        → get our HWND
          2.  MonitorFromWindow  → find which monitor the window is on
          3.  GetMonitorInfoW    → get that monitor's pixel bounds
          4.  SetWindowLongW     → swap to popup style (removes title-bar/borders)
          5.  SetWindowPos       → resize to exactly cover that monitor

        Toggling back restores the saved style and position.
        """
        u32 = ctypes.windll.user32

        if not self._is_fullscreen:
            # ── Enter fullscreen ─────────────────────────────────────────────
            hwnd = u32.FindWindowW(None, "Real-Time Oscilloscope V5")
            if not hwnd:
                return                          # viewport not ready — ignore

            # Save current windowed state
            self._windowed_hwnd  = hwnd
            self._windowed_style = u32.GetWindowLongW(hwnd, _GWL_STYLE)
            rc = ctypes.wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(rc))
            self._windowed_rect = (rc.left, rc.top, rc.right, rc.bottom)

            # Find which monitor currently hosts this window
            mon = u32.MonitorFromWindow(hwnd, _MONITOR_NEAREST)
            mi  = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            u32.GetMonitorInfoW(mon, ctypes.byref(mi))
            r = mi.rcMonitor          # full monitor bounds (not work area)

            # Popup style = no title bar, no borders → true borderless fullscreen
            u32.SetWindowLongW(hwnd, _GWL_STYLE, _WS_POPUP)
            u32.SetWindowPos(hwnd, None,
                             r.left, r.top,
                             r.right - r.left, r.bottom - r.top,
                             _SWP_FRAMECHANGED | _SWP_SHOWWINDOW)
            self._is_fullscreen = True

        else:
            # ── Restore windowed mode ────────────────────────────────────────
            hwnd = self._windowed_hwnd
            u32.SetWindowLongW(hwnd, _GWL_STYLE, self._windowed_style)
            l, t, r2, b = self._windowed_rect
            u32.SetWindowPos(hwnd, None,
                             l, t, r2 - l, b - t,
                             _SWP_FRAMECHANGED | _SWP_SHOWWINDOW)
            self._is_fullscreen = False

    # =================================================================
    # RENDER LOOP
    # =================================================================

    def update_render_loop(self):
        if not self.is_running:
            return

        now = time.perf_counter()

        while not self.audio_queue.empty():
            try:
                chunk = self.audio_queue.get_nowait()
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
                self.last_chunk_wall_time = now
            except queue.Empty:
                break

        if self.last_chunk_wall_time is None:
            return

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

        dpg.set_value("wave_line_color", (r, g, b, 255))
        dpg.set_value("wave_shade_color", (r, g, b, 60))

        if visible_points != self.last_visible_points:
            self.x_data_cache = np.arange(visible_points, dtype=np.float32)
            self.zero_line_cache = np.zeros(visible_points, dtype=np.float32)
            self.last_visible_points = visible_points

        dpg.set_value("waveform_shade", [self.x_data_cache, visible_y_np, self.zero_line_cache])
        dpg.set_value("waveform_line",  [self.x_data_cache, visible_y_np])
        dpg.set_axis_limits("x_axis", 0, visible_points)

    # =================================================================
    # GUI
    # =================================================================

    def build_gui(self):
        print("Building GUI context (V5 - Effects Edition)...")
        dpg.create_context()
        dpg.create_viewport(title='Real-Time Oscilloscope V5', width=1400, height=900)
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
        self._logo_w = 0   # display width  (0 = logo not available)
        self._logo_h = 0   # display height
        logo_path = os.path.join(_HERE, "bdars-logo.png")
        try:
            _result = dpg.load_image(logo_path)
            if _result is None:
                print(f"Warning: bdars-logo.png not found at {logo_path}")
            else:
                raw_w, raw_h, _, raw_data = _result
                # Scale so the displayed logo is at most 253 px wide (~15 % larger)
                max_display_w = 253
                self._logo_w = min(max_display_w, raw_w)
                self._logo_h = int(raw_h * self._logo_w / raw_w)
                with dpg.texture_registry(tag="logo_tex_registry"):
                    dpg.add_static_texture(raw_w, raw_h, raw_data, tag="bdars_logo_tex")
                print(f"BDARS logo loaded ({raw_w}×{raw_h} → displayed {self._logo_w}×{self._logo_h})")
        except Exception as e:
            print(f"Warning: bdars-logo.png could not be loaded ({e})")

        with dpg.window(label="Oscilloscope V5", tag="Primary Window"):

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

            # Per-effect button themes (distinct colours for children)
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

            t_normal   = _mk_theme( 60,  60,  60)   # grey
            t_chipmunk = _mk_theme(200, 155,  20)   # amber
            t_deep     = _mk_theme( 45,  70, 185)   # blue
            t_echo     = _mk_theme( 25, 155, 155)   # teal
            t_robot    = _mk_theme(155,  35, 155)   # purple

            # ---- Row 1: Device selectors ----------------------------------------
            with dpg.group(horizontal=True, tag="ctrl_grp"):
                dpg.add_combo(self.input_devices,  label="Input Source", tag="in_device",  width=200,
                              default_value=self.input_devices[0]  if self.input_devices  else "")
                dpg.add_combo(self.output_devices, label="Output Sink",  tag="out_device", width=200,
                              default_value=self.output_devices[0] if self.output_devices else "")
            dpg.add_spacer(height=5)

            # ---- Row 2: Timebase (label left) -----------------------------------
            with dpg.group(horizontal=True, tag="timebase_grp"):
                dpg.add_text("Timebase (Zoom):")
                dpg.add_slider_float(label="##speed", tag="speed_slider",
                                     default_value=int(self.max_points * 0.9),
                                     min_value=500, max_value=self.max_points, width=400)
            dpg.add_spacer(height=5)

            # ---- Row 3: Start | Mic Level | Output Volume | Fullscreen ----------
            with dpg.group(horizontal=True, tag="controls_row"):
                btn_start = dpg.add_button(label="Start Oscilloscope", tag="start_btn",
                                           callback=self.toggle_audio, width=170, height=34)
                dpg.bind_item_theme(btn_start, start_button_theme)
                dpg.add_spacer(width=10)
                dpg.add_text("Mic Level (%):")
                dpg.add_slider_float(label="##in_gain", tag="in_gain_slider",
                                     default_value=1000.0, max_value=20000.0,
                                     callback=self.update_gains, width=220)
                dpg.add_spacer(width=10)
                dpg.add_text("Output Volume (%):")
                dpg.add_slider_float(label="##out_gain", tag="out_gain_slider",
                                     default_value=0.0, max_value=200.0,
                                     callback=self.update_gains, width=180)
                dpg.add_spacer(width=10)
                btn_fs = dpg.add_button(label="Toggle Fullscreen", tag="fullscreen_btn",
                                        callback=self.toggle_fullscreen, width=150)

            dpg.bind_item_theme("ctrl_grp",      standout_theme)
            dpg.bind_item_theme("timebase_grp",  standout_theme)
            dpg.bind_item_theme("controls_row",  standout_theme)

            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=5)

            # =================================================================
            # FX PANEL — label + buttons on one row
            # =================================================================
            with dpg.group(horizontal=True):
                dpg.add_text("Voice Effects:")
                dpg.add_spacer(width=6)
                fx_btns = [
                    dpg.add_button(label="   Normal",     tag="fx_btn_normal",
                                   callback=self.set_effect, user_data=0, width=130, height=40),
                    dpg.add_button(label="   Chipmunk",   tag="fx_btn_chipmunk",
                                   callback=self.set_effect, user_data=1, width=130, height=40),
                    dpg.add_button(label="   Voice Pitch", tag="fx_btn_deep",
                                   callback=self.set_effect, user_data=2, width=130, height=40),
                    dpg.add_button(label="   Echo",       tag="fx_btn_echo",
                                   callback=self.set_effect, user_data=3, width=130, height=40),
                    dpg.add_button(label="   Robot",      tag="fx_btn_robot",
                                   callback=self.set_effect, user_data=4, width=130, height=40),
                ]
                for btn, theme in zip(fx_btns,
                                      [t_normal, t_chipmunk, t_deep, t_echo, t_robot]):
                    dpg.bind_item_theme(btn, theme)

            dpg.add_spacer(height=6)

            # Effect parameter sliders — label text is placed LEFT of each slider
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

            # Universal output delay (always active on every effect path)
            dpg.add_spacer(height=4)
            with dpg.group(horizontal=True):
                dpg.add_text("Output Delay — all paths (seconds):")
                dpg.add_slider_float(label="##delay_time", tag="fx_delay_time",
                                     default_value=0.0, min_value=0.0, max_value=10.0, width=400)

            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=6)

            # =================================================================
            # OSCILLOSCOPE PLOT
            # =================================================================
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

        dpg.set_primary_window("Primary Window", True)

        # --- BDARS logo overlay (borderless, non-interactive, top-right) ---------
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

        # Cap render loop to ~60 fps — reduces CPU burn during long exhibit sessions.
        _frame_budget = 1.0 / 60.0
        _LOGO_PAD = 10

        print("Entering main render loop...")
        while dpg.is_dearpygui_running():
            _t0 = time.perf_counter()

            # Keep logo pinned to top-right corner (handles fullscreen / resize)
            if self._logo_w > 0 and dpg.does_item_exist("logo_win"):
                _vp_w = dpg.get_viewport_client_width()
                dpg.set_item_pos("logo_win",
                                 [_vp_w - self._logo_w - _LOGO_PAD * 2, _LOGO_PAD])

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

