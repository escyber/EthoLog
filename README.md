# A PyQt6-based GUI for mice behavior scoring

A desktop tool for frame-accurate behavioral scoring of mouse videos, built as a lightweight alternative to BORIS.

## Features

- **Frame-by-frame video playback** — Play/Pause button and a scrub bar (seek slider) for navigating the video, in addition to arrow-key stepping.
- **Frame-accurate sync** — the scrub bar position always matches the exact current frame; dragging it seeks frame-by-frame with no drift.
- **Bidirectional table/video sync** — selecting a row in the event log jumps the video to that moment, and playing/scrubbing the video highlights the corresponding row in the log automatically.
- **Customizable ethogram** — define behavior shortcuts (key, name, event type, description) via the sidebar table, add new ones through a dialog, or load a full ethogram from a CSV file.
- **Keyboard-driven event logging** — press a behavior's assigned key to log a START/STOP (state behaviors) or POINT (point behaviors) event at the current frame.
- **Drag-and-drop row reordering** — reorder existing entries in the event log table by dragging rows into place.
- **Row editing and deletion** — click any table cell to edit its value directly; select a row and click "Delete Selected Row" to remove it.
- **Motion pre-scoring** — an automated pass that flags frame ranges with detected motion as an "Active" state, to speed up a first-pass scoring.
- **Standardized export** — export the event log as a BORIS-style TSV or CSV file for downstream analysis.

## Requirements

- Python 3.x
- PyQt6
- OpenCV (`opencv-python`)

## Quick Start (recommended for first-time setup)

Two setup scripts are included that create an isolated environment, install everything needed, and launch the app in one step. On later runs, they skip setup and just launch it directly.

**Mac/Linux:**
```bash
chmod +x setup_and_run.sh   # only needed once, if the file isn't already executable
./setup_and_run.sh
```

**Windows:**
Double-click `setup_and_run.bat`, or run it from Command Prompt.

Both scripts require Python 3 to already be installed on your system (get it from [python.org](https://www.python.org/downloads/) if needed — on Windows, be sure to check "Add Python to PATH" during install).

## Manual Setup

If you'd rather install dependencies globally instead of using the setup script:
```bash
pip install PyQt6 opencv-python
```

## Usage

1. Run the script: `python behavior_scorer.py`
2. Load a video file via "Open Video File."
3. Use Play/Pause and the scrub bar to navigate, or step frame-by-frame with the arrow keys.
4. Press behavior shortcut keys to log START/STOP/POINT events at the current frame.
5. Click a row in the event log to jump the video to that point, or reorder rows by dragging.
6. Export your log via "Export Log" once scoring is complete.
