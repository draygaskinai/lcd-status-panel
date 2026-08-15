"""
Live mic-mute + whisper-dictation status panel, plus a condensed CPU/GPU/RAM
stats grid, for the 3.5" USB-C display (Turing Smart Screen rev. A / TURZX
clone, VID_1A86:PID_5722, "USB35INCHIPSV2").

Replaces the vendor UsbMonitor.exe app, which can only show a fixed sensor
set plus static text - no way to push live custom data to it. This talks to
the panel directly over its serial protocol via the turing-smart-screen-python
library (library/lcd/lcd_comm_rev_a.py).

CPU temperature needs admin rights (LibreHardwareMonitor reads low-level MSRs,
which Windows only allows to an elevated process) - see _lhm_cpu below. This
script degrades gracefully (shows "N/A") when not elevated instead of crashing;
run it via the elevated Scheduled Task for real CPU temp readings.
"""

import configparser
import ctypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import POINTER, cast
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import GPUtil
import psutil
import pystray
from comtypes import CLSCTX_ALL
from PIL import Image, ImageDraw, ImageFont
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# BUNDLE_DIR is where the bundled data files (external/LibreHardwareMonitor,
# PawnIO) live; APP_DIR is where the user's editable config.ini lives. Under
# PyInstaller these differ - data is unpacked next to the exe, but the user
# edits the copy beside the exe itself.
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = APP_DIR = os.path.dirname(os.path.abspath(__file__))

# sensors_librehardwaremonitor resolves its DLL against os.getcwd(), not against
# its own location, so a frozen exe launched from anywhere else silently loses
# CPU temp (it degrades to N/A rather than erroring, which reads as a hardware
# fault). Pin cwd to the bundle before that import happens, below.
os.chdir(BUNDLE_DIR)

sys.path.insert(0, BUNDLE_DIR)
from library.lcd.lcd_comm import Orientation
from library.lcd.lcd_comm_rev_a import LcdCommRevA

STALE_AFTER_SEC = 10  # dictate.py heartbeats faster than this -> stale really means "not running"
POLL_INTERVAL = 0.5  # was 1.0 - doubled once the GPU read stopped spawning nvidia-smi per poll

ORIENTATIONS = {
    "landscape": Orientation.LANDSCAPE,
    "landscape_180": Orientation.REVERSE_LANDSCAPE,
    "portrait": Orientation.PORTRAIT,
    "portrait_180": Orientation.REVERSE_PORTRAIT,
}

# The dictation block (left half in landscape, top band in portrait) is
# shown whenever a status file is configured - independent of the "mic
# panel" below. The mic panel is the *other* slot: the full top band when
# dictation is off, the right half / second band when it's on. It shows
# exactly one of these.
MIC_PANELS = ("mic", "datetime")
MIC_PANEL_LABELS = {
    "mic": "Mic status",
    "datetime": "Date & Time",
}

_DEFAULTS = {
    "display": {"orientation": "landscape", "brightness": "80", "mic_panel": "mic"},
    "dictation": {"status_file": ""},
    "thresholds": {
        "cpu_warn": "50", "cpu_bad": "80",
        "cpu_temp_warn": "75", "cpu_temp_bad": "85",
        "ram_warn": "65", "ram_bad": "80",
        "gpu_warn": "50", "gpu_bad": "80",
        "gpu_temp_warn": "50", "gpu_temp_bad": "60",
        "vram_warn": "50", "vram_bad": "80",
    },
}


def load_config():
    parser = configparser.ConfigParser()
    parser.read_dict(_DEFAULTS)
    parser.read(os.path.join(APP_DIR, "config.ini"), encoding="utf-8")
    return parser


def _load_display_settings():
    """(Re)reads config.ini into the module globals below. Called once at
    import time and again by the tray's Reload Config, so anything that
    should pick up an edited config.ini without a restart has to be read
    from these globals at use time, not cached elsewhere."""
    global CONFIG, ORIENTATION, BRIGHTNESS, DICTATE_STATUS_FILE, SHOW_DICTATION, MIC_PANEL
    CONFIG = load_config()
    ORIENTATION = ORIENTATIONS.get(
        CONFIG["display"]["orientation"].strip().lower(), Orientation.LANDSCAPE
    )
    BRIGHTNESS = CONFIG["display"].getint("brightness")
    DICTATE_STATUS_FILE = CONFIG["dictation"]["status_file"].strip()
    SHOW_DICTATION = bool(DICTATE_STATUS_FILE)

    mic_panel = CONFIG["display"]["mic_panel"].strip().lower()
    MIC_PANEL = mic_panel if mic_panel in MIC_PANELS else "mic"


CONFIG = ORIENTATION = BRIGHTNESS = DICTATE_STATUS_FILE = SHOW_DICTATION = MIC_PANEL = None
_load_display_settings()


def threshold(name):
    return CONFIG["thresholds"].getfloat(f"{name}_warn"), CONFIG["thresholds"].getfloat(f"{name}_bad")

