"""打包发布:把运行所需的源码、启动器与使用说明压成发布 zip。

用法: py -3 make_release.py
输出: release/ZCodeDeck_v<版本>.zip
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from deck import __version__  # noqa: E402

USAGE = """ZCodeDeck 使用说明
==================

一句话:Windows 桌面悬浮小窗,监控 ZCode 干活、远程批准操作、
看套餐额度,每天还能生成一张"AI 牛马战报"长图。

【系统要求】
· Windows 10/11
· 已安装 Python 3.10 或更高(官网 python.org 下载,勾选 Add to PATH)
· 已安装并登录 ZCode 桌面版

【三步上手】
1. 解压本文件夹到任意位置(路径不要带空格更稳)
2. 双击「启动悬浮窗.bat」。首次运行会自动安装界面依赖(约一两分钟),
   并弹出 API Key 设置窗口
3. 完全退出 ZCode 桌面版(托盘右键退出)再重新打开,
   之后新开的任务就会被小窗监控

【API Key 说明(可跳过)】
API Key 用于显示官方套餐剩余额度(智谱 BigModel 个人中心的 API Keys
页面可获取,订阅 Coding Plan 的账号都有)。跳过也不影响其他功能,
之后可在托盘菜单「API Key 设置」随时补填。

【隐私声明】
ZCodeDeck 完全运行在你的电脑上:
· 用量与任务数据只读取本地 ZCode 数据库,不外发
· API Key 仅保存在本机,只用于查询智谱官方额度接口
· 通信端口仅绑定 127.0.0.1,局域网内其他设备不可见
· 无任何统计、埋点或账号系统

【常见问题】
· 双击 bat 一闪而过:说明 Python 未安装或未加入 PATH,装好再试
· 小窗没有反应:确认 ZCode 桌面版完整重启过一次
· 点了"日报"没图:当天还没有任何会话消耗,晚点再点
· 想开机自启:托盘右键勾选「开机自启」

【卸载】
1. 托盘右键退出悬浮窗
2. 命令行执行: py -3 deck/install_hooks.py --remove
3. 删除整个文件夹即可(配置在 ~/.zcode/deck/,可一并删除)

By Alex打工人万事屋
"""


def main() -> None:
    out_dir = ROOT / "release"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"ZCodeDeck_v{__version__}.zip"
    files = [
        ("zcode_deck.py", "zcode_deck.py"),
        ("启动悬浮窗.bat", "启动悬浮窗.bat"),
        ("deck/__init__.py", "deck/__init__.py"),
        ("deck/hook_bridge.py", "deck/hook_bridge.py"),
        ("deck/server.py", "deck/server.py"),
        ("deck/usage.py", "deck/usage.py"),
        ("deck/quota.py", "deck/quota.py"),
        ("deck/report.py", "deck/report.py"),
        ("deck/install_hooks.py", "deck/install_hooks.py"),
        ("deck/autostart.py", "deck/autostart.py"),
        ("deck/assets/z_glyph.png", "deck/assets/z_glyph.png"),
        ("deck/lid.py", "deck/lid.py"),
    ]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in files:
            z.write(ROOT / src, f"ZCodeDeck/{arc}")
        z.writestr("ZCodeDeck/使用说明.txt", USAGE)
    names = z.namelist() if False else None
    print(f"release: {out}")
    print(f"files: {len(files) + 1} | size: {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
