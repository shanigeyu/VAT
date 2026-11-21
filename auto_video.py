"""Run the automatic workflow and checkpoint each completed handoff."""

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import planning
import production
import providers
import script_timing
import generated_images
import render_progress
import task_control
from shot_visuals import ILLUSTRATION_STYLES
from store import DEFAULT_VIDEO_RATIO, VIDEO_RATIOS, new_project, uid


STAGES = {
    "plan": "策划 Agent",
    "script": "文案 Agent",
    "shots": "分镜 Agent",
    "assets": "选材与原创动效",
    "images": "AI 生图",
    "render": "配音、字幕与成片",
}
STAGE_ORDER = (*STAGES, "done")
# Older projects and first-time users inherit the corresponding workspace step.
MODEL_PAGES = {"plan": "auto_video", "script": "script", "shots": "shots", "assets": "shots"}
MIN_SECONDS = 15
DURATION_PRESETS = (MIN_SECONDS, *script_timing.PRESETS)


@dataclass(frozen=True)
class Progress:
    stage: str
    message: str
    plan: dict | None


def duration_seconds(value):
    value = value.strip()
    if not value:
        return None
    plain = re.fullmatch(r"([0-9]+)\s*(?:秒|s)?", value, re.I)
    clock = re.fullmatch(r"([0-9]+)[:：]([0-5][0-9])", value)
    minutes = re.fullmatch(r"([0-9]+)\s*分[钟鐘]?\s*(?:([0-5]?[0-9])\s*秒)?", value)
    match = clock or minutes
    total = 0
    if plain:
        total = int(plain[1])
    elif match:
        total = int(match[1]) * 60 + int(match[2] or 0)
    if total >= MIN_SECONDS:
        return total
    raise ValueError(f"请输入至少 {MIN_SECONDS} 秒的时长，例如 15 秒、1:30 或 2 分钟。")


def new_job(topic, duration, style, *, direction="", visual_tier=planning.DEFAULT_VISUAL_TIER,
            orientation=DEFAULT_VIDEO_RATIO, asset_reference=planning.DEFAULT_ASSET_REFERENCE):
    if orientation not in VIDEO_RATIOS:
        raise ValueError("请选择有效的画幅比例。")
    request = {"topic": topic.strip(), "seconds": duration_seconds(duration), "style": style.strip()}
    request["visual_tier"] = planning.visual_tier_key(visual_tier)
    request["asset_reference"] = planning.asset_reference_key(asset_reference)
    if direction.strip():
        request["direction"] = direction.strip()
    title = " ".join(request["topic"].split())[:48] or "一键自动制作"
    project = new_project(title)
    project["orientation"] = orientation
    project["auto_video"] = {"request": request, "next_stage": "plan", "plan": None, "elapsed_seconds": 0}
    return project


