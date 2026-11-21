import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import providers
import script_timing
from creative_art import AUTHORING, Artwork
from asset_reading import read_assets
from source_visuals import repo_name
from store import SHOT_SLOTS, script_units, shot_clips, video_size


@dataclass(frozen=True)
class VisualTier:
    label: str
    summary: str
    prompt: str
    batch_size: int


DEFAULT_VISUAL_TIER = "refined"
VISUAL_TIERS = {
    "quick": VisualTier(
        "轻量原创", "单一视觉焦点，重点做好一个与台词同步的动作。",
        "选一个最能讲清本镜的视觉动作，画出能辨识的场景或道具。用清晰的起势、变化和停留配合旁白。"
        "宁用剪影、几何或手绘简化复杂造型，也不要用文字代替画面。不堆叠无关装饰。", 3),
    "refined": VisualTier(
        "精制原创", "分层构图、错峰动效和缓动，兼顾清晰与表现力（推荐）。",
        "用背景、主体和前景组织空间，让各层有不同的运动节奏。把镜头分成引入、关键变化、停留三个节拍；"
        "用错峰、缓动、遮罩或焦点切换表现关键动词，不要只让整个画面一起漂移。用相邻镜的色彩或形状衔接，控制字幕区。", 2),
    "director": VisualTier(
        "导演级原创", "逐镜导演，强调表演、转场和全片视觉连贯，制作较慢。",
        "先在心中分配每镜的叙事任务、舞台调度和进出画状态。让动作有预备、发生、余韵；用道具或人物剪影演出情节，"
        "用前后景视差、遮挡、光线或匹配剪辑表现揭示和转折。延续策划的视觉母题、色彩和空间关系，让相邻镜首尾呼应。"
        "先厘清主次再加细节，不为复杂而复杂；用可运行的分组和时间关键帧实现这些变化。", 1),
}


def visual_tier_key(value):
    return value if value in VISUAL_TIERS else DEFAULT_VISUAL_TIER


@dataclass(frozen=True)
class AssetReference:
    label: str
    summary: str
    prompt: str


DEFAULT_ASSET_REFERENCE = "balanced"
ASSET_REFERENCES = {
    "independent": AssetReference(
        "自主创作", "低参考：AI 先独立构思，素材只提供灵感或补充必要实证。",
        "先不看素材，按选题和台词独立设计镜头的主体、场景和动作。再看素材，从中提炼构图、色彩或表现手法，"
        "用自己的画面重构。不要为现有素材改写镜头；只有直接服务于叙事或提供必要实证时才引用原素材。"),
    "balanced": AssetReference(
        "平衡参考", "中参考：先定画面，再把适合的素材和原创内容结合（默认）。",
        "先确定每镜需要表达的内容、动作和构图，再按用途挑选能提供信息或保持视觉连贯的素材。"
        "可将合适的素材与原创画面组合；不贴题、质量不够或重复的候选留空，用原创设计补足。"),
    "high": AssetReference(
        "高度参考", "高参考：优先用贴题的录屏、底图和素材，在其上继续原创。",
        "主动核对素材库的录屏、图片、主题和动效。候选确实能演示台词、提供实证或延续系列风格时，"
        "优先把合适的素材融入主体、底图或叠层，并保留其中需要看清的细节。"
        "不要为提高使用率而硬塞素材；缺少合适素材时转为原创。"),
}
ASSET_REFERENCE_RULE = ("素材库只是参考，不是创作边界。始终以用户要求和叙事为先，素材丰富也要独立设计画面与动作。"
                        "允许设计库外场景、人物和道具；没有贴合素材或库为空时，用原创画面完整表达，"
                        "不要凑素材或用文字卡替代画面，也不要把想象画面当成实证。")


def asset_reference_key(value):
    return value if isinstance(value, str) and value in ASSET_REFERENCES else DEFAULT_ASSET_REFERENCE


