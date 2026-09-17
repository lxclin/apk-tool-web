"""PyInstaller hook that labels the separately packaged internal edition."""

import os


os.environ["APK_TOOL_BUILD_EDITION"] = "internal"
