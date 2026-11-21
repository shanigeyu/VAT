import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import providers
from api_profiles import AUTO_PROTOCOL, KIND_LABELS, OPENAI, PROTOCOLS, api_endpoint, api_key, parse_profile, validated_profile
from step_models import api_target_name
from ui_language import Choice, tr
from widgets import DIM, advanced_options, button, dialog, field, frame, label


class ApiSettings(tk.Frame):
    def __init__(self, parent, studio):
        super().__init__(parent, bg=parent.cget("bg"))
        self.studio = studio
        self.profile_id = self.key_scope = None
        self.profiles = []
        self.name = tk.StringVar(self)
        self.kind = tk.StringVar(self, value=KIND_LABELS["model"])
        self.protocol = tk.StringVar(self, value=AUTO_PROTOCOL)
        self.base = tk.StringVar(self)
        self.key = tk.StringVar(self)
        self.model = tk.StringVar(self)
        self.test_model = tk.StringVar(self)
        self.import_summary = tk.StringVar(self)

        def row(title, parent=self):
            line = frame(parent)
            line.pack(fill="x", pady=4)
            label(line, title, color=DIM, width=12).pack(side="left")
            return line

        line = row("已保存的 API")
        self.selector = ttk.Combobox(line, state="readonly", width=15)
        self.selector.pack(side="left", fill="x", expand=True)
        button(line, "新增 API", self.new_profile).pack(side="left", padx=(8, 0))
        button(line, "One API 网关", self.one_api_dialog).pack(side="left", padx=(6, 0))
        self.selector.bind("<<ComboboxSelected>>", self.select_profile)

        self.base_entry = field(row("接口地址"), self.base)
        self.base_entry.pack(fill="x")
        field(row("API 密钥"), self.key, "●").pack(fill="x")
        line = row("调用 ID")
        self.model_picker = ttk.Combobox(line, textvariable=self.model, width=15)
        self.model_picker.pack(side="left", fill="x", expand=True)
        button(line, "读取列表", self.load_models).pack(side="left", padx=(8, 0))
        label(self, "接口需要认证时填写密钥。调用 ID 可以从列表选择，也可以手填。ElevenLabs 等配音服务请到「音频制作 → 配音设置」配置。",
              9, DIM, wraplength=540, justify="left").pack(anchor="w", pady=(2, 6))

        line = row("标记")
        Choice(line, textvariable=self.kind, values=KIND_LABELS.values(), state="readonly", width=8).pack(side="left")
        label(line, "显示名（选填）", color=DIM).pack(side="left", padx=(12, 8))
        field(line, self.name).pack(side="left", fill="x", expand=True)

        advanced = advanced_options(self)
        line = row("接口协议", advanced)
        Choice(line, textvariable=self.protocol, values=(AUTO_PROTOCOL, *PROTOCOLS), state="readonly",
               width=22).pack(side="left")
        line = row("测试用 ID", advanced)
        field(line, self.test_model).pack(side="left", fill="x", expand=True)
        label(line, "留空时测试当前调用目标", 9, DIM).pack(side="left", padx=(8, 0))
        tools = row("快捷操作", advanced)
        button(tools, "本地 Ollama", lambda: self.use_preset("Ollama", "http://localhost:11434/v1")).pack(side="left")
        button(tools, "导入 API", self.import_dialog).pack(side="left", padx=(6, 0))
        button(tools, "导入本地 GGUF…", self.import_gguf).pack(side="left", padx=6)
        label(advanced, "请填写你自己的接口地址和密钥。密钥在本机加密保存。",
              9, DIM, wraplength=560, justify="left").pack(anchor="w", pady=(6, 0))

        self.import_notice = label(self, "", 10, DIM, textvariable=self.import_summary, wraplength=540, justify="left")
        self.base.trace_add("write", self.endpoint_changed)
        self.protocol.trace_add("write", self.endpoint_changed)
        self.key.trace_add("write", self.key_changed)
        self.refresh_profiles(studio.prefs["api_profile"])

    def refresh_profiles(self, selected_id):
        self.profiles = self.studio.store.api_profiles()
        self.selector.configure(values=[api_target_name(p["name"], p["model"], p["kind"]) for p in self.profiles])
        index = next((i for i, p in enumerate(self.profiles) if p["id"] == selected_id), None)
        if index is None:
            self.selector.set(tr("新 API（待保存）"))
            self.fill({}, "")
            return
        self.selector.current(index)
        self.select_profile()

    def select_profile(self, _event=None):
        profile = self.profiles[self.selector.current()]
        try:
            key = self.studio.store.profile_key(profile["id"], profile["base"], profile["protocol"])
        except ValueError as e:
            key = ""
            self.studio.status.set(tr(str(e)))
        self.fill(profile, key)

    def fill(self, profile, key):
        self.import_notice.pack_forget()
        self.key.set("")
        self.profile_id = profile.get("id")
        self.name.set(profile.get("name", ""))
        self.protocol.set(profile.get("protocol", AUTO_PROTOCOL))
        self.base.set(profile.get("base", ""))
        self.model.set(profile.get("model", ""))
        self.kind.set(KIND_LABELS[profile.get("kind", "model")])
        self.test_model.set(profile.get("test_model", ""))
        self.key.set(key)

    def new_profile(self):
        self.selector.set(tr("新 API（待保存）"))
        self.fill({}, "")
        self.base_entry.focus_set()

    def one_api_dialog(self):
        import webbrowser

        win = dialog(self.studio.root, "One API 网关", 570, 285)
        body = frame(win)
        body.pack(fill="both", expand=True, padx=18, pady=12)
        existing = self.name.get().startswith("One API · ")
        address = tk.StringVar(win, self.base.get() if existing else "http://127.0.0.1:3000/v1")
        token = tk.StringVar(win, self.key.get() if existing else "")
        def row(title):
            line = frame(body)
            line.pack(fill="x", pady=4)
            label(line, title, width=10, color=DIM).pack(side="left")
            return line
        field(row("网关地址"), address).pack(fill="x")
        field(row("调用令牌"), token, "●").pack(fill="x")
        line = row("模型 ID")
        model = ttk.Combobox(line)
        model.pack(side="left", fill="x", expand=True)
        model.set(self.model.get() if existing else "")
        notice = label(body, "填写网关签发的调用令牌，可读取模型或手填模型 ID。", 9, DIM)
        def load():
            base, key = providers.one_api_base(address.get()), api_key(token.get())
            def done(models):
                model.configure(values=models)
                if not model.get():
                    model.set(models[0])
                notice.configure(text=tr("已读取 {} 个模型。").format(len(models)))
            self.studio.job("读取网关模型", lambda _: providers.api_models(base, key), done, win)
        def quota():
            base, key = providers.one_api_base(address.get()), api_key(token.get())
            def done(values):
                total, used = values
                notice.configure(text=tr("网关额度 · 总额 {:g}，已用 {:g}（网关显示单位）").format(total, used))
            self.studio.job("查询网关额度", lambda _: providers.one_api_quota(base, key), done, win)
        def use():
            base, key = providers.one_api_base(address.get()), api_key(token.get())
            chosen = model.get().strip()
            profile = validated_profile({"name": "One API · " + chosen, "base": base, "model": chosen, "protocol": OPENAI})
            if not key:
                raise ValueError("请填写网关签发的调用令牌。")
            # A second account on another gateway can expose the very same model name.
            name = profile["name"]
            names = {p["name"].casefold(): p for p in self.profiles}
            suffix = 1
            while name.casefold() in names:
                other = names[name.casefold()]
                if other["base"] == base and other["protocol"] == OPENAI and (
                        other["id"] == self.profile_id or self.studio.store.profile_key(other["id"], base, OPENAI) == key):
                    break
                suffix += 1
                name = f"{profile['name']} ({suffix})"
            self.use_preset(name, base)
            self.model.set(chosen)
            self.key.set(key)
            self.save(activate=False)
            self.studio.prefs = self.studio.store.prefs()
            self.studio.refresh_engine_label()
            self.studio.status.set(tr("网关配置已保存，可在各制作环节选择。"))
            win.destroy()
        def reset(*_):
            token.set("")
            model.set("")
            model.configure(values=())
            notice.configure(text=tr("请填写当前网关的调用令牌。"))
        address.trace_add("write", reset)
        button(line, "读取模型", load).pack(side="left", padx=(6, 0))
        notice.pack(fill="x", pady=5)
        bar = frame(body)
        bar.pack(fill="x", pady=(6, 0))
        button(bar, "保存到 VAT", use, "primary").pack(side="right")
        button(bar, "查询额度", quota).pack(side="left")
        button(bar, "打开网关后台", lambda: webbrowser.open(providers.one_api_base(address.get())[:-3] + "/"),
               "plain").pack(side="left")

    def use_preset(self, name, base):
        # Preserve an existing named configuration and its credential when reselecting it.
        for index, profile in enumerate(self.profiles):
            if profile["name"].casefold() == name.casefold() and profile["base"] == base and profile["protocol"] == OPENAI:
                self.selector.current(index)
                self.select_profile()
                return
        self.new_profile()
        self.name.set(name)
        self.base.set(base)
        self.model_picker.focus_set()

    def set_imported_model(self, tag, title, summary):
        # Keep each imported model selectable without modifying an unrelated provider.
        base = "http://127.0.0.1:11434/v1"
        name = "Ollama · " + " ".join(title.split())
        by_name = {p["name"].casefold(): p for p in self.profiles}
        candidate, number = name, 1
        while candidate.casefold() in by_name:
            profile = by_name[candidate.casefold()]
            if profile["protocol"] == OPENAI and profile["base"] == base and profile["model"] == tag:
                break
            number += 1
            candidate = f"{name} ({number})"
        self.use_preset(candidate, base)
        self.model.set(tag)
        self.kind.set(KIND_LABELS["model"])
        self.test_model.set("")
        self.reset_choices()
        self.import_summary.set(summary + tr("。点「保存并启用」即可用于当前工作区。"))
        self.import_notice.pack(anchor="w", pady=(5, 0))
        self.studio.status.set(tr("本地模型已导入 · ") + tag)

    def scope(self):
        try:
            protocol, base, _ = api_endpoint(self.base.get(), self.protocol.get())
            return protocol, base
        except ValueError:
            return None

    def reset_choices(self):
        choices = [self.model.get()] if self.model.get() else []
        self.model_picker.configure(values=choices)

    def endpoint_changed(self, *_):
        if self.key_scope and self.key_scope != self.scope():
            self.key.set("")
            self.studio.status.set(tr("接口地址或协议已更改，请为此接口填写密钥。"))
        self.reset_choices()

    def key_changed(self, *_):
        self.key_scope = self.scope() if self.key.get() else None
        self.reset_choices()

    def load_models(self):
        protocol, base, url_model = api_endpoint(self.base.get(), self.protocol.get())
        key = api_key(self.key.get())

        def done(models):
            self.model_picker.configure(values=models)
            if not self.model.get().strip():
                if url_model:
                    self.model.set(url_model)
                elif len(models) == 1:
                    self.model.set(models[0])
            self.model_picker.focus_set()
            self.studio.status.set(tr("已读取 {} 项，可在「调用 ID」中选择。").format(len(models)))

        self.studio.job("读取 API 列表", lambda _: providers.api_models(base, key, protocol), done)

    def import_text(self, text):
        profile, key = parse_profile(text)
        existing = next((p for p in self.profiles if p["name"].casefold() == profile["name"].casefold()
                         and p["base"] == profile["base"] and p["protocol"] == profile["protocol"]), None)
        if existing:
            if not profile["model"]:
                profile["model"] = existing["model"]
            profile = {**existing, **profile}
            self.selector.current(self.profiles.index(existing))
            if key is None:
                key = self.studio.store.profile_key(existing["id"], existing["base"], existing["protocol"])
        else:
            self.selector.set(tr("导入的 API（待保存）"))
        self.fill(profile, key or "")
        tip = "已读入接口，点「保存并启用」完成导入。" if profile["model"] else "已读入接口，请读取列表或填写调用 ID。"
        self.studio.status.set(tr(tip))

    def import_dialog(self):
        win = dialog(self.studio.root, "导入 API", 475, 180)
        content = frame(win)
        content.pack(fill="both", expand=True, padx=20, pady=16)
        label(content, "导入 API 配置", 14, bold=True).pack(anchor="w")
        label(content, "支持 JSON 或每行一个字段的文本。请提供接口地址，可同时导入密钥、调用 ID 和标记。",
              10, DIM, wraplength=405, justify="left").pack(anchor="w", pady=(6, 12))

        def clipboard():
            try:
                text = win.clipboard_get()
            except tk.TclError:
                raise ValueError("剪贴板中没有文本，请先复制 API 配置。") from None
            self.import_text(text)
            win.destroy()

        def from_file():
            path = filedialog.askopenfilename(parent=win, title=tr("选择 API 配置文件"),
                    filetypes=[(tr("API 配置"), "*.json *.txt *.md"), (tr("所有文件"), "*.*")])
            if path:
                try:
                    text = Path(path).read_text(encoding="utf-8-sig")
                except UnicodeError:
                    raise ValueError("请将配置文件保存为 UTF-8 后再导入。") from None
                self.import_text(text)
                win.destroy()

        actions = frame(content)
        actions.pack(fill="x")
        button(actions, "从剪贴板导入", clipboard, "primary").pack(side="left")
        button(actions, "从文件导入", from_file).pack(side="left", padx=7)
        win.bind("<Control-v>", lambda _e: clipboard())

    def import_gguf(self):
        from gguf_ui import import_gguf_dialog
        import_gguf_dialog(self.studio, self.set_imported_model)

    def draft(self):
        profile = validated_profile({"name": self.name.get(), "protocol": self.protocol.get(),
                "base": self.base.get(), "model": self.model.get(), "kind": self.kind.get(),
                "test_model": self.test_model.get()})
        return profile, api_key(self.key.get())

    def save(self, *, activate=True):
        profile, key = self.draft()
        profile_id = self.studio.store.save_api_profile(profile, key, self.profile_id, activate=activate)
        self.refresh_profiles(profile_id)