COLOR_BG = (8, 9, 14)
COLOR_DIVIDER = (40, 42, 52)
COLOR_LABEL = (150, 150, 165)
COLOR_LISTENING = (60, 220, 130)
COLOR_IDLE = (110, 115, 130)
COLOR_TRANSCRIBING = (240, 190, 60)
COLOR_OFFLINE = (220, 70, 70)
COLOR_MIC_LIVE = (60, 220, 130)
COLOR_MIC_MUTED = (220, 70, 70)
COLOR_MIC_UNKNOWN = (110, 115, 130)
COLOR_GOOD = (60, 220, 130)
COLOR_WARN = (240, 190, 60)
COLOR_BAD = (220, 70, 70)
COLOR_NA = (90, 92, 105)
COLOR_CLOCK = (230, 232, 240)  # neutral bright - the clock isn't a status, so it skips the severity palette

# This environment has been observed to spawn a second copy of a pythonw.exe
# script under a different Python install (same phenomenon noted in
# whisper-dictation/dictate.py). Two copies fighting over the same exclusive
# COM8 serial handle corrupts writes to the panel, so guard with a mutex like
# dictate.py does.
_SINGLE_INSTANCE_MUTEX_NAME = "LcdStatusPanelSingleInstance_7c2e9a"


def _acquire_single_instance_lock():
    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
    already_running = ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    return handle, already_running


# LibreHardwareMonitorLib force-exits the whole process (os._exit) if imported
# while not elevated - see library/sensors/sensors_librehardwaremonitor.py.
# Gate the import ourselves so a non-elevated run just loses the CPU temp tile
# instead of dying outright. The os.chdir(BUNDLE_DIR) above is what makes its
# getcwd-relative DLL lookup resolve correctly.
#
# GPU stats ride along on the same gate. LHM can read GPU load/temp/VRAM
# in-process (Nvidia/AMD/Intel) with no subprocess involved, unlike GPUtil
# below - but the module-level admin check above forces the whole module
# closed when not elevated, so GPUtil stays as the non-elevated fallback
# (and, incidentally, works on AMD/Intel too, so this also quietly clears
# the "AMD GPU support" line item from the deferred universal-version scope
# for anyone running elevated).
_IS_ADMIN = ctypes.windll.shell32.IsUserAnAdmin() != 0
_lhm_cpu = None
_lhm_gpu = None
if _IS_ADMIN:
    try:
        from library.sensors.sensors_librehardwaremonitor import Cpu as _lhm_cpu
    except Exception as e:
        print(f"LibreHardwareMonitor unavailable, CPU temp will show N/A: {e}")
        _lhm_cpu = None
    else:
        # Separate try/except from the Cpu import above - a GPU-detection
        # failure here shouldn't also take down CPU temp, which just succeeded.
        try:
            from library.sensors.sensors_librehardwaremonitor import Gpu as _lhm_gpu_cls
            _lhm_gpu = _lhm_gpu_cls if _lhm_gpu_cls.is_available() else None
        except Exception as e:
            print(f"LibreHardwareMonitor GPU sensors unavailable, falling back to GPUtil: {e}")
            _lhm_gpu = None
else:
    print("Not running elevated - CPU temp will show N/A.")
    print("  Run 'LcdPanel.exe --install-autostart' to start it elevated at login.")


