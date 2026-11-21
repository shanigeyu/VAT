"""各步骤页面里的「AI 对话」：把当前页面的内容交给模型，直接用对话修改。

模型按 JSON 返回 {"reply": 回答, "changes": 建议改动}。改动先列给用户看，点「应用改动」才写回项目。
模型没按 JSON 回答时，整段文字当作回答显示，不做任何改动。
"""
import json
import tkinter as tk

import providers
from ui_language import tr
from widgets import DIM, FONT, action_row, theme_color, button, dialog, editor, frame, label, pixels, read

HISTORY_LIMIT = 40
SENT_HISTORY = 12
TEXT_LIMIT = 1000

TITLES = {"script": "文案定稿", "shots": "分镜编排", "audio": "音频制作", "cover": "封面制作"}

HINTS = {
    "script": "可以让 AI 改语气、压缩时长、换开头，或直接问写法。改好的稿子会先列出来，点「应用改动」才替换草稿。",
    "shots": "可以让 AI 改某几镜的画面说明、补原创动效（SVG）或写生图提示词。例如：“第 3 镜换成数据对比的画面”。",
    "audio": "可以告诉 AI 想要的情感和语气，例如“前半段平静，讲到结论时坚定有力”。AI 会整理成配音指令，"
             "合成文案声时随台词一起发给语音 API 或音频大模型。",
    "cover": "可以让 AI 改封面标题、副标题、角标和风格，或写一段生图的画面要求，再点「AI 生图封面」出图。",
}

BRIEFS = {
    "script": ('当前页面是「文案定稿」。用户会就口播文案和你对话。需要改稿时，在 changes.draft 里给出完整的新草稿全文'
               '（不是片段，不加标题和说明）；只是回答问题时 changes 留空。'),
    "shots": ('当前页面是「分镜编排」。输入里有全部镜头的台词、画面说明和已用素材。需要改镜头时，'
              'changes.shots 是数组，每项 {"shot":镜头编号(从1开始), "visual":"新的画面说明", '
              '"svg":"可选，完整的 SVG 原创画面或透明动效，可含 SMIL 动画", "image_prompt":"可选，生图提示词，'
              '画面里不要出现文字"}。只列需要改的镜头和字段，不改的字段不要写。'),
    "audio": ('当前页面是「音频制作」。用户会描述配音想要的情感、语气、节奏。把它整理成一段给语音合成模型的中文指令，'
              '写在 changes.voice_direction（一段话，200 字以内，只写语气、情感、语速、停顿和重音，不写台词）。'
              '用户想清空情感设置时 voice_direction 写空字符串。'),
    "cover": ('当前页面是「封面制作」。需要改封面时，changes 可包含 title（主标题，宜短）、subtitle、label（角标，'
              '2–6 字）、style（只能从输入的「可选风格」中挑）、image_direction（给生图模型的背景画面要求，'
              '不要求画面出现文字）。只写需要改的字段。'),
}

FORMAT = ('只返回一个 JSON 对象：{"reply":"给用户看的简短中文回答","changes":{...}}。'
          '不需要改动时 changes 为 {}。不要输出 JSON 以外的文字。')


def brief(page):
    return BRIEFS[page] + FORMAT


def history(project, page):
    chats = project.setdefault("chats", {})
    rows = chats.get(page)
    if not isinstance(rows, list):
        rows = chats[page] = []
    return rows


def remember(project, page, role, text):
    rows = history(project, page)
    rows.append({"role": role, "text": text})
    del rows[:-HISTORY_LIMIT]


def parse(text):
    """Return (reply, changes) from the model's answer; plain text becomes the reply."""
    try:
        answer = providers.json_answer(text.strip())
    except (json.JSONDecodeError, ValueError):
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("reply", ""), str):
        return text.strip(), {}
    changes = answer.get("changes")
    reply = answer.get("reply", "").strip()
    return reply or ("已给出改动建议。" if changes else text.strip()), changes if isinstance(changes, dict) else {}


