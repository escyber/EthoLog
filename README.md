# A PyQt6-based GUI for mice behavior scoring

A desktop tool for frame-accurate behavioral scoring of mouse videos, built as a lightweight alternative to BORIS.

## Features

- **Frame-by-frame video playback** — Play/Pause button and a scrub bar (seek slider) for navigating the video, in addition to arrow-key stepping.
- **Customizable ethogram** — define behavior shortcuts (key, name, event type, description) via the sidebar table, add new ones through a dialog, or load a full ethogram from a CSV file.
- **Drag-and-drop row reordering** — reorder existing entries in the event log table by dragging rows into place.
- **Row editing and deletion** — click any table cell to edit its value directly; select a row and click "Delete Selected Row" to remove it.
- **Standardized export** — export and import in both TSV and CSV formats

## Requirements

- Python 3.x
- PyQt6
- OpenCV (`opencv-python`)

## Quick Start (recommended for first-time setup)

**Step 1: Get the files**

Go to the [GitHub repo page](https://github.com/escyber/A-PyQt6-based-GUI-for-mice-behavior-scoring), click the green **"Code"** button, then **"Download ZIP."** Unzip it anywhere on your computer. 

**Step 2: Run the setup script**

This creates an isolated environment, installs everything needed, and launches the app in one step. On later runs, it skips setup and just launches the app directly.

**Mac/Linux** — open Terminal, `cd` into the unzipped folder, then:
```bash
chmod +x setup_and_run.sh   # only needed once, if the file isn't already executable
./setup_and_run.sh
```

**Windows** — open the unzipped folder and double-click `setup_and_run.bat` (or run it from Command Prompt).

Both scripts require Python 3 to already be installed on your system (get it from [python.org](https://www.python.org/downloads/) if needed — on Windows, be sure to check "Add Python to PATH" during install).

## Manual Setup

If you'd rather install dependencies globally instead of using the setup script:
```bash
pip install PyQt6 opencv-python
python3 behavior_scorer.py
```

## Usage

1. Run the script: `python behavior_scorer.py`
2. Load a video file via "Open Video File."
3. Use Play/Pause and the scrub bar to navigate, or step frame-by-frame with the arrow keys.
4. Press behavior shortcut keys to log START/STOP/POINT events at the current frame.
5. Click a row in the event log to jump the video to that point, or reorder rows by dragging.
6. Export your log via "Export Log" once scoring is complete.
