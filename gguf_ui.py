"""Select a GGUF weight file and register it with the user's local Ollama server."""

import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import local_models
from ui_language import tr
from widgets import DIM, action_row, button, dialog, field, frame, label


def disk_size(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def import_gguf_dialog(studio, on_imported):
    win = dialog(studio.root, "导入本地 GGUF 模型", 650, 325)
    body = frame(win)
    body.pack(fill="both", expand=True, padx=18, pady=16)
    label(body, "本地模型权重", 14, bold=True).pack(anchor="w")
    label(body, "先在本机安装并启动 Ollama，再选择可用于对话的单文件 GGUF 权重。", 10, DIM,
          wraplength=540, justify="left").pack(anchor="w", pady=(3, 9))

    def path_field(title, value, types):
        row = frame(body)
        row.pack(fill="x", pady=4)
        label(row, title, width=14).pack(side="left")
        variable = tk.StringVar(row, value=value)
        field(row, variable).pack(side="left", fill="x", expand=True)
        def browse():
            current = Path(variable.get()).expanduser()
            options = {"initialdir": str(current.parent)} if current.is_absolute() and current.parent.is_dir() else {}
            name = filedialog.askopenfilename(parent=win, title=tr(title), filetypes=types, **options)
            if name:
                variable.set(name)
        button(row, "浏览", browse).pack(side="left", padx=(6, 0))
        return variable

    weight = path_field("GGUF 权重文件", studio.prefs.get("gguf_path", ""), [("GGUF", "*.gguf *.GGUF")])
    cli = path_field("Ollama 命令", studio.prefs.get("ollama_cli", ""), [("Ollama", "*")])
    label(body, "命令路径留空将从 PATH 查找。模型保存在 Ollama 管理的本机模型库。", 9, DIM,
          wraplength=540, justify="left").pack(anchor="w", pady=(4, 8))
    details = tk.StringVar(body)
    label(body, "", 10, DIM, textvariable=details, wraplength=540, justify="left").pack(fill="x")
    def show_size(*_):
        p = Path(weight.get().strip()).expanduser()
        try:
            size = p.stat().st_size if p.is_absolute() and p.is_file() else None
        except OSError:
            size = None
        details.set(tr("源文件 {}。首次导入请为模型库预留约同等空间。无法链接文件时，系统临时目录也需容纳一份副本。相同权重可复用。").format(disk_size(size))
                    if size is not None else tr("选好文件后可查看大小。"))
    weight.trace_add("write", show_size)
    show_size()

    def start():
        model = weight.get().strip()
        executable = cli.get().strip()
        def worker(report):
            started = time.monotonic()
            tag = local_models.import_gguf(model, executable, report)
            seconds = time.monotonic() - started
            return tag, seconds, local_models.registered_size(tag)

        def ready(result):
            tag, seconds, size = result
            studio.store.set_prefs({"gguf_path": model, "ollama_cli": executable})
            studio.prefs = studio.store.prefs()
            timing = tr("导入用时 {:.1f} 秒").format(seconds)
            usage = tr("Ollama 模型大小 {}").format(disk_size(size)) if size is not None else tr("Ollama 未报告模型大小")
            win.destroy()
            on_imported(tag, Path(model).stem, timing + " · " + usage)

        studio.job("正在导入本地模型", worker, ready, parent=win, show_elapsed=True)

    actions = action_row(body)
    button(actions, "导入到 Ollama", start, "primary").pack(side="left")
    button(actions, "取消", win.destroy, "plain").pack(side="left", padx=6)

