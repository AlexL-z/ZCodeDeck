"""ZCodeDeck 悬浮窗主程序:置顶小窗 + 状态灯 + 审批/续跑卡片 + 托盘 + 双用量面板。

状态机事件源:hook 桥接脚本 POST 到本地服务,UI 每 200ms 轮询增量。
状态优先级:待批准 > 待续跑 > 出错 > 执行中 > 思考中 > 已完成 > 空闲。
v0.2.0:官方剩余额度(可选 API key)、续跑键、只读自动放行、位置记忆、开机自启。
"""
import json
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation,
                            QSettings, QSize, QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap, QPen
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QProgressBar, QPushButton, QSystemTrayIcon, QVBoxLayout,
    QWidget,
)

from deck import APP_NAME, PORT, __version__
from deck import autostart, lid, quota, report
from deck.install_hooks import install as install_hooks, is_installed
from deck.server import DeckServer, DeckState
from deck.usage import (discover_sessions, fmt_tokens, latest_completions,
                        sessions_meta, usage_summary)

WIDTH = 324
STATUS_META = {
    "idle":     ("空闲", "#8a919e"),
    "thinking": ("思考中", "#4dd0e1"),   # 青:邻接蓝的"工作系",呼吸动画区分
    "running":  ("执行中", "#4da3ff"),   # 品牌蓝 = 正在干活
    "pending":  ("待批准", "#ffb020"),
    "error":    ("出错", "#ff5d5d"),
    "done":     ("已完成", "#3ecf8e"),   # 绿专属完成
}
DONE_TTL_S = 5.0  # noqa: F841 保留历史常量名,已完成不再回落
IDLE_TTL_S = 240.0        # 空闲态:4 分钟无事件即清(多半只是被查看过)
ACTIVE_TTL_S = 43200.0    # 活跃态:长任务(渲染/过夜)可静默数小时,12 小时兜底防僵尸;陈旧度由行内时间戳表达
SCRIPT_PATH = str(Path(__file__).resolve())


def _hex(c: str, alpha: int = 255) -> QColor:
    color = QColor(c)
    color.setAlpha(alpha)
    return color


class PendingCard(QFrame):
    """待审批卡(工具/计划)或续跑卡。"""

    def __init__(self, pid: str, payload: dict, kind: str, on_decision) -> None:
        super().__init__()
        self.pid = pid
        self.kind = kind
        self._on_decision = on_decision
        tool = payload.get("tool") or "未知操作"
        is_plan = kind == "permission" and tool == "ExitPlanMode"
        if kind == "stop":
            title = "任务已停止,要续跑吗?"
        elif is_plan:
            title = "计划待批准"
        else:
            title = f"待批准 · {tool}"

        self.setObjectName("card")
        font = QFont()
        font.setPixelSize(10)
        fm = QFontMetrics(font)

        if kind == "stop":
            # 续跑是高频短暂提示,做紧凑两行条,减小高度扰动。
            # 上行任务标题(按可用宽像素省略),下行语义行保证完整不截断
            lay = QHBoxLayout(self)
            lay.setContentsMargins(8, 4, 8, 4)
            lay.setSpacing(6)
            left = QVBoxLayout()
            left.setSpacing(1)
            _title = str(payload.get("title") or "").strip()
            if _title:
                font = QFont()
                font.setPixelSize(10)
                tlab = QLabel()
                tlab.setFont(font)
                tlab.setStyleSheet("color:#c9ced6;font-size:10px;")
                avail = WIDTH - 28 - 16 - 12 - 52  # 根边距+卡边距+间距+按钮
                tlab.setText(fm.elidedText(_title,
                                           Qt.TextElideMode.ElideRight, avail))
                tlab.setToolTip(_title)
                left.addWidget(tlab)
            line2 = QHBoxLayout()
            line2.setSpacing(6)
            t = QLabel("⟳ 本轮已结束")
            t.setToolTip("任务结束了一轮(完成或停下);想让它继续就点续跑")
            t.setObjectName("stopTitle")
            line2.addWidget(t)
            self.countdown = QLabel("")
            self.countdown.setStyleSheet("color:#6b7280;font-size:9.5px;")
            line2.addWidget(self.countdown)
            line2.addStretch(1)
            left.addLayout(line2)
            lay.addLayout(left, 1)
            self.yes_btn = QPushButton("续跑")
            self.yes_btn.setObjectName("approve")
            self.yes_btn.clicked.connect(lambda: self._decide("continue"))
            lay.addWidget(self.yes_btn)
            self.expires_at = time.time() + float(payload.get("wait") or 300)
            return

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        head = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("cardTitle")
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)

        detail = payload.get("detail") or "(无预览)"
        preview = QPlainTextEdit(detail)
        preview.setObjectName("preview")
        preview.setReadOnly(True)
        preview.setFixedHeight(96 if is_plan else 44)
        preview.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(preview)
        self.yes_btn = QPushButton("接受计划" if is_plan else "批准")
        self.no_btn = QPushButton("拒绝")
        self.yes_btn.setObjectName("approve")
        self.no_btn.setObjectName("deny")
        self.yes_btn.clicked.connect(lambda: self._decide("allow"))
        self.no_btn.clicked.connect(lambda: self._decide("deny"))

        row = QHBoxLayout()
        row.addWidget(self.yes_btn)
        row.addWidget(self.no_btn)
        self.countdown = QLabel("")
        self.countdown.setStyleSheet("color:#6b7280;font-size:9.5px;")
        row.addWidget(self.countdown)
        row.addStretch(1)
        lay.addLayout(row)
        self.expires_at = time.time() + float(payload.get("wait") or 300)

    def update_countdown(self) -> None:
        secs = max(0, int(self.expires_at - time.time()))
        suffix = "后转回 ZCode" if self.kind == "permission" else "后关闭"
        self.countdown.setText(f"{secs}s {suffix}")

    def _decide(self, decision: str) -> None:
        if self.kind == "stop":
            result = {"decision": "continue"}
        else:
            reason = ("ZCodeDeck 悬浮窗批准" if decision == "allow"
                      else "用户在 ZCodeDeck 悬浮窗拒绝")
            result = {"decision": decision, "reason": reason}
        self._on_decision(self.kind, self.pid, result)


