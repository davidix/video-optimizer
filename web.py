#!/usr/bin/env python3
"""Minimal Flask UI for video_optimizer — drag-and-drop, poll progress, download results."""

from __future__ import annotations

import io
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

import typer
from flask import Flask, abort, jsonify, render_template, request, send_file
from rich.console import Console
from werkzeug.utils import secure_filename

from video_optimizer import (
    ALLOWED_CONTAINERS,
    FFMPEG_PRESET_CHOICES,
    PRESETS,
    SUPPORTED_EXTENSIONS,
    apply_encode_overrides,
    check_ffmpeg,
    fmt_size,
    optimize_file,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_UPLOADS = BASE_DIR / "web_uploads"
WEB_OUTPUTS = BASE_DIR / "web_outputs"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GiB per request

jobs_lock = threading.Lock()
jobs: dict[str, dict[str, Any]] = {}


def ensure_dirs() -> None:
    WEB_UPLOADS.mkdir(parents=True, exist_ok=True)
    WEB_OUTPUTS.mkdir(parents=True, exist_ok=True)


class ProgressSink:
    """Minimal stand-in for rich.progress.Progress (add_task + update)."""

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id

    def add_task(self, _description: str, total: float = 100.0, **_kwargs: Any) -> int:
        with jobs_lock:
            if self._job_id in jobs:
                jobs[self._job_id]["percent"] = 0.0
        return 0

    def update(self, _task_id: int, completed: float | None = None, advance: float | None = None, **_kwargs: Any) -> None:
        with jobs_lock:
            if self._job_id not in jobs:
                return
            if completed is not None:
                jobs[self._job_id]["percent"] = float(min(100.0, max(0.0, completed)))
            elif advance is not None:
                cur = float(jobs[self._job_id].get("percent", 0.0))
                jobs[self._job_id]["percent"] = min(100.0, cur + float(advance))


def _null_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, no_color=True, width=120)


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _form_bool(val: str | None) -> bool:
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _mimetype_for_path(path: Path) -> str:
    if path.suffix.lower() == ".mkv":
        return "video/x-matroska"
    return "video/mp4"


