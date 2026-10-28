"""Design tokens for the Qt interface.

Every colour, spacing value, radius and font size used by the UI lives here.
Widgets should never hard-code a hex value or a pixel number of their own.
"""

from __future__ import annotations


# --- Colour -------------------------------------------------------------
# One accent, one danger, the rest neutral greys.
PRIMARY = "#03A9F4"
PRIMARY_HOVER = "#29B6F6"
PRIMARY_PRESSED = "#0288D1"
PRIMARY_TEXT = "#0288D1"          # accent used as ink, not as a fill
PRIMARY_SOFT = "#E1F5FE"

DANGER = "#DC3545"
DANGER_HOVER = "#E4606D"
DANGER_PRESSED = "#C82333"

SUCCESS = "#00C853"
WARNING = "#FFB300"
WARNING_TEXT = "#9A6900"

SIDEBAR = "#263238"
SIDEBAR_HOVER = "#2E3D45"
SIDEBAR_ACTIVE = "#37474F"
SIDEBAR_TEXT = "#CFD8DC"
SIDEBAR_MUTED = "#90A4AE"

BACKGROUND = "#F5F7FA"
SURFACE = "#FFFFFF"
SURFACE_SUNKEN = "#F7F9FB"
HOVER = "#EEF2F6"

TEXT = "#1F2933"
TEXT_MUTED = "#66727F"
TEXT_DISABLED = "#A8B2BC"

BORDER = "#E3E8EE"
BORDER_STRONG = "#C9D2DC"
BORDER_FOCUS = PRIMARY

# --- Spacing: multiples of 4 only ---------------------------------------
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 20
SPACE_6 = 24
SPACE_8 = 32
SPACE_12 = 48

# --- Geometry -----------------------------------------------------------
RADIUS_CARD = 12
RADIUS_WORKBENCH_CARD = 7
RADIUS_CONTROL = 6
BORDER_WIDTH = 1

CONTROL_HEIGHT = 32
BUTTON_HEIGHT = 36
ROW_HEIGHT = 34

SIDEBAR_WIDTH = 200
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 780
PAGE_PADDING = 32
LOGCAT_APP_SELECT_WIDTH = 200
LOGCAT_VIEW_MIN_HEIGHT = 400
LOGCAT_WORKBENCH_VIEW_MIN_HEIGHT = 240
SHOT_PREVIEW_PANE_WIDTH = 240
SHOT_PREVIEW_HEIGHT = 280
SHOT_HISTORY_THUMB_WIDTH = 36
SHOT_HISTORY_THUMB_HEIGHT = 24
FILE_NAV_BUTTON_PADDING = 7
PERFORMANCE_CHART_HEIGHT = 150
PERFORMANCE_TABLE_MIN_HEIGHT = 140
NETWORK_REQUEST_MIN_WIDTH = 280
GPU_API_MIN_HEIGHT = 48
APPS_LIST_MIN_WIDTH = 260
APPS_DETAILS_MIN_WIDTH = 360
APPS_DEVICE_SELECT_MIN_WIDTH = 280
APPS_DEVICE_SELECT_MAX_WIDTH = 420
APPS_SEARCH_MAX_WIDTH = 700
APPS_FOREGROUND_MAX_WIDTH = 600
APPS_ACTIVITY_EXPANDED_MIN_HEIGHT = 208
APPS_ACTIVITY_LOG_HEIGHT = 156
APPS_ACTIVITY_TOGGLE_HEIGHT = 34
ACTIVITY_LOG_HEIGHT = 132

# --- Type ---------------------------------------------------------------
FONT_FAMILY = '"Helvetica Neue"'
FONT_MONO = '"SF Mono", "Menlo"'

FONT_SIZE_BODY = 11
FONT_SIZE_SMALL = 10
FONT_SIZE_SUBTITLE = 13
FONT_SIZE_TITLE = 22

# --- Icons (qtawesome / Font Awesome 6 solid) ---------------------------
ICON_APPS = "fa6s.cube"
ICON_SHOT = "fa6s.camera"
ICON_FILES = "fa6s.folder-open"
ICON_LOG = "fa6s.list"
ICON_PERFORMANCE = "fa6s.chart-line"
ICON_DART_NETWORK = "fa6s.globe"
ICON_REFRESH = "fa6s.rotate-right"
ICON_WIFI = "fa6s.wifi"
ICON_FOLDER = "fa6s.folder"
ICON_INBOX = "fa6s.inbox"
ICON_DEVICE = "fa6s.mobile-screen"
ICON_DEVICE_INFO = "fa6s.microchip"