def asset_reference_prompt(value):
    reference = ASSET_REFERENCES[asset_reference_key(value)]
    return "素材库参考程度：" + reference.label + "。" + reference.prompt + ASSET_REFERENCE_RULE


def suggested_sections(units, count):
    count = min(max(1,count),len(units))
    names = {1:["正文"],2:["开场","展开与收束"],3:["开场","核心内容","收束"],
             4:["开场","问题与背景","展开与演示","收束"]}.get(count, [])
    ends = [round((i+1)*len(units)/count) for i in range(count)]
    return [{"title":names[i] if names else units[0 if i==0 else ends[i-1]][:12], "end":end}
            for i,end in enumerate(ends)]


def valid_sections(units, rows):
    if not isinstance(rows,list) or not rows:
        raise ValueError("请至少设置一个段落。")
    prev = 0
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get("title"),str) or not row["title"].strip():
            raise ValueError("每个段落都需要标题。")
        stop = row.get("end")
        if type(stop) is not int or not prev < stop <= len(units):
            raise ValueError("段落结束句号必须递增，并且在定稿范围内。")
        prev=stop
    if prev!=len(units):
        raise ValueError("最后一段须覆盖定稿的最后一句。")
    return [{"title":r["title"].strip(),"end":r["end"]} for r in rows]


def model_list(prefs,key,brief,data):
    raw=providers.generate(prefs,key,brief,data)
    try:
        rows=providers.json_answer(raw)
        if isinstance(rows,list):
            return rows
    except json.JSONDecodeError:
        pass
    raise ValueError("模型返回的列表格式有误，可以重试。")


def ai_sections(prefs,key,units,count):
    brief=('先按叙事目的划分视频段落，根据意思给每段起具体小标题。完全覆盖已编号的定稿，不改台词。'
           '按请求的段数返回 JSON 数组，每项 {"title":"段落标题","end":该段最后一句的整数编号}。')
    result=valid_sections(units,model_list(prefs,key,brief,{"段数":count,"定稿":dict(enumerate(units,1))}))
    if len(result)!=count:
        raise ValueError("模型返回的段数与所选数量不符，可重试或手动编辑。")
    return result


def slot_categories(shot,slot):
    if slot=="main":
        return {"talk"} if shot["kind"]=="talk" else {"video","image","theme"}
    return {slot}


def reference_names(assets, shot, slots=SHOT_SLOTS):
    return [assets[clip["id"]]["name"] for slot in slots for clip in shot_clips(shot, slot)
            if clip["id"] in assets]


def terms(s):
    s = s.casefold()
    words = set(re.findall(r"[a-z][a-z0-9+#.-]*", s))
    for run in re.findall(r"[\u3400-\u9fff]+", s):
        for size in (2, 3):
            words.update(run[i:i+size] for i in range(len(run)-size+1))
    return words - {"视频", "素材", "画面", "镜头", "可以", "一个", "这里", "这个", "使用", "展示", "准备",
                    "需要", "可以看", "还有", "我们", "的话", "开始", "了解", "mp4", "mov", "png", "jpg"}


def relevance(words, clue):
    return sum(2 if len(w) >= 3 or w.isascii() else 1 for w in words & clue)


def matching_frame(clue, samples):
    return max(((relevance(terms(s["text"]), clue), s["offset"]) for s in samples),
               key=lambda pair: (pair[0], -pair[1]), default=(0, 0))


def topic_names(project):
    topic = next((t for t in project.get("topics", []) if t.get("id") == project.get("topic_id")), {})
    repo = repo_name(topic.get("url"))
    if not repo:
        return set()
    name = repo.rsplit("/", 1)[-1].casefold()
    names = {name}
    for alias in re.findall(r"([\u3400-\u9fff]{2,8})\s*[（(]?\s*" + re.escape(name),
                            (topic.get("notes", "") + "\n" + topic.get("title", "")).casefold()):
        names.add(alias)
    return names


