"""The workspace's settings hub."""

import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

import appearance
import app_paths
import release
from ui_language import tr
from widgets import GREEN, DIM, action_row, box, button, checkbox, dialog, field, frame, label, pixels, scroll_page


SECTIONS = {"appearance": "外观与语言", "models": "模型与 API", "paths": "文件与工具",
            "help": "使用指南", "changelog": "更新日志"}
TOOL_ROWS = (("ffmpeg_dir", "FFmpeg 目录"), ("ego_cli", "Ego CLI 文件"), ("sovits_dir", "GPT-SoVITS 目录"),
             ("sovits_python", "SoVITS Python"), ("font_file", "渲染字体文件"))


def description(parent, text):
    line = label(parent, text, 10, DIM, justify="left")
    line.pack(fill="x", pady=(3, 8))
    line.bind("<Configure>", lambda event: line.configure(wraplength=max(170, event.width - 4)))


def path_row(parent, caption, value, *, directory):
    row = frame(parent)
    row.pack(fill="x", pady=3)
    label(row, caption, width=15).pack(side="left")
    var = tk.StringVar(row, value=value)
    field(row, var).pack(side="left", fill="x", expand=True, padx=(0, 6))

    def browse():
        start = Path(var.get()).expanduser()
        chooser = filedialog.askdirectory if directory else filedialog.askopenfilename
        options = {}
        if var.get() and start.is_absolute() and start.exists():
            options["initialdir"] = str(start if start.is_dir() else start.parent)
        result = chooser(parent=row.winfo_toplevel(), title=tr(caption), **options)
        if result:
            var.set(result)

    button(row, "浏览", browse, "secondary").pack(side="right")
    return var


