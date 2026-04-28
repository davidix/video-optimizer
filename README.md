# Video Optimizer

Reduce video file size without visible quality loss. Uses FFmpeg with CRF-based H.265/H.264 encoding under the hood.

## Prerequisites

Install FFmpeg:

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg
```

## Install (Python)

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

The CLI is **`vopt`**-style: subcommands (`optimize`, `info`, `presets`, `watch`). For convenience, paths without a subcommand are treated as **`optimize`** (same as before).

```bash
# Help
python video_optimizer.py --help
python video_optimizer.py optimize --help

# Optimize a single video (default: medium preset)
python video_optimizer.py optimize video.mp4
# Legacy (equivalent — inserts "optimize" automatically):
python video_optimizer.py video.mp4

# Light / aggressive presets
python video_optimizer.py optimize -p light video.mp4
python video_optimizer.py optimize -p aggressive video.mp4

# Batch folder, recursive, custom output directory
python video_optimizer.py optimize -r ./videos/ -o ./output/

# Dry-run: probe and show planned ffmpeg command (no encode)
python video_optimizer.py optimize --dry-run video.mp4

# JSON summary (machine-readable, no Rich styling)
python video_optimizer.py optimize video.mp4 --json

# Skip if `<stem>_optimized.mp4` already exists in output dir
python video_optimizer.py optimize --skip-existing -o ./out/ video.mp4

# Parallel encodes (separate progress tasks)
python video_optimizer.py optimize -j 2 ./clips/*.mp4

# CI / logs: no colors or Rich progress
python video_optimizer.py optimize video.mp4 --plain

# List presets (table)
python video_optimizer.py presets

# Inspect a file (ffprobe, Rich tables)
python video_optimizer.py info video.mp4

# Watch a directory for new videos and optimize them
python video_optimizer.py watch ./incoming/ -o ./out/
```

## Presets

| Preset       | CRF | Codec   | Description                                  |
|--------------|-----|---------|----------------------------------------------|
| `light`      | 18  | H.265   | Minimal compression, virtually lossless      |
| `medium`     | 23  | H.265   | Balanced compression, near-transparent       |
| `aggressive` | 28  | H.265   | Maximum compression, minor quality trade-off   |

## Commands

| Command    | Description |
|------------|-------------|
| `optimize` | Encode inputs with the chosen preset (default when omitted). |
| `presets`  | Print preset table. |
| `info`     | Show container + stream details via ffprobe. |
| `watch`    | Watch a directory and run `optimize` on new video files (requires `watchdog`). |

## `optimize` options

| Flag | Description |
|------|-------------|
| `-p`, `--preset` | `light`, `medium`, or `aggressive` (default: `medium`). |
| `-o`, `--output-dir` | Output directory (default: same as each input). |
| `-r`, `--recursive` | Recurse into directories. |
| `--hwaccel` | Enable FFmpeg `-hwaccel auto`. |
| `--keep` | Keep output even if larger than the source. |
| `--crf` | Override CRF (0–51). |
| `--codec` | `h264` or `h265` (maps to libx264 / libx265). |
| `--dry-run` | Probe only; print planned output path and ffmpeg command. |
| `--json` | Print JSON summary to stdout. |
| `--skip-existing` | Skip if `{stem}_optimized.mp4` already exists in the output dir. |
| `-j`, `--workers` | Parallel jobs (default: 1). |
| `--plain`, `--no-color` | Disable colors and Rich progress. |

## How it works

The tool re-encodes videos using **Constant Rate Factor (CRF)** mode, which targets a consistent perceptual quality level rather than a fixed bitrate. Combined with **H.265 (HEVC)**, this typically achieves a large size reduction vs. older H.264 encodes at similar visual quality. Audio is either copied (`light`) or re-encoded to AAC (`medium`, `aggressive`).
