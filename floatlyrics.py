#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
floatlyrics.py —— 类似 Apple Music 风格的沉浸式毛玻璃歌词大窗口
左侧：大尺寸专辑封面 + 歌曲元信息 + 进度条控制
右侧：并排的播放列表 + 沉浸式高亮滚动歌词页面
采用半透明毛玻璃背景（带柔光色斑），支持无边框窗口拖拽、缩放。
"""

from PyQt5.QtCore import Qt, QRectF, QVariantAnimation, QEasingCurve, pyqtSignal, QPoint
from PyQt5.QtGui import QPainter, QPainterPath, QLinearGradient, QRadialGradient, QColor, QFont, QBrush, QPen, QPixmap
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, QSlider, QFrame, QScrollArea
)


# ======================================================================
# 毛玻璃背景与通用样式
# ======================================================================
FULL_GLASS_QSS = """
* {
    color: #e8ecf4;
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
}
QFrame#panel_card {
    background-color: rgba(26, 30, 39, 190);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 20px;
}
QListWidget#big_playlist {
    background: transparent;
    border: none;
    outline: none;
    font-size: 14px;
}
QListWidget#big_playlist::item {
    background: transparent;
    padding: 12px 14px;
    border-radius: 12px;
    color: #d7deeb;
}
QListWidget#big_playlist::item:hover {
    background-color: rgba(255, 255, 255, 20);
}
QListWidget#big_playlist::item:selected {
    background-color: rgba(110, 168, 255, 65);
    color: #ffffff;
}
QPushButton[variant="pill"] {
    background-color: rgba(255, 255, 255, 22);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 14px;
    padding: 10px 20px;
    font-size: 14px;
}
QPushButton[variant="pill"]:hover {
    background-color: rgba(110, 168, 255, 70);
    border: 1px solid rgba(110, 168, 255, 120);
}
QPushButton[variant="circle"] {
    background-color: rgba(255, 255, 255, 22);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 24px;
    min-width: 48px; max-width: 48px;
    min-height: 48px; max-height: 48px;
    font-size: 16px;
}
QPushButton[variant="circle"]:hover {
    background-color: rgba(110, 168, 255, 70);
}
QPushButton[variant="main"] {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6ea8ff, stop:1 #8b7bff);
    border: none;
    border-radius: 30px;
    color: #ffffff;
    font-size: 18px;
    min-width: 60px; max-width: 60px;
    min-height: 60px; max-height: 60px;
}
QPushButton[variant="win"] {
    background-color: rgba(255, 255, 255, 20);
    border: none;
    border-radius: 14px;
    min-width: 30px; max-width: 30px;
    min-height: 30px; max-height: 30px;
    font-size: 12px;
    color: #aab6cc;
}
QPushButton[variant="win"]:hover {
    background-color: rgba(229, 83, 75, 200);
    color: #ffffff;
}
QSlider[variant="glass"] {
    background: transparent;
    min-height: 20px;
}
QSlider[variant="glass"]::groove:horizontal {
    border: none;
    height: 6px;
    border-radius: 3px;
    background-color: rgba(255, 255, 255, 35);
}
QSlider[variant="glass"]::sub-page:horizontal {
    border: none;
    height: 6px;
    border-radius: 3px;
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6ea8ff, stop:1 #8b7bff);
}
QSlider[variant="glass"]::handle:horizontal {
    width: 16px; height: 16px;
    margin: -5px 0;
    border-radius: 8px;
    background-color: #ffffff;
}
"""


def paint_fullscreen_backdrop(widget: QWidget, radius: int = 24):
    w, h = widget.width(), widget.height()
    p = QPainter(widget)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)
    p.fillPath(path, QColor("#12151c"))
    p.save()
    p.setClipPath(path)
    p.setPen(Qt.NoPen)
    blobs = [
        (w * 0.2, h * 0.2, w * 0.5, (110, 168, 255, 60)),
        (w * 0.8, h * 0.8, w * 0.6, (139, 123, 255, 55)),
        (w * 0.5, h * 0.4, w * 0.4, (70, 200, 180, 45)),
    ]
    for cx, cy, r, col in blobs:
        g = QRadialGradient(cx, cy, r)
        g.setColorAt(0.0, QColor(col[0], col[1], col[2], col[3]))
        g.setColorAt(1.0, QColor(col[0], col[1], col[2], 0))
        p.setBrush(QBrush(g))
        p.drawRect(widget.rect())
    p.restore()
    p.setPen(QColor(255, 255, 255, 25))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


# ======================================================================
# 沉浸式大歌词滚动面板
# ======================================================================
class _BigLyricsCanvas(QWidget):
    def __init__(self, panel: "BigLyricsPanel"):
        super().__init__()
        self.panel = panel
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _):
        self.panel.paint_canvas(self)

    def mousePressEvent(self, e):
        self.panel.handle_click(e.pos().y())


class BigLyricsPanel(QScrollArea):
    seek_requested = pyqtSignal(int)

    LINE_H = 44
    PAD_Y = 30
    FADE_STEP = 24
    MIN_ALPHA = 40
    ANIM_MS = 320

    ACCENT_TOP = QColor("#8ab7ff")
    ACCENT_BOT = QColor("#c4b5ff")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")

        self.lines = []
        self.synced = False
        self.active = -1
        self.status = "暂无歌词"

        self._canvas = _BigLyricsCanvas(self)
        self.setWidget(self._canvas)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(self.ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.verticalScrollBar().setValue(int(v)))

    def show_status(self, text: str):
        self.status = text
        self.lines, self.synced, self.active = [], False, -1
        self._refresh_layout()
        self._canvas.update()

    def set_lyrics(self, lines, synced: bool):
        self.lines = list(lines or [])
        self.synced = bool(synced) and any(l.time_ms >= 0 for l in self.lines)
        self.active = -1
        self.status = "" if self.lines else "暂无歌词"
        self._refresh_layout()
        self.verticalScrollBar().setValue(0)
        self._canvas.update()

    def update_position(self, ms: int):
        if not self.synced or not self.lines:
            return
        idx = self._locate(ms)
        if idx != self.active:
            self.active = idx
            self._canvas.update()
            self._scroll_to(idx)

    def _locate(self, ms: int) -> int:
        lo, hi, ans = 0, len(self.lines) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.lines[mid].time_ms <= ms:
                ans, lo = mid, mid + 1
            else:
                hi = mid - 1
        return ans

    def _refresh_layout(self):
        n = max(len(self.lines), 1)
        vh = max(self.viewport().height(), 200)
        h = max(self.PAD_Y * 2 + n * self.LINE_H, vh)
        self._canvas.setMinimumSize(0, h)
        self._canvas.resize(max(self.viewport().width(), 100), h)

    def _scroll_to(self, idx: int):
        bar = self.verticalScrollBar()
        view_h = self.viewport().height()
        y_center = self.PAD_Y + idx * self.LINE_H + self.LINE_H // 2
        target = max(0, min(int(y_center - view_h / 2), max(0, self._canvas.height() - view_h)))
        self._anim.stop()
        self._anim.setStartValue(float(bar.value()))
        self._anim.setEndValue(float(target))
        self._anim.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh_layout()

    def paint_canvas(self, canvas: QWidget):
        p = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        w, h = canvas.width(), canvas.height()

        if not self.lines:
            p.setPen(QColor("#aab6cc"))
            f = QFont(); f.setPixelSize(18); f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(20, 0, w - 40, h), Qt.AlignCenter | Qt.TextWordWrap, self.status)
            p.end()
            return

        for i, line in enumerate(self.lines):
            y_top = self.PAD_Y + i * self.LINE_H
            if y_top + self.LINE_H < 0 or y_top > h:
                continue
            is_active = (i == self.active)
            dist = abs(i - self.active) if self.active >= 0 else 4
            alpha = 255 if is_active else max(self.MIN_ALPHA, 220 - self.FADE_STEP * dist)
            f = QFont()
            f.setPixelSize(22 if is_active else 16)
            f.setBold(is_active)
            p.setFont(f)
            if is_active:
                grad = QLinearGradient(0, y_top, w, y_top + self.LINE_H)
                grad.setColorAt(0.0, self.ACCENT_TOP)
                grad.setColorAt(1.0, self.ACCENT_BOT)
                p.setPen(QPen(QBrush(grad), 1))
            else:
                p.setPen(QColor(232, 236, 244, alpha))
            p.drawText(QRectF(20, y_top, w - 40, self.LINE_H),
                       Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
                       line.text or "· · ·")
        p.end()

    def handle_click(self, y: int):
        if not self.synced or not self.lines:
            return
        idx = int((y - self.PAD_Y) // self.LINE_H)
        if 0 <= idx < len(self.lines):
            t = self.lines[idx].time_ms
            if t >= 0:
                self.seek_requested.emit(max(0, t - 150))


# ======================================================================
# Apple Music 风格沉浸式大窗口
# ======================================================================
class FloatLyricsWindow(QWidget):
    track_selected = pyqtSignal(int)          # 列表点击切歌
    seek_requested = pyqtSignal(int)          # 进度条/歌词跳转
    play_toggled = pyqtSignal()               # 播放/暂停
    next_clicked = pyqtSignal()
    prev_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("沉浸式歌词控制台")
        self.resize(980, 680)
        self.setMinimumSize(800, 560)
        self._drag_offset = None

        self._build_ui()
        self.setStyleSheet(FULL_GLASS_QSS)

    def paintEvent(self, _):
        paint_fullscreen_backdrop(self, 24)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, _):
        self._drag_offset = None

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # 顶部标题栏
        top_bar = QHBoxLayout()
        title_lbl = QLabel("🎵 Apple Music 沉浸式歌词大屏")
        title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #e8ecf4;")
        self.btn_close = QPushButton("✕")
        self.btn_close.setProperty("variant", "win")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.hide)
        top_bar.addWidget(title_lbl)
        top_bar.addStretch(1)
        top_bar.addWidget(self.btn_close)
        root.addLayout(top_bar)

        # 中间主体：左右两栏并排
        body_layout = QHBoxLayout()
        body_layout.setSpacing(16)

        # ---- 左侧卡片：专辑封面、歌名、歌手、控制条 ----
        left_card = QFrame()
        left_card.setObjectName("panel_card")
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(20, 20, 20, 20)
        left_layout.setSpacing(16)

        self.label_cover = QLabel()
        self.label_cover.setFixedSize(240, 240)
        self.label_cover.setAlignment(Qt.AlignCenter)
        self.label_cover.setStyleSheet("background: rgba(0,0,0,30); border-radius: 16px;")

        self.label_title = QLabel("未播放曲目")
        self.label_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffffff;")
        self.label_title.setWordWrap(True)

        self.label_artist = QLabel("歌手 - 专辑")
        self.label_artist.setStyleSheet("font-size: 14px; color: #93a1bb;")
        self.label_artist.setWordWrap(True)

        info_box = QVBoxLayout()
        info_box.setSpacing(4)
        info_box.addWidget(self.label_title)
        info_box.addWidget(self.label_artist)

        # 进度条
        prog_layout = QHBoxLayout()
        self.label_time = QLabel("00:00 / 00:00")
        self.label_time.setStyleSheet("font-size: 12px; color: #93a1bb;")
        self.slider_progress = QSlider(Qt.Horizontal)
        self.slider_progress.setProperty("variant", "glass")
        self.slider_progress.setRange(0, 0)
        prog_layout.addWidget(self.label_time)
        prog_layout.addWidget(self.slider_progress, 1)

        # 控制按钮
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(16)
        self.btn_prev = QPushButton("⏮")
        self.btn_prev.setProperty("variant", "circle")
        self.btn_toggle = QPushButton("▶")
        self.btn_toggle.setProperty("variant", "main")
        self.btn_next = QPushButton("⏭")
        self.btn_next.setProperty("variant", "circle")
        for b in (self.btn_prev, self.btn_toggle, self.btn_next):
            b.setCursor(Qt.PointingHandCursor)

        ctrl_layout.addStretch(1)
        ctrl_layout.addWidget(self.btn_prev)
        ctrl_layout.addWidget(self.btn_toggle)
        ctrl_layout.addWidget(self.btn_next)
        ctrl_layout.addStretch(1)

        left_layout.addWidget(self.label_cover, 0, Qt.AlignCenter)
        left_layout.addLayout(info_box)
        left_layout.addLayout(prog_layout)
        left_layout.addLayout(ctrl_layout)
        left_layout.addStretch(1)

        # ---- 右侧卡片：播放列表 + 大歌词面板并排 ----
        right_card = QFrame()
        right_card.setObjectName("panel_card")
        right_layout = QHBoxLayout(right_card)
        right_layout.setContentsMargins(16, 16, 16, 16)
        right_layout.setSpacing(16)

        # 播放列表子卡片
        list_container = QVBoxLayout()
        list_title = QLabel("播放列表")
        list_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #aab6cc; margin-left: 4px;")
        self.list_playlist = QListWidget()
        self.list_playlist.setObjectName("big_playlist")
        self.list_playlist.setFixedWidth(260)
        list_container.addWidget(list_title)
        list_container.addWidget(self.list_playlist)

        # 歌词面板
        lyrics_container = QVBoxLayout()
        lyrics_title = QLabel("实时歌词")
        lyrics_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #aab6cc; margin-left: 4px;")
        self.lyrics_panel = BigLyricsPanel()
        lyrics_container.addWidget(lyrics_title)
        lyrics_container.addWidget(self.lyrics_panel, 1)

        right_layout.addLayout(list_container)
        right_layout.addLayout(lyrics_container, 1)

        body_layout.addWidget(left_card)
        body_layout.addWidget(right_card, 1)

        root.addLayout(body_layout, 1)

        # 信号绑定
        self.btn_toggle.clicked.connect(self.play_toggled.emit)
        self.btn_next.clicked.connect(self.next_clicked.emit)
        self.btn_prev.clicked.connect(self.prev_clicked.emit)
        self.list_playlist.itemClicked.connect(lambda it: self.track_selected.emit(it.data(Qt.UserRole)))
        self.slider_progress.sliderMoved.connect(self.seek_requested.emit)
        self.slider_progress.sliderReleased.connect(lambda: self.seek_requested.emit(self.slider_progress.value()))
        self.lyrics_panel.seek_requested.connect(self.seek_requested.emit)

    # 对外同步接口
    def update_track_info(self, track, pixmap: QPixmap):
        self.label_title.setText(track.title)
        self.label_artist.setText(f"{track.artist} - {track.album}")
        if not pixmap.isNull():
            scaled = pixmap.scaled(self.label_cover.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            # 圆角化封面
            out = QPixmap(scaled.size())
            out.fill(Qt.transparent)
            p = QPainter(out)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(0, 0, scaled.width(), scaled.height(), 16, 16)
            p.setClipPath(path)
            p.drawPixmap(0, 0, scaled)
            p.end()
            self.label_cover.setPixmap(out)
        else:
            self.label_cover.clear()

    def update_playlist(self, tracks, current_index: int):
        self.list_playlist.clear()
        for i, t in enumerate(tracks):
            it = QListWidgetItem(f"{t.title}\n{t.artist}")
            it.setData(Qt.UserRole, i)
            if i == current_index:
                it.setSelected(True)
            self.list_playlist.addItem(it)

    def update_playback_state(self, playing: bool):
        self.btn_toggle.setText("⏸" if playing else "▶")

    def update_position(self, ms: int, total_ms: int):
        if not self.slider_progress.isSliderDown():
            self.slider_progress.setRange(0, max(total_ms, 1))
            self.slider_progress.setValue(ms)
        
        s_cur = max(0, ms // 1000)
        s_tot = max(0, total_ms // 1000)
        self.label_time.setText(f"{s_cur // 60:02d}:{s_cur % 60:02d} / {s_tot // 60:02d}:{s_tot % 60:02d}")
        self.lyrics_panel.update_position(ms)

    def set_lyrics(self, lines, synced: bool):
        self.lyrics_panel.set_lyrics(lines, synced)

    def show_lyrics_status(self, text: str):
        self.lyrics_panel.show_status(text)
