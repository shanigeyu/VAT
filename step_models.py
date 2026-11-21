"""Per-step language model selection for the VAT workflow."""

from tkinter import ttk

import codex_harness
import providers
import generated_images
from api_profiles import KIND_LABELS
from ui_language import tr
from widgets import DIM, action_row, button, dialog, frame, label


PREF_PREFIX = "step_model."
HARNESSES = {
    "Codex": ("Codex CLI", "codex_model"),
    "DSH": ("DeepSeek Harness", "harness_model"),
}
AI_ACTIONS = {
    "auto_video": "用于选题、创作方向与制作耗时估算。",
    "script": "用于 AI 细评时长和改写文案。",
    "shots": "用于 AI 分段、逐段拆镜和为镜头选材。",
    "audio": "用于按字幕台词匹配口播分镜。",
    "assets": "用于选择库内素材，设计原创画面与动效，或向生图模型下达绘制要求。",
    "images": "选择 Codex CLI 或生图 API，按镜头生成原创图片。",
    "cover": "用于按封面标题绘制背景底图。",
}


def resolve(prefs, selection, store):
    if not selection:
        return prefs.copy()
    config = {**prefs, "engine": selection["engine"]}
    if config["engine"] == "API":
        profile = next((p for p in store.api_profiles() if p["id"] == selection["api_profile"]), None)
        if profile is None:
            raise ValueError("这一步的 API 配置已不可用，请重新选择模型。")
        config.update(api_profile=profile["id"], api_name=profile["name"], api_kind=profile["kind"],
                      protocol=profile["protocol"], base=profile["base"], model=profile["model"],
                      test_model=profile["test_model"])
        model_key = "model"
    else:
        _, model_key = HARNESSES[config["engine"]]
    if "model" in selection:
        if config["engine"] == "API" and config["model"] != selection["model"]:
            config["api_kind"] = ""
        config[model_key] = selection["model"]
    return config


def api_target_name(name, model, kind):
    tag = tr(KIND_LABELS.get(kind, "API"))
    target = f"{name} / {model}" if model and model != name else name or model
    return f"{tag} · {target}"


def engine_name(config):
    if not config["engine"]:
        return tr("未配置模型")
    if config["engine"] == "API":
        return api_target_name(config["api_name"], config["model"], config.get("api_kind", "model"))
    name, model_key = HARNESSES[config["engine"]]
    model = config[model_key]
    return f"{name} / {model}" if model else name


def pin(prefs, selection, store):
    """Persist only the selected route and model, never a credential or a full preferences object."""
    config = resolve(prefs, selection, store)
    engine = config["engine"]
    if not engine:
        raise ValueError(providers.MODEL_SETUP_REQUIRED)
    if engine == "API":
        return {"engine": engine, "api_profile": config["api_profile"], "model": config["model"]}
    return {"engine": engine, "model": config[HARNESSES[engine][1]]}


