# Sandevistan ADB

一个供 macOS 和 Windows 使用的 Qt 图形 ADB 工具。提供设备选择、设备信息、Wi-Fi 连接、应用管理、Monkey 测试、截图、文件传输、实时日志、崩溃文件抓取、性能监控和 Dart 网络请求查看。

## 准备

- 运行打包后的 App 无需另装 `adb`；从源码运行时需安装 Android 官方 [SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)，确保 `adb` 在 `PATH` 中，或用 `ADB_PATH` 指向它。
- 在手机上开启开发者选项和 USB 调试，连接电脑后允许调试授权。先在终端运行 `adb devices` 确认连接。

## 启动

macOS 打开 `dist/Sandevistan ADB.app`；Windows 解压开发 ZIP 后运行其中的 `Sandevistan ADB.exe`。应用包内含 Python、PySide6、Qt 和对应平台的 `adb`；接收方无需安装 Android SDK Platform-Tools。

如需从源码构建 macOS 包，在项目目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-qt.txt pyinstaller
./scripts/build_qt_app.sh
```

构建机需安装 Android SDK Platform-Tools。构建脚本会从 `PATH` 或 `ADB_PATH`
找到 `adb`，也可设置 `SANDEVISTAN_ADB_BINARY` 指向要打包的 `adb`；该文件
所在目录须含原版 `NOTICE.txt` 和 `source.properties`。打包后的 App 优先使用
内置 `adb`，不依赖接收方的 `PATH`。

构建结果位于 `dist/Sandevistan ADB.app`。Qt 界面入口是 `main_qt.py`，打包配置是 `Sandevistan ADB.spec`。
构建脚本还会生成并验证 `dist/Sandevistan-ADB-macos-arm64-dev.zip`；
这个 ZIP 去除了 macOS 桌面文件夹可能附加的 Finder 元数据。

### Windows x64 开发包

在 GitHub 的 **Actions → Build Windows exe → Run workflow** 手动启动构建，完成后下载名为 `Sandevistan-ADB-windows-x64-dev` 的 artifact。解压 artifact 得到 `Sandevistan-ADB-windows-x64-dev.zip`，再解压该 ZIP，运行 `Sandevistan ADB/Sandevistan ADB.exe`。构建只使用已推送到 GitHub 的代码。

也可在 Windows x64 上安装 **Python 3.13.0 x64**，从仓库根目录运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-qt.txt pyinstaller==6.22.3
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts\build_windows.py
```

构建脚本自动下载 Windows 版 Android SDK Platform-Tools 和对应的 Qt / PySide6 LGPL 源码并校验，生成 `dist/Sandevistan-ADB-windows-x64-dev.zip`。如已安装 Windows 版 Platform-Tools，可设置 `SANDEVISTAN_ADB_BINARY` 指向其中的 `adb.exe`；同目录需要保留 `AdbWinApi.dll`、`AdbWinUsbApi.dll`、`NOTICE.txt` 和 `source.properties`。文件夹内的 EXE 需要与 `_internal` 一起保留，不能只复制 EXE。无需安装 Android Studio；仅“不保留数据”安装模式需要另外安装 Android SDK Build-Tools 的 `aapt.exe`。

## 对外分发

macOS 包的 `Contents/Resources/licenses/` 和 Windows 包的 `_internal/licenses/` 提供第三方许可正文、对应的
Qt / PySide6 源码归档及替换动态库说明，侧边栏的
“Qt LGPLv3 · 第三方许可”可查看组件清单。构建脚本会检查 Qt Virtual Keyboard
等未使用模块没有进入应用包，并且只随包提供界面使用的 Font Awesome 6
solid 字体。`adb` 与对应的原版许可声明随包提供。

这些本地构建仅用于开发测试；macOS 包使用 ad hoc 签名，Windows 包未签名。对外发行前，发行者还需要：

1. 确认当前版本的对应源码归档继续随包提供；如修改了 Qt、PySide6 或
   Shiboken6，也要同时提供修改内容。
2. 验证接收者能替换和运行兼容的 LGPL 动态库。若对 macOS 分发版使用
   Developer ID 签名与公证，需提供替换库后重新签名运行的说明。
