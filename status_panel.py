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
import sys
import time
from ctypes import POINTER, cast

import GPUtil
import psutil
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
POLL_INTERVAL = 1.0

ORIENTATIONS = {
    "landscape": Orientation.LANDSCAPE,
    "landscape_180": Orientation.REVERSE_LANDSCAPE,
    "portrait": Orientation.PORTRAIT,
    "portrait_180": Orientation.REVERSE_PORTRAIT,
}

_DEFAULTS = {
    "display": {"orientation": "landscape", "brightness": "80"},
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


CONFIG = load_config()
ORIENTATION = ORIENTATIONS.get(
    CONFIG["display"]["orientation"].strip().lower(), Orientation.LANDSCAPE
)
BRIGHTNESS = CONFIG["display"].getint("brightness")
DICTATE_STATUS_FILE = CONFIG["dictation"]["status_file"].strip()
SHOW_DICTATION = bool(DICTATE_STATUS_FILE)


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
_IS_ADMIN = ctypes.windll.shell32.IsUserAnAdmin() != 0
_lhm_cpu = None
if _IS_ADMIN:
    try:
        from library.sensors.sensors_librehardwaremonitor import Cpu as _lhm_cpu
    except Exception as e:
        print(f"LibreHardwareMonitor unavailable, CPU temp will show N/A: {e}")
        _lhm_cpu = None
else:
    print("Not running elevated - CPU temp will show N/A.")
    print("  Run 'LcdPanel.exe --install-autostart' to start it elevated at login.")


def _pawnio_installed():
    """PawnIO is a kernel driver; LibreHardwareMonitor needs it for real CPU
    package temperature. Without it LHM silently reports 0 rather than failing,
    which reads as a hardware fault - so check and say so explicitly."""
    try:
        import subprocess
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


def render_landscape(draw, width, height, dictation_state, mic_muted, device_name, tiles):
    top_h = int(height * 0.34)
    mid_x = width // 2

    if SHOW_DICTATION:
        # Dictation and mic side by side, split down the middle.
        draw.line((mid_x, 8, mid_x, top_h - 6), fill=COLOR_DIVIDER, width=2)
        draw_dictation_block(draw, mid_x // 2, top_h // 2, dictation_state)
        draw_mic_block(draw, mid_x + mid_x // 2, top_h // 2, mic_muted, device_name, mid_x * 0.55)
    else:
        # Mic alone spans the whole band. The wider name allowance means the
        # icon has to move out with it, or a long device name grows back
        # underneath the icon.
        draw_mic_block(draw, mid_x, top_h // 2, mic_muted, device_name,
                        width * 0.5, icon_dx=-140, text_dx=40)

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
        draw_mic_block(draw, mid_x, band_h + band_h // 2 + 6, mic_muted, device_name,
                        width * 0.62, icon_dx=-85, text_dx=30)
    else:
        draw_mic_block(draw, mid_x, band_h // 2 + 6, mic_muted, device_name,
                        width * 0.62, icon_dx=-85, text_dx=30)

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

    import subprocess
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

    import subprocess
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Could not remove the scheduled task:\n{result.stderr.strip()}")
        return
    print(f'Autostart removed - "{TASK_NAME}" will no longer start at login.')


def main():
    if "--install-autostart" in sys.argv:
        install_autostart()
        return
    if "--uninstall-autostart" in sys.argv:
        uninstall_autostart()
        return

    _mutex_handle, already_running = _acquire_single_instance_lock()
    if already_running:
        print("Another instance already holds the lock, exiting.")
        return

    warn_if_cpu_temp_unavailable()

    lcd = LcdCommRevA(com_port="AUTO")
    lcd.Reset()
    lcd.InitializeComm()
    lcd.Clear()
    lcd.ScreenOn()
    lcd.SetBrightness(BRIGHTNESS)
    lcd.SetOrientation(ORIENTATION)
    width, height = lcd.get_width(), lcd.get_height()

    last_key = None
    while True:
        dictation_state = read_dictation_state() if SHOW_DICTATION else "offline"
        mic_muted = read_mic_muted()
        device_name = read_input_device_name()
        stats = read_system_stats()
        key = (dictation_state, mic_muted, device_name, stats)
        if key != last_key:
            lcd.DisplayPILImage(render_frame(width, height, dictation_state, mic_muted, device_name, *stats))
            last_key = key
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
