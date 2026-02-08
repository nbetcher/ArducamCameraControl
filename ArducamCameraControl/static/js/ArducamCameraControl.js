/*
 * View model for ArducamCameraControl
 *
 * Author: Arducam
 * License: AGPLv3
 *
 * Dynamically builds the control UI from the capabilities payload
 * returned by the plugin backend at startup.
 *
 * Read-only queries  → GET  (on_api_get)
 * Side-effect writes → POST (on_api_command)
 */
$(function () {
    function ArducamCameraControlViewModel(parameters) {
        var self = this;
        var PLUGIN = "ArducamCameraControl";

        /* ── PTZ state ──────────────────────────────────────────── */
        var tilt = 90;
        var pan  = 90;
        var step = 5;

        /* ── Debounce timer for sliders ─────────────────────────── */
        var _debounceTimers = {};
        function debounce(key, fn, delay) {
            if (_debounceTimers[key]) clearTimeout(_debounceTimers[key]);
            _debounceTimers[key] = setTimeout(fn, delay || 120);
        }

        /* ── Helper: clamp ──────────────────────────────────────── */
        function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

        /* ──────────────────────────────────────────────────────────
         *  Startup – wire static controls, then query capabilities
         * ────────────────────────────────────────────────────────── */
        self.onStartup = function () {
            $("#arducam-camera-control").insertBefore("#control-jog-custom");

            /* PTZ buttons */
            $("#arducam-tilt-up").click(function () {
                tilt = clamp(tilt + step, 0, 180);
                self.apiPost("ptz_tilt", {value: tilt}, function () {
                    $("#arducam-pan-tilt-label").text(tilt);
                });
            });
            $("#arducam-tilt-down").click(function () {
                tilt = clamp(tilt - step, 0, 180);
                self.apiPost("ptz_tilt", {value: tilt}, function () {
                    $("#arducam-pan-tilt-label").text(tilt);
                });
            });
            $("#arducam-pan-right").click(function () {
                pan = clamp(pan + step, 0, 180);
                self.apiPost("ptz_pan", {value: pan}, function () {
                    $("#arducam-pan-tilt-label").text(pan);
                });
            });
            $("#arducam-pan-left").click(function () {
                pan = clamp(pan - step, 0, 180);
                self.apiPost("ptz_pan", {value: pan}, function () {
                    $("#arducam-pan-tilt-label").text(pan);
                });
            });

            /* Step selector */
            $("#arducam-step-5").click(function ()  { step = 5; });
            $("#arducam-step-10").click(function () { step = 10; });
            $("#arducam-step-20").click(function () { step = 20; });

            /* I2C zoom slider (debounced) */
            $("#arducam-ptz-zoom").on("input", function () {
                var v = parseInt(this.value, 10);
                $("#arducam-zoom-value").text(v);
                debounce("zoom", function () {
                    self.apiPost("ptz_zoom", {value: v});
                });
            });

            /* I2C focus slider (debounced) */
            $("#arducam-ptz-focus").on("input", function () {
                var v = parseInt(this.value, 10);
                $("#arducam-focus-value").text(v);
                debounce("focus", function () {
                    self.apiPost("ptz_focus", {value: v});
                });
            });

            /* IR-cut toggle */
            $("#arducam-ircut").click(function () {
                if (this.innerText === "ON") {
                    self.apiPost("ptz_ircut", {value: 1}, function () {
                        $("#arducam-ircut").text("OFF");
                    });
                } else {
                    self.apiPost("ptz_ircut", {value: 0}, function () {
                        $("#arducam-ircut").text("ON");
                    });
                }
            });

            /* Refresh button */
            $("#arducam-refresh-btn").click(function () {
                var $btn = $(this);
                $btn.prop("disabled", true).find("i").addClass("fa-spin");
                self.apiPost("refresh_controls", {}, function (data) {
                    self.applyI2CCapabilities(data.i2c_capabilities || []);
                    self.buildV4L2Controls(data.v4l2_controls || []);

                    /* Re-evaluate status panel */
                    var hasI2C = data.i2c_capabilities && data.i2c_capabilities.length;
                    var hasV4L2 = data.v4l2_controls && data.v4l2_controls.length;
                    if (!hasI2C && !hasV4L2) {
                        self.showStatusPanel(data.diagnostics || {});
                    } else {
                        $("#arducam-status-panel").hide();
                    }

                    $btn.prop("disabled", false).find("i").removeClass("fa-spin");
                }).fail(function () {
                    $btn.prop("disabled", false).find("i").removeClass("fa-spin");
                });
            });

            /* ── Fetch capabilities and build dynamic UI ────────── */
            self.fetchCapabilities();
        };

        /* ──────────────────────────────────────────────────────────
         *  Fetch capabilities from the backend (GET)
         * ────────────────────────────────────────────────────────── */
        self.fetchCapabilities = function () {
            self.apiGet("get_capabilities").done(function (data) {
                /* Adjust focus range for motorized cameras *before*
                   showing panels so the slider never displays an
                   out-of-range initial value. */
                if (data.camera_type === "motorized") {
                    $("#arducam-ptz-focus").attr("min", 100).attr("max", 1000).val(512);
                    $("#arducam-focus-value").text(512);
                }

                self.applyI2CCapabilities(data.i2c_capabilities || []);
                self.buildV4L2Controls(data.v4l2_controls || []);

                /* Always show refresh so users can retry after a hot-plug */
                $("#arducam-refresh-panel").show();

                /* Status / diagnostics panel */
                var hasI2C = data.i2c_capabilities && data.i2c_capabilities.length;
                var hasV4L2 = data.v4l2_controls && data.v4l2_controls.length;

                if (!hasI2C && !hasV4L2) {
                    self.showStatusPanel(data.diagnostics || {});
                } else {
                    $("#arducam-status-panel").hide();
                }

                /* Restore saved focus slider position */
                self.apiGet("get_focus").done(function (r) {
                    if (r && r.value !== undefined) {
                        $("#arducam-ptz-focus").val(r.value);
                        $("#arducam-focus-value").text(r.value);
                    }
                });
            }).fail(function () {
                /* API unreachable — show a generic status */
                self.showStatusPanel({});
                $("#arducam-refresh-panel").show();
            });
        };

        /* ──────────────────────────────────────────────────────────
         *  Show / hide I2C panels based on capabilities
         * ────────────────────────────────────────────────────────── */
        self.applyI2CCapabilities = function (caps) {
            var has = function (c) { return caps.indexOf(c) !== -1; };

            if (has("pan") && has("tilt")) {
                $("#arducam-ptz-panel").show();
            }
            if (has("focus") || has("zoom")) {
                $("#arducam-i2c-panel").show();
            }
            if (has("focus")) { $("#arducam-focus-group").show(); }
            if (has("zoom"))  { $("#arducam-zoom-group").show(); }
            if (has("ircut")) { $("#arducam-ircut-panel").show(); }
        };

        /* ──────────────────────────────────────────────────────────
         *  Dynamically build v4l2 controls
         * ────────────────────────────────────────────────────────── */
        self.buildV4L2Controls = function (controls) {
            var $container = $("#arducam-v4l2-controls");
            $container.empty();

            if (!controls.length) return;
            $("#arducam-v4l2-panel").show();

            controls.forEach(function (ctrl) {
                var $row = $('<div class="arducam-v4l2-row"></div>');
                var id = "arducam-v4l2-" + ctrl.id;

                switch (ctrl.type) {
                    case "integer":
                        $row.append(self.buildSlider(ctrl, id));
                        break;
                    case "boolean":
                        $row.append(self.buildToggle(ctrl, id));
                        break;
                    case "menu":
                    case "integer_menu":
                        $row.append(self.buildMenu(ctrl, id));
                        break;
                    case "button":
                        $row.append(self.buildButton(ctrl, id));
                        break;
                    default:
                        return; /* skip unknown types */
                }

                $container.append($row);
            });
        };

        /* ── Control builders ───────────────────────────────────── */

        self.buildSlider = function (ctrl, id) {
            var disabled = ctrl.read_only || ctrl.inactive;
            var $wrap = $('<div class="arducam-ctrl-group"></div>');
            var $label = $(
                '<label class="arducam-ctrl-label">' +
                self.escHtml(ctrl.name) +
                ' <small class="arducam-ctrl-value" id="' + id + '-val">' +
                ctrl.value + '</small></label>'
            );
            var $slider = $(
                '<input type="range" id="' + id + '"' +
                ' min="' + ctrl.min + '"' +
                ' max="' + ctrl.max + '"' +
                ' step="' + ctrl.step + '"' +
                ' value="' + ctrl.value + '"' +
                ' class="input-block-level arducam-slider"' +
                (disabled ? " disabled" : "") + ' />'
            );
            var $reset = $(
                '<button class="btn btn-mini arducam-reset" title="Reset to default"' +
                (disabled ? " disabled" : "") +
                ' data-default="' + ctrl.default + '"' +
                ' data-target="' + id + '"' +
                ' data-ctrl-id="' + ctrl.id + '"' +
                ' data-device="' + self.escAttr(ctrl.device) + '"' +
                '>↺</button>'
            );

            $slider.on("input", function () {
                var v = parseInt(this.value, 10);
                $("#" + id + "-val").text(v);
                debounce(id, function () {
                    self.setV4L2(ctrl.id, v);
                });
            });

            $reset.on("click", function () {
                var def = parseInt($(this).data("default"), 10);
                $("#" + id).val(def).trigger("input");
            });

            $wrap.append($label).append(
                $('<div class="arducam-slider-row"></div>')
                    .append($slider).append($reset)
            );
            return $wrap;
        };

        self.buildToggle = function (ctrl, id) {
            var disabled = ctrl.read_only || ctrl.inactive;
            var checked = !!ctrl.value;
            var $wrap = $('<div class="arducam-ctrl-group arducam-toggle-group"></div>');
            var $label = $(
                '<label class="arducam-ctrl-label">' +
                self.escHtml(ctrl.name) + '</label>'
            );
            var $toggle = $(
                '<label class="arducam-toggle">' +
                '<input type="checkbox" id="' + id + '"' +
                (checked ? " checked" : "") +
                (disabled ? " disabled" : "") + ' />' +
                '<span class="arducam-toggle-slider"></span>' +
                '</label>'
            );

            $toggle.find("input").on("change", function () {
                self.setV4L2(ctrl.id, this.checked ? 1 : 0);
            });

            $wrap.append($label).append($toggle);
            return $wrap;
        };

        self.buildMenu = function (ctrl, id) {
            var disabled = ctrl.read_only || ctrl.inactive;
            var $wrap = $('<div class="arducam-ctrl-group"></div>');
            var $label = $(
                '<label class="arducam-ctrl-label">' +
                self.escHtml(ctrl.name) + '</label>'
            );
            var $select = $(
                '<select id="' + id + '" class="input-block-level"' +
                (disabled ? " disabled" : "") + '></select>'
            );

            if (ctrl.menu_items) {
                $.each(ctrl.menu_items, function (idx, name) {
                    var $opt = $("<option></option>")
                        .attr("value", idx)
                        .text(name);
                    if (parseInt(idx, 10) === ctrl.value) {
                        $opt.attr("selected", true);
                    }
                    $select.append($opt);
                });
            }

            $select.on("change", function () {
                self.setV4L2(ctrl.id, parseInt(this.value, 10));
            });

            $wrap.append($label).append($select);
            return $wrap;
        };

        self.buildButton = function (ctrl, id) {
            var disabled = ctrl.read_only || ctrl.inactive;
            var $btn = $(
                '<button id="' + id + '" class="btn"' +
                (disabled ? " disabled" : "") + '>' +
                self.escHtml(ctrl.name) + '</button>'
            );

            $btn.on("click", function () {
                self.setV4L2(ctrl.id, 1);
            });

            return $btn;
        };

        /* ──────────────────────────────────────────────────────────
         *  API helpers
         * ────────────────────────────────────────────────────────── */

        /* ── Diagnostics / status panel ─────────────────────────── */

        self.showStatusPanel = function (diag) {
            var $panel = $("#arducam-status-panel");
            var $text  = $("#arducam-status-text");
            var $dl    = $("#arducam-diagnostics-list");

            /* Build a human-readable status message */
            var lines = [];
            if (!diag || !Object.keys(diag).length) {
                lines.push("Unable to reach the camera control backend.");
                lines.push('Try clicking <strong>Refresh Controls</strong> below.');
            } else {
                if (diag.camera_type === "none" && diag.v4l2_controls_count === 0) {
                    lines.push("No camera controls were detected.");
                }
                if (diag.i2c_buses_found === 0) {
                    lines.push("No I2C buses found \u2014 is I2C enabled in raspi-config?");
                } else if (diag.camera_type === "none") {
                    lines.push("I2C buses present but no Arducam at address 0x0C.");
                }
                if (diag.video_devices_found === 0) {
                    lines.push("No /dev/video* devices found \u2014 is a camera connected?");
                } else if (diag.v4l2_controls_count === 0) {
                    lines.push(diag.video_devices_found + " video device(s) found but no v4l2 controls exposed.");
                }
                var probeStatus = diag.libcamera_probe_status || "unknown";
                if (probeStatus === "timeout") {
                    lines.push("libcamera probe timed out (streamer may have locked the camera).");
                } else if (probeStatus === "error") {
                    lines.push("libcamera probe encountered an error.");
                } else if (probeStatus === "skipped") {
                    lines.push("libcamera probe skipped (picamera2 not installed or camera locked).");
                }
                if (lines.length === 0) {
                    lines.push("Camera detected but no adjustable controls found.");
                }
                lines.push('Try clicking <strong>Refresh Controls</strong> to re-probe.');
            }

            $text.html(lines.join("<br>"));

            /* Populate diagnostics <dl> */
            $dl.empty();
            if (diag && Object.keys(diag).length) {
                var labelMap = {
                    "i2c_buses_found":          "I2C buses",
                    "video_devices_found":      "Video devices",
                    "camera_type":              "Camera type",
                    "i2c_bus_number":           "I2C bus #",
                    "i2c_capabilities_count":   "I2C capabilities",
                    "v4l2_controls_count":      "v4l2 controls",
                    "libcamera_controls_count": "libcamera controls",
                    "libcamera_probe_status":   "libcamera probe"
                };
                $.each(labelMap, function (key, label) {
                    if (diag[key] !== undefined && diag[key] !== null) {
                        $dl.append(
                            $("<dt></dt>").text(label),
                            $("<dd></dd>").text(String(diag[key]))
                        );
                    }
                });
                if (diag.video_device_paths && diag.video_device_paths.length) {
                    $dl.append(
                        $("<dt></dt>").text("Devices"),
                        $("<dd></dd>").text(diag.video_device_paths.join(", "))
                    );
                }
            }

            $panel.show();
        };

        /* GET — for read-only queries (get_capabilities, get_focus, get_v4l2) */
        self.apiGet = function (command, params) {
            var qs = "?command=" + encodeURIComponent(command);
            if (params) {
                $.each(params, function (k, v) {
                    qs += "&" + encodeURIComponent(k) + "=" + encodeURIComponent(v);
                });
            }
            return OctoPrint.get(
                OctoPrint.getSimpleApiUrl(PLUGIN) + qs
            );
        };

        /* POST — for side-effect commands (set_v4l2, ptz_*, refresh) */
        self.apiPost = function (command, payload, onDone) {
            payload = payload || {};
            return OctoPrint.simpleApiCommand(PLUGIN, command, payload)
                .done(function (resp) {
                    if (onDone) onDone(resp);
                });
        };

        /* Convenience: POST a v4l2 set command */
        self.setV4L2 = function (ctrlId, value, onDone) {
            self.apiPost("set_v4l2", {
                control_id: ctrlId,
                value: value
            }, onDone);
        };

        /* ── Util ───────────────────────────────────────────────── */

        self.escHtml = function (s) {
            return $("<span>").text(s).html();
        };
        self.escAttr = function (s) {
            return String(s)
                .replace(/&/g, "&amp;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#39;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;");
        };

        /* ── Plugin messages ────────────────────────────────────── */

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== PLUGIN) return;
            if (data && data.error) {
                new PNotify({
                    title: "Arducam Camera Control",
                    text: data.error,
                    type: "error",
                    hide: true
                });
            }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: ArducamCameraControlViewModel,
        dependencies: [],
        elements: []
    });
});