class SettingsPages:
    def page_settings(self):
        self.header("设置")
        tabs = frame(self.body)
        tabs.pack(fill="x", pady=(12, 14))
        for key, title in SECTIONS.items():
            button(tabs, title, lambda k=key: self.show_settings(k),
                   "primary" if key == self.settings_section else "plain").pack(side="left", padx=(0, 4))
        label(tabs, "VAT " + release.version(), 10, GREEN, localize=False).pack(side="right", padx=(8, 2))
        content = self.body if self.settings_section == "changelog" else scroll_page(self.body)
        {"appearance": self.appearance_settings, "models": self.model_settings,
         "paths": self.path_settings, "help": self.help_settings,
         "changelog": self.changelog_settings}[self.settings_section](content)

    def show_settings(self, section):
        if section != self.settings_section:
            self.settings_section = section
            self.go("settings")

    def remember_tutorial(self):
        """记住是否在启动时显示新手教程，教程内容仍可在「使用指南」里打开。"""
        shown = bool(self.tutorial_startup.get())
        self.store.set_pref("tutorial", "show" if shown else "off")
        self.prefs = self.store.prefs()
        self.status.set(tr("启动时会显示新手教程") if shown else tr("启动时不再显示新手教程"))

    def apply_appearance(self, key, value):
        if self.prefs.get(key) == value:
            return
        self.store.set_pref(key, value)
        self.prefs = self.store.prefs()
        appearance.apply_preferences(self.prefs)
        # 旧页面的 capture 回调绑定在即将销毁的控件上，先丢掉再重建外壳
        self.capture = None
        self.rail.destroy()
        self.main.destroy()
        self.root.title(tr("VAT（自动剪辑软件）"))
        self.root.configure(bg=appearance.current["bg"])
        self.build_shell()
        self.go("settings")
        self.status.set(tr("外观设置已保存"))

    def appearance_settings(self, parent):
        label(parent, "工作台主题", 13, bold=True).pack(anchor="w")
        cards = frame(parent)
        cards.pack(fill="x", pady=(7, 16))
        accent = self.prefs.get("accent", "jade")
        for i, (key, title) in enumerate(appearance.THEMES.items()):
            selected = self.prefs.get("theme", "modern") == key
            palette = appearance.palette(key, accent)
            cards.columnconfigure(i, weight=1, uniform="preview")
            outline = palette["primary"] if selected else appearance.current["line"]
            tile = tk.Frame(cards, bg=palette["surface"], highlightthickness=2 if selected else 1,
                            highlightbackground=outline)
            tile.grid(row=0, column=i, sticky="nsew", padx=(0, 6) if i == 0 else (6, 0))
            sample = tk.Canvas(tile, bg=palette["bg"], height=pixels(tile, 67), bd=0, highlightthickness=0)
            sample.pack(fill="x", padx=9, pady=(9, 2))

            def draw(event, c=sample, p=palette, modern=(key != "classic")):
                w, h = event.width, event.height
                rail = w * (.24 if modern else .29)
                c.delete("all")
                c.create_rectangle(0, 0, rail, h, fill=p["rail"], outline=p["line"])
                c.create_rectangle(6, 9, rail - 6, 16, fill=p["active"], outline="")
                for y in (25, 35, 45):
                    c.create_line(8, y, rail * .72, y, fill=p["rail_dim"], width=2)
                c.create_rectangle(rail + 9, 9, w - 9, 17, fill=p["primary"], outline="")
                c.create_rectangle(rail + 9, 25, w * .65, h - 8, fill=p["surface"], outline=p["line"])
                c.create_rectangle(w * .65 + 4, 25, w - 9, h - 8, fill=p["tint"], outline="")

            sample.bind("<Configure>", draw)
            command = lambda k=key: self.apply_appearance("theme", k)
            sample.bind("<Button-1>", lambda _, fn=command: fn())
            button(tile, ("✓  " if selected else "") + title, command, "plain").pack(fill="x")

        outer, body = box(parent)
        outer.pack(fill="x")
        label(body, "主题配色", 12, bold=True).pack(anchor="w")
        row = frame(body)
        row.pack(fill="x", pady=(8, 14))
        for key, (name, primary, tint, _) in appearance.ACCENTS.items():
            btn = button(row, ("✓  " if key == accent else "") + name, lambda k=key: self.apply_appearance("accent", k))
            btn.configure(bg=primary if key == accent else tint, fg="white" if key == accent else primary,
                          activebackground=tint, activeforeground=primary)
            btn.pack(side="left", padx=(0, 7))
        label(body, "界面语言", 12, bold=True).pack(anchor="w")
        choices = ttk.Combobox(body, values=list(appearance.LANGUAGES.values()), state="readonly", width=20)
        languages = list(appearance.LANGUAGES)
        lang = self.prefs.get("language")
        choices.current(languages.index(lang) if lang in languages else 0)
        choices.pack(anchor="w", pady=(8, 2))
        choices.bind("<<ComboboxSelected>>", lambda _: self.apply_appearance("language", languages[choices.current()]))

    def path_settings(self, parent):
        outer, body = box(parent)
        outer.pack(fill="x", pady=(0, 10))
        label(body, "工作区", 13, bold=True).pack(anchor="w")
        description(body, "项目、素材索引、缓存和导出文件保存在工作区。切换后重启生效；原工作区可随时重新打开。")
        label(body, str(self.store.root), 9, DIM, localize=False).pack(anchor="w")
        workspace = path_row(body, "下次启动位置", str(app_paths.data_home()), directory=True)

        def save_workspace():
            value = workspace.get().strip()
            app_paths.set_workspace(value)
            workspace.set(str(app_paths.data_home()))
            self.status.set(tr("已保存下次启动的工作区"))

        actions = action_row(body)
        button(actions, "保存工作区", save_workspace, "primary").pack(side="left")
        button(actions, "使用系统默认位置", lambda: workspace.set(""), "plain").pack(side="left", padx=4)
        button(actions, "打开当前目录", lambda: self.open_folder(self.store.root), "plain").pack(side="right")

        outer, body = box(parent)
        outer.pack(fill="x", pady=(0, 10))
        label(body, "素材与音色", 13, bold=True).pack(anchor="w")
        description(body, "源文件夹用于打开素材、口播和声音选择框。导入后按原路径引用文件。")
        source = path_row(body, "输入源文件夹", self.prefs.get("source_dir", ""), directory=True)
        voice = path_row(body, "音色配置 JSON", self.prefs.get("sovits_profile", ""), directory=False)

        def save_sources():
            folder = source.get().strip()
            profile = voice.get().strip()
            if folder:
                path = Path(folder).expanduser()
                if not path.is_absolute() or not path.is_dir():
                    raise ValueError(tr("请选择已存在的素材目录，或清空后使用系统默认位置。"))
                folder = str(path.resolve())
            if profile:
                import sovits
                path = Path(profile).expanduser()
                if not path.is_absolute() or not path.is_file():
                    raise ValueError(tr("请选择本机的音色配置 JSON 文件。"))
                profile = str(path.resolve())
                sovits.load_profile(profile)
            self.store.set_prefs({"source_dir": folder, "sovits_profile": profile})
            self.prefs = self.store.prefs()
            self.status.set(tr("素材与音色路径已保存"))

        button(body, "保存素材与音色", save_sources, "primary").pack(anchor="w", pady=(8, 0))
        outer, body = box(parent)
        outer.pack(fill="x")
        label(body, "本机工具", 13, bold=True).pack(anchor="w")
        description(body, "仅填写需要指定的工具。FFmpeg 目录应同时包含 ffmpeg 与 ffprobe。GPT-SoVITS 可连接已运行的服务，也可指定安装目录和 Python。")
        tools = {key: path_row(body, name, app_paths.get_path(key), directory=app_paths.TOOL_PATHS[key] == "dir")
                 for key, name in TOOL_ROWS}

        def save_tools():
            app_paths.set_paths({key: var.get().strip() for key, var in tools.items()})
            self.status.set(tr("本机工具路径已保存"))

        button(body, "保存工具路径", save_tools, "primary").pack(anchor="w", pady=(8, 0))

    def help_settings(self, parent):
        outer, body = box(parent)
        outer.pack(fill="x", pady=(0, 10))
        label(body, "新手教程", 13, bold=True).pack(anchor="w")
        description(body, "教程先介绍运行环境，再介绍如何导入模型 API、交给 agent 配置、使用本机的 Codex 或 "
                          "DeepSeek Harness，以及连接本地语音服务。")
        row = action_row(body)
        button(row, "重新打开新手教程", self.open_tutorial, "primary").pack(side="left")
        self.tutorial_startup = tk.BooleanVar(body, value=self.prefs.get("tutorial") != "off")
        checkbox(row, "启动时显示", self.tutorial_startup, self.remember_tutorial).pack(side="right")

        outer, body = box(parent)
        outer.pack(fill="x", pady=(0, 10))
        label(body, "开始制作", 13, bold=True).pack(anchor="w")
        description(body, "新建一期，手动添加选题或直接写稿。确认定稿后按句拆镜，导入素材和录音，在样片审看中检查画面，再到封面制作出图，最后到成片输出导出视频。")
        button(body, "阅读完整使用说明", self.show_manual).pack(anchor="w")
        outer, body = box(parent)
        outer.pack(fill="x")
        label(body, "选择模型服务", 13, bold=True).pack(anchor="w")
        description(body, "可连接自己已配置的 Codex CLI 或 DeepSeek Harness。使用云端服务或本地 Ollama 时，请添加自己的 API 配置。")
        label(body, "配置 API", 11, bold=True).pack(anchor="w")
        description(body, "打开「模型与 API → 直连 API」，填接口地址和服务要求的密钥。点「读取列表」选调用目标，或直接输入调用 ID。可标记为「模型」或「Agent」，再点「保存并启用」。")
        label(body, "本地 Ollama", 11, bold=True).pack(anchor="w")
        description(body, "启动 Ollama 后，可在「直连 API → 高级选项」用「本地 Ollama」填入地址，或点「导入本地 GGUF」添加已有权重。默认地址为 http://localhost:11434/v1，密钥可留空。")
        label(body, "连接问题", 11, bold=True).pack(anchor="w")
        description(body, "「测试连接」会实际调用一次。401 或 403 请核对密钥和账号权限。404 请核对接口地址、调用 ID 和高级选项里的协议。本机连接被拒绝时，请检查服务和端口。")
        row = action_row(body)
        button(row, "前往模型设置", lambda: self.show_settings("models"), "primary").pack(side="left")
        button(row, "DeepSeek 官方文档", lambda: webbrowser.open("https://api-docs.deepseek.com/"), "plain").pack(side="left")
        button(row, "Ollama 官方文档", lambda: webbrowser.open("https://docs.ollama.com/api/openai-compatibility"), "plain").pack(side="left")

        outer, body = box(parent)
        outer.pack(fill="x", pady=(10, 0))
        label(body, "关于 VAT " + release.version(), 13, bold=True, localize=False).pack(anchor="w")
        description(body, release.NOTE_LICENCE)
        for caption, value in release.CONTACTS:
            contact = frame(body)
            contact.pack(fill="x", pady=2)
            label(contact, caption, 10, DIM, width=13).pack(side="left")
            label(contact, value, 10, bold=True, localize=False).pack(side="left")
        row = action_row(body)
        button(row, "联系方式与使用须知", self.show_notice, "primary").pack(side="left")

    def changelog_settings(self, parent):
        self.show_readonly_document(parent, "CHANGELOG.md")

    def show_manual(self):
        win = dialog(self.root, "使用说明", 760, 560)
        self.show_readonly_document(win, "使用说明.md")

    def show_readonly_document(self, parent, filename):
        text = Path(__file__).with_name(filename).read_text(encoding="utf-8-sig")
        wrap = frame(parent)
        wrap.pack(fill="both", expand=True, padx=16, pady=16)
        view = tk.Text(wrap, wrap="word", relief="flat", padx=16, pady=12,
                       bg=appearance.current["surface"], fg=appearance.current["ink"], font="TkTextFont")
        bar = ttk.Scrollbar(wrap, command=view.yview)
        view.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        view.pack(fill="both", expand=True)
        # Keep Markdown links, CLI commands, paths and configuration keys copyable as written.
        view.insert("1.0", text)
        view.configure(state="disabled")

