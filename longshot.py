#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy"]
# ///
"""longshot — experimental long / full-page screenshot by scroll-and-stitch.

Standalone playground (does NOT touch droidshot.py). The whole point is to make
the stitching algorithm easy to iterate on:

  ./longshot.py capture            # scroll a screen, save frames, stitch -> long.png
  ./longshot.py stitch <framedir>  # re-stitch saved frames (no device needed)

So you capture ONCE (slow, needs a phone), then tune the algorithm by re-running
`stitch` over the saved frames as many times as you like.

THE ALGORITHM
-------------
A `screencap` only ever sees the viewport. To fake a taller image we scroll and
glue the newly-revealed slivers together. Two things make this non-trivial:

  1. Fixed chrome. The status bar + app/action bar (top) and the nav/gesture bar
     (bottom) DON'T scroll. If you naively stack frames you get the app bar
     repeated every screen. So we must find the static top band `t` and bottom
     band `b`, keep them ONCE (t at the very top, b at the very bottom), and only
     stack the scrolling region in between.

  2. Unknown scroll distance. Each swipe (plus fling momentum) moves the content
     by some `d` pixels we don't control precisely. We *measure* d per frame pair
     by cross-correlating a vertical strip (minimize mean-abs-difference over
     candidate shifts) — the pixels tell us how far they moved.

Detecting chrome: when content has scrolled by d>0, the rows that are still
identical *in place* (zero shift) between two consecutive frames are, by
definition, the non-scrolling chrome. We take the contiguous static run from the
top edge (-> t) and from the bottom edge (-> b), and use the median across all
frame pairs for stability.

Stitch: top-chrome(frame0) + band(frame0) + [for each later frame: its bottom d
newly-revealed rows] + bottom-chrome(last frame).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor" / "platform-tools"


# --------------------------------------------------------------------------- #
# minimal adb plumbing (kept independent of droidshot.py on purpose)
# --------------------------------------------------------------------------- #
def adb_bin() -> str:
    override = os.environ.get("DROIDSHOT_ADB")
    if override:
        return override
    exe = "adb.exe" if platform.system() == "Windows" else "adb"
    vendored = VENDOR / exe
    if vendored.exists():
        return str(vendored)
    sys.exit("adb not found. Run droidshot.py setup, or set DROIDSHOT_ADB.")


def adb(args: list[str], *, serial: str | None = None, binary: bool = False,
        timeout: float | None = None):
    cmd = [adb_bin()]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: "
                           f"{res.stderr.decode(errors='replace').strip()}")
    return res.stdout if binary else res.stdout.decode(errors="replace")


def pick_serial(requested: str | None) -> str:
    out = adb(["devices"])
    devs = [l.split("\t")[0] for l in out.splitlines()[1:]
            if "\tdevice" in l]
    if not devs:
        sys.exit("No authorized device connected.")
    if requested:
        if requested not in devs:
            sys.exit(f"{requested} not found. Connected: {', '.join(devs)}")
        return requested
    if len(devs) > 1:
        sys.exit(f"Multiple devices; pass --serial. Connected: {', '.join(devs)}")
    return devs[0]


def display_size(serial: str) -> tuple[int, int]:
    m = re.search(r"(\d+)x(\d+)", adb(["shell", "wm", "size"], serial=serial))
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)


def device_insets(serial: str) -> dict:
    """System bar heights (px) from the window manager. The gesture-nav bar is
    the key one: it sits over edge-to-edge content, so it's NOT full-width-static
    in the pixels and our chrome detector misses it — but the WM knows its exact
    height. Returns {'top': statusBar, 'bottom': navBar}."""
    try:
        out = adb(["shell", "dumpsys", "window"], serial=serial, timeout=10.0)
    except RuntimeError:
        return {"top": 0, "bottom": 0}

    def bar(kind: str) -> int:
        # frame=[x1,y1][x2,y2]; the bar's thickness is y2 - y1
        m = re.search(rf"type={kind} frame=\[\d+,(\d+)\]\[\d+,(\d+)\]", out)
        return int(m.group(2)) - int(m.group(1)) if m else 0

    return {"top": bar("statusBars"), "bottom": bar("navigationBars")}


def screencap(serial: str) -> bytes:
    return adb(["exec-out", "screencap", "-p"], serial=serial, binary=True)


# --------------------------------------------------------------------------- #
# image helpers
# --------------------------------------------------------------------------- #
GRAY_W = 100  # downscale width for analysis; height kept FULL so the measured
              # vertical offset is in real pixels (no vertical rescaling)


def gray(png: bytes):
    """Full-height, narrow grayscale array (H, GRAY_W) as float32."""
    import numpy as np
    from PIL import Image
    img = Image.open(BytesIO(png)).convert("L")
    w, h = img.size
    img = img.resize((GRAY_W, h))  # width->GRAY_W, height unchanged
    return np.asarray(img, dtype=np.float32)


def frames_equal(a, b, thresh: float = 2.0) -> bool:
    """Tolerant equality on gray arrays (ignores a ticking clock etc.)."""
    import numpy as np
    return float(np.abs(a - b).mean()) < thresh


def scroll_delta(A, B) -> tuple[int, float]:
    """Pixels the content moved UP from frame A to frame B (0 = no scroll).

    Feature at row r in A appears at row r-d in B, i.e.
        A[top+d : top+d+win]  ~=  B[top : top+win]
    We minimise mean-abs-difference over candidate shifts d. A generous interior
    band ignores top/bottom chrome; win = band/3 leaves a wide search range so a
    fling that moved a lot is still measurable. Returns (d, score)."""
    import numpy as np
    H = A.shape[0]
    top = int(H * 0.12)
    bot = int(H * 0.95)
    band = bot - top
    win = band // 3
    max_shift = band - win
    ref = B[top:top + win]
    best_d, best = 0, None
    for d in range(max_shift):
        score = float(np.abs(A[top + d:top + d + win] - ref).mean())
        if best is None or score < best:
            best, best_d = score, d
    return best_d, (best if best is not None else 0.0)


def chrome_bands(A, B, thresh: float = 4.0) -> tuple[int, int]:
    """Given two frames that DID scroll, find the fixed top/bottom chrome heights:
    the contiguous run of rows from each edge that stay identical in place."""
    import numpy as np
    rowdiff = np.abs(A - B).mean(axis=1)  # per-row mean-abs-diff at zero shift
    H = len(rowdiff)
    t = 0
    while t < H and rowdiff[t] < thresh:
        t += 1
    b = 0
    while b < H and rowdiff[H - 1 - b] < thresh:
        b += 1
    return t, b


# --------------------------------------------------------------------------- #
# the stitcher
# --------------------------------------------------------------------------- #
def stitch(pngs: list[bytes], *, top_override: int | None = None,
           bot_override: int | None = None, bot_floor: int = 0,
           min_move: int = 6, verbose=True):
    """Stitch a list of viewport PNGs (top-of-screen first) into one tall image.
    Returns (PIL.Image, diagnostics dict)."""
    from PIL import Image

    if len(pngs) < 2:
        return Image.open(BytesIO(pngs[0])).convert("RGB"), {"frames": len(pngs)}

    grays = [gray(p) for p in pngs]
    H = grays[0].shape[0]

    # measure scroll delta + chrome for every consecutive pair
    deltas, ts, bs = [], [], []
    for i in range(len(grays) - 1):
        d, score = scroll_delta(grays[i], grays[i + 1])
        deltas.append(d)
        if d >= min_move:  # only frames that actually scrolled reveal chrome
            t, b = chrome_bands(grays[i], grays[i + 1])
            ts.append(t)
            bs.append(b)

    t = top_override if top_override is not None else (int(statistics.median(ts)) if ts else 0)
    if bot_override is not None:
        b = bot_override
    else:
        # the gesture-nav bar isn't full-width-static, so pixel detection misses
        # it; floor the bottom chrome at the WM-reported nav inset so its pill is
        # cropped from every revealed sliver (and shown once, from the last frame)
        b = max(int(statistics.median(bs)) if bs else 0, bot_floor)

    frames = [Image.open(BytesIO(p)).convert("RGB") for p in pngs]
    W = frames[0].width
    band0_h = H - b - t

    # canvas height = top chrome + first window of content + all revealed slivers
    revealed = [d for d in deltas if d >= min_move]
    canvas_h = t + band0_h + sum(revealed) + b
    canvas = Image.new("RGB", (W, canvas_h))

    # `seams` records, in STITCHED coordinates, where each frame's contribution
    # starts — so the viewer can draw the join lines and highlight what each
    # frame added. `frame_y` is the same in content space (cumulative scroll).
    seams = []
    y = 0
    canvas.paste(frames[0].crop((0, 0, W, t)), (0, y)); y += t        # top chrome
    seams.append({"frame": 0, "y": y, "h": band0_h})                 # frame0 band
    canvas.paste(frames[0].crop((0, t, W, H - b)), (0, y)); y += band0_h

    for i, d in enumerate(deltas, start=1):
        if d < min_move:
            continue  # nothing new (bounce / bottom) — skip
        seams.append({"frame": i, "y": y, "h": d})
        strip = frames[i].crop((0, H - b - d, W, H - b))  # bottom d rows of band
        canvas.paste(strip, (0, y)); y += d

    canvas.paste(frames[-1].crop((0, H - b, W, H)), (0, y))  # bottom chrome

    frame_y = [0]
    for d in deltas:
        frame_y.append(frame_y[-1] + d)

    diag = {"frames": len(pngs), "H": H, "top_chrome": t, "bottom_chrome": b,
            "deltas": deltas, "content_px": band0_h + sum(revealed),
            "out_size": (W, canvas_h), "seams": seams, "frame_y": frame_y}
    if verbose:
        print(f"  frames={len(pngs)}  viewport={W}x{H}")
        floor_note = f" (nav-inset floor {bot_floor})" if bot_floor and b == bot_floor else ""
        print(f"  top chrome={t}px  bottom chrome={b}px{floor_note}")
        print(f"  per-swipe deltas={deltas}")
        print(f"  stitched size={W}x{canvas_h}  "
              f"({canvas_h / H:.1f}x viewport height)")
    return canvas, diag


# --------------------------------------------------------------------------- #
# .longshot bundle (zip: manifest + raw frames + stitched) for the viewer
# --------------------------------------------------------------------------- #
def write_bundle(out_path: Path, pngs: list[bytes], stitched_png: bytes,
                 diag: dict) -> None:
    """Package the raw frames, the stitched image, and the stitch diagnostics
    into one .longshot file the viewer's /longshot route can open."""
    W, _ = diag["out_size"]
    deltas = [0] + diag.get("deltas", [])  # frame0 has no incoming delta
    frame_y = diag.get("frame_y", [])
    frames_meta = [
        {"file": f"frames/frame_{i:03d}.png",
         "delta": deltas[i] if i < len(deltas) else 0,
         "y": frame_y[i] if i < len(frame_y) else 0}
        for i in range(len(pngs))
    ]
    manifest = {
        "tool": "longshot/0.1",
        "device": {"w": W, "h": diag["H"]},
        "viewport": [W, diag["H"]],
        "topChrome": diag["top_chrome"],
        "bottomChrome": diag["bottom_chrome"],
        "stitched": "stitched.png",
        "stitchedSize": list(diag["out_size"]),
        "seams": diag.get("seams", []),
        "frames": frames_meta,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        for i, p in enumerate(pngs):
            z.writestr(f"frames/frame_{i:03d}.png", p)
        z.writestr("stitched.png", stitched_png)


def png_bytes(img) -> bytes:
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# capture loop
# --------------------------------------------------------------------------- #
def wait_stable(serial: str, settle: float, timeout: float = 6.0) -> bytes:
    deadline = time.monotonic() + timeout
    prev_png = screencap(serial)
    prev = gray(prev_png)
    while time.monotonic() < deadline:
        time.sleep(settle)
        cur_png = screencap(serial)
        cur = gray(cur_png)
        if frames_equal(cur, prev):
            return cur_png
        prev_png, prev = cur_png, cur
    return prev_png


def capture_frames(serial: str, *, settle: float, max_frames: int,
                   frames_dir: Path) -> list[bytes]:
    w, h = display_size(serial)
    # A crisp, fast FLICK (300ms). Counter-intuitively a *slow* swipe is worse:
    # Android reads a slow press-then-drag that starts on a clickable row as a
    # tap/long-press and opens the row instead of scrolling. A fast flick is
    # unambiguously a scroll. Travel ~30% of height; the cross-correlation search
    # window (~55% of H) easily covers the resulting fling distance.
    x = w // 2
    y1, y2 = int(h * 0.70), int(h * 0.40)
    try:
        adb(["shell", "input", "keyevent", "224"], serial=serial)  # WAKEUP
    except RuntimeError:
        pass

    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("frame_*.png"):  # don't let a prior longer run leave stale frames
        old.unlink()
    pngs: list[bytes] = []
    png = wait_stable(serial, settle)
    pngs.append(png)
    (frames_dir / "frame_000.png").write_bytes(png)
    print(f"  frame 0 captured")

    prev = gray(png)
    for i in range(1, max_frames):
        adb(["shell", "input", "swipe", *map(str, (x, y1, x, y2, 300))], serial=serial)
        png = wait_stable(serial, settle)
        cur = gray(png)
        if frames_equal(cur, prev):
            print(f"  reached bottom after {i} swipe(s)")
            break
        pngs.append(png)
        (frames_dir / f"frame_{i:03d}.png").write_bytes(png)
        d, _ = scroll_delta(prev, cur)
        print(f"  frame {i} captured  (moved ~{d}px)")
        prev = cur
    else:
        print(f"  hit max-frames={max_frames}")
    return pngs


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_capture(args) -> None:
    serial = pick_serial(args.serial)
    out = Path(args.out) if args.out else ROOT / "captures" / "longshot.png"
    # frames live in a sibling dir named after the output file (no timestamp)
    frames_dir = Path(args.frames_dir) if args.frames_dir else out.parent / f"{out.stem}-frames"
    insets = device_insets(serial)
    print(f"Capturing scroll frames from {serial} -> {frames_dir}")
    pngs = capture_frames(serial, settle=args.settle, max_frames=args.max_frames,
                          frames_dir=frames_dir)
    # stash insets so an offline re-stitch from this frames dir gets the nav floor too
    (frames_dir / "insets.json").write_text(json.dumps(insets))
    print("Stitching...")
    img, diag = stitch(pngs, top_override=args.top, bot_override=args.bottom,
                       bot_floor=insets["bottom"])
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"Wrote {out}  ({out.stat().st_size // 1024} KiB)")
    bundle = out.with_suffix(".longshot")
    write_bundle(bundle, pngs, png_bytes(img), diag)
    print(f"Wrote {bundle}  (open in the viewer's /longshot route)")
    print(f"Frames kept in {frames_dir} — re-stitch with:  "
          f"./longshot.py stitch {frames_dir}")


