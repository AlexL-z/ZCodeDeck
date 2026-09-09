"""合盖不休眠:改当前电源计划的合盖动作(0=不采取操作,1=睡眠)。

启用前备份 AC/DC 原值,禁用时精确恢复;未备份时按系统默认 1 恢复。
powercfg 输出本地化,索引用双语言正则容错解析。
"""
import json
import re
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
BACKUP_PATH = Path.home() / ".zcode" / "deck" / "lid_backup.json"


def _run(args) -> subprocess.CompletedProcess:
    # Windows 中文系统 powercfg 输出为 GBK;显式解码,坏字节替换不炸
    return subprocess.run(
        ["powercfg", *args], capture_output=True,
        encoding="gbk", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=15)


def _current_indices() -> tuple[int, int]:
    """读当前合盖动作 AC/DC 索引;解析失败返回 (1, 1)(系统默认睡眠)。"""
    r = _run(["/q", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION"])
    vals = re.findall(
        r"(?:索引|Index)[^\r\n]*?0x([0-9a-fA-F]{8})", r.stdout, re.IGNORECASE)
    if len(vals) < 2:
        vals = ["1", "1"]
    return int(vals[0], 16), int(vals[1], 16)


def _set_indices(ac: int, dc: int) -> bool:
    ok = True
    for cmd, val in (("/setacvalueindex", ac), ("/setdcvalueindex", dc)):
        r = _run([cmd, "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", str(val)])
        if r.returncode != 0:
            ok = False
    r = _run(["/setactive", "SCHEME_CURRENT"])
    return ok and r.returncode == 0


def _read_backup() -> dict | None:
    try:
        return json.loads(BACKUP_PATH.read_text("utf-8"))
    except Exception:
        return None


def _write_backup(ac: int, dc: int) -> None:
    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_PATH.write_text(json.dumps({"ac": ac, "dc": dc}), "utf-8")


def arm() -> dict:
    """开启合盖不休眠。返回 {ok, ac, dc}(ac/dc 为备份的原值)。"""
    backup = _read_backup()
    if not backup:
        ac, dc = _current_indices()
        _write_backup(ac, dc)
        backup = {"ac": ac, "dc": dc}
    ok = _set_indices(0, 0)
    return {"ok": ok, **backup}


def restore() -> dict:
    """恢复合盖动作;未备份时按系统默认(1=睡眠)。返回 {ok, ac, dc}。"""
    backup = _read_backup() or {"ac": 1, "dc": 1}
    ok = _set_indices(int(backup["ac"]), int(backup["dc"]))
    try:
        BACKUP_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": ok, **backup}


def is_armed() -> bool:
    return _read_backup() is not None