def _pawnio_installed():
    """PawnIO is a kernel driver; LibreHardwareMonitor needs it for real CPU
    package temperature. Without it LHM silently reports 0 rather than failing,
    which reads as a hardware fault - so check and say so explicitly."""
    try:
        result = subprocess.run(
            ["sc", "query", "PawnIO"], capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


def warn_if_cpu_temp_unavailable():
    if not _IS_ADMIN or _pawnio_installed():
        return
    installer = os.path.join(BUNDLE_DIR, "external", "PawnIO", "PawnIO_setup.exe")
    print("\nCPU temperature needs the PawnIO driver, which isn't installed.")
    print("It ships alongside this app but is NOT installed automatically,")
    print("because it is a kernel driver and that should be your call.")
    print(f"  To install it, run as administrator:\n    \"{installer}\" -install -silent")
    print("Everything else works without it; CPU TEMP will read N/A.\n")


DICTATION_LABELS = {
    "listening": "LISTENING...",
    "idle": "IDLE",
    "transcribing": "TRANSCRIBING...",
    "offline": "OFFLINE",
}
DICTATION_COLORS = {
    "listening": COLOR_LISTENING,
    "idle": COLOR_IDLE,
    "transcribing": COLOR_TRANSCRIBING,
    "offline": COLOR_OFFLINE,
}
# Waveform bar heights (0-1 ratio) per dictation state - taller/more varied
# reads as "active", flat reads as "at rest".
DICTATION_BARS = {
    "listening": [0.35, 0.75, 1.0, 0.55, 0.9, 0.45, 0.65],
    "transcribing": [0.55, 0.35, 0.6, 0.4, 0.6, 0.35, 0.55],
    "idle": [0.15, 0.15, 0.15, 0.15, 0.15, 0.15, 0.15],
    "offline": [0.08, 0.08, 0.08, 0.08, 0.08, 0.08, 0.08],
}


def get_font(size, bold=False):
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    try:
        return ImageFont.truetype(os.path.join(os.environ["WINDIR"], "Fonts", name), size)
    except OSError:
        return ImageFont.load_default()


FONT_LABEL_SM = get_font(15)
FONT_STATE_SM = get_font(20, bold=True)
FONT_TILE_LABEL = get_font(13)
FONT_TILE_VALUE = get_font(19, bold=True)
FONT_CLOCK_TIME = get_font(34, bold=True)
FONT_CLOCK_DATE = get_font(15)


def read_dictation_state():
    try:
        with open(DICTATE_STATUS_FILE) as f:
            data = json.load(f)
        if time.time() - data["ts"] > STALE_AFTER_SEC:
            return "offline"
        return data.get("state", "offline")
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
        return "offline"


def read_mic_muted():
    try:
        mic = AudioUtilities.GetMicrophone()
        interface = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        return bool(volume.GetMute())
    except Exception:
        return None  # no default capture device, or query failed


def read_input_device_name():
    try:
        mic = AudioUtilities.GetMicrophone()
        name = AudioUtilities.CreateDevice(mic).FriendlyName
        # "Microphone (GSX 1000 Communication Audio)" -> "GSX 1000 Communication Audio"
        if name.startswith("Microphone (") and name.endswith(")"):
            name = name[len("Microphone ("):-1]
        return name
    except Exception:
        return "Unknown device"


def read_system_stats():
    cpu_pct = psutil.cpu_percent(interval=None)
    ram_pct = psutil.virtual_memory().percent

    cpu_temp = None
    if _lhm_cpu is not None:
        try:
            t = _lhm_cpu.temperature()
            if not math.isnan(t):
                cpu_temp = t
        except Exception:
            cpu_temp = None

    gpu_pct = gpu_temp = vram_pct = None
    if _lhm_gpu is not None:
        # In-process read via LibreHardwareMonitor - no subprocess spawn, unlike
        # the GPUtil path below. load/vram% already come back as 0-100.
        try:
            load, vram_pct_val, _used, _total, temp = _lhm_gpu.stats()
            if not math.isnan(load):
                gpu_pct = load
            if not math.isnan(temp):
                gpu_temp = temp
            if not math.isnan(vram_pct_val):
                vram_pct = vram_pct_val
        except Exception:
            pass
    else:
        # Non-elevated fallback - GPUtil shells out to nvidia-smi.exe on every
        # call, so this path is real subprocess overhead per poll, not free
        # like the LHM path above. Nvidia only.
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                g = gpus[0]
                gpu_pct = g.load * 100
                gpu_temp = g.temperature
                vram_pct = (g.memoryUsed / g.memoryTotal * 100) if g.memoryTotal else None
        except Exception:
            pass

    return cpu_pct, cpu_temp, ram_pct, gpu_pct, gpu_temp, vram_pct


def wrap_text(draw, text, font, max_width, max_lines=2):
    words = text.split()
    lines = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)

    consumed_words = sum(len(l.split()) for l in lines)
    if consumed_words < len(words) and lines:
        last = lines[-1]
        while draw.textlength(last + "...", font=font) > max_width and last:
            last = last[:-1].rstrip()
        lines[-1] = last + "..."
    return lines or [""]


