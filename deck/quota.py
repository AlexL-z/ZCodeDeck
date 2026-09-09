"""官方剩余额度:查询智谱 open.bigmodel.cn 的 /api/monitor/usage/quota/limit。

鉴权为裸 API key(Authorization 头不带 Bearer 前缀),与官方 glm-plan-usage
插件同源同法;桌面版 OAuth token 不被该端点接受,因此 key 需用户自填:
  1. 环境变量 ZCODEDECK_BIGMODEL_KEY 或 BIGMODEL_API_KEY
  2. 配置文件 ~/.zcode/deck/config.json 的 bigmodel_api_key 字段
任何失败都静默降级(ok=False),绝不影响悬浮窗其他功能。
"""
import json
import os
import time
import urllib.request
from pathlib import Path

QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
CONFIG_PATH = Path.home() / ".zcode" / "deck" / "config.json"

# 已知额度类型与窗口单元的中文名
KNOWN_LABELS = {
    "TOKENS_LIMIT": "5小时 Token",
    "TIME_LIMIT": "MCP 月度",
    "WEEKLY_TOKENS_LIMIT": "每周 Token",
}
# CREDIT_LIMIT 响应里 unit 枚举:3=小时窗,6=周窗(2026-09-02 实测);
# number 为窗口数量(unit 3 × number 5 = 5 小时窗)
UNIT_LABELS = {3: "5小时", 6: "每周"}
UNIT_SECONDS = {3: 3600, 6: 604800}
# 进度条按窗口类型固定着色,对齐 ZCode:5 小时蓝,每周绿
COLOR_BY_UNIT = {3: "#4da3ff", 6: "#3ecf8e"}
GREEN, AMBER, RED = "#3ecf8e", "#ffb020", "#ff5d5d"


def pace(item: dict, now_ms: float | None = None) -> dict | None:
    """按窗口已过时间计算"应用用量",用于判断消耗快慢。

    返回 {elapsed_pct, expected_used_pct, diff};diff = 实际用量 − 应用用量,
    正值偏快。缺字段或时间越界(如接口时钟漂移)返回 None,不显示速率。
    """
    unit = item.get("unit")
    number = item.get("number")
    reset = item.get("reset_ms")
    if unit not in UNIT_SECONDS or not number or not reset:
        return None
    dur_ms = UNIT_SECONDS[unit] * number * 1000
    now = now_ms if now_ms is not None else time.time() * 1000
    remain_ms = reset - now
    if remain_ms < 0 or remain_ms > dur_ms:
        return None
    elapsed_pct = (1 - remain_ms / dur_ms) * 100
    diff = float(item.get("percentage") or 0) - elapsed_pct
    return {"elapsed_pct": elapsed_pct,
            "expected_used_pct": elapsed_pct, "diff": diff}


def level_color(pct: float) -> str:
    if pct >= 85:
        return RED
    if pct >= 60:
        return AMBER
    return GREEN


def bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def _parse_limits(data: dict) -> dict:
    """从官方响应提取额度列表;防御式,字段缺失直接跳过。

    实测结构(CREDIT_LIMIT):{type, unit, number, usage, currentValue,
    remaining, percentage, nextResetTime};unit 3 为 5 小时窗,6 为每周窗。
    """
    raw = data.get("data") or {}
    limits = []
    for item in raw.get("limits") or []:
        if not isinstance(item, dict) or "percentage" not in item:
            continue
        ltype = str(item.get("type") or "?")
        unit = item.get("unit")
        try:
            pct = float(item["percentage"])
        except (TypeError, ValueError):
            continue
        limits.append({
            "type": ltype,
            "unit": unit,
            "number": item.get("number"),
            "label": UNIT_LABELS.get(unit) or KNOWN_LABELS.get(ltype) or ltype,
            "percentage": pct,
            "used": item.get("currentValue"),
            "total": item.get("usage"),
            "remaining": item.get("remaining"),
            "reset_ms": item.get("nextResetTime"),
        })
    return {"limits": limits, "level": str(raw.get("level") or "")}


def _read_key() -> str:
    for env in ("ZCODEDECK_BIGMODEL_KEY", "BIGMODEL_API_KEY"):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        return str(cfg.get("bigmodel_api_key") or "").strip()
    except Exception:
        return ""


def fetch_quota(timeout: float = 10.0) -> dict:
    """返回 {ok, limits:[{label,type,percentage,...}], error, hint}。"""
    key = _read_key()
    if not key:
        return {"ok": False,
                "hint": "填入 API Key 可显示官方剩余额度(~/.zcode/deck/config.json)"}
    try:
        req = urllib.request.Request(
            QUOTA_URL, headers={"Authorization": key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    if not isinstance(data, dict) or (data.get("code") not in (0, 200)
                                      and data.get("success") is not True):
        msg = data.get("msg") if isinstance(data, dict) else "响应异常"
        return {"ok": False, "error": f"官方接口: {msg}"[:120]}
    parsed = _parse_limits(data)
    return {"ok": True, "limits": parsed["limits"],
            "level": parsed["level"], "at": time.strftime("%H:%M")}


if __name__ == "__main__":
    import sys
    result = fetch_quota()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)