def choose(studio, page, title, *, saved=None, on_save=None):
    pref = PREF_PREFIX + page
    saved = studio.prefs.get(pref, {}) if saved is None else saved
    if page == "cover" and not saved:
        # 封面背景默认按生图出，先把「一键生成视频」里的 AI 生图通道带进来。
        saved = studio.prefs.get(PREF_PREFIX + "images", {})
    image = page in ("images", "cover")
    win = dialog(studio.root, title + " · 模型与 Harness", 550, 230)
    content = frame(win)
    content.pack(fill="both", expand=True, padx=20, pady=14)
    label(content, AI_ACTIONS.get(page, "此步骤可预先保存模型选择。"), 10, DIM).pack(anchor="w", pady=(0, 10))

    harnesses = {"Codex": HARNESSES["Codex"]} if image else HARNESSES
    default_route = "不调用生图接口" if image else "跟随全局设置"
    route_labels = [tr(default_route), *(name for name, _ in harnesses.values())]
    routes = [{}, *({"engine": name} for name in harnesses)]
    for profile in studio.store.api_profiles():
        if image and profile["protocol"] not in generated_images.PROTOCOLS:
            continue
        route_labels.append(api_target_name(profile["name"], profile["model"], profile["kind"]))
        routes.append({"engine": "API", "api_profile": profile["id"]})

    initial = {k: v for k, v in saved.items() if k != "model"}
    if initial not in routes:
        route_labels.append(tr("原 API 配置已不可用"))
        routes.append(initial)

    route_row = frame(content)
    route_row.pack(fill="x", pady=(0, 8))
    label(route_row, "调用方式", width=12).pack(side="left")
    route = ttk.Combobox(route_row, state="readonly", values=route_labels)
    route.current(routes.index(initial))
    route.pack(side="left", fill="x", expand=True)

    model_row = frame(content)
    model_row.pack(fill="x")
    label(model_row, "调用 ID", width=12).pack(side="left")
    model = ttk.Combobox(model_row)
    hint = label(content, "", 9, DIM)
    hint.pack(fill="x", pady=(6, 0))
    inherit = tr("沿用通道设置")
    cli_default = tr("使用 CLI 默认模型")

    def model_choices(models=()):
        selection = routes[route.current()]
        defaults = (inherit, cli_default) if selection.get("engine") in HARNESSES else (inherit,)
        model.configure(values=tuple(dict.fromkeys((*defaults, *models))))

    def update_model(_event=None):
        selection = routes[route.current()]
        model.configure(state="normal")
        model_choices()
        load.configure(state="normal" if selection else "disabled")
        try:
            config = resolve(studio.prefs, selection, studio.store)
        except ValueError as error:
            model.set("")
            model.configure(state="disabled")
            load.configure(state="disabled")
            hint.configure(text=tr(str(error)))
            return
        if not selection:
            model.set("" if image else engine_name(config))
            model.configure(state="disabled")
            hint.configure(text="")
            return
        override = saved.get("model") if selection == initial else None
        model.set(cli_default if override == "" else override or inherit)
        hint.configure(text=tr("通道当前使用 ") + engine_name(config))

    def load_models():
        config = resolve(studio.prefs, routes[route.current()], studio.store)
        engine = config["engine"]
        if engine == "Codex":
            path = codex_harness.cli_path(config["codex_path"])
            read = lambda: codex_harness.models(path)
        elif engine == "DSH":
            path = providers.harness_path(config["harness"])
            read = lambda: providers.dsh_models(path)
        else:
            base, protocol = config["base"], config["protocol"]
            key = studio.store.profile_key(config["api_profile"], base, protocol)
            read = lambda: providers.api_models(base, key, protocol)

        def done(models):
            model_choices(models)
            hint.configure(text=tr("已读取 {} 项，可从下拉列表选择。").format(len(models)))

        studio.job("读取模型", lambda _: read(), done, win)

    def save():
        selection = routes[route.current()].copy()
        if selection:
            config = resolve(studio.prefs, selection, studio.store)
            if image and config["engine"] == "API" and config.get("protocol") not in generated_images.PROTOCOLS:
                raise ValueError("请选择 Codex CLI 或支持生图的 API 配置。")
            value = model.get().strip()
            if value and value != inherit:
                selection["model"] = "" if value == cli_default and selection["engine"] in HARNESSES else value
        if on_save:
            on_save(selection)
        else:
            studio.store.set_pref(pref, selection)
            studio.prefs[pref] = selection
        win.destroy()
        studio.refresh_engine_label()
        studio.status.set(tr("已保存本步骤的模型设置"))

    load = button(model_row, "读取列表", load_models)
    load.pack(side="right", padx=(6, 0))
    model.pack(side="left", fill="x", expand=True)
    bar = action_row(content)
    button(bar, "保存", save, "primary").pack(side="right")
    button(bar, "取消", win.destroy, "plain").pack(side="right", padx=6)
    route.bind("<<ComboboxSelected>>", update_model)
    update_model()
