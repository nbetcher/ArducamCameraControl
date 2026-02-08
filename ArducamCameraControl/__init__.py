# coding=utf-8
"""OctoPrint plugin for controlling Arducam cameras over I2C and v4l2.

PTZ (pan / tilt / zoom / focus / IR-cut) is driven over I2C via the Arducam
motor controller at address 0x0C.  Image-processing controls (brightness,
contrast, saturation, white-balance, flip, exposure …) are discovered at
runtime by probing ``/dev/video*`` devices with direct v4l2 ioctls.

At startup — **before** the webcam streamer acquires the camera — the plugin
optionally probes ``picamera2`` to capture libcamera control metadata (ranges,
defaults, supported controls).  This data is cached as read-only introspection
metadata.  All **runtime** control changes go through v4l2 ioctls, which work
even while a streamer (``camera-streamer``, ``mjpg-streamer``) holds the
camera.

Only controls that the camera actually supports are exposed to the frontend;
the UI is built dynamically from the capability data returned by the
``get_capabilities`` API endpoint.
"""

from __future__ import annotations

import glob
import threading
import time
from typing import Any

import flask
import octoprint.plugin
from octoprint.access.permissions import ADMIN_GROUP, Permissions

from . import camera_controls

# ── I2C constants ───────────────────────────────────────────────────

_I2C_ADDR = 0x0C

_REG_ZOOM = 0x00
_REG_FOCUS = 0x01
_REG_STATUS = 0x04
_REG_PAN = 0x05
_REG_TILT = 0x06
_REG_IRCUT = 0x0C

_MAX_WRITE_RETRIES = 10
_API_RATE_LIMIT_SEC = 0.1
_LIBCAMERA_PROBE_TIMEOUT_SEC = 5.0

# ── Camera type enum ───────────────────────────────────────────────


class CameraType:
    PTZ = "ptz"
    MOTORIZED = "motorized"
    NONE = "none"


# ── Plugin ──────────────────────────────────────────────────────────


class ArducamCameraControlPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SimpleApiPlugin,
):
    # ── Initialisation ──────────────────────────────────────────────

    def __init__(self) -> None:
        super().__init__()
        self._bus: Any | None = None
        self._bus_number: int | None = None
        self._bus_lock = threading.Lock()
        self._camera_type: str = CameraType.NONE
        self._last_command_time: float = 0.0
        self._i2c_capabilities: set[str] = set()
        self._v4l2_controls: list[camera_controls.V4L2Control] = []
        self._v4l2_control_ids: set[int] = set()
        self._libcamera_controls: list[dict[str, Any]] = []
        self._libcamera_probe_status: str = "pending"

    # ── StartupPlugin ───────────────────────────────────────────────

    def on_startup(self, host: str, port: int) -> None:
        """Run *before* the server starts listening.

        On a stock OctoPi installation ``webcamd.service`` depends on
        ``octoprint.service``, so the camera is **not yet locked** by the
        streamer at this point.  We use this narrow window for the
        picamera2 introspection probe.

        The probe runs in a **daemon thread** with a hard timeout so that
        a hung libcamera stack can never block OctoPrint startup.
        """
        result: list[dict[str, Any]] = []
        error_holder: list[str] = []

        def _probe() -> None:
            try:
                result.extend(camera_controls.probe_libcamera_controls())
            except Exception as exc:  # noqa: BLE001
                error_holder.append(str(exc))

        probe_thread = threading.Thread(target=_probe, daemon=True)
        probe_thread.start()
        probe_thread.join(timeout=_LIBCAMERA_PROBE_TIMEOUT_SEC)

        if probe_thread.is_alive():
            self._libcamera_probe_status = "timeout"
            self._logger.warning(
                "libcamera probe timed out after %.1f s — skipping. "
                "The probe thread will be abandoned (daemon).",
                _LIBCAMERA_PROBE_TIMEOUT_SEC,
            )
        elif error_holder:
            self._libcamera_probe_status = "error"
            self._logger.warning(
                "libcamera probe failed: %s", error_holder[0]
            )
        elif result:
            self._libcamera_controls = result
            self._libcamera_probe_status = "ok"
            names = ", ".join(c["name"] for c in self._libcamera_controls)
            self._logger.info(
                "libcamera introspection captured %d controls: %s",
                len(self._libcamera_controls),
                names,
            )
        else:
            self._libcamera_probe_status = "skipped"
            self._logger.info(
                "libcamera introspection: no controls captured "
                "(picamera2 unavailable or camera already locked)"
            )

    def on_after_startup(self) -> None:
        """Run *after* the server is listening.

        By this point the webcam streamer may have acquired the camera.
        Everything here uses interfaces that co-exist with the streamer:
        I2C (separate bus entirely) and v4l2 ioctls (the kernel's v4l2
        compat layer allows concurrent control access).
        """
        # 1. Detect Arducam I2C camera
        self._camera_type, self._bus_number = self._detect_camera()
        self._i2c_capabilities = self._capabilities_for_type(self._camera_type)

        if self._camera_type != CameraType.NONE:
            try:
                import smbus2

                self._bus = smbus2.SMBus(self._bus_number)
            except Exception:
                self._logger.exception(
                    f"Failed to open I2C bus {self._bus_number}"
                )
                self._camera_type = CameraType.NONE
            else:
                self._logger.info(
                    f"Arducam I2C camera: type={self._camera_type}, "
                    f"bus={self._bus_number}"
                )
                focus_level: int = self._settings.get_int(["focus_level"])
                self._logger.info(f"Restoring focus to {focus_level}")
                self._ptz_focus(focus_level)
        else:
            self._logger.warning(
                "No Arducam I2C camera detected. PTZ controls disabled."
            )

        # 2. Probe v4l2 controls (works alongside any running camera streamer)
        self._probe_v4l2()

        # 3. Refine I2C capabilities against v4l2 evidence
        self._i2c_capabilities = self._refine_i2c_capabilities(
            self._i2c_capabilities, self._v4l2_controls
        )

    # ── ShutdownPlugin ──────────────────────────────────────────────

    def on_shutdown(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    # ── SimpleApiPlugin — GET (read-only queries) ───────────────────

    def is_api_protected(self) -> bool:
        return True

    def on_api_get(self, request: flask.Request) -> flask.Response:
        if not Permissions.PLUGIN_ARDUCAMCAMERACONTROL_ADMIN.can():
            return flask.make_response("Forbidden", 403)

        command: str | None = request.args.get("command")
        if command is None:
            return flask.make_response("Missing command", 400)

        if command == "get_capabilities":
            return flask.jsonify(self._build_capabilities_payload())

        if command == "get_focus":
            return flask.jsonify(
                {"value": self._settings.get_int(["focus_level"])}
            )

        if command == "get_id":
            return flask.make_response(self._camera_type_id(), 200)

        if command == "get_v4l2":
            return self._handle_get_v4l2(request)

        return flask.make_response("Unknown command", 400)

    # ── SimpleApiPlugin — POST (side-effect commands) ───────────────

    def get_api_commands(self) -> dict[str, list[str]]:
        return {
            "set_v4l2": ["control_id", "value"],
            "refresh_controls": [],
            "ptz_tilt": ["value"],
            "ptz_pan": ["value"],
            "ptz_zoom": ["value"],
            "ptz_focus": ["value"],
            "ptz_ircut": ["value"],
        }

    def on_api_command(self, command: str, data: dict) -> flask.Response:
        if not Permissions.PLUGIN_ARDUCAMCAMERACONTROL_ADMIN.can():
            return flask.make_response("Forbidden", 403)

        # ── Refresh controls (no rate limit) ────────────────────────

        if command == "refresh_controls":
            self._probe_v4l2()
            self._i2c_capabilities = self._refine_i2c_capabilities(
                self._capabilities_for_type(self._camera_type),
                self._v4l2_controls,
            )
            return flask.jsonify(self._build_capabilities_payload())

        # ── Rate-limit all write commands ───────────────────────────

        now = time.monotonic()
        if now - self._last_command_time < _API_RATE_LIMIT_SEC:
            return flask.make_response("Too Fast", 429)

        # ── set_v4l2 ───────────────────────────────────────────────

        if command == "set_v4l2":
            return self._handle_set_v4l2(data, now)

        # ── I2C PTZ commands ────────────────────────────────────────

        try:
            value = int(data.get("value", ""))
        except (TypeError, ValueError):
            return flask.make_response("Invalid or missing value", 400)

        i2c_dispatch: dict[str, tuple[str, Any]] = {
            "ptz_tilt": ("tilt", self._ptz_tilt),
            "ptz_pan": ("pan", self._ptz_pan),
            "ptz_zoom": ("zoom", self._ptz_zoom),
            "ptz_focus": ("focus", self._ptz_focus),
            "ptz_ircut": ("ircut", self._ptz_ircut),
        }

        entry = i2c_dispatch.get(command)
        if entry is None:
            return flask.make_response("Unknown command", 400)

        capability, handler = entry
        if capability not in self._i2c_capabilities:
            return flask.jsonify({"error": "Unsupported by this camera"}), 409

        self._last_command_time = now
        if not handler(value):
            return flask.jsonify({"error": f"{capability} command failed"}), 500
        return flask.make_response("ok", 200)

    # ── v4l2 get/set handlers ───────────────────────────────────────

    def _handle_get_v4l2(self, request: flask.Request) -> flask.Response:
        try:
            ctrl_id = int(request.args.get("control_id", ""))
        except (TypeError, ValueError):
            return flask.make_response("Invalid control_id", 400)

        ctrl = self._find_v4l2_control(ctrl_id)
        if ctrl is None:
            return flask.make_response("Unknown control", 404)

        current = camera_controls.get_control_value(ctrl.device, ctrl_id)
        if current is None:
            return flask.make_response("Read failed", 500)
        return flask.jsonify({"control_id": ctrl_id, "value": current})

    def _handle_set_v4l2(
        self, data: dict, now: float
    ) -> flask.Response:
        try:
            ctrl_id = int(data.get("control_id", ""))
            value = int(data.get("value", ""))
        except (TypeError, ValueError):
            return flask.make_response("Invalid control_id or value", 400)

        ctrl = self._find_v4l2_control(ctrl_id)
        if ctrl is None:
            return flask.make_response("Unknown control", 404)
        if ctrl.read_only:
            return flask.make_response("Control is read-only", 403)

        # ── Per-control range validation ────────────────────────────
        error = self._validate_control_value(ctrl, value)
        if error:
            return flask.jsonify({"error": error}), 400

        self._last_command_time = now
        ok = camera_controls.set_control_value(ctrl.device, ctrl_id, value)
        if not ok:
            return flask.jsonify({"error": "Failed to set control"}), 500

        # Read back the actual value the driver accepted
        actual = camera_controls.get_control_value(ctrl.device, ctrl_id)
        if actual is None:
            actual = value
        ctrl.value = actual
        return flask.jsonify({"control_id": ctrl_id, "value": actual})

    @staticmethod
    def _validate_control_value(
        ctrl: camera_controls.V4L2Control, value: int
    ) -> str | None:
        """Return an error string if *value* is out of range, else ``None``."""
        if value < ctrl.minimum:
            return (
                f"Value {value} is below minimum {ctrl.minimum} "
                f"for '{ctrl.name}'"
            )
        if value > ctrl.maximum:
            return (
                f"Value {value} exceeds maximum {ctrl.maximum} "
                f"for '{ctrl.name}'"
            )
        if ctrl.step > 1:
            offset = (value - ctrl.minimum) % ctrl.step
            if offset != 0:
                return (
                    f"Value {value} is not aligned to step {ctrl.step} "
                    f"(from minimum {ctrl.minimum}) for '{ctrl.name}'"
                )
        return None

    def _find_v4l2_control(
        self, ctrl_id: int
    ) -> camera_controls.V4L2Control | None:
        controls = self._v4l2_controls  # atomic snapshot under GIL
        for c in controls:
            if c.id == ctrl_id:
                return c
        return None

    # ── v4l2 re-probing helper ──────────────────────────────────────

    def _probe_v4l2(self) -> None:
        """(Re-)probe all v4l2 controls.  Safe to call at any time."""
        self._v4l2_controls = camera_controls.probe_all_video_devices()
        self._v4l2_control_ids = {c.id for c in self._v4l2_controls}

        if self._v4l2_controls:
            names = ", ".join(c.name for c in self._v4l2_controls)
            self._logger.info(f"v4l2 controls discovered: {names}")
        else:
            self._logger.info(
                "No v4l2 controls found on any /dev/video* device."
            )

    # ── Capabilities payload ────────────────────────────────────────

    def _build_capabilities_payload(self) -> dict[str, Any]:
        controls = self._v4l2_controls  # atomic snapshot under GIL
        return {
            "camera_type": self._camera_type,
            "i2c_capabilities": sorted(self._i2c_capabilities),
            "v4l2_controls": [c.to_dict() for c in controls],
            "libcamera_controls": self._libcamera_controls,
            "diagnostics": self._build_diagnostics(),
        }

    def _build_diagnostics(self) -> dict[str, Any]:
        """Return a diagnostic summary for the frontend status panel."""
        i2c_buses = sorted(glob.glob("/dev/i2c-*"))
        video_devices = sorted(glob.glob("/dev/video*"))

        return {
            "i2c_buses_found": len(i2c_buses),
            "i2c_bus_paths": i2c_buses,
            "video_devices_found": len(video_devices),
            "video_device_paths": video_devices,
            "camera_type": self._camera_type,
            "i2c_bus_number": self._bus_number,
            "i2c_capabilities_count": len(self._i2c_capabilities),
            "v4l2_controls_count": len(self._v4l2_controls),
            "libcamera_controls_count": len(self._libcamera_controls),
            "libcamera_probe_status": self._libcamera_probe_status,
        }

    # ── I2C camera detection ────────────────────────────────────────

    @staticmethod
    def _i2c_bus_numbers() -> list[int]:
        buses: list[int] = []
        for path in sorted(glob.glob("/dev/i2c-*")):
            try:
                buses.append(int(path.rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                continue
        return buses

    @staticmethod
    def _probe_bus_for_camera(bus_number: int) -> bool:
        try:
            import smbus2

            bus = smbus2.SMBus(bus_number)
            try:
                bus.read_byte(_I2C_ADDR)
                return True
            except OSError:
                return False
            finally:
                bus.close()
        except Exception:
            return False

    def _detect_camera(self) -> tuple[str, int | None]:
        if self._probe_bus_for_camera(1):
            return CameraType.PTZ, 1
        for bus_num in self._i2c_bus_numbers():
            if bus_num == 1:
                continue
            if self._probe_bus_for_camera(bus_num):
                return CameraType.MOTORIZED, bus_num
        return CameraType.NONE, None

    # ── I2C capability helpers ──────────────────────────────────────

    @staticmethod
    def _capabilities_for_type(camera_type: str) -> set[str]:
        if camera_type == CameraType.PTZ:
            return {"pan", "tilt", "zoom", "focus", "ircut"}
        if camera_type == CameraType.MOTORIZED:
            return {"focus"}
        return set()

    @staticmethod
    def _refine_i2c_capabilities(
        base: set[str],
        v4l2_ctrls: list[camera_controls.V4L2Control],
    ) -> set[str]:
        """If v4l2 probing found controls, cross-check focus/zoom existence."""
        if not v4l2_ctrls:
            return set(base)

        v4l2_names = {c.name.lower() for c in v4l2_ctrls}

        # Build a set of capabilities the v4l2 controls confirm
        confirmed: set[str] = set()
        focus_keywords = {"focus", "focus (absolute)", "focus_absolute"}
        zoom_keywords = {
            "zoom",
            "zoom (absolute)",
            "zoom_absolute",
            "zoom, absolute",
        }
        for name in v4l2_names:
            if name in focus_keywords:
                confirmed.add("focus")
            if name in zoom_keywords:
                confirmed.add("zoom")

        refined = set(base)
        for cap in ("focus", "zoom"):
            # Only prune an I2C capability when v4l2 probing *did* confirm
            # at least one of focus/zoom (``confirmed`` is non-empty) but
            # this specific capability was *not* among them.  When
            # ``confirmed`` is empty (no v4l2 evidence at all) we keep the
            # I2C-declared capabilities intact — they may be I2C-only.
            if cap in refined and confirmed and cap not in confirmed:
                refined.discard(cap)
        return refined

    # ── I2C read/write helpers (thread-safe) ────────────────────────

    def _i2c_write_block(self, register: int, data: list[int]) -> bool:
        if self._bus is None:
            self._send_error("I2C bus is not available")
            return False
        with self._bus_lock:
            for attempt in range(_MAX_WRITE_RETRIES):
                try:
                    self._bus.write_i2c_block_data(_I2C_ADDR, register, data)
                    return True
                except OSError:
                    if attempt == _MAX_WRITE_RETRIES - 1:
                        self._send_error(
                            "I2C bus failure — is camera plugged in?"
                        )
                        return False
        return False  # unreachable, keeps mypy happy

    def _i2c_write_byte(self, register: int, value: int) -> bool:
        if self._bus is None:
            self._send_error("I2C bus is not available")
            return False
        with self._bus_lock:
            for attempt in range(_MAX_WRITE_RETRIES):
                try:
                    self._bus.write_byte_data(_I2C_ADDR, register, value)
                    return True
                except OSError:
                    if attempt == _MAX_WRITE_RETRIES - 1:
                        self._send_error(
                            "I2C bus failure — is camera plugged in?"
                        )
                        return False
        return False

    def _is_camera_ready(self) -> bool:
        if self._bus is None:
            return False
        with self._bus_lock:
            try:
                state = self._bus.read_i2c_block_data(
                    _I2C_ADDR, _REG_STATUS, 2
                )
                return (state[1] & 0x01) == 0
            except OSError:
                return False

    def _send_error(self, message: str) -> None:
        self._plugin_manager.send_plugin_message(
            self._identifier, {"error": message}
        )

    @staticmethod
    def _value_to_bytes(value: int) -> list[int]:
        return [(value >> 8) & 0xFF, value & 0xFF]

    # ── PTZ I2C commands ────────────────────────────────────────────

    def _ptz_zoom(self, value: int) -> bool:
        if "zoom" not in self._i2c_capabilities:
            return False
        if not self._is_camera_ready():
            return False
        return self._i2c_write_block(_REG_ZOOM, self._value_to_bytes(value))

    def _ptz_focus(self, value: int) -> bool:
        if "focus" not in self._i2c_capabilities:
            return False
        if self._camera_type == CameraType.PTZ:
            if not self._is_camera_ready():
                return False
            if self._i2c_write_block(
                _REG_FOCUS, self._value_to_bytes(value)
            ):
                self._settings.set_int(["focus_level"], value)
                self._settings.save()
                return True
            return False
        elif self._camera_type == CameraType.MOTORIZED:
            # MOTORIZED VCM chips (e.g. DW9714) use a 2-byte I2C
            # write where the "register" and "data" bytes both
            # encode the DAC position — there is no named register.
            value = max(100, min(1000, value))
            encoded = (value << 4) & 0x3FF0
            data1 = (encoded >> 8) & 0x3F
            data2 = encoded & 0xF0
            if self._i2c_write_byte(data1, data2):
                self._settings.set_int(["focus_level"], value)
                self._settings.save()
                return True
            return False
        return False

    def _ptz_pan(self, value: int) -> bool:
        if "pan" not in self._i2c_capabilities:
            return False
        if not self._is_camera_ready():
            return False
        return self._i2c_write_block(_REG_PAN, self._value_to_bytes(value))

    def _ptz_tilt(self, value: int) -> bool:
        if "tilt" not in self._i2c_capabilities:
            return False
        if not self._is_camera_ready():
            return False
        return self._i2c_write_block(_REG_TILT, self._value_to_bytes(value))

    def _ptz_ircut(self, value: int) -> bool:
        if "ircut" not in self._i2c_capabilities:
            return False
        if not self._is_camera_ready():
            return False
        return self._i2c_write_block(_REG_IRCUT, self._value_to_bytes(value))

    # ── Legacy helper ───────────────────────────────────────────────

    def _camera_type_id(self) -> str:
        if self._camera_type == CameraType.PTZ:
            return "1"
        if self._camera_type == CameraType.MOTORIZED:
            return "0"
        return "2"

    # ── SettingsPlugin ──────────────────────────────────────────────

    def get_settings_defaults(self) -> dict[str, Any]:
        return {"focus_level": 512}

    # ── TemplatePlugin ──────────────────────────────────────────────

    def get_template_configs(self) -> list[dict[str, Any]]:
        return [{"type": "generic", "custom_bindings": False}]

    # ── AssetPlugin ─────────────────────────────────────────────────

    def get_assets(self) -> dict[str, list[str]]:
        return {
            "js": ["js/ArducamCameraControl.js"],
            "css": ["css/ArducamCameraControl.css"],
        }

    # ── Permissions ─────────────────────────────────────────────────

    def get_permissions(self, *args, **kwargs) -> list[dict[str, Any]]:
        return [
            {
                "key": "ADMIN",
                "name": "Admin",
                "description": "Access to control of camera",
                "roles": ["admin"],
                "dangerous": True,
                "default_groups": [ADMIN_GROUP],
            }
        ]

    # ── Software Update ─────────────────────────────────────────────

    def get_update_information(self) -> dict[str, Any]:
        return {
            "ArducamCameraControl": {
                "displayName": "Arducam Camera Control",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "arducam",
                "repo": "ArducamCameraControl",
                "current": self._plugin_version,
                "pip": (
                    "https://github.com/arducam/ArducamCameraControl"
                    "/archive/{target_version}.zip"
                ),
            }
        }


# ── Module-level registration ───────────────────────────────────────

__plugin_name__ = "Arducam Camera Control"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__() -> None:
    global __plugin_implementation__
    __plugin_implementation__ = ArducamCameraControlPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": (
            __plugin_implementation__.get_update_information
        ),
        "octoprint.access.permissions": (
            __plugin_implementation__.get_permissions
        ),
    }
