import hashlib
import json
import re


DEFAULT_SECONDS = 40
PRESETS = (30, DEFAULT_SECONDS, 45, 60, 90, 120, 180, 300)
# Changing the pace invalidates AI advice produced for an older pace.
TIMING_PROFILE = "fast-zh-v1"


def display_time(seconds):
    minutes, rest = divmod(seconds, 60)
    if not minutes:
        return f"{rest} 秒"
    if not rest:
        return f"{minutes} 分钟"
    return f"{minutes} 分 {rest} 秒"


def topic_key(topic):
    material = [TIMING_PROFILE, *(topic.get(k, "") for k in ("id", "source", "title", "notes"))]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()


# Editorial starting points. Explicit signals in the title take precedence over
# incidental mentions in the source notes.
FORMATS = (
    (r"教程|手把手|从零|实操|实战|安装教程|部署指南|一步步|step.by.step|walkthrough|tutorial",
     100, "教程要交代起点、演示关键步骤，再展示操作结果。"),
    (r"横评|对比|测评|评测|横向比较|怎么选|值得买|benchmark|compar(?:e|ison)|\bversus\b|\bvs\b",
     80, "对比题适合用同一个例子检验差异，最后交代各自适合谁。"),
    (r"盘点|清单|合集|排行榜|必备|推荐.{0,3}[款个种]|[一二三四五六七八九十\d]+(?:款|大工具|个工具|个项目)",
     60, "清单题需要逐项说明用途，并留时间给观众作选择。"),
    (r"原理|实验|研究|论文|机制|复盘|深度|调查|为什么|背后|paper|research|under.the.hood",
     100, "解释型题材需要讲清问题、证据和结论，适合留出一个具体例子。"),
    (r"快讯|上线|刚刚|发布|更新|推出|上新|新功能|release|launch|changelog",
     30, "快讯可集中讲一个变化、一个例子，以及它对观众的影响。"),
)


def recommend(project, topic):
    if not topic:
        return None
    saved = project.get("duration_advice")
    if (isinstance(saved, dict) and saved.get("topic_key") == topic_key(topic)
            and type(saved.get("seconds")) is int and saved["seconds"] > 0 and saved.get("reason")):
        return saved
    for text in (topic.get("title", ""), topic.get("notes", "")[:4000]):
        for pattern, seconds, reason in FORMATS:
            if re.search(pattern, text, re.I):
                return {"seconds": seconds, "reason": reason}
    if topic.get("source") == "GitHub":
        return {"seconds": 50, "reason": "介绍项目时，可以用一次实际演示说明它解决什么问题。"}
    return {"seconds": DEFAULT_SECONDS, "reason": "按短视频的口播节奏，用一个例子讲清本期观点，再用一句话收尾。"}


def target(project, advice):
    return project.get("target_seconds") or (advice["seconds"] if advice else None)


def estimated_speech(text):
    # Calibrated against the 40-second episode 2 captions, including their pauses.
    han = len(re.findall(r"[\u3400-\u9fff]", text))
    words = len(re.findall(r"[a-zA-Z]+(?:['’-][a-zA-Z]+)*", text))
    digits = len(re.findall(r"\d", text))
    if not (han or words or digits):
        return 0
    pauses = 0.08 * len(re.findall(r"[,，、；;：:]", text)) + 0.18 * len(re.findall(r"[。！？!?]+", text))
    return max(1, round(han / 6 + words * 0.28 + digits * 0.16 + pauses))


def draft_budget(seconds, *, synthesized=False):
    # From the local voice tests, synthesized narration needs more room for pauses.
    low, high = (4.0, 4.7) if synthesized else (5.0, 5.8)
    return round(seconds * low), round(seconds * high)
