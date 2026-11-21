"""把「AI 对话」给出的镜头改动写回项目。单独成模块，方便测试。"""
from store import shot_clips


def apply_chat(project, changes):
    """Apply chat changes to shots. Returns (indices needing image generation, shot numbers skipped)."""
    pending, skipped = [], []
    for row in changes.get("shots", []):
        index = row["shot"] - 1
        shot = project["shots"][index]
        if "visual" in row:
            shot["visual"] = row["visual"]
        art = dict(shot.get("art") or {})
        has_main = bool(shot_clips(shot, "main"))
        if "image_prompt" in row:
            if has_main:
                skipped.append(row["shot"])
            else:
                art["image_prompt"] = row["image_prompt"]
                art.pop("image_file", None)
                pending.append(index)
        if "svg" in row:
            art["svg"] = row["svg"]
            art["layer"] = "effect" if has_main or art.get("image_prompt") else "main"
        elif "image_prompt" in row and art.get("svg") and not has_main:
            art["layer"] = "effect"
        if art:
            shot["art"] = art
    return pending, skipped