def draw_centered_text(draw, cx, y, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def draw_mic_icon(draw, cx, cy, size, color, line_width=4):
    head_w = size * 0.42
    head_h = size * 0.62
    head_top = cy - size * 0.55
    head_left = cx - head_w / 2
    draw.rounded_rectangle(
        [head_left, head_top, head_left + head_w, head_top + head_h],
        radius=head_w / 2, outline=color, width=line_width,
    )
    for i in range(3):
        gy = head_top + head_h * (0.28 + 0.22 * i)
        draw.line(
            [head_left + head_w * 0.18, gy, head_left + head_w * 0.82, gy],
            fill=color, width=2,
        )
    arc_r = head_w * 0.95
    arc_top = head_top + head_h * 0.30
    arc_bottom = arc_top + arc_r * 1.7
    draw.arc([cx - arc_r, arc_top, cx + arc_r, arc_bottom], start=10, end=170,
              fill=color, width=line_width)
    stem_top = arc_bottom - arc_r * 0.15
    stem_bottom = stem_top + size * 0.16
    draw.line([cx, stem_top, cx, stem_bottom], fill=color, width=line_width)
    base_half = size * 0.20
    draw.line([cx - base_half, stem_bottom, cx + base_half, stem_bottom],
              fill=color, width=line_width)


def draw_waveform_icon(draw, cx, cy, width, height, color, bar_ratios):
    n = len(bar_ratios)
    bar_w = width / (2 * n - 1)
    total_w = bar_w * (2 * n - 1)
    start_x = cx - total_w / 2
    for i, ratio in enumerate(bar_ratios):
        bh = max(height * ratio, bar_w)
        x0 = start_x + i * 2 * bar_w
        x1 = x0 + bar_w
        y0 = cy - bh / 2
        y1 = cy + bh / 2
        draw.rounded_rectangle([x0, y0, x1, y1], radius=bar_w / 2, fill=color)


def draw_chip_icon(draw, x, y, size, color, line_width=2):
    body = size * 0.62
    left = x + (size - body) / 2
    top = y + (size - body) / 2
    draw.rectangle([left, top, left + body, top + body], outline=color, width=line_width)
    pin_len = size * 0.16
    n_pins = 3
    for i in range(n_pins):
        py = top + body * (0.2 + 0.3 * i)
        draw.line([left - pin_len, py, left, py], fill=color, width=line_width)
        draw.line([left + body, py, left + body + pin_len, py], fill=color, width=line_width)
    for i in range(n_pins):
        px = left + body * (0.2 + 0.3 * i)
        draw.line([px, top - pin_len, px, top], fill=color, width=line_width)
        draw.line([px, top + body, px, top + body + pin_len], fill=color, width=line_width)


def draw_thermometer_icon(draw, x, y, size, color, line_width=2):
    stem_w = size * 0.22
    stem_top = y + size * 0.05
    stem_bottom = y + size * 0.62
    cx = x + size / 2
    draw.rounded_rectangle(
        [cx - stem_w / 2, stem_top, cx + stem_w / 2, stem_bottom],
        radius=stem_w / 2, outline=color, width=line_width,
    )
    bulb_r = size * 0.24
    bulb_cy = y + size * 0.78
    draw.ellipse([cx - bulb_r, bulb_cy - bulb_r, cx + bulb_r, bulb_cy + bulb_r], fill=color)
    fill_top = stem_top + (stem_bottom - stem_top) * 0.35
    draw.rounded_rectangle(
        [cx - stem_w / 4, fill_top, cx + stem_w / 4, stem_bottom + bulb_r * 0.3],
        radius=stem_w / 4, fill=color,
    )


def draw_memory_icon(draw, x, y, size, color, line_width=2):
    body_h = size * 0.55
    top = y + (size - body_h) / 2
    left = x + size * 0.08
    right = x + size * 0.92
    draw.rounded_rectangle([left, top, right, top + body_h], radius=3, outline=color, width=line_width)
    n_notch = 5
    notch_w = (right - left) * 0.6 / n_notch
    gap = (right - left) * 0.4 / (n_notch + 1)
    nx = left + gap
    for _ in range(n_notch):
        draw.rectangle([nx, top - size * 0.12, nx + notch_w, top], fill=color)
        nx += notch_w + gap


def severity_color(value, warn=50, bad=80):
    if value is None:
        return COLOR_NA
    if value >= bad:
        return COLOR_BAD
    if value >= warn:
        return COLOR_WARN
    return COLOR_GOOD


def format_stat(value, unit):
    return f"{value:.0f}{unit}" if value is not None else "N/A"


def mic_color_for(mic_muted):
    if mic_muted is None:
        return COLOR_MIC_UNKNOWN
    return COLOR_MIC_MUTED if mic_muted else COLOR_MIC_LIVE


def build_tiles(cpu_pct, cpu_temp, ram_pct, gpu_pct, gpu_temp, vram_pct):
    return [
        (draw_chip_icon, "CPU", format_stat(cpu_pct, "%"), severity_color(cpu_pct, *threshold("cpu"))),
        (draw_thermometer_icon, "CPU TEMP", format_stat(cpu_temp, "°C"), severity_color(cpu_temp, *threshold("cpu_temp"))),
        (draw_memory_icon, "RAM", format_stat(ram_pct, "%"), severity_color(ram_pct, *threshold("ram"))),
        (draw_chip_icon, "GPU", format_stat(gpu_pct, "%"), severity_color(gpu_pct, *threshold("gpu"))),
        (draw_thermometer_icon, "GPU TEMP", format_stat(gpu_temp, "°C"), severity_color(gpu_temp, *threshold("gpu_temp"))),
        (draw_memory_icon, "VRAM", format_stat(vram_pct, "%"), severity_color(vram_pct, *threshold("vram"))),
    ]


def draw_stat_grid(draw, tiles, x0, y0, w, h, cols):
    rows = -(-len(tiles) // cols)
    col_w = w / cols
    row_h = h / rows
    icon_size = 34
    for i, (icon_fn, label, value, color) in enumerate(tiles):
        row, col = divmod(i, cols)
        cx = x0 + col * col_w
        cy = y0 + row * row_h
        icon_fn(draw, cx + 12, cy + row_h / 2 - icon_size / 2, icon_size, color)
        text_x = cx + 12 + icon_size + 10
        draw.text((text_x, cy + row_h / 2 - 20), label, font=FONT_TILE_LABEL, fill=COLOR_LABEL)
        draw.text((text_x, cy + row_h / 2 - 2), value, font=FONT_TILE_VALUE, fill=color)


# icon_dx/text_dx space the icon and its text either side of the block centre.
# Portrait passes wider values: the block spans the full 320px there rather
# than a ~240px half-band, and the default spacing lets a long state string
# ("TRANSCRIBING...") run back into the icon.
def draw_dictation_block(draw, cx, cy, dictation_state, icon_dx=-55, text_dx=35):
    dict_color = DICTATION_COLORS.get(dictation_state, COLOR_IDLE)
    draw_waveform_icon(draw, cx + icon_dx, cy, 60, 46, dict_color,
                        DICTATION_BARS.get(dictation_state, DICTATION_BARS["idle"]))
    draw_centered_text(draw, cx + text_dx, cy - 26, "DICTATION", FONT_LABEL_SM, COLOR_LABEL)
    draw_centered_text(draw, cx + text_dx, cy - 4,
                        DICTATION_LABELS.get(dictation_state, dictation_state.upper()),
                        FONT_STATE_SM, dict_color)


def draw_mic_block(draw, cx, cy, mic_muted, device_name, name_width, icon_dx=-60, text_dx=30):
    color = mic_color_for(mic_muted)
    draw_mic_icon(draw, cx + icon_dx, cy, 44, color)
    device_lines = wrap_text(draw, device_name, FONT_LABEL_SM, name_width, max_lines=1)
    draw_centered_text(draw, cx + text_dx, cy - 26, device_lines[0], FONT_LABEL_SM, COLOR_LABEL)
    draw_centered_text(draw, cx + text_dx, cy - 4,
                        "MUTED" if mic_muted else "LIVE", FONT_STATE_SM, color)


def draw_datetime_block(draw, cx, cy, compact):
    # Portrait's band is only 58px tall (vs. landscape's 108), and the old
    # cy+18 date offset put the date text's real ink 3px past the divider
    # line drawn right below this block - looked like the line was cutting
    # through the text. cy+10 clears it with margin to spare.
    now = datetime.now()
    time_str = now.strftime("%I:%M %p").lstrip("0")
    date_fmt = "%a, %b %d" if compact else "%A, %B %d"
    draw_centered_text(draw, cx, cy - 24, time_str, FONT_CLOCK_TIME, COLOR_CLOCK)
    draw_centered_text(draw, cx, cy + 10, now.strftime(date_fmt), FONT_CLOCK_DATE, COLOR_LABEL)


# The "mic panel" slot shows mic status or the clock, independently of
# whether the dictation block (elsewhere in the top band) is showing - the
# two are unrelated axes, not a combined 3-way choice.
def draw_mic_panel(draw, cx, cy, mic_muted, device_name, name_width, compact,
                    icon_dx=-60, text_dx=30):
    if MIC_PANEL == "datetime":
        # cx is already the slot's centre - the icon_dx/text_dx offsets below
        # are a mic_block-specific quirk (asymmetric icon+text layout) that
        # the already-centred datetime block has no reason to mimic.
        draw_datetime_block(draw, cx, cy, compact)
    else:
        draw_mic_block(draw, cx, cy, mic_muted, device_name, name_width, icon_dx, text_dx)


def render_landscape(draw, width, height, dictation_state, mic_muted, device_name, tiles):
    top_h = int(height * 0.34)
    mid_x = width // 2

    if SHOW_DICTATION:
        # Dictation and mic panel side by side, split down the middle.
        draw.line((mid_x, 8, mid_x, top_h - 6), fill=COLOR_DIVIDER, width=2)
        draw_dictation_block(draw, mid_x // 2, top_h // 2, dictation_state)
        draw_mic_panel(draw, mid_x + mid_x // 2, top_h // 2, mic_muted, device_name,
                       mid_x * 0.55, compact=True)
    else:
        # Mic panel alone spans the whole band. The wider name allowance means
        # the icon has to move out with it, or a long device name grows back
        # underneath the icon.
        draw_mic_panel(draw, mid_x, top_h // 2, mic_muted, device_name,
                       width * 0.5, compact=False, icon_dx=-140, text_dx=40)

    draw.line((10, top_h, width - 10, top_h), fill=COLOR_DIVIDER, width=2)
    draw_stat_grid(draw, tiles, 10, top_h + 10, width - 20, height - 18 - top_h, cols=3)


def render_portrait(draw, width, height, dictation_state, mic_muted, device_name, tiles):
    # 320 wide can't fit two blocks side by side or a 3-column grid, so the
    # blocks stack and the grid goes 2x3 instead of 3x2.
    band_h = 58
    top_h = band_h * (2 if SHOW_DICTATION else 1) + 12
    mid_x = width // 2

    if SHOW_DICTATION:
        draw_dictation_block(draw, mid_x, band_h // 2 + 6, dictation_state,
                              icon_dx=-85, text_dx=35)
        draw.line((14, band_h + 6, width - 14, band_h + 6), fill=COLOR_DIVIDER, width=1)
        draw_mic_panel(draw, mid_x, band_h + band_h // 2 + 6, mic_muted, device_name,
                       width * 0.62, compact=True, icon_dx=-85, text_dx=30)
    else:
        draw_mic_panel(draw, mid_x, band_h // 2 + 6, mic_muted, device_name,
                       width * 0.62, compact=True, icon_dx=-85, text_dx=30)

    draw.line((10, top_h, width - 10, top_h), fill=COLOR_DIVIDER, width=2)
    draw_stat_grid(draw, tiles, 10, top_h + 10, width - 20, height - 18 - top_h, cols=2)


def render_frame(width, height, dictation_state, mic_muted, device_name,
                  cpu_pct, cpu_temp, ram_pct, gpu_pct, gpu_temp, vram_pct):
    img = Image.new("RGB", (width, height), COLOR_BG)
    draw = ImageDraw.Draw(img)
    tiles = build_tiles(cpu_pct, cpu_temp, ram_pct, gpu_pct, gpu_temp, vram_pct)
    layout = render_portrait if height > width else render_landscape
    layout(draw, width, height, dictation_state, mic_muted, device_name, tiles)
    return img


TASK_NAME = "LCD Status Panel"


def _launch_command():
    """How Task Scheduler should start us: the exe directly when frozen,
    otherwise the venv's pythonw plus this script."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(BUNDLE_DIR, "venv", "Scripts", "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def _run_elevated(args):
    """Re-launch ourselves elevated via UAC and wait for the result."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, " ".join(args)
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(__file__)}" ' + " ".join(args)
    # ShellExecuteW with "runas" is what raises the UAC prompt; a return value
    # of 32 or less is a failure code (5 = user declined).
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if rc <= 32:
        print("Elevation was declined or failed. Autostart not changed.")
        return False
    return True


def install_autostart():
    if not _IS_ADMIN:
        print("Requesting administrator rights...")
        _run_elevated(["--install-autostart"])
        return

    # /RL HIGHEST is what makes CPU temp work at login - LibreHardwareMonitor
    # needs an elevated process to read the MSRs.
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", _launch_command(),
         "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Could not create the scheduled task:\n{result.stderr.strip()}")
        return
    print(f'Autostart installed - "{TASK_NAME}" will start at every login.')
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True)
    print("Started it now, so you don't need to log out and back in.")