class PaceBar(QProgressBar):
    """进度条 + 可选"应用位置"刻度线(速率指示)。"""

    def __init__(self) -> None:
        super().__init__()
        self.marker_frac = None  # 0..1,None 不画

    def set_marker(self, frac: float | None) -> None:
        self.marker_frac = None if frac is None else max(0.02, min(0.98, frac))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.marker_frac is None:
            return
        p = QPainter(self)
        p.setPen(QPen(_hex("#e8eaed", 220), 1.4))
        x = int(self.width() * self.marker_frac)
        p.drawLine(x, 1, x, self.height() - 2)


class QuotaBarRow(QFrame):
    """一行额度进度条,对齐 ZCode 的展现方式:剩余量作为高亮填充。

    配色按窗口固定(5 小时蓝 / 每周绿);每周窗带速率刻度线与快慢判定:
    刻度线 = 按已过时间推算的"应用剩余"位置,条填充越过刻度即用量超前。
    """

    def __init__(self, item: dict) -> None:
        super().__init__()
        used_pct = float(item.get("percentage") or 0)
        rem_pct = max(0.0, 100.0 - used_pct)
        color = quota.COLOR_BY_UNIT.get(item.get("unit"), "#8a919e")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        name = QLabel(str(item.get("label") or "?"))
        name.setFixedWidth(40)
        name.setStyleSheet("color:#9aa2ad;font-size:10px;")
        lay.addWidget(name)

        self.bar_w = PaceBar()
        self.bar_w.setRange(0, 100)
        self.bar_w.setValue(int(rem_pct))
        self.bar_w.setTextVisible(False)
        self.bar_w.setFixedHeight(6)
        self.bar_w.setStyleSheet(
            f"QProgressBar {{ background: rgba(255,255,255,26);"
            f" border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {color};"
            f" border-radius: 3px; }}")
        lay.addWidget(self.bar_w, 1)

        bits = [f"{rem_pct:.0f}%"]
        tips = []
        if item.get("total"):
            tips.append(f"总量 {fmt_tokens(item['total'])}")
        if item.get("used") is not None:
            tips.append(f"已用 {fmt_tokens(item['used'])}")
        reset_ms = item.get("reset_ms")
        if reset_ms:
            reset = time.strftime("%m-%d %H:%M", time.localtime(reset_ms / 1000))
            tips.append(f"{reset} 重置")

        # 速率只用刻度线表达:填充越过刻度 = 用量超前;文字不加,保持一瞥即得
        pc = quota.pace(item) if item.get("unit") == 6 else None
        if pc is not None:
            # 剩余条坐标系:应用剩余 = 100 − 应用用量
            self.bar_w.set_marker((100 - pc["expected_used_pct"]) / 100)
            tips.append(f"本周已过 {pc['elapsed_pct']:.0f}%,"
                        f"应用已用 {pc['expected_used_pct']:.0f}%")

        summary = QLabel(" ".join(bits))
        summary.setStyleSheet(f"color:{color};font-size:10px;")
        lay.addWidget(summary)
        self.setToolTip(" · ".join(tips))


class SessionRow(QFrame):
    """一行会话状态,固定分列对齐:色点|标题|轮次|状态|时间。

    标题列定宽并按像素省略,保证不同长度标题下,轮次与状态严格垂直对齐。
    未读完成行可点击标记已读(dismiss_requested 携带 sid)。
    """

    dismiss_requested = Signal(str)

    COL_TITLE = 96
    COL_ROUND = 40
    COL_AGO = 40

    def __init__(self, sid: str, dismissable: bool, dot_color: str,
                 title: str, round_txt: str, status_html: str,
                 ago: str) -> None:
        super().__init__()
        self._sid = sid
        self._dismissable = dismissable
        if dismissable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("点击标记已读")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        font = QFont()
        font.setPixelSize(10)

        dot = QLabel("●")
        dot.setFont(font)
        dot.setFixedWidth(12)
        dot.setStyleSheet(f"color:{dot_color};")
        lay.addWidget(dot)

        t = QLabel()
        t.setFont(font)
        t.setFixedWidth(self.COL_TITLE)
        fm = QFontMetrics(font)
        t.setText(fm.elidedText(title, Qt.TextElideMode.ElideRight,
                                self.COL_TITLE - 2))
        t.setStyleSheet("color:#c9ced6;")
        lay.addWidget(t)

        r = QLabel(round_txt)
        r.setFont(font)
        r.setFixedWidth(self.COL_ROUND)
        r.setStyleSheet("color:#8a919e;")
        lay.addWidget(r)

        # 状态列文字按像素省略,杜绝 sizeHint 撑爆行宽挤压前列
        plain = status_html.replace("<b>", "").replace("</b>", "")
        status_w = 324 - 28 - (12 + self.COL_TITLE + self.COL_ROUND
                               + self.COL_AGO) - 4 * 4
        s = QLabel()
        s.setTextFormat(Qt.TextFormat.RichText)
        s.setFont(font)
        elided = fm.elidedText(plain, Qt.TextElideMode.ElideRight, status_w)
        s.setText(f"<b>{elided}</b>" if status_html != plain else elided)
        s.setStyleSheet("color:#8a919e;")
        lay.addWidget(s, 1)

        a = QLabel(ago)
        a.setFont(font)
        a.setFixedWidth(self.COL_AGO)
        a.setAlignment(Qt.AlignmentFlag.AlignRight)
        a.setStyleSheet("color:#6b7280;")
        lay.addWidget(a)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton and self._dismissable):
            self.dismiss_requested.emit(self._sid)


_Z_PIXMAP = None


