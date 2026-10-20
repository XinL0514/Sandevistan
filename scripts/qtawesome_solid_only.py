"""Load only the Font Awesome 6 solid font used by this application.

QtAwesome 1.4.2 eagerly loads all of its bundled fonts on first icon access.
The distribution includes only fa6s, so narrow the registry before main_qt
imports and uses QtAwesome. Keep this hook in sync with requirements-qt.txt.
"""

import qtawesome


fonts = tuple(font for font in qtawesome._BUNDLED_FONTS if font[0] == "fa6s")
if len(fonts) != 1:
    raise RuntimeError("QtAwesome's Font Awesome 6 solid registry has changed")
qtawesome._BUNDLED_FONTS = fonts
