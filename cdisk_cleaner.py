# -*- coding: utf-8 -*-
"""
C 盘垃圾清理器

白名单制清理工具。只处理内置清单里经过人工审核的目标，清单之外的路径一律不碰。

设计原则
--------
1. 白名单优先：删除前必须证明路径落在某个已审核的清理目标内，证明不了就拒绝。
2. 双闸门：先过「白名单归属」检查，再过「绝对保护区」检查，两者都通过才允许删。
   少数系统目标（Windows\\Temp 等）靠 allow_protected 显式放行。
3. 先扫后清：扫描全只读，把每一项的大小和文件数摆出来，由使用者勾选后再执行。
4. 空目录单列：默认不勾选，且强制跳过系统保护区。

只用标准库，零第三方依赖。
"""

__version__ = "1.2.0"
APP_NAME = "C 盘垃圾清理器"

import ctypes
import glob
import os
import queue
import shutil
import stat
import sys
import threading
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

tk = None  # 延迟导入，见 main()

# ---------------------------------------------------------------- 常量

LEVEL_SAFE = "safe"      # 纯垃圾，删了没有任何影响
LEVEL_CACHED = "cached"  # 缓存，删后程序会自己重建或重新下载
LEVEL_RISKY = "risky"    # 有副作用，默认不勾选

DEFAULT_CHECKED = (LEVEL_SAFE, LEVEL_CACHED)

SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:")

# onefile 打包时，程序自身会解压到 %TEMP%\\_MEIxxxxx。清理临时文件时
# 必须跳过它，否则等于把自己脚下的地板拆了。
SELF_RUNTIME_DIR = getattr(sys, "_MEIPASS", None)


# ---------------------------------------------------------------- 通用工具

def norm(path):
    """规范化路径，用于比较。失败时退回原值。"""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except (OSError, ValueError):
        return os.path.normcase(path)


def is_within(path, root):
    """path 是否等于 root 或位于 root 之下。"""
    n = norm(path)
    r = norm(root)
    return n == r or n.startswith(r + os.sep)


