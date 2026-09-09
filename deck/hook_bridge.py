"""ZCode hook 桥接脚本:被 ZCode 的 hook runner 调用,与 ZCodeDeck 悬浮窗通信。

用法(ZCode 通过 process hook 调用):
    python hook_bridge.py status      <EventName>   状态事件,快速转发立即返回
    python hook_bridge.py pretooluse  <EventName>   状态转发 + 只读放行查询(短等待)
    python hook_bridge.py permission  <EventName>   审批事件,挂起等待悬浮窗决定
    python hook_bridge.py stop        <EventName>   停止事件;armed 时短窗口等待续跑点击

stdin 收到 ZCode 的 JSON 输入,字段以运行时实测为准(已知含 session_id /
hook_event_name / tool_name / tool_input / permission_mode / transcript_path)。

铁律:任何异常都静默退出 0,绝不阻塞用户会话;拿不到决定时不输出任何内容,
回退为 ZCode 的默认行为(权限走终端提示,停止走正常收尾)。
"""
import json
import os
import sys
import time
import urllib.request

DEFAULT_PORT = 17654
DEFAULT_WAIT = 300.0
DEFAULT_STOP_WAIT = 8.0
# 提问类工具不出审批卡:其真正交互面在任务窗口的提问框内,
# 远程"批准"一个提问没有意义,出卡只会制造困惑
SKIP_PERMISSION_TOOLS = {"AskUserQuestion"}


def _detail(data: dict) -> str:
    """从 hook 输入提取给用户看的一行摘要;计划类事件带全文。"""
    tool = str(data.get("tool_name") or "")
    ti = data.get("tool_input")
    try:
        if tool == "ExitPlanMode":
            plan = ti.get("plan") if isinstance(ti, dict) else None
            return str(plan or "") or json.dumps(ti, ensure_ascii=False)
        if isinstance(ti, dict):
            for key in ("command", "file_path", "url", "pattern", "prompt", "query"):
                if key in ti and ti[key]:
                    return f"{key}: {ti[key]}"
        return json.dumps(ti, ensure_ascii=False)[:1200] if ti else ""
    except Exception:
        return ""


def _post(path: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        f"http://127.0.0.1:{DEFAULT_PORT}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw.strip() else {}


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def main() -> int:
    if len(sys.argv) < 3:
        return 0
    mode, event = sys.argv[1], sys.argv[2]
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    payload = {
        "event": event,
        "session_id": str(data.get("session_id")
                          or os.environ.get("CLAUDE_SESSION_ID") or ""),
        "tool": str(data.get("tool_name") or ""),
        "mode": str(data.get("permission_mode") or ""),
        "cwd": str(data.get("cwd") or ""),
        "ts": int(time.time() * 1000),
    }

    try:
        if mode == "status":
            _post("/event", payload, timeout=3)
            return 0

        if mode == "pretooluse":
            _post("/event", payload, timeout=3)
            body = _post("/pretooluse", {"tool": payload["tool"]}, timeout=3)
            if (body or {}).get("decision") == "allow":
                _emit({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "ZCodeDeck 只读自动放行",
                }})
            return 0

        if mode == "permission":
            if payload["tool"] in SKIP_PERMISSION_TOOLS:
                _post("/event", payload, timeout=3)
                return 0
            payload["detail"] = _detail(data)
            wait = float(os.environ.get("ZCODEDECK_WAIT", DEFAULT_WAIT))
            payload["wait"] = wait
            body = _post("/permission", payload, timeout=wait + 5)
            decision = (body or {}).get("decision")
            if decision == "allow":
                _emit({"hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }})
            elif decision == "deny":
                _emit({"hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "deny",
                        "message": (body or {}).get("reason") or "ZCodeDeck 悬浮窗拒绝",
                    },
                }})
            return 0

        if mode == "stop":
            _post("/event", payload, timeout=3)
            wait = float(os.environ.get("ZCODEDECK_STOP_WAIT", DEFAULT_STOP_WAIT))
            body = _post("/stop-continue",
                         {"wait": wait, "session_id": payload["session_id"]},
                         timeout=wait + 5)
            if (body or {}).get("decision") == "continue":
                _emit({"decision": "block",
                       "reason": "用户在 ZCodeDeck 悬浮窗点击续跑:请继续完成当前任务"})
            return 0
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
