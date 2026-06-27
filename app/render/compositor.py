"""Compositor — assemble the reel with FFmpeg (single final encode).

Per shot: input-seek to the chosen sub-window, scale+crop to 9:16, normalize fps/sar. Concat
the shots (cuts land on beats, by construction), overlay the static caption PNG, bake the
audio (loudnorm ~-14 LUFS), one libx264 encode (BT.709, yuv420p, AAC). No mid-pipeline
re-encode, no downscale below the 1080p target.
"""
from __future__ import annotations

import subprocess

from app.config import settings


def compose_reel(
    shots: list[dict],
    caption_png: str,
    audio_path: str,
    output_path: str,
    reel_duration: float,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
) -> str:
    """shots: [{src_path, src_start, duration}] in order. Cuts on beats come from the sequencer."""
    width = width or settings.reel_width
    height = height or settings.reel_height
    fps = fps or settings.reel_fps
    n = len(shots)

    cmd = ["ffmpeg", "-y"]
    for sh in shots:  # one input per shot, seeking to the sub-window (decoder autorotates)
        cmd += ["-ss", f"{sh['src_start']:.3f}", "-t", f"{sh['duration']:.3f}", "-i", sh["src_path"]]
    cmd += ["-loop", "1", "-i", caption_png]                                   # input n: caption
    cmd += ["-ss", "0", "-t", f"{reel_duration:.3f}", "-i", audio_path]        # input n+1: audio

    chains = []
    for i in range(n):
        chains.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps},setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
    chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]")
    chains.append(f"[cat][{n}:v]overlay=0:0:eof_action=pass[outv]")
    chains.append(f"[{n + 1}:a]loudnorm=I=-14:TP=-1.5:LRA=11[outa]")
    filtergraph = ";".join(chains)

    cmd += [
        "-filter_complex", filtergraph,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{reel_duration:.3f}", "-movflags", "+faststart",
        output_path,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg compose failed:\n" + proc.stderr[-3500:])
    return output_path


def compose_template_reel(
    segments: list[dict],
    audio_path: str,
    output_path: str,
    total_duration: float,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
) -> str:
    """Multi-segment template reel: each segment is its OWN clip with its OWN caption, shown only
    over that segment's window. segments: [{src_path, src_start, duration, t_in, t_out, caption_png|None}]
    in order. Short clips freeze-hold their last frame (tpad) so a segment is never short."""
    width = width or settings.reel_width
    height = height or settings.reel_height
    fps = fps or settings.reel_fps
    n = len(segments)
    cap_segs = [sg for sg in segments if sg.get("caption_png")]

    cmd = ["ffmpeg", "-y"]
    for sg in segments:                                  # inputs 0..n-1: the clips
        cmd += ["-ss", f"{float(sg.get('src_start', 0.0)):.3f}", "-i", sg["src_path"]]
    for sg in cap_segs:                                  # inputs n..n+C-1: the caption PNGs
        cmd += ["-loop", "1", "-i", sg["caption_png"]]
    cmd += ["-ss", "0", "-t", f"{total_duration:.3f}", "-i", audio_path]   # input n+C: audio

    chains = []
    for i, sg in enumerate(segments):
        d = float(sg["duration"])
        chains.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={d:.3f},trim=duration={d:.3f},"
            f"setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
    chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]")

    cur = "cat"
    for j, sg in enumerate(cap_segs):
        cin = n + j                                       # this caption's input index
        nxt = f"o{j}"
        chains.append(
            f"[{cur}][{cin}:v]overlay=0:0:enable='between(t,{float(sg['t_in']):.3f},{float(sg['t_out']):.3f})'"
            f":eof_action=pass[{nxt}]"
        )
        cur = nxt
    audio_idx = n + len(cap_segs)
    chains.append(f"[{audio_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11[outa]")

    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", f"[{cur}]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total_duration:.3f}", "-movflags", "+faststart",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg template compose failed:\n" + proc.stderr[-3500:])
    return output_path
