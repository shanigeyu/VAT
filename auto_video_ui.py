"""Desktop entry point for the automatic video workflow."""

from copy import deepcopy
from pathlib import Path
from tkinter import filedialog

import auto_video
import script_timing
import speech_api
import step_models
from shot_visuals import ILLUSTRATION_STYLES
from ui_language import Choice, tr
from widgets import DIM, GREEN, action_row, box, button, editor, entry_row, frame, label, read, scroll_page


ROLE_TITLES = {"plan": "策划", "script": "文案", "shots": "分镜", "assets": "选材与动效", "images": "AI 生图"}
AUTO_PREF = "auto_model."


def estimated_time(plan):
    low, high = (script_timing.display_time(value) for value in plan["estimate"])
    return tr("AI 预计制作耗时 · {} 至 {}").format(tr(low), tr(high))


class AutoVideoPage:
    def page_auto_video(self):
        self.header("一键生成视频", "留空的要求由 AI 决定。每次开始制作会新建一个可编辑的项目。")
        state = self.p.get("auto_video", {})
        next_stage = state.get("next_stage")
        pending = next_stage in auto_video.STAGES
        inputs = state["request"] if pending else {}
        page = scroll_page(self.body)
        cards = frame(page)
        cards.pack(fill="x")
        cards.columnconfigure(0, weight=3, uniform="auto")
        cards.columnconfigure(1, weight=2, uniform="auto")
        left, form = box(cards, padx=16, pady=13)
        right, tracker = box(cards, padx=16, pady=13)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        label(form, "视频要求（选填）", 13, bold=True).pack(anchor="w", pady=(0, 7))
        topic = entry_row(form, "选题", inputs.get("topic", ""))
        duration_row = frame(form)
        duration_row.pack(fill="x", pady=4)
        label(duration_row, "预期成片时长", color=DIM, width=12).pack(side="left")
        duration = Choice(duration_row, values=[script_timing.display_time(s) for s in script_timing.PRESETS])
        if inputs.get("seconds"):
            duration.set(script_timing.display_time(inputs["seconds"]))
        duration.pack(side="left", fill="x", expand=True)
        label(form, "可输入 45 秒、1:30 或 2 分钟。", 9, DIM).pack(anchor="w", pady=(0, 5))
        style_row = frame(form)
        style_row.pack(fill="x", pady=4)
        label(style_row, "主题风格", color=DIM, width=12).pack(side="left")
        style = Choice(style_row, values=tuple(ILLUSTRATION_STYLES))
        style.set(inputs.get("style", ""))
        style.pack(side="left", fill="x", expand=True)
        label(form, "创作要求（选填）", 10, DIM).pack(anchor="w", pady=(6, 3))
        direction = editor(form, inputs.get("direction", ""), height=2)
        direction.pack(fill="x")

        label(tracker, "制作进度", 13, bold=True).pack(anchor="w", pady=(0, 7))
        rows = {}
        for stage, title in auto_video.STAGES.items():
            row = frame(tracker)
            row.pack(fill="x", pady=3)
            label(row, title, 10).pack(side="left")
            status = label(row, "", 9, DIM)
            status.pack(side="right")
            rows[stage] = status

        model_card, models_form = box(page, padx=16, pady=12)
        model_card.pack(fill="x", pady=(12, 0))
        label(models_form, "各环节模型", 13, bold=True).pack(anchor="w", pady=(0, 6))
        grid = frame(models_form)
        grid.pack(fill="x")
        saved_models = state.get("models", {}) if pending else {}
        choices = {}
        model_buttons = {}
        for col in (1, 3):
            grid.columnconfigure(col, weight=1, uniform="models")

        def refresh_role(role):
            choice = choices[role]
            if role == "images" and not choice:
                name = tr("不调用生图接口")
            else:
                try:
                    config = step_models.resolve(self.prefs, choice, self.store)
                    name = ("" if choice else tr("跟随全局") + " · ") + step_models.engine_name(config)
                except ValueError:
                    name = tr("请重新选择 API")
            model_buttons[role].configure(text=(name if len(name) < 31 else name[:29] + "…") + " ▾")

        def choose_role(role):
            def save(selection):
                choices[role] = selection
                self.store.set_pref(AUTO_PREF + role, selection)
                self.prefs[AUTO_PREF + role] = selection
                if pending and auto_video.STAGE_ORDER.index(role) >= auto_video.STAGE_ORDER.index(next_stage):
                    state.setdefault("models", {})[role] = selection.copy()
                    self.save_now()
                refresh_role(role)
                show_status(next_stage)
            page_key = "auto_video" if role == "plan" else role
            step_models.choose(self, page_key, ROLE_TITLES[role],
                               saved=choices[role], on_save=save)

        for n, (role, name) in enumerate(ROLE_TITLES.items()):
            legacy = self.prefs.get(step_models.PREF_PREFIX + auto_video.MODEL_PAGES.get(role, ""), {})
            choices[role] = saved_models.get(role, self.prefs.get(AUTO_PREF + role, legacy))
            col = 2 * (n % 2)
            row = n // 2
            label(grid, name, 10, DIM).grid(row=row, column=col, sticky="w", padx=(0 if not col else 12, 6))
            control = button(grid, "", lambda role=role: choose_role(role), "secondary")
            control.configure(anchor="w", width=12)
            control.grid(row=row, column=col+1, sticky="ew", pady=3)
            model_buttons[role] = control
            refresh_role(role)

        voice = state.get("voice_profile", "") if pending else ""
        voice_row = frame(grid)
        voice_row.grid(row=2, column=3, sticky="ew")
        label(grid, "配音", 10, DIM).grid(row=2, column=2, sticky="w", padx=(12, 6))

        def voice_name():
            if voice:
                name = Path(voice).stem
                return "GPT-SoVITS · " + (name if len(name) <= 15 else name[:14] + "…") + " ▾"
            backend = self.prefs["speech_backend"]
            if backend == "local" and not self.prefs["sovits_profile"]:
                return tr("未配置配音") + " ▾"
            return speech_api.BACKENDS[backend] + " ▾"

        def set_voice(path):
            nonlocal voice
            voice = path
            voice_button.configure(text=tr(voice_name()))
            override_button.configure(text=tr("跟随设置" if voice else "本期 JSON"))
            if pending:
                if voice:
                    state["voice_profile"] = voice
                else:
                    state.pop("voice_profile", None)
                self.save_now()

        def select_local():
            if voice:
                set_voice("")
                return
            path = filedialog.askopenfilename(
                parent=self.root, title=tr("选择 GPT-SoVITS 音色配置"), filetypes=[("JSON", "*.json")])
            if path:
                set_voice(path)

        def configure_voice():
            self.audio_profile_dialog(on_saved=lambda: set_voice(""))

        voice_button = button(voice_row, voice_name(), configure_voice)
        voice_button.configure(anchor="w", width=10)
        voice_button.pack(side="left", expand=True, fill="x")
        override_button = button(voice_row, "跟随设置" if voice else "本期 JSON", select_local, "plain")
        override_button.pack(side="left")

        result, details = box(page, padx=16, pady=13)
        heading = frame(details)
        heading.pack(fill="x")
        label(heading, "制作方案", 13, bold=True).pack(side="left")
        eta = label(heading, "", 10, GREEN, True)
        eta.pack(side="right")
        summary = label(details, "", 11, bold=True, justify="left")
        summary.pack(fill="x", pady=(8, 2))
        summary.bind("<Configure>", lambda event: summary.configure(wraplength=max(event.width - 4, 160)))
        metrics = label(details, "", 10, DIM, justify="left")
        metrics.pack(fill="x")
        metrics.bind("<Configure>", lambda event: metrics.configure(wraplength=max(event.width - 4, 160)))
        actions = frame(details)

        def show_status(stage, running=False):
            position = auto_video.STAGE_ORDER.index(stage) if stage else -1
            for i, status in enumerate(rows.values()):
                if i < position:
                    text = "已完成"
                elif i == position:
                    text = "进行中" if running else ("已中断 · 可继续" if state.get("status") == "paused" else "可继续")
                else:
                    text = "等待"
                image_route = state.get("models", {}).get("images") if stage == "done" else choices["images"]
                if auto_video.STAGE_ORDER[i] == "images" and not image_route:
                    text = "未启用"
                status.configure(text=tr(text))

        def show_plan(plan, completed=None):
            if not plan:
                result.pack_forget()
                return
            result.pack(fill="x", pady=(12, 0))
            eta.configure(text=estimated_time(plan))
            summary.configure(text=plan["title"])
            text = tr("目标成片时长 · {}  |  主题风格 · {}").format(tr(script_timing.display_time(plan["seconds"])), plan["style"])
            if completed:
                text += "\n" + tr("成片 {} 秒  |  实际制作耗时 {}  |  {}").format(
                    f"{completed['video_seconds']:.1f}", tr(script_timing.display_time(completed["elapsed_seconds"])),
                    tr(self.p["orientation"]),
                )
            metrics.configure(text=text)

        def launch(project):
            self.save_now()
            job_state = project["auto_video"]
            start_stage = job_state["next_stage"]
            if start_stage == "images" and not choices["images"] and any(
                    shot.get("art", {}).get("image_prompt") for shot in project["shots"]):
                start_stage = "assets"
            position = auto_video.STAGE_ORDER.index(start_stage)
            models = dict(job_state.get("models", {}))
            models.pop("render", None)
            for role, selection in choices.items():
                if auto_video.STAGE_ORDER.index(role) >= position:
                    models[role] = ({} if role == "images" and not selection else
                                    step_models.pin(self.prefs, selection, self.store))
            job_state.update(models=models, next_stage=start_stage)
            if voice:
                job_state["voice_profile"] = voice
            else:
                job_state.pop("voice_profile", None)
            if project is not self.p:
                self.store.save(project)
                self.p = project
                self.refresh_projects()
                self.save_now()
            snapshot = deepcopy(project)
            voice_profile = voice or self.current_voice_profile()
            self.save_now()
            actions.pack_forget()
            show_status(snapshot["auto_video"]["next_stage"], running=True)
            show_plan(snapshot["auto_video"]["plan"])

            def progress(event):
                show_status(event.stage, running=True)
                show_plan(event.plan)
                line = f"{auto_video.STAGES[event.stage]} · {event.message}"
                return line + ("\n" + estimated_time(event.plan) if event.plan else "")

            def show_result(project, message):
                self.p = project
                self.refresh_projects()
                self.go("auto_video")
                self.status.set(tr(message))

            def failed(_message):
                saved = next(p for p in self.store.projects() if p["id"] == snapshot["id"])
                show_result(saved, "制作中断，可从当前步骤继续")

            def cancelled():
                saved = next(p for p in self.store.projects() if p["id"] == snapshot["id"])
                show_result(saved, "已中断并保存，点击“继续制作”接着完成")

            self.job("一键生成视频", lambda report: auto_video.run(self.store, snapshot, self.engine, voice_profile, report),
                     lambda project: show_result(project, "一键制作完成"), show_elapsed=True,
                     on_progress=progress, on_error=failed, on_cancel=cancelled)

        controls = action_row(form)
        button(controls, "另开一期" if pending else "开始制作",
               lambda: launch(auto_video.new_job(topic.get(), duration.get(), style.get(), direction=read(direction))),
               "secondary" if pending else "primary").pack(side="left")
        if pending:
            button(controls, "继续制作", lambda: launch(self.p), "primary").pack(side="left", padx=6)
        label(form, "制作时可点“中断并保存”，重新打开项目后可继续。", 9, DIM).pack(anchor="w", pady=(8, 0))

        show_status(next_stage)
        if next_stage == "done":
            video = Path(state["video_path"])
            button(actions, "播放成片", lambda: self.open_file(video), "primary").pack(side="left")
            button(actions, "打开导出目录", lambda: self.open_folder(video.parent), "plain").pack(side="left", padx=6)
            actions.pack(fill="x", pady=(10, 0))
        show_plan(state.get("plan"), state if next_stage == "done" else None)
