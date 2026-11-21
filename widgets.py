import math
import tkinter as tk
from tkinter import ttk, font as tkfont

import appearance
from appearance import color as theme_color
from ui_language import tr


BG = "#f5f6f2"
WHITE = "#ffffff"
INK = "#1c302b"
DIM = "#52665e"
LINE = "#d9e1d9"
DARK = "#18342e"
GREEN = "#19694f"
MINT = "#d8eedc"
FONT = "Microsoft YaHei UI"

# 柔和主题：圆角卡片 + 低对比细线 + 松绿重点。由 init_style() 按当前外观设置切换。
SOFT = False
RADIUS = 16
RADIUS_SM = 10


def sync_soft():
    """Read the presentation tokens once per shell build."""
    global SOFT, RADIUS, RADIUS_SM
    SOFT = bool(appearance.token("rounded", False))
    RADIUS = int(appearance.token("radius", 16) or 16)
    RADIUS_SM = int(appearance.token("radius_sm", 10) or 10)


def pixels(widget, value):
    factor = float(widget.tk.call("tk", "scaling")) / (96 / 72)
    return round(value * max(1.0, min(2.0, factor)))


def frame(parent, bg=None, **kw):
    return tk.Frame(parent, bg=theme_color(bg) if bg else parent.cget("bg"), **kw)


def label(parent, text, size=10, color=INK, bold=False, localize=True, **kw):
    return tk.Label(parent, text=tr(text) if localize else text, bg=parent.cget("bg"), fg=theme_color(color), bd=0,
                    font=(FONT, size, "bold" if bold else "normal"), anchor="w", **kw)


# ---------------------------------------------------------------- 圆角绘制

def round_points(x1, y1, x2, y2, radius, steps=7):
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = []
    for cx, cy, start in ((x2 - radius, y1 + radius, -90), (x2 - radius, y2 - radius, 0),
                          (x1 + radius, y2 - radius, 90), (x1 + radius, y1 + radius, 180)):
        for i in range(steps + 1):
            angle = math.radians(start + 90 * i / steps)
            points += [cx + radius * math.cos(angle), cy + radius * math.sin(angle)]
    return points


def round_rect(canvas, x1, y1, x2, y2, radius, **kw):
    return canvas.create_polygon(round_points(x1, y1, x2, y2, radius), smooth=False, **kw)


class Card(tk.Canvas):
    """圆角面板：尺寸跟随内容，或填满父容器。"""

    def __init__(self, parent, fill=None, outline=None, radius=None, padx=18, pady=16,
                 mode="content", **kw):
        bg = parent.cget("bg")
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0, **kw)
        self.fill = theme_color(fill) if fill else appearance.token("card", "#ffffff")
        self.outline = theme_color(outline) if outline else appearance.token("hairline", "#eeeeee")
        self.radius = radius if radius is not None else RADIUS
        self.padx, self.pady, self.mode = padx, pady, mode
        self.inner = tk.Frame(self, bg=self.fill)
        self._window = self.create_window(padx, pady, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_resize)
        self.inner.bind("<Configure>", self._on_content)

    def _draw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 4 or height < 4:
            return
        shadow = appearance.token("shadow", "#e7e1d3")
        self.delete("chrome")
        round_rect(self, 1, 3, width - 1, height, self.radius, fill=shadow, outline="", tags="chrome")
        round_rect(self, 0, 0, width - 1, height - 4, self.radius, fill=self.fill, outline=self.outline,
                   width=1, tags="chrome")
        self.tag_lower("chrome")
        self.itemconfigure(self._window, width=max(1, width - 2 * self.padx))

    def _on_content(self, _event=None):
        need = self.inner.winfo_reqheight() + 2 * self.pady + 4
        if self.mode == "content":
            if abs(need - self.winfo_height()) > 1:
                self.configure(height=need)
        elif need > self.winfo_height() + 1:
            self.configure(height=need)
        self._draw()

    def _on_resize(self, _event=None):
        self._draw()
        if self.mode == "parent":
            self.itemconfigure(self._window, height=max(1, self.winfo_height() - 2 * self.pady - 2))


