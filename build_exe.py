# -*- coding: utf-8 -*-
"""
一键打包成 exe。

做四件事：
  1. 从主程序读 __version__（版本号的唯一来源，别在别处硬编码）
  2. 生成图标（调用 make_icon.py）
  3. 生成 PyInstaller 版本资源，让 exe 的右键属性里有版本、产品名、版权
  4. 调 PyInstaller 打包

用法：
    python build_exe.py                # 单文件版（默认，UPX 关闭）
    python build_exe.py --onedir       # 目录版，启动/退出更快
    python build_exe.py --upx          # 启用 UPX 压缩
    python build_exe.py --upx-dir "C:\\path\\to\\upx"
    python build_exe.py --keep-console # 保留控制台，排查启动问题时用

注意 UPX 必须在命令行传 --upx-dir，写进 spec 里是无效的。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

APP_NAME = "cdisk-cleaner"
DISPLAY_NAME = "C 盘垃圾清理器"
COMPANY = "sickpoet"
MAIN_SCRIPT = "cdisk_cleaner.py"
ICON_FILE = "icon.ico"
VERSION_FILE = "version_info.txt"
DIST_DIR = "dist"
BUILD_DIR = "build"

VERSION_TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(tup)s,
    prodvers=%(tup)s,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', '%(company)s'),
        StringStruct('FileDescription', '%(display)s'),
        StringStruct('FileVersion', '%(version)s'),
        StringStruct('InternalName', '%(name)s'),
        StringStruct('LegalCopyright', 'Copyright (C) 2026 %(company)s'),
        StringStruct('OriginalFilename', '%(name)s.exe'),
        StringStruct('ProductName', '%(display)s'),
        StringStruct('ProductVersion', '%(version)s')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def die(msg, code=1):
    print("[错误] %s" % msg, file=sys.stderr)
    sys.exit(code)


def here():
    return os.path.dirname(os.path.abspath(__file__))


def read_version():
    """从主程序里读 __version__，这是版本号的唯一来源。"""
    path = os.path.join(here(), MAIN_SCRIPT)
    if not os.path.isfile(path):
        die("找不到主程序 %s" % path)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.M)
    if not m:
        die("在 %s 里找不到 __version__ 定义" % MAIN_SCRIPT)
    return m.group(1)


def check_tkinter():
    """确保当前解释器能 import tkinter，否则打出来的 exe 是废的。"""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        die("当前 Python（%s）没有 tkinter 组件，打出来的 exe 跑不起来。\n"
            "请改用带 tcl/tk 的 Python，例如：\n"
            r"    C:\Program Files\Python312\python.exe build_exe.py"
            % sys.executable)


def make_icon():
    path = os.path.join(here(), ICON_FILE)
    script = os.path.join(here(), "make_icon.py")
    if os.path.isfile(path) and not os.path.isfile(script):
        return path
    print("      生成图标 ...")
    rc = subprocess.call([sys.executable, script], cwd=here())
    if rc != 0 or not os.path.isfile(path):
        die("图标生成失败")
    return path


def write_version_file(version):
    nums = [int(x) for x in re.findall(r"\d+", version)][:4]
    nums += [0] * (4 - len(nums))
    tup = "(%d, %d, %d, %d)" % tuple(nums)
    content = VERSION_TEMPLATE % {
        "tup": tup,
        "version": version,
        "display": DISPLAY_NAME,
        "name": APP_NAME,
        "company": COMPANY,
    }
    path = os.path.join(here(), VERSION_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def find_upx(explicit=None):
    """
    找 upx.exe。优先级：命令行 --upx-dir > 环境变量 UPX_DIR > 项目内 tools/upx
    > PATH > 本机已知位置。
    """
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("UPX_DIR"):
        cands.append(os.environ["UPX_DIR"])
    cands.append(os.path.join(here(), "tools", "upx"))
    # 本机其它项目里放过一份，拿来做兜底
    cands.append(r"F:\AI\OPANAI-X\CodexAPI\tools\upx-5.2.1-win64")

    for c in cands:
        if c and os.path.isfile(os.path.join(c, "upx.exe")):
            return c

    exe = shutil.which("upx")
    if exe:
        return os.path.dirname(exe)
    return None


def clean_old():
    """删掉旧产物。不用 --clean，那个批量删缓存会被沙箱拦。"""
    for d in (BUILD_DIR, DIST_DIR):
        p = os.path.join(here(), d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="打包 %s" % DISPLAY_NAME)
    parser.add_argument("--onedir", action="store_true",
                        help="打成目录版（启动/退出更快，但不是单文件）")
    parser.add_argument("--upx", action="store_true",
                        help="启用 UPX 压缩。默认关闭")
    parser.add_argument("--upx-dir", default=None, help="UPX 所在目录")
    parser.add_argument("--keep-console", action="store_true",
                        help="保留控制台窗口，排查启动问题时用")
    args = parser.parse_args()

    check_tkinter()
    version = read_version()

    print("%s  v%s" % (DISPLAY_NAME, version))
    print("      解释器: %s" % sys.executable)
    print("      模式  : %s" % ("目录版" if args.onedir else "单文件版"))

    icon = make_icon()
    vfile = write_version_file(version)
    clean_old()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir" if args.onedir else "--onefile",
        "--name", APP_NAME,
        "--icon", icon,
        # 也把图标打进包内，主程序启动时用它设窗口/任务栏图标。
        # 只给 --icon 的话，那只是 exe 的文件图标资源，窗口拿不到。
        # 必须用绝对路径：--specpath 指向 build/，相对路径会按 spec 的位置解析。
        "--add-data", os.path.join(here(), ICON_FILE) + os.pathsep + ".",
        "--version-file", vfile,
        "--distpath", DIST_DIR,
        "--workpath", BUILD_DIR,
        "--specpath", BUILD_DIR,
        "--exclude-module", "PIL",
        "--exclude-module", "numpy",
        "--exclude-module", "pytest",
        "--exclude-module", "setuptools",
        "--exclude-module", "pip",
    ]
    if not args.keep_console:
        cmd.append("--windowed")

    if args.upx:
        upx_dir = find_upx(args.upx_dir)
        if upx_dir:
            cmd += ["--upx-dir", upx_dir]
            print("      UPX   : %s" % upx_dir)
        else:
            print("      UPX   : 没找到 upx.exe，本次不压缩")
    else:
        print("      UPX   : 关闭（加壳会拉高国内杀软误报率，需要时用 --upx）")

    cmd.append(MAIN_SCRIPT)

    print("\n开始打包 ...")
    rc = subprocess.call(cmd, cwd=here())
    if rc != 0:
        die("PyInstaller 退出码 %d" % rc)

    if args.onedir:
        out_dir = os.path.join(here(), DIST_DIR, APP_NAME)
        exe = os.path.join(out_dir, APP_NAME + ".exe")
        total = 0
        for root, _dirs, files in os.walk(out_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        if not os.path.isfile(exe):
            die("打包结束但找不到产物：%s" % exe)
        print("\n完成: %s" % exe)
        print("      目录合计 %.1f MB" % (total / 1048576.0))
    else:
        exe = os.path.join(here(), DIST_DIR, APP_NAME + ".exe")
        if not os.path.isfile(exe):
            die("打包结束但找不到产物：%s" % exe)
        print("\n完成: %s  (%.1f MB)"
              % (exe, os.path.getsize(exe) / 1048576.0))

    return 0


if __name__ == "__main__":
    sys.exit(main())