3. 复核最终包及其依赖的第三方许可；macOS 正式版使用 Developer ID 签名并公证，Windows 正式版按发行需求签名。

在完成这些发行步骤之前，请勿将本地开发包当作正式分发包。

设备列表优先显示 Android 系统中的设备名称，并标注 USB 或 Wi-Fi 连接方式及序列号。选择设备后即可通过左侧导航进入应用管理、屏幕截图和文件传输。应用管理页采用紧凑设备栏和左右分栏：顶部可切换第三方或系统应用、搜索及刷新；左侧自动加载当前设备的应用包名列表，右侧显示所选应用的操作。点击“安装 APK”打开安装窗口，可选择当前设备、所有已连接设备或自定义设备，工具会逐台安装并在活动记录中显示每台结果；默认覆盖安装并保留已有应用数据。勾选“允许降级安装”会传入 `adb install -d`，设备仍可能拒绝覆盖降级。取消“保留数据”会先识别 APK 包名，确认后在目标设备上卸载旧版并安装新 APK；这会清除应用数据，且安装失败时不会自动恢复旧版。此模式需要本机 Android SDK Build-Tools 的 `aapt`；找不到时会阻止卸载并提示。列表加载完成后会显示耗时。

活动记录固定在应用管理、屏幕截图和文件管理页的窗口底部：应用管理页折叠时显示当前状态，其他页面折叠时显示最新一条记录；展开后可查看最近 2000 行操作记录。实时日志、性能监控和 Dart 网络页会隐藏这块区域。

侧边栏的“设备信息”页显示当前设备的 CPU SoC、核心数与架构、GPU 渲染器、内部存储与系统分区容量，以及 RAM 和 Swap。点击“刷新信息”可重新读取；厂商未公开或普通 ADB 无权读取的字段会显示“设备未提供”。内部存储显示的是 `/data` 分区容量，可能小于宣传的 ROM 容量。

应用管理页右侧的详情面板使用顶部当前设备。选择包名，或点击“手动指定…”输入包名和可选的 Activity 后，可以启动默认入口或指定 Activity、强制停止应用、清除缓存或应用数据，并查看权限及进程内存信息。启动后会核对设备的前台 Activity，并在按钮下方显示结果。前台 Activity 也可独立读取，结果可复制。权限页同时显示常见 Android 权限的中文名称和原始权限标识，未收录或应用自定义的权限保留原始标识；只有运行时权限提供授予和撤销按钮。授予或撤销后只核对当前权限；手动刷新会批量核对列表中的权限状态，设备不支持快速查询时才重新读取整份权限信息。权限弹窗独立执行操作，进行中会暂时禁用重复操作。清除数据会删除应用的登录状态和本地设置；“仅清缓存”需要设备支持 `pm clear --cache-only`，不支持时会报错，不会改为清除全部数据。

右侧“Monkey 测试”按钮会打开测试窗口，使用右侧应用包名和顶部当前设备。解锁设备后设置事件数（默认 500）和事件间隔（默认 100 毫秒），点击“开始测试”运行 Android Monkey；下方显示实时输出和完成状态，也可提前停止。测试使用 `-p` 限定目标应用包名；随机操作可能改变应用状态。Monkey 报告崩溃、中止或注入事件不足时会显示失败。切换设备或退出应用会停止当前测试。

点击顶部“Wi-Fi 连接”可连接无线调试设备：

- Android 11 及以上：手机和电脑接入同一 Wi-Fi，在开发者选项中开启“无线调试”，进入“使用配对码配对设备”。先填写弹窗中的配对地址及 6 位配对码并点击“配对”，再填写无线调试主页显示的**连接地址**并点击“连接”。配对端口与连接端口通常不同。
- 较旧设备：先通过 USB 连接并授权，选择该 USB 设备后点击“USB 开启 5555”，再填写手机的局域网 IP 和 `:5555`（例如 `192.168.1.20:5555`）并点击“连接”。
- 需要断开时，在顶部选择 Wi-Fi 设备，点击“断开当前 Wi-Fi”。

