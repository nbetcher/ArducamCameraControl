# coding=utf-8
"""Direct v4l2 ioctl probing and optional picamera2/libcamera introspection.

Uses only the Python standard library (``fcntl``, ``struct``, ``os``) for v4l2
access so there is zero dependency on external CLI tools.

``picamera2`` is used **once at startup** — before the webcam streamer locks
the camera — to capture libcamera control metadata (ranges, defaults, names).
These are cached as read-only introspection data.  All **runtime** reads and
writes go through v4l2 ioctls, which work even while a streamer holds the
camera.
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

_logger = logging.getLogger(__name__)

# ── V4L2 ioctl plumbing ────────────────────────────────────────────


def _ioc(direction: int, ioc_type: int, nr: int, size: int) -> int:
    """Compute ioctl request code (works on all Linux architectures)."""
    return (direction << 30) | (size << 16) | (ioc_type << 8) | nr


_IOC_READ = 2
_IOC_WRITE = 1
_IOWR = _IOC_READ | _IOC_WRITE  # 3
_V = ord("V")  # 0x56

# struct v4l2_queryctrl  (68 bytes: II32siiiiIII)
_QUERYCTRL_FMT = "=II32siiiiIII"
_QUERYCTRL_SIZE = struct.calcsize(_QUERYCTRL_FMT)  # 68
VIDIOC_QUERYCTRL = _ioc(_IOWR, _V, 36, _QUERYCTRL_SIZE)

# struct v4l2_querymenu  (44 bytes: II32sI)
_QUERYMENU_FMT = "=II32sI"
_QUERYMENU_SIZE = struct.calcsize(_QUERYMENU_FMT)  # 44
VIDIOC_QUERYMENU = _ioc(_IOWR, _V, 37, _QUERYMENU_SIZE)

# struct v4l2_control  (8 bytes: Ii)
_CONTROL_FMT = "=Ii"
_CONTROL_SIZE = struct.calcsize(_CONTROL_FMT)  # 8
VIDIOC_G_CTRL = _ioc(_IOWR, _V, 27, _CONTROL_SIZE)
VIDIOC_S_CTRL = _ioc(_IOWR, _V, 28, _CONTROL_SIZE)

# Iterate to the next available control
V4L2_CTRL_FLAG_NEXT_CTRL = 0x80000000

# ── V4L2 control types & flags ─────────────────────────────────────


class V4L2CtrlType(IntEnum):
    INTEGER = 1
    BOOLEAN = 2
    MENU = 3
    BUTTON = 4
    INTEGER64 = 5
    CTRL_CLASS = 6
    STRING = 7
    BITMASK = 8
    INTEGER_MENU = 9


_CTRL_TYPE_NAMES: dict[int, str] = {
    V4L2CtrlType.INTEGER: "integer",
    V4L2CtrlType.BOOLEAN: "boolean",
    V4L2CtrlType.MENU: "menu",
    V4L2CtrlType.BUTTON: "button",
    V4L2CtrlType.INTEGER64: "integer",
    V4L2CtrlType.STRING: "string",
    V4L2CtrlType.BITMASK: "bitmask",
    V4L2CtrlType.INTEGER_MENU: "integer_menu",
}

_SKIP_TYPES: set[int] = {V4L2CtrlType.CTRL_CLASS, V4L2CtrlType.STRING}

V4L2_CTRL_FLAG_DISABLED = 0x0001
V4L2_CTRL_FLAG_GRABBED = 0x0002
V4L2_CTRL_FLAG_READ_ONLY = 0x0004
V4L2_CTRL_FLAG_INACTIVE = 0x0010
V4L2_CTRL_FLAG_WRITE_ONLY = 0x0040

# ── Data transfer object ───────────────────────────────────────────


@dataclass
class V4L2Control:
    """A single v4l2 control with full metadata."""

    id: int
    name: str
    type: str
    minimum: int
    maximum: int
    step: int
    default: int
    value: int
    flags: int
    device: str
    read_only: bool = False
    inactive: bool = False
    menu_items: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "default": self.default,
            "value": self.value,
            "read_only": self.read_only,
            "inactive": self.inactive,
            "device": self.device,
        }
        if self.menu_items:
            d["menu_items"] = {str(k): v for k, v in self.menu_items.items()}
        return d


# ── Low-level ioctl helpers ─────────────────────────────────────────


def _query_control(fd: int, ctrl_id: int) -> tuple | None:
    buf = bytearray(_QUERYCTRL_SIZE)
    struct.pack_into("=I", buf, 0, ctrl_id)
    try:
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
    except OSError:
        return None
    return struct.unpack(_QUERYCTRL_FMT, buf)


def _query_menu_items(
    fd: int, ctrl_id: int, minimum: int, maximum: int, ctrl_type: int
) -> dict[int, str]:
    items: dict[int, str] = {}
    for idx in range(minimum, maximum + 1):
        buf = bytearray(_QUERYMENU_SIZE)
        struct.pack_into("=II", buf, 0, ctrl_id, idx)
        try:
            fcntl.ioctl(fd, VIDIOC_QUERYMENU, buf)
        except OSError:
            continue
        _, _, payload, _ = struct.unpack(_QUERYMENU_FMT, buf)
        if ctrl_type == V4L2CtrlType.INTEGER_MENU:
            items[idx] = str(struct.unpack_from("=q", payload, 0)[0])
        else:
            items[idx] = payload.split(b"\x00", 1)[0].decode(
                "utf-8", errors="replace"
            )
    return items


def _get_control_value(fd: int, ctrl_id: int) -> int | None:
    buf = bytearray(_CONTROL_SIZE)
    struct.pack_into("=Ii", buf, 0, ctrl_id, 0)
    try:
        fcntl.ioctl(fd, VIDIOC_G_CTRL, buf)
    except OSError:
        return None
    _, value = struct.unpack(_CONTROL_FMT, buf)
    return value


# ── Public API — v4l2 ──────────────────────────────────────────────


def set_control_value(device: str, ctrl_id: int, value: int) -> bool:
    """Set a v4l2 control on *device*.  Returns ``True`` on success."""
    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return False
    try:
        buf = bytearray(struct.pack(_CONTROL_FMT, ctrl_id, value))
        fcntl.ioctl(fd, VIDIOC_S_CTRL, buf)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def get_control_value(device: str, ctrl_id: int) -> int | None:
    """Read the current value of a v4l2 control on *device*."""
    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return None
    try:
        return _get_control_value(fd, ctrl_id)
    finally:
        os.close(fd)


def probe_device_controls(device: str) -> list[V4L2Control]:
    """Enumerate all v4l2 controls on a single device via ioctl."""
    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return []

    controls: list[V4L2Control] = []
    ctrl_id = V4L2_CTRL_FLAG_NEXT_CTRL

    try:
        while True:
            result = _query_control(fd, ctrl_id)
            if result is None:
                break

            (
                qc_id,
                qc_type,
                name_bytes,
                qc_min,
                qc_max,
                qc_step,
                qc_default,
                qc_flags,
                _,
                _,
            ) = result

            ctrl_id = qc_id | V4L2_CTRL_FLAG_NEXT_CTRL

            if qc_flags & V4L2_CTRL_FLAG_DISABLED:
                continue
            if qc_type in _SKIP_TYPES:
                continue

            type_str = _CTRL_TYPE_NAMES.get(qc_type)
            if type_str is None:
                continue

            name = (
                name_bytes.split(b"\x00", 1)[0]
                .decode("utf-8", errors="replace")
                .strip()
            )

            current = _get_control_value(fd, qc_id)
            if current is None:
                current = qc_default

            menu_items: dict[int, str] = {}
            if qc_type in (V4L2CtrlType.MENU, V4L2CtrlType.INTEGER_MENU):
                menu_items = _query_menu_items(fd, qc_id, qc_min, qc_max, qc_type)

            controls.append(
                V4L2Control(
                    id=qc_id,
                    name=name,
                    type=type_str,
                    minimum=qc_min,
                    maximum=qc_max,
                    step=qc_step if qc_step > 0 else 1,
                    default=qc_default,
                    value=current,
                    flags=qc_flags,
                    device=device,
                    read_only=bool(qc_flags & V4L2_CTRL_FLAG_READ_ONLY),
                    inactive=bool(qc_flags & V4L2_CTRL_FLAG_INACTIVE),
                    menu_items=menu_items,
                )
            )
    finally:
        os.close(fd)

    return controls


def probe_all_video_devices() -> list[V4L2Control]:
    """Probe every ``/dev/video*`` device and return deduplicated controls.

    When the same control ID appears on multiple devices the first device
    that exposes it wins (devices are probed in sorted order).
    """
    seen_ids: set[int] = set()
    all_controls: list[V4L2Control] = []

    for device in sorted(glob.glob("/dev/video*")):
        for ctrl in probe_device_controls(device):
            if ctrl.id not in seen_ids:
                seen_ids.add(ctrl.id)
                all_controls.append(ctrl)

    return all_controls


# ── Optional libcamera / picamera2 introspection ────────────────────


def probe_libcamera_controls() -> list[dict[str, Any]]:
    """One-shot probe via ``picamera2`` — intended for early startup only.

    This function **opens and closes** the camera via libcamera.  It must be
    called *before* any webcam streamer acquires the camera (i.e. during
    OctoPrint's ``on_startup`` hook, which fires before ``webcamd.service``
    has started on a stock OctoPi installation).

    Returns a list of control dicts with ``name``, ``type``, ``min``,
    ``max``, ``default`` and ``source``.  Returns an empty list if
    picamera2 is unavailable or the camera is already in use.

    The data is meant as **introspection-only** metadata.  All runtime
    reads and writes go through the v4l2 interface.
    """
    try:
        from picamera2 import Picamera2  # type: ignore[import-untyped]
    except ImportError:
        _logger.debug("picamera2 not available – skipping libcamera probe")
        return []

    controls_list: list[dict[str, Any]] = []
    try:
        picam2 = Picamera2()
        try:
            # camera_controls: dict of name → (min, max, default)
            for name, (lo, hi, default) in picam2.camera_controls.items():
                ctrl_type = "integer"
                if isinstance(default, bool):
                    ctrl_type = "boolean"
                elif isinstance(default, float):
                    ctrl_type = "float"

                controls_list.append(
                    {
                        "name": name,
                        "type": ctrl_type,
                        "min": _serialise_value(lo),
                        "max": _serialise_value(hi),
                        "default": _serialise_value(default),
                        "source": "libcamera",
                    }
                )

            # Log camera properties for diagnostic purposes
            try:
                props = picam2.camera_properties
                if props:
                    _logger.info(
                        "libcamera camera properties: %s",
                        {k: _serialise_value(v) for k, v in props.items()},
                    )
            except Exception:
                pass

        finally:
            picam2.close()
    except Exception:
        _logger.debug(
            "picamera2 probe failed (camera likely already in use) – skipping",
            exc_info=True,
        )

    return controls_list


def _serialise_value(v: Any) -> Any:
    """Make a libcamera value JSON-safe (numpy arrays, tuples, etc.)."""
    try:
        import numpy as np  # type: ignore[import-untyped]

        if isinstance(v, np.ndarray):
            return v.tolist()
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return float(v)
    except ImportError:
        pass

    if isinstance(v, tuple):
        return list(v)

    return v
