# C 盘垃圾清理器

Windows 上一键清理 C 盘垃圾、临时文件和空文件夹的小工具。

**它不扫描整个磁盘找"垃圾"**——那种做法迟早会出事。它内置一份经过人工审核的清理清单，
只清清单里的东西，清单之外的一律不碰。

![界面](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![语言](https://img.shields.io/badge/python-3.8%2B-green)
![依赖](https://img.shields.io/badge/dependencies-none-brightgreen)

## 特点

- **白名单制**：只有内置清单里的目标会被处理。删之前还会过两道闸门——先确认路径确实
  属于某个已审核的目标，再确认它不在绝对保护区内。两道都过才动手。
- **先扫后清**：点「扫描」只读地算出每项占多少、有多少文件，你勾选后点「清理选中项」
  才真正删除。删之前还会再弹一次清单让你确认。
- **默认永久删除**：真正释放磁盘空间。也提供「送回收站」模式（但注意：送回收站不会
  立刻释放空间，要再清空回收站才算数）。
- **不做多余的事**：不常驻后台、不写注册表、不开机自启、不连网。
- **零第三方依赖**：只用 Python 标准库。

## 绝对不碰的东西

工具内部有一份硬编码的保护名单，落在其中的路径**即使被写进清理清单也会被拒绝**：

| 类别 | 具体 |
|---|---|
| 系统核心 | `C:\Windows`（除 `Temp` 与软件分发缓存）|
| 程序目录 | `Program Files`、`Program Files (x86)`、`ProgramData` |
| 用户资料 | 桌面、文档、图片、视频、音乐、下载、OneDrive |
| 程序配置 | `AppData\Roaming`、`AppData\LocalLow`、`AppData\Local\Packages` |
| 系统机制 | `$Recycle.Bin`、`System Volume Information`、`Recovery`、`Boot`、`EFI` |
| 关键子目录 | `System32`、`SysWOW64`、`WinSxS`、`assembly`、`Fonts`、`Installer` 等 |

另外还有两条兜底：不允许删除任何盘符根目录，不允许删除当前程序自身的运行目录
（打包成单文件时程序解压在 `%TEMP%` 里，清理临时文件时不能把自己删了）。

## 能清什么

| 分类 | 项目 |
|---|---|
| 临时文件 | 用户临时文件、系统临时文件、错误报告、崩溃转储 |
| 系统缓存 | Windows 更新缓存、传递优化缓存、缩略图缓存 |
| 显卡缓存 | D3D / NVIDIA / AMD 着色器缓存 |
| 浏览器 | Chrome、Edge、Firefox 网页缓存（**不碰**书签、密码、Cookie、登录状态）|
| 开发工具 | pip、npm、pnpm、Yarn、uv、NuGet、Go 模块、Cargo、Gradle、通用 `.cache` |
| 应用残留 | 各类客户端下载完却没装的更新包 |
| 空文件夹 | 单独一栏列出，**默认不勾选** |
| 回收站 | 清空回收站（不可撤销，默认不勾选）|

## 用法

### 直接下载 exe（推荐）

到 [Releases](../../releases) 下载 `cdisk-cleaner.exe`，双击运行。不需要装 Python。

### 从源码运行

```bash
python cdisk_cleaner.py
```

或者双击 `run.bat`，它会自动在 PATH 里挑一个带 tkinter 的 Python。

> 注意：如果你的机器上 `python` 指向的是没装 tcl/tk 的解释器，
> 直接跑会弹出「缺少 tkinter」的提示框。用 `run.bat` 可以避开这个坑。

### 想要清得更彻底

「系统临时文件」「Windows 更新缓存」「旧版 Windows 文件」这几项需要管理员权限。
**右键 exe → 以管理员身份运行**，这几项才能生效。普通权限下它们会被自动跳过，
其余项目照常工作。

## 空文件夹这一项，说实话

清空文件夹**几乎省不了空间**——一个空目录在 NTFS 上只占一个目录项，几 KB 而已。
扫出一万个也就几十 MB。

而且 Windows 上的"空文件夹"很多是必需的：`C:\Windows\assembly` 下的、回收站内部的、
`System32` 里的，删了直接出系统故障。

所以本工具的做法是：

- 默认**不勾选**
- 扫描时**跳过所有系统目录与保护区**
- 只是把找得到的空目录列出来，让你自己判断

如果你看到这一栏里没几个项目，那是正常的，也是对的。

## 打包成 exe

仓库里带了打包脚本，版本号、图标、版本资源都只有一个来源：

```bash
pip install pyinstaller pillow
python build_exe.py
```

可选参数：

| 参数 | 作用 |
|---|---|
| `--onedir` | 打成目录版。启动约 1 秒、退出秒关；单文件版每次要先解压再清理临时目录，慢一些 |
| `--upx` | 启用 UPX 压缩。**默认关闭**——只省一点体积，但加壳会拉高国内杀软的误报率 |
| `--upx-dir` | 指定 UPX 目录（必须在命令行传，写进 spec 里无效）|
| `--keep-console` | 保留控制台窗口，排查启动问题时用 |

## 实现要点

- **界面**：tkinter，扫描与清理都在后台线程跑，通过队列回传进度，界面不会卡住。
- **回收站大小**：走 `SHQueryRecycleBin` API 查询，不遍历文件系统——
  本机回收站有十几万项，遍历一次要好几十秒，用 API 是毫秒级。
- **送回收站**：`SHFileOperationW` + `FOF_ALLOWUNDO`，一次调用处理整批。
- **删除只读文件**：先用 `os.chmod` 去掉只读位再删。
- **被占用的文件**：跳过并计数，不会中断整个流程。
- **路径比较**：`os.path.normcase` + `normpath` 规范化后再比对，
  避免 `C:\ab` 被误判为 `C:\a` 的子路径。

## 环境要求

- Windows 7 及以上（在 Windows 10 22H2 上实测）
- Python 3.8+（仅从源码运行时需要，且要带 tkinter）

## License

MIT
