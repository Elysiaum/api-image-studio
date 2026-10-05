# -*- coding: utf-8 -*-
"""一键构建：图标 -> 主程序 -> 安装包。

默认只产出**单个安装程序**：
    dist/API生图平台_安装程序.exe

需要额外的绿色版时加 --all：
    dist/API生图平台/            文件夹版（启动最快）
    dist/API生图平台_便携版.exe  单文件绿色版
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
SRC = os.path.join(ROOT, "src")
PKG = os.path.join(SRC, "api_image_studio")
DIST = os.path.join(ROOT, "dist")
APP_NAME = "API生图平台"
INSTALLER_NAME = f"{APP_NAME}_安装程序"
PORTABLE_NAME = f"{APP_NAME}_便携版"
ICON = os.path.join(PKG, "assets", "app.ico")
ENTRY = os.path.join(PKG, "__main__.py")
PAYLOAD = os.path.join(BUILD, "payload.zip")
PY = sys.executable

COMMON = ["--noconfirm", "--clean", "--windowed",
          # 包位于 src/ 下，需要显式加入搜索路径
          "--paths", SRC,
          "--collect-all", "customtkinter",
          # 用于 ComfyUI 的实时进度；显式声明以免将来重构后漏掉
          "--hidden-import", "websocket",
          # 窗口图标随程序分发
          "--add-data", f"{ICON}{os.pathsep}assets",
          "--icon", ICON]

WORK_DIRS = ("_work_app", "_work_inst", "_work_port")


def run(cmd: list) -> None:
    print("\n>>> " + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def pyinstaller(*extra: str, work: str, name: str, script: str) -> None:
    run([PY, "-m", "PyInstaller", *COMMON,
         "--name", name, *extra,
         "--distpath", DIST, "--workpath", os.path.join(BUILD, work),
         "--specpath", BUILD, script])


def zip_dir(src: str, dst: str) -> int:
    """把目录内容打包（根目录即目录内文件），返回文件数。"""
    n = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for base, _dirs, files in os.walk(src):
            for f in files:
                full = os.path.join(base, f)
                zf.write(full, os.path.relpath(full, src))
                n += 1
    return n


def clean_intermediates(app_dir: str) -> None:
    """删掉中间产物，让 dist 里只剩最终交付物。"""
    shutil.rmtree(app_dir, ignore_errors=True)
    for name in WORK_DIRS:
        shutil.rmtree(os.path.join(BUILD, name), ignore_errors=True)
    for name in os.listdir(BUILD):
        if name.endswith(".spec"):
            try:
                os.remove(os.path.join(BUILD, name))
            except OSError:
                pass
    if os.path.isfile(PAYLOAD):
        try:
            os.remove(PAYLOAD)
        except OSError:
            pass


def main() -> None:
    with_portable = "--all" in sys.argv
    os.makedirs(DIST, exist_ok=True)

    # 清掉上次的产物，避免新旧混杂
    for name in os.listdir(DIST):
        p = os.path.join(DIST, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass

    # 1) 图标
    print("\n[1/5] 生成图标 …", flush=True)
    run([PY, os.path.join(BUILD, "make_icon.py")])

    # 2) 主程序（文件夹版，作为安装包负载）
    print("\n[2/5] 打包主程序 …", flush=True)
    pyinstaller("--onedir", work="_work_app", name=APP_NAME, script=ENTRY)

    app_dir = os.path.join(DIST, APP_NAME)
    exe = os.path.join(app_dir, f"{APP_NAME}.exe")
    if not os.path.isfile(exe):
        raise SystemExit(f"打包失败，未找到 {exe}")

    # 3) 安装包负载
    print("\n[3/5] 生成安装包负载 …", flush=True)
    count = zip_dir(app_dir, PAYLOAD)
    print(f"      payload.zip: {count} 个文件，"
          f"{os.path.getsize(PAYLOAD) / 1048576:.1f} MB", flush=True)

    # 4) 安装程序
    print("\n[4/5] 打包安装程序 …", flush=True)
    pyinstaller("--onefile",
                "--add-data", f"{PAYLOAD}{os.pathsep}.",
                work="_work_inst", name=INSTALLER_NAME,
                script=os.path.join(BUILD, "installer.py"))

    # 5) 可选：便携版
    if with_portable:
        print("\n[5/5] 打包便携版（单文件）…", flush=True)
        pyinstaller("--onefile", work="_work_port", name=PORTABLE_NAME, script=ENTRY)
    else:
        print("\n[5/5] 清理中间产物 …", flush=True)

    clean_intermediates(app_dir)

    installer = os.path.join(DIST, f"{INSTALLER_NAME}.exe")
    print("\n" + "=" * 58, flush=True)
    print(f"  交付物：{os.path.basename(installer)}   "
          f"{os.path.getsize(installer) / 1048576:.1f} MB", flush=True)
    if with_portable:
        p = os.path.join(DIST, f"{PORTABLE_NAME}.exe")
        if os.path.isfile(p):
            print(f"          {os.path.basename(p)}   "
                  f"{os.path.getsize(p) / 1048576:.1f} MB", flush=True)
    print("=" * 58, flush=True)


if __name__ == "__main__":
    main()
