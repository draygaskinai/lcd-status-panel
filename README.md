# LCD Status Panel

A replacement for the vendor software that ships with 3.5" USB-C info panels
(sold as Turing Smart Screen, TURZX, XuanFang, and others — same hardware).

Shows your **microphone mute state** and **live system stats** (CPU, CPU temp,
RAM, GPU, GPU temp, VRAM), colour-coded green/yellow/red against thresholds you
choose. Optionally shows dictation status if you run a speech-to-text tool that
can write a status file.

**Windows only.**

---

## Will it work with my panel?

This works with one specific panel family. To check yours:

1. Plug the panel in.
2. Open **Device Manager** → expand **Ports (COM & LPT)**.
3. Look for **USB Serial Device (COM*n*)**. Right-click → **Properties** →
   **Details** tab → set the dropdown to **Hardware Ids**.

You want to see **`VID_1A86`** and **`PID_5722`**. (The panel also reports the
serial number `USB35INCHIPSV2`.)

If you see different IDs, this won't drive your panel — it isn't a generic tool,
and it will simply fail to find the display rather than doing anything harmful.

---

## Setup

### 1. Stop the vendor software first

**This matters.** The panel accepts one connection at a time. If the original
vendor app (often called `UsbMonitor.exe`, showing as `USBLCD` in the system
tray) is running, the two will fight over the port and neither will work
reliably.

- Quit it from the system tray, **and**
- stop it starting with Windows. It usually installs itself as a scheduled task
  — from an **administrator** Command Prompt:

  ```
  schtasks /Change /TN "UsbMonitor" /Disable
  ```

  If that reports the task doesn't exist, check **Task Manager → Startup apps**
  instead.

### 2. Run it

Unzip anywhere and double-click **`LcdPanel.exe`**. The panel should light up
within a few seconds.

To start it automatically at every login, run this once (it will ask for
administrator rights, which is what lets it read CPU temperature):

```
LcdPanel.exe --install-autostart
```

To undo that later:

```
LcdPanel.exe --uninstall-autostart
```

---

## Is this safe to run?

**`LcdPanel.exe` itself is not code-signed.** Windows will show a blue
**"Windows protected your PC"** SmartScreen screen the first time you run it.
This is expected — it isn't a sign anything is wrong, it's what Windows shows
for *any* unsigned executable, however trustworthy. To continue:

> **More info** → **Run anyway**

If you'd rather not take that on faith, here's how to check for yourself
instead of trusting the file blind:

- **Read the source.** This is a small, human-readable Python program —
  [`status_panel.py`](status_panel.py) is the whole thing, no obfuscation,
  no network calls except what's needed to talk to the display. It's included
  in this download, and it's also the same file that gets built into the
  `.exe` — see **Building it yourself**, below.
- **Verify the download hasn't been tampered with.** Compare the SHA-256
  hash of the zip you downloaded against the one published on the
  [Releases page](https://github.com/draygaskinai/lcd-status-panel/releases).
  In PowerShell:
  ```
  Get-FileHash LcdPanel.zip -Algorithm SHA256
  ```
- **Build it yourself, rather than trusting the prebuilt zip.** See
  **Building it yourself** below — if the .exe is built on your own machine
  from source you read, there's nothing to trust but your own compiler.

**The one component that touches the Windows kernel — the PawnIO driver — is
properly code-signed** (by its author, not installed or run automatically by
this app; see the next section). `LibreHardwareMonitorLib.dll`, like
`LcdPanel.exe`, is unsigned — it's a well-known open-source hardware-sensor
library, vendored in unmodified from its own project, not something built by
this project.

### Building it yourself

If you have Python 3.12 installed:

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pyinstaller.exe lcd-panel.spec
```

The result lands in `dist\LcdPanel\`. This is exactly what the GitHub Actions
workflow in this repo does on every tagged release — the built artifact you'd
download is the output of that same public, inspectable build script, not
something assembled by hand.

---

## CPU temperature needs one extra step

Every reading except **CPU temp** works out of the box.

CPU package temperature has to be read through a small kernel driver called
**PawnIO** (open-source, MIT-licensed). It's included in this download but is
**not installed automatically** — installing a kernel driver should be your
decision, not a side effect of running an app.

Without it, everything still works and `CPU TEMP` just reads `N/A`.

To enable it, from an **administrator** Command Prompt, inside this folder:

```
external\PawnIO\PawnIO_setup.exe -install -silent
```

Then restart the panel. (The app prints this same reminder if it detects the
driver is missing.)

> Note: CPU temp also requires the panel to be running **elevated**, which
> `--install-autostart` handles for you. Launched by double-click without
> admin rights, it will read `N/A` even with PawnIO installed.

---

## Configuration

Edit **`config.ini`** next to the .exe in any text editor, then restart the app.

### Orientation

```ini
[display]
orientation = landscape
```

One of `landscape`, `landscape_180`, `portrait`, `portrait_180`. The `180`
variants are the same layout rotated, for when the USB cable needs to exit the
other side. Portrait uses a taller layout with the stats in two columns.

### Brightness

```ini
brightness = 80
```

`0`–`100`.

### Colour thresholds

Values at or above `_warn` turn yellow; at or above `_bad` turn red.
Percentages are 0–100, temperatures in Celsius.

```ini
[thresholds]
cpu_temp_warn = 75
cpu_temp_bad  = 85
gpu_temp_warn = 50
gpu_temp_bad  = 60
ram_warn      = 65
ram_bad       = 80
```

Sensible defaults are shipped; adjust to taste (GPU temps in particular vary a
lot between cards).

### Dictation panel (optional, off by default)

Most people should leave this blank. If you run a speech-to-text tool that can
write a small JSON status file, point at it and the panel gains a dictation
block:

```ini
[dictation]
status_file = C:\path\to\status.json
```

The file must contain:

```json
{"state": "listening", "ts": 1786626151.5}
```

`state` is one of `listening`, `transcribing`, `idle`, `offline`. `ts` is a Unix
timestamp — rewrite the file at least every few seconds, because a timestamp
older than 10 seconds is treated as "the tool isn't running" and displays
`OFFLINE`.

---

## Troubleshooting

**Nothing appears on the panel.**
The vendor app is almost certainly still holding the port — see step 1. Confirm
in Device Manager that the panel shows up as a COM port at all.

**CPU TEMP shows N/A.**
Either PawnIO isn't installed, or the app isn't running elevated. See above.

**CPU TEMP shows 0°C.**
PawnIO isn't installed. LibreHardwareMonitor reports zero rather than failing,
which looks like a hardware fault but isn't.

**The panel is upside down.**
Switch `orientation` between `landscape` and `landscape_180` (or the portrait
pair).

**It stopped after a Windows update / reboot.**
Re-run `LcdPanel.exe --install-autostart`.

---

## Licence

**GPL-3.0.** This is built on
[turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python)
by Matthieu Houdebine and contributors, which is GPL-3.0, so this app is too.
The full licence text is in `LICENSE`.

Practically, that means: if you pass this program along to anyone, you must also
make the corresponding source code available to them. The upstream project is
linked above; the panel-specific source is `status_panel.py`.

PawnIO (bundled in `external/PawnIO`) is MIT-licensed and carries its own
`LICENSE` file.
