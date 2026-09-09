"""实机场景摄影:用 HTTP 通道把活体悬浮窗摆进各场景,DPI 感知直抓。

场景:① 多任务看板 ② 审批卡 ③ 续跑条(临时武装 stop 开关,拍完复原)
用法: py -3 make_shots_live.py   输出: docs/配图/实机_*.png
"""
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 物理坐标,防左上角裁切

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "配图"
MY = "sess_ee6dc919-5b00-4abd-b338-7bd7a90fcff5"
MBA = "sess_ecec3cee-7fed-432d-a0be-99a7b918656a"
LOT = "sess_23ce0cf1-6e7b-4fcd-8c8f-0d90d24ee1f1"
BASE = "http://127.0.0.1:17654"


def post(path, payload, timeout=6):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def find_main_hwnd():
    """枚举题为 ZCodeDeck 的窗口,主窗宽≥300;圆钉 53px 会抢 FindWindow。"""
    u = ctypes.windll.user32
    found = []

    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == "ZCodeDeck":
                r = wt.RECT()
                u.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right - r.left >= 300:
                    found.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, ctypes.c_long)
    u.EnumWindows(WNDENUMPROC(cb), 0)
    assert found, "主窗未找到(可能处于收起态,先展开)"
    return found[0]


def grab(name):
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    hwnd = find_main_hwnd()
    rect = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biX", ctypes.c_int32), ("biY", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    hdc = u.GetWindowDC(hwnd)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mem, bmp)
    u.PrintWindow(hwnd, mem, 2)
    bi = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    from PySide6.QtGui import QImage
    img = QImage(buf, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
    out = OUT / name
    img.save(str(out))
    g.DeleteObject(bmp)
    g.DeleteDC(mem)
    u.ReleaseDC(hwnd, hdc)
    print(f"{name}: {w}x{h} {out.stat().st_size // 1024}KB")


def restart_deck():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" "
                    "| Select-Object -ExpandProperty ProcessId "
                    "| ForEach-Object { Stop-Process -Id $_ -Force }"],
                   capture_output=True)
    time.sleep(2)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Start-Process -FilePath "
                    "'C:\\Users\\ROG\\AppData\\Local\\Programs\\Python\\"
                    "Python314\\pythonw.exe' "
                    f"-ArgumentList '{ROOT}\\zcode_deck.py' "
                    f"-WorkingDirectory '{ROOT}' "
                    f"-RedirectStandardError '{ROOT}\\logs\\deck_stderr.log' "
                    f"-RedirectStandardOutput '{ROOT}\\logs\\deck_stdout.log'"],
                   capture_output=True)
    for _ in range(40):
        time.sleep(1)
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2).read()
            return
        except Exception:
            continue
    raise RuntimeError("deck did not come back")


def set_stop_armed(on: bool):
    import winreg
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                         r"Software\ZCodeDeck\ZCodeDeck\toggles")
    winreg.SetValueEx(k, "stop", 0, winreg.REG_SZ, "true" if on else "false")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)

    # 场景一:多任务看板(真任务标题 + 三种状态)
    def ev(event, sid, tool=""):
        post("/event", {"event": event, "session_id": sid,
                        "tool": tool, "ts": now})
    ev("PreToolUse", MY, "Bash")
    ev("UserPromptSubmit", MBA)
    ev("Stop", LOT)
    time.sleep(1.8)
    grab("实机_看板.png")

    # 场景二:审批卡(挂起等待决定,拍完放行)
    import threading
    holder = threading.Thread(
        target=lambda: post("/permission",
                            {"event": "PermissionRequest",
                             "session_id": MY, "tool": "Bash",
                             "detail": "command: py -3 make_release.py 打包发布版本",
                             "wait": 300}, timeout=320),
        daemon=True)
    holder.start()
    time.sleep(1.8)
    grab("实机_审批卡.png")
    post("/resolve", {"kind": "permission", "decision": "allow"})
    time.sleep(1.5)

    # 场景三:续跑条(临时武装,拍完复原重启)
    set_stop_armed(True)
    restart_deck()
    now = int(time.time() * 1000)
    post("/event", {"event": "Stop", "session_id": MBA,
                    "tool": "", "ts": now})
    threading.Thread(
        target=lambda: post("/stop-continue",
                            {"wait": 300, "session_id": MBA}, timeout=320),
        daemon=True).start()
    time.sleep(2.0)
    grab("实机_续跑条.png")
    set_stop_armed(False)
    restart_deck()


if __name__ == "__main__":
    main()
