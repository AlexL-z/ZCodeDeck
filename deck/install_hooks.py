"""把 ZCodeDeck 的 hook 注册写入 ~/.zcode/cli/config.json。

幂等且支持升级:每次安装先移除本项目的旧条目再写入当前版本;已运行会话不受
影响,新会话生效。自动备份原配置。
用法:
    py -3 deck/install_hooks.py            安装/升级
    py -3 deck/install_hooks.py --remove   卸载(只移除 ZCodeDeck 的条目)
"""
import json
import sys
import time
from pathlib import Path

CONFIG_PATH = Path.home() / ".zcode" / "cli" / "config.json"
BRIDGE_PATH = Path(__file__).resolve().parent / "hook_bridge.py"
MARKER = "ZCodeDeck"

# 事件 → (桥接模式, timeoutMs)。升级 v0.2.0:PreToolUse 走放行查询,
# Stop 走续跑窗口(armed 才挂起,默认关,最多拖住会话 8 秒)。
SPECS = {
    "SessionStart":       ("status", 10000),
    "UserPromptSubmit":   ("status", 10000),
    "PreToolUse":         ("pretooluse", 10000),
    "PermissionRequest":  ("permission", 330000),
    "PostToolUse":        ("status", 10000),
    "PostToolUseFailure": ("status", 10000),
    "Stop":               ("stop", 20000),
}


def _python_exe() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    return str(exe)


def _is_ours(entry: dict) -> bool:
    return MARKER in json.dumps(entry, ensure_ascii=False)


def _entry(event: str, mode: str, timeout_ms: int) -> dict:
    return {"hooks": [{
        "type": "process",
        "command": _python_exe(),
        "args": [str(BRIDGE_PATH), mode, event],
        "timeoutMs": timeout_ms,
        "statusMessage": MARKER,
    }]}


def install() -> None:
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    backup = CONFIG_PATH.with_name(
        f"config.json.bak_zcodedeck_{time.strftime('%Y%m%d_%H%M%S')}")
    backup.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")

    hooks = cfg.setdefault("hooks", {})
    hooks["enabled"] = True
    events = hooks.setdefault("events", {})
    for event, (mode, timeout_ms) in SPECS.items():
        entries = events.setdefault(event, [])
        entries[:] = [e for e in entries if not _is_ours(e)]
        entries.append(_entry(event, mode, timeout_ms))

    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    _verify()


def remove() -> int:
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    events = cfg.get("hooks", {}).get("events", {})
    removed = 0
    for ev in list(events):
        kept = [e for e in events[ev] if not _is_ours(e)]
        if len(kept) != len(events[ev]):
            removed += len(events[ev]) - len(kept)
            if kept:
                events[ev] = kept
            else:
                del events[ev]
    if removed:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return removed


def is_installed() -> bool:
    """已安装且与当前版本参数一致才视为已装;否则启动时自动升级。"""
    try:
        events = json.loads(CONFIG_PATH.read_text("utf-8")).get(
            "hooks", {}).get("events", {})
        for event, (mode, _) in SPECS.items():
            ours = [e for e in events.get(event, []) if _is_ours(e)]
            if len(ours) != 1:
                return False
            args = ours[0]["hooks"][0].get("args") or []
            if args[1:3] != [mode, event]:
                return False
        return True
    except Exception:
        return False


def _verify() -> None:
    ok = is_installed()
    print(f"[install_hooks] 安装/升级{'完成' if ok else '异常,请检查'}: "
          f"7 事件 → status×4 / pretooluse / permission / stop")


def main() -> int:
    if "--remove" in sys.argv:
        n = remove()
        print(f"[install_hooks] 已移除 {n} 组 ZCodeDeck hook 条目")
    else:
        install()
    return 0


if __name__ == "__main__":
    sys.exit(main())