文件管理页采用双栏：左侧浏览本机文件夹，右侧浏览设备文件夹。两侧都能查看名称、大小和修改时间，支持双击进入文件夹、路径跳转、返回、上级、刷新及显示隐藏文件。选中左侧的一个或多个文件/文件夹后，点击中间的 **→ 上传** 到右侧当前目录；选中右侧的项后，点击 **← 下载** 到左侧当前目录。右侧还可多选设备文件或文件夹，点击 **删除选中** 并确认后永久删除；删除文件夹会连同内容一起删除，完成后会刷新列表。传输遇到同名项会先确认。设备从 `/sdcard/` 开始浏览，受 Android 权限限制的系统目录可能无法打开。

屏幕截图页会记录从本版本起通过工具保存的截图，显示文件名、时间、设备和保存文件夹。选择记录后点击“在 Finder 中显示”或 Windows 上的“在资源管理器中显示”，也可双击记录定位原文件。历史记录在 macOS 保存在 `~/Library/Application Support/Sandevistan ADB/screenshot_history.json`，Windows 保存在 `%APPDATA%\Sandevistan ADB\screenshot_history.json`；重新启动应用后仍可查看，移动或删除截图文件后定位时会提示文件不存在。

实时日志页的“来源”可选 Logcat 或 HiLog（华为/鸿蒙）。检测到华为设备时会默认选择 HiLog，仍可手动切换。Logcat 默认同时读取 main、system、events 和 crash 缓冲区，也可切换为其中单个缓冲区；HiLog 直接读取设备的 HiLog 流，不使用 Logcat 缓冲区。包名旁可以从当前设备的用户安装应用中选择，也可以手动输入；应用列表在后台加载，可点击“刷新应用”更新。点击“启动”持续读取，点击“停止”结束读取。包名、最低级别（V/D/I/W/E）和关键词会在本机过滤已收到的日志；包名会匹配日志文字及当前应用进程 PID。页面显示收到与匹配的行数；如果应用没有向当前来源写日志，包名过滤结果可能为空。Error 行显示为红色，Warning 行显示为黄色。页面最多保留最近 10000 行，可导出当前筛选结果为 TXT；“清空显示”只清除页面内容，“清空设备缓冲区”会清除所选 Logcat 缓冲区，选择 HiLog 时可清空设备 HiLog。切换设备会停止旧设备的日志流。

性能监控页可从当前设备的用户应用列表选择包名，也可手动输入。点击“开始监控”后，约每 2.5 秒采样一次并保留最近 60 条记录；离开页面、切换设备或点击“停止”会停止采样。CPU 为该应用进程使用率之和，以单个逻辑核心为 100%；内存显示 PSS 与 RSS。网络卡片分别显示设备物理接口合计的实时 RX/TX 速率，以及应用 UID 的累计 RX/TX；UID 统计由 Android 系统提供，可能延迟，不能当作实时应用网速。帧率优先读取匹配应用的 SurfaceView/窗口图层的 SurfaceFlinger 帧时间，获取不到时改用 `dumpsys gfxinfo ... framestats`。应用没有运行或设备未提供帧时间时显示空值。

“Dart 网络”页用于正在运行的 Flutter debug/profile 应用。选择设备和包名后点击“开始抓取”，工具从该应用进程日志中发现 Dart VM Service，经临时 ADB 端口转发开启 `dart:io` HTTP 记录；新请求会显示方法、状态、耗时和 URL，选中后读取可用的请求头、响应头和正文。列表支持关键词筛选与导出 JSON，停止或切换设备时会恢复原来的记录开关并清理临时端口转发。仅记录通过 Dart `HttpClient` 发出的请求（包括常见的 Dio 请求），原生插件的请求可能不在列表中；release 包没有可连接的 VM Service。页面最多保留最近 3000 条请求，导出的 JSON 对已选中读取过的请求包含详细内容，其余请求包含列表字段。

同页的“抓取 ANR / tombstone”会把崩溃文件保存到所选文件夹下的独立目录。设备允许读取时直接拉取 `/data/anr` 和 `/data/tombstones`；受权限限制时自动生成 bugreport 并提取其中可用的文件。若 bugreport 不含这些文件，会保留原始 ZIP 及其中的文本报告供查看。生成 bugreport 可能耗时数分钟。

所有操作通过 Android 官方 `adb` 程序执行。设备连接状态、手机存储权限和 Android 版本会影响可用操作。错误信息会显示在窗口下方的操作记录中。
