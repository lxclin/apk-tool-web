"""PyInstaller hook that permanently disables internal-only features."""

import os


os.environ["APK_TOOL_BUILD_EDITION"] = "standard"
for _feature in (
    "CP_CANDIDATE_ASSIGNMENT",
    "BACKEND_SUBMISSION",
    "BATCH_AUTOMATION",
    "ASANA_WRITE",
    "BULK_DEVICE_CLEANUP",
):
    os.environ[f"APK_TOOL_PRIVATE_{_feature}"] = "0"
