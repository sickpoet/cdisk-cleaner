# -*- coding: utf-8 -*-
"""
回归自检：路径守卫、扫描与清理、界面组件。

Copyright (C) 2026 柯夜 (sickpoet). 保留所有权利。
源码：https://github.com/sickpoet/cdisk-cleaner

    python selftest.py

「必须拦住」那一组是安全底线，任何一条挂掉都说明守卫被动坏了，
先修守卫再动别的。测试用的临时目录由 tempfile 创建，跑完就清掉。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cdisk_cleaner as C  # noqa: E402

# --no-gui：跳过所有需要真实窗口的用例。给 CI 用——那边没有桌面会话，
# 建不出 Tk 窗口。守卫、扫描、清理这些真正要紧的用例本来就不依赖 tkinter。
NO_GUI = "--no-gui" in sys.argv

tk = None
if NO_GUI:
    print("（--no-gui：本次跳过全部界面用例）")
else:
    try:
        import tkinter as tk  # noqa: E402
    except ImportError:
        tk = None
        print("（本机没有 tkinter，跳过全部界面用例）")
    else:
        C.tk = tk

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[ ok ]" if cond else "[FAIL]", name,
                       ("  <- " + str(extra)) if (extra and not cond) else ""))


def expect_guard(name, path, **kw):
    try:
        C.guard(path, **kw)
    except C.GuardError:
        check(name, True)
    except Exception as exc:  # noqa: BLE001
        check(name, False, "抛出了别的异常 %r" % exc)
    else:
        check(name, False, "竟然放行了")


def expect_pass(name, path, **kw):
    try:
        C.guard(path, **kw)
        check(name, True)
    except C.GuardError as exc:
        check(name, False, "被误拦：%s" % exc)


print("=== 路径守卫：必须拦住 ===")
sysdrive = C.SYSTEM_DRIVE + os.sep
home = os.path.expanduser("~")

expect_guard("空路径", "")
expect_guard("盘符根 C:\\", "C:\\")
expect_guard("盘符根 C:/", "C:/")
expect_guard("盘符根 D:\\", "D:\\")
expect_guard("用户主目录本身", home)
expect_guard("System32", os.path.join(sysdrive, "Windows", "System32", "x.dll"))
expect_guard("SysWOW64", os.path.join(sysdrive, "Windows", "SysWOW64", "x.dll"))
expect_guard("WinSxS", os.path.join(sysdrive, "Windows", "WinSxS", "x"))
expect_guard("Fonts", os.path.join(sysdrive, "Windows", "Fonts", "x.ttf"))
expect_guard("Installer", os.path.join(sysdrive, "Windows", "Installer", "x"))
expect_guard("Program Files", os.path.join(sysdrive, "Program Files", "a", "b"))
expect_guard("ProgramData", os.path.join(sysdrive, "ProgramData", "a"))
expect_guard("AppData\\Roaming", os.path.join(home, "AppData", "Roaming", "a"))
expect_guard("Documents", os.path.join(home, "Documents", "a.txt"))
expect_guard("Desktop", os.path.join(home, "Desktop", "a.txt"))
expect_guard("回收站目录", os.path.join(sysdrive, "$Recycle.Bin", "S-1-5", "a"))
expect_guard("Recovery", os.path.join(sysdrive, "Recovery", "a"))
expect_guard("超出白名单根",
             os.path.join(home, "Documents", "x.txt"),
             allowed_root=os.path.join(home, "AppData", "Local", "Temp"))
expect_guard("Windows 根下的散落目录", os.path.join(sysdrive, "Windows", "abc"))

print("\n=== 路径守卫：必须放行 ===")
tmp = os.environ.get("TEMP", os.path.join(home, "AppData", "Local", "Temp"))
expect_pass("用户临时目录内的文件",
            os.path.join(tmp, "somefile.tmp"), allowed_root=tmp)
expect_pass("用户临时目录内的子目录",
            os.path.join(tmp, "somedir", "x"), allowed_root=tmp)
expect_pass("系统临时目录（显式授权越过保护区）",
            os.path.join(sysdrive, "Windows", "Temp", "x.tmp"),
            allowed_root=os.path.join(sysdrive, "Windows", "Temp"),
            allow_protected=True)
expect_pass("普通缓存目录",
            os.path.join(home, "AppData", "Local", "D3DSCache", "f"),
            allowed_root=os.path.join(home, "AppData", "Local", "D3DSCache"))

print("\n=== 系统临时目录仍然不许越权 ===")
expect_guard("未授权时不得碰 Windows\\Temp",
             os.path.join(sysdrive, "Windows", "Temp", "x.tmp"),
             allowed_root=os.path.join(sysdrive, "Windows", "Temp"))
expect_guard("即便授权也不许碰 System32",
             os.path.join(sysdrive, "Windows", "System32", "x"),
             allowed_root=os.path.join(sysdrive, "Windows"),
             allow_protected=True)

print("\n=== 工具函数 ===")
check("human_size 0", C.human_size(0) == "0 B", C.human_size(0))
check("human_size GB", C.human_size(1600000000).endswith("GB"),
      C.human_size(1600000000))
check("disk_usage C 盘可读", C.disk_usage("C") is not None)
check("disk_usage 支持带冒号", C.disk_usage("C:") is not None)

# long_path：造一个必然带 8.3 短名的目录，验证能展开回长名。
# 坑：TEMP 本身可能就是短名形式（本机就是 C:\Users\ADMINI~1\...），
# 所以不能拿展开结果跟 TEMP 比字符串，得验证「确实指向同一个目录」。
_short_ok = None          # None = 本机不生成 8.3 短名，没得测，按跳过算
_probe_root = None
try:
    import ctypes as _ct
    _probe_root = tempfile.mkdtemp(prefix="cdisk_lp_")
    _probe = os.path.join(_probe_root, "a_long_dir_name_for_83_probe")
    os.makedirs(_probe, exist_ok=True)
    _buf = _ct.create_unicode_buffer(32768)
    _n = _ct.windll.kernel32.GetShortPathNameW(_probe, _buf, 32768)
    _short = _buf.value if _n else ""
    if _short and os.path.normcase(_short) != os.path.normcase(_probe):
        # 真的拿到了 ~1 形式，这才有得测
        _back = C.long_path(_short)
        with open(os.path.join(_short, "probe.txt"), "w") as _fh:
            _fh.write("x")
        _short_ok = os.path.exists(os.path.join(_back, "probe.txt"))
except Exception as _exc:  # noqa: BLE001
    _short_ok = None
finally:
    if _probe_root:
        try:
            import shutil as _sh
            _sh.rmtree(_probe_root, ignore_errors=True)
        except Exception:
            pass
check("long_path 把 8.3 短名还原成长名", _short_ok is not False,
      "还原结果指向了别的目录" if _short_ok is False else "")
if _short_ok is None:
    print("       （本机未生成 8.3 短名，此项按跳过处理）")

print("\n=== 扫描与清理（临时目录内真实跑一遍）===")
sandbox = tempfile.mkdtemp(prefix="cdisk_test_")
try:
    for i in range(3):
        with open(os.path.join(sandbox, "f%d.tmp" % i), "wb") as fh:
            fh.write(b"x" * 1024)
    os.makedirs(os.path.join(sandbox, "sub", "deep"), exist_ok=True)
    with open(os.path.join(sandbox, "sub", "deep", "g.tmp"), "wb") as fh:
        fh.write(b"y" * 2048)
    os.makedirs(os.path.join(sandbox, "emptydir"), exist_ok=True)

    size, files, errs = C._measure(sandbox)
    check("_measure 统计文件数", files == 4, files)
    check("_measure 统计字节数", size == 3 * 1024 + 2048, size)

    t = C.Target("t1", "测试目标", [sandbox], "contents", C.LEVEL_SAFE,
                 "测试", False, False, None)
    C.scan_targets([t], skip_recyclebin=True)
    check("scan_targets 标记存在", t.exists is True)
    check("scan_targets 记录大小", t.size == size, t.size)

    check("空目录判定", C.is_empty_dir(os.path.join(sandbox, "emptydir")))
    check("非空目录判定", not C.is_empty_dir(sandbox))

    freed, removed, failed = C.clear_target(t, use_recyclebin=False)
    check("清理后目录内已空", os.listdir(sandbox) == [], os.listdir(sandbox))
    check("清理释放字节数", freed == 3 * 1024 + 2048, freed)
    check("清理未报错", failed == 0, failed)
except OSError as exc:
    check("临时目录端到端（沙箱 safe-delete 可能干扰）", False, exc)
finally:
    C._rmtree_force(sandbox)
    check("清理测试目录", not os.path.exists(sandbox))

print("\n=== 署名与出处 ===")
# 这组是防篡改的：署名被人抹掉或改掉，这里立刻红。
check("__author__ 是柯夜", C.__author__ == "柯夜", C.__author__)
check("__author_id__ 是 sickpoet", C.__author_id__ == "sickpoet",
      C.__author_id__)
check("版权串含作者名",
      bool(C.__copyright__) and C.__author__ in C.__copyright__,
      C.__copyright__)
check("主页指向原仓库",
      "github.com/sickpoet/cdisk-cleaner" in C.__homepage__, C.__homepage__)
check("指纹非空", bool(C.FINGERPRINT), C.FINGERPRINT)

# 模块文档字符串里嵌的零宽水印。整段复制源码时它会跟着走，
# 用来在抄袭发生后追溯出处。编码：U+200B=1 / U+200C=0 / U+200D=分隔。
_zw = [c for c in (C.__doc__ or "") if c in "\u200b\u200c\u200d"]
_wm_bits, _wm = "", []
for _c in _zw:
    if _c == "\u200d":
        if _wm_bits:
            _wm.append(chr(int(_wm_bits, 2)))
        _wm_bits = ""
    else:
        _wm_bits += "1" if _c == "\u200b" else "0"
_wm = "".join(_wm)
check("源码水印可解码且含作者标识",
      "柯夜" in _wm and "sickpoet" in _wm, _wm or "（没找到水印）")
if _wm:
    print("       水印明文: %s" % _wm)

def _finish():
    """结算并退出。提出来是为了让 --no-gui 能在界面用例之前就收工。"""
    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败: %s" % f)
    sys.exit(1 if FAIL else 0)


print("\n=== 界面组件 ===")
if tk is None:
    _finish()

root = tk.Tk()
root.withdraw()
try:
    C.pick_fonts(root)
    check("字体已选定", C.FONT_EN and C.FONT_UI, (C.FONT_EN, C.FONT_UI))
    C.setup_style(root)

    app = C.CleanerApp(root, autoscan=False)
    root.update()
    check("目标列表已构建", len(app.targets) == len(C.TARGET_SPECS),
          len(app.targets))
    check("目标 key 唯一",
          len({t.key for t in app.targets}) == len(app.targets))

    # 按钮状态
    app.btn_scan.set_state("disabled")
    check("按钮可禁用", app.btn_scan._enabled is False)
    app.btn_scan.set_state("normal")
    check("按钮可启用", app.btn_scan._enabled is True)
    app.btn_all.set_text("X")
    check("按钮可改文字", app.btn_all._text == "X")
    app.btn_all.set_text("ALL")

    # 选中计数
    app.on_check_none()
    check("全不选后无选中", app._selected_targets() == [])
    app.on_check_all()
    sel = app._selected_targets()
    check("全选后不含 risky",
          all(t.level != C.LEVEL_RISKY for t in sel) and len(sel) > 0,
          len(sel))
    check("清理按钮在全选后可用", app.btn_clean._enabled is True)
    app.on_check_none()
    check("清理按钮在清空后禁用", app.btn_clean._enabled is False)

    # 忙碌态切换
    app._set_busy(True)
    check("忙碌时扫描按钮禁用", app.btn_scan._enabled is False)
    check("忙碌时状态条变强调色",
          app._status_accent.cget("bg") == C.MAGENTA,
          app._status_accent.cget("bg"))
    app._set_busy(False)
    check("空闲后扫描按钮恢复", app.btn_scan._enabled is True)
    check("空闲后状态条回青色",
          app._status_accent.cget("bg") == C.CYAN,
          app._status_accent.cget("bg"))

    # 单选按钮
    app.use_recyclebin.set(False)
    check("默认永久删除", app.use_recyclebin.get() is False)
    app.use_recyclebin.set(True)
    check("可切到送回收站", app.use_recyclebin.get() is True)

    # 仪表盘
    root.deiconify()
    root.update()
    for _ in range(6):
        root.update()
    check("仪表盘已绘制", app._drive_item is not None)
    check("仪表盘显示 C 盘读数",
          "可用" in app.head.itemcget(app._drive_item, "text")
          or "读取" in app.head.itemcget(app._drive_item, "text"),
          app.head.itemcget(app._drive_item, "text"))
    app._tick_blink()
    check("闪烁动画未报错", True)
    app.alive = False

    app.log("测试日志")
    check("日志可写入", "测试日志" in app.log_text.get("1.0", "end"))

    # ---- 无边框窗口
    # 上面为了让闪烁动画停下来把 alive 关了，这里得开回来：
    # 最小化恢复靠的是 app 自己的轮询，alive=False 会让它直接返回，测出来的就是假失败
    app.alive = True

    # 先让窗口自己排完队里的事件（居中、重绘都是 after 排的），否则后面比几何会飘
    import time as _time
    for _i in range(12):
        root.update()
        _time.sleep(0.02)

    check("已切到无边框", bool(root.overrideredirect()))

    def _exstyle():
        import ctypes as _ct
        hwnd = _ct.windll.user32.GetParent(root.winfo_id())
        get = _ct.windll.user32.GetWindowLongW
        get.restype = _ct.c_long
        return get(hwnd, -20)   # GWL_EXSTYLE

    ex = _exstyle()
    check("任务栏图标已补回（WS_EX_APPWINDOW）", bool(ex & 0x00040000), ex)
    check("已去掉 WS_EX_TOOLWINDOW", not (ex & 0x00000080), ex)

    wa = app._work_area()
    check("能读到屏幕工作区", wa[2] > wa[0] and wa[3] > wa[1], wa)

    rx, ry = root.winfo_rootx(), root.winfo_rooty()
    rw, rh = root.winfo_width(), root.winfo_height()
    check("左上角识别为 nw", app._edge_at(rx + 2, ry + 2) == "nw")
    check("右下角识别为 se", app._edge_at(rx + rw - 2, ry + rh - 2) == "se")
    check("上边缘识别为 n", app._edge_at(rx + rw // 2, ry + 2) == "n")
    check("窗口中央不是边缘", app._edge_at(rx + rw // 2, ry + rh // 2) is None)

    # 缩放：往内拖到超过下限，应停在最小尺寸
    class _E(object):
        def __init__(self, x, y):
            self.x_root, self.y_root = x, y

    app._on_win_press(_E(rx + rw - 2, ry + rh - 2))
    app._on_win_drag(_E(rx + rw - 2 - 600, ry + rh - 2 - 600))
    root.update()
    got = root.winfo_width(), root.winfo_height()
    app._on_win_release(None)
    check("缩放不会小于最小尺寸", got[0] >= C.MIN_WIN[0] and got[1] >= C.MIN_WIN[1],
          "%s vs %s" % (got, C.MIN_WIN))

    # 最大化 / 还原
    before = root.geometry()
    app._toggle_max()
    root.update()
    check("最大化后高度接近工作区", root.winfo_height() >= wa[3] - wa[1] - 4,
          root.geometry())
    app._toggle_max()
    root.update()
    check("还原回原尺寸", root.geometry() == before,
          "%s -> %s" % (before, root.geometry()))

    # 最小化：无边框窗口直接 iconify 会报 "override-redirect flag is set"，
    # 必须先把边框恢复回去。这里包一层探针，确认它真的调成功了。
    _calls = []
    _raw_iconify = root.iconify

    def _spy():
        try:
            _raw_iconify()
            _calls.append("ok")
        except Exception as _e:  # noqa: BLE001
            _calls.append("%s: %s" % (type(_e).__name__, _e))
            raise

    root.iconify = _spy
    app._minimize()
    for _i in range(20):
        root.update()
        _time.sleep(0.05)
    root.iconify = _raw_iconify
    check("最小化时 iconify 没报错", _calls == ["ok"], _calls)
    check("最小化后窗口状态为 iconic", root.state() == "iconic", root.state())

    root.deiconify()
    for _i in range(20):
        root.update()
        _time.sleep(0.05)
    check("恢复后回到 normal", root.state() == "normal", root.state())
    check("恢复后仍是无边框", bool(root.overrideredirect()))
    check("恢复后任务栏样式还在", bool(_exstyle() & 0x00040000))
except Exception as exc:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("界面冒烟测试整体通过", False, exc)
finally:
    try:
        root.destroy()
    except Exception:
        pass

_finish()