def _text(value, limit=None):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if limit else value


def normalize(page, changes, project, *, styles=()):
    """Keep only the changes this page can apply; return (changes, problems)."""
    clean, problems = {}, []
    if page == "script":
        draft = _text(changes.get("draft"))
        if draft:
            clean["draft"] = draft
    elif page == "audio":
        if "voice_direction" in changes:
            value = _text(changes["voice_direction"], TEXT_LIMIT)
            if value is not None:
                clean["voice_direction"] = " ".join(value.split())
    elif page == "cover":
        for key, limit in (("title", 60), ("subtitle", 80), ("label", 12), ("image_direction", TEXT_LIMIT)):
            value = _text(changes.get(key), limit)
            if value is not None and (value or key in ("subtitle", "label", "image_direction")):
                clean[key] = value
        style = changes.get("style")
        if isinstance(style, str) and style.strip():
            if style.strip() in styles:
                clean["style"] = style.strip()
            else:
                problems.append(f"风格「{style.strip()}」不在可选列表里，已忽略。")
    elif page == "shots":
        rows, count = [], len(project.get("shots", []))
        for row in changes.get("shots") or []:
            if not isinstance(row, dict) or type(row.get("shot")) is not int or not 1 <= row["shot"] <= count:
                problems.append("有一项改动没有写对镜头编号，已忽略。")
                continue
            item = {"shot": row["shot"]}
            for key in ("visual", "image_prompt"):
                value = _text(row.get(key), 2000)
                if value:
                    item[key] = value
            svg = _text(row.get("svg"))
            if svg:
                try:
                    from creative_art import Artwork
                    Artwork(svg)
                    item["svg"] = svg
                except Exception as error:  # 模型给的 SVG 可能不合法，只丢掉这一项
                    problems.append(f"第 {row['shot']} 镜的 SVG 无法解析（{error}），已忽略。")
            if len(item) > 1:
                rows.append(item)
        if rows:
            clean["shots"] = rows
    return clean, problems


def summary(page, changes):
    lines = []
    if page == "script" and "draft" in changes:
        draft = changes["draft"]
        lines.append(f"替换草稿（{len(draft)} 字）：{draft[:60]}{'…' if len(draft) > 60 else ''}")
    if page == "audio" and "voice_direction" in changes:
        lines.append("配音情感与语气：" + (changes["voice_direction"] or "（清空）"))
    if page == "cover":
        names = {"title": "标题", "subtitle": "副标题", "label": "角标", "style": "风格", "image_direction": "画面要求"}
        lines += [f"{names[k]}：{v or '（清空）'}" for k, v in changes.items() if k in names]
    if page == "shots":
        for row in changes.get("shots", []):
            parts = []
            if "visual" in row:
                parts.append("画面说明 → " + row["visual"][:40])
            if "svg" in row:
                parts.append("原创动效")
            if "image_prompt" in row:
                parts.append("AI 生图")
            lines.append(f"镜头 {row['shot']:02d}：" + "，".join(parts))
    return "\n".join(lines)


def ask(prefs, key, page, context, past, message, *, styles=(), project=None):
    """One chat turn. Returns (reply, changes, problems)."""
    data = {"页面": TITLES[page], "当前内容": context,
            "对话记录": [{"角色": "用户" if row["role"] == "user" else "AI", "内容": row["text"][:4000]}
                     for row in past[-SENT_HISTORY:]],
            "用户消息": message}
    raw = providers.generate(prefs, key, brief(page), data)
    reply, changes = parse(raw)
    clean, problems = normalize(page, changes, project or {}, styles=styles)
    return reply, clean, problems


