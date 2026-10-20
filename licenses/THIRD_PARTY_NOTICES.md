# Sandevistan ADB · 第三方许可

本应用使用以下第三方组件。应用包内 `licenses/` 目录提供相应许可正文。
这些声明不改变 Sandevistan ADB 自身代码的授权方式。

| 组件 | 随包版本/内容 | 许可 | 随包文件 |
| --- | --- | --- | --- |
| Qt | 6.11.1；Qt Core、GUI、Widgets、Network、DBus、OpenGL、SVG 与图像格式插件 | LGPLv3（也可依 GPLv3 分发） | `LGPL-3.0.txt`、`GPL-3.0.txt` |
| PySide6 / Shiboken6 | 6.11.1 | LGPLv3（也可依 GPLv3 分发） | `LGPL-3.0.txt`、`GPL-3.0.txt` |
| CPython | 3.13.0 | Python Software Foundation License | `Python-3.13.0-LICENSE.txt` |
| OpenSSL | 3.0.15；`libssl`、`libcrypto` | Apache License 2.0 | `OpenSSL-3.0.15-LICENSE.txt` |
| QtAwesome | 1.4.2 | MIT | `QtAwesome-1.4.2-MIT.txt` |
| QtPy | 2.4.3 | MIT | `QtPy-2.4.3-MIT.txt` |
| Font Awesome Free | 6.7.2 solid 字体及字形表 | 字体为 SIL OFL 1.1；其他随包文件按原项目声明 | `FontAwesome-6-LICENSE.txt` |

Qt 与 PySide6 的版权归 The Qt Company 和各贡献者所有。你可以依 LGPLv3
使用、修改和替换这些库；本应用不得限制为调试库的修改而进行的逆向分析。
本包在 `licenses/sources/` 中直接提供对应源码及校验值，具体替换方法
见 `SOURCES_AND_RELINKING.md`。
Qt 的第三方组件及其许可按对应版本的
[Qt 文档](https://doc.qt.io/qt-6.11/licenses-used-in-qt.html)
和 [Qt for Python 文档](https://doc.qt.io/qtforpython-6.11/licenses.html)核对。

Font Awesome Free：Copyright © Fonticons, Inc.；保留字体名称
“Font Awesome”。原项目许可正文已随包提供。

本应用使用的 `adb` 由使用者另外安装；它不包含在本应用包内。
PyInstaller 用于生成应用包，其官方许可例外允许分发所生成的商业应用包，
并不要求应用附带 PyInstaller 的许可文件。
