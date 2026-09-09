"""今日使用日报:按任务聚合当日消耗,渲染可分享的暗色长图 PNG。

数据全部来自本地库(turn_usage × session),按日 0 点切窗;
渲染用 QWidget 自绘,游标式排版(段落推进),杜绝区块越界。
文案口吻:AI 牛马战报,调侃但不说教。
"""
import datetime
import sqlite3
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

DB_PATH = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"

BG = "#16181d"
FG = "#e8eaed"
MUTED = "#8a919e"
FAINT = "#5a616c"
ACCENT = "#4da3ff"
QUIP = "#f2a65a"
BAR_COLORS = ["#4da3ff", "#3ecf8e", "#9d7bff", "#4dd0e1", "#f2a65a"]
W = 900
PAD = 64

ALIAS = [
    ("制作Zcode桌面悬浮监控控制窗口", "ZCodeDeck 悬浮窗"),
    ("Zcode桌面悬浮监控控制窗口", "ZCodeDeck 悬浮窗"),
    ("MBA迎新晚会歌舞串烧背景视频剪辑", "MBA晚会视频"),
    ("安卓APK应用", "手机端"),
    ("APK应用", "手机端"),
    ("同步看板数据", "天地人看板"),
    ("同步看板", "天地人看板"),
    ("羽毛球赛赞助清单", "羽毛球赞助"),
]
# 项目级截断:分享物只报项目名,任务细节(如忌讳号规则)不上图
PROJECT_TRUNC = ["大屏抽奖", "959A晚会", "量化工程"]
STRIP_PREFIX = ("开发", "制作", "设计", "进行", "新增")
STRIP_SUFFIX = ("整理", "优化", "迭代", "讨论", "处理")


def _fmt(n) -> str:
    n = n or 0
    if n >= 1e8:
        return f"{n / 1e8:.2f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return f"{n:,}"


def _units(t: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in t)


def _cap(t: str, limit: int) -> str:
    if _units(t) <= limit:
        return t
    if " " in t:  # 拉丁+中文混合时优先保留完整词
        head = t.split(" ")[0]
        if 5 <= _units(head) <= limit:
            return head
    out, acc = "", 0
    for ch in t:
        w = 2 if ord(ch) > 0x2E80 else 1
        if acc + w > limit:
            return out + "…"
        out += ch
        acc += w
    return out


def _theme(raw: str) -> str:
    """任务主题提炼:别名归一(宽上限);未命中走通用压缩(窄上限,
    去动词前后缀,少暴露过程细节)。"""
    t = (raw or "").strip().replace("\\", "/").split("/")[-1]
    aliased = False
    for a, b in ALIAS:
        if a in t:
            t = t.replace(a, b)
            aliased = True
    for proj in PROJECT_TRUNC:
        if t.startswith(proj):
            t = proj
            return t
    for p in STRIP_PREFIX:
        if t.startswith(p):
            t = t[len(p):]
            break
    for s in STRIP_SUFFIX:
        if t.endswith(s) and _units(t) > 10:
            t = t[: -len(s)]
            break
    if t.startswith("手机端") and "看板" in t:
        t = t.replace("手机端", "", 1) + "手机端"
    # 句中"时"多为分句标记(如"记账时隐藏"),超长时删除以保留主干;
    # 前后皆汉字才删,避开"临时"这类词
    if _units(t) > 12 and "时" in t[1:-1]:
        i = t.index("时", 1)
        if ord(t[i - 1]) > 0x2E80 and ord(t[i + 1]) > 0x2E80:
            t = t[:i] + t[i + 1:]
    return _cap(t, 18 if aliased else 12)


def _quip(task: dict, total: int) -> str:
    """今日主力的调侃式一句话:按占比与轮数分档拼装。"""
    pct = task["tokens"] * 100 // max(total, 1)
    turns = task["turns"]
    if pct >= 50:
        a = f"独吞 {pct}% 弹药,其他任务只能喝汤"
    elif pct >= 30:
        a = f"{pct}% 的额度都喂给了它,今天是它的主场"
    elif pct >= 10:
        a = f"稳稳吃下 {pct}%,低调的大胃王"
    else:
        a = f"只占 {pct}%,蚊子腿也是肉"
    if turns >= 15:
        b = f"{turns} 轮对话,今日话痨之王"
    elif turns >= 8:
        b = f"你来我往 {turns} 回合,聊出真感情"
    elif turns >= 3:
        b = f"{turns} 轮拿捏,节奏大师"
    else:
        b = f"{turns} 轮速战速决,深藏功与名"
    return a + "," + b


def today_by_task() -> dict:
    today0 = int(datetime.datetime.combine(
        datetime.date.today(), datetime.time.min).timestamp() * 1000)
    tasks = []
    total_turns = 0
    try:
        con = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=0.5)
        try:
            rows = con.execute(
                "SELECT t.session_id,"
                " COALESCE(NULLIF(s.title,''), s.directory, t.session_id),"
                " SUM(t.input_tokens + t.output_tokens), COUNT(*)"
                " FROM turn_usage t LEFT JOIN session s ON s.id = t.session_id"
                " WHERE t.started_at >= ?"
                " GROUP BY t.session_id ORDER BY 3 DESC", (today0,)).fetchall()
        finally:
            con.close()
        for sid, title, tokens, turns in rows:
            tasks.append({"sid": sid, "title": _theme(str(title)),
                          "tokens": int(tokens or 0), "turns": int(turns)})
            total_turns += int(turns or 0)
    except Exception:
        pass
    return {"tasks": tasks,
            "total": sum(t["tokens"] for t in tasks),
            "turns": total_turns}