def open_chat(studio, page, context, apply, *, engine_page=None, styles=(), initial=""):
    """Open the chat dialog. `context()` reads the page; `apply(changes, win)` writes the accepted changes."""
    prefs, key = studio.engine(engine_page or page)
    from step_models import engine_name
    win = dialog(studio.root, f"AI 对话 · {TITLES[page]}", 760, 600)
    body = frame(win)
    body.pack(fill="both", expand=True, padx=18, pady=14)
    head = frame(body)
    head.pack(fill="x")
    label(head, "AI 对话", 14, bold=True).pack(side="left")
    label(head, tr("对话模型 · ") + engine_name(prefs), 9, DIM, localize=False).pack(side="right")
    label(body, HINTS[page], 9, DIM, wraplength=pixels(win, 700), justify="left").pack(fill="x", pady=(4, 8))

    bottom = frame(body)
    bottom.pack(side="bottom", fill="x")
    pending_box = frame(bottom)
    pending_box.pack(fill="x", pady=(6, 0))
    pending_text = tk.StringVar(win, value="")
    pending_label = label(pending_box, "", 9, localize=False, textvariable=pending_text, justify="left",
                          wraplength=pixels(win, 560))
    pending_label.pack(side="left", fill="x", expand=True)
    apply_btn = button(pending_box, "应用改动", lambda: accept(), "primary")
    apply_btn.pack(side="right")
    message = editor(bottom, initial, 3)
    message.pack(fill="x", pady=(8, 0))
    bar = action_row(bottom)

    log = editor(body, "", 12)
    log.configure(cursor="arrow")
    log.pack(fill="both", expand=True)
    log.tag_configure("who", foreground=theme_color(DIM), font=(FONT, 9, "bold"), spacing1=8)
    log.tag_configure("ai", lmargin1=4, lmargin2=4)
    log.tag_configure("user", lmargin1=4, lmargin2=4)
    state = {"changes": {}}

    def write(role, text):
        log.configure(state="normal")
        log.insert("end", tr("你") if role == "user" else "AI", "who")
        log.insert("end", "\n" + text + "\n", role)
        log.configure(state="disabled")
        log.see("end")

    def render():
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        rows = history(studio.p, page)
        if not rows:
            write("ai", tr("你好，我能看到这一页的内容。想怎么改，直接告诉我。"))
        for row in rows:
            write(row["role"], row["text"])

    def show_pending(changes, problems=()):
        state["changes"] = changes
        text = summary(page, changes)
        notes = "\n".join(problems)
        if text:
            pending_text.set(tr("待应用：") + "\n" + text + ("\n" + notes if notes else ""))
        else:
            pending_text.set(notes)
        apply_btn.configure(state="normal" if changes else "disabled")

    def accept():
        changes = state["changes"]
        if not changes:
            return
        apply(changes, win)
        remember(studio.p, page, "ai", tr("（已应用改动）"))
        studio.store.save(studio.p)
        show_pending({})
        if win.winfo_exists():
            render()
            win.lift()

    def send(_event=None):
        text = read(message).strip()
        if not text:
            return "break"
        past = list(history(studio.p, page))
        snapshot = context()
        remember(studio.p, page, "user", text)
        write("user", text)
        message.delete("1.0", "end")

        def done(result):
            reply, changes, problems = result
            remember(studio.p, page, "ai", reply)
            studio.store.save(studio.p)
            if win.winfo_exists():
                write("ai", reply)
                show_pending(changes, problems)

        studio.job("AI 对话", lambda _report: ask(prefs, key, page, snapshot, past, text, styles=styles,
                                                 project=studio.p), done, win)
        return "break"

    def clear():
        studio.p.setdefault("chats", {})[page] = []
        studio.store.save(studio.p)
        show_pending({})
        render()

    button(bar, "发送", send, "primary").pack(side="right")
    label(bar, "Ctrl + Enter 发送", 9, DIM).pack(side="right", padx=10)
    button(bar, "清空记录", clear, "plain").pack(side="left")
    message.bind("<Control-Return>", send)
    render()
    show_pending({})
    message.focus_set()
    message.mark_set("insert", "end")
    return win
