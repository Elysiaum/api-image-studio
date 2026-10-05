# -*- coding: utf-8 -*-
"""程序入口。

源码运行：

    python -m api_image_studio

打包运行时，本文件同样作为 PyInstaller 的入口脚本。
"""
from __future__ import annotations

import sys


def main() -> None:
    from api_image_studio.gui import run

    run()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback

        detail = traceback.format_exc()
        try:
            from api_image_studio.gui import show_fatal_error

            show_fatal_error(detail)
        except Exception:  # noqa: BLE001
            print(detail, file=sys.stderr)
        raise
