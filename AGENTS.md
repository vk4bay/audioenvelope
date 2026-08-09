# AudioEnvelope — project notes (resume here)

Real-time oscilloscope + spectrograph museum exhibit app. Single file `main.py`
(DearPyGui + sounddevice + NumPy, optional scipy for GIL-free sosfilt in the
audio callback). Run with `.venv/bin/python3 main.py` — the system `python3`
does NOT have the deps. Version in `APP_VERSION` (currently 7.12); bump minor
on each change.

## Last user request (FIXED → verify)
T530 is on X11 (Xorg) now. Fullscreen originally did NOTHING (V7.13 fix, part 1):
on GNOME/Mutter the title search matched the **WM frame** window, and
`XMoveResizeWindow` on a frame is ignored — the search now resolves the titled
CHILD of the matched window (the real client) and resizes THAT (verified on this
machine: enter covers the monitor it's on). Part 2 (same version, fixed after
user retest): exiting fullscreen shrank the window to ~logo size on GNOME/Mutter
— restore now goes through DPG's OWN viewport setters (`set_viewport_pos` +
`set_viewport_width/height`, saved on enter from the DPG getters) after an EWMH
`_NET_WM_STATE` REMOVE `_NET_WM_STATE_FULLSCREEN` ClientMessage; raw X11 restore
was abandoned. User should retest: Fullscreen covers the monitor; Escape/button
returns EXACTLY to the pre-fullscreen size.

## Fullscreen implementation (monitor-aware)
- Windows: custom Win32 path (`MonitorFromWindow` + `GetMonitorInfoW`) —
  confirmed by user to keep the app on the external monitor.
- Linux X11: `_toggle_fullscreen_x11()` — Xlib/XRandR via ctypes; finds the
  DPG window by APP_TITLE (toplevel may be the WM FRAME whose children include
  the real client — resolve to the titled child), picks the CRTC containing the
  window centre, `XMoveResizeWindow` covers that monitor. Geometry (pos/size)
  is captured and restored through DPG's OWN viewport getters/setters
  (`get_viewport_pos`/`set_viewport_pos`/`set_viewport_width`/... ) — raw
  X11 move/resize left the window tiny on exit under GNOME/Mutter. On exit,
  first send EWMH ClientMessage clearing `_NET_WM_STATE_FULLSCREEN` (Mutter
  otherwise ignores the restore request).
  CRTC struct layout validated against .../Xrandr.h — do NOT change field order!
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
- Version: 7.13 (fullscreen X11 fix), NOT yet committed. Remote =
  `git@github.com:bobwis/audioenvelope.git` (was switched from vk4bay/bdars).
  Push via `git push` works over SSH.
- Logo overlay: right-top, top edge aligned with Spectrograph/Fullscreen
  buttons, 180 px max width, 40 px right margin.
- If a session resumes and DPG/DSP performance complaints come in: audio
  callback must stay allocation-light; slider reads are cached main-thread
  only; scipy `sosfilt` avoids GIL in callback.