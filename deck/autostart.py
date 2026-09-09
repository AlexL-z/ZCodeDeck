"""开机自启:HKCU 注册表 Run 键,用户级,不写 HKLM。"""
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "ZCodeDeck"


def enable(script_path: str) -> None:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        exe = exe.with_name("pythonw.exe")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ,
                          f'"{exe}" "{script_path}"')


def disable() -> bool:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        try:
            winreg.DeleteValue(k, NAME)
            return True
        except FileNotFoundError:
            return False


def is_on() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, NAME)
            return True
    except FileNotFoundError:
        return False
