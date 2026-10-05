# -*- coding: utf-8 -*-
"""API 生图平台 —— 安装 / 卸载程序。

本机没有 Inno Setup / NSIS，因此安装程序自身也用 PyInstaller 打包：
  · 安装包内嵌 dist 目录的 zip（payload.zip）
  · 运行时解压到安装目录，创建快捷方式，并注册到「应用和功能」
  · 自身复制一份为 uninstall.exe，带 --uninstall 参数即执行卸载

命令行：
  installer.exe                 进入安装向导
  installer.exe --uninstall     执行卸载
"""
from __future__ import annotations

import base64
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import winreg
import zipfile

import customtkinter as ctk
from tkinter import filedialog, messagebox

APP_NAME = "API生图平台"
APP_VERSION = "1.0.0"
PUBLISHER = "本地工具"
EXE_NAME = f"{APP_NAME}.exe"
PAYLOAD = "payload.zip"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def res(name: str) -> str:
    """定位打包进本程序的资源（图标放在 assets/ 子目录下）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(base, name), os.path.join(base, "assets", name)):
        if os.path.isfile(cand):
            return cand
    return os.path.join(base, name)


def default_install_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "Programs", APP_NAME)


def data_dir() -> str:
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)


def start_menu_dir() -> str:
    return os.path.join(
        os.environ.get("APPDATA") or "",
        "Microsoft", "Windows", "Start Menu", "Programs",
    )


def desktop_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(d):
        d = os.path.join(os.path.expanduser("~"), "桌面")
    return d


# ---------------------------------------------------------------------------
# 快捷方式（用 PowerShell 的 WScript.Shell，避免额外依赖）
# ---------------------------------------------------------------------------

def _ps(script: str) -> bool:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            creationflags=CREATE_NO_WINDOW, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _q(value: str) -> str:
    """PowerShell 单引号字符串转义。"""
    return value.replace("'", "''")


def make_shortcut(lnk: str, target: str, workdir: str, icon: str, desc: str) -> bool:
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    script = (
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$s = $ws.CreateShortcut('{_q(lnk)}')\n"
        f"$s.TargetPath = '{_q(target)}'\n"
        f"$s.WorkingDirectory = '{_q(workdir)}'\n"
        f"$s.IconLocation = '{_q(icon)}'\n"
        f"$s.Description = '{_q(desc)}'\n"
        "$s.Save()\n"
    )
    return _ps(script)


def remove_shortcut(lnk: str) -> None:
    try:
        if os.path.isfile(lnk):
            os.remove(lnk)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def register_uninstall(install_dir: str, size_kb: int) -> None:
    exe = os.path.join(install_dir, EXE_NAME)
    uninst = os.path.join(install_dir, "uninstall.exe")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, f"{exe},0")
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninst}" --uninstall')
        winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ, f'"{uninst}" --uninstall --silent')
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, max(1, size_kb))


def unregister_uninstall() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def is_installed() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return winreg.QueryValueEx(key, "InstallLocation")[0]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 安装 / 卸载核心
# ---------------------------------------------------------------------------

def _dir_size_kb(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total // 1024


def install(payload_zip: str, target: str, desktop: bool, startmenu: bool,
            progress) -> None:
    def step(pct: float, msg: str) -> None:
        progress(pct, msg)

    step(0.05, "正在准备安装目录…")
    cleanup_strays()
    os.makedirs(target, exist_ok=True)

    step(0.10, "正在释放程序文件…")
    with zipfile.ZipFile(payload_zip) as zf:
        members = zf.infolist()
        total = len(members) or 1
        for i, item in enumerate(members, start=1):
            zf.extract(item, target)
            if i % 40 == 0 or i == total:
                step(0.10 + 0.68 * (i / total), f"正在释放程序文件… {int(i / total * 100)}%")

    step(0.82, "正在写入卸载程序…")
    uninst = os.path.join(target, "uninstall.exe")
    try:
        shutil.copy2(sys.executable, uninst)
    except OSError:
        pass

    exe = os.path.join(target, EXE_NAME)

    step(0.88, "正在创建快捷方式…")
    if desktop:
        make_shortcut(os.path.join(desktop_dir(), f"{APP_NAME}.lnk"),
                      exe, target, exe, APP_NAME)
    if startmenu:
        sm = os.path.join(start_menu_dir(), APP_NAME)
        os.makedirs(sm, exist_ok=True)
        make_shortcut(os.path.join(sm, f"{APP_NAME}.lnk"), exe, target, exe, APP_NAME)
        make_shortcut(os.path.join(sm, "卸载.lnk"),
                      os.path.join(target, "uninstall.exe"), target,
                      os.path.join(target, "uninstall.exe"), f"卸载 {APP_NAME}")

    step(0.94, "正在登记到「应用和功能」…")
    register_uninstall(target, _dir_size_kb(target))

    step(1.0, "安装完成")


def _safe_remove(path: str) -> bool:
    """只允许删除明显属于本程序的目录，避免误删。"""
    p = os.path.abspath(path).rstrip("\\/")
    if not p or len(p) < 12 or APP_NAME not in p:
        return False
    if not os.path.isdir(p):
        return False
    # 不能是盘符根目录或用户主目录
    if os.path.dirname(p) in ("", os.path.expanduser("~")):
        return False
    return True


def _schedule_delete_on_reboot(path: str) -> None:
    """兜底：把删不掉的文件登记给系统，重启后自动清除。"""
    import ctypes
    MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
    targets = []
    if os.path.isdir(path):
        for base, dirs, files in os.walk(path):
            targets.extend(os.path.join(base, f) for f in files)
            targets.extend(os.path.join(base, d) for d in dirs)
    targets.append(path)
    for p in targets:
        try:
            ctypes.windll.kernel32.MoveFileExW(str(p), None, MOVEFILE_DELAY_UNTIL_REBOOT)
        except Exception:  # noqa: BLE001
            pass


def _spawn_cmd_rmdir(path: str) -> None:
    """最后手段：交给一个延迟的 cmd 进程删除目录。"""
    _spawn_cmd(f'ping -n 3 127.0.0.1 >nul & rmdir /s /q "{path}"')


def _spawn_cmd_del(path: str) -> None:
    """延迟删除单个文件。"""
    _spawn_cmd(f'ping -n 3 127.0.0.1 >nul & del /f /q "{path}"')


def _spawn_cmd(cmd: str) -> None:
    try:
        subprocess.Popen(["cmd", "/c", cmd], shell=False,
                         creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                         close_fds=True)
    except Exception:  # noqa: BLE001
        pass


def _stray_target(install_dir: str) -> str:
    """给「正在运行的卸载程序」找一个同盘的落脚点。

    Windows 允许重命名正在运行的 exe，但不允许删除它。
    所以把它移出安装目录，目录就能干净删掉了。
    """
    drive = os.path.splitdrive(os.path.abspath(install_dir))[0].lower()
    name = f"{APP_NAME}_uninstall_{os.getpid()}.exe"
    for base in (tempfile.gettempdir(), os.path.dirname(os.path.abspath(install_dir))):
        if base and os.path.splitdrive(base)[0].lower() == drive:
            return os.path.join(base, name)
    return ""


def cleanup_strays() -> None:
    """清理历史遗留的临时卸载程序（此时它们早已退出，可以正常删除）。"""
    seen = {tempfile.gettempdir(), os.path.dirname(default_install_dir())}
    for base in seen:
        if not base or not os.path.isdir(base):
            continue
        try:
            for f in os.listdir(base):
                if f.startswith(f"{APP_NAME}_uninstall_") and f.endswith(".exe"):
                    try:
                        os.remove(os.path.join(base, f))
                    except OSError:
                        pass
        except OSError:
            pass


def spawn_delete(path: str) -> None:
    """彻底删除一个目录，包含正在运行的卸载程序自身。"""
    if not _safe_remove(path):
        return
    path = os.path.abspath(path)

    # 1) 先把能删的都删掉（正在运行的 uninstall.exe 删不掉）
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.isdir(path):
        return

    # 2) 把自身移出安装目录 —— 运行中的 exe 允许重命名，之后目录就空了
    stray = ""
    self_exe = os.path.abspath(sys.executable)
    if os.path.isfile(self_exe) and os.path.dirname(self_exe).lower() == path.lower():
        target = _stray_target(path)
        if target:
            try:
                shutil.move(self_exe, target)
                stray = target
            except OSError:
                pass

    # 3) 再删一次
    shutil.rmtree(path, ignore_errors=True)
    if os.path.isdir(path):
        try:
            os.rmdir(path)
        except OSError:
            pass

    # 4) 仍有残留才动用延迟 cmd / 重启兜底
    if os.path.isdir(path):
        _spawn_cmd_rmdir(path)
        _schedule_delete_on_reboot(path)

    # 5) 临时文件已随进程退出而解锁，登记重启清除做保险
    if stray and os.path.exists(stray):
        try:
            os.remove(stray)
        except OSError:
            _spawn_cmd_del(stray)
            _schedule_delete_on_reboot(stray)


def uninstall(silent: bool = False, remove_data: bool = False) -> None:
    target = is_installed() or default_install_dir()

    if not silent:
        extra = "\n\n是否同时删除配置与历史记录？" if os.path.isdir(data_dir()) else ""
        if not messagebox.askyesno(
            f"卸载 {APP_NAME}",
            f"确定要卸载 {APP_NAME} 吗？\n\n安装目录：\n{target}{extra}\n\n"
            "（已生成的图片不会被删除）",
        ):
            return
        if os.path.isdir(data_dir()):
            remove_data = messagebox.askyesno(
                f"卸载 {APP_NAME}",
                "是否删除配置与历史记录？\n\n选「否」将保留，重新安装后仍可使用。",
            )

    remove_shortcut(os.path.join(desktop_dir(), f"{APP_NAME}.lnk"))
    remove_shortcut(os.path.join(start_menu_dir(), APP_NAME, f"{APP_NAME}.lnk"))
    remove_shortcut(os.path.join(start_menu_dir(), APP_NAME, "卸载.lnk"))
    try:
        sm = os.path.join(start_menu_dir(), APP_NAME)
        if os.path.isdir(sm) and not os.listdir(sm):
            os.rmdir(sm)
    except OSError:
        pass

    unregister_uninstall()

    if remove_data:
        spawn_delete(data_dir())

    spawn_delete(target)

    if not silent:
        messagebox.showinfo(
            f"卸载 {APP_NAME}",
            f"{APP_NAME} 已卸载。\n\n已生成的图片保留在「图片\\{APP_NAME}」目录中。",
        )


# ---------------------------------------------------------------------------
# 安装向导界面
# ---------------------------------------------------------------------------

class Installer(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(f"{APP_NAME} {APP_VERSION} 安装程序")
        self.geometry("600x430")
        self.resizable(False, False)

        self.target = ctk.StringVar(value=is_installed() or default_install_dir())
        self.opt_desktop = ctk.BooleanVar(value=True)
        self.opt_startmenu = ctk.BooleanVar(value=True)
        self.opt_run = ctk.BooleanVar(value=True)
        self.busy = False
        self.q: "queue.Queue" = queue.Queue()

        self._build()
        self.after(60, self._drain)

        try:
            if os.path.isfile(res("app.ico")):
                self.iconbitmap(res("app.ico"))
        except Exception:  # noqa: BLE001
            pass

    def _build(self) -> None:
        head = ctk.CTkFrame(self, corner_radius=0, height=96)
        head.pack(fill="x")
        head.pack_propagate(False)
        inner = ctk.CTkFrame(head, fg_color="transparent")
        inner.pack(side="left", padx=22, pady=18)
        ctk.CTkLabel(inner, text="◆", font=ctk.CTkFont(size=26, weight="bold"),
                     text_color="#7C6CFF").pack(side="left", padx=(0, 12))
        txt = ctk.CTkFrame(inner, fg_color="transparent")
        txt.pack(side="left")
        ctk.CTkLabel(txt, text=APP_NAME, font=ctk.CTkFont(size=19, weight="bold"),
                     anchor="w").pack(anchor="w")
        ctk.CTkLabel(txt, text=f"版本 {APP_VERSION} · 本地桌面工具，不需要联网即可安装",
                     font=ctk.CTkFont(size=11), text_color=("gray45", "gray65"),
                     anchor="w").pack(anchor="w")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=(16, 0))

        ctk.CTkLabel(body, text="安装位置", font=ctk.CTkFont(size=12, weight="bold"),
                     anchor="w").pack(anchor="w", pady=(0, 6))
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        self.ent_dir = ctk.CTkEntry(row, textvariable=self.target, height=36,
                                    corner_radius=9, font=ctk.CTkFont(size=12))
        self.ent_dir.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="浏览…", width=72, height=36, corner_radius=9,
                      font=ctk.CTkFont(size=12), fg_color="transparent", border_width=1,
                      command=self._browse).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(body, text="安装到当前用户目录，无需管理员权限。",
                     font=ctk.CTkFont(size=11), text_color=("gray50", "gray65"),
                     anchor="w").pack(anchor="w", pady=(6, 14))

        ctk.CTkLabel(body, text="快捷方式", font=ctk.CTkFont(size=12, weight="bold"),
                     anchor="w").pack(anchor="w", pady=(0, 6))
        ctk.CTkCheckBox(body, text="创建桌面快捷方式", variable=self.opt_desktop,
                        font=ctk.CTkFont(size=12), checkbox_width=17, checkbox_height=17
                        ).pack(anchor="w", pady=3)
        ctk.CTkCheckBox(body, text="创建开始菜单快捷方式", variable=self.opt_startmenu,
                        font=ctk.CTkFont(size=12), checkbox_width=17, checkbox_height=17
                        ).pack(anchor="w", pady=3)
        ctk.CTkCheckBox(body, text="安装完成后立即运行", variable=self.opt_run,
                        font=ctk.CTkFont(size=12), checkbox_width=17, checkbox_height=17
                        ).pack(anchor="w", pady=3)

        self.lbl_status = ctk.CTkLabel(body, text="", font=ctk.CTkFont(size=12),
                                       text_color=("gray45", "gray65"), anchor="w")
        self.lbl_status.pack(anchor="w", pady=(16, 4), fill="x")
        self.bar = ctk.CTkProgressBar(body, height=6, corner_radius=3)
        self.bar.set(0)
        self.bar.pack(fill="x")

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=22, pady=16)
        self.btn_install = ctk.CTkButton(foot, text="开始安装", width=118, height=40,
                                         corner_radius=10, font=ctk.CTkFont(size=14, weight="bold"),
                                         command=self._start)
        self.btn_install.pack(side="right")
        ctk.CTkButton(foot, text="取消", width=84, height=40, corner_radius=10,
                      font=ctk.CTkFont(size=13), fg_color="transparent", border_width=1,
                      command=self.destroy).pack(side="right", padx=10)

    def _browse(self) -> None:
        d = filedialog.askdirectory(parent=self, title="选择安装位置",
                                    initialdir=os.path.dirname(self.target.get()))
        if d:
            self.target.set(os.path.normpath(os.path.join(d, APP_NAME)))

    # -- 线程与界面 ---------------------------------------------------------
    def _drain(self) -> None:
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        try:
            self.after(60, self._drain)
        except Exception:  # noqa: BLE001
            pass

    def _post(self, fn, *args) -> None:
        self.q.put((fn, args))

    def _progress(self, pct: float, msg: str) -> None:
        self._post(self._set_progress, pct, msg)

    def _set_progress(self, pct: float, msg: str) -> None:
        self.bar.set(pct)
        self.lbl_status.configure(text=msg)

    def _start(self) -> None:
        if self.busy:
            return
        target = (self.target.get() or "").strip()
        if not target:
            messagebox.showwarning(APP_NAME, "请填写安装位置。", parent=self)
            return
        if not os.path.isabs(target):
            messagebox.showwarning(APP_NAME, "安装位置必须是绝对路径。", parent=self)
            return

        payload = res(PAYLOAD)
        if not os.path.isfile(payload):
            messagebox.showerror(APP_NAME, f"安装包数据缺失：{PAYLOAD}", parent=self)
            return

        if is_installed() and not messagebox.askyesno(
            APP_NAME, "检测到已安装过，是否覆盖安装？", parent=self
        ):
            return

        self.busy = True
        self.btn_install.configure(state="disabled", text="安装中…")
        self.bar.set(0.02)

        threading.Thread(
            target=self._worker,
            args=(payload, target, self.opt_desktop.get(), self.opt_startmenu.get()),
            daemon=True,
        ).start()

    def _worker(self, payload: str, target: str, desktop: bool, startmenu: bool) -> None:
        try:
            install(payload, target, desktop, startmenu, self._progress)
            self._post(self._done, target, None)
        except Exception as exc:  # noqa: BLE001
            self._post(self._done, target, f"{type(exc).__name__}: {exc}")

    def _done(self, target: str, err) -> None:
        self.busy = False
        if err:
            self.btn_install.configure(state="normal", text="重试")
            self.lbl_status.configure(text=f"安装失败：{err}", text_color=("#B91C1C", "#F87171"))
            messagebox.showerror(
                APP_NAME,
                f"安装失败：\n\n{err}\n\n"
                "如果提示权限不足，请换一个目录（例如桌面上的文件夹）重试。",
                parent=self,
            )
            return

        self.btn_install.configure(text="完成")
        self.bar.set(1)
        self.lbl_status.configure(text="安装完成", text_color=("#15803D", "#4ADE80"))

        if self.opt_run.get():
            try:
                subprocess.Popen([os.path.join(target, EXE_NAME)], cwd=target)
            except Exception:  # noqa: BLE001
                pass
        self.after(600, self.destroy)


# ---------------------------------------------------------------------------

def _say(msg: str) -> None:
    """静默模式下没有控制台，安全地尝试输出。"""
    try:
        if sys.stderr:
            sys.stderr.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    args = sys.argv[1:]
    low = [a.lower() for a in args]

    if "--uninstall" in low:
        uninstall(silent="--silent" in low)
        return

    if "--silent" in low:
        # 静默安装：installer.exe --silent [--dir "路径"] [--no-desktop] [--no-startmenu]
        target = default_install_dir()
        for i, a in enumerate(args):
            if a.lower() == "--dir" and i + 1 < len(args):
                target = args[i + 1]
        payload = res(PAYLOAD)
        if not os.path.isfile(payload):
            _say(f"安装包数据缺失：{PAYLOAD}")
            sys.exit(2)
        try:
            install(payload, target, "--no-desktop" not in low,
                    "--no-startmenu" not in low, lambda *_a: None)
        except Exception as exc:  # noqa: BLE001
            _say(f"安装失败：{exc}")
            sys.exit(1)
        _say(f"已安装到 {target}")
        return

    app = Installer()
    app.mainloop()


if __name__ == "__main__":
    main()
