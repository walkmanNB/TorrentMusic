#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py —— 极简音乐播放器（v5：+去中心化多源歌词同步）

本次新增（依赖同目录模块 lrcrequest.py）：
  * 去中心化歌词：接入 lrclib.net 开放 API，多路候选本地智能择优
  * 简繁双轨搜索：OpenCC 生成 简体/繁体 变体并发搜索、按 id 去重合并
      （opencc 未安装时优雅降级为仅原始关键词）
  * 智能模糊匹配：归一化 + 序列相似度 + 关键词重合度 + 包含关系 + 时长加成，
      自动挑选最像的一条，绝不要求 100% 一致
  * 歌词全文统一转简体；syncedLyrics 优先、plainLyrics 降级
  * 毛玻璃滚动歌词面板：随播放实时高亮当前行、平滑滚动、点击行跳播
  * 后台线程异步拉取（threading.Thread），绝不卡死 UI

既有特性：
  毛玻璃 UI / 订阅管理与持久化 / 三档播放模式 / 封面兜底 /
  边下边播 / 进度条 / 分秒显示

依赖安装：
  pip install PyQt5 requests opencc-python-reimplemented
运行：
  python run.py
"""

import json
import sys
import threading
from dataclasses import dataclass
from typing import List, Optional

import requests
from PyQt5.QtCore import (
    Qt, QUrl, QObject, pyqtSignal, QRectF, QTimer, QSettings,
    QVariantAnimation, QEasingCurve,
)
from PyQt5.QtGui import (
    QPixmap, QImage, QPainter, QPainterPath, QLinearGradient,
    QRadialGradient, QColor, QFont, QBrush, QPen,
)
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QSlider,
    QMessageBox, QFrame, QDialog, QScrollArea,
)
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent

try:
    from floatlyrics import FloatLyricsWindow
except ImportError:
    FloatLyricsWindow = None

try:
    from lrcrequest import LrclibProvider
except ImportError:
    LrclibProvider = None


# ======================================================================
# [0] 配置持久化（INI 落盘）
# ======================================================================
def derive_sub_name(url: str) -> str:
    """从链接尾部推导一个可读的订阅名。"""
    tail = url.strip().rstrip("/").split("?")[0]
    tail = tail.replace("\\", "/").split("/")[-1] or url
    if tail.lower().endswith(".json"):
        tail = tail[:-5]
    return (tail or "我的订阅")[:24]


class ConfigStore:
    """轻量配置仓库：订阅列表 + 当前默认项。所有异常吞掉，绝不影响播放。"""
    ORG = "MiniPlayer"
    APP = "SubscriptionPlayer"

    @staticmethod
    def _s() -> QSettings:
        return QSettings(QSettings.IniFormat, QSettings.UserScope,
                         ConfigStore.ORG, ConfigStore.APP)

    @staticmethod
    def load_subs() -> List[dict]:
        try:
            raw = ConfigStore._s().value("subs/data", "[]")
            data = json.loads(raw) if isinstance(raw, str) else []
            if isinstance(data, list):
                return [{"name": str(x.get("name") or derive_sub_name(x.get("url", ""))),
                         "url": str(x.get("url") or "").strip()}
                        for x in data
                        if isinstance(x, dict) and str(x.get("url") or "").strip()]
        except Exception as e:
            print("[Config] 读取订阅失败:", e)
        return []

    @staticmethod
    def save_subs(subs: List[dict]):
        try:
            ConfigStore._s().setValue(
                "subs/data", json.dumps(subs, ensure_ascii=False))
        except Exception as e:
            print("[Config] 保存订阅失败:", e)

    @staticmethod
    def get_current() -> str:
        try:
            return str(ConfigStore._s().value("subs/current", "") or "")
        except Exception:
            return ""

    @staticmethod
    def set_current(url: str):
        try:
            ConfigStore._s().setValue("subs/current", url or "")
        except Exception as e:
            print("[Config] 写入默认订阅失败:", e)


# ======================================================================
# 全局样式表（Glassmorphism）
# ======================================================================
GLASS_QSS = """
* {
    color: #e8ecf4;
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
}

QFrame#card {
    background-color: rgba(30, 34, 43, 184);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 18px;
}

QLineEdit#source {
    background-color: rgba(255, 255, 255, 15);
    border: 1px solid rgba(255, 255, 255, 23);
    border-radius: 12px;
    padding: 9px 14px;
    font-size: 13px;
}
QLineEdit#source:focus {
    border: 1px solid #6ea8ff;
    background-color: rgba(255, 255, 255, 25);
}

QListWidget#playlist {
    background: transparent;
    border: none;
    outline: none;
    font-size: 13px;
}
QListWidget#playlist::item {
    background: transparent;
    padding: 10px 12px;
    border-radius: 11px;
    color: #d7deeb;
}
QListWidget#playlist::item:hover {
    background-color: rgba(255, 255, 255, 18);
}
QListWidget#playlist::item:selected {
    background-color: rgba(110, 168, 255, 58);
    color: #ffffff;
}
QScrollBar:vertical {
    background: transparent; width: 6px; margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 46); border-radius: 3px; min-height: 30px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QPushButton[variant="pill"] {
    background-color: rgba(255, 255, 255, 20);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 12px;
    padding: 9px 18px;
    font-size: 13px;
}
QPushButton[variant="pill"]:hover {
    background-color: rgba(110, 168, 255, 66);
    border: 1px solid rgba(110, 168, 255, 115);
}
QPushButton[variant="pill"]:pressed {
    background-color: rgba(110, 168, 255, 38);
}