class ReportCard(QWidget):
    """纯自绘长图:游标式排版,三个一级模块统一标题格式。"""

    def __init__(self, data: dict):
        super().__init__()
        self.data = data
        shown = data["tasks"][:5]
        others = data["tasks"][5:]
        rows = shown[:]
        if others:
            rows.append({"title": f"其他 {len(others)} 个任务",
                         "tokens": sum(t["tokens"] for t in others),
                         "turns": sum(t["turns"] for t in others),
                         "sid": ""})
        self.rows = rows
        self.n_tasks = len(data["tasks"])
        self.y_bars_end = 640 + len(rows) * 78
        self.H = self.y_bars_end + 350
        self.resize(W, self.H)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(BG))
        d = self.data
        weekday = "一二三四五六日"[datetime.date.today().weekday()]

        def font(px, bold=False):
            f = QFont("Microsoft YaHei UI")
            f.setPixelSize(px)
            f.setBold(bold)
            return f

        def section(y, text):
            """一级模块标题:蓝色短竖标 + 30px 加粗,三处统一。"""
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(QRectF(PAD, y - 24, 8, 32), 4, 4)
            p.setFont(font(30, True))
            p.setPen(QColor(FG))
            p.drawText(PAD + 24, y, text)

        # 顶部:徽标 + 日期(含生成时间)
        p.setFont(font(22, True))
        p.setPen(QColor(ACCENT))
        p.drawText(PAD, 96, "● ZCodeDeck")
        p.setFont(font(22))
        p.setPen(QColor(MUTED))
        date_txt = (datetime.date.today().strftime("%Y.%m.%d")
                    + f" 周{weekday} "
                    + datetime.datetime.now().strftime("%H:%M"))
        p.drawText(W - PAD - QFontMetrics(font(22)).horizontalAdvance(date_txt),
                   96, date_txt)
        p.setFont(font(56, True))
        p.setPen(QColor(FG))
        p.drawText(PAD, 196, "今日AI牛马战报")

        # 模块一:今日共消耗
        section(300, "今日共消耗")
        p.setFont(font(104, True))
        p.setPen(QColor(ACCENT))
        p.drawText(PAD, 420, _fmt(d["total"]))
        p.setFont(font(26))
        p.setPen(QColor(MUTED))
        p.drawText(PAD + QFontMetrics(font(104, True)).horizontalAdvance(
            _fmt(d["total"])) + 18, 420, "tokens")

        avg = _fmt(d["total"] / d["turns"]) if d["turns"] else "0"
        chips = [f"{d['turns']} 轮对话", f"{self.n_tasks} 个任务",
                 f"平均 {avg}/轮"]
        cx = PAD
        p.setFont(font(22))
        for c in chips:
            cw = QFontMetrics(font(22)).horizontalAdvance(c) + 36
            rect = QRectF(cx, 456, cw, 46)
            p.setPen(QPen(QColor("#2e323b"), 1))
            p.setBrush(QColor("#1d2026"))
            p.drawRoundedRect(rect, 23, 23)
            p.setPen(QColor("#9aa2ad"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, c)
            cx += cw + 14

        # 模块二:额度分布
        section(580, "今天的额度都给了谁")
        y = 640
        top = max((r["tokens"] for r in self.rows), default=0) or 1
        for i, r in enumerate(self.rows):
            title = r["title"]
            pct = r["tokens"] * 100 // max(d["total"], 1)
            bc = QColor(BAR_COLORS[i % len(BAR_COLORS)]) if i < 3 \
                else QColor("#3a3f47")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bc)
            p.drawEllipse(QPointF(PAD + 12, y + 22), 13, 13)
            p.setFont(font(17, True))
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(PAD - 1, y + 8, 26, 26),
                       Qt.AlignmentFlag.AlignCenter, str(i + 1))
            p.setFont(font(24))
            p.setPen(QColor("#c9ced6"))
            p.drawText(PAD + 40, y + 30, title)
            right = f"{_fmt(r['tokens'])} · {pct}%"
            p.setPen(QColor(MUTED))
            p.drawText(W - PAD - QFontMetrics(font(24)).horizontalAdvance(right),
                       y + 30, right)
            track = QPen(QColor("#26292f"), 14)
            track.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(track)
            p.drawLine(PAD + 6, y + 58, W - PAD - 6, y + 58)
            fill = QPen(QColor(BAR_COLORS[i % len(BAR_COLORS)]), 14)
            fill.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(fill)
            w_line = int((W - PAD * 2 - 12) * r["tokens"] / top)
            if w_line > 4:
                p.drawLine(PAD + 6, y + 58, PAD + 6 + w_line, y + 58)
            y += 78

        # 模块三:今日主力(调侃一句话)
        if d["tasks"]:
            t0 = d["tasks"][0]
            section(y + 60, "今日主力")
            p.setFont(font(34, True))
            p.setPen(QColor(ACCENT))
            p.drawText(PAD, y + 118, t0["title"])
            p.setFont(font(26))
            p.setPen(QColor(QUIP))
            p.drawText(PAD, y + 168, _quip(t0, d["total"]))

        # 底部署名:灰色小字斜体,单行
        p.setPen(QPen(QColor("#2a2d35"), 2))
        p.drawLine(PAD, self.H - 120, W - PAD, self.H - 120)
        sig = font(24)
        sig.setItalic(True)
        p.setFont(sig)
        p.setPen(QColor(FAINT))
        p.drawText(PAD, self.H - 64, "ZCodeDeck · Alex打工人万事屋")


def generate() -> Path | None:
    """渲染并保存今日日报,返回 PNG 路径;无数据返回 None。"""
    data = today_by_task()
    if not data["tasks"]:
        return None
    card = ReportCard(data)
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"ZCodeDeck日报_{datetime.date.today():%Y-%m-%d}.png"
    card.grab().save(str(path))
    return path