def _run_job(
    job_id: str,
    input_path: Path,
    preset_name: str,
    preset_cfg: dict[str, Any],
    *,
    keep: bool,
    hwaccel: bool,
) -> None:
    console = _null_console()
    sink = ProgressSink(job_id)

    def fail(msg: str) -> None:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["state"] = "error"
                jobs[job_id]["error"] = msg
                jobs[job_id]["cancelled"] = False
                jobs[job_id]["percent"] = 0.0

    def mark_cancelled(msg: str) -> None:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["state"] = "cancelled"
                jobs[job_id]["cancelled"] = True
                jobs[job_id]["skipped"] = False
                jobs[job_id]["message"] = msg
                jobs[job_id]["error"] = None
                jobs[job_id]["percent"] = 0.0
                jobs[job_id]["download_url"] = None

    cancel_ev: threading.Event | None = None
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["state"] = "probing"
            raw_ev = jobs[job_id].get("cancel_event")
            if isinstance(raw_ev, threading.Event):
                cancel_ev = raw_ev

    if isinstance(cancel_ev, threading.Event) and cancel_ev.is_set():
        mark_cancelled("Cancelled.")
        return

    try:
        check_ffmpeg(console)
    except typer.Exit:
        fail("FFmpeg and ffprobe are required on PATH.")
        return

    out_dir = WEB_OUTPUTS / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["state"] = "encoding"
            jobs[job_id]["percent"] = 0.0

    cancel_ev = None
    with jobs_lock:
        if job_id in jobs:
            raw2 = jobs[job_id].get("cancel_event")
            if isinstance(raw2, threading.Event):
                cancel_ev = raw2
    if isinstance(cancel_ev, threading.Event) and cancel_ev.is_set():
        mark_cancelled("Cancelled.")
        return

    task_id = sink.add_task(input_path.name, total=100.0)
    try:
        result = optimize_file(
            input_path,
            out_dir,
            preset_name,
            preset_cfg,
            hwaccel=hwaccel,
            keep=keep,
            console=console,
            dry_run=False,
            skip_existing=False,
            progress=sink,
            task_id=task_id,
            plain=True,
            show_panels=False,
            cancel_event=cancel_ev if isinstance(cancel_ev, threading.Event) else None,
        )
    except Exception as e:  # noqa: BLE001
        fail(str(e))
        return

    with jobs_lock:
        if job_id not in jobs:
            return
        j = jobs[job_id]
        j["original_bytes"] = result.original_bytes
        j["new_bytes"] = result.new_bytes
        j["reduction_percent"] = result.reduction_percent
        j["skipped"] = result.skipped
        j["cancelled"] = result.cancelled

        if result.cancelled:
            j["state"] = "cancelled"
            j["error"] = None
            j["message"] = result.error or "Cancelled."
            j["percent"] = 0.0
            j["download_url"] = None
            return

        if not result.success:
            j["state"] = "error"
            j["error"] = result.error or "Encoding failed."
            j["percent"] = 0.0
            return

        if result.dry_run:
            j["state"] = "error"
            j["error"] = "Unexpected dry-run."
            return

        if result.output and Path(result.output).is_file():
            j["output_path"] = str(Path(result.output).resolve())
            j["server_path"] = j["output_path"]
            j["state"] = "done"
            j["percent"] = 100.0
            if result.new_bytes is not None and result.original_bytes > 0:
                j["summary"] = (
                    f"{fmt_size(result.original_bytes)} → {fmt_size(result.new_bytes)}"
                    + (
                        f" ({result.reduction_percent:+.1f}%)"
                        if result.reduction_percent is not None
                        else ""
                    )
                )
            else:
                j["summary"] = "Done."
            if result.skipped:
                j["message"] = "Skipped — output already existed."
            else:
                j["message"] = None
            return

        # Success but no file on disk (e.g. removed because not smaller)
        j["state"] = "done"
        j["percent"] = 100.0
        j["output_path"] = None
        j["server_path"] = None
        j["download_url"] = None
        if result.new_bytes is not None:
            j["message"] = (
                "Encoded output was not smaller than the source; file was removed "
                "(same as CLI without --keep)."
            )
        else:
            j["message"] = "No output file produced."


@app.route("/")
def index() -> str:
    preset_rows = [
        {"name": name, "description": str(cfg.get("description", ""))}
        for name, cfg in PRESETS.items()
    ]
    ext_list = sorted(e.lstrip(".").lower() for e in SUPPORTED_EXTENSIONS)
    return render_template(
        "index.html",
        presets=preset_rows,
        supported_extensions=ext_list,
        ffmpeg_ok=_ffmpeg_available(),
        ffmpeg_presets=sorted(FFMPEG_PRESET_CHOICES),
        containers=sorted(ALLOWED_CONTAINERS),
    )