def uninstall_autostart():
    if not _IS_ADMIN:
        print("Requesting administrator rights...")
        _run_elevated(["--uninstall-autostart"])
        return

    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Could not remove the scheduled task:\n{result.stderr.strip()}")
        return
    print(f'Autostart removed - "{TASK_NAME}" will no longer start at login.')


# --- Background render thread + tray icon -------------------------------
#
# The panel used to run its draw loop directly on main(), with Task
# Scheduler's console window as the only UI. Closing that window sends a
# close signal that kills the whole process - the render loop dies with it,
# and the panel just holds its last frame forever (see "A frozen panel means
# nothing is driving it" above). Now the loop runs on its own thread, the
# console window is hidden outright, and a tray icon is the only surface a
# user can interact with - there's nothing left to click X on.

_stop_event = threading.Event()  # set to make panel_loop return
_force_redraw = threading.Event()  # set to make the next poll redraw even if nothing changed
_lcd_lock = threading.Lock()  # guards _lcd/_panel_size against reload/restart racing the render loop
_lcd = None
_panel_size = (0, 0)
_mutex_handle = None
_render_thread = None


def hide_console_window():
    """Frozen console=True builds get a console window at startup so
    --install-autostart / the PawnIO hint stay readable when run from a
    terminal; hide it once we're past that and into the tray-driven run
    loop. Only called when frozen - hiding it during `python status_panel.py`
    from a dev terminal would hide the terminal itself, since an inherited
    console shares the parent's window."""
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE


def reload_config():
    """Tray 'Reload Config': re-reads config.ini and pushes orientation/
    brightness to the already-connected panel without restarting the
    process. Thresholds don't need special handling - threshold() reads the
    global CONFIG live on every call - but a threshold-only edit doesn't
    change any of the values in panel_loop's change-detection key, so force
    one redraw to make a threshold-driven colour change visible immediately."""
    _load_display_settings()
    with _lcd_lock:
        if _lcd is not None:
            global _panel_size
            _lcd.SetBrightness(BRIGHTNESS)
            _lcd.SetOrientation(ORIENTATION)
            _panel_size = (_lcd.get_width(), _lcd.get_height())
    _force_redraw.set()


def panel_loop():
    global _lcd, _panel_size
    lcd = LcdCommRevA(com_port="AUTO")
    lcd.Reset()
    lcd.InitializeComm()
    lcd.Clear()
    lcd.ScreenOn()
    lcd.SetBrightness(BRIGHTNESS)
    lcd.SetOrientation(ORIENTATION)
    with _lcd_lock:
        _lcd = lcd
        _panel_size = (lcd.get_width(), lcd.get_height())

    try:
        last_key = None
        while not _stop_event.is_set():
            dictation_state = read_dictation_state() if SHOW_DICTATION else "offline"
            mic_muted = read_mic_muted()
            device_name = read_input_device_name()
            stats = read_system_stats()
            # Minute-granularity clock tick: nothing else in this key changes on
            # its own, so without this the clock could visibly stall on a poll
            # where every stat happens to read identical to the last one.
            clock_tick = datetime.now().strftime("%H:%M") if MIC_PANEL == "datetime" else None
            key = (dictation_state, mic_muted, device_name, stats, ORIENTATION, BRIGHTNESS,
                   SHOW_DICTATION, MIC_PANEL, clock_tick)
            if key != last_key or _force_redraw.is_set():
                with _lcd_lock:
                    width, height = _panel_size
                    lcd.DisplayPILImage(
                        render_frame(width, height, dictation_state, mic_muted, device_name, *stats)
                    )
                last_key = key
                _force_redraw.clear()
            _stop_event.wait(POLL_INTERVAL)
    finally:
        try:
            lcd.ScreenOff()
            lcd.closeSerial()
        except Exception:
            pass
        with _lcd_lock:
            _lcd = None


def _build_tray_image():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, size - 2, size - 2], radius=14, fill=COLOR_BG + (255,))
    draw_chip_icon(draw, size * 0.2, size * 0.2, size * 0.6, COLOR_GOOD, line_width=3)
    return img


