import sys
import time
import os
from collections import deque

from icmplib import ping  # pip install icmplib

from PySide6.QtWidgets import QApplication, QWidget, QLabel, QPushButton, QSizeGrip
from PySide6.QtCore import QThread, Signal, Qt, QPoint, QTimer
from PySide6.QtGui import (
    QPainter, QColor, QBrush,
    QShortcut, QKeySequence, QFont, QWheelEvent
)

# --- Настройки цели ---
TARGET_HOST = "google.com"
PING_INTERVAL = 0.5
ICMP_TIMEOUT = 1.0

# --- Настройки окна (стартовые) ---
START_WINDOW_SIZE = (300, 100)
SMOOTH_WINDOW = 20

POINT_RADIUS_RAW = 1
POINT_RADIUS_SMOOTH = 2

COLOR_GOOD = QColor("#00FF00")
COLOR_WARN = QColor("#FFCC00")
COLOR_BAD  = QColor("#FF4444")
COLOR_RAW  = QColor(150, 150, 150)

COLOR_TEXT = QColor(180, 180, 180)
COLOR_AXIS = QColor(150, 150, 170)
COLOR_BG   = QColor(22, 22, 32, 230)  # фон сам по себе с альфой

PING_LOW  = 50.0
PING_MED  = 200.0
PING_HIGH = 250.0

FPS = 10
FRAME_INTERVAL = 1.0 / FPS

# Прозрачность фона (НЕ окна)
ALPHA_MIN = 50
ALPHA_MAX = 255
ALPHA_STEP = 15


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


class PingWaveWidget(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("PingWave")
        self.resize(*START_WINDOW_SIZE)
        self.setMinimumSize(150, 60)

        self.setWindowFlags(
            Qt.Tool |
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        # НИКАКОЙ общей прозрачности окна
        self.current_alpha = COLOR_BG.alpha()  # стартуем с альфой фона
        self.setWindowOpacity(1.0)

        self.raw_len = self.width()
        self.raw_data = deque(maxlen=self.raw_len)   # (value, is_real)
        self.smooth_data = deque(maxlen=self.raw_len)

        self.last_real = None

        self.lbl_status = QLabel(self)
        self.lbl_status.move(10, 5)
        self.lbl_status.resize(200, 20)
        self.lbl_status.setText(f"Connecting {TARGET_HOST}...")
        self.lbl_status.setStyleSheet(
            f"color: {COLOR_TEXT.name()}; font-family: Consolas; "
            "font-size: 12px; font-weight: bold;"
        )

        self.btn_close = QPushButton("×", self)
        self.btn_close.setStyleSheet(
            "QPushButton {background: transparent; color: #777; "
            "border: none; font-size: 16px; font-weight: bold;}"
            "QPushButton:hover {color: #FFFFFF;}"
        )
        self.btn_close.clicked.connect(self.close)

        self.sizegrip = QSizeGrip(self)
        self.sizegrip.setStyleSheet("QSizeGrip { width: 16px; height: 16px; }")

        QShortcut(QKeySequence("Esc"), self, activated=self.close)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

        self.thread = PingerThread()
        self.thread.ping_result.connect(self.on_new_ping)
        self.thread.start()

        self.frame_timer = QTimer(self)
        self.frame_timer.timeout.connect(self.on_frame)
        self.frame_timer.start(int(FRAME_INTERVAL * 1000))

        self._old_pos = None

        self.move(0, 0)
        self._update_controls_pos()

    def _update_controls_pos(self):
        self.btn_close.setGeometry(self.width() - 25, 5, 20, 20)
        self.sizegrip.move(self.width() - 16, self.height() - 16)

    def resizeEvent(self, event):
        new_width = event.size().width()
        if new_width != self.raw_len:
            self._resize_buffers(new_width)
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
        self.update()

    def on_frame(self):
        if self.last_real is None:
            return
        last_val = self.raw_data[-1][0] if self.raw_data else self.last_real
        self.raw_data.append((last_val, False))
        self._append_smooth_for_last_raw()
        self.update()

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

        w = self.width()
        h = self.height()

        bg = QColor(COLOR_BG)
        bg.setAlpha(self.current_alpha)
        painter.setBrush(QBrush(bg))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        if not self.raw_data:
            return

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
            max_val = current_max + pad
            min_val = max(0.0, current_min - pad)
        else:
            max_val = 100.0
            min_val = 0.0

        padding_top = 30
        padding_bottom = 10
        graph_h = h - padding_top - padding_bottom
        value_range = max_val - min_val
        if value_range <= 0.001:
            value_range = 1.0

        start_x = w - len(self.raw_data)

        for i, (v, is_real) in enumerate(self.raw_data):
            if not is_real:
                continue
            x = start_x + i
            if x < 0 or x > w:
                continue
            if v < 0:
                y = h - padding_bottom
            else:
                ratio = (v - min_val) / value_range
                ratio = max(0.0, min(1.0, ratio))
                y = (h - padding_bottom) - ratio * graph_h
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
                ratio = (avg - min_val) / value_range
                ratio = max(0.0, min(1.0, ratio))
                y = (h - padding_bottom) - ratio * graph_h
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(self._color_for_value(avg)))
                painter.drawEllipse(QPoint(int(x), int(y)),
                                    POINT_RADIUS_SMOOTH, POINT_RADIUS_SMOOTH)

        font = QFont("Consolas", 8)
        painter.setFont(font)
        painter.setPen(COLOR_AXIS)
        painter.drawText(4, h - padding_bottom - 1, f"{int(min_val)}")
        painter.drawText(4, padding_top, f"{int(max_val)}")

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

    # Колесо мыши: изменяем только альфу фона
    def wheelEvent(self, event: QWheelEvent):
        if not self.isActiveWindow():
            return
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
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PingWaveWidget()
    window.show()
    code = app.exec()
    os._exit(code)
