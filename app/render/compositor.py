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
    caption_png: str | None,
    audio_path: str,
    output_path: str,
    reel_duration: float,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
) -> str:
    """shots: [{src_path, src_start, duration}] in order. Cuts on beats come from the sequencer.
    caption_png=None -> no text overlay (a blank-caption reel: just the beat-cut clips + audio)."""
    width = width or settings.reel_width
    height = height or settings.reel_height
    fps = fps or settings.reel_fps
    n = len(shots)
    has_cap = bool(caption_png)

    cmd = ["ffmpeg", "-y"]
    for sh in shots:  # one input per shot, seeking to the sub-window (decoder autorotates)
        cmd += ["-ss", f"{sh['src_start']:.3f}", "-t", f"{sh['duration']:.3f}", "-i", sh["src_path"]]
    if has_cap:
        cmd += ["-loop", "1", "-i", caption_png]                               # input n: caption
    audio_idx = n + (1 if has_cap else 0)
    cmd += ["-ss", "0", "-t", f"{reel_duration:.3f}", "-i", audio_path]        # input audio_idx: audio

    chains = []
    for i in range(n):
        chains.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps},setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
    chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]")
    if has_cap:                                              # overlay the caption only when present
        chains.append(f"[cat][{n}:v]overlay=0:0:eof_action=pass[outv]")
        vmap = "[outv]"
    else:
        vmap = "[cat]"
    chains.append(f"[{audio_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11[outa]")
    filtergraph = ";".join(chains)

    cmd += [
        "-filter_complex", filtergraph,
        "-map", vmap, "-map", "[outa]",
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
    video_chunks: list[dict],
    caption_windows: list[dict],
    audio_path: str,
    output_path: str,
    total_duration: float,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
) -> str:
    """Multi-segment template reel. Video and captions are DECOUPLED so one authored segment can be
    filled by several clips (gap-filling) and a short tail can be trimmed rather than frozen:

      video_chunks:    [{src_path, src_start, duration}] concatenated in order. A chunk whose source
                       is shorter than `duration` freeze-holds its last frame (tpad) — the caller
                       chooses `duration` so this only happens for tiny, intentional mid-reel holds.
      caption_windows: [{caption_png|None, t_in, t_out}] overlaid by time window on the final timeline.

    `total_duration` is the real content length (audio is trimmed to it, with a short fade-out so a
    trimmed end isn't an abrupt cut)."""
    width = width or settings.reel_width
    height = height or settings.reel_height
    fps = fps or settings.reel_fps
    n = len(video_chunks)
    cap_wins = [c for c in caption_windows if c.get("caption_png")]
    total_q = round(total_duration * fps) / fps            # frame-align caps so audio/video end together
    frame = 1.0 / fps

    cmd = ["ffmpeg", "-y"]
    for ch in video_chunks:                              # inputs 0..n-1: the clips
        cmd += ["-ss", f"{float(ch.get('src_start', 0.0)):.3f}", "-i", ch["src_path"]]
    for c in cap_wins:                                   # inputs n..n+C-1: the caption PNGs
        cmd += ["-loop", "1", "-i", c["caption_png"]]
    cmd += ["-ss", "0", "-t", f"{total_q:.3f}", "-i", audio_path]   # input n+C: audio

    chains = []
    for i, ch in enumerate(video_chunks):
        d = float(ch["duration"])
        chains.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={d:.3f},trim=duration={d:.3f},"
            f"setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
    chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]")

    cur = "cat"
    for j, c in enumerate(cap_wins):
        cin = n + j                                       # this caption's input index
        nxt = f"o{j}"
        t_in = float(c["t_in"])
        t_out = float(c["t_out"])
        if j < len(cap_wins) - 1:                         # half-open: the boundary frame -> the NEXT caption
            t_out = max(t_in, t_out - 0.5 * frame)
        chains.append(
            f"[{cur}][{cin}:v]overlay=0:0:enable='between(t,{t_in:.3f},{t_out:.3f})':eof_action=pass[{nxt}]"
        )
        cur = nxt
    audio_idx = n + len(cap_wins)
    audio_chain = f"[{audio_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11"
    fade_d = min(0.3, total_q)
    if fade_d > 0.0:                                      # always fade the end (clamped for short reels)
        audio_chain += f",afade=t=out:st={max(0.0, total_q - fade_d):.3f}:d={fade_d:.3f}"
    audio_chain += "[outa]"
    chains.append(audio_chain)

    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", f"[{cur}]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total_q:.3f}", "-movflags", "+faststart",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg template compose failed:\n" + proc.stderr[-3500:])
    return output_path