def draw_z(p: QPainter, cx: float, cy: float, w: float) -> None:
    """在 (cx,cy) 画 Z 字形,宽 w;官方字形优先,几何 Z 兜底。"""
    global _Z_PIXMAP
    if _Z_PIXMAP is None:
        asset = Path(__file__).resolve().parent / "deck" / "assets" / "z_glyph.png"
        _Z_PIXMAP = QPixmap(str(asset)) if asset.exists() else QPixmap()
    if not _Z_PIXMAP.isNull():
        aspect = _Z_PIXMAP.height() / _Z_PIXMAP.width()
        pw, ph = w, w * aspect
        p.drawPixmap(QRectF(cx - pw / 2, cy - ph / 2, pw, ph), _Z_PIXMAP,
                     QRectF(_Z_PIXMAP.rect()))
        return
    h = w * 0.62
    t = h * 0.28
    d = w * 0.30
    from PySide6.QtGui import QPolygonF
    pts = [(0, 0), (w, 0), (w, t), (d, h - t), (w, h - t), (w, h),
           (0, h), (0, h - t), (w - d, t), (0, t)]
    poly = QPolygonF([QPointF(cx - w / 2 + x, cy - h / 2 + y) for x, y in pts])
    p.drawPolygon(poly)


class EdgeDot(QWidget):
    """收起态的边缘状态点:颜色实时跟随状态机,单击展开,可沿边缘拖动。"""

    clicked = Signal()
    DOT = 30

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.DOT, self.DOT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color = "#8a919e"
        self._hover = False
        self._press_at = None
        self._attention = False
        self._phase = 0

    def set_color(self, color: str) -> None:
        if color != self._color:
            self._color = color
            self.update()

    def set_attention(self, on: bool) -> None:
        """有待处理审批时脉冲提醒;不打扰,由用户决定何时展开。"""
        if on != self._attention:
            self._attention = on
            self._phase = 0
            self.update()

    def tick(self) -> None:
        if self._attention:
            self._phase += 1
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._attention:
            on = self._phase % 2 == 0
            r = 13.5 if on else 10.0
            p.setPen(QPen(_hex("#ffb020", 220 if on else 90), 1.6))
        else:
            r = 12.5 if self._hover else 11.0
            p.setPen(QPen(_hex("#e8eaed", 70 if not self._hover else 140), 1.2))
        p.setBrush(_hex(self._color, 235))
        p.drawEllipse(QRectF(self.DOT / 2 - r, self.DOT / 2 - r, 2 * r, 2 * r))
        # 官方 Z 字形,白色,保留状态色圆底
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_hex("#ffffff", 235))
        draw_z(p, self.DOT / 2, self.DOT / 2, r * 1.35)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_at = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_at is None:
            return
        center = event.globalPosition().toPoint() - QPoint(self.DOT // 2,
                                                           self.DOT // 2)
        geo = self.screen().availableGeometry()
        y = max(geo.top() + self.DOT // 2,
                min(geo.bottom() - self.DOT, center.y()))
        self.move(geo.right() - self.DOT - 2, y)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._press_at is not None:
            moved = (event.globalPosition().toPoint() - self._press_at).manhattanLength()
            self._press_at = None
            if moved < 5:
                self.clicked.emit()

    def place(self, y_center: int) -> None:
        geo = self.screen().availableGeometry()
        y = max(geo.top() + self.DOT // 2,
                min(geo.bottom() - self.DOT, int(y_center) - self.DOT // 2))
        self.move(geo.right() - self.DOT - 2, y)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu()
        act = menu.addAction("展开悬浮窗")
        if menu.exec(event.globalPos()) == act:
            self.clicked.emit()


class DeckWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("ZCodeDeck", "ZCodeDeck")
        self.state = DeckState()
        self.server = DeckServer(self.state, PORT)
        self.events_read = 0
        self.sessions: dict[str, dict] = {}
        self.cards: dict[str, PendingCard] = {}
        self._meta: dict[str, dict] = {}   # sid -> {title, turns}
        self._meta_dirty: set = set()
        self.status = "idle"
        self.status_tool = ""
        self._phase = 0.0
        self._quota: dict = {}
        self._drag_at = None
        self._border = _hex("#8a919e", 80)
        self._bg_alpha = 242

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setObjectName("dot")
        self.title = QLabel("ZCode")
        self.title.setObjectName("title")
        self.title.setToolTip(f"{APP_NAME} v{__version__}")
        self.usage_inline = QLabel("")
        self.usage_inline.setStyleSheet("color:#6b7280;font-size:9.5px;")
        self.session_badge = QLabel("")
        self.session_badge.setObjectName("badge")
        self.session_badge.hide()
        self.report_btn = QPushButton("日报")
        self.report_btn.setObjectName("reportBtn")
        self.report_btn.setToolTip("生成今日使用总结长图")
        self.report_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.report_btn.clicked.connect(self._gen_report)
        self.lid_btn = QPushButton("合盖")
        self.lid_btn.setObjectName("lidBtn")
        self.lid_btn.setCheckable(True)
        self.lid_btn.setToolTip(
            "开启后合上笔记本盖子电脑不休眠,AI 长任务继续;" + chr(10)
            + "关闭恢复系统默认;退出 ZCodeDeck 时也会自动恢复")
        self.lid_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lid_btn.toggled.connect(self._set_lid)
        self.collapse_btn = QPushButton("›")
        self.collapse_btn.setObjectName("collapse")
        self.collapse_btn.setToolTip("收起到屏幕边缘")
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.clicked.connect(self.collapse)
        head.addWidget(self.dot)
        head.addWidget(self.title)
        head.addWidget(self.usage_inline, 1)
        head.addWidget(self.session_badge)
        head.addWidget(self.lid_btn)
        head.addWidget(self.report_btn)
        head.addWidget(self.collapse_btn)
        root.addLayout(head)

        # 整体状态不设独立文字行:会话模块逐会话展示,整体态势由
        # 窗框颜色/标题圆点/托盘表达,避免双处展示同一信息
        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(6)
        root.addLayout(self.cards_box)

        # 会话状态模块:每个活跃会话一行(色点 + 状态 + 相对时间)
        self.sessions_box = QVBoxLayout()
        self.sessions_box.setSpacing(2)
        root.addLayout(self.sessions_box)
        self._sessions_sig = None

        # 今日用量已并入标题行(usage_inline),此处不再单列一行

        self.quota_head = QLabel("")
        self.quota_head.setObjectName("quota")
        self.quota_head.setTextFormat(Qt.TextFormat.RichText)
        self.quota_head.linkActivated.connect(
            lambda link: self._open_key_dialog() if link == "key" else None)
        root.addWidget(self.quota_head)
        self.quota_bars_box = QVBoxLayout()
        self.quota_bars_box.setSpacing(3)
        root.addLayout(self.quota_bars_box)
        self._quota_sig = None

        self._make_tray()
        self._load_position()

        # 收起态:边缘状态点 + 动画
        self._collapsed = False
        self._animating = False
        self._expand_pos = None
        self.edge_dot = EdgeDot()
        self.edge_dot.clicked.connect(self.expand)

        self._refresh_usage()
        self._refresh_quota()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(200)

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._tick)
        self.anim_timer.start(350)

        self.usage_timer = QTimer(self)
        self.usage_timer.timeout.connect(self._refresh_usage)
        self.usage_timer.start(30_000)

        self.quota_timer = QTimer(self)
        self.quota_timer.timeout.connect(self._refresh_quota)
        self.quota_timer.start(300_000)

        self.meta_timer = QTimer(self)
        self.meta_timer.timeout.connect(self._topup_meta)
        self.meta_timer.timeout.connect(self._reconcile_discovered)
        self.meta_timer.start(60_000)

    # ---- 托盘 ----

    def _make_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return  # 无托盘环境(无头/RDP)直接跳过
        tray = QSystemTrayIcon(self)
        tray.setIcon(self._tray_icon("#8a919e"))
        menu = QMenu()
        act_toggle = menu.addAction("显示 / 隐藏")
        act_toggle.triggered.connect(self.toggle)
        menu.addSeparator()
        self.act_stop = menu.addAction("续跑键(任务停止后 8 秒内可续跑)")
        self.act_stop.setCheckable(True)
        self.act_stop.setChecked(self.settings.value("toggles/stop", False, bool))
        self.act_stop.toggled.connect(self._set_stop_armed)
        self.act_readonly = menu.addAction("只读操作自动放行")
        self.act_readonly.setCheckable(True)
        self.act_readonly.setChecked(self.settings.value("toggles/readonly", False, bool))
        self.act_readonly.toggled.connect(self._set_allow_readonly)
        self.act_lid = menu.addAction("合盖不休眠(长任务继续)")
        self.act_lid.setCheckable(True)
        self.act_lid.toggled.connect(self.lid_btn.setChecked)
        act_key = menu.addAction("API Key 设置(官方额度)")
        act_key.triggered.connect(self._open_key_dialog)
        self.act_auto = menu.addAction("开机自启")
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(autostart.is_on())
        self.act_auto.toggled.connect(
            lambda on: autostart.enable(SCRIPT_PATH) if on else autostart.disable())
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._quit)
        tray.setContextMenu(menu)
        tray.setToolTip(APP_NAME)
        tray.activated.connect(
            lambda r: self.toggle() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        self.tray = tray
        self._set_stop_armed(self.act_stop.isChecked())
        self._set_allow_readonly(self.act_readonly.isChecked())
        # 合盖状态以事实源(lid_backup.json)为准,恢复界面与系统一致
        armed = lid.is_armed()
        self.lid_btn.blockSignals(True)
        self.lid_btn.setChecked(armed)
        self.lid_btn.blockSignals(False)
        self.act_lid.setChecked(armed)

    def _set_lid(self, on: bool) -> None:
        try:
            result = lid.arm() if on else lid.restore()
        except Exception:
            result = {"ok": False}
        if not result.get("ok"):
            self.lid_btn.blockSignals(True)
            self.lid_btn.setChecked(not on)
            self.lid_btn.blockSignals(False)
            self._ghost("合盖设置失败:powercfg 不可用或被策略限制")
            return
        self._ghost("合盖不休眠已" + ("开启,合上盖子任务继续"
                    if on else "关闭,已恢复系统默认"))

    def _open_key_dialog(self) -> None:
        if ask_api_key(self):
            self._quota_at = 0
            self._refresh_quota()

    def _set_stop_armed(self, on: bool) -> None:
        self.state.stop_armed = bool(on)
        self.settings.setValue("toggles/stop", bool(on))

    def _set_allow_readonly(self, on: bool) -> None:
        self.state.allow_readonly = bool(on)
        self.settings.setValue("toggles/readonly", bool(on))

    def _tray_icon(self, color: str) -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(_hex(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(6, 6, 52, 52)
        p.setBrush(_hex("#ffffff", 235))
        draw_z(p, 32, 32, 30)
        p.end()
        return QIcon(pm)

    # ---- 事件轮询与状态机 ----

    def _poll(self) -> None:
        batch, self.events_read = self.state.drain_events(self.events_read)
        for ev in batch:
            sid = ev.get("session_id") or "?"
            first_sight = sid not in self.sessions
            rec = self.sessions.setdefault(
                sid, {"status": "idle", "tool": "", "ts": 0, "done_at": 0,
                  "read": False})
            kind = ev.get("event")
            rec["ts"] = ev.get("ts") or int(time.time() * 1000)
            if kind == "UserPromptSubmit":
                rec.update(status="thinking", tool="")
            elif kind == "PreToolUse":
                rec.update(status="running", tool=ev.get("tool") or "")
            elif kind == "PostToolUse":
                rec.update(status="thinking", tool="")
            elif kind == "PostToolUseFailure":
                rec.update(status="error", tool=ev.get("tool") or "")
            elif kind == "Stop":
                rec.update(status="done", tool="", done_at=time.time(), read=False)
                self._maybe_refresh_quota()
            elif kind == "SessionStart":
                rec.update(status="idle", tool="")
            # 标题/轮次刷新时机:首次见到该会话,或每轮结束
            #(标题可能此时才生成,轮次+1)
            if first_sight or kind == "Stop":
                self._meta_dirty.add(sid)
                if kind == "Stop":
                    # Stop 事件可能先于该轮 turn_usage 落账,读早一轮;
                    # 延迟二读等落账完成后校正
                    QTimer.singleShot(
                        4000, lambda s=sid: self._meta_dirty.add(s))

        if self._meta_dirty:
            ids = list(self._meta_dirty)
            self._meta_dirty.clear()
            fetched = sessions_meta(ids)
            for sid in ids:
                if sid not in fetched:
                    prev = self._meta.get(sid) or {}
                    fetched[sid] = {"title": prev.get("title") or "新会话",
                                    "turns": prev.get("turns", 0)}
                elif sid in self._meta:
                    # 库瞬时读失败时保留已知标题,避免闪回目录名
                    old = self._meta[sid].get("title")
                    if old and old != "新会话":
                        fetched[sid]["title"] = old
            self._meta.update(fetched)

        pending = self.state.snapshot("permission")
        for pid, payload in pending.items():
            if pid not in self.cards:
                self.cards[pid] = PendingCard(pid, payload, "permission",
                                              self._resolve)
                self.cards_box.addWidget(self.cards[pid])
                self._relayout()
        stops = self.state.snapshot("stop")
        for pid, payload in stops.items():
            if pid not in self.cards:
                payload = dict(payload)
                meta = self._meta.get(payload.get("session_id") or "") or {}
                if meta.get("title"):
                    payload["title"] = meta["title"]
                self.cards[pid] = PendingCard(pid, payload, "stop", self._resolve)
                self.cards_box.addWidget(self.cards[pid])
                self._relayout()
        # 对账:挂起已超时消失的卡(没点)同步移除,避免残留;留一条短暂痕迹
        for pid in list(self.cards):
            if pid not in pending and pid not in stops:
                card = self.cards.pop(pid)
                kind = card.kind
                card.setParent(None)
                card.deleteLater()
                self._relayout()
                if kind == "permission":
                    self._ghost("一个审批已超时关闭;如任务仍在等待,请到 ZCode 内确认")

        # 已完成保持未读态:不自动回落空闲,直到该会话有新动作或被更新的
        # 会话挤出列表;非完成态的陈旧会话仍按时限清理

        self._apply_status()
        self._sync_sessions()
        self._sync_quota_panel()

    def _relayout(self) -> None:
        """卡片与会话行的增删改变内容高度;按 sizeHint 平滑收缩/扩展。

        必须先 invalidate+activate:刚移除子控件时 QWidget.sizeHint()
        可能返回陈旧缓存值,导致 resize 变空操作、高度卡在高处不回落。
        """
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        target = max(120, self.sizeHint().height())
        if abs(target - self.height()) <= 1:
            return
        if getattr(self, "_size_anim", None) is not None:
            try:
                self._size_anim.stop()
            except RuntimeError:
                pass
            self._size_anim = None
        anim = QPropertyAnimation(self, b"size", self)
        anim.setDuration(140)
        anim.setStartValue(self.size())
        anim.setEndValue(QSize(WIDTH, target))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: setattr(self, "_size_anim", None))
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._size_anim = anim

    def _reconcile_discovered(self) -> None:
        """事实源回填与纠偏。

        补上因重启/漏事件从未进场的会话;对已跟踪会话,仅当事实时间
        新于我方最后已知事件时采纳(纠正丢 Stop 事件留下的陈旧思考/执行,
        不误伤真正在思考的新一轮——其 UserPromptSubmit 时间新于完结)。
        """
        try:
            found = discover_sessions()
        except Exception:
            return
        now_s = time.time()
        # 已跟踪的活跃会话:不受窗口限制核对最新完结,纠丢 Stop 的陈旧执行/思考
        active = [sid for sid, r in self.sessions.items()
                  if r["status"] in ("thinking", "running", "error")]
        for sid, done_ms in latest_completions(active).items():
            rec = self.sessions.get(sid)
            if rec is None or done_ms <= rec.get("ts", 0):
                continue
            rec.update(status="done", tool="", ts=done_ms,
                       done_at=now_s, read=False)
            self._meta_dirty.add(sid)
        for sid, seed in found.items():
            fact_ts = seed.get("ts", 0)
            rec = self.sessions.get(sid)
            if rec is None:
                self.sessions[sid] = {
                    "status": seed["status"], "tool": seed.get("tool", ""),
                    "ts": fact_ts or int(now_s * 1000),
                    "done_at": now_s if seed["status"] == "done" else 0,
                    "read": False,
                }
                self._meta_dirty.add(sid)
            elif fact_ts > rec.get("ts", 0):
                rec.update(status=seed["status"], tool=seed.get("tool", ""),
                           ts=fact_ts,
                           done_at=now_s if seed["status"] == "done"
                           else rec.get("done_at", 0))
                if seed["status"] == "done":
                    rec["read"] = False  # 新完成 = 新未读
                self._meta_dirty.add(sid)

    def _topup_meta(self) -> None:
        """周期补读:自愈 Stop 读早、漏事件等造成 的账实漂移。"""
        for sid, r in self.sessions.items():
            if r["status"] != "idle":
                self._meta_dirty.add(sid)

    def _gen_report(self) -> None:
        """生成今日日报长图:保存到 reports/ 并用系统查看器打开。"""
        try:
            path = report.generate()
        except Exception as e:
            self._ghost(f"日报生成失败:{type(e).__name__}")
            return
        if path is None:
            self._ghost("今天还没有可用数据,晚点再来")
            return
        import os
        os.startfile(str(path))  # noqa: Windows 系统查看器

    def _mark_read(self, sid: str) -> None:
        """点击未读完成行:标记已读,行消失;该会话再次完成会重新未读。"""
        rec = self.sessions.get(sid)
        if rec is not None:
            rec["read"] = True
            self._sync_sessions()

    def _sync_sessions(self) -> None:
        """会话模块:分列对齐的任务行,只展示有活动意义的会话。

        纯空闲不占行(点开查看≠运行);全部空闲时只显示一行"空闲"。
        """
        now_ms = int(time.time() * 1000)
        rows = []
        for sid, r in sorted(self.sessions.items(),
                             key=lambda kv: -kv[1]["ts"]):
            if r["status"] == "idle" or (r["status"] == "done" and r.get("read")):
                continue
            if len(rows) >= 4:
                break
            text, color = STATUS_META.get(r["status"], STATUS_META["idle"])
            if r["status"] == "running" and r["tool"]:
                text = f"执行中 · {r['tool']}"
            meta = self._meta.get(sid) or {}
            title = meta.get("title") or "新会话"
            turns = meta.get("turns") or 0
            # 账本只记已完结轮:进行中显示已完结+1(首轮即"第1轮"),
            # 完成态显示已完结数(恰为刚结束的轮)
            n = turns if r["status"] == "done" else turns + 1
            round_txt = f"第{n}轮"
            status_html = f"<b>{text}</b>" if r["status"] == "done" else text
            age_min = max(0, (now_ms - r["ts"]) / 60000)
            if age_min < 1:
                ago = "刚刚"
            elif age_min < 60:
                ago = f"{int(age_min)}分前"
            else:
                ago = f"{int(age_min // 60)}时前"
            rows.append((sid, r["status"] == "done", color, title,
                         round_txt, status_html, ago))
        if not rows:
            rows = [("", False, "#8a919e", "没有任务在运行", "", "", "")]
        sig = tuple(tuple(x) for x in rows)
        if sig == self._sessions_sig:
            return
        self._sessions_sig = sig
        while self.sessions_box.count():
            item = self.sessions_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for row in rows:
            widget = SessionRow(*row)
            widget.dismiss_requested.connect(self._mark_read)
            self.sessions_box.addWidget(widget)
        self._relayout()

    def _sync_quota_panel(self) -> None:
        """额度数据由后台线程更新;按内容签名增量重建进度条。"""
        q = self._quota
        sig = (json.dumps(q.get("limits"), sort_keys=True) if q.get("ok")
               else q.get("error") or q.get("hint") or "")
        if sig == self._quota_sig:
            return
        self._quota_sig = sig
        while self.quota_bars_box.count():
            item = self.quota_bars_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not q.get("ok"):
            color = quota.RED if q.get("error") else "#6b7280"
            if q.get("hint"):
                head = ("<a style='color:#6b7280;text-decoration:none' "
                        "href='key'>官方额度  点此设置 API Key</a>")
            else:
                text = q.get("error") or "查询中…"
                head = (f"<span style='color:{color}'>官方额度  "
                        f"{text[:60]}</span>")
            self.quota_head.setText(head)
            self._relayout()
            return
        level = f" · {q['level'].upper()}" if q.get("level") else ""
        at = f" <span style='color:#6b7280'>({q.get('at', '')})</span>"
        self.quota_head.setText(
            f"<span style='color:#8a919e'>套餐额度{level}</span>{at}")
        for it in q.get("limits", []):
            self.quota_bars_box.addWidget(QuotaBarRow(it))
        self._relayout()

    def _resolve(self, kind: str, pid: str, result: dict) -> None:
        self.state.resolve(kind, pid, result)
        card = self.cards.pop(pid, None)
        if card:
            card.setParent(None)
            card.deleteLater()
            self._relayout()
        self._apply_status()

    def _apply_status(self) -> None:
        now_ms = int(time.time() * 1000)
        def _alive(r) -> bool:
            age = now_ms - r["ts"]
            if r["status"] == "done":
                return True              # 未读完成:只被已读/挤出清除
            if r["status"] == "idle":
                return age < IDLE_TTL_S * 1000
            return age < ACTIVE_TTL_S * 1000  # 思考/执行/出错:容忍长静默

        live = {sid: r for sid, r in self.sessions.items() if _alive(r)}
        self.sessions = live

        if self.cards:
            kinds = {c.kind for c in self.cards.values()}
            status = "pending" if "permission" in kinds else "running"
            tool = ""
        elif any(r["status"] != "idle"
                 and not (r["status"] == "done" and r.get("read"))
                 for r in live.values()):
            # 聚合只看可见会话(与列表同口径):已读的完成不参与,
            # 否则全部读完后边框仍挂在完成档的绿色上
            rank = {"idle": 0, "done": 1, "thinking": 2,
                    "running": 3, "error": 4}
            best = max(
                (r for r in live.values()
                 if r["status"] != "idle"
                 and not (r["status"] == "done" and r.get("read"))),
                key=lambda r: (rank.get(r["status"], 0), r["ts"]))
            status, tool = best["status"], best["tool"]
        else:
            status, tool = "idle", ""
        self.status, self.status_tool = status, tool
        self.session_badge.setText(f"{len(live)} 个会话" if live else "")
        self._render()

    def _render(self, phase: float | None = None) -> None:
        status = self.status
        text, color = STATUS_META.get(status, STATUS_META["idle"])
        if status == "running" and self.status_tool:
            text = f"执行中 · {self.status_tool}"
        if status == "error" and self.status_tool:
            text = f"出错 · {self.status_tool}"

        phase = self._phase if phase is None else phase
        if status == "pending":
            on = int(phase * 2) % 2 == 0
            dot_color = color if on else "#6b5a2a"
            border = _hex(color, 220 if on else 110)
        elif status == "thinking":
            glow = 0.55 + 0.45 * abs(((phase % 2.0) - 1.0))
            dot_color = color
            border = _hex(color, int(90 + 130 * glow))
        else:
            dot_color = color
            if status == "idle":
                border = _hex(color, 70)
            elif status == "done":
                border = _hex(color, 110)  # 完成暗一档,与执行中的绿区分
            else:
                border = _hex(color, 170)

        self.dot.setStyleSheet(f"color: {dot_color};")
        if hasattr(self, "edge_dot"):
            self.edge_dot.set_color(dot_color)
        self._border = border
        if self.tray:
            self.tray.setIcon(self._tray_icon(dot_color))
            self.tray.setToolTip(f"{APP_NAME} · {text}\n{self._quota_tooltip()}")
        self.update()

    def _tick(self) -> None:
        self._phase += 0.35
        self.edge_dot.set_attention(bool(self.cards))
        self.edge_dot.tick()
        for card in self.cards.values():
            card.update_countdown()
        self._render()

    def _ghost(self, text: str) -> None:
        """超时/移除后的短暂提示行,12 秒后自动消失。"""
        lab = QLabel(text)
        lab.setStyleSheet("color:#6b7280;font-size:10px;")
        lab.setWordWrap(True)
        self.cards_box.addWidget(lab)
        self._relayout()

        def _drop():
            lab.setParent(None)
            lab.deleteLater()
            self._relayout()

        QTimer.singleShot(12000, _drop)

    # ---- 本地用量 ----

    def _refresh_usage(self) -> None:
        s = usage_summary()
        if not s.get("ok"):
            self.usage_inline.setText("")
            self.usage_inline.setToolTip("")
            return
        t, h5, d7 = s["today"], s["h5"], s["d7"]
        today_tok = t["input"] + t["output"]
        week_tok = d7["input"] + d7["output"]
        self.usage_inline.setText(
            f"今日 {fmt_tokens(today_tok)} · {t['turns']} 次")
        self.usage_inline.setToolTip(
            f"今日 {fmt_tokens(today_tok)} tokens · {t['turns']} 次对话\n"
            f"近5小时 {fmt_tokens(h5['input'] + h5['output'])} tokens · {h5['turns']} 次\n"
            f"近7天 {fmt_tokens(week_tok)} tokens · {d7['turns']} 次\n"
            "输入含缓存命中;数据来自本地数据库,按 API 口径统计")

    # ---- 官方剩余额度 ----

    def _refresh_quota(self) -> None:
        self._quota_at = time.time()
        def worker():
            result = quota.fetch_quota()
            self._quota = result
            # 网络线程不碰 Qt;下一次 200ms 轮询会带着新数据重建面板
        threading.Thread(target=worker, daemon=True, name="quota").start()

    def _maybe_refresh_quota(self, min_interval: float = 60.0) -> None:
        """额度随消耗变化,会话每轮结束都值得刷新;限流避免打爆接口。"""
        if time.time() - getattr(self, "_quota_at", 0) >= min_interval:
            self._refresh_quota()

    def _quota_tooltip(self) -> str:
        q = self._quota
        if q.get("ok"):
            return "官方额度  " + " · ".join(
                f"{it['label']} {it['percentage']:.0f}%" for it in q.get("limits", []))
        return ""

    # ---- 窗体行为 ----

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(self._border, 1.4))
        p.setBrush(_hex("#16181d", self._bg_alpha))
        p.drawRoundedRect(QRectF(0.7, 0.7, self.width() - 1.4, self.height() - 1.4), 12, 12)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_at = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_at is not None:
            self.move(event.globalPosition().toPoint() - self._drag_at)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_at is not None:
            self._drag_at = None
            self.settings.setValue("pos", self.pos())

    def _load_position(self) -> None:
        pos = self.settings.value("pos")
        if pos is not None:
            self.move(self._clamp_pos(QPoint(pos.x(), pos.y())))
        else:
            geo = self.screen().availableGeometry()
            self.move(geo.right() - WIDTH - 24, geo.top() + 120)

    def _clamp_pos(self, pos: QPoint) -> QPoint:
        """把窗口左上角夹回可用屏幕内,杜绝任何出屏。"""
        geo = self.screen().availableGeometry()
        x = max(geo.left(), min(pos.x(), geo.right() - WIDTH + 1))
        y = max(geo.top(), min(pos.y(), geo.bottom() - self.height() + 1))
        return QPoint(int(x), int(y))

    def toggle(self) -> None:
        if self._collapsed:
            self.expand()
        else:
            self.setVisible(not self.isVisible())

    # ---- 收起 / 展开 ----

    def initial_show(self) -> None:
        """启动时按持久化状态决定显示完整窗还是边缘点。"""
        if self.settings.value("collapsed", False, bool):
            self._collapsed = True
            self.edge_dot.place(self.settings.value("dot_y", 200, int))
            self.edge_dot.show()
        else:
            self.show()

    def collapse(self) -> None:
        if self._collapsed or self._animating or not self.isVisible():
            return
        self._animating = True
        self._collapsed = True
        self.settings.setValue("collapsed", True)
        # 动画会把窗口挪向右缘,先记住真实位置,展开时恢复,防止漂移出屏
        self._expand_pos = self._clamp_pos(self.pos())
        center_y = self.geometry().center().y()
        self.settings.setValue("dot_y", int(center_y))
        end = QPoint(self.x() + 90, self.y())

        def _done():
            self.hide()
            self.setWindowOpacity(1.0)
            self.move(self._expand_pos)  # 复位内部位置,消除动画残留
            self.edge_dot.place(center_y)
            self.edge_dot.setWindowOpacity(0.0)
            self.edge_dot.show()
            fade = QPropertyAnimation(self.edge_dot, b"windowOpacity", self)
            fade.setDuration(140)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            self._animating = False

        self._slide(end, 1.0, 0.0, 170, _done)

    def expand(self) -> None:
        if not self._collapsed or self._animating:
            return
        self._animating = True
        self._collapsed = False
        self.settings.setValue("collapsed", False)
        geo = self.screen().availableGeometry()
        base = self._expand_pos or QPoint(geo.right() - WIDTH - 24,
                                          geo.top() + 120)
        # 展开对齐圆钉:垂直以圆钉为中心,横向用收起前的位置,整体夹回屏内
        dot_cy = (self.edge_dot.geometry().center().y()
                  if self.edge_dot.isVisible()
                  else base.y() + self.height() // 2)
        final = self._clamp_pos(QPoint(base.x(),
                                       int(dot_cy) - self.height() // 2))
        start = QPoint(geo.right() + 2, final.y())
        self.edge_dot.hide()
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()
        self._slide(final, 0.0, 1.0, 210,
                    lambda: self.settings.setValue("pos", self.pos()))

    def _slide(self, end: QPoint, o_from: float, o_to: float,
               ms: int, done) -> None:
        """位相与透明度并行动画;结束后回调。"""
        pos_anim = QPropertyAnimation(self, b"pos", self)
        pos_anim.setDuration(ms)
        pos_anim.setStartValue(self.pos())
        pos_anim.setEndValue(end)
        pos_anim.setEasingCurve(
            QEasingCurve.Type.InCubic if o_to < o_from
            else QEasingCurve.Type.OutCubic)
        op_anim = QPropertyAnimation(self, b"windowOpacity", self)
        op_anim.setDuration(ms)
        op_anim.setStartValue(o_from)
        op_anim.setEndValue(o_to)
        op_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        pos_anim.start()
        op_anim.start()

        def _finish():
            self.setWindowOpacity(o_to)
            self._animating = False
            done()

        op_anim.finished.connect(_finish)

    def _quit(self) -> None:
        try:
            if lid.is_armed():
                lid.restore()  # 带着合盖不休眠退出会偷偷耗电,自动恢复
        except Exception:
            pass
        self.server.stop()
        QApplication.quit()


STYLE = """
#title { color: #e8eaed; font-size: 12px; font-weight: 600; }
#badge { color: #8a919e; font-size: 10px; }
#lidBtn { background: transparent; color: #6b7280;
           border: 1px solid rgba(255,255,255,0.14); border-radius: 8px;
           font-size: 10px; font-weight: 600; padding: 2px 8px; }
#lidBtn:checked { background: rgba(255,176,32,0.18); color: #ffb020;
                  border-color: rgba(255,176,32,0.6); }
#lidBtn:hover { border-color: rgba(255,176,32,0.6); }
#reportBtn { background: rgba(77,163,255,0.16); color: #4da3ff;
             border: 1px solid rgba(77,163,255,0.55); border-radius: 8px;
             font-size: 10px; font-weight: 600; padding: 2px 8px; }
#reportBtn:hover { background: rgba(77,163,255,0.32); }
#collapse { background: transparent; color: #6b7280; border: none;
            font-size: 14px; font-weight: 600; padding: 0 2px; }
#collapse:hover { color: #e8eaed; }
#dot { font-size: 13px; }
#status { font-size: 13px; font-weight: 600; }
#usage, #quota { color: #9aa2ad; font-size: 10.5px; }
#card { background: rgba(255,255,255,14); border: 1px solid rgba(255,176,32,90);
        border-radius: 9px; }
#cardTitle { color: #ffb020; font-size: 11.5px; font-weight: 600; }
#stopTitle { color: #4da3ff; font-size: 11.5px; font-weight: 600; }
#preview { background: rgba(0,0,0,60); color: #c9ced6; font-size: 10.5px;
           border-radius: 6px; padding: 4px; }
QPushButton { border: none; border-radius: 6px; padding: 5px 14px;
              font-size: 11.5px; font-weight: 600; }
#approve { background: #2ea36b; color: white; }
#approve:hover { background: #35bd7c; }
#deny { background: rgba(255,255,255,26); color: #e8eaed; }
#deny:hover { background: rgba(255,93,93,120); }
"""


PRIVACY_LINES = (
    "ZCodeDeck 完全运行在你的电脑上:",
    "· 用量与任务数据只读取本地 ZCode 数据库,不外发",
    "· API Key 仅保存在本机,只用于查询智谱官方额度接口",
    "· 通信端口仅绑定 127.0.0.1,局域网内其他设备不可见",
    "· 无任何统计、埋点或账号系统")
PRIVACY_TEXT = chr(10).join(PRIVACY_LINES)


def key_path():
    from pathlib import Path as _P
    p = _P.home() / ".zcode" / "deck" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def key_configured() -> bool:
    import json as _json
    try:
        return bool(_json.loads(key_path().read_text("utf-8"))
                    .get("bigmodel_api_key"))
    except Exception:
        return False


def ask_api_key(parent=None) -> bool:
    """首次设置/修改 API Key;返回是否保存。可跳过(额度面板降级)。"""
    import json as _json
    dlg = QDialog(parent)
    dlg.setWindowTitle("ZCodeDeck 首次设置")
    dlg.setFixedWidth(420)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(18, 14, 18, 14)
    lay.setSpacing(10)
    head = QLabel("输入智谱 BigModel 订阅的 API Key(个人中心可获取)")
    head.setWordWrap(True)
    head.setStyleSheet("color:#e8eaed;font-size:13px;font-weight:600;")
    lay.addWidget(head)
    edit = QLineEdit()
    edit.setPlaceholderText("格式形如 6510923xxxx.su3l3Dxxxx(可先跳过)")
    edit.setStyleSheet("color:#e8eaed;font-size:12px;")
    lay.addWidget(edit)
    privacy = QLabel(PRIVACY_TEXT)
    privacy.setWordWrap(True)
    privacy.setStyleSheet("color:#8a919e;font-size:10.5px;")
    lay.addWidget(privacy)
    row = QHBoxLayout()
    skip = QPushButton("以后再说")
    skip.setStyleSheet("color:#8a919e;border:none;font-size:12px;")
    save = QPushButton("保存")
    save.setEnabled(False)
    save.setStyleSheet(
        "background:#2ea36b;color:white;border:none;border-radius:6px;"
        "padding:5px 16px;font-size:12px;font-weight:600;")
    row.addStretch(1)
    row.addWidget(skip)
    row.addWidget(save)
    lay.addLayout(row)
    edit.textChanged.connect(lambda t: save.setEnabled(len(t.strip()) > 20))
    result = {"saved": False}

    def _save():
        key = edit.text().strip()
        if len(key) <= 20:
            return
        p = key_path()
        cfg = {}
        try:
            cfg = _json.loads(p.read_text("utf-8"))
        except Exception:
            pass
        cfg["bigmodel_api_key"] = key
        p.write_text(_json.dumps(cfg, indent=2), "utf-8")
        result["saved"] = True
        dlg.accept()

    save.clicked.connect(_save)
    skip.clicked.connect(dlg.reject)
    dlg.setStyleSheet("QDialog{background:#1b1e24;}")
    dlg.exec()
    return result["saved"]


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)

    if not key_configured():
        ask_api_key()

    if not is_installed():
        install_hooks()

    window = DeckWindow()
    window.server.start()
    window.initial_show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
