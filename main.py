import sys
import time
import asyncio
import tomllib
from pathlib import Path
from collections import deque

from icmplib import ping  # pip install icmplib

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QSizeGrip,
    QDialog, QComboBox, QLineEdit, QCheckBox, QFormLayout, QHBoxLayout,
    QVBoxLayout, QMenu, QMessageBox,
)
from PySide6.QtCore import QThread, Signal, Qt, QPoint, QTimer, QSize
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QPixmap,
    QShortcut, QKeySequence, QFont, QWheelEvent, QAction,
)

# --- Config ---
CONFIG_PATH = Path(__file__).parent / "config.toml"

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}

def save_config(cfg: dict):
    lines = []
    for section, values in cfg.items():
        lines.append(f"[{section}]")
        for k, v in values.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, float):
                lines.append(f"{k} = {v}")
            elif isinstance(v, int):
                lines.append(f"{k} = {v}")
        lines.append("")
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")

_config = load_config()
_ping_cfg = _config.get("ping", {})
_bt_cfg = _config.get("bluetooth", {})

# --- Ping settings ---
TARGET_HOST = _ping_cfg.get("host", "google.com")
PING_INTERVAL = _ping_cfg.get("interval", 0.5)
ICMP_TIMEOUT = _ping_cfg.get("timeout", 1.0)

# --- Bluetooth ---
BT_DEVICE_NAME = _bt_cfg.get("device_name", "")
BT_DEVICE_MAC = _bt_cfg.get("device_mac", "")
BT_POLL_INTERVAL = _bt_cfg.get("poll_interval", 5)

# --- Window (saved or defaults) ---
_win_cfg = _config.get("window", {})
START_WINDOW_SIZE = (_win_cfg.get("width", 300), _win_cfg.get("height", 100))
START_WINDOW_POS = (_win_cfg.get("x", 0), _win_cfg.get("y", 0))
START_ALPHA = _win_cfg.get("alpha", 230)
SMOOTH_WINDOW = 20

POINT_RADIUS_RAW = 1
POINT_RADIUS_SMOOTH = 2

COLOR_GOOD = QColor("#00FF00")
COLOR_WARN = QColor("#FFCC00")
COLOR_BAD  = QColor("#FF4444")
COLOR_RAW  = QColor(150, 150, 150)

COLOR_TEXT = QColor(180, 180, 180)
COLOR_AXIS = QColor(150, 150, 170)
COLOR_BG   = QColor(22, 22, 32, 230)

PING_LOW  = 50.0
PING_MED  = 200.0
PING_HIGH = 250.0

FPS = 10
FRAME_INTERVAL = 1.0 / FPS

# Background alpha range
ALPHA_MIN = 50
ALPHA_MAX = 255
ALPHA_STEP = 15


