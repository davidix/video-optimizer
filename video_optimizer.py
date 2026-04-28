#!/usr/bin/env python3
"""Video Optimizer — reduce video file size without visible quality loss."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

SUPPORTED_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts"}

SUBCOMMANDS = frozenset({"optimize", "info", "presets", "watch"})

HELP_PRESET_OPTION = (
    "Named recipe: [bold]light[/bold] (CRF 18, copy audio) — largest files, closest to lossless; "
    "[bold]medium[/bold] (CRF 23, AAC) — default balance; "
    "[bold]aggressive[/bold] (CRF 28, AAC) — smallest files, more visible trade-offs. "
        "Run [cyan]python video_optimizer.py presets[/cyan] for the full table."
)

HELP_CRF = (
    "Constant Rate Factor override (x264/x265). "
    "Lower = higher quality and bigger files (typical video range ~18–28). "
    "0 is lossless (huge); 51 is worst. Preset defaults: light=18, medium=23, aggressive=28."
)

HELP_CODEC = (
    "Force video encoder: [bold]h265[/bold] (libx265, smaller, slower) or [bold]h264[/bold] "
    "(libx264, wider compatibility). Default follows the chosen preset (H.265)."
)

HELP_CONTAINER = (
    "Output container for H.264/H.265 encodes: [bold]mp4[/bold] (default, +faststart) or [bold]mkv[/bold]."
)

HELP_FFMPEG_PRESET = (
    "Override FFmpeg encoder [bold]-preset[/bold] (speed vs compression): "
    "ultrafast … veryslow. Default comes from the named [bold]-p[/bold] recipe."
)

HELP_AUDIO = (
    "Audio: [bold]preset[/bold] (use the recipe’s copy/AAC policy); [bold]copy[/bold]; "
    "[bold]aac[/bold] (re-encode); [bold]none[/bold] (strip audio / -an)."
)

HELP_AUDIO_BITRATE = (
    "AAC bitrate when [bold]--audio aac[/bold] (e.g. [cyan]128k[/cyan]). "
    "When [bold]--audio preset[/bold] and the recipe uses AAC, overrides that recipe’s bitrate."
)

ALLOWED_CONTAINERS = frozenset({"mp4", "mkv"})

FFMPEG_PRESET_CHOICES = frozenset(
    {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    }
)


PRESETS: dict[str, dict[str, Any]] = {
    "light": {
        "description": "Minimal compression, virtually lossless (CRF 18 / H.265)",
        "video_codec": "libx265",
        "crf": 18,
        "audio_strategy": "copy",
        "preset": "medium",
    },
    "medium": {
        "description": "Balanced compression, near-transparent quality (CRF 23 / H.265)",
        "video_codec": "libx265",
        "crf": 23,
        "audio_strategy": "compress",
        "audio_bitrate": "128k",
        "preset": "medium",
    },
    "aggressive": {
        "description": "Maximum compression, minor quality trade-off (CRF 28 / H.265)",
        "video_codec": "libx265",
        "crf": 28,
        "audio_strategy": "compress",
        "audio_bitrate": "96k",
        "preset": "slow",
    },
}


@dataclass
class ProbeResult:
    duration: float
    video_codec: str
    width: int
    height: int
    bitrate: int
    audio_codec: str | None
    audio_bitrate: int | None
    file_size: int


@dataclass
class EncodeResult:
    input: str
    output: str | None
    success: bool
    skipped: bool
    dry_run: bool
    error: str | None
    original_bytes: int
    new_bytes: int | None
    reduction_percent: float | None
    cancelled: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "input": self.input,
            "output": self.output,
            "success": self.success,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
            "cancelled": self.cancelled,
            "error": self.error,
            "original_bytes": self.original_bytes,
            "new_bytes": self.new_bytes,
            "reduction_percent": self.reduction_percent,
        }
        return d


class EncodeCancelled(Exception):
    """Raised when encoding is stopped before FFmpeg exits successfully (e.g. user cancel)."""


def fmt_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def check_ffmpeg(console: Console) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        console.print(
            "[bold red]Error:[/bold red] ffmpeg and ffprobe are required but not found.\n"
            "Install via: [cyan]brew install ffmpeg[/cyan]  (macOS)\n"
            "             [cyan]sudo apt install ffmpeg[/cyan]  (Ubuntu/Debian)",
        )
        raise typer.Exit(1)


def ffprobe_json(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def probe(path: Path) -> ProbeResult:
    data = ffprobe_json(path)
    video_stream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)

    if not video_stream:
        raise ValueError(f"No video stream found in {path}")

    fmt = data.get("format", {})

    return ProbeResult(
        duration=float(fmt.get("duration", 0)),
        video_codec=video_stream.get("codec_name", "unknown"),
        width=int(video_stream.get("width", 0)),
        height=int(video_stream.get("height", 0)),
        bitrate=int(fmt.get("bit_rate", 0)),
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
        audio_bitrate=int(audio_stream.get("bit_rate", 0))
        if audio_stream and audio_stream.get("bit_rate")
        else None,
        file_size=os.path.getsize(path),
    )


def check_encoder(codec: str) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
    )
    return codec in result.stdout


def output_suffix(preset_cfg: dict[str, Any]) -> str:
    c = str(preset_cfg.get("container", "mp4")).lower()
    if c not in ALLOWED_CONTAINERS:
        raise ValueError(f"Invalid container {c!r} (allowed: {', '.join(sorted(ALLOWED_CONTAINERS))})")
    return f".{c}"


def apply_encode_overrides(
    base_cfg: dict[str, Any],
    *,
    crf: int | None = None,
    codec: str | None = None,
    ffmpeg_preset: str | None = None,
    audio_mode: str = "preset",
    audio_bitrate: str | None = None,
    container: str | None = None,
) -> dict[str, Any]:
    """Merge CLI/web tuning into a preset config copy. Raises ValueError on invalid input."""
    out: dict[str, Any] = {**base_cfg}

    if crf is not None:
        if not 0 <= crf <= 51:
            raise ValueError("CRF must be between 0 and 51.")
        out["crf"] = crf

    if codec:
        if codec not in ("h264", "h265"):
            raise ValueError("--codec must be h264 or h265.")
        out["video_codec"] = "libx264" if codec == "h264" else "libx265"

    if ffmpeg_preset is not None:
        p = ffmpeg_preset.strip().lower()
        if p not in FFMPEG_PRESET_CHOICES:
            raise ValueError(
                f"Unknown --ffmpeg-preset {ffmpeg_preset!r}. "
                f"Choose one of: {', '.join(sorted(FFMPEG_PRESET_CHOICES))}"
            )
        out["preset"] = p

    if audio_mode not in ("preset", "copy", "aac", "none"):
        raise ValueError("--audio must be preset, copy, aac, or none.")

    if audio_mode == "copy":
        out["audio_strategy"] = "copy"
        out.pop("audio_bitrate", None)
    elif audio_mode == "aac":
        out["audio_strategy"] = "compress"
        out["audio_bitrate"] = str(audio_bitrate).strip() if audio_bitrate else "128k"
    elif audio_mode == "none":
        out["audio_strategy"] = "none"
        out.pop("audio_bitrate", None)
    elif audio_bitrate is not None and out.get("audio_strategy") == "compress":
        out["audio_bitrate"] = str(audio_bitrate).strip()

    if container is not None:
        c = container.strip().lower()
        if c not in ALLOWED_CONTAINERS:
            raise ValueError(
                f"Invalid --container {container!r}. Choose: {', '.join(sorted(ALLOWED_CONTAINERS))}."
            )
        out["container"] = c
    out.setdefault("container", "mp4")

    return out


def build_ffmpeg_cmd(
    input_path: Path,
    output_path: Path,
    preset_cfg: dict[str, Any],
    hwaccel: bool = False,
    *,
    on_codec_fallback: Optional[Callable[[str], None]] = None,
) -> tuple[list[str], str]:
    codec = preset_cfg["video_codec"]

    if codec == "libx265" and not check_encoder("libx265"):
        if on_codec_fallback:
            on_codec_fallback("H.265 encoder not available, falling back to H.264 (libx264)")
        codec = "libx264"

    cmd = ["ffmpeg", "-hide_banner", "-y"]

    if hwaccel:
        cmd += ["-hwaccel", "auto"]

    cmd += ["-i", str(input_path)]

    cmd += [
        "-c:v",
        codec,
        "-crf",
        str(preset_cfg["crf"]),
        "-preset",
        preset_cfg["preset"],
    ]

    if codec == "libx265":
        cmd += ["-tag:v", "hvc1"]

    if preset_cfg["audio_strategy"] == "copy":
        cmd += ["-c:a", "copy"]
    elif preset_cfg["audio_strategy"] == "compress":
        cmd += ["-c:a", "aac", "-b:a", preset_cfg["audio_bitrate"]]
    else:
        cmd += ["-an"]

    if str(preset_cfg.get("container", "mp4")).lower() == "mp4":
        cmd += ["-movflags", "+faststart"]

    cmd += [str(output_path)]
    return cmd, codec


def _parse_ffmpeg_time(line: str) -> tuple[float, str | None]:
    """Return (current_seconds, speed_str) from an ffmpeg stderr line."""
    parts = line.strip().split()
    time_part = next((p for p in parts if p.startswith("time=")), None)
    speed_part = next((p for p in parts if p.startswith("speed=")), None)
    current = 0.0
    if time_part:
        t = time_part.split("=", 1)[1]
        try:
            h, m, s = t.split(":")
            current = int(h) * 3600 + int(m) * 60 + float(s)
        except (ValueError, IndexError):
            current = 0.0
    speed = speed_part.split("=", 1)[1] if speed_part else None
    return current, speed


def run_encode(
    cmd: list[str],
    duration: float,
    *,
    progress: Progress | None = None,
    task_id: int | None = None,
    plain: bool = False,
    cancel_event: threading.Event | None = None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise EncodeCancelled()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if cancel_event is not None and cancel_event.is_set():
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        raise EncodeCancelled()

    killer: threading.Thread | None = None
    if cancel_event is not None:

        def _terminate_when_cancelled() -> None:
            cancel_event.wait()
            if process.poll() is None:
                process.terminate()

        killer = threading.Thread(target=_terminate_when_cancelled, daemon=True)
        killer.start()

    assert process.stderr is not None

    while True:
        line = process.stderr.readline()
        if not line and process.poll() is not None:
            break

        if "time=" in line and progress is not None and task_id is not None:
            current, _speed = _parse_ffmpeg_time(line)
            if duration > 0:
                pct = min(current / duration * 100.0, 100.0)
                progress.update(task_id, completed=pct)
            else:
                progress.update(task_id, advance=0.1)
        elif "time=" in line and plain:
            # Minimal heartbeat for CI/plain when duration unknown
            pass

    rc = process.wait()
    if cancel_event is not None and cancel_event.is_set():
        raise EncodeCancelled()
    if progress is not None and task_id is not None:
        progress.update(task_id, completed=100.0)

    if rc != 0:
        stderr = process.stderr.read()
        raise RuntimeError(f"FFmpeg failed (exit {rc}):\n{stderr}")


def resolve_output_path(input_path: Path, output_dir: Path, preset_cfg: dict[str, Any]) -> Path:
    suffix = output_suffix(preset_cfg) if preset_cfg["video_codec"] in ("libx265", "libx264") else input_path.suffix
    stem = input_path.stem
    output_path = output_dir / f"{stem}_optimized{suffix}"
    counter = 1
    while output_path.exists():
        output_path = output_dir / f"{stem}_optimized_{counter}{suffix}"
        counter += 1
    return output_path


def first_candidate_output(input_path: Path, output_dir: Path, preset_cfg: dict[str, Any]) -> Path:
    """First output path (no numeric suffix) for --skip-existing."""
    suffix = output_suffix(preset_cfg) if preset_cfg["video_codec"] in ("libx265", "libx264") else input_path.suffix
    return output_dir / f"{input_path.stem}_optimized{suffix}"


def collect_files(paths: list[str], recursive: bool, console: Console) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_file():
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(path)
            else:
                console.print(f"[yellow]Skipping unsupported file:[/yellow] {path}")
        elif path.is_dir():
            pattern = "**/*" if recursive else "*"
            for f in sorted(path.glob(pattern)):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                    files.append(f)
        else:
            console.print(f"[red]Path not found:[/red] {path}")
    return files


def optimize_file(
    input_path: Path,
    output_dir: Path,
    preset_name: str,
    preset_cfg: dict[str, Any],
    hwaccel: bool,
    keep: bool,
    *,
    console: Console,
    dry_run: bool = False,
    skip_existing: bool = False,
    progress: Progress | None = None,
    task_id: int | None = None,
    plain: bool = False,
    show_panels: bool = True,
    cancel_event: threading.Event | None = None,
) -> EncodeResult:
    original_size = os.path.getsize(input_path)

    def _cancelled_result() -> EncodeResult:
        return EncodeResult(
            input=str(input_path),
            output=None,
            success=False,
            skipped=False,
            dry_run=dry_run,
            error="Cancelled",
            original_bytes=original_size,
            new_bytes=None,
            reduction_percent=None,
            cancelled=True,
        )

    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result()

    if skip_existing:
        candidate = first_candidate_output(input_path, output_dir, preset_cfg)
        if candidate.exists():
            if show_panels and not plain:
                console.print(
                    Panel(
                        f"[dim]Skipped — output already exists:[/dim]\n[cyan]{candidate}[/cyan]",
                        title=f"[bold]{input_path.name}[/bold]",
                        border_style="yellow",
                    )
                )
            elif plain:
                console.print(f"{input_path.name}: skipped (exists: {candidate})")
            return EncodeResult(
                input=str(input_path),
                output=str(candidate),
                success=True,
                skipped=True,
                dry_run=False,
                error=None,
                original_bytes=original_size,
                new_bytes=os.path.getsize(candidate),
            reduction_percent=None,
        )

    try:
        info = probe(input_path)
    except Exception as e:
        if show_panels and not plain:
            console.print(Panel(f"[red]{e}[/red]", title=input_path.name, border_style="red"))
        elif plain:
            console.print(f"{input_path.name}: ERROR: {e}")
        return EncodeResult(
            input=str(input_path),
            output=None,
            success=False,
            skipped=False,
            dry_run=dry_run,
            error=str(e),
            original_bytes=original_size,
            new_bytes=None,
            reduction_percent=None,
        )

    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result()

    output_path = resolve_output_path(input_path, output_dir, preset_cfg)

    def on_fallback(msg: str) -> None:
        if show_panels and not plain:
            console.print(f"  [yellow]{msg}[/yellow]")

    cmd, _effective_codec = build_ffmpeg_cmd(
        input_path, output_path, preset_cfg, hwaccel, on_codec_fallback=on_fallback
    )

    subtitle = (
        f"[dim]{info.video_codec}[/dim] {info.width}x{info.height} · "
        f"{fmt_size(info.file_size)} · {fmt_duration(info.duration)}\n"
        f"[bold]Preset[/bold] {preset_name} — {preset_cfg['description']}"
    )

    if dry_run:
        if show_panels and not plain:
            console.print(
                Panel(
                    f"{subtitle}\n\n[green]Would encode to[/green]\n[cyan]{output_path}[/cyan]\n\n"
                    f"[dim]ffmpeg:[/dim] [white]{' '.join(cmd)}[/white]",
                    title=f"[bold]{input_path.name}[/bold] [dim](dry-run)[/dim]",
                    border_style="cyan",
                )
            )
        elif plain:
            console.print(f"{input_path.name} (dry-run)")
            console.print(f"  -> {output_path}")
            console.print(
                f"  {info.video_codec} {info.width}x{info.height} | "
                f"{fmt_size(info.file_size)} | {fmt_duration(info.duration)}"
            )
            console.print(f"  {' '.join(cmd)}")
        return EncodeResult(
            input=str(input_path),
            output=str(output_path),
            success=True,
            skipped=False,
            dry_run=True,
            error=None,
            original_bytes=original_size,
            new_bytes=None,
            reduction_percent=None,
        )

    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result()

    if show_panels and not plain:
        console.print(
            Panel(
                f"{subtitle}\n\n[cyan]Encoding…[/cyan]",
                title=f"[bold]{input_path.name}[/bold]",
                border_style="blue",
            )
        )

    try:
        run_encode(
            cmd,
            info.duration,
            progress=progress,
            task_id=task_id,
            plain=plain,
            cancel_event=cancel_event,
        )
    except EncodeCancelled:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        if show_panels and not plain:
            console.print(
                Panel("[dim]Encoding cancelled.[/dim]", title=input_path.name, border_style="yellow")
            )
        elif plain:
            console.print(f"{input_path.name}: cancelled")
        return _cancelled_result()
    except RuntimeError as e:
        if show_panels and not plain:
            console.print(Panel(f"[red]{e}[/red]", title=input_path.name, border_style="red"))
        elif plain:
            console.print(f"{input_path.name}: FAILED: {e}")
        return EncodeResult(
            input=str(input_path),
            output=str(output_path) if output_path.exists() else None,
            success=False,
            skipped=False,
            dry_run=False,
            error=str(e),
            original_bytes=original_size,
            new_bytes=None,
            reduction_percent=None,
        )

    new_size = os.path.getsize(output_path)
    reduction = (1 - new_size / original_size) * 100 if original_size > 0 else 0.0

    if new_size >= original_size:
        if not keep:
            output_path.unlink()
            if show_panels and not plain:
                console.print(
                    Panel(
                        f"[yellow]Output not smaller than source — removed.[/yellow]\n"
                        f"[dim]Use --keep to retain larger outputs.[/dim]",
                        title=input_path.name,
                        border_style="yellow",
                    )
                )
            elif plain:
                console.print(
                    f"{input_path.name}: output not smaller ({fmt_size(new_size)}); removed (use --keep)"
                )
            return EncodeResult(
                input=str(input_path),
                output=None,
                success=True,
                skipped=False,
                dry_run=False,
                error=None,
                original_bytes=original_size,
                new_bytes=new_size,
                reduction_percent=-abs(reduction),
            )

    border = "green" if reduction > 0 else "yellow"
    if show_panels and not plain:
        console.print(
            Panel(
                f"{fmt_size(original_size)} [bold]→[/bold] {fmt_size(new_size)}  "
                f"[bold green]−{abs(reduction):.1f}%[/bold green]\n"
                f"[cyan]{output_path}[/cyan]",
                title=f"[bold]{input_path.name}[/bold] — done",
                border_style=border,
            )
        )

    elif plain:
        console.print(
            f"{input_path.name}: {fmt_size(original_size)} -> {fmt_size(new_size)} "
            f"({reduction:+.1f}%) -> {output_path}"
        )

    return EncodeResult(
        input=str(input_path),
        output=str(output_path),
        success=True,
        skipped=False,
        dry_run=False,
        error=None,
        original_bytes=original_size,
        new_bytes=new_size,
        reduction_percent=reduction,
    )


def _make_progress(plain: bool) -> Progress | None:
    if plain:
        return None
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        transient=True,
    )


def run_optimize(
    inputs: list[str],
    *,
    preset: str,
    output_dir: str | None,
    recursive: bool,
    hwaccel: bool,
    keep: bool,
    crf: int | None,
    codec: str | None,
    dry_run: bool,
    as_json: bool,
    skip_existing: bool,
    workers: int,
    plain: bool,
    console: Console,
    container: str | None = None,
    ffmpeg_preset: str | None = None,
    audio: str = "preset",
    audio_bitrate: str | None = None,
) -> None:
    check_ffmpeg(console)

    if workers < 1:
        console.print("[red]--workers must be >= 1[/red]")
        raise typer.Exit(1)

    files = collect_files(inputs, recursive, console)
    if not files:
        console.print("[red]No supported video files found.[/red]")
        raise typer.Exit(1)

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
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    results: list[EncodeResult] = []

    if not as_json and not plain:
        header = Table.grid(padding=(0, 2))
        header.add_row("[bold magenta]Video Optimizer[/bold magenta]", "")
        console.print(Panel.fit(header, border_style="magenta"))
        meta = Table(show_header=False, box=None, padding=(0, 2))
        meta.add_row("[bold]Files[/bold]", str(len(files)))
        meta.add_row("[bold]Preset[/bold]", preset)
        meta.add_row("[bold]Codec[/bold]", str(preset_cfg["video_codec"]))
        meta.add_row("[bold]CRF[/bold]", str(preset_cfg["crf"]))
        meta.add_row("[bold]Container[/bold]", str(preset_cfg.get("container", "mp4")))
        meta.add_row("[bold]FFmpeg -preset[/bold]", str(preset_cfg.get("preset", "—")))
        meta.add_row("[bold]Audio[/bold]", str(preset_cfg.get("audio_strategy", "—")))
        meta.add_row("[bold]Workers[/bold]", str(workers))
        meta.add_row("[bold]Dry-run[/bold]", "yes" if dry_run else "no")
        console.print(meta)
        console.print()

    show_panels = not as_json and not plain
    use_progress = not plain and not as_json
    progress_ctx = _make_progress(plain) if use_progress else None

    def run_one(filepath: Path, progress: Progress | None, task_id: int | None) -> EncodeResult:
        out_dir = Path(output_dir) if output_dir else filepath.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        return optimize_file(
            filepath,
            out_dir,
            preset,
            preset_cfg,
            hwaccel,
            keep,
            console=console,
            dry_run=dry_run,
            skip_existing=skip_existing,
            progress=progress,
            task_id=task_id,
            plain=plain,
            show_panels=show_panels,
        )

    if workers == 1:
        ctx_mgr = progress_ctx if progress_ctx is not None else nullcontext()
        with ctx_mgr as prog:
            for filepath in files:
                tid: int | None = None
                if prog is not None:
                    tid = prog.add_task(filepath.name, total=100.0)
                results.append(run_one(filepath, prog, tid))
    else:
        ctx_mgr = progress_ctx if progress_ctx is not None else nullcontext()
        with ctx_mgr as prog:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures: dict[Future[EncodeResult], tuple[Path, int | None]] = {}
                for filepath in files:
                    tid = prog.add_task(filepath.name, total=100.0) if prog is not None else None
                    fut = pool.submit(run_one, filepath, prog, tid)
                    futures[fut] = (filepath, tid)
                done_map: dict[str, EncodeResult] = {}
                for fut in as_completed(futures):
                    fp, _tid = futures[fut]
                    done_map[str(fp)] = fut.result()
                results = [done_map[str(f)] for f in files]

    if as_json:
        total_orig = sum(r.original_bytes for r in results)
        total_new_bytes = 0
        for r in results:
            if r.new_bytes is not None:
                total_new_bytes += r.new_bytes
            elif r.skipped and r.output:
                p = Path(r.output)
                if p.is_file():
                    total_new_bytes += os.path.getsize(p)
                else:
                    total_new_bytes += r.original_bytes
            else:
                total_new_bytes += r.original_bytes

        out = {
            "preset": preset,
            "preset_config": {k: v for k, v in preset_cfg.items() if k != "description"},
            "results": [r.to_json_dict() for r in results],
            "summary": {
                "total_original_bytes": total_orig,
                "total_output_bytes": total_new_bytes,
                "saved_bytes": total_orig - total_new_bytes,
                "saved_percent": (1 - total_new_bytes / total_orig) * 100 if total_orig else 0.0,
            },
        }
        typer.echo(json.dumps(out, indent=2))
        return

    if len(results) > 1 or any(r.skipped or r.dry_run or r.cancelled or not r.success for r in results):
        summary = Table(title="Summary", box=box.ROUNDED, show_lines=True)
        summary.add_column("Input", style="cyan", no_wrap=True)
        summary.add_column("Status", style="bold")
        summary.add_column("Before", justify="right")
        summary.add_column("After", justify="right")
        summary.add_column("Δ %", justify="right")

        total_orig = 0
        total_new = 0

        for r in results:
            total_orig += r.original_bytes
            if r.skipped:
                summary.add_row(
                    Path(r.input).name,
                    "[yellow]skipped[/yellow]",
                    fmt_size(r.original_bytes),
                    fmt_size(r.new_bytes or 0),
                    "—",
                )
                total_new += r.new_bytes or r.original_bytes
            elif r.dry_run:
                summary.add_row(Path(r.input).name, "[cyan]dry-run[/cyan]", fmt_size(r.original_bytes), "—", "—")
                total_new += r.original_bytes
            elif r.cancelled:
                summary.add_row(Path(r.input).name, "[dim]cancelled[/dim]", fmt_size(r.original_bytes), "—", "—")
                total_new += r.original_bytes
            elif not r.success:
                summary.add_row(Path(r.input).name, "[red]failed[/red]", fmt_size(r.original_bytes), "—", "—")
                total_new += r.original_bytes
            elif r.new_bytes is None:
                summary.add_row(Path(r.input).name, "[dim]no output[/dim]", fmt_size(r.original_bytes), "—", "—")
                total_new += r.original_bytes
            else:
                total_new += r.new_bytes
                pct = r.reduction_percent or 0.0
                st = "[green]ok[/green]" if pct > 0 else "[yellow]ok[/yellow]"
                summary.add_row(
                    Path(r.input).name,
                    st,
                    fmt_size(r.original_bytes),
                    fmt_size(r.new_bytes),
                    f"{pct:+.1f}%",
                )

        console.print()
        console.print(summary)

        if len(results) > 1 and total_orig > 0:
            saved_pct = (1 - total_new / total_orig) * 100
            sign = "−" if saved_pct > 0 else "+"
            console.print(
                f"\n[bold]Total[/bold]  {fmt_size(total_orig)} → {fmt_size(total_new)}  "
                f"([bold green]{sign}{abs(saved_pct):.1f}%[/bold green])  "
                f"[dim]saved {fmt_size(total_orig - total_new)}[/dim]\n"
            )


def render_presets_table(console: Console) -> None:
    table = Table(title="Compression presets", box=box.ROUNDED, show_lines=True)
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    table.add_column("Codec", style="magenta")
    table.add_column("CRF", justify="right")
    table.add_column("FFmpeg preset", style="dim")

    for name, cfg in PRESETS.items():
        table.add_row(
            name,
            str(cfg["description"]),
            str(cfg["video_codec"]),
            str(cfg["crf"]),
            str(cfg["preset"]),
        )
    console.print(table)


def render_info(path: Path, console: Console) -> None:
    check_ffmpeg(console)
    if not path.is_file():
        console.print(f"[red]Not a file:[/red] {path}")
        raise typer.Exit(1)

    data = ffprobe_json(path)
    fmt = data.get("format", {})

    meta = Table(show_header=False, box=None, title=f"[bold]{path.name}[/bold]", padding=(0, 1))
    meta.add_row("[bold]Path[/bold]", str(path.resolve()))
    meta.add_row("[bold]Container[/bold]", str(fmt.get("format_name", "—")))
    meta.add_row("[bold]Duration[/bold]", fmt_duration(float(fmt.get("duration", 0))))
    meta.add_row("[bold]Size[/bold]", fmt_size(os.path.getsize(path)))
    br = fmt.get("bit_rate")
    meta.add_row("[bold]Bitrate[/bold]", f"{int(br) // 1000} kbps" if br else "—")
    console.print(Panel(meta, border_style="blue", title="Format"))

    streams = Table(box=box.ROUNDED, title="Streams", show_lines=True)
    streams.add_column("#", style="dim", justify="right")
    streams.add_column("Type", style="bold")
    streams.add_column("Codec", style="cyan")
    streams.add_column("Details")

    for i, s in enumerate(data.get("streams", [])):
        stype = s.get("codec_type", "?")
        codec = s.get("codec_name", "—")
        details_parts: list[str] = []
        if stype == "video":
            details_parts.append(f"{s.get('width', '?')}×{s.get('height', '?')}")
            if fr := s.get("r_frame_rate"):
                details_parts.append(f"{fr} fps")
            if pix := s.get("pix_fmt"):
                details_parts.append(pix)
        elif stype == "audio":
            if ch := s.get("channels"):
                details_parts.append(f"{ch} ch")
            if sr := s.get("sample_rate"):
                details_parts.append(f"{sr} Hz")
        brs = s.get("bit_rate")
        if brs:
            details_parts.append(f"{int(brs) // 1000} kbps")
        streams.add_row(str(i), stype, codec, " · ".join(details_parts) if details_parts else "—")

    console.print(streams)


# --- Typer application ---------------------------------------------------------

app = typer.Typer(
    name="vopt",
    rich_markup_mode="rich",
    help=(
        "Shrink video files with FFmpeg using [bold]CRF[/bold]-based H.265/H.264 encodes "
        "(quality-first, not fixed bitrate).\n\n"
        "[bold]Commands[/bold]\n"
        "  [cyan]optimize[/cyan]  … Re-encode files or folders ([dim]default if you omit the subcommand[/dim]).\n"
        "  [cyan]presets[/cyan]   … Table of [bold]light[/bold] / [bold]medium[/bold] / [bold]aggressive[/bold] (CRF, codec, audio, FFmpeg -preset).\n"
        "  [cyan]info[/cyan]      … [bold]ffprobe[/bold] summary: container, streams, resolution, bitrates.\n"
        "  [cyan]watch[/cyan]     … Watch a directory and optimize new videos ([dim]requires watchdog[/dim]).\n\n"
        "[bold]Encoding basics[/bold]\n"
        "  • [bold]CRF[/bold] targets perceptual quality; file size varies with content.\n"
        "  • [bold]H.265[/bold] (default) usually beats H.264 at the same visual quality.\n"
        "  • [bold]Audio[/bold]: [italic]light[/italic] copies audio; [italic]medium[/italic]/[italic]aggressive[/italic] re-encode to AAC.\n"
        "  • Outputs are [bold]<name>_optimized.mp4[/bold] (numeric suffix if that name exists).\n\n"
        "Requires [bold]ffmpeg[/bold] and [bold]ffprobe[/bold] on PATH."
    ),
    no_args_is_help=True,
    epilog=(
        "Examples:  python video_optimizer.py optimize clip.mp4\n"
        "            python video_optimizer.py optimize -p light -o ./out/ ./src/\n"
        "            python video_optimizer.py optimize --dry-run --json clip.mp4\n"
        "            python video_optimizer.py presets\n"
        "            python video_optimizer.py info clip.mp4\n"
        "            python video_optimizer.py watch ./incoming/ -o ./out/"
    ),
)


@app.command(
    "optimize",
    epilog=(
        "Preset vs tuning: use [bold]-p[/bold] for everyday choices; use [bold]--crf[/bold]/[bold]--codec[/bold] "
        "when you need an exact quality point or player compatibility. "
        "See [bold]python video_optimizer.py presets[/bold] for the built-in table."
    ),
)
def optimize_cmd(
    inputs: list[str] = typer.Argument(
        ...,
        help=(
            "One or more video files and/or directories. "
            f"Recognized extensions: {', '.join(sorted(SUPPORTED_EXTENSIONS))}. "
            "Directories need [bold]-r[/bold] to include subfolders. "
            "Outputs go next to each input unless [bold]-o[/bold] is set."
        ),
    ),
    preset: str = typer.Option("medium", "--preset", "-p", help=HELP_PRESET_OPTION),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Write all outputs under this folder (created if missing). Default: each file's directory.",
    ),
    recursive: bool = typer.Option(
        False,
        "--recursive",
        "-r",
        help="When an input is a directory, include supported videos in subdirectories (recursive glob).",
    ),
    hwaccel: bool = typer.Option(
        False,
        "--hwaccel",
        help="Pass [bold]-hwaccel auto[/bold] to FFmpeg for decode acceleration when available (GPU/driver dependent).",
    ),
    keep: bool = typer.Option(
        False,
        "--keep",
        help="If the encoded file is not smaller than the source, keep it anyway. Default: delete the larger output.",
    ),
    crf: int | None = typer.Option(None, "--crf", help=HELP_CRF),
    codec: str | None = typer.Option(None, "--codec", help=HELP_CODEC),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run ffprobe and print the output path + full ffmpeg command; do not encode or write files.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="After processing, print a JSON object (results + totals) to stdout. Implies no Rich progress UI.",
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        help="If [bold]<stem>_optimized[/bold] with the usual extension already exists in the output dir, skip that input.",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        "-j",
        help=(
            "Number of files to encode in parallel. Each job is a full ffmpeg process — "
            "use 2–4 on multi-core machines; higher can thrash disk I/O. [bold]1[/bold] is sequential."
        ),
    ),
    plain: bool = typer.Option(
        False,
        "--plain",
        "--no-color",
        help="Disable colors and Rich progress bars (logs/CI). Errors and dry-run still print plain text lines.",
    ),
    container: str | None = typer.Option(
        None,
        "--container",
        "-f",
        help=HELP_CONTAINER,
    ),
    ffmpeg_preset: str | None = typer.Option(
        None,
        "--ffmpeg-preset",
        help=HELP_FFMPEG_PRESET,
    ),
    audio: str = typer.Option("preset", "--audio", help=HELP_AUDIO),
    audio_bitrate: str | None = typer.Option(None, "--audio-bitrate", help=HELP_AUDIO_BITRATE),
) -> None:
    """Re-encode inputs with a [bold]preset[/bold] (CRF + codec + audio policy).

    [bold]Presets[/bold] bundle: video codec (H.265 by default), CRF value, FFmpeg [bold]-preset[/bold] speed
    ([italic]slow[/italic] trades CPU time for smaller files in [bold]aggressive[/bold]), and audio handling
    ([italic]copy[/italic] vs AAC bitrate). Override CRF or codec only when you need finer control than [bold]-p[/bold].

    [bold]Outputs[/bold] use MP4 with [bold]+faststart[/bold] for web-friendly layout. HEVC streams are tagged [bold]hvc1[/bold]
    for better Apple/QuickTime compatibility.
    """
    if preset not in PRESETS:
        typer.echo(f"Unknown preset {preset!r}. Choose: {', '.join(PRESETS)}", err=True)
        raise typer.Exit(1)
    if codec and codec not in ("h264", "h265"):
        typer.echo("--codec must be h264 or h265", err=True)
        raise typer.Exit(1)
    if audio not in ("preset", "copy", "aac", "none"):
        typer.echo("--audio must be preset, copy, aac, or none", err=True)
        raise typer.Exit(1)

    color_system = None if plain else "standard"
    force_terminal = not plain
    console = Console(color_system=color_system, force_terminal=force_terminal, no_color=plain)

    run_optimize(
        inputs,
        preset=preset,
        output_dir=output_dir,
        recursive=recursive,
        hwaccel=hwaccel,
        keep=keep,
        crf=crf,
        codec=codec,
        dry_run=dry_run,
        as_json=as_json,
        skip_existing=skip_existing,
        workers=workers,
        plain=plain,
        console=console,
        container=container,
        ffmpeg_preset=ffmpeg_preset,
        audio=audio,
        audio_bitrate=audio_bitrate,
    )


@app.command(
    "presets",
    epilog=(
        "Columns: [bold]Codec[/bold] = FFmpeg video encoder; [bold]CRF[/bold] = quality/size knob for that encoder; "
        "[bold]FFmpeg preset[/bold] = encoder speed ([italic]slow[/italic] squeezes more bits per CPU second). "
        "These are starting points — combine with [bold]vopt optimize --crf[/bold] for fine tuning."
    ),
)
def presets_cmd(
    plain: bool = typer.Option(
        False,
        "--plain",
        "--no-color",
        help="Disable color markup; table borders still use Unicode box drawing.",
    ),
) -> None:
    """Print the built-in [bold]light[/bold] / [bold]medium[/bold] / [bold]aggressive[/bold] recipes.

    Each row is what [bold]optimize -p <name>[/bold] applies: CRF, video codec, audio policy
    (copy vs AAC), and FFmpeg's [bold]-preset[/bold] (encoding speed; [italic]aggressive[/italic] uses
    [dim]slow[/dim] for more compression per CPU second than [italic]light[/italic]/[italic]medium[/italic]).

    Does not run ffmpeg — safe to run anywhere for a quick reference.
    """
    console = Console(no_color=plain)
    render_presets_table(console)


@app.command(
    "info",
    epilog=(
        "Uses [bold]ffprobe[/bold] JSON: [bold]Format[/bold] shows container, duration, aggregate bitrate; "
        "[bold]Streams[/bold] lists each video/audio track with resolution, frame rate, sample rate, and per-stream bitrate."
    ),
)
def info_cmd(
    path: Path = typer.Argument(
        ...,
        help="Media file to analyze (must exist). Any container ffprobe understands is fine; optimizer presets target common video extensions.",
        exists=True,
        readable=True,
    ),
    plain: bool = typer.Option(
        False,
        "--plain",
        "--no-color",
        help="Disable color markup in Rich output.",
    ),
) -> None:
    """Show container and stream details via [bold]ffprobe[/bold].

    Useful before encoding to see codecs, resolution, and whether audio exists (presets may copy or re-encode audio).
    Requires [bold]ffprobe[/bold] on PATH.
    """
    console = Console(no_color=plain)
    render_info(path, console)


@app.command(
    "watch",
    epilog=(
        "New files are debounced until size stabilizes, then encoded with the same rules as [bold]optimize[/bold]. "
        "If an optimized sibling already exists, the file is skipped. Requires the [bold]watchdog[/bold] package."
    ),
)
def watch_cmd(
    directory: Path = typer.Argument(
        ...,
        help="Root directory to watch recursively for creates/modifies on supported video extensions.",
        exists=True,
        file_okay=False,
    ),
    preset: str = typer.Option("medium", "--preset", "-p", help=HELP_PRESET_OPTION),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Where to write optimized files. Default: same directory as the new file.",
    ),
    hwaccel: bool = typer.Option(False, "--hwaccel", help="Same as [bold]optimize --hwaccel[/bold]."),
    keep: bool = typer.Option(False, "--keep", help="Same as [bold]optimize --keep[/bold]."),
    crf: int | None = typer.Option(None, "--crf", help=HELP_CRF),
    codec: str | None = typer.Option(None, "--codec", help=HELP_CODEC),
    plain: bool = typer.Option(
        False,
        "--plain",
        "--no-color",
        help="Same as [bold]optimize --plain[/bold]: no colors / no Rich progress.",
    ),
    container: str | None = typer.Option(
        None,
        "--container",
        "-f",
        help=HELP_CONTAINER,
    ),
    ffmpeg_preset: str | None = typer.Option(
        None,
        "--ffmpeg-preset",
        help=HELP_FFMPEG_PRESET,
    ),
    audio: str = typer.Option("preset", "--audio", help=HELP_AUDIO),
    audio_bitrate: str | None = typer.Option(None, "--audio-bitrate", help=HELP_AUDIO_BITRATE),
) -> None:
    """Watch a directory tree and run [bold]optimize[/bold] when new videos appear.

    The observer listens for file events under the given path, waits until the file size stops changing,
    then encodes with your [bold]-p[/bold] / [bold]--crf[/bold] / [bold]--codec[/bold] settings. Press Ctrl+C to stop.
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        typer.echo("watch requires the 'watchdog' package: pip install watchdog", err=True)
        raise typer.Exit(1)

    if preset not in PRESETS:
        typer.echo(f"Unknown preset {preset!r}", err=True)
        raise typer.Exit(1)
    if codec and codec not in ("h264", "h265"):
        typer.echo("--codec must be h264 or h265", err=True)
        raise typer.Exit(1)
    if audio not in ("preset", "copy", "aac", "none"):
        typer.echo("--audio must be preset, copy, aac, or none", err=True)
        raise typer.Exit(1)

    console = Console(no_color=plain)
    check_ffmpeg(console)

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
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    pending: dict[str, float] = {}
    lock = threading.Lock()

    def wait_stable(p: Path, attempts: int = 20, delay: float = 0.25) -> bool:
        last = -1
        stable = 0
        for _ in range(attempts):
            if not p.is_file():
                return False
            sz = p.stat().st_size
            if sz == last and sz > 0:
                stable += 1
                if stable >= 2:
                    return True
            else:
                stable = 0
            last = sz
            time.sleep(delay)
        return p.is_file() and p.stat().st_size > 0

    class Handler(FileSystemEventHandler):
        def on_created(self, event: object) -> None:
            if getattr(event, "is_directory", False):
                return
            src = Path(str(getattr(event, "src_path", "")))
            if src.suffix.lower() not in SUPPORTED_EXTENSIONS:
                return
            key = str(src.resolve())
            with lock:
                pending[key] = time.time()

        def on_modified(self, event: object) -> None:
            self.on_created(event)

    def process_one(src: Path) -> None:
        out_dir = Path(output_dir) if output_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        console.rule(f"[bold green]New file[/bold green] {src.name}")
        prog = _make_progress(plain)
        if prog is not None:
            with prog:
                tid = prog.add_task(src.name, total=100.0)
                optimize_file(
                    src,
                    out_dir,
                    preset,
                    preset_cfg,
                    hwaccel,
                    keep,
                    console=console,
                    dry_run=False,
                    skip_existing=True,
                    progress=prog,
                    task_id=tid,
                    plain=plain,
                    show_panels=not plain,
                )
        else:
            optimize_file(
                src,
                out_dir,
                preset,
                preset_cfg,
                hwaccel,
                keep,
                console=console,
                dry_run=False,
                skip_existing=True,
                progress=None,
                task_id=None,
                plain=plain,
                show_panels=not plain,
            )

    def process_loop() -> None:
        while True:
            time.sleep(0.5)
            now = time.time()
            with lock:
                keys = [k for k, t in pending.items() if now - t >= 1.0]
                for k in keys:
                    del pending[k]
            for key in keys:
                src = Path(key)
                if not wait_stable(src):
                    continue
                process_one(src)

    observer = Observer()
    handler = Handler()
    observer.schedule(handler, str(directory.resolve()), recursive=True)
    observer.start()
    console.print(Panel.fit(
        f"[bold]Watching[/bold] [cyan]{directory.resolve()}[/cyan]\n"
        f"preset=[magenta]{preset}[/magenta]  container=[magenta]{preset_cfg.get('container', 'mp4')}[/magenta]  "
        f"output_dir={output_dir or '[dim]same as file[/dim]'}\n"
        "[dim]Ctrl+C to stop[/dim]",
        title="vopt watch",
        border_style="green",
    ))

    try:
        process_loop()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    finally:
        observer.stop()
        observer.join(timeout=5)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help", "--version"):
        sys.argv.insert(1, "optimize")
    app()


if __name__ == "__main__":
    main()
