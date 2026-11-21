import json
import math
import sys
import queue
import re
import threading
import time
import tkinter as tk
import webbrowser
from copy import deepcopy
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urlsplit

from PIL import Image, ImageOps, ImageTk

from desktop import log, open_path, start
import appearance
import app_paths
import covers
import generated_images
import widgets
from ui_language import Choice, tr
from settings_ui import SettingsPages, path_row
from api_settings import ApiSettings
from auto_video_ui import AutoVideoPage
from workflow_ui import WorkflowPages
from store import APP_NAME, VIDEO_RATIOS
from widgets import action_row, advanced_options, bind_context_menu, entry_row, scroll_page, selected
import codex_harness
import planning
import production
import providers
import release
import review
import script_timing
import step_models
import task_control
from store import IMAGES, LIBRARIES, SHOT_SLOTS, Store, confirmed, count_metric, eligible, new_project, script_units, shot_clips, shots_from_plan, uid
from widgets import (BG, DARK, DIM, GREEN, INK, LINE, WHITE, box, button, checkbox, dialog, editor, field, frame,
                     glyph, init_style, label, number_badge, pixels, read, table)


PAGES = {"auto_video": "一键生成视频", "topics": "选题收集", "script": "文案定稿", "shots": "分镜编排",
         "assets": "素材库", "audio": "音频制作", "captions": "字幕制作", "review": "样片审看",
         "cover": "封面制作", "output": "成片输出", "settings": "设置"}

TUTORIAL_ENVIRONMENT = (
    "Windows x64 可直接运行 VAT.exe。Windows、macOS 和 Linux 从源码启动时需带 Tk 的 Python 3.11 或以上版本。\n"
    "合成样片和导出成片需要同一套 FFmpeg 和 FFprobe，支持 libx264、AAC 和 ass 字幕滤镜。"
    "将两者加入 PATH，或到「设置 → 文件与工具」指定所在目录。\n"
    "手工剪辑可以先用。需要 AI 写稿、生图或配音时，再按所用功能配置相应的模型或语音服务。"
)

# 配置模型和语音的四步。措辞跟着界面里的实际按钮走。
TUTORIAL_STEPS = (
    ("先导入模型 API",
     "软件不自带模型。打开「设置 → 模型与 API → 直连 API」，\n"
     "填接口地址和密钥，「读取列表」选调用目标，点「保存并启用」；\n"
     "Ollama 和 GGUF 也在高级选项里。"),
    ("不想自己填，就交给 agent",
     "把软件位置发给 Codex、Claude 或 DeepSeek Harness 这类 agent，让它直接读配置替你填好。"),
    ("本机有 Codex，可以直接调用",
     "「调用方式」里选 Codex CLI 或 DeepSeek Harness，用你自己已登录的账号跑，\n"
     "不必另外买 API；软件会自动查找本机命令。"),
    ("本地语音模型，填地址就能用",
     "自己搭了 GPT-SoVITS 或 OpenAI 兼容的语音服务，\n"
     "到「音频制作 → 配音设置」填本机地址和音色（或选音色 JSON），配音直接走本机。"),
)


def notice(text):
    # Diagnostic messages quote filenames, script fragments and project names verbatim.
    parts = re.split(r"(「[^」]*」|《[^》]*》|https?://[^\s]+)", text)
    return "".join(part if i % 2 else tr(part) for i, part in enumerate(parts))


def paint_window_chrome(root):
    """Let the Windows 11 title bar follow the current theme instead of the system accent."""
    if sys.platform != "win32":
        return
    caption = appearance.token("chrome", "") if appearance.token("rounded", False) else ""

    def colorref(value):
        return int(value[5:7] + value[3:5] + value[1:3], 16)

    try:
        import ctypes
        from ctypes import wintypes
        hwnd = wintypes.HWND(ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id())
        dwm = ctypes.windll.dwmapi
        if not caption:
            # 回到系统默认标题栏（切换回简约/经典主题时）
            default = ctypes.c_int(-1)
            for attribute in (35, 34, 36):
                dwm.DwmSetWindowAttribute(hwnd, wintypes.DWORD(attribute), ctypes.byref(default), wintypes.DWORD(4))
            dark = ctypes.c_int(0)
            dwm.DwmSetWindowAttribute(hwnd, wintypes.DWORD(20), ctypes.byref(dark), wintypes.DWORD(4))
            return
        for attribute, value in ((20, 1), (35, colorref(caption)), (34, colorref(caption)),
                                 (36, colorref(appearance.token("chrome_text", "#ffffff")))):
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, wintypes.DWORD(attribute), ctypes.byref(data), wintypes.DWORD(4))
    except Exception:
        # Older Windows builds simply keep the system appearance.
        pass