def _notify(icon, message):
    try:
        icon.notify(message, "LCD Status Panel")
    except Exception:
        pass  # notify() isn't supported on every backend/Windows config


def _on_open_config(icon, item):
    try:
        os.startfile(os.path.join(APP_DIR, "config.ini"))
    except OSError:
        _notify(icon, "config.ini not found next to the exe.")


def _on_reload_config(icon, item):
    try:
        reload_config()
        _notify(icon, "Config reloaded.")
    except Exception as e:
        _notify(icon, f"Reload failed: {e}")


def _on_restart_panel(icon, item):
    """Relaunches the whole process rather than reconnecting the serial port
    in-place - re-opening a COM port that a still-running process has open
    is exactly the "two copies fighting over the port" failure mode noted
    above, so the old instance fully releases the port and its single-
    instance mutex before the new one starts."""
    launch_cmd = _launch_command()
    _stop_event.set()
    if _render_thread is not None:
        _render_thread.join(timeout=5)
    if _mutex_handle:
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
    subprocess.Popen(launch_cmd, shell=True, cwd=APP_DIR)
    icon.stop()


def _on_exit(icon, item):
    _stop_event.set()
    icon.stop()


# --- Settings window -------------------------------------------------------
#
# A GUI alternative to hand-editing config.ini. save_config_values() rewrites
# only the matched "key = value" lines rather than using configparser.write()
# for the whole file - configparser would silently drop every hand-written
# comment in config.ini/config.dist.ini, which is most of what makes that
# file readable.

_INI_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_INI_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*).*$")

THRESHOLD_FIELDS = [
    ("cpu", "CPU %", 100),
    ("cpu_temp", "CPU Temp °C", 120),
    ("ram", "RAM %", 100),
    ("gpu", "GPU %", 100),
    ("gpu_temp", "GPU Temp °C", 120),
    ("vram", "VRAM %", 100),
]

_settings_lock = threading.Lock()
_settings_open = False


def save_config_values(values):
    """values: {(section, key): str}. In-place line edit rather than a full
    rewrite - see module note above for why. Only replaces lines that already
    exist in the file; it does not insert missing keys. Any new Settings
    field needs its "key =" line added to config.dist.ini (and any already-
    deployed config.ini) at the same time, or saving it here will silently
    no-op on a config.ini that predates that field."""
    path = os.path.join(APP_DIR, "config.ini")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    section = None
    for i, line in enumerate(lines):
        m = _INI_SECTION_RE.match(line)
        if m:
            section = m.group(1).strip().lower()
            continue
        m = _INI_KEY_RE.match(line)
        if m and section is not None:
            key = m.group(2).strip().lower()
            if (section, key) in values:
                lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{values[(section, key)]}\n"

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _on_open_settings(icon, item):
    global _settings_open
    with _settings_lock:
        if _settings_open:
            return
        _settings_open = True
    threading.Thread(target=_run_settings_window, args=(icon,), daemon=True).start()


def _run_settings_window(icon):
    global _settings_open
    try:
        _build_settings_window(icon)
    finally:
        with _settings_lock:
            _settings_open = False