class SoftButton(tk.Canvas):
    """圆角按钮，保留 tk.Button 常用的 configure/invoke 接口。"""

    _fonts = {}

    def __init__(self, parent, text, command=None, fill="#ffffff", ink="#000000", hover=None,
                 border="", radius=None, padx=17, pady=8, font_size=10, bold=False):
        bg = parent.cget("bg")
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0, cursor="hand2", takefocus=True)
        self._text, self._command = tr(text), command
        self._fill, self._ink, self._border = fill, ink, border
        self._hover = hover or fill
        self._radius, self._padx, self._pady = (radius if radius is not None else RADIUS_SM), padx, pady
        self._font = (FONT, font_size, "bold" if bold else "normal")
        self._state, self._hovering = "normal", False
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<Configure>", lambda _e: self._draw())
        self._resize()

    def _measure(self):
        key = (self._text, self._font)
        if key not in SoftButton._fonts:
            SoftButton._fonts[key] = tkfont.Font(family=self._font[0], size=self._font[1],
                                                 weight=self._font[2]).measure(self._text)
        return SoftButton._fonts[key]

    def _resize(self):
        # 直接调用 Canvas.configure，避免被下面过滤 tk.Button 参数的 configure 吞掉
        tk.Canvas.configure(self, width=self._measure() + 2 * self._padx,
                            height=pixels(self, self._pady * 2) + pixels(self, 20))
        self._draw()

    def _draw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 4 or height < 4:
            return
        self.delete("all")
        fill = self._hover if self._hovering else self._fill
        ink = self._ink
        if self._state == "disabled":
            fill, ink = appearance.token("soft", "#efece3"), appearance.token("faint", "#9aa2a1")
        if self._border:
            round_rect(self, 0, 0, width - 1, height - 1, self._radius, fill=fill, outline=self._border, width=1)
        else:
            round_rect(self, 0, 0, width - 1, height - 1, self._radius, fill=fill, outline="")
        self.create_text(width / 2, height / 2, text=self._text, fill=ink, font=self._font)

    def _enter(self, _event=None):
        self._hovering = True
        self._draw()

    def _leave(self, _event=None):
        self._hovering = False
        self._draw()

    def _press(self, _event=None):
        if self._state == "normal":
            self.focus_set()
            return "break"

    def _release(self, event=None):
        if self._state != "normal":
            return
        inside = event is None or (0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height())
        if inside and self._command:
            self._command()

    def _invoke(self, _event=None):
        if self._state == "normal" and self._command:
            self._command()

    def configure(self, cnf=None, **kw):
        kw = dict(cnf or {}, **kw)
        if "text" in kw:
            self._text = tr(kw.pop("text"))
            self._resize()
        if "command" in kw:
            self._command = kw.pop("command")
        if "bg" in kw:
            self._fill = kw.pop("bg")
        if "background" in kw:
            self._fill = kw.pop("background")
        if "fg" in kw:
            self._ink = kw.pop("fg")
        if "foreground" in kw:
            self._ink = kw.pop("foreground")
        if "activebackground" in kw:
            self._hover = kw.pop("activebackground")
        if "state" in kw:
            self._state = kw.pop("state")
        # tk.Button 专用的外观参数：Canvas 上没有，忽略即可（不影响布局与文字）
        for name in ("anchor", "width", "height", "font", "padx", "pady", "relief", "bd", "overrelief",
                     "activeforeground", "highlightbackground", "highlightcolor", "compound", "image",
                     "underline", "wraplength", "justify", "default"):
            kw.pop(name, None)
        if kw:
            super().configure(**kw)
        self._draw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


# ---------------------------------------------------------------- 基础控件

def button(parent, text, command, kind="secondary"):
    if not SOFT:
        colors = {"primary": (GREEN, WHITE, "#14573f"), "secondary": ("#e8eee7", INK, "#d9e5da"),
                  "plain": (parent.cget("bg"), GREEN, "#e2ede2")}
        bg, fg, hover = colors[kind]
        return tk.Button(parent, text=tr(text), command=command, bg=theme_color(bg), fg=theme_color(fg),
                         activebackground=theme_color(hover), activeforeground=theme_color(fg), font=(FONT, 10),
                         relief="flat", bd=0, cursor="hand2", padx=13, pady=6, highlightthickness=0, takefocus=True)
    fill = {"primary": appearance.token("primary", GREEN),
            "secondary": appearance.token("soft", "#e8eee7"),
            "plain": parent.cget("bg")}[kind]
    ink = {"primary": WHITE, "secondary": appearance.token("ink", INK),
           "plain": appearance.token("primary", GREEN)}[kind]
    hover = {"primary": appearance.token("hover", "#14573f"),
             "secondary": appearance.token("line", "#d9e5da"),
             "plain": appearance.token("tint", "#e2ede2")}[kind]
    return SoftButton(parent, text, command, fill=fill, ink=ink, hover=hover, radius=RADIUS_SM)


