from __future__ import annotations

import sys


def main() -> None:
    # Sharper UI on Windows high-DPI displays
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    from pgwinal.gui.app import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
