"""无头端到端回路测试:桥接 → 服务 → 卡片 → 真实按钮点击 → 桥接输出断言。

运行: py -3 tests/test_roundtrip.py
QT_QPA_PLATFORM=offscreen,不依赖屏幕与托盘。七个场景:审批批准/拒绝/超时回退、
续跑、只读放行(开/关)。全部通过打印 ALL PASS 并退出 0。
"""
import faulthandler
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 卡死自诊断:30 秒后转储全部线程栈并退出
faulthandler.dump_traceback_later(30, exit=True)


def log(*args):
    print(*args, flush=True)

from PySide6.QtWidgets import QApplication  # noqa: E402

from zcode_deck import DeckWindow  # noqa: E402

BRIDGE = ROOT / "deck" / "hook_bridge.py"


def run_bridge(mode: str, hook_input: dict, extra_env: dict | None = None) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), mode, mode.capitalize()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=str(ROOT),
        env={**os.environ, **(extra_env or {})})
    proc.stdin.write(json.dumps(hook_input))
    proc.stdin.close()
    return proc


def wait_card(app, window, proc=None, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if window.cards:
            return next(iter(window.cards.values()))
        time.sleep(0.05)
    if proc is not None and proc.poll() is not None:  # 已退出才可安全读管道
        log(f"[diag] bridge rc={proc.returncode} stderr={proc.stderr.read()!r}")
    log(f"[diag] pending={list(window.state.snapshot('permission').keys())} "
        f"stops={list(window.state.snapshot('stop').keys())}")
    raise AssertionError("卡片未在超时内出现")


def main() -> int:
    # 前置守卫:端口上已有活的悬浮窗时,桥接流量会被它截走,测试必假失败
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:17654/health",
                                    timeout=2) as r:
            alive = json.loads(r.read())
    except Exception:
        alive = None
    if alive:
        log(f"[abort] 端口 17654 已有运行中的悬浮窗(v{alive.get('version')}),"
            f"请先退出托盘图标再跑本测试")
        return 2

    app = QApplication([])
    window = DeckWindow()
    window.server.start()

    # 场景一:审批批准路径
    proc = run_bridge("permission", {"session_id": "sess_t", "tool_name": "Bash",
                       "tool_input": {"command": "echo roundtrip"}},
                      {"ZCODEDECK_WAIT": "12"})
    card = wait_card(app, window, proc)
    assert "批准" in card.yes_btn.text()
    card.yes_btn.click()
    out = proc.communicate(timeout=15)[0].strip()
    got = json.loads(out)
    assert got["hookSpecificOutput"]["decision"] == {"behavior": "allow"}, got
    assert got["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    app.processEvents()
    assert not window.cards, "批准后卡片未移除"
    log("PASS 批准路径:", out)

    # 场景二:审批拒绝路径
    proc = run_bridge("permission", {"session_id": "sess_t", "tool_name": "ExitPlanMode",
                       "tool_input": {"plan": "1. 先做 A\n2. 再做 B"}},
                      {"ZCODEDECK_WAIT": "12"})
    card = wait_card(app, window, proc)
    assert "接受计划" in card.yes_btn.text()
    card.no_btn.click()
    out = proc.communicate(timeout=15)[0].strip()
    got = json.loads(out)
    assert got["hookSpecificOutput"]["decision"]["behavior"] == "deny", got
    assert got["hookSpecificOutput"]["decision"]["message"], "拒绝缺少 message"
    app.processEvents()
    assert not window.cards, "拒绝后卡片未移除"
    log("PASS 拒绝路径:", out)

    # 场景三:审批超时回退(无输出退出 0,由 ZCode 回退终端提示)
    proc = run_bridge("permission", {"session_id": "sess_t", "tool_name": "Bash",
                       "tool_input": {"command": "nobody will click"}},
                      {"ZCODEDECK_WAIT": "10"})
    wait_card(app, window, proc)
    deadline = time.time() + 18
    while time.time() < deadline and window.cards:
        app.processEvents()
        time.sleep(0.05)
    out = proc.communicate(timeout=5)[0].strip()
    assert out == "", f"超时路径不应有输出: {out}"
    assert proc.returncode == 0
    assert not window.cards, "超时后卡片应对账移除"
    log("PASS 审批超时回退:无输出,卡片对账移除")

    # 场景四:续跑键(armed 时点击续跑 → Stop hook 要求继续)
    window.state.stop_armed = True
    proc = run_bridge("stop", {"session_id": "sess_t"},
                      {"ZCODEDECK_STOP_WAIT": "10"})
    card = wait_card(app, window, proc)
    assert card.kind == "stop" and "续跑" in card.yes_btn.text()
    card.yes_btn.click()
    out = proc.communicate(timeout=15)[0].strip()
    got = json.loads(out)
    assert got["decision"] == "block" and got["reason"], got
    app.processEvents()
    assert not window.cards, "续跑后卡片未移除"
    log("PASS 续跑路径:", out)

    # 场景五:续跑未 armed → 立即 off,无输出
    window.state.stop_armed = False
    proc = run_bridge("stop", {"session_id": "sess_t"},
                      {"ZCODEDECK_STOP_WAIT": "10"})
    out = proc.communicate(timeout=8)[0].strip()
    assert out == "" and proc.returncode == 0, f"未 armed 应无输出: {out}"
    log("PASS 续跑未开启:静默跳过")

    # 场景六:只读自动放行(开)→ Read 放行,Stop 不受影响
    window.state.allow_readonly = True
    proc = run_bridge("pretooluse", {"session_id": "sess_t", "tool_name": "Read",
                       "tool_input": {"file_path": "x.txt"}})
    out = proc.communicate(timeout=10)[0].strip()
    got = json.loads(out)
    assert got["hookSpecificOutput"]["permissionDecision"] == "allow", got
    assert got["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    log("PASS 只读放行(开):", out)

    # 场景七:只读自动放行(关)→ 无输出;写工具即使在开的状态也不放行
    window.state.allow_readonly = False
    proc = run_bridge("pretooluse", {"session_id": "sess_t", "tool_name": "Read",
                       "tool_input": {"file_path": "x.txt"}})
    out = proc.communicate(timeout=10)[0].strip()
    assert out == "", f"关闭后应无输出: {out}"
    window.state.allow_readonly = True
    proc = run_bridge("pretooluse", {"session_id": "sess_t", "tool_name": "Bash",
                       "tool_input": {"command": "rm -rf"}})
    out = proc.communicate(timeout=10)[0].strip()
    assert out == "", f"写工具不应放行: {out}"
    window.state.allow_readonly = False
    log("PASS 只读放行(关/白名单外):均无输出")

    window.server.stop()
    log("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