def auto_library_subjects(project, assets, root=None, report=lambda _: None):
    """Resolve unassigned voice shots without changing the user's saved edit."""
    if project.get("auto_video"):
        return project  # The automatic selection stage leaves unmatched shots for illustration.
    options = {aid: terms(Path(a["name"]).stem + " " + Path(a["path"]).parent.name)
               for aid, a in assets.items() if a["category"] in {"video","image"} and a.get("enabled", 1)
               and Path(a["path"]).is_file()}
    if not options or not any(s["kind"] == "voice" and not shot_clips(s, "main") for s in project["shots"]):
        return project
    samples = read_assets(root, assets, report)
    project = deepcopy(project)
    names = topic_names(project)
    if names:
        # A shared library may contain several episodes. Generic words such as “设置”
        # must not pull another product's demonstration into a named repository review.
        options = {aid: words for aid, words in options.items()
                   if any(name in (str(assets[aid]["path"]) + " " + " ".join(s["text"] for s in samples.get(aid, []))).casefold()
                          for name in names)}
        stills = [aid for aid in options if Path(assets[aid]["path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}]
        project["_library_illustrations"] = [
            {"url": "本机素材 / " + assets[aid]["name"], "label": Path(assets[aid]["name"]).stem, "context": "",
             "kind": "library", "path": assets[aid]["path"]} for aid in stills]
        for aid in stills:
            del options[aid]
        if not options:
            return project
    units = script_units(project["approved"])
    used = Counter(clip["id"] for shot in project["shots"] for clip in shot_clips(shot, "main"))
    for shot in project["shots"]:
        if shot["kind"] != "voice" or shot_clips(shot, "main"):
            continue
        clue = terms(shot["visual"] + " " + "".join(units[shot["start"]:shot["end"]]))
        positions = {aid: matching_frame(clue, samples.get(aid, [])) for aid in options}
        scores = {aid: relevance(words, clue) + (positions[aid][0] if positions[aid][0] >= 3 else 0)
                  for aid, words in options.items()}
        best = max(scores, key=lambda aid: (scores[aid] / (1 + used[aid] * .45), -used[aid]))
        if scores[best] >= 2:
            clip = {"id": best}
            score, offset = positions[best]
            if assets[best]["category"] == "video" and score >= 3 and offset > 0:
                clip["offset"] = offset
            shot["main"] = [clip]
            used[best] += 1
    return project


def batch_choices(result, allowed, batch, project, creative, image_enabled):
    changes, designs = [], {}
    expected = {item["shot"]-1 for item in batch}
    seen = set()
    for row in result:
        if not isinstance(row, dict) or type(row.get("shot")) is not int:
            continue
        i = row["shot"]-1
        if i not in expected or i in seen:
            continue
        seen.add(i)
        picked = {}
        for slot in ("theme", "main", "effect"):
            aid = allowed.get((i, slot, str(row.get(slot, ""))))
            if aid and aid not in picked.values():
                picked[slot] = aid
                changes.append((i, slot, aid))
        if not creative:
            continue
        shot = project["shots"][i]
        svg, prompt = row.get("svg") or "", row.get("image_prompt") or ""
        if not isinstance(svg, str) or not isinstance(prompt, str):
            raise ValueError(f"第 {i+1} 镜的 svg 和 image_prompt 应为文本。")
        svg, prompt = svg.strip(), prompt.strip()
        has_main = bool(picked.get("main") or shot_clips(shot, "main"))
        design = {}
        if image_enabled and prompt and not has_main:
            design["image_prompt"] = prompt
        if not svg:
            raise ValueError(f"第 {i+1} 镜缺少原创动效。素材或静态图片只能辅助，请补交可播放的 SVG。")
        try:
            art = Artwork(svg)
            size = video_size(project["orientation"], 90)
            if not art.has_visible_motion(size):
                raise ValueError("画面在镜头时钟内没有可见变化，请让与台词有关的主体或叠层动起来。")
        except (ValueError, OverflowError, RuntimeError) as error:
            raise ValueError(f"第 {i+1} 镜原创动效有误：{error}") from None
        design.update(svg=svg, layer="effect" if has_main or design.get("image_prompt") else "main")
        designs[i] = design
    if creative and seen != expected:
        missing = "、".join(str(i+1) for i in sorted(expected - seen))
        raise ValueError(f"视觉方案缺少第 {missing} 镜。")
    return changes, designs


def visual_batches(prefs, key, p, assets, samples=None, *, video_style="", creative=False, image_enabled=False,
                   report=lambda _: None):
    samples = samples or {}
    catalog={aid:a for aid,a in assets.items() if a.get("enabled",1) and Path(a["path"]).is_file()}
    if not catalog and not creative:
        return
    units=script_units(p["approved"])
    changes=[]
    words={aid:terms(a["name"]+" "+Path(a["path"]).parent.name) for aid,a in catalog.items()}
    brief=('按镜头台词、画面说明、素材文件名和所属目录选材。每镜只填给出的空槽。主体优先用能实际演示台词的素材。'
           '主题是底图。动效只在有对应动作时选，例如有数值比较才放柱图，有指认目标才放圈选，段落切换可用转场。'
           '可用编号按本机画面识字与名称的相关度排序，识字命中项优先。其余槽留空。'
           '同一主体连续复用须有内容连续的理由，参考前面镜头已使用的素材。仅使用该镜该槽候选编号。'
           '返回 JSON 数组，每项 {"shot":镜头号,"main":"候选编号","theme":"候选编号","effect":"候选编号"}。')
    if creative:
        state = p.get("auto_video", {})
        request = state.get("request", {})
        tier = VISUAL_TIERS[visual_tier_key(request.get("visual_tier"))]
        reference = asset_reference_key(request.get("asset_reference"))
        brief = ("你是本片的动画导演和制作人。先按台词与创作要求自行构思，再交付真实会动的画面。"
                 "故事或情绪镜头用场景、动作和光影讲述，不要做复述台词的文字卡、字幕模板或无关图表。"
                 "返回 JSON 数组，每项必填 shot（输入镜头号）和原创动画 svg。可选 main、theme、effect 引用本镜槽位的素材编号。"
                 "素材或生图作底时，SVG 应透明且补上关键动作；独立原创时，SVG 本身画出背景、主体和动作。"
                 + asset_reference_prompt(reference)
                 + tier.prompt
                 + AUTHORING)
        if image_enabled:
            brief += "本期已启用生图模型。需要摄影、质感插画或复杂场景时可写 image_prompt 作主体，并可用 svg 加透明动效。"
        else:
            brief += "本次可用库素材与本地 SVG 绘制。库外构想用 SVG 实现。"
    batch_size = 10
    if creative:
        batch_size = tier.batch_size if catalog or image_enabled else 1
    pending = []
    for i, shot in enumerate(p["shots"]):
        art = shot.get("art") or {}
        if creative and art.get("svg") and not (art.get("image_prompt") and not image_enabled and not art.get("image_file")):
            continue
        pending.append((i, shot))
    for first in range(0, len(pending), batch_size):
        batch=[]
        aliases={}
        allowed={}
        for i,s in pending[first:first+batch_size]:
            txt="".join(units[s["start"]:s["end"]])
            clue=terms(s["visual"]+" "+txt)
            slots={}
            existing = {clip["id"] for slot in SHOT_SLOTS for clip in shot_clips(s, slot)}
            for slot in SHOT_SLOTS:
                clips = shot_clips(s, slot)
                if len(clips) > 1 or any(clip["id"] in catalog for clip in clips):
                    continue
                categories = {"video","image"} if slot == "main" and s["kind"] == "voice" else slot_categories(s,slot)
                candidates=[aid for aid,a in catalog.items() if aid not in existing and a["category"] in categories]
                candidates.sort(key=lambda a:relevance(words[a], clue) + matching_frame(clue, samples.get(a, []))[0],
                                reverse=True)
                slots[slot]=[]
                for aid in candidates[:24]:
                    alias=aliases.setdefault(aid,str(len(aliases)+1))
                    slots[slot].append(alias)
                    allowed[i,slot,alias]=aid
            if creative or any(slots.values()):
                hits = [alias for aid, alias in aliases.items() if matching_frame(clue, samples.get(aid, []))[0] >= 3]
                item = {"shot":i+1,"台词":txt,"画面":s["visual"],"可用编号":slots,"识字命中":hits}
                if creative:
                    item["旁白估计秒数"] = script_timing.estimated_speech(txt)
                if creative and existing:
                    item["已有素材"] = reference_names(catalog, s)
                batch.append(item)
        if not batch:
            continue
        files={a:[catalog[k]["name"],Path(catalog[k]["path"]).parent.name] for k,a in aliases.items()}
        recent = [catalog[aid]["name"] for _, slot, aid in changes[-24:] if slot == "main"]
        data = {"镜头":batch,"素材":files,"前序主体":recent}
        if video_style:
            data["本期主题风格"] = video_style
        if creative:
            data["动效提示词档位"] = tier.label
            data["素材库参考程度"] = ASSET_REFERENCES[reference].label
            data["画幅"] = p["orientation"]
            data["创作意图"] = (state.get("plan") or {}).get("angle", "")
            data["相邻镜头"] = [p["shots"][n]["visual"] for n in (batch[0]["shot"]-2, batch[-1]["shot"])
                               if 0 <= n < len(p["shots"])]
            if intent := request.get("direction"):
                data["用户创作要求"] = intent
        single = creative and len(batch) == 1
        instructions = brief
        if single:
            instructions += "本次只制作一个镜头。若用 SVG 独立完成画面，也可直接返回完整 SVG，无需封装 JSON。"
        if creative and len(p["shots"]) > 1:
            report("正在设计镜头 " + "、".join(str(item["shot"]) for item in batch) + f" / {len(p['shots'])}")
        for attempt in range(2 if creative else 1):
            # A provider error belongs to the caller. Only retry a malformed creative deliverable.
            raw = providers.generate(prefs, key, instructions, data)
            try:
                standalone = (re.fullmatch(r"\s*(?:```(?:svg|xml)?\s*)?(?:<\?xml\s+[^?]*\?>\s*)?"
                                           r"(<svg\b[\s\S]*</svg>)\s*(?:```)?\s*",
                                           raw, re.I) if single else None)
                result = ([{"shot": batch[0]["shot"], "svg": standalone[1]}] if standalone else
                          providers.json_answer(raw))
                if isinstance(result, dict) and creative:
                    result = result.get("shots", [result] if "shot" in result else None)
                if not isinstance(result, list):
                    raise ValueError("模型返回的选材列表格式有误。")
                selected, artwork = batch_choices(result, allowed, batch, p, creative, image_enabled)
            except ValueError as error:
                if not creative or attempt:
                    raise ValueError(f"无法使用视觉方案：{error}") from None
                data["上次需修正的问题"] = str(error)
                continue
            changes.extend(selected)
            yield selected, artwork
            break


def choose_assets(prefs, key, p, assets, samples=None):
    return [change for batch, _ in visual_batches(prefs, key, p, assets, samples) for change in batch]


def apply_asset_choices(project, changes, samples):
    units = script_units(project["approved"])
    for i, slot, aid in changes:
        shot = project["shots"][i]
        previous = shot_clips(shot, slot)
        shot[slot] = [{**(previous[0] if previous else {}), "id": aid}]
        if slot == "main" and shot["kind"] == "voice" and not previous:
            clue = terms(shot["visual"] + " " + "".join(units[shot["start"]:shot["end"]]))
            score, offset = matching_frame(clue, samples.get(aid, []))
            if score >= 3:
                shot[slot][0]["offset"] = offset
