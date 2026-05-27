# Audio Envelope Oscilloscope

A museum exhibit application that displays a real-time scrolling oscilloscope of a speaker's voice envelope, with live audio effects for visitor interaction.

Built with Python, DearPyGui, NumPy and sounddevice.

---

## Features

- **Scrolling oscilloscope** — smooth phosphor-style waveform display with colour-coded amplitude
- **Voice effects** — selectable per-visitor effects processed in real time:
  - **Normal** — dry pass-through
  - **Chipmunk** — pitch shifted up ×2
  - **Voice Pitch** — continuously variable pitch (0.25× – 4×) via slider
  - **Echo** — configurable delay and feedback
  - **Robot** — dual square-wave ring modulation + comb filter
- **Output delay** — 0 – 10 s delay on all output paths (prevents acoustic feedback)
- **Fullscreen on any monitor** — correctly targets whichever display the window is dragged to in a dual-monitor setup
- **Branding overlay** — logo displayed in the top-right corner, auto-repositioned on resize/fullscreen

---

## Requirements

```
pip install dearpygui sounddevice numpy
```

A working audio input device (microphone) and output device (speaker/headphones) are required.

---

## Usage

```
python main.py
```

1. Select your **Input Source** and **Output Sink** from the drop-downs.
2. Press **Start Oscilloscope**.
3. Speak into the microphone — your voice envelope appears on the display.
4. Select a **Voice Effect** and adjust the parameter sliders.
5. Use **Toggle Fullscreen** to go full-screen on the current monitor.

---

## Asset files

| File | Purpose |
|---|---|
| `NicerFont.ttf` | Display font (optional — falls back to default if missing) |
| `bdars-logo.png` | Branding overlay shown in the top-right corner |

Both files must be in the same folder as `main.py`.

---

## License

Internal exhibit software — © BDARS. Not licensed for redistribution.