def box(parent, color=WHITE, padx=15, pady=12):
    if SOFT:
        card = Card(parent, fill=color, padx=padx + 5, pady=pady + 5, radius=RADIUS)
        return card, card.inner
    outer = frame(parent, color, highlightbackground=theme_color(LINE), highlightthickness=1)
    inside = frame(outer, color)
    inside.pack(fill="both", expand=True, padx=padx, pady=pady)
    return outer, inside


def field(parent, variable=None, show=None):
    entry = ttk.Entry(parent, textvariable=variable, font=(FONT, 10), show=show or "")
    if SOFT:
        entry.configure(style="Soft.TEntry")
    return entry


def checkbox(parent, text, variable, command=None):
    """主题化勾选框：设置开关与教程里的「不再显示」共用。"""
    bg = parent.cget("bg")
    return tk.Checkbutton(parent, text=tr(text), variable=variable, command=command, bg=bg,
                          activebackground=bg, activeforeground=theme_color(INK), fg=theme_color(INK),
                          selectcolor=appearance.token("surface", WHITE), disabledforeground=theme_color(DIM),
                          font=(FONT, 10), bd=0, highlightthickness=0, anchor="w", cursor="hand2", padx=0, pady=1)


def number_badge(parent, number, size=22):
    """圆形序号，用于教程与分步说明。"""
    size = pixels(parent, size)
    canvas = tk.Canvas(parent, width=size, height=size, bg=parent.cget("bg"), bd=0, highlightthickness=0)
    canvas.create_oval(1, 1, size - 1, size - 1, fill=appearance.token("primary", theme_color(GREEN)), outline="")
    canvas.create_text(size / 2, size / 2, text=str(number), fill="#ffffff", font=(FONT, 9, "bold"))
    return canvas


def editor(parent, value="", height=5):
    if SOFT:
        t = tk.Text(parent, font=(FONT, 10), fg=theme_color(INK), bg=appearance.token("field", WHITE),
                    insertbackground=theme_color(GREEN), wrap="word", relief="flat", borderwidth=0,
                    highlightthickness=1, highlightbackground=appearance.token("line", LINE),
                    highlightcolor=theme_color(GREEN), padx=12, pady=10, height=height, width=15,
                    undo=True, spacing1=3, spacing3=3)
        t.insert("1.0", value)
        return t
    t = tk.Text(parent, font=(FONT, 10), fg=theme_color(INK), bg=WHITE, insertbackground=theme_color(GREEN), wrap="word",
                relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=theme_color(LINE),
                highlightcolor=theme_color(GREEN), padx=10, pady=8, height=height, width=15,
                undo=True, spacing1=2, spacing3=2)
    t.insert("1.0", value)
    return t


def read(t):
    return t.get("1.0", "end-1c")


def table(parent, columns, height=6):
    if SOFT:
        wrapper = Card(parent, fill=WHITE, padx=6, pady=6, radius=RADIUS, mode="parent")
        host = wrapper.inner
        view = ttk.Treeview(host, show="headings", columns=[c[0] for c in columns],
                            selectmode="browse", height=height, style="Studio.Treeview")
        sb = ttk.Scrollbar(host, orient="vertical", command=view.yview, style="Soft.Vertical.TScrollbar")
        view.configure(yscrollcommand=sb.set)
        for name, caption, width, grow in columns:
            view.heading(name, text=tr(caption), anchor="w")
            view.column(name, width=pixels(parent, width), minwidth=pixels(parent, min(52, width)),
                        stretch=grow, anchor="w")
        sb.pack(side="right", fill="y")
        view.pack(fill="both", expand=True)
        return wrapper, view
    wrapper = frame(parent, WHITE, highlightbackground=theme_color(LINE), highlightthickness=1)
    view = ttk.Treeview(wrapper, show="headings", columns=[c[0] for c in columns],
                        selectmode="browse", height=height, style="Studio.Treeview")
    sb = ttk.Scrollbar(wrapper, orient="vertical", command=view.yview)
    view.configure(yscrollcommand=sb.set)
    for name, caption, width, grow in columns:
        view.heading(name, text=tr(caption), anchor="w")
        view.column(name, width=pixels(parent, width), minwidth=pixels(parent, min(52, width)), stretch=grow, anchor="w")
    sb.pack(side="right", fill="y")
    view.pack(fill="both", expand=True)
    return wrapper, view


