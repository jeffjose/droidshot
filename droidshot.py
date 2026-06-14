#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy"]
# ///
"""droidshot — capture the UI state of a connected Android device into a
portable .droidshot file (a zip: manifest.json + content-addressed assets).

M0 scope: vendor adb locally, verify a device, capture a single screen
(screenshot + UI hierarchy + device metadata) into a valid 1-node .droidshot.

Commands:
  setup     download platform-tools (adb) into ./vendor  (no global install)
  doctor    check the vendored adb and list connected devices
  capture   capture the current screen into a .droidshot file
"""

from __future__ import annotations

import argparse
import hashlib
from collections import deque
import io
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor" / "platform-tools"
TOOL_VERSION = "0.1.0"
FORMAT_VERSION = 2  # v2: a node's screenshot may be a stitched full-page image;
                    # nodes carry `viewport` [w,h] and `longshot` metadata (null
                    # for single-viewport screens)
TIMING = False  # crawl --timing: print a per-snapshot latency breakdown

PLATFORM_TOOLS_URL = {
    "Linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "Darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "Windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
}


# --------------------------------------------------------------------------- #
# adb plumbing
# --------------------------------------------------------------------------- #
def adb_path() -> Path | None:
    """Resolve adb, preferring the vendored copy. Never falls back to a global
    adb silently unless the user opts in via DROIDSHOT_ADB."""
    override = os.environ.get("DROIDSHOT_ADB")
    if override:
        return Path(override)
    exe = "adb.exe" if platform.system() == "Windows" else "adb"
    vendored = VENDOR / exe
    if vendored.exists():
        return vendored
    return None


def require_adb() -> Path:
    p = adb_path()
    if p is None or not p.exists():
        sys.exit("adb not found. Run:  ./droidshot.py setup")
    return p


def adb(args: list[str], *, serial: str | None = None, binary: bool = False,
        timeout: float | None = None):
    """Run adb. Returns str (text) or bytes (binary=True). Raises on failure or
    if it runs longer than `timeout` seconds (some adb shell calls can hang)."""
    cmd = [str(require_adb())]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb {' '.join(args)} timed out after {timeout}s")
    if res.returncode != 0:
        err = res.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"adb {' '.join(args)} failed: {err}")
    return res.stdout if binary else res.stdout.decode(errors="replace")


def list_devices() -> list[str]:
    out = adb(["devices"])
    serials = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line and "\tdevice" in line:
            serials.append(line.split("\t")[0])
    return serials


def pick_serial(requested: str | None) -> str:
    devices = list_devices()
    if not devices:
        sys.exit("No device connected (and authorized). Plug in a device and enable USB debugging.")
    if requested:
        if requested not in devices:
            sys.exit(f"Device {requested} not found. Connected: {', '.join(devices)}")
        return requested
    if len(devices) > 1:
        sys.exit(f"Multiple devices connected; pass --serial. Connected: {', '.join(devices)}")
    return devices[0]


# --------------------------------------------------------------------------- #
# capture helpers
# --------------------------------------------------------------------------- #
def getprop(serial: str, key: str) -> str:
    try:
        return adb(["shell", "getprop", key], serial=serial).strip()
    except RuntimeError:
        return ""


def device_metadata(serial: str) -> dict:
    size = adb(["shell", "wm", "size"], serial=serial)
    dens = adb(["shell", "wm", "density"], serial=serial)
    m_size = re.search(r"(\d+)x(\d+)", size)
    m_dens = re.search(r"(\d+)", dens)
    return {
        "serial": serial,
        "oem": getprop(serial, "ro.product.manufacturer"),
        "model": getprop(serial, "ro.product.model"),
        "android": getprop(serial, "ro.build.version.release"),
        "sdk": getprop(serial, "ro.build.version.sdk"),
        "build": getprop(serial, "ro.build.fingerprint"),
        "display": {
            "w": int(m_size.group(1)) if m_size else None,
            "h": int(m_size.group(2)) if m_size else None,
            "density": int(m_dens.group(1)) if m_dens else None,
            "refreshHz": None,  # M1+: parse from dumpsys display
        },
    }


def current_activity(serial: str) -> str | None:
    try:
        out = adb(["shell", "dumpsys", "activity", "activities"], serial=serial)
    except RuntimeError:
        return None
    m = re.search(r"(?:mResumedActivity|ResumedActivity).*?\{[^}]*?\s(\S+/\S+)", out)
    return m.group(1) if m else None


def grab_screenshot(serial: str) -> bytes:
    return adb(["exec-out", "screencap", "-p"], serial=serial, binary=True)


def grab_hierarchy(serial: str, retries: int = 2, dump_timeout: float = 8.0) -> str | None:
    """Dump the UI hierarchy.

    uiautomator is finicky: on screens it deems never-idle (entrance animations,
    blinking cursors) `dump` exits 0 but writes nothing — leaving the PREVIOUS
    dump on disk. If we blindly cat it we serve a stale tree from another screen.
    So: remove the file first (a failed dump then yields no file, not stale data),
    confirm the success message, and retry a few times to ride out transient
    not-idle states."""
    path = "/sdcard/window_dump.xml"
    for _ in range(retries):
        try:
            adb(["shell", "rm", "-f", path], serial=serial)
        except RuntimeError:
            pass
        try:
            # bounded: uiautomator blocks waiting for UI idle; never let it hang
            adb(["shell", "uiautomator", "dump", path], serial=serial, timeout=dump_timeout)
        except RuntimeError:
            pass
        # Trust the file, not the (sometimes-stderr) success message: accept only
        # if cat yields real XML. rm-first guarantees this can't be a stale dump.
        try:
            xml = adb(["exec-out", "cat", path], serial=serial)
        except RuntimeError:
            xml = ""
        if xml.lstrip().startswith("<"):
            return xml
        time.sleep(0.4)
    return None


FRAME_EPS = 0.01  # frames within 1% of pixels are treated as the same


def _frame_diff_ratio(a: bytes, b: bytes) -> float:
    """Fraction of differing pixels between two screenshots (downscaled,
    grayscale). Tolerant of tiny live regions — a clock or an Uptime counter —
    that would defeat an exact byte comparison."""
    from PIL import Image, ImageChops

    def prep(x: bytes):
        return Image.open(BytesIO(x)).convert("L").resize((64, 128))

    diff = ImageChops.difference(prep(a), prep(b)).point(lambda p: 255 if p > 16 else 0)
    return diff.histogram()[255] / (64 * 128)


def frames_equivalent(a: bytes, b: bytes) -> bool:
    if a == b:
        return True
    try:
        return _frame_diff_ratio(a, b) < FRAME_EPS
    except Exception:
        return a == b


