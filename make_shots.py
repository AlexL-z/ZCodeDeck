"""生成文章配图:真实 windows 平台渲染(2x 高清),真实组件+当日真实数据。

offscreen 平台无完整字体引擎,中文会渲染成乱码,因此必须用默认
windows 平台;窗口不 show 直接 grab,不闪屏。
用法: py -3 make_shots.py   输出: docs/配图/配图*.png
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_SCALE_FACTOR", "2")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from zcode_deck import DeckWindow, EdgeDot  # noqa: E402

OUT = Path(__file__).resolve().parent / "docs" / "配图"
REAL = {
    "mine": "sess_ee6dc919-5b00-4abd-b338-7bd7a90fcff5",
    "mba": "sess_ecec3cee-7fed-432d-a0be-99a7b918656a",
    "lottery": "sess_23ce0cf1-6e7b-4fcd-8c8f-0d90d24ee1f1",
}


def settle(app, sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    w = DeckWindow()
    w.server.start()
    # 不 show,先试无窗抓取;空白则挪到屏外显示再抓
    pm = w.grab()
    if pm.isNull() or pm.toImage().colorCount() == 0:
        w.show()
        w.move(-3000, -3000)
    settle(app, 5)  # 等额度/用量加载

    now = int(time.time() * 1000)
    w.state.add_event({"event": "PreToolUse", "session_id": REAL["mine"],
                       "tool": "Bash", "ts": now})
    w.state.add_event({"event": "UserPromptSubmit", "session_id": REAL["mba"],
                       "ts": now - 130_000})
    w.state.add_event({"event": "Stop", "session_id": REAL["lottery"],
                       "ts": now - 500_000})
    settle(app, 2.5)
    w._sync_sessions()
    w._relayout()
    settle(app, 1.5)
    w.grab().save(str(OUT / "配图1_会话看板.png"))

    pid = w.state.new("permission", {
        "tool": "Bash", "wait": 300,
        "detail": "command: py -3 make_release.py 打包发布版本"})
    threading.Thread(target=lambda: w.state.wait("permission", pid, 60),
                     daemon=True).start()
    w._poll()
    settle(app, 1.5)
    for card in w.cards.values():
        card.update_countdown()
    w.grab().save(str(OUT / "配图2_审批卡.png"))
    w.state.resolve("permission", pid, {"decision": "allow", "reason": "shot"})
    settle(app, 1.0)

    dot = EdgeDot()
    dot.set_color("#3ecf8e")
    dot._hover = True
    pm_dot = QPixmap(160, 240)
    pm_dot.fill(QColor("#20232a"))
    p = QPainter(pm_dot)
    dot.resize(EdgeDot.DOT, EdgeDot.DOT)
    p.drawPixmap(65, 90, dot.grab())
    p.end()
    pm_dot.save(str(OUT / "配图3_边缘圆钉.png"))

    w.server.stop()
    from PySide6.QtGui import QImage
    for f in sorted(OUT.glob("配图*.png")):
        img = QImage(str(f))
        print(f"{f.name}: {img.width()}x{img.height()} "
              f"{f.stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