def human_size(n):
    """把字节数格式化成人类可读。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%d B" % int(n) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n


def disk_usage(drive):
    """
    读取磁盘容量。drive 传 'C' 或 'C:' 都行。
    返回 (总, 已用, 可用) 字节数；失败返回 None。
    """
    letter = drive.rstrip(":").rstrip("\\/")
    if not letter:
        return None
    try:
        u = shutil.disk_usage(letter + ":" + os.sep)
        return u.total, u.used, u.free
    except OSError:
        return None


def long_path(path):
    """
    把 8.3 短名（C:\\Users\\ADMINI~1\\...）展开成完整长名。
    只影响界面显示，不影响任何判断逻辑。展开失败时原样返回。
    """
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(path, buf, 32768)
        if n and n < 32768 and buf.value:
            return buf.value
    except Exception:
        pass
    return path


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------- 路径守卫

_USER_PROFILES = None


def _local_user_profiles():
    """
    列出本机所有本地用户的主目录。

    结果只算一次就缓存住。guard() 每次校验都要用它，而清理一个几万条目的
    临时目录会调用 guard() 几万次——实测每次 scandir C:\\Users 约 0.086 ms，
    占了 guard() 总开销的 77%（单次 0.111 ms），纯属白烧。
    """
    global _USER_PROFILES
    if _USER_PROFILES is not None:
        return _USER_PROFILES

    out = []
    users_root = os.path.join(SYSTEM_DRIVE + os.sep, "Users")
    if os.path.isdir(users_root):
        try:
            with os.scandir(users_root) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                            out.append(e.path)
                    except OSError:
                        continue
        except OSError:
            pass
    return out


def _build_protected_roots():
    """
    绝对保护区。落在这些目录下的东西，即使命中了白名单也不许删，
    除非该白名单目标显式声明 allow_protected=True（只有确实身处系统目录
    的 Windows\\Temp、软件分发缓存等少数目标才这么标）。
    """
    prot = [
        os.path.join(SYSTEM_DRIVE + os.sep, "Windows"),
        os.path.join(SYSTEM_DRIVE + os.sep, "Program Files"),
        os.path.join(SYSTEM_DRIVE + os.sep, "Program Files (x86)"),
        os.path.join(SYSTEM_DRIVE + os.sep, "ProgramData"),
        os.path.join(SYSTEM_DRIVE + os.sep, "Recovery"),
        os.path.join(SYSTEM_DRIVE + os.sep, "$Recycle.Bin"),
        os.path.join(SYSTEM_DRIVE + os.sep, "System Volume Information"),
        os.path.join(SYSTEM_DRIVE + os.sep, "Boot"),
        os.path.join(SYSTEM_DRIVE + os.sep, "EFI"),
    ]
    # 用户目录下的「内容型」目录：放的是用户资料与程序配置，永远不碰
    user_sensitive = (
        ("AppData", "Roaming"),
        ("AppData", "LocalLow"),
        ("AppData", "Local", "Packages"),
        "Documents", "Desktop", "Pictures", "Videos", "Music", "Downloads",
        "OneDrive", "Favorites", "Links", "Contacts", "Saved Games",
        "Searches", "3D Objects", "NetHood", "PrintHood", "SendTo",
        "Templates", "Recent",
    )
    for home in _local_user_profiles():
        for rel in user_sensitive:
            parts = rel if isinstance(rel, tuple) else (rel,)
            prot.append(os.path.join(home, *parts))
    return [norm(p) for p in prot]


PROTECTED_ROOTS = _build_protected_roots()

# 硬禁区：任何目标都不得越过，allow_protected 也不行
_NEVER = [
    norm(os.path.join(SYSTEM_DRIVE + os.sep, "Windows", sub)) for sub in (
        "System32", "SysWOW64", "WinSxS", "assembly", "Microsoft.NET",
        "Fonts", "Installer", "servicing", "Boot", "INF", "SystemApps",
        "PolicyDefinitions", "diagnostics",
    )
]


def is_empty_dir(path):
    """严格判空：一条条目都没有才算空。带 desktop.ini 的不会被误判。"""
    try:
        with os.scandir(path) as it:
            for _ in it:
                return False
        return True
    except OSError:
        return False


class GuardError(Exception):
    """路径未通过安全校验。"""


def guard(path, allowed_root=None, allow_protected=False):
    """
    删除前的最后一道闸门。不通过就抛 GuardError。

    allowed_root     本次删除所属的白名单目标根目录，path 必须落在其内
    allow_protected  该目标是否被授权越过保护区（仅系统级临时目录等少数项）
    """
    if not path:
        raise GuardError("空路径")

    n = norm(path)

    # 1) 绝不删盘符根目录
    _drive, tail = os.path.splitdrive(n)
    if tail in ("", os.sep):
        raise GuardError("拒绝删除盘符根目录：%s" % path)

    # 2) 绝不删用户主目录本身
    for home in _local_user_profiles():
        if n == norm(home):
            raise GuardError("拒绝删除用户主目录：%s" % path)

    # 3) 绝不删当前程序自身的运行目录（onefile 时位于 %TEMP%）
    if SELF_RUNTIME_DIR and is_within(n, SELF_RUNTIME_DIR):
        raise GuardError("拒绝删除本程序自身的运行目录")

    # 4) 白名单归属：必须落在允许的根目录内
    if allowed_root is not None and not is_within(n, allowed_root):
        raise GuardError("路径不在允许范围内：%s" % path)

    # 5) 硬禁区
    for p in _NEVER:
        if n == p or n.startswith(p + os.sep):
            raise GuardError("路径位于系统关键目录，禁止删除：%s" % path)

    # 6) 保护区
    if not allow_protected:
        for p in PROTECTED_ROOTS:
            if n == p or n.startswith(p + os.sep):
                raise GuardError("路径位于保护区，禁止删除：%s" % path)

    return True


# ---------------------------------------------------------------- 清理目标

class Target(object):
    """
    一个清理目标。可能对应一个目录，也可能对应一组（glob 展开的）目录。

    exists 是三态：
        None  还没扫描过
        True  扫描确认存在
        False 扫描确认不存在

    mode:
        contents   删除目录内的全部内容，保留目录本身（推荐，程序期望目录存在）
        tree       整个目录连根删除
        pattern    只删目录下匹配 pattern 的文件
        recyclebin 清空回收站，走系统 API
    """

    __slots__ = ("key", "label", "paths", "mode", "level", "note",
                 "need_admin", "allow_protected", "pattern",
                 "size", "files", "errcount", "checked", "exists")

    def __init__(self, key, label, paths, mode="contents", level=LEVEL_SAFE,
                 note="", need_admin=False, allow_protected=False, pattern=None):
        self.key = key                 # 全局唯一的界面 id
        self.label = label
        self.paths = list(paths)
        self.mode = mode
        self.level = level
        self.note = note
        self.need_admin = need_admin
        self.allow_protected = allow_protected
        self.pattern = pattern
        self.size = 0
        self.files = 0
        self.errcount = 0
        self.checked = level in DEFAULT_CHECKED
        self.exists = None

    @property
    def path(self):
        """主路径，用于展示。"""
        return self.paths[0] if self.paths else ""

    def __repr__(self):
        return "<Target %s %s>" % (self.label, self.paths)


# 字段顺序：key, 显示名, 路径模板, mode, level, 说明, 需管理员, 越过保护区, pattern
TARGET_SPECS = [
    # ---------------- 临时文件 ----------------
    ("temp_user", "用户临时文件", r"%TEMP%", "contents", LEVEL_SAFE,
     "程序运行时产生的临时文件，可随时删除。正在被占用的会自动跳过",
     False, False, None),
    ("temp_system", "系统临时文件", r"C:\Windows\Temp", "contents", LEVEL_SAFE,
     "系统级临时文件。需要管理员权限，普通权限下会被跳过",
     True, True, None),
    ("wer", "错误报告存档", r"%LOCALAPPDATA%\Microsoft\Windows\WER", "contents",
     LEVEL_SAFE, "程序崩溃时生成的错误报告", False, False, None),
    ("crashdumps", "崩溃转储文件", r"%LOCALAPPDATA%\CrashDumps", "contents",
     LEVEL_SAFE, "崩溃时转储的内存镜像，单个文件可达数百 MB", False, False, None),

    # ---------------- 系统缓存 ----------------
    ("wu_download", "Windows 更新缓存",
     r"C:\Windows\SoftwareDistribution\Download", "contents", LEVEL_SAFE,
     "已下载的更新安装包，装完就没用了。需要管理员权限", True, True, None),
    ("delivery", "传递优化缓存",
     r"C:\Windows\SoftwareDistribution\DeliveryOptimization", "contents",
     LEVEL_SAFE, "Windows 更新的 P2P 分发缓存。需要管理员权限", True, True, None),
    ("thumbcache", "缩略图缓存", r"%LOCALAPPDATA%\Microsoft\Windows\Explorer",
     "pattern", LEVEL_SAFE, "资源管理器的缩略图数据库，会自动重建（只删 *.db）",
     False, False, ("thumbcache_*.db", "iconcache_*.db")),

    # ---------------- 显卡着色器缓存 ----------------
    ("d3d", "D3D 着色器缓存", r"%LOCALAPPDATA%\D3DSCache", "contents",
     LEVEL_SAFE, "DirectX 着色器编译缓存，游戏会自动重建", False, False, None),
    ("nvdx", "NVIDIA 着色器缓存", r"%LOCALAPPDATA%\NVIDIA\DXCache", "contents",
     LEVEL_SAFE, "N 卡着色器缓存，会自动重建", False, False, None),
    ("nvgc", "NVIDIA GL 缓存", r"%LOCALAPPDATA%\NVIDIA\GLCache", "contents",
     LEVEL_SAFE, "N 卡 OpenGL 着色器缓存", False, False, None),
    ("nvcache", "NVIDIA NV_Cache", r"%LOCALAPPDATA%\NVIDIA Corporation\NV_Cache",
     "contents", LEVEL_SAFE, "NVIDIA 驱动缓存", False, False, None),
    ("amdcache", "AMD 着色器缓存", r"%LOCALAPPDATA%\AMD\DxCache", "contents",
     LEVEL_SAFE, "A 卡着色器缓存，会自动重建", False, False, None),
    ("amdcache2", "AMD GL 缓存", r"%LOCALAPPDATA%\AMD\GLCache", "contents",
     LEVEL_SAFE, "A 卡 OpenGL 着色器缓存", False, False, None),

    # ---------------- 浏览器缓存（只碰 Cache，不碰书签密码 Cookie）----------------
    ("chrome", "Chrome 网页缓存",
     r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cache", "contents",
     LEVEL_SAFE, "网页缓存。不动书签、密码、Cookie、登录状态", False, False, None),
    ("edge", "Edge 网页缓存",
     r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache", "contents",
     LEVEL_SAFE, "网页缓存。不动书签、密码、Cookie、登录状态", False, False, None),
    ("ffcache", "Firefox 网页缓存",
     r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2", "contents",
     LEVEL_SAFE, "只删 cache2，不动配置、书签、密码", False, False, None),
    ("chromeext", "Chrome 扩展缓存", r"%LOCALAPPDATA%\ChromeExtensionCache",
     "contents", LEVEL_SAFE, "Chrome 扩展的缓存文件", False, False, None),

    # ---------------- 开发工具缓存 ----------------
    ("pip", "pip 缓存", r"%LOCALAPPDATA%\pip\Cache", "contents", LEVEL_CACHED,
     "Python 包下载缓存，下次装包会重新下载", False, False, None),
    ("npm", "npm 缓存", r"%LOCALAPPDATA%\npm-cache", "contents", LEVEL_CACHED,
     "npm 包缓存，会自动重建", False, False, None),
    ("pnpm", "pnpm 缓存", r"%LOCALAPPDATA%\pnpm-cache", "contents", LEVEL_CACHED,
     "pnpm 包缓存", False, False, None),
    ("yarn", "Yarn 缓存", r"%LOCALAPPDATA%\Yarn\Cache", "contents", LEVEL_CACHED,
     "Yarn 包缓存", False, False, None),
    ("uv", "uv 缓存", r"%LOCALAPPDATA%\uv\cache", "contents", LEVEL_CACHED,
     "uv 包缓存。只清 cache，不动 uv 管理的 Python 解释器", False, False, None),
    ("nuget", "NuGet 包缓存", r"%USERPROFILE%\.nuget\packages", "contents",
     LEVEL_CACHED, "C# 包缓存，删后首次编译会重新下载", False, False, None),
    ("gomod", "Go 模块缓存", r"%USERPROFILE%\go\pkg\mod", "contents",
     LEVEL_CACHED, "Go 模块缓存，删后首次构建会重新下载", False, False, None),
    ("cargo", "Cargo 缓存", r"%USERPROFILE%\.cargo\registry", "contents",
     LEVEL_CACHED, "Rust 包缓存", False, False, None),
    ("gradle", "Gradle 缓存", r"%USERPROFILE%\.gradle\caches", "contents",
     LEVEL_CACHED, "Java 构建缓存，删后首次构建会重新下载", False, False, None),
    ("gencache", "通用工具缓存", r"%USERPROFILE%\.cache", "contents",
     LEVEL_CACHED, "各类命令行工具自建的缓存目录", False, False, None),

    # ---------------- 应用更新残留 ----------------
    ("updater", "应用更新残留", r"%LOCALAPPDATA%\*-updater", "contents",
     LEVEL_CACHED, "各类客户端下载完的更新包，装完即无用。下次更新会重新下载",
     False, False, None),

    # ---------------- 有副作用的，默认不勾 ----------------
    ("winold", "旧版 Windows 文件", r"C:\Windows.old", "tree", LEVEL_RISKY,
     "系统升级前的旧文件。删后无法回退到旧版本。需要管理员权限", True, True, None),
    ("recyclebin", "回收站（清空）", "", "recyclebin", LEVEL_RISKY,
     "彻底清空回收站，不可撤销。用「送回收站」模式清出的东西也在这里面",
     False, False, None),
]


def _expand_spec(tmpl):
    """展开环境变量，带通配符的做 glob，顺手把短名转成长名。"""
    expanded = os.path.expandvars(tmpl)
    if not expanded:
        return []
    if "*" in expanded or "?" in expanded:
        return sorted(long_path(p) for p in glob.glob(expanded)
                      if os.path.isdir(p))
    return [long_path(expanded)]


def build_targets():
    """把 TARGET_SPECS 展开成 Target 列表。每一项都拿到全局唯一的 key。"""
    out = []
    for idx, (key, label, tmpl, mode, level, note, need_admin,
              allow_protected, pattern) in enumerate(TARGET_SPECS):
        if mode == "recyclebin":
            out.append(Target("uid%d" % idx, label, [], mode, level, note,
                              need_admin, allow_protected, pattern))
            continue

        paths = _expand_spec(tmpl)
        if len(paths) <= 1:
            out.append(Target("uid%d" % idx, label, paths or
                              [long_path(os.path.expandvars(tmpl))], mode, level,
                              note, need_admin, allow_protected, pattern))
        else:
            # 通配展开出多个目录时合成一个目标，避免列表被刷屏
            out.append(Target("uid%d" % idx, label, paths, mode, level,
                              "%s（共 %d 个目录）" % (note, len(paths)),
                              need_admin, allow_protected, pattern))
    return out


# ---------------------------------------------------------------- 扫描

def _measure(root, pattern=None):
    """
    统计 root 下的字节数与文件数。只读。
    返回 (bytes, files, errcount)
    """
    total = 0
    files = 0
    errs = 0

    if pattern:
        for pat in pattern:
            for p in glob.glob(os.path.join(root, pat)):
                try:
                    if os.path.isfile(p):
                        total += os.path.getsize(p)
                        files += 1
                    elif os.path.isdir(p):
                        b, f, e = _measure(p)
                        total += b
                        files += f
                        errs += e
                except OSError:
                    errs += 1
        return total, files, errs

    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        errs += 1
        except OSError:
            errs += 1
    return total, files, errs


def measure_recyclebin():
    """
    遍历回收站的数据目录统计大小。

    比 SHQueryRecycleBin 快得多：本机 16 万项时 API 要 50 秒，遍历只要 6 秒。
    API 在项数极多时会逐个解析 $I 元数据，开销随项数急剧上升。
    无权限读取的账户目录（如系统账户）会被跳过，所以结果会比 API 略小。
    """
    total = 0
    files = 0
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        rb = letter + ":" + os.sep + "$Recycle.Bin"
        if not os.path.isdir(rb):
            continue
        try:
            with os.scandir(rb) as it:
                for e in it:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    try:
                        b, f, _ = _measure(e.path)
                        total += b
                        files += f
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def scan_targets(targets, progress=None, cancel=None, skip_recyclebin=True):
    """
    扫描全部目标。只读操作，不修改任何文件。

    skip_recyclebin：回收站统计偏慢（本机约 6 秒），默认跳过，
    由调用方另起线程去跑，免得拖住其他只需不到 1 秒的项目。
    """
    for t in targets:
        if cancel is not None and cancel.is_set():
            return

        if t.mode == "recyclebin":
            if skip_recyclebin:
                continue
            b, n = measure_recyclebin()
            t.exists = True
            t.size, t.files = b, n
            t.errcount = 0
            if progress:
                progress(t)
            continue

        total = files = errs = 0
        found = False
        for p in t.paths:
            if not os.path.exists(p):
                continue
            found = True
            b, f, e = _measure(p, t.pattern)
            total += b
            files += f
            errs += e
        t.exists = found
        t.size, t.files, t.errcount = total, files, errs
        if progress:
            progress(t)


# ---------------------------------------------------------------- 删除

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 3
FOF_MULTIDESTFILES = 0x0001
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400
FOF_ALLOWUNDO = 0x0040


def _send_to_recyclebin(paths):
    """
    把一批路径送进回收站。返回 (成功数, 幸存列表)。
    一次调用处理整批，比逐个调用快得多。
    """
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return 0, []

    co_ready = False
    try:
        ctypes.windll.ole32.CoInitialize(None)
        co_ready = True
    except Exception:
        pass

    try:
        buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")
        op = SHFILEOPSTRUCTW()
        ctypes.memset(ctypes.byref(op), 0, ctypes.sizeof(op))
        op.wFunc = FO_DELETE
        op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI
                     | FOF_SILENT | FOF_MULTIDESTFILES)

        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        shell32.SHFileOperationW(ctypes.byref(op))

        survivors = [p for p in paths if os.path.exists(p)]
        return len(paths) - len(survivors), survivors
    finally:
        # CoInitialize 要配对的 CoUninitialize，否则每次调用都往计数上加
        if co_ready:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def _rmtree_force(path):
    """删文件/目录，遇到只读的先去掉只读属性再删。"""

    def onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)


def empty_recyclebin():
    """
    清空回收站。不可逆。

    SHEmptyRecycleBinW 成功返回 S_OK(0)，回收站本来就是空的返回 S_FALSE(1)，
    两者都算成功；其它才是真失败。以前无条件 return True，失败也报成功。
    """
    try:
        fn = ctypes.windll.shell32.SHEmptyRecycleBinW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
        fn.restype = ctypes.c_long
        SHERB_NOCONFIRMATION = 0x1
        SHERB_NOPROGRESSUI = 0x2
        SHERB_NOSOUND = 0x4
        rc = fn(None, None,
                SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
        return rc in (0, 1)
    except Exception:
        return False


def _clear_one_root(root, target, use_recyclebin, log):
    """
    清空单个根目录下的内容。返回 (释放字节, 删除文件数, 失败项数)。
    每一个待删路径都要过 guard()，未通过的直接跳过并计数。
    """
    freed = 0
    removed_files = 0
    failed = 0

    if target.mode == "pattern":
        victims = []
        for pat in (target.pattern or ()):
            victims.extend(glob.glob(os.path.join(root, pat)))
    elif target.mode == "tree":
        victims = [root]
    else:  # contents
        try:
            with os.scandir(root) as it:
                victims = [e.path for e in it]
        except OSError as exc:
            if log:
                log("  无法读取 %s：%s" % (root, exc))
            return 0, 0, 1

    safe = []
    for v in victims:
        try:
            guard(v, allowed_root=root, allow_protected=target.allow_protected)
        except GuardError as exc:
            failed += 1
            if log:
                log("  [拒绝] %s" % exc)
            continue
        safe.append(v)

    if not safe:
        return 0, 0, failed

    # 先量体积——删完就量不到了
    for v in safe:
        try:
            if os.path.islink(v):
                continue
            if os.path.isfile(v):
                freed += os.path.getsize(v)
                removed_files += 1
            elif os.path.isdir(v):
                b, f, _ = _measure(v)
                freed += b
                removed_files += f
        except OSError:
            pass

    if use_recyclebin:
        _ok, survivors = _send_to_recyclebin(safe)
        leftovers = survivors
        if log and survivors:
            log("  %d 项未能送入回收站（多被占用或权限不足）" % len(survivors))
    else:
        for v in safe:
            try:
                _rmtree_force(v)
            except OSError as exc:
                if log:
                    log("  [失败] %s：%s" % (os.path.basename(v), exc))
        # _rmtree_force 里 shutil.rmtree(onerror=...) 会把子项错误吞掉，
        # 只看有没有抛异常，会把「根本没删掉」报成成功。回头查一遍残留。
        leftovers = [v for v in safe if os.path.exists(v)]
        if log and leftovers:
            log("  %d 项未删净（多被占用或权限不足）" % len(leftovers))

    if leftovers:
        failed += len(leftovers)
        # 没删掉的要从战果里扣回来，否则报出来的「释放 X」是虚的
        for v in leftovers:
            try:
                if os.path.isfile(v):
                    freed -= os.path.getsize(v)
                    removed_files -= 1
                elif os.path.isdir(v):
                    b, f, _ = _measure(v)
                    freed -= b
                    removed_files -= f
            except OSError:
                pass

    return max(freed, 0), max(removed_files, 0), failed


def clear_target(target, use_recyclebin=False, log=None):
    """清空一个目标。返回 (释放字节, 删除文件数, 失败项数)。"""
    if target.mode == "recyclebin":
        # 扫描时已经量过了，别再遍历一遍（16 万项要 6 秒）
        before = target.size if target.exists else measure_recyclebin()[0]
        ok = empty_recyclebin()
        if not ok and log:
            log("  清空回收站失败（被占用或权限不足）")
        return before, 0, 0 if ok else 1

    freed = files = failed = 0
    for root in target.paths:
        if not os.path.exists(root):
            continue
        b, f, e = _clear_one_root(root, target, use_recyclebin, log)
        freed += b
        files += f
        failed += e
    return freed, files, failed


# ---------------------------------------------------------------- 空文件夹扫描

# 这些目录名一律跳过，不管出现在哪一层
SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
    "windows", "program files", "program files (x86)", "programdata",
    "recovery", "perflogs", "config.msi", "msocache",
    "appdata", "onedrive", "node_modules", ".git", ".svn", ".hg",
    "venv", ".venv", "env", "__pycache__", ".idea", ".vscode", ".vs",
    "packages", "obj", "bin", "target", "dist", "build", "out",
    ".cache", ".gradle", ".m2", ".npm", ".nuget", ".cargo", ".rustup",
    ".ssh", ".gnupg", "temp", "tmp", "cache", "caches",
    "3d objects", "saved games", "nethood", "printhood", "sendto",
    "templates", "recent", "favorites", "links", "contacts", "searches",
}


def _empty_scan_roots():
    """
    空文件夹的扫描根：非系统盘的根目录 + 用户主目录（会跳过敏感子目录）。
    系统盘的系统目录一律不进入。
    """
    roots = []
    sys_letter = SYSTEM_DRIVE.rstrip(":").upper()
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        if letter == sys_letter:
            continue
        p = letter + ":" + os.sep
        if os.path.isdir(p):
            roots.append(p)
    roots.extend(_local_user_profiles())
    return roots


def scan_empty_dirs(progress=None, cancel=None, limit=20000):
    """
    找出空文件夹。只读。
    保护区内的路径会被剔除；「空」按严格判定（一条条目都没有）。
    """
    found = []
    for root in _empty_scan_roots():
        if cancel is not None and cancel.is_set():
            break
        stack = [root]
        while stack:
            if cancel is not None and cancel.is_set():
                break
            d = stack.pop()
            is_root = (d == root)

            dn = norm(d)
            if any(dn == p or dn.startswith(p + os.sep) for p in PROTECTED_ROOTS):
                continue

            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError:
                continue

            if not entries:
                if not is_root:
                    found.append(d)
                    if progress and len(found) % 50 == 0:
                        progress(len(found))
                    if len(found) >= limit:
                        return found
                continue

            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in SKIP_DIR_NAMES:
                            stack.append(e.path)
                except OSError:
                    continue
    return found


# ---------------------------------------------------------------- 主题
#
# 赛博朋克配色：近黑底 + 霓虹青主色 + 品红强调 + 琥珀警示。
# tkinter 没有圆角、发光和透明度，所以「发光」靠多层描边叠加近似，
# 「切角」靠 Canvas 多边形，控件本体仍是原生控件，可用性不打折。

BG        = "#05070d"   # 窗口底色
BG_DEEP   = "#03050a"   # 更深一层，用于凹槽与标题栏
PANEL     = "#0a0f18"   # 面板
PANEL_HI  = "#111d2b"   # 面板高亮（悬停）
GRID      = "#0c1721"   # 背景网格线
BORDER    = "#173040"   # 普通描边
BORDER_HI = "#1f4a61"   # 高亮描边
CYAN      = "#00e5ff"   # 主霓虹
CYAN_DIM  = "#0b6d80"
MAGENTA   = "#ff2d95"   # 强调霓虹
PURPLE    = "#a24dff"
AMBER     = "#ffb300"   # 警示（有副作用的项）
GREEN     = "#3dff9a"
TEXT      = "#b6e6f2"   # 正文
TEXT_DIM  = "#4e7484"   # 次要文字
TEXT_OFF  = "#243845"   # 禁用

FONT_EN = "Consolas"            # 数字与西文，等宽
FONT_UI = "Microsoft YaHei UI"  # 中文

TITLEBAR_H = 40                 # 自绘标题栏高度
RESIZE_EDGE = 6                 # 边缘缩放的拖动热区宽度
MIN_WIN = (920, 668)            # 窗口最小尺寸


def _mix(color, factor):
    """按系数调暗（<1）或调亮（>1）颜色，用来叠出假发光。"""
    try:
        c = color.lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        f = lambda v: max(0, min(255, int(v * factor)))  # noqa: E731
        return "#%02x%02x%02x" % (f(r), f(g), f(b))
    except Exception:
        return color


class NeonButton(object):
    """
    赛博朋克按钮：对角切角的描边块，悬停时点亮成强调色。

    用组合而不是继承 tk.Canvas —— 模块顶层 tk 还是 None，
    继承会在 import 时就炸。转发 pack/grid/place 即可当普通控件用。
    状态用 set_state() 而不是 configure(state=...)，免得覆盖 Canvas 的 configure。
    """

    CUT = 7  # 左上与右下的切角边长

    def __init__(self, master, text, command=None, width=132, height=34,
                 color=CYAN, hot=MAGENTA, font=None):
        self._cmd = command
        self._color = color
        self._hot = hot
        self._w, self._h = width, height
        self._text = text
        self._font = font or (FONT_UI, 10, "bold")
        self._enabled = True
        self._hover = False
        self._pressed = False

        self.cv = tk.Canvas(master, width=width, height=height, bg=BG,
                            highlightthickness=0, bd=0, takefocus=0,
                            cursor="hand2")
        self.cv.bind("<Enter>", self._on_enter)
        self.cv.bind("<Leave>", self._on_leave)
        self.cv.bind("<Button-1>", self._on_press)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    # -------- 布局转发
    def pack(self, **kw):
        self.cv.pack(**kw)
        return self

    def grid(self, **kw):
        self.cv.grid(**kw)
        return self

    def place(self, **kw):
        self.cv.place(**kw)
        return self

    # -------- 交互
    def _on_enter(self, _e):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._pressed = True
        self._draw()

    def _on_release(self, _e):
        fired = self._pressed
        self._pressed = False
        self._draw()
        if fired and self._enabled and self._cmd:
            self._cmd()

    # -------- 对外接口
    def set_state(self, state):
        enabled = (state == "normal")
        if enabled != self._enabled:
            self._enabled = enabled
            self.cv.configure(cursor="hand2" if enabled else "arrow")
            self._draw()

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self._draw()

    # -------- 绘制
    def _poly(self, pad):
        c = self.CUT
        x0, y0 = pad, pad
        x1, y1 = self._w - pad, self._h - pad
        return (x0 + c, y0, x1, y0, x1, y1 - c, x1 - c, y1,
                x0, y1, x0, y0 + c)

    def _draw(self):
        self.cv.delete("all")
        if not self._enabled:
            line, fill, txt = TEXT_OFF, PANEL, TEXT_OFF
        elif self._pressed:
            line, fill, txt = self._hot, _mix(self._color, 0.30), "#ffffff"
        elif self._hover:
            line, fill, txt = self._hot, PANEL_HI, self._hot
        else:
            line, fill, txt = self._color, PANEL, self._color

        # 外层稀薄描边 = 发散光晕
        self.cv.create_polygon(self._poly(0), fill="", width=1,
                               outline=_mix(line, 0.34))
        self.cv.create_polygon(self._poly(2), fill=fill, outline=line, width=1)

        mid = self._h / 2.0
        self.cv.create_line(2, mid, 7, mid, fill=line, width=1)
        self.cv.create_line(self._w - 7, mid, self._w - 2, mid, fill=line, width=1)
        self.cv.create_text(self._w / 2.0, mid, text=self._text, fill=txt,
                            font=self._font)


class NeonRadio(object):
    """
    自绘单选按钮：方角框 + 内部亮点。

    ttk 在 clam 下的 indicatorcolor 起不来（未选中的点仍然是系统白），
    索性自己画，色彩和发光都能精确控制。
    """

    BOX = 15

    def __init__(self, master, text, variable, value, color=CYAN):
        self.variable = variable
        self.value = value
        self._color = color
        self._text = text
        self._hover = False

        try:
            from tkinter import font as tkfont
            tw = tkfont.Font(font=(FONT_UI, 9)).measure(text)
        except Exception:
            tw = len(text) * 12

        self.cv = tk.Canvas(master, width=self.BOX + 12 + tw, height=24,
                            bg=BG, highlightthickness=0, bd=0, takefocus=0,
                            cursor="hand2")
        self.cv.bind("<Enter>", self._on_enter)
        self.cv.bind("<Leave>", self._on_leave)
        self.cv.bind("<Button-1>", self._pick)
        try:
            self.variable.trace_add("write", lambda *_: self._draw())
        except Exception:
            pass
        self._draw()

    def pack(self, **kw):
        self.cv.pack(**kw)
        return self

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _pick(self, _e):
        self.variable.set(self.value)

    def _draw(self):
        self.cv.delete("all")
        try:
            on = bool(self.variable.get()) == bool(self.value)
        except Exception:
            on = False

        line = self._color if on else (BORDER_HI if self._hover else BORDER)
        txt = TEXT if (on or self._hover) else TEXT_DIM
        b = self.BOX

        # 外框（稀薄）+ 内框（实色）叠出发光感
        self.cv.create_rectangle(1, 4, 1 + b + 2, 4 + b + 2, fill="",
                                 outline=_mix(line, 0.32))
        self.cv.create_rectangle(2, 5, 2 + b, 5 + b, fill=BG_DEEP, outline=line)
        if on:
            self.cv.create_rectangle(2 + 5, 5 + 5, 2 + b - 5, 5 + b - 5,
                                     fill=self._color, outline="")
        self.cv.create_text(b + 13, 13, text=self._text, anchor="w", fill=txt,
                            font=(FONT_UI, 9))


def _set_window_icon(root):
    """换掉窗口与任务栏图标。打包后资源解压在 _MEIPASS 里。"""
    for base in (getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__))):
        if not base:
            continue
        p = os.path.join(base, "icon.ico")
        if os.path.isfile(p):
            try:
                root.iconbitmap(default=p)
                return True
            except Exception:
                continue
    return False


def pick_fonts(root):
    """
    挑一个可用的等宽字体。

    Cascadia 的数字带斜杠，比 Consolas 更有仪表盘味；系统没有就退回 Consolas。
    """
    global FONT_EN, FONT_UI
    try:
        from tkinter import font as tkfont
        have = set(tkfont.families(root))
    except Exception:
        return
    for name in ("Cascadia Mono", "Cascadia Code", "Consolas"):
        if name in have:
            FONT_EN = name
            break
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei"):
        if name in have:
            FONT_UI = name
            break


def apply_dark_titlebar(root):
    """
    把 Windows 原生标题栏切成深色，和内部主题连成一片。

    必须用 GetParent(winfo_id()) 取真正的顶层 HWND ——
    winfo_id() 给的是 Tk 的内层窗口，拿它调用一律返回 E_HANDLE。
    窗口还没映射时也可能失败，所以调用方会在显示后再补一次。
    """
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            return False
        fn = ctypes.windll.dwmapi.DwmSetWindowAttribute
        fn.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD]
        fn.restype = ctypes.c_long
        value = ctypes.c_int(1)
        for attr in (20, 19):  # 20 = Win10 1903+，19 = 1809
            if fn(hwnd, attr, ctypes.cast(ctypes.byref(value), ctypes.c_void_p),
                  ctypes.sizeof(value)) == 0:
                return True
    except Exception:
        pass
    return False


def setup_style(root):
    """
    把 ttk 切到 clam 再逐项染色。

    必须换主题：Windows 原生 vista 主题不接受颜色覆盖，
    控件会顽固地保持灰白。clam 是唯一能整身改色的内置主题。
    """
    from tkinter import ttk

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # --- 选项卡
    style.configure("Cyber.TNotebook", background=BG, borderwidth=0,
                    tabmargins=(0, 0, 0, 0))
    style.configure("Cyber.TNotebook.Tab", background=BG_DEEP,
                    foreground=TEXT_DIM, font=(FONT_UI, 9, "bold"),
                    padding=(22, 8), borderwidth=0)
    style.map("Cyber.TNotebook.Tab",
              background=[("selected", PANEL), ("active", PANEL_HI)],
              foreground=[("selected", CYAN), ("active", TEXT)])

    # --- 表格
    style.configure("Cyber.Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=TEXT, bordercolor=BORDER, borderwidth=0,
                    relief="flat", rowheight=25, font=(FONT_UI, 9))
    style.map("Cyber.Treeview",
              background=[("selected", "#0e3b4d")],
              foreground=[("selected", "#ffffff")])
    style.configure("Cyber.Treeview.Heading", background=BG_DEEP,
                    foreground=CYAN, relief="flat", borderwidth=0,
                    font=(FONT_UI, 9, "bold"), padding=(10, 7),
                    bordercolor=BORDER)
    style.map("Cyber.Treeview.Heading",
              background=[("active", PANEL_HI)],
              foreground=[("active", MAGENTA)])

    # --- 滚动条：砍掉箭头，只留轨道和滑块
    for orient in ("Vertical", "Horizontal"):
        name = "Cyber.%s.TScrollbar" % orient
        style.configure(name, background=BORDER_HI, troughcolor=BG_DEEP,
                        bordercolor=BG_DEEP, arrowcolor=CYAN,
                        darkcolor=BORDER_HI, lightcolor=BORDER_HI,
                        relief="flat", borderwidth=0, arrowsize=0)
        style.map(name, background=[("active", CYAN), ("pressed", MAGENTA)])
    style.layout("Cyber.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough",
         {"children": [("Vertical.Scrollbar.thumb",
                        {"expand": "1", "sticky": "nswe"})],
          "sticky": "ns"})])
    style.layout("Cyber.Horizontal.TScrollbar", [
        ("Horizontal.Scrollbar.trough",
         {"children": [("Horizontal.Scrollbar.thumb",
                        {"expand": "1", "sticky": "nswe"})],
          "sticky": "ew"})])


# ---------------------------------------------------------------- GUI

def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


CHECKED = "\u2611"     # ☑
UNCHECKED = "\u2610"   # ☐
PENDING = "\u2014"     # —


class CleanerApp(object):

    def __init__(self, root, autoscan=True):
        self.root = root
        self.targets = build_targets()
        self.empty_checked = set()
        self.msgq = queue.Queue()
        self.busy = False
        self.alive = True
        self.cancel = threading.Event()
        self.use_recyclebin = tk.BooleanVar(value=False)

        pick_fonts(root)
        _set_window_icon(root)
        root.title("%s  ::  v%s" % (APP_NAME, __version__))
        # 根窗口底色当窗口外框用：内容区四周留 1px 露出它
        root.configure(bg=BORDER_HI)
        root.geometry("1000x772")
        root.minsize(*MIN_WIN)

        # 无边框窗口的状态
        self._maxed = False
        self._restore_geo = None
        self._drag_off = None
        self._resizing = None
        self._frameless = False
        self._minimizing = False
        self._cur_cursor = ""

        # 顶部仪表盘要用到的动态文本句柄，_draw_head() 之后才有值
        self._drive_item = None
        self._drive_sub = None
        self._drive_others = None
        self._head_w = 0
        self._head_h = 58
        self._blink_on = True

        setup_style(root)
        self._go_frameless()
        if not self._frameless:
            # 极少见：无边框没设上，退回原生边框，至少把标题栏切深色
            apply_dark_titlebar(root)
        self._build_ui()
        self._refresh_drive_info()
        self.log("就绪。扫描过程只读，不会删任何东西。")
        if not is_admin():
            self.log("当前非管理员权限：「系统临时文件」「Windows 更新缓存」"
                     "等几项会被跳过，其余照常。")

        self.root.after(120, self._drain_queue)
        self.root.after(620, self._tick_blink)
        if not self._frameless:
            self.root.after(90, lambda: apply_dark_titlebar(root))
            self.root.after(600, lambda: apply_dark_titlebar(root))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if autoscan:
            self.root.after(400, self.on_scan)

    # ---------------- 界面 ----------------

    def _build_ui(self):
        from tkinter import ttk

        # --- 自绘标题栏。无边框窗口全靠它：拖动、缩放、最小化/最大化/关闭
        self.tb = tk.Canvas(self.root, height=TITLEBAR_H, bg=BG_DEEP,
                            highlightthickness=0, bd=0)
        self.tb.pack(fill="x", side="top", padx=1, pady=(1, 0))
        self.tb.bind("<Configure>", self._on_titlebar_resize)
        self.tb.bind("<Button-1>", self._drag_begin)
        self.tb.bind("<B1-Motion>", self._drag_move)
        self.tb.bind("<ButtonRelease-1>", self._drag_end)
        # 双击标题栏最大化/还原，沿用系统习惯
        self.tb.bind("<Double-Button-1>", lambda _e: self._toggle_max())

        pad = tk.Frame(self.root, bg=BG)
        pad.pack(fill="both", expand=True, padx=1, pady=(0, 1))

        # root 背景色是 BORDER_HI，上面两个 padx/pady=1 让它在四周露出一圈，
        # 充当窗口外框——无边框窗口没有系统阴影，需要一条线界定边界
        body = tk.Frame(pad, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(10, 12))

        # --- 顶部仪表盘：磁盘读数
        self.head = tk.Canvas(body, height=self._head_h, bg=BG,
                              highlightthickness=0, bd=0)
        self.head.pack(fill="x")
        self.head.bind("<Configure>", self._on_head_resize)

        # --- 操作条
        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", pady=(13, 10))

        self.btn_scan = NeonButton(bar, "SCAN · 扫描", self.on_scan,
                                   width=140, height=36, color=CYAN)
        self.btn_scan.pack(side="left")

        self.btn_clean = NeonButton(bar, "PURGE · 清理选中项", self.on_clean,
                                    width=182, height=36, color=MAGENTA)
        self.btn_clean.pack(side="left", padx=(8, 0))
        self.btn_clean.set_state("disabled")

        tk.Frame(bar, bg=BORDER, width=1, height=24).pack(side="left", padx=15)

        self.btn_all = NeonButton(bar, "ALL", self.on_check_all,
                                  width=82, height=36, color=PURPLE)
        self.btn_all.pack(side="left")
        self.btn_none = NeonButton(bar, "NONE", self.on_check_none,
                                   width=82, height=36, color=PURPLE)
        self.btn_none.pack(side="left", padx=(8, 0))

        tk.Frame(bar, bg=BORDER, width=1, height=24).pack(side="left", padx=15)

        tk.Label(bar, text="删除方式", bg=BG, fg=TEXT_DIM,
                 font=(FONT_UI, 9)).pack(side="left", padx=(0, 10))
        NeonRadio(bar, "永久删除", self.use_recyclebin, False,
                  color=MAGENTA).pack(side="left")
        NeonRadio(bar, "送回收站", self.use_recyclebin, True,
                  color=CYAN).pack(side="left", padx=(14, 0))

        # --- 主体（先创建不 pack，pack 顺序见本节末尾）
        self.nb = ttk.Notebook(body, style="Cyber.TNotebook")
        self._build_targets_tab()
        self._build_empty_tab()

        # --- 日志
        log_head = tk.Frame(body, bg=BG)
        tk.Frame(log_head, bg=CYAN, width=3, height=14).pack(side="left")
        tk.Label(log_head, text="LOG // 运行日志", bg=BG, fg=CYAN,
                 font=(FONT_EN, 10, "bold")).pack(side="left", padx=(8, 0))
        NeonButton(log_head, "CLR", lambda: self.log_text.delete("1.0", "end"),
                   width=66, height=24, color=TEXT_DIM,
                   font=(FONT_EN, 9, "bold")).pack(side="right")

        logf = tk.Frame(body, bg=BG)
        self.log_text = tk.Text(
            logf, height=6, wrap="none", font=(FONT_EN, 9),
            bg=BG_DEEP, fg=TEXT, insertbackground=CYAN, relief="flat",
            borderwidth=0, padx=9, pady=6,
            selectbackground="#10465c", selectforeground="#ffffff",
            inactiveselectbackground="#10465c",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=BORDER_HI)
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview,
                           style="Cyber.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # --- 状态条
        statusbar = tk.Frame(body, bg=PANEL, highlightthickness=1,
                             highlightbackground=BORDER)
        self._status_accent = tk.Frame(statusbar, bg=CYAN, width=3)
        self._status_accent.pack(side="left", fill="y")
        self.status = tk.Label(statusbar, text="就绪", bg=PANEL, fg=CYAN,
                               font=(FONT_UI, 9), anchor="w", padx=10, pady=6)
        self.status.pack(side="left", fill="x", expand=True)

        # --- 布局顺序：pack 按调用顺序分配空间，先来的先拿到固定高度。
        # 日志与状态条用 side="bottom" 自下而上贴边，Notebook 最后 pack 吃剩余空间。
        # 顺序反了的话，窗口不够高时状态条会被 expand 的 Notebook 整个挤没。
        statusbar.pack(side="bottom", fill="x", pady=(9, 0))
        logf.pack(side="bottom", fill="x")
        log_head.pack(side="bottom", fill="x", pady=(13, 5))
        self.nb.pack(side="top", fill="both", expand=True)

        # 边缘缩放：子控件会吃掉落在自己身上的 <Motion>，所以挂到 bind_all
        self.root.bind_all("<Motion>", self._on_win_motion, add="+")
        self.root.bind_all("<Button-1>", self._on_win_press, add="+")
        self.root.bind_all("<B1-Motion>", self._on_win_drag, add="+")
        self.root.bind_all("<ButtonRelease-1>", self._on_win_release, add="+")

        self.root.after(30, self._draw_head)
        self.root.after(30, self._draw_titlebar)
        self.root.after(60, self._center_window)

    # ---------------- 顶部仪表盘绘制 ----------------

    def _on_head_resize(self, event):
        if abs(event.width - self._head_w) < 2:
            return
        self._head_w = event.width
        self._draw_head()

    def _draw_head(self):
        w = self._head_w or self.head.winfo_width()
        if w < 40:
            return
        h = self._head_h
        self.head.delete("all")

        # 网格底纹：只画竖线，横线会正好穿过文字
        for x in range(0, w + 24, 24):
            self.head.create_line(x, 0, x, h, fill=GRID, tags="grid")
        self.head.tag_lower("grid")

        self.head.create_rectangle(0, 8, 3, h - 19, fill=CYAN, outline="")

        # 磁盘读数（动态更新，见 _refresh_drive_info）
        self._drive_item = self.head.create_text(
            14, 18, anchor="w", text="正在读取磁盘信息 ...", fill=GREEN,
            font=(FONT_EN, 14, "bold"))
        self._drive_sub = self.head.create_text(
            206, 21, anchor="w", text="", fill=TEXT_DIM, font=(FONT_UI, 9))
        self._drive_others = self.head.create_text(
            w - 2, 21, anchor="e", text="", fill=TEXT_DIM, font=(FONT_EN, 9))

        self.head.create_text(14, 42, anchor="w", fill=TEXT_DIM,
                              font=(FONT_UI, 9),
                              text="只清理人工审核过的目标，清单之外的路径一律不碰")

        # 底部霓虹分割线
        self.head.create_line(0, h - 2, w, h - 2, fill=BORDER)
        self.head.create_line(0, h - 2, w * 0.34, h - 2, fill=CYAN, width=2)
        self.head.create_line(w - 78, h - 2, w, h - 2, fill=MAGENTA, width=2)

        self._refresh_drive_info()

    # ---------------- 自绘标题栏 ----------------

    def _on_titlebar_resize(self, event):
        if abs(event.width - getattr(self, "_tb_w", 0)) < 2:
            return
        self._tb_w = event.width
        self._draw_titlebar()

    def _draw_titlebar(self):
        w = getattr(self, "_tb_w", 0) or self.tb.winfo_width()
        if w < 80:
            return
        h = TITLEBAR_H
        self.tb.delete("all")
        self.tb.configure(bg=BG_DEEP)
        self._tb_btns = {}

        # 品牌竖条
        self.tb.create_rectangle(0, 0, 3, h, fill=CYAN, outline="")
        # 闪烁方块
        self._blink = self.tb.create_rectangle(14, 15, 23, 26, fill=CYAN,
                                               outline="")

        # 标题（一层暗色副本做假阴影）
        self.tb.create_text(32, 21, text="CDISK//CLEANER", anchor="w",
                            fill=_mix(CYAN, 0.30), font=(FONT_EN, 15, "bold"))
        self.tb.create_text(31, 20, text="CDISK//CLEANER", anchor="w",
                            fill=CYAN, font=(FONT_EN, 15, "bold"))

        try:
            from tkinter import font as tkfont
            tw = tkfont.Font(font=(FONT_EN, 15, "bold")).measure("CDISK//CLEANER")
        except Exception:
            tw = 140
        self.tb.create_text(31 + tw + 16, 21, text="v" + __version__, anchor="w",
                            fill=MAGENTA, font=(FONT_EN, 10, "bold"))
        self.tb.create_text(31 + tw + 78, 21, text="WHITELIST MODE", anchor="w",
                            fill=TEXT_DIM, font=(FONT_EN, 8))

        # 右侧三个窗口按钮
        bw = 46
        x = w - bw * 3
        self.tb.create_line(x, 8, x, h - 8, fill=BORDER)
        self._tb_button("min", x, bw, h)
        self._tb_button("max", x + bw, bw, h)
        self._tb_button("close", x + bw * 2, bw, h)

    def _tb_button(self, kind, x, bw, h):
        """画一个标题栏按钮，并记下 item id 好在悬停时改色。"""
        hot = MAGENTA if kind == "close" else CYAN
        rect = self.tb.create_rectangle(x + 1, 1, x + bw - 1, h - 1,
                                        fill=BG_DEEP, outline="", tags=kind)
        cx, cy = x + bw / 2.0, h / 2.0
        marks = []
        if kind == "min":
            marks.append(self.tb.create_line(cx - 6, cy + 4, cx + 6, cy + 4,
                                             fill=TEXT_DIM, width=1, tags=kind))
        elif kind == "max":
            marks.append(self.tb.create_rectangle(cx - 5, cy - 5, cx + 5, cy + 5,
                                                  outline=TEXT_DIM, width=1,
                                                  tags=kind))
        else:
            marks.append(self.tb.create_line(cx - 5, cy - 5, cx + 5, cy + 5,
                                             fill=TEXT_DIM, width=1, tags=kind))
            marks.append(self.tb.create_line(cx + 5, cy - 5, cx - 5, cy + 5,
                                             fill=TEXT_DIM, width=1, tags=kind))

        self._tb_btns[kind] = (rect, marks, hot)

        def enter(_e, k=kind):
            r, ms, c = self._tb_btns[k]
            self.tb.itemconfigure(r, fill=_mix(c, 0.22))
            for m in ms:
                self.tb.itemconfigure(m, fill="#ffffff")

        def leave(_e, k=kind):
            r, ms, c = self._tb_btns[k]
            self.tb.itemconfigure(r, fill=BG_DEEP)
            for m in ms:
                self.tb.itemconfigure(m, fill=TEXT_DIM)

        self.tb.tag_bind(kind, "<Enter>", enter)
        self.tb.tag_bind(kind, "<Leave>", leave)

        action = {"min": self._minimize, "max": self._toggle_max,
                  "close": self._on_close}[kind]
        # 返回 "break"：别让 tag 上的点击再冒泡到整条标题栏的拖动处理
        self.tb.tag_bind(kind, "<Button-1>", lambda _e, f=action: (f(), "break")[1])

    def _tick_blink(self):
        """标题栏的小方块来回变色，让界面看起来「在工作」。"""
        if not self.alive:
            return
        self._blink_on = not self._blink_on
        try:
            self.tb.itemconfigure(self._blink,
                                  fill=CYAN if self._blink_on else "#0d3b47")
        except Exception:
            pass
        self.root.after(620, self._tick_blink)

    # ---------------- 无边框窗口 ----------------
    #
    # overrideredirect(True) 去掉整个系统边框，顺带丢掉三样东西：
    # 任务栏图标、最小化、边缘缩放。三者都得自己补回来。

    def _go_frameless(self):
        try:
            self.root.overrideredirect(True)
            self._frameless = True
        except Exception:
            self._frameless = False
        self._apply_appwindow()

    def _apply_appwindow(self):
        """
        补任务栏图标。

        overrideredirect 会把窗口变成 WS_POPUP 并带上 WS_EX_TOOLWINDOW，
        结果从任务栏和 Alt+Tab 里一起消失。加回 WS_EX_APPWINDOW 让它回来。
        """
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                return
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            get = ctypes.windll.user32.GetWindowLongW
            setw = ctypes.windll.user32.SetWindowLongW
            get.restype = ctypes.c_long
            setw.restype = ctypes.c_long
            style = get(hwnd, GWL_EXSTYLE)
            setw(hwnd, GWL_EXSTYLE, (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
        except Exception:
            pass

    def _minimize(self):
        """
        overrideredirect 的窗口不能直接 iconify——会报
        "can't iconify ...: override-redirect flag is set"。
        先把边框恢复回去再最小化，之后轮询等它回来，再把边框去掉。
        """
        try:
            self.root.overrideredirect(False)
            self.root.iconify()
        except Exception:
            self._restore_frameless()
            return
        self.root.after(200, self._watch_restore)

    def _watch_restore(self):
        """
        等窗口从最小化恢复。

        不监听 <Map>：deiconify 时它不一定触发（实测就没触发），
        而 <Map> 又会被 overrideredirect(False) 自己触发一次，混在一起没法区分。
        直接查状态反而最稳。
        """
        if not self.alive:
            return
        try:
            iconic = (self.root.state() == "iconic")
        except Exception:
            iconic = False
        if iconic:
            self.root.after(200, self._watch_restore)
            return
        self._restore_frameless()

    def _restore_frameless(self):
        try:
            self.root.overrideredirect(True)
        except Exception:
            return
        self._apply_appwindow()

    @staticmethod
    def _work_area():
        """屏幕工作区（去掉任务栏）。最大化时用。"""
        r = wintypes.RECT()
        try:
            ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
        except Exception:
            return 0, 0, 1920, 1080

    def _center_window(self):
        """把窗口摆到工作区中央偏上的位置。"""
        try:
            left, top, right, bottom = self._work_area()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            x = max(left, left + (right - left - w) // 2)
            y = max(top, top + (bottom - top - h) // 3)
            self.root.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _toggle_max(self):
        if self._maxed:
            if self._restore_geo:
                self.root.geometry(self._restore_geo)
            self._maxed = False
        else:
            self._restore_geo = self.root.geometry()
            left, top, right, bottom = self._work_area()
            self.root.geometry("%dx%d+%d+%d"
                               % (right - left, bottom - top, left, top))
            self._maxed = True
        self.root.after(20, self._draw_titlebar)

    # ---- 拖动移动（抓标题栏空白处）

    def _drag_begin(self, event):
        if self._maxed:
            return
        self._drag_off = (event.x_root - self.root.winfo_x(),
                          event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        if self._drag_off is None or self._maxed:
            return
        dx, dy = self._drag_off
        self.root.geometry("+%d+%d" % (event.x_root - dx, event.y_root - dy))

    def _drag_end(self, _event):
        self._drag_off = None

    # ---- 边缘缩放

    CURSORS = {"n": "size_ns", "s": "size_ns", "w": "size_we", "e": "size_we",
               "nw": "size_nw_se", "se": "size_nw_se",
               "ne": "size_ne_sw", "sw": "size_ne_sw"}

    def _edge_at(self, x_root, y_root):
        if self._maxed or not self._frameless:
            return None
        x = x_root - self.root.winfo_rootx()
        y = y_root - self.root.winfo_rooty()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        e = RESIZE_EDGE
        if x < -2 or y < -2 or x > w + 2 or y > h + 2:
            return None
        left, right = x < e, x >= w - e
        top, bottom = y < e, y >= h - e
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if top:
            return "n"
        if bottom:
            return "s"
        if left:
            return "w"
        if right:
            return "e"
        return None

    def _on_win_motion(self, event):
        if self._resizing or self._drag_off:
            return
        edge = self._edge_at(event.x_root, event.y_root)
        want = self.CURSORS.get(edge, "")
        if want == self._cur_cursor:
            return          # 鼠标每动一像素就调一次 configure 是白费
        self._cur_cursor = want
        try:
            self.root.configure(cursor=want)
        except Exception:
            pass

    def _on_win_press(self, event):
        edge = self._edge_at(event.x_root, event.y_root)
        if not edge:
            return
        self._resizing = (edge, event.x_root, event.y_root,
                          self.root.winfo_x(), self.root.winfo_y(),
                          self.root.winfo_width(), self.root.winfo_height())

    def _on_win_drag(self, event):
        if not self._resizing:
            return
        edge, sx, sy, ox, oy, ow, oh = self._resizing
        dx, dy = event.x_root - sx, event.y_root - sy
        x, y, w, h = ox, oy, ow, oh
        if "e" in edge:
            w = max(MIN_WIN[0], ow + dx)
        if "s" in edge:
            h = max(MIN_WIN[1], oh + dy)
        if "w" in edge:
            w = max(MIN_WIN[0], ow - dx)
            x = ox + (ow - w)
        if "n" in edge:
            h = max(MIN_WIN[1], oh - dy)
            y = oy + (oh - h)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _on_win_release(self, _event):
        if self._resizing:
            self._resizing = None

    def _build_targets_tab(self):
        from tkinter import ttk

        frame = tk.Frame(self.nb, bg=PANEL)
        self.nb.add(frame, text="垃圾与临时文件")

        cols = ("chk", "name", "size", "count", "note")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="browse", style="Cyber.Treeview")
        for cid, text, width, anchor, stretch in (
                ("chk", "", 34, "center", False),
                ("name", "项目", 178, "w", False),
                ("size", "大小", 96, "e", False),
                ("count", "文件数", 94, "e", False),
                ("note", "说明", 480, "w", True)):
            self.tree.heading(cid, text=text, anchor=anchor)
            self.tree.column(cid, width=width, anchor=anchor, stretch=stretch)

        self.tree.tag_configure("risky", foreground=AMBER)
        self.tree.tag_configure("missing", foreground=TEXT_OFF)
        self.tree.tag_configure("admin", foreground="#22b8d8")

        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview,
                           style="Cyber.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True,
                       padx=(1, 0), pady=1)
        sb.pack(side="right", fill="y", padx=(0, 1), pady=1)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>",
                       lambda _e: self._toggle_target(self.tree.selection()))

        for t in self.targets:
            self.tree.insert("", "end", iid=t.key, tags=(self._tag_of(t),),
                             values=self._row_values(t))

    def _build_empty_tab(self):
        from tkinter import ttk

        frame = tk.Frame(self.nb, bg=PANEL)
        self.nb.add(frame, text="空文件夹")

        tip = tk.Frame(frame, bg=PANEL)
        tip.pack(fill="x", padx=10, pady=(11, 8))
        tk.Frame(tip, bg=AMBER, width=3, height=36).pack(side="left")
        tk.Label(
            tip, bg=PANEL, fg=TEXT_DIM, font=(FONT_UI, 9), justify="left",
            wraplength=850,
            text="空文件夹默认不勾选。系统目录（Windows、Program Files、"
                 "AppData、回收站等）已强制排除，根本不参与扫描。\n"
                 "提示：这一项收益有限——一个空目录在 NTFS 上只占几 KB。"
        ).pack(side="left", padx=(9, 0))

        # 按钮要排在表格之前 pack：表格 expand 会吃掉全部剩余高度，
        # 排在后面的控件在空间不够时会被整个裁掉。
        self.btn_scan_empty = NeonButton(frame, "SCAN · 空目录",
                                         self.on_scan_empty, width=152,
                                         height=34, color=GREEN)
        self.btn_scan_empty.pack(anchor="w", padx=10, pady=(0, 9))

        holder = tk.Frame(frame, bg=PANEL)
        holder.pack(fill="both", expand=True, padx=1, pady=(0, 9))
        cols = ("chk", "path")
        self.etree = ttk.Treeview(holder, columns=cols, show="headings",
                                  selectmode="extended", style="Cyber.Treeview")
        self.etree.heading("chk", text="", anchor="center")
        self.etree.heading("path", text="路径", anchor="w")
        self.etree.column("chk", width=34, anchor="center", stretch=False)
        self.etree.column("path", width=800, anchor="w", stretch=True)

        sb = ttk.Scrollbar(holder, orient="vertical", command=self.etree.yview,
                           style="Cyber.Vertical.TScrollbar")
        self.etree.configure(yscrollcommand=sb.set)
        self.etree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.etree.bind("<Button-1>", self._on_etree_click)
        self.etree.bind("<space>", self._on_etree_space)

    # ---------------- 行数据 ----------------

    @staticmethod
    def _tag_of(t):
        if t.exists is False:
            return "missing"
        if t.level == LEVEL_RISKY:
            return "risky"
        if t.need_admin:
            return "admin"
        return ""

    @staticmethod
    def _row_values(t):
        if t.exists is False:
            return (UNCHECKED, t.label, PENDING, PENDING, t.note)
        mark = CHECKED if t.checked else UNCHECKED
        if t.exists is None:
            return (mark, t.label, PENDING, PENDING, t.note)
        note = ("【需管理员】" + t.note) if t.need_admin else t.note
        return (mark, t.label, human_size(t.size),
                "{:,}".format(t.files), note)

    # ---------------- 日志与状态 ----------------

    def log(self, msg):
        self.log_text.insert("end", "%s > %s\n" % (time.strftime("%H:%M:%S"), msg))
        self.log_text.see("end")

    def set_status(self, text):
        self.status.configure(text=text)

    def _refresh_drive_info(self):
        # 仪表盘可能还没绘制（首帧），等 _draw_head 完成时会再调一次
        if self._drive_item is None:
            return

        u0 = disk_usage("C")
        if u0:
            used_pct = u0[1] * 100.0 / max(u0[0], 1)
            color = GREEN if used_pct < 85 else (AMBER if used_pct < 93 else MAGENTA)
            self.head.itemconfigure(self._drive_item, fill=color,
                                    text="C: 可用 %s" % human_size(u0[2]))
            self.head.itemconfigure(
                self._drive_sub,
                text="/ 共 %s   ·   已用 %.0f%%" % (human_size(u0[0]), used_pct))
        else:
            self.head.itemconfigure(self._drive_item, fill=TEXT_DIM,
                                    text="未读取到磁盘信息")
            self.head.itemconfigure(self._drive_sub, text="")

        others = []
        for letter in "DEF":
            u = disk_usage(letter)
            if u:
                others.append("%s: 可用 %s" % (letter, human_size(u[2])))
        self.head.itemconfigure(self._drive_others, text="   ".join(others))

    # ---------------- 勾选 ----------------

    def _find_target(self, iid):
        for t in self.targets:
            if t.key == iid:
                return t
        return None

    def _toggle_target(self, iids):
        if isinstance(iids, str):
            iids = (iids,)
        for iid in iids:
            t = self._find_target(iid)
            if t is None or t.exists is False:
                continue
            if t.level == LEVEL_RISKY and not t.checked:
                from tkinter import messagebox
                if not messagebox.askyesno(
                        APP_NAME,
                        "「%s」有副作用：\n\n%s\n\n确定要勾选吗？" % (t.label, t.note),
                        parent=self.root):
                    continue
            t.checked = not t.checked
            self._refresh_row(t)
        self._update_summary()

    def _refresh_row(self, t):
        try:
            self.tree.item(t.key, values=self._row_values(t),
                           tags=(self._tag_of(t),))
        except Exception:
            pass

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) not in ("#1", "#2"):
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_target(iid)

    def _toggle_empty(self, iid):
        if iid in self.empty_checked:
            self.empty_checked.discard(iid)
            mark = UNCHECKED
        else:
            self.empty_checked.add(iid)
            mark = CHECKED
        try:
            vals = list(self.etree.item(iid, "values"))
            vals[0] = mark
            self.etree.item(iid, values=vals)
        except Exception:
            pass
        self._update_summary()

    def _on_etree_click(self, event):
        if self.etree.identify_region(event.x, event.y) != "cell":
            return
        if self.etree.identify_column(event.x) not in ("#1", "#2"):
            return
        iid = self.etree.identify_row(event.y)
        if iid:
            self._toggle_empty(iid)

    def _on_etree_space(self, _event):
        for iid in self.etree.selection():
            self._toggle_empty(iid)

    @staticmethod
    def _is_top_level_dir(path):
        """
        判断是不是盘符根的直接子目录（如 D:\\WeGameApps）。

        这类空目录多半是程序自己建的占位目录，运行时才往里写东西，
        删掉可能让程序报错。一键全选时跳过，真想删得单独勾。
        """
        _drive, tail = os.path.splitdrive(norm(path))
        return len([p for p in tail.split(os.sep) if p]) <= 1

    def on_check_all(self):
        for t in self.targets:
            if t.exists is not False and t.level != LEVEL_RISKY:
                t.checked = True
                self._refresh_row(t)

        skipped = 0
        for iid in self.etree.get_children():
            try:
                path = self.etree.item(iid, "values")[1]
            except Exception:
                path = ""
            if self._is_top_level_dir(path):
                skipped += 1
                continue
            if iid not in self.empty_checked:
                self.empty_checked.add(iid)
                try:
                    vals = list(self.etree.item(iid, "values"))
                    vals[0] = CHECKED
                    self.etree.item(iid, values=vals)
                except Exception:
                    pass
        if skipped:
            self.log("已跳过 %d 个盘符根下的顶层空目录（多为程序占位目录），"
                     "要清理请单独勾选" % skipped)
        self._update_summary()

    def on_check_none(self):
        for t in self.targets:
            t.checked = False
            self._refresh_row(t)
        for iid in list(self.empty_checked):
            self.empty_checked.discard(iid)
            try:
                vals = list(self.etree.item(iid, "values"))
                vals[0] = UNCHECKED
                self.etree.item(iid, values=vals)
            except Exception:
                pass
        self._update_summary()

    def _selected_targets(self):
        return [t for t in self.targets if t.checked and t.exists is not False]

    def _update_summary(self):
        sel = self._selected_targets()
        parts = ["已选 %d 项，预计释放 %s"
                 % (len(sel), human_size(sum(t.size for t in sel)))]
        if self.empty_checked:
            parts.append("外加 %d 个空文件夹" % len(self.empty_checked))
        self.set_status("    ".join(parts))
        self.btn_clean.set_state(
            "disabled" if self.busy or not (sel or self.empty_checked)
            else "normal")

    # ---------------- 扫描 ----------------

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.btn_scan, self.btn_all, self.btn_none,
                  self.btn_scan_empty):
            w.set_state(state)
        self.btn_clean.set_state(
            "disabled" if busy or not (self._selected_targets()
                                       or self.empty_checked) else "normal")
        self._touch_status_accent(busy)

    def _touch_status_accent(self, busy):
        """忙的时候把状态条左侧的色条点亮，给个一眼可见的运行提示。"""
        try:
            self._status_accent.configure(bg=MAGENTA if busy else CYAN)
        except Exception:
            pass

    def on_scan(self):
        if self.busy:
            return
        self._set_busy(True)
        self.set_status("正在扫描 ...")
        self.log("开始扫描（只读，不会删除任何东西）")
        self.cancel.clear()

        def work():
            try:

                def prog(t):
                    self.msgq.put(("row", t))
                    self.msgq.put(("status", "已扫描：%s — %s"
                                   % (t.label, human_size(t.size))))

                # 回收站统计偏慢，单独开线程，别拖住其他不到一秒的项目
                rb_targets = [t for t in self.targets if t.mode == "recyclebin"]
                others = [t for t in self.targets if t.mode != "recyclebin"]

                if rb_targets:
                    self.msgq.put(("status", "正在统计回收站 ..."))

                    def rb_work():
                        for t in rb_targets:
                            try:
                                b, n = measure_recyclebin()
                                t.exists = True
                                t.size, t.files = b, n
                                t.errcount = 0
                                self.msgq.put(("row", t))
                            except Exception:  # noqa: BLE001
                                pass
                        self.msgq.put(("rb_done", None))

                    threading.Thread(target=rb_work, daemon=True).start()

                scan_targets(others, progress=prog, cancel=self.cancel)
                self.msgq.put(("scan_done", bool(rb_targets)))
            except Exception as exc:  # noqa: BLE001
                self.msgq.put(("error", "扫描失败：%s" % exc))

        threading.Thread(target=work, daemon=True).start()

    def on_scan_empty(self):
        if self.busy:
            return
        self._set_busy(True)
        self.set_status("正在扫描空文件夹 ...")
        self.log("开始扫描空文件夹（系统目录已排除，只读）")
        self.cancel.clear()

        def work():
            try:
                found = scan_empty_dirs(
                    progress=lambda n: self.msgq.put(
                        ("status", "已找到 %d 个空文件夹 ..." % n)),
                    cancel=self.cancel)
                self.msgq.put(("empty_done", found))
            except Exception as exc:  # noqa: BLE001
                self.msgq.put(("error", "扫描空文件夹失败：%s" % exc))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 清理 ----------------

    def on_clean(self):
        if self.busy:
            return
        sel = self._selected_targets()
        empty_sel = list(self.empty_checked)
        if not sel and not empty_sel:
            from tkinter import messagebox
            messagebox.showinfo(APP_NAME, "还没有勾选任何项目。", parent=self.root)
            return

        mode = "送进回收站" if self.use_recyclebin.get() else "永久删除"
        lines = ["即将%s以下内容：" % mode, ""]
        for t in sel:
            lines.append("  · %-16s %10s" % (t.label, human_size(t.size)))
        if empty_sel:
            lines.append("  · %-16s %10d 个" % ("空文件夹", len(empty_sel)))
        lines.append("")
        lines.append("合计约 %s" % human_size(sum(t.size for t in sel)))
        lines.append("")
        if self.use_recyclebin.get():
            lines.append("注意：送回收站不会立刻释放磁盘空间，")
            lines.append("要之后再清空回收站才算真正腾出空间。")
        else:
            lines.append("永久删除不可撤销，文件不会进回收站。")

        from tkinter import messagebox
        if not messagebox.askyesno(APP_NAME, "\n".join(lines), parent=self.root):
            self.log("已取消")
            return

        self._set_busy(True)
        self.cancel.clear()

        def work():
            cleared = []
            freed = files = failed = 0
            for t in sel:
                if self.cancel.is_set():
                    break
                self.msgq.put(("log", "清理：%s" % t.label))
                b, f, e = clear_target(t, use_recyclebin=self.use_recyclebin.get(),
                                       log=lambda m: self.msgq.put(("log", m)))
                freed += b
                files += f
                failed += e
                cleared.append(t)
                self.msgq.put(("log", "  → 释放 %s，处理 %d 个文件%s"
                               % (human_size(b), f,
                                  ("，%d 项未成功" % e) if e else "")))

            if empty_sel and not self.cancel.is_set():
                self.msgq.put(("log", "清理空文件夹 %d 个" % len(empty_sel)))
                for d in empty_sel:
                    if self.cancel.is_set():
                        break
                    # 空目录是全盘扫出来的，不属于任何白名单目标（allowed_root=None），
                    # 但硬禁区、保护区、盘符根、程序自身运行目录这些照样要过一遍
                    try:
                        guard(d)
                    except GuardError as exc:
                        failed += 1
                        self.msgq.put(("log", "  [拒绝] %s" % exc))
                        continue
                    try:
                        if os.path.isdir(d) and is_empty_dir(d):
                            os.rmdir(d)
                            self.msgq.put(("eempty", d))
                    except OSError as exc:
                        failed += 1
                        self.msgq.put(("log", "  [失败] %s：%s" % (d, exc)))

            self.msgq.put(("clean_done", (freed, files, failed, cleared)))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 消息泵 ----------------

    def _drain_queue(self):
        if not self.alive:
            return
        try:
            while True:
                kind, payload = self.msgq.get_nowait()

                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self.set_status(payload)
                elif kind == "row":
                    self._refresh_row(payload)
                elif kind == "scan_done":
                    self._refresh_drive_info()
                    total = sum(t.size for t in self._selected_targets())
                    self.log("扫描完成。勾选项合计 %s" % human_size(total))
                    if payload:
                        self.log("回收站项数较多，正在后台统计 ...")
                    else:
                        self._set_busy(False)
                    self._update_summary()
                elif kind == "rb_done":
                    self._set_busy(False)
                    rb = [t for t in self.targets if t.mode == "recyclebin"]
                    if rb:
                        self.log("回收站统计完成：%s / %d 项"
                                 % (human_size(rb[0].size), rb[0].files))
                    self._refresh_drive_info()
                    self._update_summary()
                elif kind == "empty_done":
                    self._set_busy(False)
                    self.empty_checked.clear()
                    self.etree.delete(*self.etree.get_children())
                    for i, d in enumerate(payload):
                        self.etree.insert("", "end", iid="e%d" % i,
                                          values=(UNCHECKED, d))
                    self.log("空文件夹扫描完成：找到 %d 个（默认不勾选）" % len(payload))
                    self._update_summary()
                elif kind == "eempty":
                    try:
                        self.etree.delete(payload)
                    except Exception:
                        pass
                elif kind == "clean_done":
                    freed, files, failed, cleared = payload
                    self._set_busy(False)
                    self._refresh_drive_info()
                    for t in cleared:
                        t.checked = False
                        if t.mode != "recyclebin":
                            t.size, t.files = 0, 0
                        self._refresh_row(t)
                    self.log("清理完成：释放 %s，处理 %d 个文件%s"
                             % (human_size(freed), files,
                                ("，%d 项未成功（多被占用）" % failed) if failed else ""))
                    if self.use_recyclebin.get() and freed:
                        self.log("提示：这些内容现在还在回收站里占着空间，"
                                 "需要清空回收站才真正释放。")
                    self.log("点「扫描」可刷新最新数据。")
                    self.set_status("清理完成，释放 %s" % human_size(freed))
                elif kind == "error":
                    self._set_busy(False)
                    self.log("[错误] %s" % payload)

        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    # ---------------- 结束 ----------------

    def _on_close(self):
        if self.busy:
            from tkinter import messagebox
            if not messagebox.askyesno(APP_NAME, "还有任务在运行，确定退出吗？",
                                       parent=self.root):
                return
            self.cancel.set()
        self.alive = False
        self.root.destroy()


# ---------------------------------------------------------------- 入口

def _fatal(msg, title=APP_NAME):
    """没 tkinter 时的兜底提示，走原生消息框。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, title, 0x10)
    except Exception:
        sys.stderr.write(msg + "\n")


def main():
    global tk

    if not IS_WINDOWS:
        _fatal("本工具仅支持 Windows。")
        return 1

    try:
        import tkinter as _tk
        from tkinter import ttk  # noqa: F401
    except ImportError:
        _fatal("当前 Python 缺少 tkinter 组件，界面无法启动。\n\n"
               "两种办法：\n"
               "  1. 换用带 tcl/tk 的 Python 运行\n"
               "  2. 直接下载打包好的 exe\n")
        return 1

    tk = _tk
    _enable_dpi_awareness()
    root = tk.Tk()
    CleanerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