@app.route("/upload", methods=["POST"])
def upload() -> Any:
    if "file" not in request.files:
        return jsonify({"error": "Missing file field."}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Empty filename."}), 400

    preset = request.form.get("preset", "medium").strip()
    if preset not in PRESETS:
        return jsonify({"error": f"Unknown preset: {preset}"}), 400

    audio = request.form.get("audio", "preset").strip().lower()
    if audio not in ("preset", "copy", "aac", "none"):
        return jsonify({"error": "Invalid audio mode."}), 400

    crf_raw = (request.form.get("crf") or "").strip()
    crf: int | None = None
    if crf_raw:
        try:
            crf = int(crf_raw)
        except ValueError:
            return jsonify({"error": "CRF must be an integer."}), 400

    codec_raw = (request.form.get("codec") or "").strip().lower()
    codec: str | None = codec_raw if codec_raw in ("h264", "h265") else None

    container_raw = (request.form.get("container") or "").strip().lower()
    container: str | None = container_raw if container_raw else None

    ffmpeg_preset_raw = (request.form.get("ffmpeg_preset") or "").strip()
    ffmpeg_preset: str | None = ffmpeg_preset_raw if ffmpeg_preset_raw else None

    audio_bitrate_raw = (request.form.get("audio_bitrate") or "").strip()
    audio_bitrate: str | None = audio_bitrate_raw if audio_bitrate_raw else None

    keep = _form_bool(request.form.get("keep"))
    hwaccel = _form_bool(request.form.get("hwaccel"))

    try:
        preset_cfg = apply_encode_overrides(
            {**PRESETS[preset]},
            crf=crf,
            codec=codec,
            ffmpeg_preset=ffmpeg_preset,
            audio_mode=audio,
            audio_bitrate=audio_bitrate,
            container=container,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return jsonify({"error": f"Unsupported type: {suffix}"}), 400

    safe = secure_filename(f.filename)
    if not safe:
        safe = f"upload{suffix}"

    job_id = uuid.uuid4().hex
    job_dir = WEB_UPLOADS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    save_path = job_dir / safe
    f.save(str(save_path))

    cancel_event = threading.Event()
    with jobs_lock:
        jobs[job_id] = {
            "state": "queued",
            "percent": 0.0,
            "filename": f.filename,
            "saved_name": safe,
            "preset": preset,
            "original_bytes": save_path.stat().st_size,
            "new_bytes": None,
            "reduction_percent": None,
            "error": None,
            "output_path": None,
            "server_path": None,
            "skipped": False,
            "cancelled": False,
            "message": None,
            "summary": None,
            "download_url": f"/download/{job_id}",
            "cancel_event": cancel_event,
        }

    t = threading.Thread(
        target=_run_job,
        args=(job_id, save_path, preset, preset_cfg),
        kwargs={"keep": keep, "hwaccel": hwaccel},
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str) -> Any:
    with jobs_lock:
        j = jobs.get(job_id)
    if not j:
        abort(404)

    out: dict[str, Any] = {
        "state": j["state"],
        "percent": round(float(j.get("percent", 0.0)), 1),
        "filename": j.get("filename"),
        "original_bytes": j.get("original_bytes"),
        "new_bytes": j.get("new_bytes"),
        "reduction_percent": j.get("reduction_percent"),
        "error": j.get("error"),
        "message": j.get("message"),
        "summary": j.get("summary"),
        "skipped": j.get("skipped", False),
        "cancelled": bool(j.get("cancelled", False)),
        "output_relpath": None,
    }
    if j["state"] == "done" and j.get("output_path"):
        out["download_url"] = j.get("download_url")
        try:
            rel = Path(j["output_path"]).resolve().relative_to(BASE_DIR)
            out["output_relpath"] = str(rel)
        except ValueError:
            out["output_relpath"] = Path(j["output_path"]).name
    else:
        out["download_url"] = None
    return jsonify(out)


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id: str) -> Any:
    with jobs_lock:
        j = jobs.get(job_id)
    if not j:
        abort(404)
    st = j.get("state")
    if st not in ("queued", "probing", "encoding"):
        return jsonify({"error": "Job is not running."}), 400
    ev = j.get("cancel_event")
    if isinstance(ev, threading.Event):
        ev.set()
    return jsonify({"ok": True})


@app.route("/download/<job_id>")
def download(job_id: str) -> Any:
    with jobs_lock:
        j = jobs.get(job_id)
    if not j:
        abort(404)
    path_str = j.get("output_path")
    if j.get("state") != "done" or not path_str:
        abort(404)
    path = Path(path_str).resolve()
    allowed_root = (WEB_OUTPUTS / job_id).resolve()
    try:
        path.relative_to(allowed_root)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=path.name,
        mimetype=_mimetype_for_path(path),
    )


ensure_dirs()

if __name__ == "__main__":
    # Dev server — use a production WSGI server for deployment.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=True)