_VIEW_RE = re.compile(
    r"^(\s*)([\w.$]+)\{[0-9a-fA-F]+ (\S+) \S+ (-?\d+),(-?\d+)-(-?\d+),(-?\d+)"
    r"(?: #[0-9a-fA-F]+ ([^\s}]+))?"
)


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def grab_view_tree(serial: str) -> str | None:
    """Fallback hierarchy via `dumpsys activity top` — the View tree, which has
    NO accessibility idle-gate, so it works on live screens uiautomator refuses
    (e.g. About phone's Uptime clock). Gives class + resource-id + bounds +
    clickable, but NOT visible text. Bounds in the dump are parent-relative, so
    we accumulate offsets down the tree to absolute screen coordinates.

    Emitted as uiautomator-shaped XML so the viewer parses it identically."""
    try:
        out = adb(["shell", "dumpsys", "activity", "top"], serial=serial, timeout=10.0)
    except RuntimeError:
        return None
    lines = out.splitlines()
    decors = [i for i, l in enumerate(lines) if "DecorView{" in l]
    if not decors:
        return None
    start = decors[-1]  # the focused/topmost activity is dumped last
    base = len(lines[start]) - len(lines[start].lstrip())
    stack: list[tuple[int, int, int]] = []  # (indent, abs_left, abs_top)
    rows: list[tuple[int, int, int, int, str, str, bool]] = []
    for line in lines[start:]:
        m = _VIEW_RE.match(line)
        if not m:
            indent = len(line) - len(line.lstrip())
            if line.strip() and indent <= base and rows:
                break  # left the view-hierarchy section
            continue
        indent = len(m.group(1))
        if indent < base:
            break
        cls, flags = m.group(2), m.group(3)
        rl, rt, rr, rb = (int(m.group(i)) for i in (4, 5, 6, 7))
        rid = (m.group(8) or "").rstrip("}")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        pax, pay = (stack[-1][1], stack[-1][2]) if stack else (0, 0)
        ax, ay = pax + rl, pay + rt
        stack.append((indent, ax, ay))
        if flags[:1] != "V" or rr - rl <= 0 or rb - rt <= 0:  # skip gone/invisible/zero-area
            continue
        rows.append((ax, ay, ax + (rr - rl), ay + (rb - rt), cls, rid, "C" in flags))
    if not rows:
        return None
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<hierarchy rotation="0">']
    for x1, y1, x2, y2, cls, rid, clk in rows:
        parts.append(
            f'<node class="{_xml_escape(cls)}" resource-id="{_xml_escape(rid)}" '
            f'text="" content-desc="" clickable="{str(clk).lower()}" '
            f'bounds="[{x1},{y1}][{x2},{y2}]" />'
        )
    parts.append("</hierarchy>")
    return "".join(parts)


VIEWTREE_ACTIVITIES: set[str] = set()  # activities seen to stall uiautomator


def grab_tree(serial: str, activity: str | None) -> tuple[str | None, str | None]:
    """Hierarchy for the current screen, preferring uiautomator (it has text).

    Some screens are never-idle — a live clock (About phone's Uptime) makes
    uiautomator block until timeout and return nothing. We remember such an
    activity and thereafter try uiautomator with a SHORT budget instead of the
    full one: a genuinely never-idle screen fails fast and we use the View tree,
    but a normally-idle screen still dumps (with text!). This matters because
    many distinct screens share one activity (com.android.settings/.SubSettings),
    so we must NOT blanket-skip uiautomator for the activity — that would strip
    text from every sub-screen and break path-replay (which navigates by text).
    Returns (xml, source)."""
    if activity and activity in VIEWTREE_ACTIVITIES:
        xml = grab_hierarchy(serial, retries=2, dump_timeout=3.0)  # quick attempt
        if xml:
            return xml, "uiautomator"
        return grab_view_tree(serial), "viewtree"
    xml = grab_hierarchy(serial)  # full retries/timeout the first time
    if xml:
        return xml, "uiautomator"
    if activity:
        VIEWTREE_ACTIVITIES.add(activity)
    return grab_view_tree(serial), "viewtree"


def _scroll_offset(a: bytes, b: bytes, disp_h: int) -> int:
    """Pixels the content moved up from frame a to b (0 = no scroll / at bottom).
    Cross-correlates a content band, ignoring top chrome (status + app bar) and
    the bottom gesture area, so fling distance is measured from the pixels."""
    import numpy as np
    from PIL import Image

    H, W = 600, 32
    def arr(x: bytes):
        return np.asarray(Image.open(BytesIO(x)).convert("L").resize((W, H)), dtype=np.float32)

    A, B = arr(a), arr(b)
    top, bot = int(H * 0.16), int(H * 0.94)
    max_shift = int(H * 0.85)
    win = (bot - top) - max_shift
    if win < 20:
        win = (bot - top) // 2
        max_shift = (bot - top) - win
    bband = B[top:top + win]
    best_d, best = 0, None
    for d in range(max_shift):
        score = float(np.abs(A[top + d:top + d + win] - bband).mean())
        if best is None or score < best:
            best, best_d = score, d
    return int(round(best_d / H * disp_h))


def compute_scroll(nodes: list[dict], edges: list[dict], get_png, disp: dict) -> None:
    """Annotate each node in a swipe (scroll) chain with its vertical position:
    scroll = {y, content, viewport, chain}. Lets the viewer draw a scrollbar."""
    disp_h = disp.get("h") or 2400
    nmap = {n["id"]: n for n in nodes}
    nxt, prv = {}, {}
    for e in edges:
        if e["action"]["type"] == "swipe":
            nxt[e["from"]] = e["to"]
            prv[e["to"]] = e["from"]
    starts = [nid for nid in nmap if nid in nxt and nid not in prv]
    for s in starts:
        chain = [s]
        while chain[-1] in nxt:
            chain.append(nxt[chain[-1]])
        ys = [0]
        for i in range(1, len(chain)):
            d = _scroll_offset(get_png(nmap[chain[i - 1]]), get_png(nmap[chain[i]]), disp_h)
            ys.append(ys[-1] + d)
        content = ys[-1] + disp_h
        for nid, y in zip(chain, ys):
            nmap[nid]["scroll"] = {"y": y, "content": content, "viewport": disp_h, "chain": s}


# --------------------------------------------------------------------------- #
# long screenshot stitching (ported from longshot.py)
#
# A screencap only sees the viewport. To get a full-page image we scroll and
# glue the newly-revealed slivers together, keeping the fixed top/bottom chrome
# (status bar, app bar, nav bar) ONCE. The crawl already scrolls each screen to
# discover rows, so we reuse those frames — no extra device time.
# --------------------------------------------------------------------------- #
GRAY_W = 100  # downscale width for analysis; height kept FULL so the measured
              # vertical offset is in real pixels (no vertical rescaling)


def _gray(png: bytes):
    """Full-height, narrow grayscale array (H, GRAY_W) as float32."""
    import numpy as np
    from PIL import Image
    img = Image.open(BytesIO(png)).convert("L")
    _, h = img.size
    img = img.resize((GRAY_W, h))  # width->GRAY_W, height unchanged
    return np.asarray(img, dtype=np.float32)


