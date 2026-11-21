"""Desktop integration kept outside the editing and rendering workflows."""
import ctypes
import hashlib
import os
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path


def prepare_external_tools():
    if not getattr(sys, "frozen", False):
        return
    if sys.platform.startswith("linux"):
        # glibc already captured VAT's loader path at startup. Restore the environment
        # inherited by tools such as FFmpeg, Tesseract and the user's model CLI.
        if "LD_LIBRARY_PATH_ORIG" in os.environ:
            os.environ["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
    elif sys.platform == "darwin":
        # Finder launches with a system-only PATH. Include installed Homebrew bin
        # directories so a selected CLI using /usr/bin/env node can find its runtime.
        paths = os.environ.get("PATH", os.defpath).split(os.pathsep)
        for directory in ("/opt/homebrew/bin", "/usr/local/bin"):
            if directory not in paths and Path(directory).is_dir():
                paths.append(directory)
        os.environ["PATH"] = os.pathsep.join(paths)


def open_path(path):
    path = str(Path(path).resolve())
    try:
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    except OSError as exc:
        raise ValueError(f"无法用系统默认应用打开「{path}」。{exc}") from None


@contextmanager
def workspace_lock(folder):
    """Keep the lock until the editor exits, including after a crash via the OS."""
    if os.name == "nt":
        # Retain the mutex name used by previous Windows releases.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        key = hashlib.sha256(str(folder.resolve()).casefold().encode()).hexdigest()[:16]
        handle = kernel.CreateMutexW(None, False, "Local\\FenjingStudio-" + key)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield ctypes.get_last_error() != 183
        finally:
            kernel.CloseHandle(handle)
    else:
        import fcntl
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (folder / ".vat.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True


def start(studio, store_factory, data_home, app_name):
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from app_paths import default_data_home, set_workspace

    prepare_external_tools()
    if os.name == "nt":
        ctypes.windll.user32.SetProcessDPIAware()
    root = tk.Tk()
    root.withdraw()
    try:
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
    except (ImportError, RuntimeError, tk.TclError):
        pass  # File and folder pickers also work without native drag and drop.
    try:
        with ExitStack() as locks:
            while True:
                try:
                    folder = data_home()
                    if not os.getenv("FENJING_HOME") and folder != default_data_home() and not folder.is_dir():
                        raise ValueError(f"找不到上次选择的工作区「{folder}」。请检查移动硬盘或网络卷。")
                    if not locks.enter_context(workspace_lock(folder)):
                        messagebox.showinfo(app_name, "此工作区已打开，请切换到已有的 VAT 窗口。", parent=root)
                        return
                    store = store_factory(folder)
                    break
                except (OSError, ValueError, sqlite3.Error) as exc:
                    locks.close()
                    if os.getenv("FENJING_HOME"):
                        messagebox.showerror(app_name, f"{exc}\n请检查 FENJING_HOME 指定的目录。", parent=root)
                        return
                    if not messagebox.askyesno(app_name, f"无法打开工作区。{exc}\n要选择其他工作区吗？", parent=root):
                        return
                    selected = filedialog.askdirectory(parent=root, title="选择工作区")
                    if not selected:
                        return
                    set_workspace(selected)
            editor = studio(root, store)
            root.deiconify()
            # 先弹新手教程（可在教程里永久关闭），关掉后再弹每次启动的使用须知
            welcome = getattr(editor, "show_welcome", None) or getattr(editor, "show_notice", None)
            if welcome:
                # 主窗口先画出来再弹窗，避免和标题栏着色抢焦点
                root.after(320, welcome)
            root.mainloop()
    except (OSError, ValueError, sqlite3.Error) as exc:
        messagebox.showerror(app_name, str(exc), parent=root)
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass  # Normal window close already destroyed the Tk application.