def make_plan(prefs, key, request, asset_count, *, orientation=DEFAULT_VIDEO_RATIO):
    brief = (
        "按给定画幅策划一条可合成的中文视频。优先满足用户的选题、时长和创作意图，留空的部分由你创作。"
        f"如果用户未指定时长，目标成片至少 {MIN_SECONDS} 秒。"
        "可采用故事、演示、视觉隐喻或其他适合内容的形式。自定结构、构图、色彩和运动语言。"
        "叙事内容要用画面演出来，不能把旁白做成文字卡来替代。"
        "在 angle 里交代主旨、叙事和美术方向，给下游留下创作空间。估算模型调用、配音和渲染的总用时。"
        '只返回 JSON 对象 {"title":"标题","angle":"讲述角度与要点","seconds":40,'
        '"style":"自由描述风格","estimate":[预计最少秒数,预计最多秒数]}。'
    )
    data = {"用户选题": request["topic"], "预期成片秒数": request["seconds"], "主题风格": request["style"],
            "画幅": orientation, "可用本机素材数量": asset_count}
    tier = planning.VISUAL_TIERS[planning.visual_tier_key(request.get("visual_tier"))]
    reference = planning.asset_reference_key(request.get("asset_reference"))
    data["动效提示词档位"] = tier.label
    data["素材库参考程度"] = planning.ASSET_REFERENCES[reference].label
    brief += "视觉制作方向：" + tier.prompt + planning.asset_reference_prompt(reference)
    if request.get("direction"):
        data["用户创作要求"] = request["direction"]
    started = time.monotonic()
    raw = providers.generate(prefs, key, brief, data)
    planning_seconds = time.monotonic() - started
    try:
        response = providers.json_answer(raw)
    except json.JSONDecodeError:
        response = None
    if not isinstance(response, dict):
        raise ValueError("策划模型未返回有效的制作方案。")

    def clean(field):
        value = response.get(field)
        return " ".join(value.split()) if isinstance(value, str) else ""

    title, angle = clean("title"), clean("angle")
    style = request["style"] or clean("style") or "按本期创意自由设计"
    seconds = request["seconds"] or response.get("seconds", 45)
    estimate = response.get("estimate")
    palette = style if style in ILLUSTRATION_STYLES else clean("palette")
    if not title or not angle or not style or type(seconds) is not int or seconds < 1:
        raise ValueError("制作方案缺少选题、讲述角度、主题风格或目标时长。")
    if request["seconds"] is None:
        seconds = max(MIN_SECONDS, seconds)
    if (not isinstance(estimate, list) or len(estimate) != 2
            or any(type(value) is not int for value in estimate) or not 0 < estimate[0] < estimate[1]):
        estimate = [max(30, seconds), max(90, seconds*4)]
    # Account for a slow live connection before showing the model's estimate.
    remaining_calls = 3
    observed_low = planning_seconds * (1 + .55 * remaining_calls) + seconds * .3
    observed_high = planning_seconds * (1 + 1.5 * remaining_calls) + seconds * 2.5
    estimate = [max(bound, 5 * math.ceil(observed / 5))
                for bound, observed in zip(estimate, (observed_low, observed_high))]
    return {"title": title, "angle": angle, "seconds": seconds, "style": style, "estimate": estimate,
            "palette": palette if palette in ILLUSTRATION_STYLES else "简约图解"}


def run(store, project, engine, voice_profile, report):
    """Persist a resumable project on completion, failure and a user's stop."""
    state = project["auto_video"]
    started, elapsed = time.monotonic(), state.get("elapsed_seconds", 0)
    state["status"] = "running"
    state.pop("last_error", None)
    try:
        result = _run_stages(store, project, engine, voice_profile, report)
    except task_control.JobCancelled:
        state["status"] = "paused"
        raise
    except Exception as error:
        state["status"] = "failed"
        state["last_error"] = str(error) or type(error).__name__
        raise
    else:
        state["status"] = "done"
        return result
    finally:
        state["elapsed_seconds"] = round(elapsed + time.monotonic() - started)
        store.save(project)