def _stitch_delta(A, B) -> int:
    """Pixels the content moved UP from frame A to frame B (0 = no scroll).
    Feature at row r in A appears at row r-d in B; minimise mean-abs-difference
    over candidate shifts d, ignoring top/bottom chrome."""
    import numpy as np
    H = A.shape[0]
    top, bot = int(H * 0.12), int(H * 0.95)
    band = bot - top
    win = band // 3
    max_shift = band - win
    ref = B[top:top + win]
    best_d, best = 0, None
    for d in range(max_shift):
        score = float(np.abs(A[top + d:top + d + win] - ref).mean())
        if best is None or score < best:
            best, best_d = score, d
    return best_d


def _chrome_bands(A, B, thresh: float = 4.0) -> tuple[int, int]:
    """Given two frames that DID scroll, find the fixed top/bottom chrome heights:
    the contiguous run of rows from each edge that stay identical in place."""
    import numpy as np
    rowdiff = np.abs(A - B).mean(axis=1)
    H = len(rowdiff)
    t = 0
    while t < H and rowdiff[t] < thresh:
        t += 1
    b = 0
    while b < H and rowdiff[H - 1 - b] < thresh:
        b += 1
    return t, b


def content_top(xml: str | None, view_w: int, view_h: int) -> int | None:
    """Top y (device px) of the main scrollable list — i.e. the bottom of a
    collapsing ("fat") header. We can't find this from pixels (a big first swipe
    leaves no overlap to match), but the hierarchy has it: the content list sits
    BELOW the collapsing toolbar, so its top bound is the header bottom. Pick the
    full-width, tall scrollable that starts LOWEST (the inner list, not the outer
    toolbar container). None if there's no such list (e.g. view-tree fallback)."""
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    best = None
    for el in root.iter("node"):
        if el.get("scrollable") != "true":
            continue
        m = BOUNDS_RE.search(el.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if (x2 - x1) < 0.8 * view_w or (y2 - y1) < 0.3 * view_h:
            continue  # ignore narrow/short scrollers (chip rows etc.)
        if best is None or y1 > best:
            best = y1
    return best


def _crop_top_png(png: bytes, h: int) -> bytes:
    """Crop the top `h` px of a PNG (the collapsed status + skinny app bar)."""
    from PIL import Image
    img = Image.open(BytesIO(png)).convert("RGB")
    buf = BytesIO()
    img.crop((0, 0, img.width, h)).save(buf, "PNG")
    return buf.getvalue()


def device_insets(serial: str) -> dict:
    """System-bar heights (px) from the window manager. The gesture-nav bar sits
    over edge-to-edge content so the pixel chrome detector misses it, but the WM
    knows its exact height. Returns {'top': statusBar, 'bottom': navBar}."""
    try:
        out = adb(["shell", "dumpsys", "window"], serial=serial, timeout=10.0)
    except RuntimeError:
        return {"top": 0, "bottom": 0}

    def bar(kind: str) -> int:
        m = re.search(rf"type={kind} frame=\[\d+,(\d+)\]\[\d+,(\d+)\]", out)
        return int(m.group(2)) - int(m.group(1)) if m else 0

    return {"top": bar("statusBars"), "bottom": bar("navigationBars")}


def stitch_frames(pngs: list[bytes], *, bot_floor: int = 0, min_move: int = 6):
    """Stitch viewport PNGs (top-of-screen first) into one tall image.
    Returns (stitched_png_bytes, meta) where meta has viewport, chrome, size,
    and seams. Returns (pngs[0], None) when there's nothing to stitch."""
    from PIL import Image

    if len(pngs) < 2:
        return (pngs[0] if pngs else b""), None

    grays = [_gray(p) for p in pngs]
    H = grays[0].shape[0]
    deltas, ts, bs = [], [], []
    for i in range(len(grays) - 1):
        d = _stitch_delta(grays[i], grays[i + 1])
        deltas.append(d)
        if d >= min_move:  # only frames that actually scrolled reveal chrome
            t, b = _chrome_bands(grays[i], grays[i + 1])
            ts.append(t)
            bs.append(b)

    if not any(d >= min_move for d in deltas):
        return pngs[0], None  # never actually scrolled — a single viewport

    t = int(statistics.median(ts)) if ts else 0
    # the gesture-nav bar isn't full-width-static, so floor the bottom chrome at
    # the WM-reported nav inset so its pill is cropped from every sliver
    b = max(int(statistics.median(bs)) if bs else 0, bot_floor)

    frames = [Image.open(BytesIO(p)).convert("RGB") for p in pngs]
    W = frames[0].width
    band0_h = H - b - t

    revealed = [d for d in deltas if d >= min_move]
    canvas_h = t + band0_h + sum(revealed) + b
    canvas = Image.new("RGB", (W, canvas_h))

    seams = []
    y = 0
    canvas.paste(frames[0].crop((0, 0, W, t)), (0, y)); y += t          # top chrome
    seams.append({"frame": 0, "y": y, "h": band0_h})
    canvas.paste(frames[0].crop((0, t, W, H - b)), (0, y)); y += band0_h  # frame0 band
    for i, d in enumerate(deltas, start=1):
        if d < min_move:
            continue
        seams.append({"frame": i, "y": y, "h": d})
        canvas.paste(frames[i].crop((0, H - b - d, W, H - b)), (0, y)); y += d
    canvas.paste(frames[-1].crop((0, H - b, W, H)), (0, y))            # bottom chrome

    buf = BytesIO()
    canvas.save(buf, "PNG")
    meta = {
        "viewport": [W, H],
        "topChrome": t,
        "bottomChrome": b,
        "stitchedSize": [W, canvas_h],
        "seams": seams,
    }
    return buf.getvalue(), meta


def wait_stable(serial: str, settle: float, timeout: float = 6.0) -> bytes:
    """Poll screenshots until two consecutive frames are ~identical (no
    animation/scroll in flight), then return that frame. Uses a tolerant compare
    so a ticking counter doesn't make the screen look perpetually busy."""
    deadline = time.monotonic() + timeout
    prev = grab_screenshot(serial)
    while time.monotonic() < deadline:
        time.sleep(settle)
        cur = grab_screenshot(serial)
        if frames_equivalent(cur, prev):
            return cur
        prev = cur
    return prev  # gave up waiting; return the latest


def snapshot(serial: str, settle: float) -> tuple[bytes, str | None, str | None, str | None]:
    """One coherent capture of the current state: nudge awake, stabilize, then
    grab the screenshot and hierarchy. Prefer uiautomator (rich, has text); fall
    back to the dumpsys View tree on screens uiautomator can't dump.
    Returns (png, hierarchy_xml, activity, hierarchy_source)."""
    try:
        adb(["shell", "input", "keyevent", "224"], serial=serial)  # KEYCODE_WAKEUP
    except RuntimeError:
        pass
    t = time.monotonic()
    png = wait_stable(serial, settle)
    t_png = time.monotonic() - t
    t = time.monotonic()
    activity = current_activity(serial)  # cheap; fetched first so grab_tree can
    t_act = time.monotonic() - t          # skip uiautomator on never-idle screens
    t = time.monotonic()
    xml, source = grab_tree(serial, activity)
    t_hier = time.monotonic() - t
    if TIMING:
        print(f"      [snap] stable={t_png:.1f}s activity={t_act:.1f}s "
              f"hier={t_hier:.1f}s ({source or 'none'})")
    return png, xml, activity, source


BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def find_element(xml: str | None, by: str, value: str) -> dict | None:
    """Find the first hierarchy node matching by resource-id ('id') or visible
    text substring ('text'). Returns its center + bounds + resource-id."""
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for el in root.iter("node"):
        rid = el.get("resource-id", "")
        txt = el.get("text", "")
        hit = (by == "id" and rid == value) or (by == "text" and value.lower() in txt.lower())
        if not hit:
            continue
        m = BOUNDS_RE.search(el.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        # record the smallest *clickable* element under the tap (the whole row),
        # not just the matched text — that's the real touch target
        bounds = _clickable_container(root, cx, cy) or [x1, y1, x2, y2]
        return {"bounds": bounds, "cx": cx, "cy": cy, "rid": rid or None}
    return None


def _clickable_container(root, cx: int, cy: int) -> list[int] | None:
    best, best_area = None, None
    for el in root.iter("node"):
        if el.get("clickable") != "true":
            continue
        m = BOUNDS_RE.search(el.get("bounds", ""))
        if not m:
            continue
        a, b, c, d = map(int, m.groups())
        if a <= cx <= c and b <= cy <= d:
            area = (c - a) * (d - b)
            if area > 0 and (best_area is None or area < best_area):
                best, best_area = [a, b, c, d], area
    return best


def perform_action(serial: str, kind: str, val: str | None, prev_xml: str | None, disp: dict) -> dict:
    """Execute one input action and return its edge `action` record."""
    w, h = disp.get("w") or 1080, disp.get("h") or 2400
    if kind == "tap":
        x, y = (int(p) for p in val.split(","))
        adb(["shell", "input", "tap", str(x), str(y)], serial=serial)
        return {"type": "tap", "x": x, "y": y, "elementId": None, "bounds": None}
    if kind in ("tap-id", "tap-text"):
        el = find_element(prev_xml, "id" if kind == "tap-id" else "text", val or "") if prev_xml else None
        if not el:
            print(f"  (skip {kind}={val}: not found on this screen)")
            return None  # non-fatal: skip this step, stay on the current screen
        adb(["shell", "input", "tap", str(el["cx"]), str(el["cy"])], serial=serial)
        return {"type": "tap", "x": el["cx"], "y": el["cy"], "elementId": el["rid"], "bounds": el["bounds"]}
    if kind == "swipe":
        # directional scroll: 'up' reveals content below (finger moves up), etc.
        seg = {
            "up": (w // 2, int(h * 0.70), w // 2, int(h * 0.30)),
            "down": (w // 2, int(h * 0.30), w // 2, int(h * 0.70)),
            "left": (int(w * 0.70), h // 2, int(w * 0.30), h // 2),
            "right": (int(w * 0.30), h // 2, int(w * 0.70), h // 2),
        }.get(val or "up")
        if not seg:
            sys.exit(f"swipe: direction must be up/down/left/right, got '{val}'")
        x1, y1, x2, y2 = seg
        # slow drag (500ms) to limit fling momentum; wait_stable still settles it
        adb(["shell", "input", "swipe", *map(str, (x1, y1, x2, y2, 500))], serial=serial)
        return {"type": "swipe", "x": x1, "y": y1, "elementId": None, "bounds": None, "dir": val}
    if kind == "back":
        adb(["shell", "input", "keyevent", "4"], serial=serial)
        return {"type": "back", "x": None, "y": None, "elementId": None, "bounds": None}
    if kind == "key":
        adb(["shell", "input", "keyevent", val or "0"], serial=serial)
        return {"type": "key", "key": val, "x": None, "y": None, "elementId": None, "bounds": None}
    sys.exit(f"unknown action: {kind}")


def parse_steps(do_list: list[str] | None) -> list[tuple[str, str | None]]:
    steps: list[tuple[str, str | None]] = []
    for raw in do_list or []:
        if raw == "back":
            steps.append(("back", None))
        elif "=" in raw:
            k, v = raw.split("=", 1)
            steps.append((k, v))
        else:
            sys.exit(f"bad --do '{raw}' (expected e.g. tap-id=..., swipe=up, tap=540,1200, back)")
    return steps


def reset_scroll(serial: str, disp: dict, settle: float, limit: int = 12) -> None:
    """Swipe down until the screen stops changing — i.e. scrolled to the top."""
    prev = grab_screenshot(serial)
    for _ in range(limit):
        perform_action(serial, "swipe", "down", None, disp)
        cur = wait_stable(serial, settle)
        if cur == prev:
            return
        prev = cur


def keep_awake(serial: str) -> str | None:
    """Wake the screen and stop it dozing during capture. `stayon` alone is
    unreliable on some devices, so we also extend screen_off_timeout. Returns
    the prior timeout so the caller can restore it."""
    prior = None
    try:
        prior = adb(["shell", "settings", "get", "system", "screen_off_timeout"], serial=serial).strip()
    except RuntimeError:
        pass
    for cmd in (
        ["shell", "input", "keyevent", "224"],            # KEYCODE_WAKEUP
        ["shell", "svc", "power", "stayon", "true"],
        ["shell", "settings", "put", "system", "screen_off_timeout", "1800000"],
    ):
        try:
            adb(cmd, serial=serial)
        except RuntimeError:
            pass
    return prior


def restore_awake(serial: str, prior: str | None) -> None:
    try:
        adb(["shell", "svc", "power", "stayon", "false"], serial=serial)
    except RuntimeError:
        pass
    if prior and prior.isdigit():
        try:
            adb(["shell", "settings", "put", "system", "screen_off_timeout", prior], serial=serial)
        except RuntimeError:
            pass


# --------------------------------------------------------------------------- #
# .droidshot writer
# --------------------------------------------------------------------------- #
def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_asset(assets: dict[str, bytes], data: bytes, ext: str) -> str:
    name = f"{sha256(data)}.{ext}"
    assets[name] = data
    return f"assets/{name}"


def build_node(node_id: str, png: bytes, xml: str | None, activity: str | None,
               source: str | None, assets: dict[str, bytes]) -> dict:
    return {
        "id": node_id,
        "screenshot": register_asset(assets, png, "png"),
        "hierarchy": register_asset(assets, xml.encode(), "xml") if xml else None,
        "hierarchySource": source,  # "uiautomator" | "viewtree" (no text) | null
        "activity": activity,
        "capturedAt": now_iso(),
    }


def write_droidshot(out_path: Path, manifest: dict, assets: dict[str, bytes]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, data in assets.items():
            z.writestr(f"assets/{name}", data)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_setup(args) -> None:
    system = platform.system()
    url = PLATFORM_TOOLS_URL.get(system)
    if not url:
        sys.exit(f"Unsupported OS: {system}")
    exe = "adb.exe" if system == "Windows" else "adb"
    if (VENDOR / exe).exists() and not args.force:
        print(f"adb already vendored at {VENDOR / exe}  (use --force to re-download)")
        return
    print(f"Downloading platform-tools for {system}...")
    with urllib.request.urlopen(url) as resp:
        blob = resp.read()
    dest = ROOT / "vendor"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(dest)  # extracts a top-level platform-tools/ dir
    adb_bin = VENDOR / exe
    if system != "Windows":
        adb_bin.chmod(0o755)
    print(f"adb installed at {adb_bin}")
    print(subprocess.run([str(adb_bin), "version"], capture_output=True, text=True).stdout.strip())


def cmd_doctor(args) -> None:
    p = adb_path()
    if p is None or not p.exists():
        sys.exit("adb not vendored. Run:  ./droidshot.py setup")
    print(f"adb: {p}")
    print(subprocess.run([str(p), "version"], capture_output=True, text=True).stdout.splitlines()[0])
    devices = list_devices()
    if not devices:
        print("devices: none connected/authorized")
    else:
        for s in devices:
            model = getprop(s, "ro.product.model")
            android = getprop(s, "ro.build.version.release")
            print(f"device: {s}  {model}  Android {android}")


def cmd_capture(args) -> None:
    serial = pick_serial(args.serial)
    steps = parse_steps(args.do)
    meta = device_metadata(serial)
    print(f"Capturing from {serial} ({meta['oem']} {meta['model']}), {len(steps)} action(s)...")

    awake_state: str | None = None
    if not args.no_wake:
        awake_state = keep_awake(serial)
    assets: dict[str, bytes] = {}
    nodes: list[dict] = []
    edges: list[dict] = []
    disp = meta["display"]
    idx = 0          # highest node index allocated
    cur_id = "n0"    # the node we're currently "on"
    registry: list[tuple[str, str | None, bytes]] = []  # (id, activity, png) for dedup

    def find_dup(png: bytes, activity: str | None) -> str | None:
        for nid, act, p in registry:
            if act == activity and frames_equivalent(png, p):
                return nid
        return None

    def add(action: dict, png: bytes, xml: str | None, activity: str | None,
            source: str | None, label: str):
        # if this screen matches one already captured (e.g. back to Home), reuse
        # that node so the flow BRANCHES instead of duplicating the screen
        nonlocal idx, cur_id
        dup = find_dup(png, activity)
        if dup and dup != cur_id:
            edges.append({"from": cur_id, "to": dup, "action": action, "transition": None})
            print(f"  ↩  {label}  ->  {activity}  (revisit {dup})")
            cur_id = dup
            return
        idx += 1
        nid = f"n{idx}"
        nodes.append(build_node(nid, png, xml, activity, source, assets))
        registry.append((nid, activity, png))
        edges.append({"from": cur_id, "to": nid, "action": action, "transition": None})
        print(f"  {nid}  {label}  ->  {activity}  [{source or 'no-hierarchy'}]")
        cur_id = nid

    try:
        if args.reset:
            reset_scroll(serial, disp, args.settle)
        png, xml, activity, source = snapshot(serial, args.settle)
        nodes.append(build_node("n0", png, xml, activity, source, assets))
        registry.append(("n0", activity, png))
        print(f"  n0  {activity}  [{source or 'no-hierarchy'}]")

        for kind, val in steps:
            if kind == "scroll-to":
                target = val or ""
                if find_element(xml, "text", target):
                    print(f"      ('{target}' already visible)")
                tries = 0
                while not find_element(xml, "text", target) and tries < args.max_scroll:
                    prev_png = png
                    action = perform_action(serial, "swipe", "up", xml, disp)
                    png, xml, activity, source = snapshot(serial, args.settle)
                    if frames_equivalent(prev_png, png):  # didn't move -> bottom
                        print(f"      (reached end; '{target}' not found)")
                        break
                    add(action, png, xml, activity, source, f"scroll-to={target} (swipe up)")
                    tries += 1
                continue
            if kind == "scroll":
                # scroll all the way: keep going until two consecutive frames are
                # identical (the real bottom), capped at --max-scroll
                swdir = "down" if val == "up" else "up"  # scroll=down -> swipe up
                tries = 0
                while tries < args.max_scroll:
                    prev_png = png
                    action = perform_action(serial, "swipe", swdir, xml, disp)
                    png, xml, activity, source = snapshot(serial, args.settle)
                    if frames_equivalent(prev_png, png):
                        break  # nothing new -> end of the list
                    add(action, png, xml, activity, source, f"scroll {val or 'down'}")
                    tries += 1
                continue
            action = perform_action(serial, kind, val, xml, disp)
            if action is None:  # tap target not found — skip, stay on this screen
                continue
            png, xml, activity, source = snapshot(serial, args.settle)
            add(action, png, xml, activity, source, f"{kind}{('=' + val) if val else ''}")
    finally:
        if not args.no_wake:
            restore_awake(serial, awake_state)

    try:
        compute_scroll(nodes, edges, lambda n: assets[n["screenshot"].split("assets/", 1)[1]], disp)
    except Exception as e:
        print(f"  (scroll annotation skipped: {e})")

    manifest = {
        "formatVersion": FORMAT_VERSION,
        "tool": f"droidshot/{TOOL_VERSION}",
        "capturedAt": now_iso(),
        "device": meta,
        "app": None,  # M2+: derive from focused package
        "nodes": nodes,
        "edges": edges,
    }

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_model = re.sub(r"[^A-Za-z0-9]+", "-", meta["model"] or "device").strip("-")
        out_path = ROOT / "captures" / f"{stamp}-{safe_model}.droidshot"

    write_droidshot(out_path, manifest, assets)
    print(f"Wrote {out_path}  ({out_path.stat().st_size // 1024} KiB, "
          f"{len(nodes)} node(s), {len(edges)} edge(s))")


def cmd_annotate(args) -> None:
    path = Path(args.file)
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        pngs = {n["id"]: z.read(n["screenshot"]) for n in manifest["nodes"]}
        members = {name: z.read(name) for name in z.namelist() if name != "manifest.json"}
    compute_scroll(manifest["nodes"], manifest["edges"], lambda n: pngs[n["id"]],
                   manifest["device"]["display"])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, data in members.items():
            z.writestr(name, data)
    annotated = sum(1 for n in manifest["nodes"] if "scroll" in n)
    print(f"Annotated {path}: scroll metadata on {annotated}/{len(manifest['nodes'])} nodes")


# --------------------------------------------------------------------------- #
# crawl — autonomous breadth-first exploration from a starting screen
# --------------------------------------------------------------------------- #
# Words that name an irreversible / state-wiping area. `safe` (default) refuses
# to even tap these; `open` opens them (so you can map the screen) but still
# refuses COMMIT-level buttons; `all` taps everything.
DESTRUCTIVE_RE = re.compile(
    r"\b(reset|erase|factory|wipe|format|forget|"
    r"delete\s+account|remove\s+account|sign\s*out)\b", re.I)
# The actual point-of-no-return buttons, usually inside a confirmation dialog.
COMMIT_RE = re.compile(
    r"(erase\s+(all\s+data|everything)|reset\s+(settings|phone|network|all)|"
    r"delete\s+(account|forever)|remove\s+account|sign\s*out|format\s+sd)", re.I)


# the toolbar back/up arrow exposes itself as a clickable with this content-desc;
# it's navigation chrome, not a child row — tapping it just goes back
NAV_UP_RE = re.compile(r"^(navigate up|go back|back)$", re.I)


def allowed_to_tap(text: str, risk: str) -> bool:
    """Gate a candidate row's text against the risk tier."""
    if risk == "all":
        return True
    if COMMIT_RE.search(text):
        return False
    if risk == "open":
        return True
    return not DESTRUCTIVE_RE.search(text)  # safe


def _norm(v: str) -> str:
    # neutralize live values (clocks, percentages, counts) so the same screen
    # signs identically across visits
    return re.sub(r"\d+", "#", v.strip())


def screen_signature(xml: str | None, activity: str | None) -> tuple:
    """A stable identity for a screen: its activity plus the set of normalized
    resource-ids / texts / content-descs. Distinguishes sibling SubSettings
    screens (same activity, different content) without being fooled by toggles
    or ticking values the way a pixel compare is."""
    if not xml:
        return (activity or "", "noxml")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return (activity or "", "badxml")
    keys = set()
    for el in root.iter("node"):
        for attr in ("resource-id", "text", "content-desc"):
            v = (el.get(attr) or "").strip()
            if v:
                keys.add(_norm(v))
    return (activity or "", tuple(sorted(keys)))


def has_focused_input(xml: str | None) -> bool:
    """True if a text field currently holds focus (keyboard is up). We refuse to
    swipe in this state — the gesture would type into the field."""
    if not xml:
        return False
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    for el in root.iter("node"):
        if el.get("class", "").endswith("EditText") and el.get("focused") == "true":
            return True
    return False


def clickable_texts(xml: str | None, risk: str) -> list[str]:
    """Ordered, de-duplicated identifiers (text / content-desc) of the tappable
    rows on a screen, filtered by the risk tier. Rows with no text are skipped —
    we can't re-locate them by text for path-replay."""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    seen, out = set(), []
    for el in root.iter("node"):
        if el.get("clickable") != "true":
            continue
        # the row's label usually lives in a descendant TextView, not on the
        # clickable container itself — take the first non-empty one in the subtree
        text = ""
        for d in el.iter("node"):
            text = (d.get("text") or d.get("content-desc") or "").strip()
            if text:
                break
        if not text or text in seen or NAV_UP_RE.match(text):
            continue
        if not allowed_to_tap(text, risk):
            continue
        seen.add(text)
        out.append(text)
    return out


def launch_start(serial: str, start: str) -> None:
    """Bring the crawl's start screen to the front, *reset to the top*. `start`
    is a component (pkg/Activity) launched with -n, or an intent action with -a.

    NEW_TASK|CLEAR_TASK (0x10008000) clears any existing task stack first, so we
    always land on the real start screen — not wherever a prior run left the app
    (e.g. parked deep in a SubSettings). This is what makes path-replay reliable:
    every node begins from an identical, known root."""
    flag = "-n" if "/" in start else "-a"
    try:
        adb(["shell", "am", "start", "-f", "0x10008000", flag, start],
            serial=serial, timeout=10.0)
    except RuntimeError as e:
        print(f"  (am start {start} failed: {e})")


def navigate(serial: str, start: str, path: tuple[str, ...], disp: dict,
             settle: float, max_scroll: int, probe_text: str | None = None):
    """Path-replay: relaunch `start`, then tap each row in `path` by its text
    (scrolling to find it). Deterministic and immune to scroll drift — every
    frontier node is reached fresh from the root. Returns
    (png, xml, activity, source, last_tap_action) or None if a row vanished."""
    launch_start(serial, start)
    png, xml, activity, source = snapshot(serial, settle)
    # clean slate: a soft keyboard left up by a prior text screen would make our
    # scroll swipes type garbage into it — dismiss it before touching anything.
    # While the IME is showing, BACK only hides it (it does not navigate).
    for _ in range(2):
        if not has_focused_input(xml):
            break
        adb(["shell", "input", "keyevent", "4"], serial=serial)
        png, xml, activity, source = snapshot(serial, settle)
    last_action = None
    for text in path:
        tries = 0
        while not find_element(xml, "text", text) and tries < max_scroll:
            if has_focused_input(xml):
                break  # keyboard is up — never swipe (it would type)
            prev = png
            perform_action(serial, "swipe", "up", xml, disp)
            png = wait_stable(serial, settle)
            xml, _ = grab_tree(serial, activity)
            if frames_equivalent(prev, png):
                break  # bottom reached, text not present
            tries += 1
        el = find_element(xml, "text", text)
        if not el:
            return None  # row no longer reachable — replay broke
        adb(["shell", "input", "tap", str(el["cx"]), str(el["cy"])], serial=serial)
        last_action = {"type": "tap", "x": el["cx"], "y": el["cy"],
                       "elementId": el["rid"], "bounds": el["bounds"]}
        png, xml, activity, source = snapshot(serial, settle)
    # if we landed on a text field (e.g. search), type a sample query so the
    # captured node shows results. The next node's relaunch resets everything.
    if probe_text and has_focused_input(xml):
        adb(["shell", "input", "text", probe_text], serial=serial)
        png, xml, activity, source = snapshot(serial, settle)
    return png, xml, activity, source, last_action


def enumerate_rows(serial: str, xml: str | None, png: bytes, disp: dict,
                   settle: float, max_scroll: int, risk: str,
                   activity: str | None = None) -> tuple[list[str], list[bytes]]:
    """Scroll the current screen top-to-bottom, collecting every tappable row's
    text AND every distinct viewport frame (top-first). The screen must be at the
    top when called (it is, right after a snapshot). Leaves the screen scrolled
    down — fine, the next node relaunches from the root. The frames are stitched
    into the node's full-page screenshot; viewport count = len(frames)."""
    seen, order = set(), []
    frames = [png]

    def harvest(x: str | None) -> None:
        for t in clickable_texts(x, risk):
            if t not in seen:
                seen.add(t)
                order.append(t)

    harvest(xml)
    steps = 0
    while steps < max_scroll:
        if has_focused_input(xml):
            break  # keyboard is up — never swipe (it would type)
        prev = png
        perform_action(serial, "swipe", "up", xml, disp)
        png = wait_stable(serial, settle)
        nxt, _ = grab_tree(serial, activity)
        if frames_equivalent(prev, png):
            break  # nothing new revealed -> at the bottom
        frames.append(png)
        if nxt:
            harvest(nxt)
            xml = nxt
        steps += 1
    return order, frames


class Ansi:
    """Minimal ANSI colorizer. No-ops when output isn't a TTY or NO_COLOR is
    set, so piped/redirected logs stay clean."""
    CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32",
             "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
             "gray": "90"}

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __getattr__(self, name: str):
        code = self.CODES.get(name)
        if code is None:
            raise AttributeError(name)
        if not self.enabled:
            return lambda s: s
        return lambda s: f"\x1b[{code}m{s}\x1b[0m"


def _color_enabled() -> bool:
    return (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb")


def short_activity(activity: str | None) -> str:
    """`com.android.settings/.SubSettings` -> `.SubSettings`."""
    if not activity:
        return "?"
    return activity.split("/", 1)[1] if "/" in activity else activity


def viewport_bar(c: Ansi, viewports: int | None) -> str:
    """A little bar whose length is the screen's viewport height, e.g. [███]."""
    if not viewports:
        return ""
    n = min(viewports, 12)
    bar = "█" * n + ("…" if viewports > 12 else "")
    return c.gray("[") + c.green(bar) + c.gray("]")


def render_tree(c: Ansi, nodes: list[dict], edges: list[dict]) -> None:
    """Draw the discovered flow as a tree. Each node prints under the parent
    that first reached it; later edges to an already-placed node render as dim
    `↩` revisit references (so cycles/back-edges don't recurse forever)."""
    if not nodes:
        return
    nmap = {n["id"]: n for n in nodes}
    creation_parent: dict[str, str] = {}
    revisits: dict[str, list[tuple[str, str | None]]] = {}
    for e in edges:
        to = e["to"]
        if to != "n0" and to not in creation_parent:
            creation_parent[to] = e["from"]
        else:  # a back-edge / cycle: show it as a labeled revisit reference
            revisits.setdefault(e["from"], []).append((to, e.get("label")))
    kids: dict[str, list[str]] = {}
    for n in nodes:  # preserve creation order
        p = creation_parent.get(n["id"])
        if p is not None:
            kids.setdefault(p, []).append(n["id"])

    print()
    print(c.bold("Flow tree"))

    def walk(nid: str, prefix: str, is_last: bool, is_root: bool = False) -> None:
        node = nmap.get(nid, {})
        conn = "" if is_root else ("└── " if is_last else "├── ")
        title = node.get("title") or short_activity(node.get("activity"))
        line = (f"{prefix}{conn}{c.bold(title)} "
                f"{viewport_bar(c, node.get('viewports'))}")
        print(line.rstrip())
        child_prefix = "" if is_root else prefix + ("    " if is_last else "│   ")
        items = ([("node", k, None) for k in kids.get(nid, [])]
                 + [("rev", to, lbl) for to, lbl in revisits.get(nid, [])])
        for i, (kind, k, lbl) in enumerate(items):
            last = i == len(items) - 1
            if kind == "node":
                walk(k, child_prefix, last)
            else:
                rc = "└── " if last else "├── "
                rtitle = lbl or nmap.get(k, {}).get("title") or short_activity(nmap.get(k, {}).get("activity"))
                print(f"{child_prefix}{c.gray(rc + '↩ ' + rtitle + '  (seen)')}".rstrip())

    walk("n0", "", True, is_root=True)


def cmd_crawl(args) -> None:
    global TIMING
    TIMING = args.timing
    c = Ansi(_color_enabled())
    serial = pick_serial(args.serial)
    meta = device_metadata(serial)
    disp = meta["display"]
    start = args.start or current_activity(serial)
    if not start:
        sys.exit("Could not determine the current activity; pass --start "
                 "(e.g. --start android.settings.SETTINGS).")
    # if --start is a component we know the package up front; if it's an intent
    # action we don't, so derive it from the captured root activity below.
    start_pkg = start.split("/", 1)[0] if "/" in start else None
    print(f"Crawling from {start}  (depth<={args.max_depth}, "
          f"<={args.max_screens} screens, risk={args.risk}, {args.strategy})")

    awake_state = None if args.no_wake else keep_awake(serial)
    insets = device_insets(serial)  # nav-bar height: the stitch chrome floor
    assets: dict[str, bytes] = {}
    nodes: list[dict] = []
    edges: list[dict] = []
    visited: dict[tuple, str] = {}   # screen signature -> node id
    path_node: dict[tuple, str] = {}  # replay path -> node id (for linking edges)
    idx = 0
    skipped_oos = 0  # children not recursed because they left the start package
    total_nav = total_scan = 0.0  # cumulative latency, reported at the end
    crawl_t0 = time.monotonic()

    frontier: deque[tuple[str, ...]] = deque([()])
    try:
        while frontier and len(nodes) < args.max_screens:
            path = frontier.popleft() if args.strategy == "bfs" else frontier.pop()
            if path in path_node:
                continue  # already resolved via a shorter route
            t_nav = time.monotonic()
            nav = navigate(serial, start, path, disp, args.settle, args.max_scroll,
                           probe_text=args.probe_text)
            nav_dt = time.monotonic() - t_nav
            total_nav += nav_dt
            depth = len(path)
            indent = "  " * depth
            title = path[-1] if path else None
            if nav is None:
                print(f"{indent}{c.gray('· could not reach ' + (title or 'start'))}")
                continue
            png, xml, activity, source, action = nav
            if not path and activity:  # root: lock the in-scope package to it
                start_pkg = activity.split("/", 1)[0]
            sig = screen_signature(xml, activity)
            parent_id = path_node.get(path[:-1])

            if sig in visited:
                existing = visited[sig]
                path_node[path] = existing
                if path and parent_id and existing != parent_id:
                    edges.append({"from": parent_id, "to": existing, "action": action,
                                  "label": title, "transition": None})
                    print(f"{indent}{c.gray('↩ ' + (title or '') + '  (already seen)')}")
                continue

            # explore (scroll-scan) BEFORE building the node: the same scroll
            # pass that finds child rows also yields the frames we stitch into
            # this screen's full-page screenshot — no extra device time
            in_scope = not (start_pkg and activity and not activity.startswith(start_pkg))
            do_scan = in_scope and depth < args.max_depth
            rows, frames, scan_dt = [], [png], 0.0
            if do_scan:
                t_scan = time.monotonic()
                rows, frames = enumerate_rows(serial, xml, png, disp, args.settle,
                                              args.max_scroll, args.risk, activity)
                scan_dt = time.monotonic() - t_scan
                total_scan += scan_dt
            if not in_scope:
                skipped_oos += 1

            # one tall PNG per screen (stitched from the scrolled frames); a
            # single-viewport screen stitches to itself with longshot=None
            shot, longshot_meta = stitch_frames(frames, bot_floor=insets["bottom"])

            # collapsing-header crossfade: the fat title in the stitched top morphs
            # into the skinny app bar as you scroll. Skinny height = the collapsed
            # app bar = status inset + a standard 56dp toolbar (stable on short
            # 2-viewport screens, unlike the stitch's median chrome). Fat bottom =
            # the scrollable list's top from the hierarchy. The viewer fades a
            # skinny crop in over `collapsePx`.
            header = None
            if longshot_meta and len(frames) >= 2:
                density = disp.get("density") or 160
                skinny_h = (insets.get("top") or 0) + round(56 * density / 160)
                if not (insets.get("top")):  # no inset reading -> fall back to chrome
                    skinny_h = longshot_meta["topChrome"]
                fat_h = content_top(xml, disp.get("w") or 1080, disp.get("h") or 2400)
                if fat_h and skinny_h and fat_h - skinny_h >= 24:
                    header = {
                        "skinny": register_asset(assets, _crop_top_png(frames[-1], skinny_h), "png"),
                        "skinnyH": skinny_h, "fatH": fat_h, "collapsePx": fat_h - skinny_h,
                    }

            if path:
                idx += 1
                nid = f"n{idx}"
            else:
                nid = "n0"
            node_obj = build_node(nid, shot, xml, activity, source, assets)
            node_obj["title"] = title or short_activity(activity)
            node_obj["viewport"] = [disp.get("w"), disp.get("h")]
            node_obj["longshot"] = longshot_meta  # null unless it scrolled
            node_obj["viewports"] = len(frames)
            node_obj["header"] = header  # null unless a collapsing header was found
            nodes.append(node_obj)
            visited[sig] = nid
            path_node[path] = nid
            if path:
                edges.append({"from": parent_id, "to": nid, "action": action,
                              "label": title, "transition": None})

            # one clean line per screen: name, viewport bar, and timing
            bar = (" " + viewport_bar(c, len(frames))) if len(frames) > 1 else ""
            note = (" " + c.yellow("· external") if not in_scope else
                    " " + c.gray("· max depth") if depth >= args.max_depth else "")
            print(f"{indent}{c.bold(node_obj['title'])}{bar}{note}  "
                  f"{c.gray(f'{nav_dt + scan_dt:.1f}s')}")

            for row in rows:
                frontier.append(path + (row,))
    finally:
        if not args.no_wake:
            restore_awake(serial, awake_state)

    if frontier:
        print(c.gray(f"  stopped at the {args.max_screens}-screen limit; "
                     f"{len(frontier)} more to explore"))
    if skipped_oos:
        print(c.gray(f"  {skipped_oos} screen(s) outside {start_pkg} — captured, not explored"))
    wall = time.monotonic() - crawl_t0
    m, s = divmod(int(wall), 60)
    dur = f"{m}m {s}s" if m else f"{s}s"
    print()
    summary = c.bold(f"Captured {len(nodes)} screen(s) in {dur}")
    if TIMING and nodes:
        summary += c.gray(f"   nav {total_nav:.0f}s · scan {total_scan:.0f}s "
                          f"· {total_nav / len(nodes):.1f}s/screen")
    print(summary)

    render_tree(c, nodes, edges)

    manifest = {
        "formatVersion": FORMAT_VERSION,
        "tool": f"droidshot/{TOOL_VERSION}",
        "capturedAt": now_iso(),
        "device": meta,
        "app": None,
        "nodes": nodes,
        "edges": edges,
    }
    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_model = re.sub(r"[^A-Za-z0-9]+", "-", meta["model"] or "device").strip("-")
        out_path = ROOT / "captures" / f"{stamp}-{safe_model}-crawl.droidshot"
    write_droidshot(out_path, manifest, assets)
    print(f"Wrote {out_path}  ({out_path.stat().st_size // 1024} KiB, "
          f"{len(nodes)} node(s), {len(edges)} edge(s))")


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # live progress even when piped
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="droidshot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="download adb into ./vendor")
    p_setup.add_argument("--force", action="store_true", help="re-download even if present")
    p_setup.set_defaults(func=cmd_setup)

    p_doctor = sub.add_parser("doctor", help="check adb and connected devices")
    p_doctor.set_defaults(func=cmd_doctor)

    p_cap = sub.add_parser("capture", help="capture screen(s) into a .droidshot")
    p_cap.add_argument("--serial", help="device serial (required if multiple connected)")
    p_cap.add_argument("--out", help="output path (default: captures/<stamp>-<model>.droidshot)")
    p_cap.add_argument("--do", action="append", metavar="ACTION",
                       help="action after the current screen; repeatable, runs in order. "
                            "e.g. --do tap-id=com.x:id/btn  --do tap-text=Display  "
                            "--do swipe=up  --do tap=540,1200  --do back  --do key=4")
    p_cap.add_argument("--settle", type=float, default=0.4,
                       help="seconds between stability-check frames (default 0.4)")
    p_cap.add_argument("--reset", action="store_true",
                       help="scroll to the top before capturing n0")
    p_cap.add_argument("--max-scroll", type=int, default=20,
                       help="max swipes for a scroll / scroll-to step (default 20)")
    p_cap.add_argument("--no-wake", action="store_true", help="don't wake/keep-awake the device")
    p_cap.set_defaults(func=cmd_capture)

    p_crawl = sub.add_parser("crawl", help="autonomously explore from a starting screen")
    p_crawl.add_argument("--serial", help="device serial (required if multiple connected)")
    p_crawl.add_argument("--out", help="output path (default: captures/<stamp>-<model>-crawl.droidshot)")
    p_crawl.add_argument("--start", metavar="ACTIVITY",
                         help="screen to start from: a component (pkg/Activity) or an "
                              "intent action (e.g. android.settings.SETTINGS). "
                              "Default: whatever's on screen now.")
    p_crawl.add_argument("--max-depth", type=int, default=3,
                         help="how many taps deep to explore from the start (default 3)")
    p_crawl.add_argument("--max-screens", type=int, default=150,
                         help="hard cap on captured screens, so the crawl terminates (default 150)")
    p_crawl.add_argument("--strategy", choices=["bfs", "dfs"], default="bfs",
                         help="bfs maps each level before going deeper (default); dfs plunges")
    p_crawl.add_argument("--risk", choices=["safe", "open", "all"], default="safe",
                         help="safe: never tap reset/erase/sign-out rows (default); "
                              "open: open them but refuse final commit buttons; "
                              "all: tap everything")
    p_crawl.add_argument("--probe-text", default="test", metavar="STR",
                         help="text to type if the crawler lands on a search/text "
                              "field, to capture the result (default 'test'; '' to skip)")
    p_crawl.add_argument("--settle", type=float, default=0.4,
                         help="seconds between stability-check frames (default 0.4)")
    p_crawl.add_argument("--max-scroll", type=int, default=20,
                         help="max swipes when scanning/scrolling a screen (default 20)")
    p_crawl.add_argument("--no-wake", action="store_true", help="don't wake/keep-awake the device")
    p_crawl.add_argument("--timing", action="store_true",
                         help="print a per-snapshot latency breakdown (stable/uiauto/viewtree)")
    p_crawl.set_defaults(func=cmd_crawl)

    p_ann = sub.add_parser("annotate", help="(re)compute scroll metadata for an existing .droidshot")
    p_ann.add_argument("file", help="path to a .droidshot file")
    p_ann.set_defaults(func=cmd_annotate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