def _read_bt_battery(mac_hex: str) -> int:
    """Read BT headphone battery via PnP device property.

    Finds BTHENUM HFP node ({0000111E}) matching the MAC,
    reads DEVPKEY {104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2.
    Returns battery 0-100, or -1 if unavailable.
    """
    import subprocess
    cmd = (
        f"Get-PnpDevice | Where-Object {{ $_.InstanceId -like '*0000111E*{mac_hex}*' }} |"
        " ForEach-Object { (Get-PnpDeviceProperty -InstanceId $_.InstanceId"
        " -KeyName '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2').Data }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        val = result.stdout.strip()
        if val.isdigit():
            return int(val)
    except Exception:
        pass
    return -1


class BluetoothMonitor(QThread):
    # bt_on, device_connected, battery_percent (-1 = unknown)
    status_updated = Signal(bool, bool, int)

    def __init__(self, device_mac: str):
        super().__init__()
        self._mac_int = int(device_mac.replace(":", ""), 16)
        self._mac_hex = device_mac.replace(":", "").upper()
        self._skip_polls = 0

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self.isInterruptionRequested():
                if self._skip_polls > 0:
                    self._skip_polls -= 1
                else:
                    bt_on, connected, battery = loop.run_until_complete(self._poll())
                    self.status_updated.emit(bt_on, connected, battery)
                ms = BT_POLL_INTERVAL * 1000
                while ms > 0 and not self.isInterruptionRequested():
                    step = min(500, ms)
                    self.msleep(int(step))
                    ms -= step
        finally:
            loop.close()

    async def _poll(self):
        import winrt.windows.devices.radios as radios
        import winrt.windows.devices.bluetooth as bt

        bt_on = False
        connected = False
        battery = -1

        try:
            all_radios = await radios.Radio.get_radios_async()
            for r in all_radios:
                if r.kind == radios.RadioKind.BLUETOOTH:
                    bt_on = (r.state == radios.RadioState.ON)
                    break
        except Exception:
            bt_on = True  # can't determine, assume on

        if bt_on:
            try:
                device = await bt.BluetoothDevice.from_bluetooth_address_async(self._mac_int)
                if device:
                    connected = (device.connection_status == bt.BluetoothConnectionStatus.CONNECTED)
                    device.close()
            except Exception:
                pass

            if connected:
                try:
                    battery = _read_bt_battery(self._mac_hex)
                except Exception:
                    battery = -1

        return bt_on, connected, battery

    def toggle_device(self, connected: bool):
        self._skip_polls = 2
        import ctypes
        from ctypes import wintypes
        import uuid

        bthprops = ctypes.windll.LoadLibrary("bthprops.cpl")

        class SYSTEMTIME(ctypes.Structure):
            _fields_ = [
                ("wYear", wintypes.WORD), ("wMonth", wintypes.WORD),
                ("wDayOfWeek", wintypes.WORD), ("wDay", wintypes.WORD),
                ("wHour", wintypes.WORD), ("wMinute", wintypes.WORD),
                ("wSecond", wintypes.WORD), ("wMilliseconds", wintypes.WORD),
            ]

        class BLUETOOTH_ADDRESS(ctypes.Structure):
            _fields_ = [("ullLong", ctypes.c_ulonglong)]

        class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("Address", BLUETOOTH_ADDRESS),
                ("ulClassofDevice", ctypes.c_ulong),
                ("fConnected", wintypes.BOOL),
                ("fRemembered", wintypes.BOOL),
                ("fAuthenticated", wintypes.BOOL),
                ("stLastSeen", SYSTEMTIME),
                ("stLastUsed", SYSTEMTIME),
                ("szName", ctypes.c_wchar * 248),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def make_guid(s):
            u = uuid.UUID(s)
            b = u.bytes_le
            return GUID(
                int.from_bytes(b[0:4], "little"),
                int.from_bytes(b[4:6], "little"),
                int.from_bytes(b[6:8], "little"),
                (ctypes.c_ubyte * 8)(*b[8:16]),
            )

        di = BLUETOOTH_DEVICE_INFO()
        di.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_INFO)
        di.Address.ullLong = self._mac_int

        flags = 0x01 if not connected else 0x00  # ENABLE if disconnected, DISABLE if connected
        for guid_str in ("0000110B-0000-1000-8000-00805F9B34FB",   # A2DP Sink
                         "0000111E-0000-1000-8000-00805F9B34FB"):   # HFP
            guid = make_guid(guid_str)
            bthprops.BluetoothSetServiceState(
                None, ctypes.byref(di), ctypes.byref(guid), flags
            )

    def toggle_bt(self):
        self._skip_polls = 2
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._toggle_bt())
        finally:
            loop.close()

    async def _toggle_bt(self):
        import winrt.windows.devices.radios as radios
        try:
            all_radios = await radios.Radio.get_radios_async()
            for r in all_radios:
                if r.kind == radios.RadioKind.BLUETOOTH:
                    new_state = radios.RadioState.OFF if r.state == radios.RadioState.ON else radios.RadioState.ON
                    await r.set_state_async(new_state)
                    break
        except Exception:
            pass


class PingerThread(QThread):
    ping_result = Signal(float)

    def run(self):
        while not self.isInterruptionRequested():
            t_start = time.perf_counter()
            try:
                res = ping(
                    TARGET_HOST,
                    count=1,
                    interval=0,
                    timeout=ICMP_TIMEOUT,
                    privileged=False
                )
                if res.packets_received == 0:
                    delay_ms = -1.0
                else:
                    delay_ms = res.rtts[0]
                self.ping_result.emit(delay_ms)
            except Exception:
                self.ping_result.emit(-1.0)

            elapsed = time.perf_counter() - t_start
            sleep_time = max(0.05, PING_INTERVAL - elapsed)
            ms = int(sleep_time * 1000)
            while ms > 0 and not self.isInterruptionRequested():
                step = min(100, ms)
                self.msleep(step)
                ms -= step


