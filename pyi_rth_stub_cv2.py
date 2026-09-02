"""PyInstaller runtime hook: satisfy windows_capture's unused cv2 import.

windows_capture imports cv2 at module scope but only uses it in
Frame.save_as_image() / DxgiDuplicationFrame.save_as_image(), which PaneCast
never calls. Bundling OpenCV to satisfy that import adds ~45 MB to the build,
so it is excluded and this stub stands in for it.

If a future change starts calling save_as_image(), drop the --exclude-module
and --runtime-hook flags from the build so the real OpenCV is bundled again.
"""
import sys
import types

if "cv2" not in sys.modules:
    sys.modules["cv2"] = types.ModuleType("cv2")