QPushButton[variant="circle"] {
    background-color: rgba(255, 255, 255, 20);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 21px;
    min-width: 42px; max-width: 42px;
    min-height: 42px; max-height: 42px;
    font-size: 14px;
}
QPushButton[variant="circle"]:hover {
    background-color: rgba(110, 168, 255, 61);
    border: 1px solid rgba(110, 168, 255, 102);
}
QPushButton[variant="circle"]:pressed {
    background-color: rgba(110, 168, 255, 36);
}

QPushButton[variant="main"] {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #6ea8ff, stop:1 #8b7bff);
    border: none;
    border-radius: 28px;
    color: #ffffff;
    font-size: 17px;
    min-width: 56px; max-width: 56px;
    min-height: 56px; max-height: 56px;
}
QPushButton[variant="main"]:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #84b6ff, stop:1 #9d90ff);
}
QPushButton[variant="main"]:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #5c97ee, stop:1 #7a6cf0);
}

QPushButton[variant="win"] {
    background-color: rgba(255, 255, 255, 20);
    border: none;
    border-radius: 13px;
    min-width: 26px; max-width: 26px;
    min-height: 26px; max-height: 26px;
    font-size: 11px;
    color: #aab6cc;
}
QPushButton[variant="win"]:hover {
    background-color: rgba(255, 255, 255, 46);
    color: #ffffff;
}
QPushButton[danger="true"]:hover {
    background-color: #e5534b;
    color: #ffffff;
}

QSlider[variant="glass"] {
    background: transparent;
    min-height: 18px;
}
QSlider[variant="glass"]::groove:horizontal {
    border: none;
    height: 5px;
    border-radius: 2px;
    background-color: rgba(255, 255, 255, 31);
}
QSlider[variant="glass"]::sub-page:horizontal {
    border: none;
    height: 5px;
    border-radius: 2px;
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #6ea8ff, stop:1 #8b7bff);
}
QSlider[variant="glass"]::sub-page:horizontal:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #8ab7ff, stop:1 #a394ff);
}
QSlider[variant="glass"]::handle:horizontal {
    width: 14px; height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background-color: #ffffff;
}
QSlider[variant="glass"]::handle:horizontal:hover {
    background-color: #dfeaff;
}
QSlider[variant="glass"]::handle:horizontal:pressed {
    background-color: #b9cffd;
}