def _list_paired_bt_audio() -> list[tuple[str, str, int, bool, bool]]:
    """Return list of (name, mac_str, battery, has_hfp, connected) for paired BT audio devices.
    battery is 0-100 or -1 if unknown. has_hfp = HFP node exists (battery capable).
    connected = device currently connected (checked via WinRT)."""
    import subprocess
    cmd = (
        "Get-PnpDevice -Class Bluetooth | Where-Object {"
        " $_.InstanceId -like 'BTHENUM\\DEV_*' -and $_.Status -eq 'OK' } |"
        " ForEach-Object {"
        " $mac = ($_.InstanceId -split '_' | Select-Object -Last 1) -replace 'BLUETOOTHDEVICE_','';"
        " $fn = $_.FriendlyName;"
        " $bat = -1; $hasHfp = 0;"
        " $hfp = Get-PnpDevice | Where-Object {"
        "   $_.InstanceId -like \"*0000111E*$mac*\" } | Select-Object -First 1;"
        " if ($hfp) { $hasHfp = 1;"
        "   $p = Get-PnpDeviceProperty -InstanceId $hfp.InstanceId"
        "     -KeyName '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2';"
        "   if ($p.Data -ne $null) { $bat = $p.Data } };"
        " \"$fn|$mac|$bat|$hasHfp\" }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        raw_devices = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 4:
                name = parts[0].strip()
                mac = parts[1].strip().upper()
                try:
                    bat = int(parts[2].strip())
                except ValueError:
                    bat = -1
                has_hfp = parts[3].strip() == "1"
                if len(mac) == 12:
                    formatted = ":".join(mac[i:i+2] for i in range(0, 12, 2))
                    raw_devices.append((name, formatted, bat, has_hfp))
        # Check connection status via WinRT
        loop = asyncio.new_event_loop()
        try:
            devices = loop.run_until_complete(_check_bt_connections(raw_devices))
        finally:
            loop.close()
        return devices
    except Exception:
        return []


async def _check_bt_connections(
    raw_devices: list[tuple[str, str, int, bool]],
) -> list[tuple[str, str, int, bool, bool]]:
    """Check connection status for each device via WinRT."""
    import winrt.windows.devices.bluetooth as bt

    results = []
    for name, mac, battery, has_hfp in raw_devices:
        connected = False
        try:
            mac_int = int(mac.replace(":", ""), 16)
            device = await bt.BluetoothDevice.from_bluetooth_address_async(mac_int)
            if device:
                connected = (
                    device.connection_status == bt.BluetoothConnectionStatus.CONNECTED
                )
                device.close()
        except Exception:
            pass
        results.append((name, mac, battery, has_hfp, connected))
    return results


DARK_STYLE = """
    QDialog, QWidget#settingsInner {
        background-color: #16161E;
        color: #B4B4B4;
        font-family: Consolas;
        font-size: 12px;
    }
    QLabel { color: #B4B4B4; }
    QLineEdit, QComboBox {
        background-color: #1E1E2A;
        color: #E0E0E0;
        border: 1px solid #333;
        border-radius: 4px;
        padding: 4px 6px;
    }
    QLineEdit:focus, QComboBox:focus {
        border-color: #00FF00;
    }
    QComboBox::drop-down {
        border: none;
        width: 20px;
    }
    QComboBox QAbstractItemView {
        background-color: #1E1E2A;
        color: #E0E0E0;
        selection-background-color: #00AA00;
    }
    QPushButton {
        background-color: #1E1E2A;
        color: #00DD00;
        border: 1px solid #00AA00;
        border-radius: 4px;
        padding: 6px 16px;
        font-weight: bold;
    }
    QPushButton:hover {
        background-color: #00AA00;
        color: #000000;
    }
    QPushButton#btnCancel {
        color: #888;
        border-color: #444;
    }
    QPushButton#btnCancel:hover {
        background-color: #444;
        color: #FFF;
    }
"""


class BtLoaderThread(QThread):
    """Background thread that loads paired BT audio devices."""
    finished = Signal(list)

    def run(self):
        devices = _list_paired_bt_audio()
        self.finished.emit(devices)


class LoadingDialog(QDialog):
    """Small spinner dialog shown while BT devices are being loaded."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(280, 70)

        inner = QWidget(self)
        inner.setObjectName("loadingInner")
        inner.setStyleSheet(
            "#loadingInner { background-color: #16161E;"
            " border: 1px solid #00AA00; border-radius: 10px; }"
        )

        lay = QHBoxLayout(inner)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        self._spinner_label = QLabel(self)
        self._spinner_label.setFixedSize(20, 20)
        self._spinner_angle = 0
        lay.addWidget(self._spinner_label)

        msg = QLabel("Loading Bluetooth devices...")
        msg.setStyleSheet(
            "color: #B4B4B4; font-family: Consolas; font-size: 12px;"
        )
        lay.addWidget(msg)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(inner)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._rotate)
        self._timer.start(80)

        # Center on parent
        if parent:
            pg = parent.geometry()
            self.move(
                pg.x() + (pg.width() - self.width()) // 2,
                pg.y() + (pg.height() - self.height()) // 2,
            )

    def _rotate(self):
        self._spinner_angle = (self._spinner_angle + 30) % 360
        self._spinner_label.update()
        size = 20
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = size // 2, size // 2
        import math
        for i in range(12):
            angle = math.radians(self._spinner_angle + i * 30)
            alpha = int(255 * (1.0 - i / 12.0))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(0, 220, 0, alpha)))
            x = cx + int(7 * math.cos(angle)) - 2
            y = cy - int(7 * math.sin(angle)) - 2
            p.drawEllipse(x, y, 4, 4)
        p.end()
        self._spinner_label.setPixmap(pix)


class SettingsDialog(QDialog):
    def __init__(self, parent=None, bt_devices=None):
        super().__init__(parent)
        self.setWindowTitle("PingWave — Settings")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(440)
        self.setStyleSheet(DARK_STYLE)
        self._old_pos = None
        self._result_cfg = None

        inner = QWidget(self)
        inner.setObjectName("settingsInner")
        inner.setStyleSheet(
            "#settingsInner { background-color: #16161E;"
            " border: 1px solid #00AA00; border-radius: 10px; }"
        )

        form = QFormLayout()
        form.setContentsMargins(20, 20, 20, 16)
        form.setSpacing(10)

        # Title
        title = QLabel("Settings")
        title.setStyleSheet("color: #00FF00; font-size: 14px; font-weight: bold;")
        form.addRow(title)

        # BT device
        self.combo_bt = QComboBox()
        self.combo_bt.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.combo_bt.setMinimumWidth(280)
        self.combo_bt.addItem("(none)", "")
        self._bt_devices = sorted(bt_devices or [], key=lambda x: x[0].lower())
        current_mac = BT_DEVICE_MAC.upper().replace(":", "")
        selected_idx = 0
        for i, (name, mac, bat, has_hfp, connected) in enumerate(self._bt_devices):
            if connected and bat >= 0:
                bat_str = f"  {bat}%"
            elif has_hfp:
                bat_str = "  (bat.)"
            else:
                bat_str = ""
            label = f"{name}  [{mac}]{bat_str}"
            self.combo_bt.addItem(label, mac)
            # Color: connected devices bright, disconnected dim
            if connected and bat >= 0:
                color = self._battery_color(bat)
                self.combo_bt.setItemData(i + 1, QColor(color), Qt.ForegroundRole)
            elif connected:
                self.combo_bt.setItemData(i + 1, QColor("#E0E0E0"), Qt.ForegroundRole)
            else:
                # Disconnected — dim gray
                self.combo_bt.setItemData(i + 1, QColor("#555"), Qt.ForegroundRole)
            if mac.replace(":", "") == current_mac:
                selected_idx = i + 1
        self.combo_bt.setCurrentIndex(selected_idx)
        form.addRow("Headphones:", self.combo_bt)

        # Ping host
        self.edit_host = QLineEdit(TARGET_HOST)
        form.addRow("Ping host:", self.edit_host)

        # Ping interval with +/- buttons
        interval_row = QHBoxLayout()
        interval_row.setSpacing(4)
        spin_style = (
            "QPushButton#spinBtn { font-family: Consolas; font-size: 16px; font-weight: bold;"
            " background-color: #1E1E2A; color: #00DD00;"
            " border: 1px solid #00AA00; border-radius: 4px; padding: 0px; }"
            "QPushButton#spinBtn:hover { background-color: #00AA00; color: #000; }"
        )
        btn_minus = QPushButton("-")
        btn_minus.setObjectName("spinBtn")
        btn_minus.setFixedSize(28, 28)
        btn_minus.setStyleSheet(spin_style)
        btn_minus.clicked.connect(lambda: self._adjust_interval(-0.1))
        interval_row.addWidget(btn_minus)
        self.edit_interval = QLineEdit(str(PING_INTERVAL))
        self.edit_interval.setFixedWidth(60)
        self.edit_interval.setAlignment(Qt.AlignCenter)
        interval_row.addWidget(self.edit_interval)
        btn_plus = QPushButton("+")
        btn_plus.setObjectName("spinBtn")
        btn_plus.setFixedSize(28, 28)
        btn_plus.setStyleSheet(spin_style)
        btn_plus.clicked.connect(lambda: self._adjust_interval(0.1))
        interval_row.addWidget(btn_plus)
        interval_row.addStretch()
        form.addRow("Interval (sec):", interval_row)

        # Checkbox: save window position
        self.chk_save_pos = QCheckBox("Save window position and size")
        self.chk_save_pos.setStyleSheet(
            "QCheckBox { color: #888; font-size: 11px; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
            "QCheckBox::indicator:unchecked { border: 1px solid #555; background: #1E1E2A; border-radius: 2px; }"
            "QCheckBox::indicator:checked { border: 1px solid #00AA00; background: #00AA00; border-radius: 2px; }"
        )
        form.addRow(self.chk_save_pos)

        # Buttons: reset | cancel | save
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_reset = QPushButton("Defaults")
        btn_reset.setObjectName("btnCancel")
        btn_reset.setToolTip("Reset all settings to defaults")
        btn_reset.clicked.connect(self._on_reset)
        btn_row.addWidget(btn_reset)

        btn_row.addStretch()

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setObjectName("btnCancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_save)

        form.addRow(btn_row)

        inner.setLayout(form)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(inner)

    @staticmethod
    def _battery_color(battery: int) -> str:
        if battery >= 50:
            t = (battery - 50) / 50.0
            r = int(255 * (1 - t))
            g = int(255 * t + 204 * (1 - t))
            b = 0
        elif battery >= 10:
            t = (battery - 10) / 40.0
            r = 255
            g = int(204 * t + 68 * (1 - t))
            b = int(68 * (1 - t))
        else:
            r, g, b = 255, 68, 68
        return f"#{r:02X}{g:02X}{b:02X}"

    def _adjust_interval(self, delta):
        try:
            val = float(self.edit_interval.text())
        except ValueError:
            val = 0.5
        val = round(max(0.1, val + delta), 1)
        self.edit_interval.setText(str(val))

    def _on_reset(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("Reset settings")
        msg.setText("Reset all settings to defaults?")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        msg.setStyleSheet(
            "QMessageBox { background-color: #16161E; color: #B4B4B4;"
            " font-family: Consolas; font-size: 12px; }"
            "QLabel { color: #B4B4B4; }"
            "QPushButton { background-color: #1E1E2A; color: #00DD00;"
            " border: 1px solid #00AA00; border-radius: 4px;"
            " padding: 4px 16px; font-weight: bold; }"
            "QPushButton:hover { background-color: #00AA00; color: #000; }"
        )
        if msg.exec() != QMessageBox.Yes:
            return
        # Reset parent window
        p = self.parent()
        if p:
            p.move(0, 0)
            p.resize(300, 100)
            p.current_alpha = 230
            p.update()
        # Save default config (no window section)
        cfg = {
            "ping": {"host": "google.com", "interval": 0.5, "timeout": 1.0},
            "bluetooth": {"device_name": "", "device_mac": "", "poll_interval": 5},
        }
        self._result_cfg = cfg
        save_config(cfg)
        self.accept()

    def _on_save(self):
        mac = self.combo_bt.currentData() or ""
        name = ""
        for dev in self._bt_devices:
            if dev[1] == mac:
                name = dev[0]
                break

        host = self.edit_host.text().strip() or "google.com"
        try:
            interval = float(self.edit_interval.text())
        except ValueError:
            interval = 0.5

        cfg = {
            "ping": {
                "host": host,
                "interval": interval,
                "timeout": ICMP_TIMEOUT,
            },
            "bluetooth": {
                "device_name": name,
                "device_mac": mac,
                "poll_interval": BT_POLL_INTERVAL,
            },
        }

        p = self.parent()
        if self.chk_save_pos.isChecked() and p:
            cfg["window"] = {
                "x": p.pos().x(),
                "y": p.pos().y(),
                "width": p.width(),
                "height": p.height(),
                "alpha": p.current_alpha,
            }
        elif _config.get("window"):
            cfg["window"] = _config["window"]

        self._result_cfg = cfg
        save_config(cfg)
        self.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._old_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._old_pos:
            delta = event.globalPosition().toPoint() - self._old_pos
            self.move(self.pos() + delta)
            self._old_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self._old_pos = None


class PingWaveWidget(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("PingWave")
        self.setToolTip("Mouse wheel — opacity\nDrag — move window")
        self.resize(*START_WINDOW_SIZE)
        self.setMinimumSize(150, 60)

        self.setWindowFlags(
            Qt.Tool |
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Background alpha (from config or default)
        self.current_alpha = START_ALPHA
        self.setWindowOpacity(1.0)

        self.raw_len = self.width()
        self.raw_data = deque(maxlen=self.raw_len)   # (value, is_real)
        self.smooth_data = deque(maxlen=self.raw_len)

        self.last_real = None

        self._graph_buf = QPixmap(self.width(), self.height())
        self._graph_buf.fill(Qt.transparent)
        self._buf_min = 0.0
        self._buf_max = 100.0
        self._buf_dirty = True

        self.lbl_status = QLabel(self)
        self.lbl_status.move(10, 5)
        self.lbl_status.resize(200, 20)
        self.lbl_status.setText(f"Connecting {TARGET_HOST}...")
        self.lbl_status.setStyleSheet(
            f"color: {COLOR_TEXT.name()}; font-family: Consolas; "
            "font-size: 12px; font-weight: bold;"
        )

        bt_btn_style = (
            "QPushButton {background: transparent; color: #555; "
            "border: none; font-family: Consolas; font-size: 11px; font-weight: bold;}"
            "QPushButton:hover {color: #FFFFFF;}"
        )

        self.lbl_battery = QLabel("—%", self)
        self.lbl_battery.setStyleSheet(
            "color: #888; font-family: Consolas; font-size: 11px;"
        )
        self.lbl_battery.setToolTip("Headphone battery")

        self.btn_headphone = QPushButton("🎧", self)
        self.btn_headphone.setStyleSheet(bt_btn_style)
        self.btn_headphone.setToolTip("Connect / Disconnect headphones")
        self.btn_headphone.setCursor(Qt.PointingHandCursor)
        self.btn_headphone.clicked.connect(self._on_headphone_click)
        self._bt_connected = False

        self.btn_bt = QPushButton("BT", self)
        self.btn_bt.setStyleSheet(bt_btn_style)
        self.btn_bt.setToolTip("Toggle Bluetooth on / off")
        self.btn_bt.setCursor(Qt.PointingHandCursor)
        self.btn_bt.clicked.connect(self._on_bt_click)

        self.btn_close = QPushButton("×", self)
        self.btn_close.setStyleSheet(
            "QPushButton {background: transparent; color: #777; "
            "border: none; font-family: Consolas; font-size: 14px; font-weight: bold;}"
            "QPushButton:hover {color: #FFFFFF;}"
        )
        self.btn_close.setToolTip("Close PingWave")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)

        self.sizegrip = QSizeGrip(self)
        self.sizegrip.setStyleSheet("QSizeGrip { width: 16px; height: 16px; }")
        self.sizegrip.setToolTip("Resize window")
        self.sizegrip.setCursor(Qt.SizeFDiagCursor)

        QShortcut(QKeySequence("Esc"), self, activated=self.close)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

        self.thread = PingerThread()
        self.thread.ping_result.connect(self.on_new_ping)
        self.thread.start()

        if BT_DEVICE_MAC:
            self.bt_monitor = BluetoothMonitor(BT_DEVICE_MAC)
            self.bt_monitor.status_updated.connect(self.on_bt_status)
            self.bt_monitor.start()
        else:
            self.bt_monitor = None

        self.frame_timer = QTimer(self)
        self.frame_timer.timeout.connect(self.on_frame)
        self.frame_timer.start(int(FRAME_INTERVAL * 1000))

        self._old_pos = None

        self.move(*START_WINDOW_POS)
        self._update_controls_pos()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #1E1E2A; color: #B4B4B4;"
            " border: 1px solid #333; font-family: Consolas; font-size: 12px; }"
            "QMenu::item:selected { background-color: #00AA00; color: #000; }"
        )
        act_settings = menu.addAction("Settings...")
        menu.addSeparator()
        act_close = menu.addAction("Close")
        action = menu.exec(event.globalPos())
        if action == act_settings:
            self._open_settings()
        elif action == act_close:
            self.close()

    def _open_settings(self):
        # Show loading spinner while BT devices are being enumerated
        self._loading_dlg = LoadingDialog(self)
        self._loading_dlg.show()

        self._bt_loader = BtLoaderThread()
        self._bt_loader.finished.connect(self._on_bt_loaded)
        self._bt_loader.start()

    def _on_bt_loaded(self, bt_devices):
        self._loading_dlg.close()
        self._loading_dlg = None
        self._bt_loader = None

        dlg = SettingsDialog(self, bt_devices=bt_devices)
        if dlg.exec() == QDialog.Accepted and dlg._result_cfg:
            global TARGET_HOST, PING_INTERVAL, BT_DEVICE_MAC, BT_DEVICE_NAME
            cfg = dlg._result_cfg
            new_host = cfg["ping"]["host"]
            new_interval = cfg["ping"]["interval"]
            new_mac = cfg["bluetooth"]["device_mac"]

            need_restart = (
                new_host != TARGET_HOST
                or new_interval != PING_INTERVAL
                or new_mac != BT_DEVICE_MAC
            )

            TARGET_HOST = new_host
            PING_INTERVAL = new_interval
            BT_DEVICE_MAC = new_mac
            BT_DEVICE_NAME = cfg["bluetooth"]["device_name"]

            if "window" in cfg:
                w = cfg["window"]
                self.move(w["x"], w["y"])
                self.resize(w["width"], w["height"])
                self.current_alpha = w["alpha"]
                self.update()

            if need_restart:
                self._restart_threads()

    def _restart_threads(self):
        # Stop old threads
        self.thread.requestInterruption()
        self.thread.quit()
        self.thread.wait(3000)
        if self.bt_monitor:
            self.bt_monitor.requestInterruption()
            self.bt_monitor.quit()
            self.bt_monitor.wait(3000)

        # Start new ping thread
        self.thread = PingerThread()
        self.thread.ping_result.connect(self.on_new_ping)
        self.thread.start()
        self.lbl_status.setText(f"Connecting {TARGET_HOST}...")

        # Start new BT monitor
        if BT_DEVICE_MAC:
            self.bt_monitor = BluetoothMonitor(BT_DEVICE_MAC)
            self.bt_monitor.status_updated.connect(self.on_bt_status)
            self.bt_monitor.start()
        else:
            self.bt_monitor = None
            self.on_bt_status(False, False, -1)

    def _update_controls_pos(self):
        top = 5
        h = 20
        gap = 2
        r = self.width() - 6
        # All items: same height, uniform gap, right-aligned
        w_close = 20;  r -= w_close; self.btn_close.setGeometry(r, top, w_close, h)
        r -= gap
        w_bt = 22;     r -= w_bt;    self.btn_bt.setGeometry(r, top, w_bt, h)
        r -= gap
        w_hp = 20;     r -= w_hp;    self.btn_headphone.setGeometry(r, top, w_hp, h)
        r -= gap
        w_bat = 32;    r -= w_bat;   self.lbl_battery.setGeometry(r, top, w_bat, h)
        self.sizegrip.move(self.width() - 16, self.height() - 16)

    def resizeEvent(self, event):
        new_width = event.size().width()
        if new_width != self.raw_len:
            self._resize_buffers(new_width)
        self._graph_buf = QPixmap(event.size().width(), event.size().height())
        self._graph_buf.fill(Qt.transparent)
        self._buf_dirty = True
        self._update_controls_pos()
        super().resizeEvent(event)

    def _resize_buffers(self, new_len: int):
        new_len = max(10, new_len)
        self.raw_len = new_len
        old_raw = list(self.raw_data)
        old_smooth = list(self.smooth_data)
        self.raw_data = deque(old_raw[-new_len:], maxlen=new_len)
        self.smooth_data = deque(old_smooth[-new_len:], maxlen=new_len)

    def _append_smooth_for_last_raw(self):
        if not self.raw_data:
            return
        values = [v for (v, _) in list(self.raw_data)[-SMOOTH_WINDOW:] if v >= 0]
        if not values:
            avg = -1.0
        else:
            avg = sum(values) / len(values)
        self.smooth_data.append(avg)

    def _calc_range(self):
        raw_vals = [v for (v, _) in self.raw_data if v >= 0]
        smooth_vals = [v for v in self.smooth_data if v >= 0]
        candidates = raw_vals + smooth_vals
        if candidates:
            current_max = max(candidates)
            current_min = min(candidates)
            span = current_max - current_min
            if span < 1.0:
                span = 1.0
            pad = span * 0.1
            return max(0.0, current_min - pad), current_max + pad
        return 0.0, 100.0

    def _val_to_y(self, v, h):
        padding_top = 30
        padding_bottom = 10
        graph_h = h - padding_top - padding_bottom
        value_range = self._buf_max - self._buf_min
        if value_range <= 0.001:
            value_range = 1.0
        if v < 0:
            return h - padding_bottom
        ratio = (v - self._buf_min) / value_range
        ratio = max(0.0, min(1.0, ratio))
        return (h - padding_bottom) - ratio * graph_h

    def _redraw_full_buffer(self):
        w = self._graph_buf.width()
        h = self._graph_buf.height()
        self._graph_buf.fill(Qt.transparent)

        if not self.raw_data:
            self._buf_dirty = False
            return

        painter = QPainter(self._graph_buf)
        painter.setRenderHint(QPainter.Antialiasing)

        start_x = w - len(self.raw_data)

        for i, (v, is_real) in enumerate(self.raw_data):
            if not is_real:
                continue
            x = start_x + i
            if x < 0 or x > w:
                continue
            y = self._val_to_y(v, h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(COLOR_RAW))
            painter.drawEllipse(QPoint(int(x), int(y)),
                                POINT_RADIUS_RAW, POINT_RADIUS_RAW)

        if POINT_RADIUS_SMOOTH > 0:
            m = len(self.smooth_data)
            start_x_s = w - m
            for i, avg in enumerate(self.smooth_data):
                x = start_x_s + i
                if x < 0 or x > w:
                    continue
                if avg < 0:
                    continue
                y = self._val_to_y(avg, h)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(self._color_for_value(avg)))
                painter.drawEllipse(QPoint(int(x), int(y)),
                                    POINT_RADIUS_SMOOTH, POINT_RADIUS_SMOOTH)

        painter.end()
        self._buf_dirty = False

    def _scroll_and_draw_new(self):
        w = self._graph_buf.width()
        h = self._graph_buf.height()

        shifted = QPixmap(w, h)
        shifted.fill(Qt.transparent)
        p = QPainter(shifted)
        p.drawPixmap(-1, 0, self._graph_buf)
        p.setRenderHint(QPainter.Antialiasing)

        if self.raw_data:
            v, is_real = self.raw_data[-1]
            if is_real:
                y = self._val_to_y(v, h)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(COLOR_RAW))
                p.drawEllipse(QPoint(w - 1, int(y)),
                              POINT_RADIUS_RAW, POINT_RADIUS_RAW)

        if POINT_RADIUS_SMOOTH > 0 and self.smooth_data:
            avg = self.smooth_data[-1]
            if avg >= 0:
                y = self._val_to_y(avg, h)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(self._color_for_value(avg)))
                p.drawEllipse(QPoint(w - 1, int(y)),
                              POINT_RADIUS_SMOOTH, POINT_RADIUS_SMOOTH)

        p.end()
        self._graph_buf = shifted

    def _update_graph_buffer(self):
        min_val, max_val = self._calc_range()

        if abs(min_val - self._buf_min) > 0.01 or abs(max_val - self._buf_max) > 0.01:
            self._buf_min = min_val
            self._buf_max = max_val
            self._buf_dirty = True

        if self._buf_dirty:
            self._redraw_full_buffer()
        else:
            self._scroll_and_draw_new()

    def on_new_ping(self, value: float):
        self.last_real = value
        self.raw_data.append((value, True))
        self._append_smooth_for_last_raw()

        if value < 0:
            self.lbl_status.setText(f"LOST ({TARGET_HOST})")
            self.lbl_status.setStyleSheet(
                f"color: {COLOR_BAD.name()}; font-family: Consolas; "
                "font-size: 12px; font-weight: bold;"
            )
        else:
            self.lbl_status.setText(f"{value:.1f} {TARGET_HOST}")
            color = self._color_for_value(value)
            self.lbl_status.setStyleSheet(
                f"color: {color.name()}; font-family: Consolas; "
                "font-size: 12px; font-weight: bold;"
            )
        if self.isVisible():
            self._update_graph_buffer()
            self.update()

    def on_frame(self):
        if self.last_real is None:
            return
        last_val = self.raw_data[-1][0] if self.raw_data else self.last_real
        self.raw_data.append((last_val, False))
        self._append_smooth_for_last_raw()
        if self.isVisible():
            self._update_graph_buffer()
            self.update()

    def _on_bt_click(self):
        if self.bt_monitor:
            self._bt_on = not getattr(self, '_bt_on', True)
            self.on_bt_status(self._bt_on, self._bt_connected, -1)
            import threading
            threading.Thread(target=self.bt_monitor.toggle_bt, daemon=True).start()

    def _on_headphone_click(self):
        if self.bt_monitor:
            self._bt_connected = not self._bt_connected
            self.on_bt_status(True, self._bt_connected, -1)
            import threading
            threading.Thread(
                target=self.bt_monitor.toggle_device,
                args=(not self._bt_connected,),
                daemon=True,
            ).start()

    def on_bt_status(self, bt_on: bool, connected: bool, battery: int):
        self._bt_on = bt_on
        self._bt_connected = connected
        bt_style = (
            "QPushButton {{background: transparent; color: {color}; "
            "border: none; font-family: Consolas; font-size: 11px; font-weight: bold;}}"
            "QPushButton:hover {{color: #FFFFFF;}}"
        )

        self.btn_bt.setStyleSheet(
            bt_style.format(color="#4488FF" if bt_on else "#555")
        )

        from PySide6.QtWidgets import QGraphicsOpacityEffect
        opacity_effect = QGraphicsOpacityEffect(self.btn_headphone)
        opacity_effect.setOpacity(1.0 if connected else 0.3)
        self.btn_headphone.setGraphicsEffect(opacity_effect)

        if battery >= 0:
            self.lbl_battery.setText(f"{battery}%")
            # Gradient: 100%=#00FF00, 50%=#FFCC00, 10%=#FF4444, <10%=#FF4444
            if battery >= 50:
                t = (battery - 50) / 50.0  # 1.0 at 100%, 0.0 at 50%
                r = int(255 * (1 - t))
                g = int(255 * t + 204 * (1 - t))
                b = 0
            elif battery >= 10:
                t = (battery - 10) / 40.0  # 1.0 at 50%, 0.0 at 10%
                r = 255
                g = int(204 * t + 68 * (1 - t))
                b = int(68 * (1 - t))
            else:
                r, g, b = 255, 68, 68
            color = f"#{r:02X}{g:02X}{b:02X}"
        else:
            self.lbl_battery.setText("—%")
            color = "#555"
        self.lbl_battery.setStyleSheet(
            f"color: {color}; font-family: Consolas; font-size: 11px;"
        )

    def _color_for_value(self, v: float) -> QColor:
        if v < 0:
            return COLOR_BAD
        if v <= PING_LOW:
            return COLOR_GOOD
        if v >= PING_HIGH:
            return COLOR_BAD
        if v < PING_MED:
            t = (v - PING_LOW) / (PING_MED - PING_LOW)
            c1, c2 = COLOR_GOOD, COLOR_WARN
        else:
            t = (v - PING_MED) / (PING_HIGH - PING_MED)
            c1, c2 = COLOR_WARN, COLOR_BAD
        t = max(0.0, min(1.0, t))
        r = int(c1.red() + (c2.red() - c1.red()) * t)
        g = int(c1.green() + (c2.green() - c1.green()) * t)
        b = int(c1.blue() + (c2.blue() - c1.blue()) * t)
        return QColor(r, g, b)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        h = self.height()

        bg = QColor(COLOR_BG)
        bg.setAlpha(self.current_alpha)
        painter.setBrush(QBrush(bg))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        painter.drawPixmap(0, 0, self._graph_buf)

        if self.raw_data:
            font = QFont("Consolas", 8)
            painter.setFont(font)
            painter.setPen(COLOR_AXIS)
            painter.drawText(4, h - 10 - 1, f"{int(self._buf_min)}")
            painter.drawText(4, 30, f"{int(self._buf_max)}")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self.sizegrip.geometry().contains(event.position().toPoint()):
                self._old_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._old_pos:
            delta = event.globalPosition().toPoint() - self._old_pos
            self.move(self.pos() + delta)
            self._old_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self._old_pos = None

    def showEvent(self, event):
        self._buf_dirty = True
        super().showEvent(event)

    # Mouse wheel: change background alpha
    def enterEvent(self, event):
        self.activateWindow()
        super().enterEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        step = ALPHA_STEP if delta > 0 else -ALPHA_STEP
        self.current_alpha = max(ALPHA_MIN, min(ALPHA_MAX, self.current_alpha + step))
        self.update()

    def closeEvent(self, event):
        self.frame_timer.stop()
        self.thread.requestInterruption()
        self.thread.quit()
        self.thread.wait(3000)
        if self.bt_monitor:
            self.bt_monitor.requestInterruption()
            self.bt_monitor.quit()
            self.bt_monitor.wait(3000)
        super().closeEvent(event)
        import os
        os._exit(0)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(
        "QToolTip { background-color: #1E1E2A; color: #B4B4B4;"
        " border: 1px solid #333; font-family: Consolas; font-size: 12px;"
        " padding: 4px; }"
    )
    window = PingWaveWidget()
    window.show()
    sys.exit(app.exec())