def _run_stages(store, project, engine, voice_profile, report):
    """Run from the last saved handoff. Only completed stages advance the project."""
    state = project["auto_video"]
    request = state["request"]
    assets = store.assets(project["id"])
    catalog = {aid: asset for aid, asset in assets.items()
               if asset["category"] != "talk" and asset.get("enabled", 1) and Path(asset["path"]).is_file()}
    started, elapsed = time.monotonic(), state["elapsed_seconds"]

    def send(stage, message):
        task_control.checkpoint()
        report(Progress(stage, message, state["plan"]))
        task_control.checkpoint()

    def model(role):
        page = MODEL_PAGES.get(role, role)
        choices = state.get("models", {})
        prefs, key = engine(page, selection=choices[role]) if role in choices else engine(page)
        return prefs, key

    first = STAGE_ORDER.index(state["next_stage"])
    for index, stage in enumerate(STAGE_ORDER[first:-1], first):
        send(stage, "正在规划并估算耗时…" if stage == "plan" else "正在处理…")
        try:
            plan = state["plan"]
            if stage == "plan":
                prefs, key = model("plan")
                plan = make_plan(prefs, key, request, len(catalog), orientation=project["orientation"])
                notes = "\n".join(dict.fromkeys(filter(None, (request["topic"], plan["angle"], request.get("direction")))))
                topic = {"id": uid(), "source": "其他", "title": plan["title"], "url": "", "notes": notes}
                project.update(title=plan["title"], topics=[topic], topic_id=topic["id"],
                               target_seconds=plan["seconds"], visual_palette=plan["palette"])
                state["plan"] = plan
            elif stage == "script":
                prefs, key = model("script")
                topic = next(t for t in project["topics"] if t["id"] == project["topic_id"])
                script = providers.rewrite(prefs, key, topic, project["target_seconds"], video_style=plan["style"]).strip()
                if not script_timing.estimated_speech(script):
                    raise ValueError("文案模型未返回可朗读的正文。")
                project.update(draft=script, approved=script)
            elif stage == "shots":
                prefs, key = model("shots")
                tier = planning.VISUAL_TIERS[planning.visual_tier_key(request.get("visual_tier"))]
                reference_prompt = planning.asset_reference_prompt(request.get("asset_reference"))
                shots = providers.storyboard(prefs, key, project["approved"], catalog,
                                             target_seconds=project["target_seconds"], video_style=plan["style"],
                                             direction="\n".join(filter(None, (plan["angle"], request.get("direction")))),
                                             visual_prompt=tier.prompt, orientation=project["orientation"],
                                             reference_prompt=reference_prompt)
                sections = []
                for shot in shots:
                    title = shot.pop("section", "") or (sections[-1]["title"] if sections else plan["title"])
                    if sections and sections[-1]["title"] == title:
                        sections[-1]["end"] = shot["end"]
                    else:
                        sections.append({"title": title, "end": shot["end"]})
                project.update(sections=sections, shots=shots)
            elif stage == "assets":
                prefs, key = model("assets")
                samples = planning.read_assets(store.root, catalog, lambda text: send(stage, text)) if catalog else {}
                batches = planning.visual_batches(prefs, key, project, catalog, samples, video_style=plan["style"],
                                                  creative=True, image_enabled=bool(state.get("models", {}).get("images")),
                                                  report=lambda text: send(stage, text))
                for changes, designs in batches:
                    planning.apply_asset_choices(project, changes, samples)
                    for shot_index, design in designs.items():
                        project["shots"][shot_index]["art"] = design
                    state["elapsed_seconds"] = round(elapsed + time.monotonic() - started)
                    store.save(project)
            elif stage == "images":
                jobs = [(i, shot["art"]) for i, shot in enumerate(project["shots"], 1)
                        if shot.get("art", {}).get("image_prompt") and not (
                            shot["art"].get("image_file") and generated_images.file_path(
                                store.root, project["id"], shot["art"]["image_file"]).is_file())]
                if jobs and state.get("models", {}).get("images"):
                    prefs, key = model("images")
                    for i, art in jobs:
                        send(stage, f"正在准备第 {i} 镜的原创图片")
                        art["image_file"] = generated_images.create(store.root, project, art["image_prompt"], prefs, key)
                        state["elapsed_seconds"] = round(elapsed + time.monotonic() - started)
                        store.save(project)
            elif stage == "render":
                production.tool("ffmpeg")
                production.tool("ffprobe")
                voice = state.get("voice_profile", voice_profile)
                signature = render_progress.render_identity(store.root, project, assets, True, voice)
                ready = state.get("render_outputs", {})
                entries = ready.get("entries", [])
                if not (ready.get("signature") == signature and entries
                        and all(Path(path).is_file() for _, path in entries)):
                    entries = production.render(store.root, project, assets, True, lambda text: send(stage, text),
                                                voice_profile=voice)
                    # Exported files are complete even if the user stops during
                    # the final metadata check. Record them before probing.
                    state["render_outputs"] = {"signature": signature, "entries": entries}
                    store.save(project)
                video = Path(next(path for kind, path in entries if kind == "完整成片"))
                metadata = production.probe(video)
                state["video_seconds"] = production.media_seconds(video, metadata, "video")
                project["exports"].extend({"kind": kind, "path": path} for kind, path in entries)
                state["video_path"] = str(video)
                state.pop("render_outputs", None)
            state["next_stage"] = STAGE_ORDER[index + 1]
            state["elapsed_seconds"] = round(elapsed + time.monotonic() - started)
            store.save(project)
        except Exception as error:
            raise ValueError(f"{STAGES[stage]}未完成。\n{str(error) or type(error).__name__}") from error
    return project
