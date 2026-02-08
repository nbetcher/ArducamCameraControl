# Arducam Camera Control

An OctoPrint plugin to control Arducam PTZ and motorized cameras.

Controls are **discovered dynamically** — the plugin probes the camera
at startup and only shows controls that your hardware actually supports.

![screenshot](extras/assets/img/plugins/ArducamCameraControl/ArducamCameraControl.png)

## Features

- **I2C PTZ controls** — Pan, tilt, zoom, focus and IR-cut filter for
  Arducam cameras with a motor controller on address `0x0C`.
- **Dynamic v4l2 controls** — Brightness, contrast, saturation,
  white balance, exposure, sharpness, flip/mirror and any other control
  your camera's v4l2 driver exposes.  Sliders, toggles, dropdowns and
  buttons are generated automatically.
- **Optional libcamera introspection** — If `picamera2` is installed the
  plugin captures control metadata (ranges, defaults) via libcamera
  *before* the webcam streamer starts.  This gives maximum-precision
  feature-set data without interfering with the video feed.
- **Refresh button** — Re-probe v4l2 controls at any time without
  restarting OctoPrint (useful if you reconnect a USB camera).
- **Thread-safe I2C** — A lock protects all bus operations so concurrent
  requests cannot corrupt I2C transactions.
- **Per-control validation** — The backend validates every value against
  the control's minimum, maximum and step before sending it to the
  driver.

## Requirements

| Component        | Version / Notes                              |
| ---------------- | -------------------------------------------- |
| OctoPrint        | 1.9.0+                                       |
| Python           | 3.7+                                         |
| Raspberry Pi     | 3B+ or newer recommended                     |
| OS               | Raspberry Pi OS Bookworm (or later)          |
| `smbus2` (pip)   | Installed automatically with the plugin       |
| `picamera2` (optional) | For libcamera introspection at startup  |

## Hardware Setup

Follow the manufacturer's instructions for physically connecting the camera.

### Enable I2C

I2C is required for PTZ motor control and is **not** enabled by default.

1. **Edit `/boot/config.txt`** (or `/boot/firmware/config.txt` on Bookworm):

   ```
   dtparam=i2c_vc=on
   dtparam=i2c_arm=on
   ```

2. **Enable the I2C kernel module** with `raspi-config`:

   ```bash
   sudo raspi-config
   ```

   → *3 Interfacing Options* → *P5 I2C* → *Yes*

3. Reboot.

### Optional: Install picamera2

If you want the plugin to capture libcamera control metadata at startup:

```bash
sudo apt install -y python3-picamera2
```

The plugin works without it — picamera2 only provides supplementary
introspection data (richer control names, float-precision ranges).
All runtime control changes use v4l2.

## Plugin Installation

Install from the OctoPrint Plugin Manager:

> **Plugin Manager → Get More → Search "ArducamCameraControl"**

Or install manually:

```bash
pip install https://github.com/arducam/ArducamCameraControl/archive/main.zip
```

After restarting OctoPrint the camera controls appear in the **Control** tab.

## Architecture

```
  OctoPrint startup
  ──────────────────────────────────────────────────────────
  on_startup()        ← picamera2 probe (camera still free)
       │                 captures libcamera control metadata
       ▼                 closes camera immediately
  on_after_startup()  ← I2C camera detection
       │                 v4l2 ioctl probing (works alongside
       │                 the streamer)
       ▼
  Server ready        ← webcamd / camera-streamer starts
  ──────────────────────────────────────────────────────────
  Runtime             ← all control reads / writes use v4l2
                        ioctls (concurrent access OK)
```

### Why v4l2 for runtime?

On Raspberry Pi OS Bookworm, libcamera's v4l2 compatibility layer
exposes all camera controls through standard `/dev/video*` devices.
v4l2 ioctls work **while the streamer is running** — they do not need
exclusive camera access.  This lets the plugin adjust brightness,
contrast, exposure, etc. without interrupting the video feed.

## API

### GET endpoints (`on_api_get`)

| `command`          | Parameters     | Returns                        |
| ------------------ | -------------- | ------------------------------ |
| `get_capabilities` | —              | Full capabilities payload      |
| `get_focus`        | —              | `{value: <int>}`               |
| `get_id`           | —              | Camera type ID string          |
| `get_v4l2`         | `control_id`   | `{control_id, value}`          |

### POST commands (`on_api_command`)

| Command            | Payload                 | Description              |
| ------------------ | ----------------------- | ------------------------ |
| `set_v4l2`         | `{control_id, value}`   | Set a v4l2 control       |
| `refresh_controls` | `{}`                    | Re-probe v4l2 controls   |
| `ptz_tilt`         | `{value}`               | I2C tilt command         |
| `ptz_pan`          | `{value}`               | I2C pan command          |
| `ptz_zoom`         | `{value}`               | I2C zoom command         |
| `ptz_focus`        | `{value}`               | I2C focus command        |
| `ptz_ircut`        | `{value}`               | I2C IR-cut toggle        |

All endpoints require the `PLUGIN_ARDUCAMCAMERACONTROL_ADMIN` permission.
Write commands are rate-limited to one every 100 ms.

## License

AGPLv3