def _build_settings_window(icon):
    root = tk.Tk()
    root.title("LCD Status Panel Settings")
    root.resizable(False, False)
    pad = {"padx": 8, "pady": 4}

    display_frame = ttk.LabelFrame(root, text="Display")
    display_frame.grid(row=0, column=0, sticky="ew", **pad)

    ttk.Label(display_frame, text="Orientation:").grid(row=0, column=0, sticky="w", **pad)
    orientation_var = tk.StringVar(value=CONFIG["display"]["orientation"].strip().lower())
    ttk.Combobox(
        display_frame, textvariable=orientation_var, state="readonly",
        values=list(ORIENTATIONS.keys()), width=16,
    ).grid(row=0, column=1, sticky="w", **pad)

    ttk.Label(display_frame, text="Brightness:").grid(row=1, column=0, sticky="w", **pad)
    brightness_var = tk.IntVar(value=CONFIG["display"].getint("brightness"))
    tk.Scale(
        display_frame, from_=0, to=100, orient="horizontal", length=220,
        variable=brightness_var,
    ).grid(row=1, column=1, sticky="w", **pad)

    ttk.Label(display_frame, text="Mic panel:").grid(row=2, column=0, sticky="w", **pad)
    label_to_mic_panel = {v: k for k, v in MIC_PANEL_LABELS.items()}
    mic_panel_var = tk.StringVar(value=MIC_PANEL_LABELS.get(MIC_PANEL, MIC_PANEL_LABELS["mic"]))
    ttk.Combobox(
        display_frame, textvariable=mic_panel_var, state="readonly",
        values=list(MIC_PANEL_LABELS.values()), width=16,
    ).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(
        display_frame, text="(shows next to Dictation below when that's on)",
        foreground="#888888",
    ).grid(row=3, column=0, columnspan=2, sticky="w", padx=8)

    dictation_frame = ttk.LabelFrame(root, text="Dictation")
    dictation_frame.grid(row=1, column=0, sticky="ew", **pad)

    existing_status_file = CONFIG["dictation"]["status_file"].strip()
    dictation_enabled_var = tk.BooleanVar(value=bool(existing_status_file))
    status_file_var = tk.StringVar(value=existing_status_file)
    status_entry = ttk.Entry(dictation_frame, textvariable=status_file_var, width=40)

    def _toggle_status_entry():
        status_entry.configure(state="normal" if dictation_enabled_var.get() else "disabled")

    ttk.Checkbutton(
        dictation_frame, text="Show dictation status", variable=dictation_enabled_var,
        command=_toggle_status_entry,
    ).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
    status_entry.grid(row=1, column=0, sticky="w", **pad)

    def _browse():
        path = filedialog.askopenfilename(
            title="Select dictation status file",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if path:
            status_file_var.set(path)

    ttk.Button(dictation_frame, text="Browse...", command=_browse).grid(row=1, column=1, sticky="w", **pad)
    _toggle_status_entry()

    thresholds_frame = ttk.LabelFrame(root, text="Colour thresholds")
    thresholds_frame.grid(row=2, column=0, sticky="ew", **pad)
    ttk.Label(thresholds_frame, text="Warn", anchor="center").grid(row=0, column=1, **pad)
    ttk.Label(thresholds_frame, text="Bad", anchor="center").grid(row=0, column=2, **pad)

    threshold_vars = {}
    for row, (name, label, max_val) in enumerate(THRESHOLD_FIELDS, start=1):
        ttk.Label(thresholds_frame, text=f"{label}:").grid(row=row, column=0, sticky="w", **pad)
        warn_var = tk.DoubleVar(value=CONFIG["thresholds"].getfloat(f"{name}_warn"))
        bad_var = tk.DoubleVar(value=CONFIG["thresholds"].getfloat(f"{name}_bad"))
        threshold_vars[name] = (warn_var, bad_var)
        tk.Scale(thresholds_frame, from_=0, to=max_val, orient="horizontal", length=140,
                 variable=warn_var).grid(row=row, column=1, **pad)
        tk.Scale(thresholds_frame, from_=0, to=max_val, orient="horizontal", length=140,
                 variable=bad_var).grid(row=row, column=2, **pad)

    button_row = ttk.Frame(root)
    button_row.grid(row=3, column=0, sticky="e", **pad)

    def _save():
        if dictation_enabled_var.get() and not status_file_var.get().strip():
            messagebox.showerror(
                "Missing dictation status file",
                "\"Show dictation status\" is checked but no status file is set. "
                "Either choose a file or uncheck it.",
                parent=root,
            )
            return

        for name, label, _ in THRESHOLD_FIELDS:
            warn_var, bad_var = threshold_vars[name]
            if warn_var.get() > bad_var.get():
                messagebox.showerror(
                    "Invalid thresholds",
                    f"{label}: Warn ({warn_var.get():.0f}) can't be higher than Bad ({bad_var.get():.0f}).",
                    parent=root,
                )
                return

        values = {
            ("display", "orientation"): orientation_var.get(),
            ("display", "brightness"): str(brightness_var.get()),
            ("display", "mic_panel"): label_to_mic_panel[mic_panel_var.get()],
            ("dictation", "status_file"):
                status_file_var.get().strip() if dictation_enabled_var.get() else "",
        }
        for name, _, _ in THRESHOLD_FIELDS:
            warn_var, bad_var = threshold_vars[name]
            values[("thresholds", f"{name}_warn")] = f"{warn_var.get():.0f}"
            values[("thresholds", f"{name}_bad")] = f"{bad_var.get():.0f}"

        save_config_values(values)
        reload_config()
        _notify(icon, "Settings saved and applied.")
        root.destroy()

    ttk.Button(button_row, text="Cancel", command=root.destroy).pack(side="right", padx=4)
    ttk.Button(button_row, text="Save & Apply", command=_save).pack(side="right", padx=4)

    root.lift()
    root.attributes("-topmost", True)
    root.after_idle(root.attributes, "-topmost", False)
    root.mainloop()


def main():
    if "--install-autostart" in sys.argv:
        install_autostart()
        return
    if "--uninstall-autostart" in sys.argv:
        uninstall_autostart()
        return

    global _mutex_handle, _render_thread
    _mutex_handle, already_running = _acquire_single_instance_lock()
    if already_running:
        print("Another instance already holds the lock, exiting.")
        return

    warn_if_cpu_temp_unavailable()

    if getattr(sys, "frozen", False):
        hide_console_window()

    _render_thread = threading.Thread(target=panel_loop, name="panel-render", daemon=True)
    _render_thread.start()

    icon = pystray.Icon(
        "lcd_status_panel",
        icon=_build_tray_image(),
        title="LCD Status Panel",
        menu=pystray.Menu(
            pystray.MenuItem("LCD Status Panel", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings...", _on_open_settings),
            pystray.MenuItem("Open Config...", _on_open_config),
            pystray.MenuItem("Reload Config", _on_reload_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Panel", _on_restart_panel),
            pystray.MenuItem("Exit", _on_exit),
        ),
    )
    icon.run()  # blocks the main thread until _on_exit/_on_restart_panel calls icon.stop()

    _stop_event.set()
    _render_thread.join(timeout=5)


if __name__ == "__main__":
    main()
