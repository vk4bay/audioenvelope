# AudioEnvelope — project notes (resume here)

Real-time oscilloscope + spectrograph museum exhibit app. Single file `main.py`
(DearPyGui + sounddevice + NumPy, optional scipy for GIL-free sosfilt in the
audio callback). Run with `.venv/bin/python3 main.py` — the system `python3`
does NOT have the deps. Version in `APP_VERSION` (currently 7.12); bump minor
on each change.

## Last user request (PENDING)
User is testing switching the T530 exhibit PC from **Wayland to X11** at the
login screen (gear icon → "Ubuntu on Xorg") so the newly-added monitor-aware
X11 fullscreen path works. After login, verify with:
`echo $XDG_SESSION_TYPE` → expect `x11`.

## Fullscreen implementation (monitor-aware)
- Windows: custom Win32 path (`MonitorFromWindow` + `GetMonitorInfoW`) —
  confirmed by user to keep the app on the external monitor.
- Linux X11: `_toggle_fullscreen_x11()` — Xlib/XRandR via ctypes; finds the
  DPG window by APP_TITLE, picks the CRTC containing the window centre,
  `XMoveResizeWindow` covers that monitor (saved/restored). CRTC struct layout
  validated against `/usr/include/X11/extensions/Xrandr.h` — do NOT change the
  field order!
- Wayland: detected via `_x11_wayland_session()` → falls back to DPG's
  built-in fullscreen (always jumps to *primary* monitor — the laptop LCD).
  There is NO Wayland-positioning fix without forking DPG/GLFW, per research.

## Audio chain / defaults (all settled)
Constants: `IN_GAIN_SLIDER_DEFAULT=50`, `OUT_GAIN_SLIDER_DEFAULT=5`,
`ROBOT_FREQ_DEFAULT=67.5` (user's hand-set value — DON'T change back),
`DEEP_PITCH_DEFAULT=1.3`. Gains seeded in `__init__` from the same formulas
`update_gains()` uses (matches sliders at startup; fix for hot-mic bug).
Robot effect = square ring (crushed 7-level voice layer) + comb + mix in
(`combed*0.47 + crushed*0.28 + dry*0.31`) + tanh soft clip + **notch filter
at the carrier freq** (`ROBOT_NOTCH_Q=5`), rebuilt whenever the Robot Freq
slider moves (render loop), so notch always tracks.

## Other current-state notes
- Version: 7.12, committed. Remote = `git@github.com:bobwis/audioenvelope.git`
  (was switched from vk4bay/bdars). Push via `git push` works over SSH.
- Logo overlay: right-top, top edge aligned with Spectrograph/Fullscreen
  buttons, 180 px max width, 40 px right margin.
- If a session resumes and DPG/DSP performance complaints come in: audio
  callback must stay allocation-light; slider reads are cached main-thread
  only; scipy `sosfilt` avoids GIL in callback.