def cmd_stitch(args) -> None:
    d = Path(args.framedir)
    files = sorted(d.glob("frame_*.png")) or sorted(d.glob("*.png"))
    if not files:
        sys.exit(f"No frame PNGs in {d}")
    print(f"Stitching {len(files)} frame(s) from {d}")
    pngs = [f.read_bytes() for f in files]
    # reuse the nav-bar inset captured alongside the frames, if present
    insets_path = d / "insets.json"
    bot_floor = json.loads(insets_path.read_text()).get("bottom", 0) if insets_path.exists() else 0
    img, diag = stitch(pngs, top_override=args.top, bot_override=args.bottom, bot_floor=bot_floor)
    out = Path(args.out) if args.out else d / "long.png"
    img.save(out)
    print(f"Wrote {out}  ({out.stat().st_size // 1024} KiB)")
    bundle = out.with_suffix(".longshot")
    write_bundle(bundle, pngs, png_bytes(img), diag)
    print(f"Wrote {bundle}  (open in the viewer's /longshot route)")


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(prog="longshot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("capture", help="scroll a screen, save frames, stitch")
    pc.add_argument("--serial")
    pc.add_argument("--out", help="output PNG (default captures/longshot.png)")
    pc.add_argument("--frames-dir", help="where to save raw frames")
    pc.add_argument("--settle", type=float, default=0.4)
    pc.add_argument("--max-frames", type=int, default=40)
    pc.add_argument("--top", type=int, help="force top chrome height (px)")
    pc.add_argument("--bottom", type=int, help="force bottom chrome height (px)")
    pc.set_defaults(func=cmd_capture)

    ps = sub.add_parser("stitch", help="re-stitch saved frames (no device)")
    ps.add_argument("framedir", help="directory of frame_*.png")
    ps.add_argument("--out", help="output PNG (default <framedir>/long.png)")
    ps.add_argument("--top", type=int, help="force top chrome height (px)")
    ps.add_argument("--bottom", type=int, help="force bottom chrome height (px)")
    ps.set_defaults(func=cmd_stitch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