class Studio(SettingsPages, WorkflowPages, AutoVideoPage):
    def __init__(self, root, store):
        self.root, self.store = root, store
        self.prefs = store.prefs()
        appearance.apply_preferences(self.prefs)
        self.settings_section = "appearance"
        projects = store.projects()
        self.p = next((p for p in projects if p["id"] == self.prefs.get("current")), None)
        self.p = self.p or (projects[0] if projects else new_project("第一期视频"))
        self.store.save(self.p)
        self.page, self.capture, self.last_saved = "", None, ""
        self.cli_model_lists = {}
        self.active_job = None
        self._cancel_job = None
        self._close_after_job = False
        self._closing = False
        self.library, self.topic_filter = "theme", "全部选题"
        self.library_locations = {}
        self.status = tk.StringVar(root, value=tr("项目保存在本机"))
        self.root.report_callback_exception = lambda _t, e, _tb: messagebox.showerror(tr(APP_NAME), notice(str(e)), parent=self.root.grab_current() or self.root)
        root.title(tr(APP_NAME))
        icon = Path(__file__).with_name("studio.ico")
        if sys.platform == "win32" and icon.exists():
            root.iconbitmap(default=str(icon))
        root.configure(bg=appearance.color(BG))
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(pixels(root,1090), sw - 40), min(pixels(root,710), sh - 85)
        root.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2-18)}")
        root.minsize(min(pixels(root,890),w), min(pixels(root,580),h))
        self.build_shell()
        self.go("auto_video")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-s>", lambda _e: self.save_now())
        if root.tk.call("tk", "windowingsystem") == "aqua":
            root.bind("<Command-s>", lambda _e: self.save_now())
        self.timer = root.after(1000, self.autosave)

    def build_shell(self):
        root = self.root
        init_style(root)
        soft = appearance.token("rounded", False)
        modern = self.prefs.get("theme") != "classic"
        self.rail = frame(root, DARK, width=pixels(root, 202 if soft else (194 if modern else 186)))
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)
        self.main = frame(root, BG)
        self.main.pack(side="left", fill="both", expand=True)
        top = frame(self.main, appearance.token("surface", WHITE) if soft else WHITE)
        top.pack(fill="x")
        label(top, "当前项目", 9, DIM).pack(side="left", padx=(28 if soft else 24, 10), pady=(18 if soft else 14))
        self.picker = ttk.Combobox(top, width=24, state="readonly")
        self.picker.pack(side="left", pady=(18 if soft else 0))
        self.picker.bind("<<ComboboxSelected>>", self.switch_project)
        if modern:
            menu = tk.Menu(top, tearoff=False)
            menu.add_command(label=tr("新建一期"), command=self.create_project)
            menu.add_command(label=tr("重命名"), command=self.rename_project)
            manage = ttk.Menubutton(top, text=tr("管理项目"), menu=menu)
            manage.pack(side="left", padx=8, pady=(18 if soft else 0))
        else:
            button(top, "新建一期", self.create_project, "plain").pack(side="left", padx=8)
            button(top, "重命名", self.rename_project, "plain").pack(side="right", padx=16)
        if soft:
            # 顶栏右侧的版本标识：浅色重点底，和侧栏选中态呼应。
            tk.Label(top, text=release.version(), bg=appearance.token("tint"), fg=appearance.token("primary"),
                     font=(widgets.FONT, 9, "bold"), padx=10, pady=3, bd=0).pack(side="right", padx=28, pady=(18, 0))
        frame(self.main, appearance.token("hairline", LINE) if soft else LINE, height=1).pack(fill="x")
        footer = frame(self.main, "#eaf0e8")
        footer.pack(side="bottom", fill="x")
        self.engine_label = label(footer, "", 9, GREEN)
        self.step_model_button = button(footer, "", self.choose_step_model, "plain")
        self.footer_status = label(footer, "", 9, DIM, textvariable=self.status)
        self.footer_status.pack(side="left", fill="x", expand=True, padx=20, pady=(10 if soft else 5))
        self.body = frame(self.main)
        self.body.pack(fill="both", expand=True, padx=28 if soft else 24, pady=(24, 18) if soft else (20, 16))
        self.refresh_projects()
        root.after(30, lambda: paint_window_chrome(root))

    def show_welcome(self):
        """启动引导：先看新手教程（可永久关闭），关掉后再接每次启动的使用须知。"""
        if self.prefs.get("tutorial") == "off":
            self.show_notice()
        else:
            self.show_tutorial(self.show_notice)

    def open_tutorial(self):
        """「设置 → 使用指南」里的入口：随时重新打开教程。"""
        self.show_tutorial(lambda: self.go(self.page))

    def copy_program_location(self):
        """把程序、配置和工作区位置一次复制好，方便直接粘给 agent。"""
        program = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
        lines = (APP_NAME + " " + release.version(),
                 tr("程序") + "：" + str(program.resolve()),
                 tr("配置") + "：" + str(app_paths.config_dir()),
                 tr("工作区") + "：" + str(Path(self.store.root) / "workspace.sqlite3"))
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status.set(tr("软件位置已复制"))

    def show_tutorial(self, on_close=None):
        """先介绍运行环境，再说明如何配置模型和语音。"""
        win = dialog(self.root, "新手教程", 660, 560)
        page = frame(win)
        page.pack(fill="both", expand=True, padx=26, pady=(20, 16))

        head = frame(page)
        head.pack(fill="x")
        label(head, "VAT 新手教程", 17, INK, bold=True).pack(side="left")
        label(head, release.version(), 10, DIM, localize=False).pack(side="right", pady=(7, 0))
        frame(page, appearance.token("hairline", LINE), height=1).pack(fill="x", pady=(10, 12))

        footer = frame(page)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        content = scroll_page(page)
        label(content, "运行环境", 11, INK, bold=True).pack(anchor="w")
        environment = label(content, TUTORIAL_ENVIRONMENT, 9, DIM, justify="left")
        environment.pack(anchor="w", fill="x", pady=(2, 12))
        frame(content, appearance.token("hairline", LINE), height=1).pack(fill="x", pady=(0, 12))
        copied, descriptions = {}, [environment]

        def copy_location():
            self.copy_program_location()
            if copied.get("label") is not None:
                copied["label"].configure(text=tr("已复制，粘给 agent 就行"))

        for index, (title, text) in enumerate(TUTORIAL_STEPS, 1):
            row = frame(content)
            row.pack(fill="x", pady=(0, 10))
            number_badge(row, index, 22).pack(side="left", anchor="n", padx=(0, 10), pady=(1, 0))
            block = frame(row)
            block.pack(side="left", fill="x", expand=True)
            label(block, title, 11, INK, bold=True).pack(anchor="w")
            desc = label(block, text, 9, DIM, justify="left")
            desc.pack(anchor="w", fill="x", pady=(2, 0))
            descriptions.append(desc)
            if index == 2:
                line = frame(block)
                line.pack(anchor="w", pady=(6, 0))
                button(line, "复制软件位置", copy_location).pack(side="left")
                copied["label"] = label(line, "", 9, GREEN)
                copied["label"].pack(side="left", padx=(8, 0))

        # 对话框宽度固定；排版前先把换行宽度定下来，省掉每个标签的 <Configure> 绑定
        win.update_idletasks()
        width = win.winfo_width()
        wrap = max(pixels(win, 240), (width if width > 1 else pixels(win, 660)) - pixels(win, 88))
        for desc in descriptions:
            desc.configure(wraplength=wrap)

        never = tk.BooleanVar(win, value=self.prefs.get("tutorial") == "off")

        def finish():
            # 教程是启动时唯一的操作窗口：这里保存失败也必须能关掉，否则整个软件都动不了。
            try:
                self.store.set_pref("tutorial", "off" if never.get() else "show")
                self.prefs = self.store.prefs()
            except Exception as exc:
                log("保存教程设置失败", error=exc)
            # 销毁排到空闲时：先让控件把排队的布局事件走完，避免 Tk 报无效命令
            win.after_idle(win.destroy)
            if on_close:
                # 使用须知紧接教程出现；也排到空闲时，保证教程已经关掉再抢焦点
                self.root.after_idle(on_close)

        checkbox(footer, "启动时不再显示（「设置 → 使用指南」里还能打开）", never).pack(side="left")
        button(footer, "知道了，开始使用", finish, "primary").pack(side="right")
        win.protocol("WM_DELETE_WINDOW", finish)
        win.bind("<Escape>", lambda _e: finish())
        win.bind("<Return>", lambda _e: finish())
        win.focus_set()
        return win

    def show_notice(self):
        """每次启动显示的使用须知：版本、联系方式与授权说明，配色跟随当前外观。"""
        win = dialog(self.root, "VAT " + release.version(), 680, 368)
        page = frame(win)
        page.pack(fill="both", expand=True, padx=26, pady=(22, 18))

        head = frame(page)
        head.pack(fill="x")
        label(head, "VAT 自动剪辑软件", 17, INK, bold=True).pack(side="left")
        label(head, release.version(), 10, DIM, localize=False).pack(side="right", pady=(7, 0))
        label(page, "由" + release.AUTHOR + "制作并上传 · 永久免费", 9, DIM).pack(anchor="w", pady=(2, 0))
        frame(page, appearance.token("hairline", LINE), height=1).pack(fill="x", pady=(10, 12))

        outer, body = box(page)
        outer.pack(fill="x")
        label(body, "联系方式", 10, DIM).pack(anchor="w", pady=(0, 6))
        for caption, value in release.CONTACTS:
            row = frame(body)
            row.pack(fill="x", pady=2)
            label(row, caption, 10, DIM, width=12).pack(side="left")
            # 微信号与群号按原文显示，不做繁简转换
            label(row, value, 10, INK, bold=True, localize=False).pack(side="left")

        for index, text in enumerate(release.NOTICE):
            # 授权声明比反馈说明重要，用正文色；其余用次级色
            line = label(page, text, 10, INK if index == 0 else DIM, justify="left")
            line.pack(anchor="w", fill="x", pady=(10, 0))
            line.bind("<Configure>", lambda event, w=line: w.configure(wraplength=max(200, event.width - 6)))

        def copy_group():
            self.root.clipboard_clear()
            self.root.clipboard_append(release.CONTACTS[-1][1])
            self.status.set(tr("反馈群号已复制"))

        actions = frame(page)
        actions.pack(side="bottom", fill="x", pady=(12, 0))
        button(actions, "复制反馈群号", copy_group, "secondary").pack(side="left")
        button(actions, "知道了，开始使用", win.destroy, "primary").pack(side="right")
        tip = label(page, "此窗口每次启动显示；联系信息也可在「设置 → 使用指南」查看。", 9, DIM)
        tip.pack(side="bottom", anchor="w", pady=(10, 0))
        win.bind("<Return>", lambda _e: win.destroy())
        win.focus_set()
        return win

    def refresh_projects(self):
        ps = self.store.projects()
        self.project_ids = [p["id"] for p in ps]
        self.picker.configure(values=[p["title"] for p in ps])
        self.picker.current(self.project_ids.index(self.p["id"]))
        self.store.set_pref("current", self.p["id"])

    def switch_project(self, _event=None):
        pid = self.project_ids[self.picker.current()]
        self.save_now()
        self.p = next(p for p in self.store.projects() if p["id"] == pid)
        self.capture = None
        self.store.set_pref("current", pid)
        self.go("topics")

    def create_project(self):
        name = simpledialog.askstring(tr("新建一期"), tr("这期视频叫什么？"), parent=self.root)
        if name and name.strip():
            self.save_now()
            self.capture = None
            self.p = new_project(name.strip())
            self.store.save(self.p)
            self.refresh_projects()
            self.go("topics")

    def rename_project(self):
        name = simpledialog.askstring(tr("重命名"), tr("项目名称"), initialvalue=self.p["title"], parent=self.root)
        if name and name.strip():
            self.p["title"] = name.strip()
            self.save_now()
            self.refresh_projects()

    def save_now(self):
        if self.capture:
            self.capture()
        state = json.dumps(self.p, ensure_ascii=False)
        if state != self.last_saved:
            self.store.save(self.p)
            self.last_saved = state

    def autosave(self):
        try:
            self.save_now()
        finally:
            self.timer = self.root.after(1000, self.autosave)

    def close(self):
        """关窗口就一定要退出去。

        以前任务卡在网络或外部进程上时，这里只发一次中断就返回，窗口和进程都不会关；
        工作区锁还握在手里，下次再打开软件就成了「此工作区已打开」。所以保存和中断
        都不许挡路，任务没停也要在几秒后强制关掉。
        """
        if self._closing:
            return
        self._closing = True
        try:
            self.save_now()
        except Exception as exc:
            log("关闭前保存失败", error=exc)
        if self.active_job is not None:
            try:
                if self._cancel_job:
                    self._cancel_job()
            except Exception as exc:
                log("关闭时中断任务失败", error=exc)
            try:
                self.status.set(tr("正在中断并保存，窗口会自动关闭…"))
                self.root.after(2500, self._force_close)
            except tk.TclError:
                self._force_close()
            return
        self._force_close()

    def _force_close(self):
        """不管前面发生什么，都把窗口销毁——这是唯一能保证退出的动作。"""
        try:
            if self.timer is not None:
                self.root.after_cancel(self.timer)
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def go(self, page):
        self.save_now()
        self.capture = None
        self.page = page
        for w in self.body.winfo_children():
            w.destroy()
        self.draw_nav()
        self.refresh_engine_label()
        getattr(self, "page_" + page)()

    def refresh_engine_label(self):
        if self.page == "auto_video":
            self.step_model_button.pack_forget()
            self.engine_label.pack_forget()
            return
        if self.page == "settings":
            self.step_model_button.pack_forget()
            self.engine_label.configure(text=step_models.engine_name(self.prefs))
            self.engine_label.pack(side="right", before=self.footer_status, padx=20, pady=5)
            return
        self.engine_label.pack_forget()
        explicit = self.prefs.get(step_models.PREF_PREFIX + self.page)
        selection, prefix = explicit, ""
        if not explicit and self.page == "cover":
            # 封面背景按生图出，沿用「一键生成视频」里配置好的 AI 生图通道。
            selection = self.prefs.get(step_models.PREF_PREFIX + "images")
            prefix = tr("沿用 AI 生图 · ")
        try:
            config = step_models.resolve(self.prefs, selection, self.store)
            current = step_models.engine_name(config)
        except ValueError:
            current = tr("API 配置已不可用")
        if len(current) > 35:
            current = current[:34] + "…"
        if not explicit:
            current = (prefix or (tr("跟随全局") + " · ")) + current
        self.step_model_button.configure(text=tr("模型与 Harness") + f"  ·  {current}  ▾")
        self.step_model_button.pack(side="right", before=self.footer_status, padx=8)

    def choose_step_model(self):
        step_models.choose(self, self.page, PAGES[self.page])

    def draw_nav(self):
        for w in self.rail.winfo_children():
            w.destroy()
        theme = appearance.current
        soft = appearance.token("rounded", False)
        modern = self.prefs.get("theme", appearance.DEFAULT_THEME) != "classic"
        brand = frame(self.rail)
        brand.pack(fill="x", padx=18 if soft else 17, pady=(24, 20) if soft else ((20, 18) if modern else (25, 26)))
        glyph(brand, "brand", theme.get("rail_accent", theme["primary"]) if modern else theme["active_text"], theme["rail"], 32).pack(side="left", padx=(0, 8))
        wordmark=frame(brand)
        wordmark.pack(side="left")
        label(wordmark, "VAT", 17, theme["rail_title"], True).pack(anchor="w")
        label(wordmark, "自动剪辑软件", 9, theme["rail_dim"]).pack(anchor="w")

        def preview_folder():
            self.open_folder(review.project_dir(self.store.root, self.p["id"]), create=True)

        def assets_actions(_event):
            assets = self.store.assets(self.p["id"])
            return self.asset_source_actions(assets.values(), self.library)

        folder_commands = {
            "audio": ("打开导出文件夹", self.open_output_folder),
            "captions": ("打开导出文件夹", self.open_output_folder),
            "review": ("打开样片文件夹", preview_folder),
            "cover": ("打开封面文件夹", lambda: self.open_folder(
                covers.project_dir(self.store.root, self.p["id"]), create=True)),
            "output": ("打开导出文件夹", self.open_output_folder),
            "settings": ("打开数据文件夹", lambda: self.open_folder(self.store.root)),
        }

        def nav(parent, key, bottom=False):
            active = self.page == key
            bg = theme["active"] if active else theme["rail"]
            fg = theme["active_text"] if active else theme["rail_text"]
            row = frame(parent, bg, cursor="hand2", takefocus=True, highlightthickness=1,
                        highlightbackground=bg, highlightcolor=theme["primary"])
            row.pack(side="bottom" if bottom else "top", fill="x",
                     padx=12 if soft else 10, pady=(3, 18 if soft else 16) if bottom else ((3, 3) if soft else (2, 2)))
            if modern:
                indicator = frame(row, theme.get("rail_accent", theme["primary"]) if active else bg, width=pixels(row, 3))
                indicator.pack(side="left", fill="y", pady=6 if soft else 4)
                indicator.bind("<Button-1>", lambda _: self.go(key))
            row.bind("<Return>", lambda _: self.go(key))
            row.bind("<space>", lambda _: self.go(key))
            icon = glyph(row, key, fg, bg, 20)
            icon.pack(side="left", padx=(13, 11), pady=9 if soft else (6 if modern else 9))
            txt = label(row, PAGES[key], 11, fg, active)
            txt.pack(side="left")
            for widget in (row, icon, txt):
                widget.bind("<Button-1>", lambda _e, k=key: self.go(k))
            if key == "assets":
                bind_context_menu(row, assets_actions, icon, txt)
            elif key in folder_commands:
                bind_context_menu(row, lambda _event: [folder_commands[key]], icon, txt)
        nav(self.rail, "settings", True)
        items = scroll_page(self.rail)
        sections = (("创作", ("auto_video", "topics", "script", "shots", "assets")),
                    ("制作与交付", ("audio", "captions", "review", "cover", "output"))) if modern else (
                    ("制作流程", tuple(PAGES)[:-1]),)
        for title, keys in sections:
            label(items, title, 9, theme["rail_dim"]).pack(anchor="w", padx=20 if soft else 22,
                                                           pady=(14, 7) if soft else (6, 5))
            for key in keys:
                nav(items, key)

    def header(self, title, subtitle=""):
        soft = appearance.token("rounded", False)
        label(self.body, title, 21 if soft else 22, INK, True).pack(anchor="w", pady=(0, 2) if soft else 0)
        if subtitle:
            label(self.body, subtitle, 10, DIM).pack(anchor="w", pady=(6, 20) if soft else (4, 14))

    def job(self, title, worker, done, parent=None, *, show_elapsed=False, on_progress=None, on_error=None,
            on_cancel=None):
        if self.active_job is not None:
            # 任务窗口已经被关掉却还留在这里时，不能让它挡住所有后续操作。
            try:
                if self.active_job.winfo_exists():
                    self.active_job.lift()
                    return
            except tk.TclError:
                pass
            self.active_job = None
            self._cancel_job = None
        self.save_now()
        parent = parent or self.root
        win = dialog(parent, title, 480 if on_progress else 425, 254 if on_progress else 220)
        control = task_control.JobControl()
        try:
            work = frame(win)
            work.pack(fill="both", expand=True, padx=22, pady=20)
            heading = frame(work)
            heading.pack(fill="x")
            label(heading, title, 14, bold=True).pack(side="left")
            if show_elapsed:
                elapsed = tk.StringVar(win)
                label(heading, "", 10, DIM, textvariable=elapsed).pack(side="right")
                started = time.monotonic()
            status = tk.StringVar(win, value=tr("正在处理…"))
            label(work, "", 9, DIM, textvariable=status, justify="left",
                  wraplength=pixels(win, 425) if on_progress else 0).pack(anchor="w", pady=(6, 11))
            bar = ttk.Progressbar(work, mode="indeterminate")
            bar.pack(fill="x")
            bar.start(12)
            label(work, "已完成的步骤会保留，可稍后继续制作。", 9, DIM).pack(anchor="w", pady=(9, 5))

            def cancel():
                if control.cancelled:
                    return
                control.cancel()
                cancel_button.configure(state="disabled", text=tr("正在中断并保存…"))
                status.set(tr("正在停止当前工作并保存进度…"))

            cancel_button = button(work, "中断并保存", cancel, "secondary")
            cancel_button.pack(anchor="e")
            self.active_job, self._cancel_job = win, cancel
            win.protocol("WM_DELETE_WINDOW", cancel)
            win.bind("<Escape>", lambda _event: cancel())
        except Exception:
            # 建到一半失败也要把窗口收掉，否则抓取会一直留在半成品窗口上。
            try:
                win.destroy()
            except tk.TclError:
                pass
            self.active_job = self._cancel_job = None
            raise
        events = queue.Queue()

        def report(value):
            task_control.checkpoint()
            events.put(("progress", value))

        def run():
            try:
                with task_control.running(control):
                    task_control.checkpoint()
                    value = worker(report)
                events.put(("ok", value))
            except task_control.JobCancelled:
                events.put(("cancelled", None))
            except Exception as e:
                events.put(("error", str(e) or type(e).__name__))
        def poll():
            if show_elapsed:
                minutes, seconds = divmod(int(time.monotonic() - started), 60)
                elapsed.set(tr("已用时 ") + f"{minutes:02}:{seconds:02}")
            while not events.empty():
                kind, value = events.get_nowait()
                if kind == "progress":
                    if not control.cancelled:
                        status.set(notice(on_progress(value) if on_progress else value))
                    continue
                bar.stop()
                self.active_job = None
                self._cancel_job = None
                win.destroy()
                if parent != self.root:
                    try:
                        if parent.winfo_exists():
                            parent.grab_set()
                    except tk.TclError:
                        pass
                closing = self._close_after_job or self._closing
                if kind == "cancelled" and on_cancel:
                    try:
                        on_cancel()
                    except Exception as exc:
                        log("中断回调出错", error=exc)
                if closing:
                    # 窗口正在关：结果已经写进项目，别再弹窗或刷新页面拖住退出。
                    self._close_after_job = False
                    try:
                        self.save_now()
                    except Exception as exc:
                        log("退出前保存失败", error=exc)
                    self._force_close()
                    return
                if kind == "cancelled":
                    self.save_now()
                    self.status.set(tr("已中断，已完成的步骤和项目已保存"))
                elif kind == "error":
                    if on_error:
                        on_error(value)
                    messagebox.showerror(tr(title), notice(value), parent=parent)
                else:
                    done(value)
                return
            if self._close_after_job or self._closing:
                return  # 正在关窗：不再排下一轮，强制关闭的计时会接手
            self.root.after(75, poll)
        threading.Thread(target=run, daemon=True).start()
        poll()

    def current_topic(self):
        return next((t for t in self.p["topics"] if t["id"] == self.p["topic_id"]), None)

    def page_topics(self):
        self.header("选题收集", "把值得讲的 GitHub 项目和抖音 AI 视频放进本期候选。")
        cards = frame(self.body)
        cards.pack(fill="x", pady=(0, 16))
        specs = [("GitHub 新项目", "按创建时间和简介语言筛选", "发现项目", self.find_github, "#e5efe4"),
                 ("抖音 AI 视频", "单条作品点赞量 > 作者粉丝数 × 10", "用 Ego 找视频", self.douyin_window, "#e6edef")]
        for i, (title, desc, cta, fn, tint) in enumerate(specs):
            cards.columnconfigure(i, weight=1, uniform="source")
            outer, inside = box(cards, tint, 16, 13)
            outer.grid(row=0, column=i, sticky="nsew", padx=(0 if not i else 6, 6 if not i else 0))
            label(inside, title, 14, bold=True).pack(anchor="w")
            label(inside, desc, 9, DIM).pack(anchor="w", pady=(5, 11))
            button(inside, cta, fn, "primary" if i == 0 else "secondary").pack(anchor="w")
        row = frame(self.body)
        row.pack(fill="x", pady=(0, 8))
        label(row, "候选题材", 13, bold=True).pack(side="left")
        filt = Choice(row, values=["全部选题", "抖音 · 超十倍"], width=14, state="readonly")
        filt.set(self.topic_filter)
        filt.pack(side="right")
        holder, tv = table(self.body, [("source","来源",72,False),("title","题材",200,True),
            ("metric","筛选指标",170,False),("use","本期状态",80,False)], 5)
        holder.pack(fill="both", expand=True)
        empty = label(holder, "", 10, DIM)
        def populate(*_):
            self.topic_filter = filt.get()
            for j in tv.get_children():
                tv.delete(j)
            for t in reversed(self.p["topics"]):
                dy = t["source"] == "抖音"
                if self.topic_filter.endswith("超十倍") and not eligible(t):
                    continue
                if dy:
                    a, b = t["likes"], t["followers"]
                    ratio = (f"{a/b:.1f} 倍" if b else "粉丝为 0") if a is not None and b is not None else "待核对"
                    metric = ratio + (" · 入选" if eligible(t) else " · 未达标")
                    if a is None or b is None:
                        metric = "缺少指标 · 待核对"
                    elif b and a != b*10 and round(a/b, 1) == 10:
                        metric = ("略超 10 倍 · 入选" if eligible(t) else "略低于 10 倍 · 未达标")
                else:
                    metric = f"{t['stars']:,} 星标" if "stars" in t else "—"
                tv.insert("", "end", iid=t["id"], values=(tr(t["source"]), t["title"], tr(metric),
                          tr("已采用" if t["id"] == self.p["topic_id"] else "候选")))
            if tv.get_children():
                empty.place_forget()
            else:
                empty.config(text=tr("本期还没有候选题材" if self.topic_filter=="全部选题" else "尚无符合十倍条件的作品"))
                empty.place(relx=.5, rely=.55, anchor="center")
            if self.p["topic_id"] in tv.get_children():
                tv.selection_set(self.p["topic_id"])
        populate()
        filt.bind("<<ComboboxSelected>>", populate)
        def chosen():
            tid = selected(tv, "请先选择一条候选题材。")
            return next(t for t in self.p["topics"] if t["id"] == tid)
        def use_topic():
            t = chosen()
            if t["source"] == "抖音" and not eligible(t):
                raise ValueError("这条作品的点赞量尚未超过粉丝数的十倍。")
            if self.p["topic_id"] != t["id"] and (self.p["draft"].strip() or self.p["shots"]):
                if not messagebox.askyesno(tr("另开一期"), tr("当前项目已有文案。用所选题材另开一期？"), parent=self.root):
                    return
                self.p = new_project(t["title"])
                self.p["topics"] = [deepcopy(t)]
                self.p["topic_id"] = t["id"]
                self.store.save(self.p)
                self.refresh_projects()
            else:
                self.p["topic_id"] = t["id"]
            self.go("script")
        tv.bind("<Double-1>", lambda _e: self.topic_dialog(chosen()))
        bar = action_row(self.body)
        button(bar, "手动添加", self.topic_dialog).pack(side="left")
        button(bar, "编辑资料", lambda: self.topic_dialog(chosen()), "plain").pack(side="left", padx=4)
        button(bar, "采用并写文案 →", use_topic, "primary").pack(side="right")

    def topic_dialog(self, existing=None, source="抖音"):
        item = existing or {}
        win = dialog(self.root, "选题资料", 630, 485)
        body = frame(win)
        body.pack(fill="both", expand=True, padx=18, pady=12)
        typ = tk.StringVar(win, value=item.get("source", source))
        row = frame(body)
        row.pack(fill="x")
        label(row, "来源", color=DIM, width=12).pack(side="left")
        Choice(row, values=["抖音", "GitHub", "其他"], state="readonly", textvariable=typ, width=15).pack(side="left")
        title = entry_row(body, "题材名称", item.get("title", ""))
        url = entry_row(body, "来源链接", item.get("url", ""))
        nums = frame(body)
        nums.pack(fill="x")
        vals = [tk.StringVar(win, str(item[k]) if item.get(k) is not None else "") for k in ("likes", "followers")]
        fields = []
        for name, v in zip(("作品点赞", "作者粉丝"), vals):
            label(nums, name, 9, DIM).pack(side="left", padx=(0, 6))
            e = field(nums, v)
            e.configure(width=12)
            e.pack(side="left", padx=(0, 12), pady=5)
            fields.append(e)
        def toggle(*_):
            for w in fields:
                w.configure(state="normal" if typ.get()=="抖音" else "disabled")
        typ.trace_add("write", toggle)
        toggle()
        label(body, "原文案或项目资料", 10, bold=True).pack(anchor="w", pady=(10, 5))
        notes = editor(body, item.get("notes", ""), 5)
        notes.pack(fill="both", expand=True)
        def save():
            if not title.get().strip():
                raise ValueError("请填写题材名称。")
            link = url.get().strip()
            if link:
                match = re.search(r"https?://[^\s，。<>]+", link)
                if not match or not urlsplit(match[0]).hostname:
                    raise ValueError("请填写可打开的网址或带链接的分享文本。")
                link = match[0].rstrip(".,;)")
            updated = {**item, "id": item.get("id") or uid(), "source": typ.get(), "title": title.get().strip(),
                       "url": link, "notes": read(notes).strip()}
            if typ.get() == "抖音":
                updated["likes"], updated["followers"] = [count_metric(v.get()) if v.get().strip() else None for v in vals]
                if updated["id"] == self.p["topic_id"] and not eligible(updated):
                    raise ValueError("本期已采用这条题材，点赞和粉丝数需符合十倍规则。")
            if existing:
                self.p["topics"][self.p["topics"].index(existing)] = updated
            else:
                self.p["topics"].append(updated)
            win.destroy()
            self.go(self.page)
        bar = action_row(body)
        button(bar, "保存资料", save, "primary").pack(side="right")
        if item.get("url"):
            button(bar, "打开来源", lambda: webbrowser.open(item["url"]), "plain").pack(side="left")

    def find_github(self):
        win = dialog(self.root, "发现 GitHub 项目", 960, 595)
        win.minsize(min(pixels(win, 720), win.winfo_screenwidth() - 36),
                    min(pixels(win, 475), win.winfo_screenheight() - 82))
        body = frame(win)
        body.pack(fill="both", expand=True, padx=18, pady=16)
        label(body, "发现 GitHub 项目", 17, bold=True).pack(anchor="w")
        row = frame(body)
        row.pack(fill="x", pady=(8, 0))
        label(row, "关键词", color=DIM).pack(side="left", padx=(0, 8))
        keyword = tk.StringVar(win, value="topic:artificial-intelligence")
        keyword_entry = field(row, keyword)
        keyword_entry.pack(side="left", fill="x", expand=True)
        search_button = button(row, "搜索", lambda: search(), "primary")
        search_button.pack(side="right", padx=(8, 0))

        filters = frame(body)
        filters.pack(fill="x", pady=(8, 0))
        presets = {"近 7 天": 7, "近 30 天": 30, "近 90 天": 90, "近 180 天": 180, "近 365 天": 365, "不限": None}
        custom = "自定义日期"
        label(filters, "创建时间", color=DIM).pack(side="left", padx=(0, 8))
        period = Choice(filters, values=[*presets, custom], state="readonly", width=13)
        period.set("近 30 天")
        period.pack(side="left")
        languages = {"不限": "", "中文": "zh", "英语": "en", "日语": "ja", "韩语": "ko",
                     "法语": "fr", "德语": "de", "西班牙语": "es", "俄语": "ru"}
        label(filters, "简介语言", color=DIM).pack(side="left", padx=(20, 8))
        language = Choice(filters, values=list(languages), state="readonly", width=12)
        language.set("不限")
        language.pack(side="left")
        label(filters, "根据简介自动识别", 9, DIM).pack(side="left", padx=8)

        dates = frame(body)
        label(dates, "从", color=DIM).pack(side="left", padx=(0, 8))
        start_entry = field(dates)
        start_entry.configure(width=13)
        start_entry.pack(side="left")
        label(dates, "至", color=DIM).pack(side="left", padx=8)
        end_entry = field(dates)
        end_entry.configure(width=13)
        end_entry.pack(side="left")
        label(dates, "YYYY-MM-DD，可留空一端", 9, DIM).pack(side="left", padx=8)

        def show_dates(_event):
            if period.get() == custom:
                dates.pack(fill="x", pady=(8, 0), after=filters)
            else:
                dates.pack_forget()

        period.bind("<<ComboboxSelected>>", show_dates)
        result_bar = frame(body)
        result_bar.pack(fill="x", pady=(9, 4))
        summary = tk.StringVar(win, value=tr("尚未加载项目"))
        label(result_bar, "", 9, DIM, textvariable=summary).pack(side="left")
        more_button = button(result_bar, "加载更多", lambda: search(more=True), "plain")
        more_button.configure(state="disabled")
        more_button.pack(side="right")
        cancel_button = button(result_bar, "取消搜索", lambda: finish_search("已取消搜索"), "plain")
        holder, tv = table(body, [("title", "项目", 248, False), ("stars", "星标", 74, False),
                                 ("date", "创建日期", 112, False), ("description", "简介", 380, True)], height=3)
        holder.pack(fill="both", expand=True, pady=(0, 8))
        label(body, "完整简介", 10, bold=True).pack(anchor="w", pady=(0, 4))
        preview_box = frame(body)
        preview_box.pack(fill="x")
        preview = editor(preview_box, height=3)
        preview.configure(state="disabled")
        scrollbar = ttk.Scrollbar(preview_box, orient="vertical", command=preview.yview)
        preview.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        preview.pack(side="left", fill="x", expand=True)

        items = {}
        last_query, last_page = "", 0
        has_more = incomplete = False
        poll_id = None

        def show_intro(_event=None):
            selection = tv.selection()
            project = items.get(selection[0]) if selection else None
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            if project:
                preview.insert("1.0", project["notes"] or tr("暂无简介"))
            preview.configure(state="disabled")
            add_button.configure(state="normal" if project else "disabled")

        def render(_event=None):
            selection = tv.selection()
            code = languages[language.get()]
            tv.delete(*tv.get_children())
            for project in sorted(items.values(), key=lambda item: item["stars"], reverse=True):
                if not code or project["description_language"] == code:
                    intro = " ".join(project["notes"].split()) or tr("暂无简介")
                    tv.insert("", "end", iid=project["url"], values=(project["title"], f"{project['stars']:,}",
                                                                    project["date"], intro))
            rows = tv.get_children()
            if rows:
                chosen = selection[0] if selection and tv.exists(selection[0]) else rows[0]
                tv.selection_set(chosen)
                tv.focus(chosen)
                tv.see(chosen)
            status = f"{len(items)} 个项目 · 按星标排序"
            if not items and last_page:
                status = "没有找到符合关键词和时间范围的项目"
            elif items and not rows:
                status = f"已加载 {len(items)} 个项目，暂无匹配的简介语言"
            elif code:
                status = f"显示 {len(rows)} 个项目 · 已加载 {len(items)} 个"
            if incomplete:
                status += " · GitHub 返回了部分结果"
            if poll_id is None:
                summary.set(tr(status))
            show_intro()

        def finish_search(status=None):
            nonlocal poll_id
            if poll_id is not None:
                win.after_cancel(poll_id)
                poll_id = None
            search_button.configure(state="normal")
            more_button.configure(state="normal" if has_more else "disabled")
            cancel_button.pack_forget()
            if status is not None:
                summary.set(tr(status))

        def close():
            finish_search()
            win.destroy()

        def search(more=False):
            nonlocal poll_id
            if poll_id is not None:
                return
            choice = period.get()
            if choice == custom:
                start, end = start_entry.get().strip(), end_entry.get().strip()
                if not (start or end):
                    raise ValueError("请填写起始日期或结束日期。")
                query = providers.github_search_query(keyword.get(), start=start, end=end)
            else:
                query = providers.github_search_query(keyword.get(), days=presets[choice])
            page = last_page + 1 if more and query == last_query else 1

            results = queue.Queue()
            deadline = time.monotonic() + providers.GITHUB_REQUEST_TIMEOUT + 6

            def fetch():
                try:
                    results.put((providers.github_topics(query, page), None))
                except Exception as error:
                    results.put((None, str(error) or type(error).__name__))

            def poll():
                nonlocal poll_id, last_query, last_page, has_more, incomplete
                poll_id = None
                try:
                    result, error = results.get_nowait()
                except queue.Empty:
                    if time.monotonic() < deadline:
                        poll_id = win.after(100, poll)
                    else:
                        finish_search("搜索超时，请检查网络后重试。")
                    return
                if error is not None:
                    finish_search("搜索失败，可以重新搜索。")
                    messagebox.showerror(tr("搜索 GitHub"), notice(error), parent=win)
                    return
                projects, has_more, partial = result
                if page == 1:
                    items.clear()
                    incomplete = False
                items.update((project["url"], project) for project in projects)
                last_query, last_page = query, page
                incomplete = incomplete or partial
                finish_search()
                render()

            threading.Thread(target=fetch, daemon=True).start()
            summary.set(tr("正在搜索 GitHub…"))
            search_button.configure(state="disabled")
            more_button.configure(state="disabled")
            cancel_button.pack(side="right", padx=(0, 8))
            poll_id = win.after(100, poll)

        def add():
            t = items[selected(tv, "请先选择项目。")].copy()
            if any(a["url"] == t["url"] for a in self.p["topics"]):
                raise ValueError("本期候选中已包含这个项目。")
            t.pop("description_language")
            self.p["topics"].append({**t, "id": uid(), "source": "GitHub"})
            self.topic_filter = "全部选题"
            close()
            self.go("topics")

        tv.bind("<<TreeviewSelect>>", show_intro)
        language.bind("<<ComboboxSelected>>", render)
        for entry in (keyword_entry, start_entry, end_entry):
            entry.bind("<Return>", lambda _e: search())
        add_button = button(action_row(body), "加入本期候选", add, "primary")
        add_button.configure(state="disabled")
        add_button.pack(side="right")
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())
        search()

    def duration_controls(self, topic, advice):
        panel, content = box(self.body, WHITE, 13, 8)
        panel.pack(fill="x", pady=(0, 10))
        controls = frame(content)
        controls.pack(fill="x")
        label(controls, "目标视频时长", 10, bold=True).pack(side="left", padx=(0, 9))
        auto, custom = "跟随题材建议", "自定义秒数…"
        presets = {script_timing.display_time(n): n for n in script_timing.PRESETS}
        picker = Choice(controls, state="readonly", width=16)
        picker.pack(side="left")

        def sync():
            manual = self.p.get("target_seconds")
            if manual:
                presets[script_timing.display_time(manual)] = manual
            picker.configure(values=[auto, *sorted(presets, key=presets.get), custom])
            picker.set(script_timing.display_time(manual) if manual else auto)

        def choose(_event):
            choice = picker.get()
            if choice == custom:
                seconds = simpledialog.askinteger(tr("自定义视频时长"), tr("整期视频的目标秒数"),
                           initialvalue=script_timing.target(self.p, advice) or script_timing.DEFAULT_SECONDS,
                           minvalue=1, parent=self.root)
                if seconds is None:
                    sync()
                    return
            else:
                seconds = None if choice == auto else presets[choice]
            self.p["target_seconds"] = seconds
            sync()
            self.save_now()

        sync()
        picker.bind("<<ComboboxSelected>>", choose)
        if advice:
            prefix = "AI 建议" if "topic_key" in advice else "题材建议"
            label(controls, prefix+"  "+script_timing.display_time(advice["seconds"]), 10, GREEN, True).pack(side="left", padx=12)
        def assess():
            self.save_now()
            prefs, key = self.engine("script")
            def done(result):
                self.p["duration_advice"] = result
                self.go("script")
                self.status.set(tr("已更新当前题材的时长建议"))
            self.job("分析题材时长", lambda _: providers.duration_advice(prefs, key, topic), done)
        if topic:
            button(controls, "AI 细评时长", assess, "plain").pack(side="right")
        reason = advice["reason"] if advice else "采用题材后会显示建议时长，也可以直接指定秒数。"
        why = label(content, reason, 9, DIM, localize=not (advice and "topic_key" in advice), justify="left")
        why.pack(fill="x", pady=(3, 0))
        why.bind("<Configure>", lambda e: why.configure(wraplength=max(160, e.width - 4)))

    def page_script(self):
        topic = self.current_topic()
        advice = script_timing.recommend(self.p, topic)
        self.header("文案定稿", "先改写并审定文案，分镜和配音共用这份定稿。")
        card, inside = box(self.body, "#e6eee3", 14, 10)
        card.pack(fill="x", pady=(0, 12))
        label(inside, "本期题材", 9, DIM).pack(side="left", padx=(0, 12))
        label(inside, topic["title"] if topic else "尚未选择，也可以直接写稿", 11, bold=True, localize=not topic).pack(side="left", fill="x", expand=True)
        button(inside, "编辑资料" if topic else "去选题",
               (lambda: self.topic_dialog(topic)) if topic else (lambda: self.go("topics")), "plain").pack(side="right")
        self.duration_controls(topic, advice)
        cols = frame(self.body)
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=3, uniform="script")
        cols.columnconfigure(1, weight=2, uniform="script")
        cols.rowconfigure(0, weight=1)
        left, li = box(cols)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right, ri = box(cols)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        title = frame(li)
        title.pack(fill="x", pady=(0, 8))
        label(title, "口播文案", 13, bold=True).pack(side="left")
        state = label(title, "", 9, GREEN)
        state.pack(side="right")
        draft = editor(li, self.p["draft"], 8)
        estimate = label(li, "", 9, DIM)
        estimate.pack(side="bottom", fill="x", pady=(5, 0))
        draft.pack(fill="both", expand=True)
        label(ri, "我的文案风格", 13, bold=True).pack(anchor="w")
        label(ri, "写表达习惯，或贴一段你自己的样稿。", 9, DIM, wraplength=265, justify="left").pack(fill="x", pady=(5, 7))
        style = editor(ri, self.prefs["style"], 4)
        style.pack(fill="both", expand=True)
        label(ri, "选题资料", 10, bold=True).pack(anchor="w", pady=(13, 3))
        sample = (topic or {}).get("notes") or tr("在选题资料里粘贴原文案或项目简介。")
        label(ri, sample[:105] + ("…" if len(sample)>105 else ""), 9, DIM, localize=False, wraplength=260,
              justify="left").pack(fill="x")
        def capture():
            self.p["draft"] = read(draft)
            seconds = script_timing.estimated_speech(self.p["draft"])
            target = script_timing.target(self.p, advice)
            timing = "预计口播约 " + script_timing.display_time(seconds) if seconds else "写入文案后估算口播时长"
            if target:
                timing += "  /  目标 " + script_timing.display_time(target)
                if seconds > target:
                    timing += "（超约 "+script_timing.display_time(seconds-target)+"）"
            estimate.configure(text=tr(timing))
            value = read(style)
            if value != self.prefs["style"]:
                self.prefs["style"] = value
                self.store.set_pref("style", value)
            state.config(text=tr("已定稿" if confirmed(self.p) else "待确认"))
        self.capture = capture
        capture()
        def gen():
            self.save_now()
            current_draft = self.p["draft"].strip()
            source = topic or {"title":self.p["title"], "notes":""}
            if not source.get("notes", "").strip() and not current_draft:
                raise ValueError("请先补充选题资料，或写入需要调整的草稿。")
            if self.p["draft"].strip() and not messagebox.askyesno(tr("重新改写"), tr("用新的改写结果替换当前草稿？"), parent=self.root):
                return
            prefs, key = self.engine("script")
            def got(value):
                draft.delete("1.0", "end")
                draft.insert("1.0", value)
                self.save_now()
                self.status.set(tr("文案已生成，可编辑并确认"))
            target_seconds = script_timing.target(self.p, advice)
            self.job("改写文案", lambda _: providers.rewrite(prefs, key, source, target_seconds, current_draft), got)
        def approve():
            self.save_now()
            current = self.p["draft"].strip()
            if not current or not script_units(current):
                raise ValueError("请先写入文案。")
            if current != self.p["approved"] and self.p["shots"]:
                if not messagebox.askyesno(tr("更新定稿"), tr("定稿变化后需要重新编排本期分镜。确认更新？"), parent=self.root):
                    return
                self.p["shots"] = []
            if current != self.p["approved"]:
                self.p["sections"] = []
                self.p["captions"]["edits"] = {}
            self.p["approved"] = current
            self.status.set(tr("文案已定稿，可以生成配音和分镜"))
            self.go("script")
        bar = action_row(self.body)
        button(bar, "AI 改写", gen).pack(side="left")
        button(bar, "确认定稿", approve, "primary").pack(side="left", padx=6)
        button(bar, "导出文案配音", self.voice).pack(side="left")
        button(bar, "配音设置", self.audio_profile_dialog, "plain").pack(side="left")
        button(bar, "去分镜 →", lambda: self.go("shots"), "plain").pack(side="right")

    def engine(self, page, *, selection=None):
        if selection is None:
            selection = self.prefs.get(step_models.PREF_PREFIX + page)
        p = step_models.resolve(self.prefs, selection, self.store)
        if not p["engine"]:
            raise ValueError(providers.MODEL_SETUP_REQUIRED)
        key = self.store.profile_key(p["api_profile"], p["base"], p["protocol"]) if p["engine"] == "API" else ""
        return p, key

    def exported(self, values):
        for kind, path in values:
            self.p["exports"].append({"kind": kind, "path": path})
        self.store.save(self.p)
        self.status.set(tr("已导出 · " + "、".join(k for k, _ in values)))
        self.go(self.page)
        messagebox.showinfo(tr("导出完成"), "\n".join(p for _, p in values), parent=self.root)

    def voice(self):
        self.save_now()
        if not confirmed(self.p):
            raise ValueError("请先确认当前文案。")
        p = deepcopy(self.p)
        voice_profile = self.current_voice_profile()
        self.job("生成定稿配音",
                 lambda report: production.export_voice(self.store.root, p, report, voice_profile=voice_profile),
                 self.exported)

    def shot_dialog(self, i):
        s = self.p["shots"][i]
        win = dialog(self.root, f"编辑镜头 {i+1:02d}", 850, 620)
        body = frame(win)
        body.pack(fill="both", expand=True, padx=17, pady=13)
        actions = frame(body)
        actions.pack(side="bottom", fill="x", pady=(9, 0))
        top = frame(body)
        top.pack(fill="x")
        label(top, "镜头类型", 9, DIM).pack(side="left", padx=(0, 10))
        kind = Choice(top, values=("素材画面", "真人出镜"), state="readonly", width=12)
        kind.current(1 if s["kind"] == "talk" else 0)
        kind.pack(side="left")
        units = script_units(self.p["approved"])
        tx = editor(body, "".join(units[s["start"]:s["end"]]), 2)
        tx.configure(state="disabled", bg="#eaf0e7")
        tx.pack(fill="x", pady=(5, 6))
        label(body, "画面说明", bold=True).pack(anchor="w")
        note = editor(body, s["visual"], 2)
        note.pack(fill="x", pady=(3, 6))
        label(body, "按 Ctrl 或 Shift 多选。各栏按顺序均分镜头时长，口播画面按取用时长拼接。",
              9, DIM).pack(anchor="w", pady=(0, 5))
        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)
        assets = self.store.assets(self.p["id"])
        draft = {slot: [clip.copy() for clip in shot_clips(s, slot)] for slot in SHOT_SLOTS}
        main_by_kind = {"voice": [], "talk": []}
        main_by_kind[s["kind"]] = draft["main"]

        def shot_kind():
            return "talk" if kind.current() == 1 else "voice"

        def picker(slot, title):
            pane = frame(tabs, BG)
            tabs.add(pane, text=tr(title))
            left, right = frame(pane), frame(pane)
            for col, side in enumerate((left, right)):
                pane.columnconfigure(col, weight=1, uniform="materials")
                side.grid(row=0, column=col, sticky="nsew", padx=7, pady=(7, 2))
            pane.rowconfigure(0, weight=1)
            left_holder, available = table(left, [("name", "可选素材", 160, True),
                                                   ("folder", "源文件夹", 100, True)], 3)
            right_holder, ordered = table(right, [("name", "使用顺序", 160, True),
                                                  ("folder", "源文件夹", 100, True)], 3)
            available.configure(selectmode="extended")
            left_actions, right_actions = frame(left), frame(right)
            for holder, bar in ((left_holder, left_actions), (right_holder, right_actions)):
                bar.pack(side="bottom", fill="x", pady=(5, 0))
                holder.pack(fill="both", expand=True)

            main = slot == "main"
            active_clip = None
            trim = {}
            rect_fields = []
            if main:
                timing = frame(pane)
                timing.grid(row=1, column=0, columnspan=2, sticky="ew", padx=7, pady=(3, 5))
                for key, caption in (("offset", "所选素材入点（秒）"), ("length", "口播取用时长（秒，0 到片尾）")):
                    row = frame(timing)
                    var = tk.StringVar(win)
                    label(row, caption, 9, DIM).pack(side="left", padx=(0, 5))
                    entry = field(row, var)
                    entry.configure(width=7)
                    entry.pack(side="left", padx=(0, 14))
                    trim[key] = row, var, entry
                trim["offset"][0].pack(side="left")
                layout = frame(pane)
                layout.grid(row=2, column=0, columnspan=2, sticky="ew", padx=7, pady=(0, 5))
                label(layout, "主体画框（画布比例 0–1）", 9, DIM).pack(side="left", padx=(0, 8))
                for caption in ("左", "上", "宽", "高"):
                    label(layout, caption, 9, DIM).pack(side="left", padx=(0, 4))
                    var = tk.StringVar(win)
                    entry = field(layout, var)
                    entry.configure(width=8)
                    entry.pack(side="left", padx=(0, 9))
                    rect_fields.append((var, entry))

            def clips():
                return main_by_kind[shot_kind()] if main else draft[slot]

            def capture_trim():
                if active_clip is not None:
                    active_clip.update({key: var.get() for key, (_, var, _) in trim.items()})
                    active_clip["rect"] = [var.get() for var, _ in rect_fields]

            def update_selection(_event=None):
                nonlocal active_clip
                choice = ordered.selection()
                index = ordered.index(choice[0]) if choice else -1
                for control, enabled in ((up_btn, index > 0), (down_btn, 0 <= index < len(clips()) - 1),
                                         (remove_btn, index >= 0)):
                    control.configure(state="normal" if enabled else "disabled")
                if main:
                    capture_trim()
                    active_clip = clips()[index] if index >= 0 else None
                    length_row = trim["length"][0]
                    if shot_kind() == "talk":
                        length_row.pack(side="left")
                    else:
                        length_row.pack_forget()
                    for key, (_, var, entry) in trim.items():
                        var.set(str(active_clip.get(key, 0)) if active_clip is not None else "")
                        entry.configure(state="normal" if active_clip is not None else "disabled")
                    rect = active_clip.get("rect") if active_clip is not None else None
                    for value, (var, entry) in zip(rect or (0, 0, 1, 1), rect_fields):
                        var.set(str(value) if active_clip is not None else "")
                        entry.configure(state="normal" if active_clip is not None else "disabled")

            def refresh(keep=None):
                categories = planning.slot_categories({"kind": shot_kind()}, slot)
                used = {clip["id"] for clip in clips()}
                available.delete(*available.get_children())
                ordered.delete(*ordered.get_children())
                for aid, asset in assets.items():
                    if aid not in used and asset["enabled"] and asset["category"] in categories:
                        available.insert("", "end", iid=aid,
                                         values=(asset["name"], Path(asset["path"]).parent.name))
                for clip in clips():
                    aid = clip["id"]
                    asset = assets.get(aid)
                    name = asset["name"] if asset else "找不到的素材"
                    if asset and not asset["enabled"]:
                        name += "（已禁用）"
                    folder = Path(asset["path"]).parent.name if asset else ""
                    ordered.insert("", "end", iid=aid, values=(name, folder))
                if used:
                    target = keep if keep in used else ordered.get_children()[0]
                    ordered.selection_set(target)
                    ordered.focus(target)
                    ordered.see(target)
                add_btn.configure(state="disabled")
                update_selection()

            def add(ids):
                picked = set(ids)
                additions = [aid for aid in available.get_children() if aid in picked]
                if additions:
                    clips().extend({"id": aid} for aid in additions)
                    refresh(additions[0])

            def remove():
                if choice := ordered.selection():
                    clips().pop(ordered.index(choice[0]))
                    refresh()

            def move(step):
                aid = ordered.selection()[0]
                index = ordered.index(aid)
                target = index + step
                if 0 <= target < len(clips()):
                    clips().insert(target, clips().pop(index))
                    ordered.move(aid, "", target)
                    ordered.see(aid)
                    update_selection()

            add_btn = button(left_actions, "添加所选 →", lambda: add(available.selection()), "primary")
            add_btn.pack(side="left")
            up_btn = button(right_actions, "上移", lambda: move(-1))
            down_btn = button(right_actions, "下移", lambda: move(1))
            remove_btn = button(right_actions, "移除", remove, "plain")
            for control in (up_btn, down_btn, remove_btn):
                control.pack(side="left", padx=(0, 4))
            available.bind("<<TreeviewSelect>>", lambda _e: add_btn.configure(
                state="normal" if available.selection() else "disabled"))
            available.bind("<Double-1>", lambda e: add((aid,)) if (aid := available.identify_row(e.y)) else None)
            ordered.bind("<<TreeviewSelect>>", update_selection)
            refresh()
            return refresh, capture_trim

        refresh_main, capture_trim = picker("main", SHOT_SLOTS["main"])
        for slot in ("theme", "effect"):
            picker(slot, SHOT_SLOTS[slot])
        kind.bind("<<ComboboxSelected>>", lambda _: refresh_main())

        art = dict(s.get("art") or {})
        if art:
            from creative_art import Artwork
            from generated_images import file_path
            art_page = frame(tabs, BG, padx=10, pady=8)
            tabs.add(art_page, text=tr("原创画面"))
            if art.get("image_file"):
                path = file_path(self.store.root, self.p["id"], art["image_file"])
                button(art_page, "查看生成图片", lambda: self.open_file(path)).pack(anchor="w")
                label(art_page, art.get("image_prompt", ""), 9, DIM, localize=False,
                      wraplength=690, justify="left").pack(anchor="w", pady=4)
            line = frame(art_page)
            line.pack(fill="x", pady=4)
            label(line, "SVG 图形与动效").pack(side="left")
            placement = Choice(line, values=("主体画面", "透明叠层"), state="readonly", width=10)
            position = art.get("layer", "effect" if art.get("image_file") else "main")
            placement.current(1 if position == "effect" else 0)
            placement.pack(side="right")
            svg_editor = editor(art_page, art.get("svg", ""), 4)
            svg_editor.pack(fill="both", expand=True)

        def save():
            capture_trim()
            edited_art = art.copy()
            if art:
                svg = read(svg_editor).strip()
                if svg:
                    Artwork(svg)
                    edited_art.update(svg=svg, layer="effect" if placement.current() == 1 else "main")
                else:
                    edited_art.pop("svg", None)
                    edited_art.pop("layer", None)
            new_kind = shot_kind()
            main = []
            for clip in main_by_kind[new_kind]:
                try:
                    offset = float(clip.get("offset") or 0)
                    length = float(clip.get("length") or 0) if new_kind == "talk" else 0
                except ValueError:
                    raise ValueError("素材入点和时长请填写秒数。") from None
                if any(not math.isfinite(value) or value < 0 for value in (offset, length)):
                    raise ValueError("素材入点和时长需为非负有限数。")
                saved_clip = dict(clip, offset=offset, length=length)
                rect = production.normalized_rect(saved_clip.pop("rect", None))
                if rect is not None and rect != (0, 0, 1, 1):
                    saved_clip["rect"] = list(rect)
                main.append(saved_clip)
            if new_kind != s["kind"] and s.get("voice_source") in ("camera", "synth", None):
                s.pop("voice_source", None)
            draft["main"] = main
            s.update(draft, kind=new_kind, visual=read(note).strip())
            if art:
                s["art"] = edited_art
            s.pop("offset", None)
            s.pop("length", None)
            win.destroy()
            self.go("shots")
        button(actions, "保存镜头", save, "primary").pack(side="right")

    def open_file(self, path):
        if not Path(path).exists():
            raise ValueError("找不到这个文件，可能已经移动。")
        open_path(path)

    def open_folder(self, path, *, create=False):
        folder = Path(path).absolute()
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        if not folder.is_dir():
            raise ValueError(f"找不到文件夹「{folder}」，可能已经移动。")
        open_path(folder)

    def open_output_folder(self):
        self.open_folder(production.output_dir(self.store.root, self.p))

    def video_format_controls(self, pane, short_edge, *, captions=False):
        row = frame(pane)
        row.pack(fill="x")
        label(row, "画幅", color=DIM).pack(side="left", padx=(0, 8))
        orientation = Choice(row, values=tuple(VIDEO_RATIOS), state="readonly", width=12)
        orientation.set(self.p["orientation"])
        orientation.pack(side="left")
        label(row, f"{short_edge}p  /  {production.FPS} fps", 9, DIM).pack(side="left", padx=10)
        label(row, f"{len(self.p['shots'])} 个镜头", 10, GREEN, True).pack(side="right")
        self.capture = lambda: self.p.update(orientation=orientation.get())
        if captions:
            options = frame(pane)
            options.pack(fill="x", pady=(8, 0))
            label(options, "成片字幕", color=DIM).pack(side="left", padx=(0, 8))
            mode = Choice(options, values=production.CAPTION_MODES, state="readonly", width=18)
            mode.set(self.p["captions"]["mode"])
            mode.pack(side="left")
            def capture():
                self.p["orientation"] = orientation.get()
                self.p["captions"]["mode"] = mode.get()
            self.capture = capture

    def page_review(self):
        self.header("样片审看", "合成全长样片并用本机播放器观看。每一版都保存对应的分镜、素材和实际配音。")
        outer, pane = box(self.body)
        outer.pack(fill="x", pady=(0, 12))
        self.video_format_controls(pane, 360)
        actions = action_row(pane)

        def create():
            self.save_now()
            project, assets = deepcopy(self.p), self.store.assets(self.p["id"])
            profile = self.current_voice_profile()

            def ready(record):
                self.p["previews"].append(record)
                self.go("review")
                self.status.set(tr("样片已保存，可重看或导出这一版的 AI 制作包"))
                self.open_file(review.version_dir(self.store.root, project["id"], record["version"]) / review.SAMPLE)

            self.job("合成样片", lambda report: review.create_preview(self.store.root, project, assets, report,
                                                                  voice_profile=profile), ready)

        make = button(actions, "合成并观看", create, "primary")
        make.pack(side="left")
        if confirmed(self.p) and self.p["shots"]:
            hint = "样片显示校对字幕，画幅与成片共用。"
        else:
            make.configure(state="disabled")
            hint = "确认文案并保存分镜后即可合成。"
        label(actions, hint, 9, DIM).pack(side="left", padx=10)

        records = self.p["previews"]
        label(self.body, "本期样片" if records else "本期尚未合成样片", 13, bold=True).pack(anchor="w", pady=(0, 6))
        holder, tv = table(self.body, [("date", "合成时间", 155, True), ("size", "画幅", 94, False),
                                       ("shots", "镜头数", 70, False), ("duration", "全长", 86, False)], 4)
        holder.pack(fill="both", expand=True)
        for i in reversed(range(len(records))):
            r = records[i]
            tv.insert("", "end", iid=str(i), values=(r["created_at"], tr(r["orientation"]), r["shots"],
                                                     f"{r['duration_ms'] / 1000:.2f} 秒"))

        def sample_folder(row):
            return review.version_dir(self.store.root, self.p["id"], records[int(row)]["version"])

        def selected_folder():
            return sample_folder(selected(tv, "请先选择一版样片。"))

        def sample_actions(event):
            row = tv.identify_row(event.y)
            return [("打开所在文件夹", lambda: self.open_folder(sample_folder(row)))] if row else []

        bind_context_menu(tv, sample_actions)

        def play():
            self.open_file(selected_folder() / review.SAMPLE)

        def package():
            folder = selected_folder()
            path = filedialog.asksaveasfilename(parent=self.root, title=tr("保存 AI 制作包"), defaultextension=".zip",
                                                filetypes=[(tr("ZIP 制作包"), "*.zip")],
                                                initialfile=f"AI制作包_{folder.name}.zip")
            if path:
                def saved(result):
                    self.status.set(tr("已导出所选样片的 AI 制作包"))
                    messagebox.showinfo(tr("AI 制作包已导出"), result, parent=self.root)
                self.job("导出 AI 制作包", lambda report: review.export_package(folder, path, report), saved)

        bar = action_row(self.body)
        watch = button(bar, "观看所选样片", play)
        watch.pack(side="left")
        notes = button(bar, "查看分镜清单", lambda: self.open_file(selected_folder() / review.BRIEF))
        notes.pack(side="left", padx=(6, 0))
        export = button(bar, "导出 AI 制作包…", package)
        export.pack(side="left", padx=6)
        if records:
            tv.selection_set(str(len(records) - 1))
            tv.bind("<Double-1>", lambda e: play() if tv.identify_row(e.y) else None)
        else:
            watch.configure(state="disabled")
            notes.configure(state="disabled")
            export.configure(state="disabled")
        button(bar, "去封面制作 →", lambda: self.go("cover"), "plain").pack(side="right")

    def default_cover_title(self):
        topic = self.current_topic()
        return ((topic or {}).get("title") or self.p["title"]).strip()

    def ai_selection(self):
        """封面背景沿用「一键生成视频」里配好的 AI 生图通道，直到本页单独选择。"""
        return (self.prefs.get(step_models.PREF_PREFIX + "cover")
                or self.prefs.get(step_models.PREF_PREFIX + "images"))

    def page_cover(self):
        self.header("封面制作", "标题压在背景上。让生图模型画三张底图挑一张，也可以从样片、成片或本地图片取。")
        records = self.p["covers"]
        last = records[-1] if records else {}
        upright = not self.p["orientation"].startswith("横")
        state = {"size": last.get("size") or ("竖屏 9∶16" if upright else "横屏 16∶9"),
                 "style": last.get("style") or covers.STYLE_NAMES[0],
                 "background": last.get("background", ""), "source": last.get("source", "纯色")}
        if state["size"] not in covers.SIZES:
            state["size"] = "竖屏 9∶16" if upright else "横屏 16∶9"
        if state["style"] not in covers.STYLE_NAMES:
            state["style"] = covers.STYLE_NAMES[0]

        outer, pane = box(self.body)
        outer.pack(fill="x", pady=(0, 10))
        canvas_width, canvas_height = pixels(self.root, 236), pixels(self.root, 236)
        thumb = tk.Canvas(pane, width=canvas_width, height=canvas_height, bg="#101314",
                          highlightthickness=0, bd=0)
        thumb.pack(side="left", padx=(0, 16), anchor="n")
        form = frame(pane)
        form.pack(side="left", fill="both", expand=True)

        top = frame(form)
        top.pack(fill="x", pady=(0, 7))
        label(top, "画幅", 9, DIM).pack(side="left", padx=(0, 6))
        size = Choice(top, values=tuple(covers.SIZES), state="readonly", width=11)
        size.set(state["size"])
        size.pack(side="left")
        dimension = label(top, "", 9, DIM)
        dimension.pack(side="left", padx=(8, 0))
        label(top, "风格", 9, DIM).pack(side="left", padx=(18, 6))
        style = Choice(top, values=covers.STYLE_NAMES, state="readonly", width=10)
        style.set(state["style"])
        style.pack(side="left")

        line = frame(form)
        line.pack(fill="x", pady=3)
        label(line, "标题", 9, DIM, width=3).pack(side="left")
        title = tk.StringVar(line, value=last.get("title") or self.default_cover_title())
        field(line, title).pack(side="left", fill="x", expand=True)
        label(line, "角标", 9, DIM, width=3).pack(side="left", padx=(10, 0))
        badge = tk.StringVar(line, value=last.get("label", ""))
        corner = field(line, badge)
        corner.configure(width=10)
        corner.pack(side="left")

        line = frame(form)
        line.pack(fill="x", pady=3)
        label(line, "副标题", 9, DIM, width=3).pack(side="left")
        subtitle = tk.StringVar(line, value=last.get("subtitle", ""))
        field(line, subtitle).pack(side="left", fill="x", expand=True)

        sources = frame(form)
        sources.pack(fill="x", pady=(8, 0))
        hint = tk.StringVar(form, value="")
        label(form, "", 9, DIM, textvariable=hint, wraplength=pixels(self.root, 520),
              justify="left").pack(anchor="w", pady=(7, 0))
        actions = frame(form)
        actions.pack(fill="x", pady=(9, 0))

        holder = {"photo": None, "after": None}

        def blank(text):
            thumb.delete("all")
            thumb.create_text(canvas_width // 2, canvas_height // 2, text=tr(text), fill="#7c8b87",
                              width=canvas_width - 24, justify="center", font=(widgets.FONT, 9))

        def show(image):
            view = ImageOps.contain(image, (canvas_width - 10, canvas_height - 10), Image.Resampling.LANCZOS)
            holder["photo"] = ImageTk.PhotoImage(view)
            thumb.delete("all")
            thumb.create_image(canvas_width // 2, canvas_height // 2, image=holder["photo"])

        def current_size():
            value = size.get()
            return value if value in covers.SIZES else state["size"]

        def current_style():
            value = style.get()
            return value if value in covers.STYLE_NAMES else state["style"]

        def background_file():
            return covers.background_path(self.store.root, self.p["id"], state["background"]) if state["background"] else ""

        def note():
            if not state["background"]:
                return "还没有背景：点「AI 生图封面」画三张挑一张，或从样片、成片取一帧。"
            if state["source"] == "样片取帧":
                return "背景：样片取帧。样片只有 360p，封面会偏软，建议先导出成片再取帧。"
            return f"背景：{state['source']}（已存到封面文件夹，换标题可以直接重新生成）。"

        def update_hint(message=""):
            width, height = covers.SIZES[current_size()]
            dimension.configure(text=f"{width} × {height}")
            hint.set(message or note())

        def refresh():
            try:
                image = covers.compose(background_file(), current_size(), title.get(),
                                       subtitle.get(), badge.get(), current_style())
            except ValueError as error:
                hint.set(notice(str(error)))
                return False
            show(image)
            update_hint()
            return True

        def run_preview():
            holder["after"] = None
            refresh()

        def schedule(*_):
            if holder["after"] is not None:
                try:
                    self.root.after_cancel(holder["after"])
                except tk.TclError:
                    pass
            holder["after"] = self.root.after(380, run_preview)

        def cleanup(_event=None):
            if holder["after"] is not None:
                try:
                    self.root.after_cancel(holder["after"])
                except tk.TclError:
                    pass
                holder["after"] = None

        def save():
            fields = {"size": current_size(), "style": current_style(), "title": title.get().strip(),
                      "subtitle": subtitle.get().strip(), "label": badge.get().strip(),
                      "background": state["background"],
                      "source": state["source"] if state["background"] else "纯色"}
            image = covers.compose(background_file(), fields["size"], fields["title"], fields["subtitle"],
                                   fields["label"], fields["style"])
            record = covers.save(self.store.root, self.p, image, fields)
            self.p["covers"].append(record)
            self.save_now()
            self.status.set(tr("封面已保存"))
            self.go("cover")

        def use_color():
            state.update(background="", source="纯色")
            update_hint()
            refresh()

        def pick_image():
            path = filedialog.askopenfilename(parent=self.root, title=tr("选择封面背景图片"),
                                              initialdir=self.source_directory(),
                                              filetypes=[(tr("图片"), " ".join("*" + x for x in sorted(IMAGES)))])
            if not path:
                return
            try:
                name = covers.keep_background(self.store.root, self.p, path, current_size())
            except ValueError as error:
                messagebox.showerror(tr("封面背景"), notice(str(error)), parent=self.root)
                return
            state.update(background=name, source="本地图片")
            update_hint()
            refresh()

        def choose_drawn(paths):
            win = dialog(self.root, "挑一张背景", 724, 436)
            inside = frame(win)
            inside.pack(fill="both", expand=True, padx=18, pady=16)
            label(inside, "挑一张做封面背景，标题和角标会压在它上面。", 10, DIM).pack(anchor="w", pady=(0, 10))
            row = frame(inside)
            row.pack(fill="both", expand=True)
            photos = []

            def adopt(path):
                name = covers.keep_background(self.store.root, self.p, path, current_size())
                state.update(background=name, source="AI 绘制")
                win.destroy()
                update_hint()
                refresh()

            for column, path in enumerate(paths):
                row.columnconfigure(column, weight=1, uniform="drawn")
                cell = frame(row)
                cell.grid(row=0, column=column, padx=6, sticky="nsew")
                try:
                    with Image.open(path) as raw:
                        view = ImageOps.contain(raw.convert("RGB"),
                                                (pixels(self.root, 186), pixels(self.root, 238)))
                except (OSError, ValueError):
                    continue
                photos.append(ImageTk.PhotoImage(view))
                tk.Label(cell, image=photos[-1], bg="#101314", bd=0).pack()
                button(cell, "用这张", lambda path=path: adopt(path), "primary").pack(pady=(8, 0))
            win.photos = photos
            bar = action_row(inside)
            button(bar, "取消", win.destroy, "plain").pack(side="right")

        def draw_ai():
            prefs, key = self.engine("cover", selection=self.ai_selection())
            if prefs["engine"] == "API" and prefs.get("protocol") not in generated_images.PROTOCOLS:
                raise ValueError("封面背景要生图模型。请在本页右下角「模型与 Harness」选择 Codex CLI，"
                                 "或支持生图的 API。")
            subject = title.get().strip() or self.p["title"]

            def done(paths):
                choose_drawn(paths)

            self.job("绘制封面背景", lambda report: covers.ai_backgrounds(
                self.store.root, self.p, prefs, key, subject, badge.get().strip(), current_style(),
                current_size(), report), done)

        def take_frame():
            candidates = covers.video_candidates(self.store.root, self.p)
            if not candidates:
                raise ValueError("还没有可用的视频。请先在「样片审看」合成一版样片。")
            win = dialog(self.root, "从视频取帧", 620, 235)
            inside = frame(win)
            inside.pack(fill="both", expand=True, padx=20, pady=16)
            label(inside, "取帧来源", 9, DIM).pack(anchor="w")
            chosen = Choice(inside, values=[candidate["label"] for candidate in candidates],
                            state="readonly", width=70)
            chosen.current(0)
            chosen.pack(fill="x", pady=(4, 10))
            seconds = entry_row(inside, "时间（秒）", "1.0")
            label(inside, "时间从 0 秒开始。片头空白多就往后取几秒；成片比样片清晰。", 9, DIM,
                  wraplength=pixels(win, 540), justify="left").pack(anchor="w", pady=(4, 0))

            def take():
                candidate = candidates[chosen.current()]

                def done(name):
                    state.update(background=name,
                                 source="成片取帧" if candidate["kind"] == "成片" else "样片取帧")
                    win.destroy()
                    update_hint()
                    refresh()

                self.job("取帧", lambda report: covers.frame_background(
                    self.store.root, self.p, candidate["path"], seconds.get(), current_size()), done, win)

            bar = action_row(inside)
            button(bar, "取这一帧", take, "primary").pack(side="right")
            button(bar, "取消", win.destroy, "plain").pack(side="right", padx=6)

        def changed(_event=None):
            state.update(size=current_size(), style=current_style())
            update_hint()
            schedule()

        for variable in (title, subtitle, badge):
            variable.trace_add("write", schedule)
        size.bind("<<ComboboxSelected>>", changed)
        style.bind("<<ComboboxSelected>>", changed)
        outer.bind("<Destroy>", cleanup)

        button(sources, "AI 生图封面", draw_ai, "primary").pack(side="left")
        button(sources, "取帧作背景", take_frame).pack(side="left", padx=5)
        button(sources, "选本地图片", pick_image).pack(side="left")
        button(sources, "纯色", use_color).pack(side="left", padx=(5, 0))
        button(actions, "预览", refresh).pack(side="left")
        button(actions, "保存封面", save, "primary").pack(side="left", padx=6)

        if records:
            try:
                saved = covers.file_path(self.store.root, self.p["id"], last.get("file", ""))
            except ValueError:
                saved = None
            if saved is not None and saved.is_file():
                with Image.open(saved) as image:
                    show(image.convert("RGB"))
            else:
                blank("封面文件已丢失，请重新生成")
        else:
            blank("预览会显示在这里")
        update_hint()

        bar = frame(self.body)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        label(self.body, "本期封面" if records else "本期还没有封面", 13, bold=True).pack(anchor="w", pady=(0, 6))
        holder_view, tv = table(self.body, [("date", "生成时间", 150, True), ("size", "画幅", 96, False),
                                            ("title", "标题", 250, True), ("style", "风格", 86, False),
                                            ("source", "背景", 96, False)], 4)
        holder_view.pack(fill="both", expand=True)
        for index in reversed(range(len(records))):
            record = records[index]
            tv.insert("", "end", iid=str(index), values=(record["created_at"], tr(record["size"]),
                                                         record["title"], tr(record["style"]), tr(record["source"])))

        def selected_record():
            return records[int(selected(tv, "请先选择一张封面。"))]

        def open_selected():
            record = selected_record()
            self.open_file(covers.file_path(self.store.root, self.p["id"], record["file"]))

        def save_as():
            record = selected_record()
            source = covers.file_path(self.store.root, self.p["id"], record["file"])
            target = filedialog.asksaveasfilename(parent=self.root, title=tr("保存封面图片"),
                                                  defaultextension=".png", initialfile=record["file"],
                                                  filetypes=[(tr("PNG 图片"), "*.png")])
            if not target or Path(target).resolve() == source.resolve():
                return
            Path(target).write_bytes(source.read_bytes())
            self.status.set(tr("已另存封面到 ") + target)

        def open_covers():
            self.open_folder(covers.project_dir(self.store.root, self.p["id"]), create=True)

        def cover_actions(event):
            row = tv.identify_row(event.y)
            return [("打开封面", open_selected), ("打开封面文件夹", open_covers)] if row else []

        bind_context_menu(tv, cover_actions)
        open_button = button(bar, "打开封面", open_selected)
        open_button.pack(side="left")
        export_button = button(bar, "另存为…", save_as)
        export_button.pack(side="left", padx=6)
        button(bar, "打开封面文件夹", open_covers, "plain").pack(side="left")
        button(bar, "去成片输出 →", lambda: self.go("output"), "plain").pack(side="right")
        if records:
            tv.selection_set(str(len(records) - 1))
            tv.bind("<Double-1>", lambda event: open_selected() if tv.identify_row(event.y) else None)
        else:
            open_button.configure(state="disabled")
            export_button.configure(state="disabled")

    def page_output(self):
        self.header("成片输出", "使用音频制作中的混音和字幕制作中的设置导出。")
        outer, pane = box(self.body)
        outer.pack(fill="x", pady=(0, 12))
        self.video_format_controls(pane, 720, captions=True)
        def render(final):
            self.save_now()
            if not confirmed(self.p) or not self.p["shots"]:
                raise ValueError("请先确认文案并生成分镜。")
            p, assets = deepcopy(self.p), self.store.assets(self.p["id"])
            voice_profile = self.current_voice_profile()
            self.job("合成完整视频" if final else "导出无字幕画面",
                     lambda report: production.render(self.store.root, p, assets, final, report, voice_profile=voice_profile),
                     self.exported)
        actions = action_row(pane)
        button(actions, "导出无字幕画面", lambda: render(False)).pack(side="left")
        button(actions, "合成完整视频", lambda: render(True), "primary").pack(side="left", padx=8)
        label(self.body, "导出记录", 13, bold=True).pack(anchor="w", pady=(0, 6))
        holder, tv = table(self.body, [("type","类型",108,False),("file","文件名称",270,True)], 4)
        holder.pack(fill="both", expand=True)
        records = self.p["exports"]
        for i, e in reversed(list(enumerate(records))):
            tv.insert("", "end", iid=str(i), values=(tr(e["kind"]), Path(e["path"]).name))

        def export_path(row):
            return Path(records[int(row)]["path"])

        def play():
            self.open_file(export_path(selected(tv, "请先选择一个导出文件。")))

        def export_actions(event):
            row = tv.identify_row(event.y)
            return [("打开所在文件夹", lambda: self.open_folder(export_path(row).parent))] if row else []

        bind_context_menu(tv, export_actions)
        tv.bind("<Double-1>", lambda _e: play())
        bar = action_row(self.body)
        button(bar, "播放文件", play).pack(side="left")
        button(bar, "打开导出目录", self.open_output_folder, "plain").pack(side="right")

    def model_settings(self, parent):
        outer, b = box(parent)
        outer.pack(fill="both", expand=True)
        row = frame(b)
        row.pack(fill="x", pady=(0, 12))
        label(row, "调用方式", color=DIM, width=12).pack(side="left")
        harnesses = {
            "Codex": ("codex_path", codex_harness.cli_path, codex_harness.models),
            "DSH": ("harness", providers.harness_path, providers.dsh_models),
        }
        engines = ("", *harnesses, "API")
        engine = Choice(row, values=("请选择调用方式", "Codex CLI", "DeepSeek Harness", "直连 API"),
                        state="readonly", width=22)
        engine.current(engines.index(self.prefs["engine"]))
        engine.pack(side="left")
        bar = frame(b)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        options = frame(b)
        options.pack(fill="x")

        api = ApiSettings(options, self)
        setup_hint = label(options, "请选择你自己的 CLI 账号或 API 配置。", 10, DIM)
        local_default = tr("使用本机默认模型")

        def harness_form(kind, path_key, find, list_models):
            name, model_key = step_models.HARNESSES[kind]
            panel = frame(options)
            line = frame(panel)
            line.pack(fill="x", pady=4)
            label(line, "模型", color=DIM, width=12).pack(side="left")
            model = ttk.Combobox(line)
            model.set(self.prefs[model_key] or local_default)
            message = label(panel, "", 9, DIM)
            message.pack(anchor="w", pady=(3, 5))
            advanced = advanced_options(panel)
            path = path_row(advanced, "命令路径", self.prefs[path_key], directory=False)
            label(advanced, "留空时自动查找本机命令。", 9, DIM).pack(anchor="w")

            def refresh(*_):
                found = find(path.get())
                models = self.cli_model_lists.get((kind, found), ())
                model.configure(values=(local_default, *models))
                message.configure(text=tr("已检测到 {}，可使用本机配置。").format(name) if found else
                                       tr("请在高级选项指定 {} 的命令路径。").format(name))

            def executable():
                found = find(path.get())
                if not found:
                    raise ValueError(f"请在高级选项指定 {name} 的命令路径。")
                return found

            def load():
                found = executable()

                def done(models):
                    self.cli_model_lists[kind, found] = models
                    refresh()
                    self.status.set(tr("已读取 {} 个模型").format(len(models)))

                self.job("读取模型列表", lambda _: list_models(found), done)

            def draft():
                executable()
                selected = model.get().strip()
                model_id = "" if selected == local_default else selected
                return {"engine": kind, path_key: path.get().strip(), model_key: model_id}

            button(line, "读取列表", load).pack(side="right", padx=(8, 0))
            model.pack(side="left", fill="x", expand=True)
            path.trace_add("write", refresh)
            panel.bind("<Map>", refresh)
            return panel, draft

        panels, harness_drafts = {"": setup_hint, "API": api}, {}
        for kind, fields in harnesses.items():
            panels[kind], harness_drafts[kind] = harness_form(kind, *fields)

        def swap(*_):
            kind = engines[engine.current()]
            for panel in panels.values():
                panel.pack_forget()
            panels[kind].pack(fill="x")
            state = "normal" if kind else "disabled"
            save_button.configure(state=state)
            test_button.configure(state=state)
            if kind == "API":
                save_only.pack(side="left", padx=(0, 7))
            else:
                save_only.pack_forget()

        def apply():
            kind = engines[engine.current()]
            if kind == "API":
                api.save()
            else:
                self.store.set_prefs(harness_drafts[kind]())
            self.prefs = self.store.prefs()
            self.refresh_engine_label()
            self.status.set(tr("已启用 ") + self.engine_label.cget("text"))

        def test():
            kind = engines[engine.current()]
            if kind == "API":
                profile, key = api.draft()
                prefs = {**profile, "engine": "API"}
                tested = profile["test_model"] or step_models.api_target_name(
                    profile["name"], profile["model"], profile["kind"])
            else:
                prefs, key = harness_drafts[kind](), ""
                tested = step_models.engine_name(prefs)

            def done(value):
                self.status.set(tr("检测成功 · ") + tested)
                messagebox.showinfo(tr("连接成功"), tested + tr(" 返回\n") + value[:160], parent=self.root)

            self.job("测试连接", lambda _: providers.probe_model(prefs, key), done)

        save_button = button(bar, "保存并启用", apply, "primary")
        save_button.pack(side="left")
        test_button = button(bar, "测试连接", test)
        test_button.pack(side="left", padx=7)
        def store_for_steps():
            api.save(activate=False)
            self.prefs = self.store.prefs()
            self.refresh_engine_label()
            self.status.set(tr("API 配置已保存，可在各制作环节选择。"))
        save_only = button(bar, "仅保存配置", store_for_steps)
        engine.bind("<<ComboboxSelected>>", swap)
        swap()


def main():
    from store import data_home
    start(Studio, Store, data_home, APP_NAME)


if __name__ == "__main__":
    main()
