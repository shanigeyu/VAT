"""Desktop integration kept outside the editing and rendering workflows."""
import ctypes
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from contextlib import ExitStack, contextmanager
from pathlib import Path

LOG_NAME = "vat.log"
LOG_LIMIT = 2 * 1024 * 1024


def log_location():
    from app_paths import config_dir
    return str(config_dir().parent / "logs" / LOG_NAME)


def log(message, error=None):
    """记一条运行记录。日志本身出错不能影响程序，所以全部吞掉。"""
    try:
        path = Path(log_location())
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_name(path.stem + ".1" + path.suffix))
        text = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + str(message)
        if error is not None:
            if isinstance(error, BaseException):
                text += "\n" + "".join(traceback.format_exception(type(error), error, error.__traceback__))
            else:
                text += "\n" + str(error)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    except Exception:
        pass


def install_exception_logging():
    """打包后的窗口程序没有控制台，未捕获的异常必须落到文件里，否则无从排查。"""
    def hook(kind, value, tb):
        if kind is SystemExit:
            return
        log("未捕获的异常", error=value)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else ""
        log("后台线程异常 · " + name, error=args.exc_value)

    sys.excepthook = hook
    threading.excepthook = thread_hook


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


def activate_window(title):
    """把已经开着的窗口提到前台；没有找到时返回 False。

    第二个实例被工作区锁挡住时，光弹一句「已打开」会让人觉得软件打不开——尤其是
    前一个窗口最小化在任务栏里、或者被别的窗口压在下面的时候。
    """
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        procedure_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows.argtypes = [procedure_type, ctypes.c_void_p]
        user32.EnumWindows.restype = ctypes.c_bool
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        user32.BringWindowToTop.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        user32.SwitchToThisWindow.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
        user32.AttachThreadInput.restype = ctypes.c_bool
        kernel32.GetCurrentProcessId.restype = ctypes.c_ulong
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        mine = kernel32.GetCurrentProcessId()
        found = []

        def visit(hwnd, _param):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value == title:
                    owner = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                    if owner.value != mine:
                        found.append(hwnd)
            return True

        user32.EnumWindows(procedure_type(visit), None)
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE：已最小化的窗口也能拉回来
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SwitchToThisWindow(hwnd, True)
            if user32.GetForegroundWindow() != hwnd:
                # 前台锁会拒绝后台进程直接抢焦点；临时接到前台线程上再试一次。
                foreground = user32.GetForegroundWindow()
                if foreground:
                    target = user32.GetWindowThreadProcessId(foreground, None)
                    current = kernel32.GetCurrentThreadId()
                    attached = bool(target and target != current
                                    and user32.AttachThreadInput(current, target, True))
                    try:
                        user32.SetForegroundWindow(hwnd)
                    finally:
                        if attached:
                            user32.AttachThreadInput(current, target, False)
        return bool(found)
    except Exception as exc:
        log("唤起已有窗口失败", error=exc)
        return False


def report(root, app_name, message):
    """出错时一定要让人看见原因；打包后没有控制台，异常不弹出来就等于没发生。"""
    text = str(message).strip()
    location = log_location()
    log(app_name + " · " + text)
    try:
        from tkinter import messagebox
        messagebox.showerror(app_name, text + "\n\n运行记录：" + location, parent=root)
    except Exception as exc:
        log("无法显示错误窗口", error=exc)


def reveal(root):
    """把主窗口真正摆到屏幕上：最小化、坐标跑到屏幕外都不能留在那儿。"""
    try:
        root.deiconify()
        root.state("normal")
    except Exception as exc:
        log("恢复窗口失败", error=exc)
    try:
        root.update_idletasks()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        width, height = root.winfo_width(), root.winfo_height()
        x, y = root.winfo_x(), root.winfo_y()
        outside = (x + width <= 40 or y + height <= 40
                   or x >= screen_w - 40 or y >= screen_h - 40)
        if outside:
            root.geometry(f"+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 2 - 18)}")
            root.update_idletasks()
        root.lift()
    except Exception as exc:
        log("摆放窗口失败", error=exc)
    if os.name == "nt":
        try:
            root.focus_force()
        except Exception:
            pass  # 前台锁被系统拒绝时保持现状，不影响使用。


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

    install_exception_logging()
    prepare_external_tools()
    if os.name == "nt":
        ctypes.windll.user32.SetProcessDPIAware()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        log("无法创建窗口", error=exc)
        try:
            print(f"{app_name} 无法创建窗口：{exc}", file=sys.stderr)
        except Exception:
            pass  # 打包成窗口程序后没有 stderr，这次失败只留在日志里。
        return
    root.withdraw()
    try:
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
    except (ImportError, RuntimeError, tk.TclError):
        pass  # File and folder pickers also work without native drag and drop.
    shown = False
    try:
        with ExitStack() as locks:
            folder = None
            while True:
                try:
                    folder = data_home()
                    if not os.getenv("FENJING_HOME") and folder != default_data_home() and not folder.is_dir():
                        raise ValueError(f"找不到上次选择的工作区「{folder}」。请检查移动硬盘或网络卷。")
                    if not locks.enter_context(workspace_lock(folder)):
                        # 另一个实例还在跑：能唤醒就交回给它，唤不醒也要说清楚怎么处理。
                        if activate_window(app_name):
                            log("已有窗口，已还原")
                            return
                        messagebox.showinfo(
                            app_name,
                            "此工作区已在另一个 VAT 窗口里打开，但没找到它的窗口。\n"
                            "请到任务管理器结束 VAT.exe 后重新打开，或在「设置」里换一个工作区。",
                            parent=root)
                        return
                    store = store_factory(folder)
                    break
                except (OSError, ValueError, sqlite3.Error) as exc:
                    locks.close()
                    log("打开工作区失败 · " + str(folder or ""), error=exc)
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
            reveal(root)
            # 先弹新手教程（可在教程里永久关闭），关掉后再弹每次启动的使用须知
            welcome = getattr(editor, "show_welcome", None) or getattr(editor, "show_notice", None)
            if welcome:
                # 主窗口先画出来再弹窗，避免和标题栏着色抢焦点
                root.after(320, welcome)
            log("窗口已打开")
            shown = True
            root.mainloop()
    except (OSError, ValueError, sqlite3.Error) as exc:
        log("启动失败", error=exc)
        report(root, app_name, exc)
    except Exception as exc:
        log("启动失败（未预期）", error=exc)
        report(root, app_name, "程序遇到问题，已经停下来了。\n" + (str(exc) or type(exc).__name__))
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass  # Normal window close already destroyed the Tk application.
        log("窗口已关闭" if shown else "未打开窗口，已退出")