def dialog(root, title, width, height):
    win = tk.Toplevel(root, bg=appearance.token("bg", theme_color(BG)))
    win.withdraw()
    win.title(tr(title))
    win.transient(root)
    width = min(pixels(root,width), root.winfo_screenwidth() - 36)
    height = min(pixels(root,height), root.winfo_screenheight() - 82)
    x = max(10, min(root.winfo_rootx() + (root.winfo_width() - width)//2, root.winfo_screenwidth() - width - 12))
    y = max(10, min(root.winfo_rooty() + (root.winfo_height() - height)//2, root.winfo_screenheight() - height - 72))
    win.geometry(f"{width}x{height}+{x}+{y}")
    win.columnconfigure(0, weight=1)
    win.rowconfigure(0, weight=1)
    win.deiconify()
    win.grab_set()
    win.bind("<Escape>", lambda _e: win.destroy())
    return win


def init_style(root):
    global FONT
    sync_soft()
    families = set(tkfont.families(root))
    FONT = next((f for f in ("Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "WenQuanYi Micro Hei")
                 if f in families), tkfont.nametofont("TkDefaultFont", root).actual("family"))
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", font=(FONT, 10))
    if SOFT:
        surface = appearance.token("card", WHITE)
        paper = appearance.token("bg", BG)
        field = appearance.token("field", "#F7F3EA")
        line = appearance.token("line", LINE)
        ink = appearance.token("ink", INK)
        dim = appearance.token("dim", DIM)
        primary = appearance.token("primary", GREEN)
        st.configure("Studio.Treeview", background=surface, fieldbackground=surface, foreground=ink,
                     borderwidth=0, relief="flat", rowheight=pixels(root, 34), font=(FONT, 10),
                     lightcolor=surface, darkcolor=surface)
        st.configure("Studio.Treeview.Heading", background=paper, foreground=dim, relief="flat",
                     padding=(10, 10), borderwidth=0, font=(FONT, 9))
        st.map("Studio.Treeview", background=[("selected", appearance.token("tint", "#d8eddf"))],
               foreground=[("selected", ink)])
        st.map("Studio.Treeview.Heading", background=[("active", paper)])
        # 输入类控件：无边框、暖色底，靠底色而不是线条区分边界
        st.configure("Soft.TEntry", fieldbackground=field, bordercolor=field, lightcolor=field, darkcolor=field,
                     padding=(10, 8), borderwidth=0, relief="flat")
        st.configure("Soft.TCombobox", padding=(10, 7), arrowsize=12, fieldbackground=field, bordercolor=field,
                     lightcolor=field, darkcolor=field, background=field, foreground=ink,
                     arrowcolor=dim, borderwidth=0, relief="flat")
        st.map("Soft.TCombobox", fieldbackground=[("readonly", field), ("disabled", paper)],
               foreground=[("readonly", ink), ("disabled", dim)],
               bordercolor=[("focus", primary), ("hover", primary)])
        st.map("Soft.TEntry", bordercolor=[("focus", primary), ("hover", primary)])
        st.configure("TEntry", fieldbackground=field, bordercolor=field, lightcolor=field, darkcolor=field,
                     padding=(10, 8), borderwidth=0, relief="flat")
        st.configure("TCombobox", padding=(10, 7), arrowsize=12, fieldbackground=field, bordercolor=field,
                     lightcolor=field, darkcolor=field, background=field, foreground=ink, arrowcolor=dim,
                     borderwidth=0, relief="flat")
        st.map("TCombobox", fieldbackground=[("readonly", field), ("disabled", paper), ("active", field)],
               foreground=[("readonly", ink), ("disabled", dim), ("active", ink)],
               background=[("readonly", field), ("active", field)],
               arrowcolor=[("disabled", line)],
               bordercolor=[("focus", primary), ("hover", primary)])
        st.configure("TMenubutton", background=field, foreground=ink, arrowcolor=dim, relief="flat",
                     borderwidth=0, padding=(14, 8))
        st.map("TMenubutton", background=[("active", appearance.token("tint", "#e3ede7")),
                                          ("pressed", appearance.token("tint", "#e3ede7"))],
               foreground=[("active", ink)])
        st.configure("Soft.Vertical.TScrollbar", troughcolor=paper, background=appearance.token("line", LINE),
                     bordercolor=paper, lightcolor=paper, darkcolor=paper, arrowcolor=paper,
                     gripcount=0, relief="flat", width=8, arrowsize=0)
        st.map("Soft.Vertical.TScrollbar", background=[("active", dim), ("pressed", dim)])
        st.configure("TProgressbar", background=appearance.token("primary", GREEN),
                     troughcolor=appearance.token("soft", "#e5ece4"), borderwidth=0, thickness=6)
        root.option_add("*TCombobox*Listbox.background", surface)
        root.option_add("*TCombobox*Listbox.foreground", ink)
        root.option_add("*TCombobox*Listbox.selectBackground", appearance.token("tint", "#d8eddf"))
        root.option_add("*TCombobox*Listbox.selectForeground", ink)
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
        root.option_add("*TCombobox*Listbox.font", (FONT, 10))
        return

    st.configure("Studio.Treeview", background=WHITE, fieldbackground=WHITE, foreground=theme_color(INK),
                 borderwidth=0, rowheight=pixels(root,32), font=(FONT, 10), lightcolor=WHITE, darkcolor=WHITE)
    st.configure("Studio.Treeview.Heading", background=theme_color("#edf1eb"), foreground=theme_color(DIM), relief="flat",
                 padding=(8, 8), borderwidth=0, font=(FONT, 9))
    st.map("Studio.Treeview", background=[("selected", theme_color("#d8eddf"))], foreground=[("selected", theme_color(INK))])
    st.configure("TCombobox", padding=4, arrowsize=12, fieldbackground=WHITE, bordercolor=theme_color(LINE))
    st.map("TCombobox", fieldbackground=[("readonly", WHITE)], foreground=[("readonly", INK)])
    st.configure("TEntry", padding=5, fieldbackground=WHITE, bordercolor=theme_color(LINE))
    st.configure("TProgressbar", background=theme_color(GREEN), troughcolor=theme_color("#e5ece4"))
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))


def glyph(parent, kind, color, bg, size=26):
    bg, color = theme_color(bg), theme_color(color)
    size = pixels(parent, size)
    c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
    scale = size / 24
    def line(*coords, width=1.7):
        c.create_line(*[x*scale for x in coords], fill=color, width=width*scale,
                      joinstyle="round", capstyle="round")
    def rect(x, y, x2, y2):
        c.create_rectangle(x*scale,y*scale,x2*scale,y2*scale,outline=color,width=1.5*scale)
    if kind == "auto_video":
        line(11,3,5,13,11,13,9,21,19,9,13,9,15,3,11,3)
    elif kind == "topics":
        c.create_oval(3*scale,3*scale,17*scale,17*scale,outline=color,width=1.8*scale)
        line(15,15,21,21)
    elif kind == "script":
        rect(4,3,19,21)
        for y, end in ((8,15),(12,15),(16,12)):
            line(8,y,end,y)
    elif kind == "shots":
        rect(2,5,22,19)
        line(9,5,9,19)
        line(9,12,22,12)
    elif kind == "assets":
        line(2,8,9,8,11,5,21,5,21,19,2,19,2,8)
        line(3,10,21,10)
    elif kind == "audio":
        for x, y in ((3,10),(7,6),(11,3),(15,7),(19,9)):
            line(x,y,x,24-y)
    elif kind == "captions":
        rect(2,5,22,19)
        line(5,12,10,12)
        line(12,12,18,12)
        line(6,15,17,15)
    elif kind == "review":
        rect(2,5,22,19)
        line(10,8,16,12,10,16,10,8)
    elif kind == "cover":
        rect(2,4,22,20)
        c.create_oval(5*scale,7*scale,9*scale,11*scale,outline=color,width=1.4*scale)
        c.create_polygon(4*scale,18*scale,9*scale,12*scale,13*scale,16*scale,
                         16*scale,13*scale,20*scale,18*scale,fill=color)
    elif kind == "output":
        line(4,13,4,20,20,20,20,13)
        line(12,15,12,3)
        line(8,7,12,3,16,7)
    elif kind == "settings":
        line(3,7,21,7)
        line(3,16,21,16)
        rect(7,4,11,10)
        rect(14,13,18,19)
    else:
        for x,y,dx,dy in ((2,3,6,0),(2,3,0,6),(22,3,-6,0),(22,3,0,6),
                          (2,21,6,0),(2,21,0,-6),(22,21,-6,0),(22,21,0,-6)):
            line(x,y,x+dx,y+dy,width=2)
        c.create_polygon(9*scale,7*scale,17*scale,12*scale,9*scale,17*scale,fill=color)
    return c

def selected(tree, tip):
    ids = tree.selection()
    if not ids:
        raise ValueError(tip)
    return ids[0]


def bind_context_menu(widget, entries_for, *children):
    menu = tk.Menu(widget, tearoff=False, font=(FONT, 10))

    def show(event):
        entries = entries_for(event)
        if not entries:
            return
        menu.delete(0, "end")
        for caption, command in entries:
            if command is None:
                menu.add_command(label=tr(caption), state="disabled")
            else:
                menu.add_command(label=tr(caption), command=command)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    for target in (widget, *children):
        target.bind("<Button-3>", show)
        if widget.tk.call("tk", "windowingsystem") == "aqua":
            target.bind("<Control-Button-1>", show)
            target.bind("<Button-2>", show)


def action_row(parent):
    row = frame(parent)
    row.pack(fill="x", pady=(10, 0))
    return row


def advanced_options(parent):
    section = frame(parent)
    section.pack(fill="x", pady=(4, 0))
    content = frame(section)

    def toggle():
        if content.winfo_manager():
            content.pack_forget()
            trigger.configure(text=tr("高级选项") + " ▸")
        else:
            content.pack(fill="x", pady=(2, 0))
            trigger.configure(text=tr("高级选项") + " ▾")

    trigger = button(section, "高级选项 ▸", toggle, "plain")
    trigger.pack(anchor="w")
    return content


def entry_row(parent, title, value=""):
    row = frame(parent)
    row.pack(fill="x", pady=4)
    label(row, title, color=DIM, width=12).pack(side="left")
    var = tk.StringVar(row, value=value)
    field(row, var).pack(side="left", fill="x", expand=True)
    return var


def scroll_page(parent):
    wrapper = frame(parent)
    wrapper.pack(fill="both", expand=True)
    canvas = tk.Canvas(wrapper, bg=wrapper["bg"], bd=0, highlightthickness=0, yscrollincrement=16)
    scrollbar = ttk.Scrollbar(wrapper, command=canvas.yview,
                              style="Soft.Vertical.TScrollbar" if SOFT else "TScrollbar")
    def sync_scrollbar(first, last):
        scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            scrollbar.pack_forget()
        elif not scrollbar.winfo_manager():
            scrollbar.pack(side="right", fill="y", before=canvas)

    canvas.configure(yscrollcommand=sync_scrollbar)
    canvas.pack(side="left", fill="both", expand=True)
    content = frame(canvas)
    window = canvas.create_window(0, 0, window=content, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
    content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))

    # Tk sends wheel events to the child under the pointer.
    def wheel(event):
        if not str(event.widget).startswith(str(wrapper)) or isinstance(event.widget, ttk.Combobox):
            return
        if canvas.yview() == (0.0, 1.0):
            return
        if event.num == 4 or event.delta > 0:
            canvas.yview_scroll(-3, "units")
        else:
            canvas.yview_scroll(3, "units")
        return "break"

    root = parent.winfo_toplevel()
    bindings = [(seq, root.bind(seq, wheel, add="+")) for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>")]

    def cleanup(event):
        if event.widget == canvas:
            for seq, ident in bindings:
                root.unbind(seq, ident)

    canvas.bind("<Destroy>", cleanup)
    return content
