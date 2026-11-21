"""Display-only Simplified / Traditional Chinese support for the desktop workspace."""

import tkinter as tk
from tkinter import ttk

import appearance
from traditional_chars import TO_TRADITIONAL


def tr(text):
    if appearance.language != "zh_TW" or not isinstance(text, str):
        return text
    # Resolve ambiguous characters using the UI's vocabulary before character conversion.
    text = text.replace("后台", "後台").replace("工作台", "工作臺").replace("画面", "畫面")
    text = text.replace("头发", "頭髮").replace("默认", "預設").replace("文件夹", "資料夾")
    return text.translate(TO_TRADITIONAL)


class Choice(ttk.Combobox):
    """Display localized built-in choices and return their original stored values."""

    def __init__(self, parent, *, values=(), **kw):
        self.keys = list(values)
        self.variable = kw.pop("textvariable", None)
        super().__init__(parent, values=[tr(x) for x in self.keys], **kw)
        if self.variable is not None:
            self.set(self.variable.get())
            trace = self.variable.trace_add("write", lambda *_: self.set(self.variable.get()))
            self.bind("<<ComboboxSelected>>", lambda _: self.variable.set(self.get()), add="+")
            self.bind("<Destroy>", lambda _: self.variable.trace_remove("write", trace), add="+")

    def configure(self, **kw):
        if "values" in kw:
            self.keys = list(kw["values"])
            kw["values"] = [tr(x) for x in self.keys]
        return super().configure(**kw)

    def set(self, value):
        super().set(tr(value))

    def get(self):
        index = self.current()
        return self.keys[index] if 0 <= index < len(self.keys) else super().get()

