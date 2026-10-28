# Qt / PySide6 源码与替换说明

本包使用 Qt 6.11.1、PySide6 6.11.1 和 Shiboken6 6.11.1 的社区版动态库。
`sources/` 目录直接提供对应的上游源码归档与 `SHA256SUMS.txt`。
这些第三方库没有经过本项目修改；若发行者以后修改了库，必须将修改内容
一同提供给接收者。源码归档由 Qt 官方发行站点提供：

- Qt Base、Qt SVG、Qt Image Formats：<https://download.qt.io/official_releases/qt/6.11/6.11.1/submodules/>
- PySide6 / Shiboken6：<https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/>

可以在此目录运行 `shasum -a 256 -c SHA256SUMS.txt` 核对归档。
Qt 与 Qt for Python 的构建方法分别见
<https://doc.qt.io/qt-6.11/build-sources.html> 和
<https://doc.qt.io/qtforpython-6.11/building_from_source/index.html>。

## 在 macOS 上替换动态库

请先复制应用包作为测试副本。Qt 动态框架位于：

`Sandevistan ADB.app/Contents/Frameworks/PySide6/Qt/lib/`

PySide6 / Shiboken6 的动态库位于 `Contents/Frameworks/PySide6/` 和
`Contents/Frameworks/shiboken6/`。使用上述源码构建 ABI 兼容的 macOS
arm64 动态库，以相同的框架名、版本目录及安装名称替换相应文件；
Qt 插件位于 `Contents/Frameworks/PySide6/Qt/plugins/`，如有修改也可替换。
保留原包中其余文件，再对测试副本重新签名：

```sh
xattr -cr "Sandevistan ADB.app"
codesign --force --deep --sign - "Sandevistan ADB.app"
codesign --verify --deep "Sandevistan ADB.app"
```

此处的签名是本机 ad hoc 签名，用于测试修改过的副本；它不冒充发行者的
Developer ID 签名。用户可以对修改版自行签名并运行。本应用不限制用户
为调试上述 LGPL 库的修改而进行的逆向分析。

## 在 Windows 上替换动态库

先复制解压后的 `Sandevistan ADB` 文件夹作为测试副本。Qt、PySide6、
Shiboken6 的 DLL 和 `.pyd` 文件位于 `_internal/` 下的对应包目录；Qt 插件
位于 `_internal/PySide6/Qt/plugins/`。使用上述源码构建与原包 ABI 兼容的
Windows x64 动态库，以相同文件名替换测试副本中的 DLL、`.pyd` 或插件，
并保留其他文件。Windows 开发包没有代码签名，不限制对上述库进行替换和
调试；修改后的副本需要自行验证能正常启动和运行。
