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
import io
import json
import os
import platform
import re
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
FORMAT_VERSION = 1

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


def grab_hierarchy(serial: str, retries: int = 2, dump_timeout: float = 13.0) -> str | None:
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
    png = wait_stable(serial, settle)
    xml = grab_hierarchy(serial)
    source = "uiautomator" if xml else None
    if xml is None:
        xml = grab_view_tree(serial)
        source = "viewtree" if xml else None
    return png, xml, current_activity(serial), source


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
        if not prev_xml:
            sys.exit(f"{kind}: no hierarchy available to resolve '{val}'")
        el = find_element(prev_xml, "id" if kind == "tap-id" else "text", val or "")
        if not el:
            sys.exit(f"{kind}: no element matching '{val}'")
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
    idx = 0  # index of the most recently added node

    def add_node_edge(action: dict, png: bytes, xml: str | None, activity: str | None,
                      source: str | None, label: str):
        nonlocal idx
        prev = f"n{idx}"
        idx += 1
        cur = f"n{idx}"
        nodes.append(build_node(cur, png, xml, activity, source, assets))
        edges.append({"from": prev, "to": cur, "action": action, "transition": None})
        print(f"  {cur}  {label}  ->  {activity}  [{source or 'no-hierarchy'}]")

    try:
        if args.reset:
            reset_scroll(serial, disp, args.settle)
        png, xml, activity, source = snapshot(serial, args.settle)
        nodes.append(build_node("n0", png, xml, activity, source, assets))
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
                    # tolerant compare: stops at the bottom even with no hierarchy
                    # (live-updating screens) and ignores a ticking counter
                    if frames_equivalent(prev_png, png):
                        print(f"      (reached end; '{target}' not found)")
                        break
                    add_node_edge(action, png, xml, activity, source, f"scroll-to={target} (swipe up)")
                    tries += 1
                continue
            # single-step action
            action = perform_action(serial, kind, val, xml, disp)
            png, xml, activity, source = snapshot(serial, args.settle)
            add_node_edge(action, png, xml, activity, source, f"{kind}{('=' + val) if val else ''}")
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
    p_cap.add_argument("--max-scroll", type=int, default=12,
                       help="max swipes for a scroll-to step (default 12)")
    p_cap.add_argument("--no-wake", action="store_true", help="don't wake/keep-awake the device")
    p_cap.set_defaults(func=cmd_capture)

    p_ann = sub.add_parser("annotate", help="(re)compute scroll metadata for an existing .droidshot")
    p_ann.add_argument("file", help="path to a .droidshot file")
    p_ann.set_defaults(func=cmd_annotate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
