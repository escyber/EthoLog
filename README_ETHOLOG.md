# EthoLog: a PyQt6 GUI for mouse behavior scoring

A desktop tool for frame-accurate behavioral scoring of mouse videos, built as a lightweight alternative to BORIS. Exported logs use the BORIS TSV column order, so they work with other tools that read BORIS files.

## Features

- Frame-by-frame video playback with arrow-key stepping, a seek slider and playback speed (1x, 0.5x, 0.25x)
- Behavior timeline: colored stretches show what is scored, and unscored stretches are marked with a dark block and an orange triangle. Zoom (Whole video, 2 min, 30 s, 10 s), scroll, and click to jump; Gap buttons move between unscored stretches
- Customizable ethogram: edit it in the app or load one from a CSV (columns: Key, Behavior, Type, Description)
- Press a behavior's key to start it and press it again to stop it; START and STOP are recorded automatically
- Event Log you can edit, undo, sort, load and export. Columns can be dragged around on screen; the export always uses the BORIS column order
- Checks for unpaired START/STOP events, with one click to jump to the problem
- Autosave after every change, with a Recover button
- Look-ahead preview, resizable and collapsible panels, light and dark themes

## What changed since v1.0

v1.0 was a single-window prototype (basic ethogram table, play/pause and scrub bar, a 16-column log, motion pre-scoring). v2.0 is a major rewrite:

- **BORIS-compatible export:** 18 columns in the BORIS order, including Source and Media file name, so logs work with other BORIS-based tools
- **Behavior timeline** with markers for unscored frames, zoom, scroll, click-to-jump and gap navigation
- **Checks and safety:** unpaired START/STOP detection with jump-to-problem, undo, autosave with a Recover button, and a confirmation before closing
- **Ethogram:** edit dialog, CSV load and export, version tracking (written into every scored event), color dots matching the timeline
- **Interface:** light and dark themes, look-ahead preview, resizable and collapsible panels, draggable log columns, playback speed, large-video mode
- **Smoother playback:** frames are read in order during playback instead of seeking on every frame
- **Removed from v1.0:** motion pre-scoring and drag-and-drop row reordering (events are now sorted by time automatically)

## Ethogram

The app opens with a small example ethogram (walking, resting, grooming, rearing). Use **Load Ethogram** to load your own CSV. The ethogram version is taken from the file name (for example `ethogram_v1.0.csv`) and is written into the Description column of every scored event.

## Install and run

```
pip install PyQt6 opencv-python
python3 etholog.py
```

Or use the setup scripts in this repo (`Setup_and_Run_Mac_Linux`, `Setup_and_Run_Windows`).

## Export format

A `.tsv` file with the BORIS columns: Observation id, Observation date, Description, Observation duration, Observation type, Source, Time offset (s), Media duration (s), FPS, Subject, Behavior, Behavioral category, Behavior type, Time, Media file name, Image index, Image file path, Comment.

## Notes

Tested on macOS. Do not commit videos or exported logs to this repository.