QLabel#app_title { font-size: 13px; font-weight: 600; color: #cbd4e6; }
QLabel#title     { font-size: 19px; font-weight: 600; }
QLabel#sub       { font-size: 13px; color: #93a1bb; }
QLabel#time      { font-size: 12px; color: #93a1bb; }
"""


# ======================================================================
# 控件工厂（模块级，供主窗口与设置窗口共用）
# ======================================================================
def make_btn(text: str, variant: str = "", danger: bool = False) -> QPushButton:
    b = QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    if danger:
        b.setProperty("danger", True)
    return b


def make_glass_slider() -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setProperty("variant", "glass")
    return s


def paint_glass_backdrop(widget: QWidget, radius: int = 22):
    """统一的玻璃底绘制：深色底 + 柔光色斑 + 描边。"""
    w, h = widget.width(), widget.height()
    p = QPainter(widget)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)
    p.fillPath(path, QColor("#14171f"))
    p.save()
    p.setClipPath(path)
    p.setPen(Qt.NoPen)
    blobs = [
        (w * 0.18, h * 0.12, w * 0.55, (96, 130, 255, 70)),
        (w * 0.88, h * 0.78, w * 0.60, (139, 123, 255, 60)),
        (w * 0.62, h * 0.30, w * 0.32, (64, 214, 190, 40)),
    ]
    for cx, cy, r, col in blobs:
        g = QRadialGradient(cx, cy, r)
        g.setColorAt(0.0, QColor(col[0], col[1], col[2], col[3]))
        g.setColorAt(1.0, QColor(col[0], col[1], col[2], 0))
        p.setBrush(QBrush(g))
        p.drawRect(widget.rect())
    p.restore()
    p.setPen(QColor(255, 255, 255, 20))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


# ======================================================================
# [1] 数据模型
# ======================================================================
@dataclass
class Track:
    title: str = "未知标题"
    artist: str = "未知歌手"
    album: str = "未知专辑"
    url: str = ""
    cover: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "Track":
        def s(v, d):
            v = str(v).strip() if v is not None else ""
            return v or d
        return cls(
            title=s(raw.get("title"), "未知标题"),
            artist=s(raw.get("artist"), "未知歌手"),
            album=s(raw.get("album"), "未知专辑"),
            url=str(raw.get("url") or "").strip(),
            cover=str(raw.get("cover") or "").strip(),
        )

    @property
    def display(self) -> str:
        return f"{self.title} - {self.artist}"


# ======================================================================
# [2] 通用 JSON 订阅加载器
# ======================================================================
class LibraryLoadError(Exception):
    pass


class LibraryLoader:
    TIMEOUT = 15
    UA = {"User-Agent": "MiniMusicPlayer/1.0"}

    @classmethod
    def fetch_raw(cls, source: str):
        s = (source or "").strip()
        if not s:
            raise LibraryLoadError("订阅源为空")
        if s.lower().startswith(("http://", "https://")):
            resp = requests.get(s, headers=cls.UA, timeout=cls.TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        with open(s, "r", encoding="utf-8-sig") as f:
            return json.load(f)

    @classmethod
    def parse_tracks(cls, data) -> List[Track]:
        if isinstance(data, dict):
            for key in ("library", "tracks", "music", "songs", "list"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise LibraryLoadError("JSON 顶层应为歌曲数组（或含 library/tracks 键）")
        tracks: List[Track] = []
        for item in data:
            if isinstance(item, dict):
                t = Track.from_dict(item)
                if t.url:
                    tracks.append(t)
        return tracks

    @classmethod
    def load(cls, source: str) -> List[Track]:
        try:
            tracks = cls.parse_tracks(cls.fetch_raw(source))
            print(f"[Library] 成功加载 {len(tracks)} 首")
            return tracks
        except Exception as e:
            print(f"[Library] 订阅加载失败: {e}")
            return []


# ======================================================================
# [3] 封面层
# ======================================================================
class CoverProvider:
    SIZE = 300
    _cache = {}

    @classmethod
    def fetch_bytes(cls, url: str) -> Optional[bytes]:
        url = (url or "").strip()
        if not url:
            return None
        if url in cls._cache:
            return cls._cache[url]
        try:
            r = requests.get(url, headers=LibraryLoader.UA, timeout=8)
            r.raise_for_status()
            data = r.content
        except Exception as e:
            print(f"[Cover] 下载失败 {url}: {e}")
            return None
        if QImage.fromData(data).isNull():
            print(f"[Cover] 内容非图片: {url}")
            return None
        cls._cache[url] = data
        return data

    @classmethod
    def placeholder(cls, letter: str = "♪", sub: str = "") -> QPixmap:
        size = cls.SIZE
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0.0, QColor("#3b4d6f"))
        grad.setColorAt(1.0, QColor("#131a2a"))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(0, 0, size, size, 48, 48)
        p.setPen(QColor("#eef2f8"))
        f = QFont(); f.setPixelSize(int(size * 0.42)); f.setBold(True)
        p.setFont(f)
        p.drawText(pix.rect(), Qt.AlignCenter, (letter or "♪").upper())
        if sub:
            f2 = QFont(); f2.setPixelSize(int(size * 0.09))
            p.setFont(f2); p.setPen(QColor("#9aa8c0"))
            p.drawText(pix.rect().adjusted(0, int(size * 0.34), 0, 0),
                       Qt.AlignHCenter | Qt.AlignTop, sub[:18])
        p.end()
        return pix


# ======================================================================
# [4] 播放内核封装
# ======================================================================
class PlayerCore(QObject):
    state_changed   = pyqtSignal(bool)
    track_changed   = pyqtSignal(object)
    media_error     = pyqtSignal(str)
    position_changed = pyqtSignal("qint64")
    duration_changed = pyqtSignal("qint64")
    buffer_changed   = pyqtSignal(int)
    media_ended     = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.positionChanged.connect(lambda v: self.position_changed.emit(int(v)))
        self.player.durationChanged.connect(lambda v: self.duration_changed.emit(int(v)))
        self.player.bufferStatusChanged.connect(lambda v: self.buffer_changed.emit(int(v)))
        try:
            self.player.error.connect(
                lambda *_: self.media_error.emit(self.player.errorString() or "未知播放错误"))
        except Exception:
            pass

    def load(self, track) -> bool:
        if not getattr(track, "url", ""):
            return False
        self.player.setMedia(QMediaContent(QUrl(track.url)))
        self.track_changed.emit(track)
        return True

    def play(self):
        self.player.play()
        self.state_changed.emit(True)

    def pause(self):
        self.player.pause()
        self.state_changed.emit(False)

    def stop(self):
        self.player.stop()
        self.state_changed.emit(False)

    def toggle(self):
        if self.player.state() == QMediaPlayer.PlayingState:
            self.pause()
        else:
            if self.player.mediaStatus() == QMediaPlayer.EndOfMedia:
                self.player.setPosition(0)
            self.play()

    def set_volume(self, value: int):
        self.player.setVolume(max(0, min(100, int(value))))

    def seek(self, ms):
        self.player.setPosition(max(0, int(ms)))

    def _on_media_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self.media_ended.emit()
        elif status == QMediaPlayer.InvalidMedia:
            self.state_changed.emit(False)
            self.media_error.emit("音频流无法解码：格式不支持或直链失效")


# ======================================================================
# [5] 设置窗口：订阅管理
# ======================================================================
class SettingsDialog(QDialog):
    """毛玻璃风格的订阅管理器。与主窗口共享同一个 subs 列表对象，
    增删即时落盘；“设为默认”通过 applied 信号回传给主窗口触发加载。"""

    applied = pyqtSignal(str, str)      # (name, url)

    def __init__(self, parent, subs: List[dict], current_url: str):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("设置 · 订阅管理")
        self.resize(500, 560)
        self.setMinimumSize(440, 480)
        self._drag_offset = None

        self.subs = subs                          # 共享引用，改完即同步
        self.current_url = current_url or ""

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ---- 头部 ----
        head = QHBoxLayout()
        t = QLabel("⚙ 设置 · 订阅管理"); t.setObjectName("title")
        self.btn_close = make_btn("✕", "win", danger=True)
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(self.btn_close)
        root.addLayout(head)

        # ---- 卡片①：新增订阅 ----
        c_add = QFrame(); c_add.setObjectName("card")
        lay = QVBoxLayout(c_add)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        cap = QLabel("新增订阅"); cap.setObjectName("sub")
        self.in_name = QLineEdit(); self.in_name.setObjectName("source")
        self.in_name.setPlaceholderText("备注名（留空将根据链接自动生成）")
        self.in_url = QLineEdit(); self.in_url.setObjectName("source")
        self.in_url.setPlaceholderText("library.json 的 URL 或本地路径…")
        row = QHBoxLayout(); row.setSpacing(8)
        self.btn_add = make_btn("+ 添加订阅", "pill")
        row.addWidget(self.in_url, 1)
        row.addWidget(self.btn_add)
        lay.addWidget(cap)
        lay.addWidget(self.in_name)
        lay.addLayout(row)
        root.addWidget(c_add)

        # ---- 卡片②：已保存列表 ----
        c_list = QFrame(); c_list.setObjectName("card")
        lv = QVBoxLayout(c_list)
        lv.setContentsMargins(6, 6, 6, 6)
        self.listw = QListWidget(); self.listw.setObjectName("playlist")
        ops = QHBoxLayout(); ops.setSpacing(8)
        self.btn_use = make_btn("✓ 设为默认并加载", "pill")
        self.btn_del = make_btn("🗑 删除选中", "pill")
        ops.addWidget(self.btn_use)
        ops.addWidget(self.btn_del)
        ops.addStretch(1)
        lv.addWidget(self.listw, 1)
        lv.addLayout(ops)
        root.addWidget(c_list, 1)

        hint = QLabel("提示：双击条目 = 快速设为默认并加载；★ 为当前默认订阅")
        hint.setObjectName("sub")
        root.addWidget(hint)

        # ---- 行为 ----
        self.btn_close.clicked.connect(self.reject)
        self.btn_add.clicked.connect(self._add)
        self.btn_use.clicked.connect(self._use)
        self.btn_del.clicked.connect(self._delete)
        self.listw.itemDoubleClicked.connect(lambda _: self._use())
        for wdg in (self.btn_add, self.btn_use, self.btn_del,
                    self.btn_close, self.listw):
            wdg.setCursor(Qt.PointingHandCursor)

        self.setStyleSheet(GLASS_QSS)
        self._refresh()

    # ---- 玻璃底与拖拽 ----
    def paintEvent(self, _):
        paint_glass_backdrop(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, _):
        self._drag_offset = None

    # ---- 业务 ----
    def _refresh(self):
        self.listw.clear()
        for i, sub in enumerate(self.subs):
            star = "★ " if sub["url"] == self.current_url else ""
            it = QListWidgetItem(f"{star}{sub['name']}  ·  {sub['url']}")
            it.setData(Qt.UserRole, i)
            self.listw.addItem(it)

    def _add(self):
        url = self.in_url.text().strip()
        if not url:
            QMessageBox.information(self, "提示", "请先填写订阅链接或本地路径")
            return
        name = self.in_name.text().strip() or derive_sub_name(url)
        for sub in self.subs:                     # 同链接视为改名
            if sub["url"] == url:
                sub["name"] = name
                break
        else:
            self.subs.append({"name": name, "url": url})
        ConfigStore.save_subs(self.subs)
        self.in_name.clear()
        self.in_url.clear()
        self._refresh()

    def _use(self):
        it = self.listw.currentItem()
        if not it:
            QMessageBox.information(self, "提示", "请先在列表中选择一个订阅")
            return
        sub = self.subs[it.data(Qt.UserRole)]
        self.current_url = sub["url"]
        ConfigStore.set_current(sub["url"])
        ConfigStore.save_subs(self.subs)
        self._refresh()
        self.applied.emit(sub["name"], sub["url"])
        self.accept()                             # 关闭并回主窗口加载

    def _delete(self):
        it = self.listw.currentItem()
        if not it:
            QMessageBox.information(self, "提示", "请先在列表中选择要删除的订阅")
            return
        removed = self.subs.pop(it.data(Qt.UserRole))
        if removed["url"] == self.current_url:    # 删掉的是默认项则清空标记
            self.current_url = ""
            ConfigStore.set_current("")
        ConfigStore.save_subs(self.subs)
        self._refresh()


# ======================================================================
# [5.5] 滚动歌词面板（毛玻璃 · 实时高亮 · 平滑滚动 · 点击跳播）
# ======================================================================
class _LyricsCanvas(QWidget):
    """歌词画布：绘制委托给父面板，点击坐标换算行号后回传。"""

    def __init__(self, panel: "LyricsPanel"):
        super().__init__()
        self.panel = panel
        self.setAutoFillBackground(False)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _):
        self.panel.paint_canvas(self)

    def mousePressEvent(self, e):
        self.panel.handle_click(e.pos().y())


class LyricsPanel(QScrollArea):
    seek_requested = pyqtSignal(int)          # 点击某行 → 请求跳转（毫秒）

    LINE_H    = 34                             # 每行槽高（px）
    PAD_Y     = 16                             # 内容上下留白
    FADE_STEP = 26                             # 距离每 +1 行透明度衰减
    MIN_ALPHA = 52
    ANIM_MS   = 360                            # 平滑滚动时长（ms）

    ACCENT_TOP = QColor("#8ab7ff")             # 当前行渐变高亮色
    ACCENT_BOT = QColor("#a394ff")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")

        self.lines = []                        # List[LyricLine]
        self.synced = False                    # 是否带时间轴
        self.active = -1                       # 当前行索引
        self.status = "播放歌曲后在此显示歌词"

        self._canvas = _LyricsCanvas(self)
        self.setWidget(self._canvas)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(self.ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(
            lambda v: self.verticalScrollBar().setValue(int(v)))

    # ---------------- 对外 API ----------------
    def show_status(self, text: str):
        """清空并显示占位文案（加载中 / 无歌词等）。"""
        self.status = text
        self.lines, self.synced, self.active = [], False, -1
        self._refresh_layout()
        self._canvas.update()

    def set_lyrics(self, lines, synced: bool):
        """装入歌词行；synced=False 时为纯文本静态展示。"""
        self.lines = list(lines or [])
        self.synced = bool(synced) and any(l.time_ms >= 0 for l in self.lines)
        self.active = -1
        self.status = "" if self.lines else "暂无歌词"
        self._refresh_layout()
        self.verticalScrollBar().setValue(0)
        self._canvas.update()

    def update_position(self, ms: int):
        """由 positionChanged 驱动：二分定位当前行并平滑居中。"""
        if not self.synced or not self.lines:
            return
        idx = self._locate(ms)
        if idx != self.active:
            self.active = idx
            self._canvas.update()
            self._scroll_to(idx)

    # ---------------- 内部实现 ----------------
    def _locate(self, ms: int) -> int:
        """二分查找：最后一个 time_ms <= ms 的行索引。"""
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
        vh = max(self.viewport().height(), 120)
        h = max(self.PAD_Y * 2 + n * self.LINE_H, vh)
        self._canvas.setMinimumSize(0, h)
        self._canvas.resize(max(self.viewport().width(), 100), h)

    def _scroll_to(self, idx: int):
        bar = self.verticalScrollBar()
        view_h = self.viewport().height()
        y_center = self.PAD_Y + idx * self.LINE_H + self.LINE_H // 2
        target = max(0, min(int(y_center - view_h / 2),
                            max(0, self._canvas.height() - view_h)))
        self._anim.stop()
        self._anim.setStartValue(float(bar.value()))
        self._anim.setEndValue(float(target))
        self._anim.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh_layout()

    # ---------------- 绘制 ----------------
    def paint_canvas(self, canvas: QWidget):
        p = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        w, h = canvas.width(), canvas.height()

        if not self.lines:                     # 占位状态
            p.setPen(QColor("#93a1bb"))
            f = QFont(); f.setPixelSize(13)
            p.setFont(f)
            p.drawText(QRectF(14, 0, w - 28, h),
                       Qt.AlignCenter | Qt.TextWordWrap, self.status)
            p.end()
            return

        for i, line in enumerate(self.lines):
            y_top = self.PAD_Y + i * self.LINE_H
            if y_top + self.LINE_H < 0 or y_top > h:      # 视口外裁剪
                continue
            is_active = (i == self.active)
            dist = abs(i - self.active) if self.active >= 0 else 3
            alpha = 255 if is_active else max(
                self.MIN_ALPHA, 205 - self.FADE_STEP * dist)
            f = QFont()
            f.setPixelSize(17 if is_active else 14)
            f.setBold(is_active)
            p.setFont(f)
            if is_active:
                grad = QLinearGradient(0, y_top, w, y_top + self.LINE_H)
                grad.setColorAt(0.0, self.ACCENT_TOP)
                grad.setColorAt(1.0, self.ACCENT_BOT)
                p.setPen(QPen(QBrush(grad), 1))
            else:
                p.setPen(QColor(232, 236, 244, alpha))
            p.drawText(QRectF(10, y_top, w - 20, self.LINE_H),
                       Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextSingleLine,
                       line.text or "· · ·")
        p.end()

    def handle_click(self, y: int):
        """点击行 → 跳到该行时间点（提前 150ms 更跟手）。"""
        if not self.synced or not self.lines:
            return
        idx = int((y - self.PAD_Y) // self.LINE_H)
        if 0 <= idx < len(self.lines):
            t = self.lines[idx].time_ms
            if t >= 0:
                self.seek_requested.emit(max(0, t - 150))


# ======================================================================
# [6] 主窗口
# ======================================================================
class MainWindow(QWidget):
    library_ready = pyqtSignal(list, str)
    cover_ready   = pyqtSignal(object, object)
    lyrics_ready  = pyqtSignal(object, object)   # (track, result_dict)

    ACCENT_1 = "#6ea8ff"
    ACCENT_2 = "#8b7bff"

    MODE_ORDER = ["list", "one", "seq"]
    MODE_META = {
        "list": ("🔁", "列表循环"),
        "one":  ("🔂", "单曲循环"),
        "seq":  ("➡", "顺序播放"),
    }

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("极简音乐播放器")
        self.resize(360, 620)
        self.setMinimumSize(320, 520)

        self.tracks: List[Track] = []
        self.index = -1
        self._drag_offset = None
        self._pending_src = ""
        self.play_mode = "list"
        # ★ 订阅持久化状态
        self.subs: List[dict] = ConfigStore.load_subs()
        self.current_sub_url = ConfigStore.get_current()
        self.core = PlayerCore(self)
        # ★ 歌词状态：结果缓存（键 = 标题+歌手），避免重复请求
        self._lyrics_cache = {}

        self._build_ui()
        self._wire()
        self.core.set_volume(self.slider.value())
        self.setStyleSheet(GLASS_QSS)
        self._apply_mode_ui()
        # ★ 启动稍等片刻（让窗口先画出来）再自动加载上次订阅
        QTimer.singleShot(200, self._auto_load_last)

    # ---------------- 窗口本体绘制 ----------------
    def paintEvent(self, _):
        paint_glass_backdrop(self)

    # ---------------- 拖拽 ----------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, _):
        self._drag_offset = None

    # ---------------- 控件工厂 ----------------
    def _btn(self, text: str, variant: str = "", danger: bool = False) -> QPushButton:
        return make_btn(text, variant, danger)

    def _glass_slider(self) -> QSlider:
        return make_glass_slider()

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ===== 顶部栏：App 名 + 设置 + 最小化 + 关闭 =====
        head = QHBoxLayout()
        lbl_app = QLabel("♪ Mini Player"); lbl_app.setObjectName("app_title")
        self.btn_expand = self._btn("⛶", "win")           # ★ 放大按钮
        self.btn_expand.setToolTip("沉浸式歌词大屏（Apple Music 风格）")
        self.btn_settings = self._btn("⚙", "win")          # ★ 设置按钮
        self.btn_settings.setToolTip("设置 · 订阅管理")
        head.addWidget(lbl_app)
        head.addStretch(1)
        head.addWidget(self.btn_expand)
        head.addWidget(self.btn_settings)
        root.addLayout(head)

        # ===== 卡片①：订阅源 =====
        c_src = QFrame(); c_src.setObjectName("card")
        lay = QHBoxLayout(c_src)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        self.input_source = QLineEdit(); self.input_source.setObjectName("source")
        self.input_source.setPlaceholderText("library.json 的 URL 或本地路径…")
        self.btn_load = self._btn("加载订阅", "pill")
        lay.addWidget(self.input_source, 1)
        lay.addWidget(self.btn_load)
        root.addWidget(c_src)

        # ===== 卡片②：正在播放 =====
        c_now = QFrame(); c_now.setObjectName("card")
        lay = QHBoxLayout(c_now)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(16)
        self.label_cover = QLabel()
        self.label_cover.setFixedSize(150, 150)
        self.label_cover.setAlignment(Qt.AlignCenter)
        box = QVBoxLayout()
        box.setSpacing(6)
        self.label_title = QLabel("未加载曲目"); self.label_title.setObjectName("title")
        self.label_artist = QLabel("—"); self.label_artist.setObjectName("sub")
        self.label_album = QLabel("—"); self.label_album.setObjectName("sub")
        for lb in (self.label_title, self.label_artist, self.label_album):
            lb.setWordWrap(True)
        box.addWidget(self.label_title)
        box.addWidget(self.label_artist)
        box.addWidget(self.label_album)
        box.addStretch(1)
        lay.addWidget(self.label_cover)
        lay.addLayout(box, 1)
        root.addWidget(c_now)

        # ===== 卡片③：滚动歌词 =====
        c_lrc = QFrame(); c_lrc.setObjectName("card")
        lay_lrc = QVBoxLayout(c_lrc)
        lay_lrc.setContentsMargins(10, 10, 10, 10)
        self.lyrics_panel = LyricsPanel()
        self.lyrics_panel.setToolTip("点击任意歌词行可跳转播放进度")
        lay_lrc.addWidget(self.lyrics_panel)
        root.addWidget(c_lrc, 1)

        # ===== 卡片④：歌单 =====
        c_list = QFrame(); c_list.setObjectName("card")
        lay = QVBoxLayout(c_list)
        lay.setContentsMargins(6, 6, 6, 6)
        self.list_tracks = QListWidget(); self.list_tracks.setObjectName("playlist")
        lay.addWidget(self.list_tracks)
        root.addWidget(c_list, 1)

        # ===== 卡片⑤：控制中心 =====
        c_ctl = QFrame(); c_ctl.setObjectName("card")
        lay = QVBoxLayout(c_ctl)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        prog = QHBoxLayout()
        prog.setSpacing(10)
        self.label_time = QLabel("00:00 / 00:00"); self.label_time.setObjectName("time")
        self.slider_progress = self._glass_slider()
        self.slider_progress.setRange(0, 0)
        self.label_buffer = QLabel(""); self.label_buffer.setObjectName("time")
        prog.addWidget(self.label_time)
        prog.addWidget(self.slider_progress, 1)
        prog.addWidget(self.label_buffer)
        lay.addLayout(prog)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(12)
        self.btn_mode = self._btn("🔁", "circle")
        self.btn_prev = self._btn("⏮", "circle")
        self.btn_toggle = self._btn("▶", "main")
        self.btn_next = self._btn("⏭", "circle")
        ctrl.addWidget(self.btn_mode)
        ctrl.addStretch(1)
        ctrl.addWidget(self.btn_prev)
        ctrl.addWidget(self.btn_toggle)
        ctrl.addWidget(self.btn_next)
        ctrl.addStretch(1)
        lay.addLayout(ctrl)

        vol = QHBoxLayout()
        vol.setSpacing(10)
        ic = QLabel("🔊"); ic.setObjectName("sub")
        self.slider = self._glass_slider()
        self.slider.setRange(0, 100)
        self.slider.setValue(75)
        vol.addWidget(ic)
        vol.addWidget(self.slider)
        lay.addLayout(vol)

        self.label_status = QLabel("就绪 · 拖住空白处可移动窗口")
        self.label_status.setObjectName("sub")
        lay.addWidget(self.label_status)
        root.addWidget(c_ctl)

        for wdg in (self.btn_load, self.btn_expand,
                    self.btn_settings, self.btn_mode, self.btn_prev,
                    self.btn_toggle, self.btn_next, self.slider_progress,
                    self.slider, self.list_tracks):
            wdg.setCursor(Qt.PointingHandCursor)

    # ---------------- 信号连接 ----------------
    def _wire(self):
        self.btn_load.clicked.connect(self._start_load)
        self.list_tracks.itemClicked.connect(self._on_item_click)
        self.btn_mode.clicked.connect(self.cycle_play_mode)
        self.btn_prev.clicked.connect(self.prev_track)
        self.btn_next.clicked.connect(self.next_track)
        self.btn_toggle.clicked.connect(self.core.toggle)
        self.slider.valueChanged.connect(self.core.set_volume)

        self.core.position_changed.connect(self._on_position)
        self.core.duration_changed.connect(self._on_duration)
        self.core.buffer_changed.connect(self._on_buffer)
        self.slider_progress.sliderMoved.connect(self.core.seek)
        self.slider_progress.sliderReleased.connect(
            lambda: self.core.seek(self.slider_progress.value()))

        self.core.state_changed.connect(self._on_state)
        self.core.media_error.connect(lambda m: self.label_status.setText(f"⚠ {m}"))
        self.core.media_ended.connect(self._on_media_ended)

        self.library_ready.connect(self._on_library_ready)
        self.cover_ready.connect(self._on_cover_ready)
        self.lyrics_ready.connect(self._on_lyrics_ready)
        self.lyrics_panel.seek_requested.connect(self.core.seek)

        self.btn_settings.clicked.connect(self.open_settings)    # ★
        self.btn_expand.clicked.connect(self.open_float_lyrics)  # ★ 沉浸式大屏

    # ================================================================
    # ★★★ 沉浸式歌词大屏（Apple Music 风格）★★★
    # ================================================================
    def open_float_lyrics(self):
        """打开沉浸式大屏窗口，并同步当前播放状态。"""
        if FloatLyricsWindow is None:
            QMessageBox.information(self, "提示", "floatlyrics 模块缺失，无法打开大屏")
            return
        if not hasattr(self, "_float_win") or self._float_win is None:
            self._float_win = FloatLyricsWindow()
            # 大屏控制信号 → 主窗口播放内核
            self._float_win.track_selected.connect(self.play_index)
            self._float_win.seek_requested.connect(self.core.seek)
            self._float_win.play_toggled.connect(self.core.toggle)
            self._float_win.next_clicked.connect(self.next_track)
            self._float_win.prev_clicked.connect(self.prev_track)
        self._sync_float_window()
        self._float_win.show()
        self._float_win.raise_()
        self._float_win.activateWindow()

    def _sync_float_window(self):
        """把当前曲目 / 列表 / 进度 / 歌词全量同步到大屏。"""
        win = getattr(self, "_float_win", None)
        if win is None:
            return
        if 0 <= self.index < len(self.tracks):
            track = self.tracks[self.index]
            pix = self.label_cover.pixmap()
            win.update_track_info(track, pix if pix else QPixmap())
        else:
            win.update_track_info(Track(), QPixmap())
        win.update_playlist(self.tracks, self.index)
        win.update_playback_state(
            self.core.player.state() == QMediaPlayer.PlayingState)
        pos = int(self.core.player.position())
        dur = max(int(self.core.player.duration()), 1)
        win.update_position(pos, dur)
        if self.lyrics_panel.lines:
            win.set_lyrics(self.lyrics_panel.lines, self.lyrics_panel.synced)
        elif self.lyrics_panel.status:
            win.show_lyrics_status(self.lyrics_panel.status)

    # ================================================================
    # ★★★ 订阅管理与自动加载 ★★★
    # ================================================================
    def open_settings(self):
        dlg = SettingsDialog(self, self.subs, self.current_sub_url)
        dlg.applied.connect(self._on_sub_applied)
        dlg.exec_()

    def _on_sub_applied(self, name: str, url: str):
        self.current_sub_url = url
        self.input_source.setText(url)
        self._start_load()                        # 立即拉取新订阅

    def _auto_load_last(self):
        """启动时恢复上次订阅；没有则给出引导文案。"""
        target = None
        if self.subs:
            target = (next((s for s in self.subs
                            if s["url"] == self.current_sub_url), None)
                      or self.subs[0])
        if not target:
            self.label_status.setText("💡 点右上角 ⚙ 添加订阅，或在上方粘贴链接后加载")
            return
        self.input_source.setText(target["url"])
        self._start_load()                        # 静默自动加载

    def _remember_subscription(self, url: str):
        """成功加载后把该来源收入订阅库并设为默认。"""
        url = (url or "").strip()
        if not url:
            return
        for sub in self.subs:
            if sub["url"] == url:
                break
        else:
            self.subs.append({"name": derive_sub_name(url), "url": url})
        self.current_sub_url = url
        ConfigStore.save_subs(self.subs)
        ConfigStore.set_current(url)

    # ================================================================
    # 播放模式系统
    # ================================================================
    def cycle_play_mode(self):
        i = self.MODE_ORDER.index(self.play_mode)
        self.play_mode = self.MODE_ORDER[(i + 1) % len(self.MODE_ORDER)]
        self._apply_mode_ui()

    def _apply_mode_ui(self):
        icon, name = self.MODE_META[self.play_mode]
        self.btn_mode.setText(icon)
        self.btn_mode.setToolTip(f"播放模式：{name}（点击切换）")

    def _on_media_ended(self):
        if self.play_mode == "one":
            self.core.seek(0)
            self.core.play()
        elif self.play_mode == "list":
            self.next_track()
        else:
            if self.index < len(self.tracks) - 1:
                self.next_track()
            else:
                self.core.stop()
                self.label_status.setText("✅ 列表播放完毕")

    # ================================================================
    # 订阅加载
    # ================================================================
    def _start_load(self):
        src = self.input_source.text().strip()
        if not src:
            QMessageBox.information(self, "提示", "请先填写订阅链接或本地路径")
            return
        self.btn_load.setEnabled(False)
        self.btn_load.setText("加载中…")
        self.label_status.setText("正在拉取订阅…")
        self._pending_src = src                   # ★ 记录本次来源，成功后入库
        threading.Thread(target=self._load_worker, args=(src,), daemon=True).start()

    def _load_worker(self, src: str):
        tracks = LibraryLoader.load(src)
        err = "" if tracks else "未取到任何歌曲，请检查链接与 JSON 结构"
        self.library_ready.emit(tracks, err)

    def _on_library_ready(self, tracks, err):
        self.btn_load.setEnabled(True)
        self.btn_load.setText("加载订阅")
        if err:
            self.label_status.setText(f"⚠ {err}")
            return
        self._remember_subscription(self._pending_src)   # ★ 成功才入库
        self.core.stop()
        self.tracks, self.index = tracks, -1
        self.list_tracks.clear()
        self.lyrics_panel.show_status("播放歌曲后在此显示歌词")
        for i, t in enumerate(tracks):
            item = QListWidgetItem(f"{i:>2}. {t.display}  ·  {t.album}")
            item.setData(Qt.UserRole, i)
            self.list_tracks.addItem(item)
        n = len(tracks)
        self.label_status.setText(f"✅ 已加载 {n} 首")

    # ================================================================
    # 播放控制
    # ================================================================
    def _on_item_click(self, item):
        self.play_index(item.data(Qt.UserRole))

    def play_index(self, idx: int):
        if not (0 <= idx < len(self.tracks)):
            return
        self.index = idx
        self.list_tracks.setCurrentRow(idx)
        track = self.tracks[idx]
        if self.core.load(track):
            self.core.play()
            self._show_info(track)

    def next_track(self):
        if self.tracks:
            self.play_index((self.index + 1) % len(self.tracks))

    def prev_track(self):
        if self.tracks:
            self.play_index((self.index - 1) % len(self.tracks))

    def _on_state(self, playing: bool):
        self.btn_toggle.setText("⏸" if playing else "▶")

    # ================================================================
    # 信息与封面
    # ================================================================
    @staticmethod
    def _rounded(src: QPixmap, radius: int = 18) -> QPixmap:
        out = QPixmap(src.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, src.width(), src.height(), radius, radius)
        p.setClipPath(path)
        p.drawPixmap(0, 0, src)
        p.end()
        return out

    def _show_info(self, track: Track):
        self.label_title.setText(track.title)
        self.label_artist.setText(f"🎤 {track.artist}")
        self.label_album.setText(f"💿 {track.album}")
        self.slider_progress.setRange(0, 0)
        self.slider_progress.setValue(0)
        self.label_time.setText("00:00 / 00:00")
        self.label_buffer.setText("")
        self._apply_cover(track, None)
        self._start_lyrics_fetch(track)           # ★ 异步拉取歌词
        if track.cover:
            threading.Thread(target=self._cover_worker, args=(track,), daemon=True).start()

    def _cover_worker(self, track: Track):
        self.cover_ready.emit(track, CoverProvider.fetch_bytes(track.cover))

    def _on_cover_ready(self, track, data):
        if 0 <= self.index < len(self.tracks) and self.tracks[self.index] is track:
            self._apply_cover(track, data)

    def _apply_cover(self, track, data):
        pix = QPixmap()
        if data is not None:
            pix.loadFromData(data)
        if pix.isNull():
            pix = CoverProvider.placeholder(track.title[:1] or "♪", track.artist)
        scaled = pix.scaled(self.label_cover.size(),
                            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        self.label_cover.setPixmap(self._rounded(scaled, 18))

    # ================================================================
    # ★ 歌词：后台线程异步拉取 + 曲目一致性校验防串台
    # ================================================================
    def _start_lyrics_fetch(self, track: Track):
        self.lyrics_panel.show_status("正在搜索歌词…")
        threading.Thread(target=self._lyrics_worker, args=(track,),
                         daemon=True).start()

    def _lyrics_worker(self, track: Track):
        key = (track.title, track.artist)
        result = self._lyrics_cache.get(key)
        if result is None:
            if LrclibProvider is None:
                result = {"lines": [], "synced": False, "score": 0.0,
                          "matched_title": "", "matched_artist": "",
                          "reason": "lrcrequest 模块缺失"}
            else:
                try:
                    result = LrclibProvider.fetch_best(track.title, track.artist)
                except Exception as e:
                    print("[Lrc] 获取失败:", e)
                    result = {"lines": [], "synced": False, "score": 0.0,
                              "matched_title": "", "matched_artist": "",
                              "reason": "歌词服务暂不可用"}
                if len(self._lyrics_cache) > 200:     # 简易缓存上限
                    self._lyrics_cache.clear()
                self._lyrics_cache[key] = result
        self.lyrics_ready.emit(track, result)         # 信号回 UI 线程渲染

    def _on_lyrics_ready(self, track, result):
        # 一致性校验：快速切歌时丢弃过期结果，防止歌词串台
        if not (0 <= self.index < len(self.tracks)) \
                or self.tracks[self.index] is not track:
            return
        lines = result.get("lines") or []
        win = getattr(self, "_float_win", None)      # ★ 大屏歌词同步
        if not lines:
            status = f"♪ {result.get('reason') or '暂无匹配歌词'}"
            self.lyrics_panel.show_status(status)
            if win is not None:
                win.show_lyrics_status(status)
            return
        self.lyrics_panel.set_lyrics(lines, bool(result.get("synced")))
        if win is not None:
            win.set_lyrics(lines, bool(result.get("synced")))

    # ================================================================
    # 进度条 / 时间 / 缓冲
    # ================================================================
    @staticmethod
    def _fmt_ms(ms) -> str:
        s = max(0, int(ms) // 1000)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _on_position(self, ms):
        if not self.slider_progress.isSliderDown():
            self.slider_progress.setValue(int(ms))
        self.label_time.setText(
            f"{self._fmt_ms(ms)} / {self._fmt_ms(self.slider_progress.maximum())}")
        self.lyrics_panel.update_position(int(ms))    # ★ 歌词实时同步
        win = getattr(self, "_float_win", None)       # ★ 大屏进度实时同步
        if win is not None and win.isVisible():
            win.update_position(int(ms), max(self.slider_progress.maximum(), 1))

    def _on_duration(self, ms):
        self.slider_progress.setRange(0, max(int(ms), 1))

    def _on_buffer(self, percent):
        self.label_buffer.setText("" if percent >= 100 else f"缓冲 {percent}%")


# ======================================================================
# 入口
# ======================================================================
def main():
    try:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    except Exception:
        pass
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()