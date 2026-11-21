import math
import tempfile
import tkinter as tk
import webbrowser
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urlsplit

import discovery
import planning
import production
import providers
import script_timing
import sovits
import speech_api
import talk_import
from api_profiles import api_key, protect_key
from store import LIBRARIES, SHOT_SLOTS, confirmed, eligible, library_extensions, library_owner, new_project, script_units, shot_clips, shots_from_plan, uid
from ui_language import Choice, tr
import appearance
from widgets import DIM, GREEN, WHITE, action_row, bind_context_menu, box, button, dialog, editor, entry_row, field, frame, label, pixels, read, scroll_page, selected, table


def pick_audio(parent, start=""):
    return filedialog.askopenfilename(parent=parent, initialdir=start, title=tr("选择声音文件或带声音的视频"),
             filetypes=[(tr("音频和视频")," ".join("*"+x for x in sorted(production.AUDIO|production.VIDEOS)))])


class WorkflowPages:
    def source_directory(self):
        folder = self.prefs.get("source_dir", "")
        return folder if folder and Path(folder).is_dir() else ""

    def douyin_window(self):
        win=dialog(self.root,"用 Ego 找抖音视频",820,480)
        body=frame(win)
        body.pack(fill="both",expand=True,padx=18,pady=15)
        label(body,"抖音视频搜索",16,bold=True).pack(anchor="w")
        setup_row = action_row(body)
        ego_status = label(setup_row, "", 9, DIM, wraplength=pixels(win, 460), justify="left")
        ego_status.pack(side="left", fill="x", expand=True)
        download = button(setup_row, "下载 Ego Lite", lambda: webbrowser.open(discovery.EGO_DOWNLOAD_URL))
        row=action_row(body)
        query=tk.StringVar(win,value=self.p.get("search_term","AI 工具"))
        q=field(row,query)
        q.pack(side="left",fill="x",expand=True)
        hint=tk.StringVar(win,value=tr("在 Ego 搜索后，可点开作品，再从这里读取。"))
        filt=Choice(row,values=("全部结果","仅超十倍","待核对"),width=10,state="readonly")
        filt.current(0)
        holder,tv=table(body,[("title","作品",250,True),("likes","点赞",75,False),
                              ("fans","粉丝",75,False),("state","十倍筛选",110,False)])
        values={r["url"]:dict(r) for r in self.p.get("discovery_rows",[])}
        def draw():
            pick=tv.selection()
            tv.delete(*tv.get_children())
            for url,r in values.items():
                a,b=discovery.page_count(r.get("likes")),discovery.page_count(r.get("followers"))
                state="待核对" if None in (a,b) else ("符合条件" if eligible({"source":"抖音","likes":a,"followers":b}) else "未达十倍")
                if filt.current()==1 and state!="符合条件" or filt.current()==2 and state!="待核对":
                    continue
                tv.insert("","end",iid=url,values=(r["title"],r.get("likes") or "—",r.get("followers") or "—",tr(state)))
            if pick and tv.exists(pick[0]):
                tv.selection_set(pick[0])
        def launch(action):
            if not refresh_ego():
                return
            term=query.get().strip()
            if action=="search" and not term:
                raise ValueError("请填写搜索关键词。")
            item=values[selected(tv,"请先选一条作品。")] if action=="inspect" else None
            if action=="batch":
                item=[record for record in values.values()
                      if any(discovery.page_count(record.get(k)) is None for k in ("likes","followers"))][:5]
                if not item:
                    raise ValueError("当前列表没有待核对作品。")
            project,space=self.p["id"],self.p.get("ego_space")
            detail_page=self.p.get("ego_detail")
            def done(data):
                self.p["ego_space"]=data["space"]
                self.p["ego_detail"]=data.get("detailPage")
                self.p["search_term"]=term
                if action=="search" and data.get("rows"):
                    values.clear()
                for r in data.get("rows",[]):
                    prior=values.setdefault(r["url"],{})
                    prior.update({k:v for k,v in r.items() if v not in ("",None)})
                if data.get("profile") and data.get("followers"):
                    profile=urlsplit(data["profile"]).path.rstrip("/")
                    for record in values.values():
                        if urlsplit(record.get("author","")).path.rstrip("/")==profile:
                            record["followers"]=data["followers"]
                self.p["discovery_rows"]=list(values.values())
                self.save_now()
                draw()
                if data.get("rows") and not data.get("gate"):
                    hint.set(tr(f"本次读取 {len(data['rows'])} 条，当前筛选显示 {len(tv.get_children())} 条。"+data["status"]))
                else:
                    hint.set(tr(data["status"]))
            self.job("连接 Ego 浏览器",lambda _:discovery.browse(project,space,action,term,item,detail_page),done,win)
        search = button(row,"在 Ego 搜索",lambda:launch("search"),"primary")
        search.pack(side="left",padx=6)
        capture = button(row,"读取当前页",lambda:launch("capture"))
        capture.pack(side="left")
        filt.pack(side="left",padx=5)
        filt.bind("<<ComboboxSelected>>",lambda _:draw())
        q.bind("<Return>",lambda _e:launch("search"))
        label(body,"",9,DIM,textvariable=hint,wraplength=pixels(win,740),justify="left").pack(anchor="w",pady=8)
        holder.pack(fill="both",expand=True)
        draw()
        def add():
            r=values[selected(tv,"请先选择一条作品。")]
            previous=next((t for t in self.p["topics"] if t["url"]==r["url"]),None)
            if previous is None:
                previous={"id":uid(),"source":"抖音","url":r["url"],"title":r["title"],"notes":r["title"],
                          "likes":None,"followers":None}
                self.p["topics"].append(previous)
            for name in ("likes","followers"):
                metric=discovery.page_count(r.get(name))
                if metric is not None:
                    previous[name]=metric
            self.save_now()
            self.topic_filter="全部选题"
            win.destroy()
            self.go("topics")
            self.topic_dialog(previous)
        actions=action_row(body)
        inspect = button(actions,"核对作品点赞与作者粉丝",lambda:launch("inspect"))
        inspect.pack(side="left")
        batch = button(actions,"核对前 5 条待核对作品",lambda:launch("batch"))
        batch.pack(side="left",padx=4)
        button(actions,"加入候选并校对",add,"primary").pack(side="right")

        def refresh_ego():
            installed = bool(discovery.ego_cli())
            ego_status.configure(
                text=tr("已检测到 Ego Lite" if installed else discovery.EGO_SETUP_HINT),
                fg=GREEN if installed else DIM,
            )
            for control in (search, capture, inspect, batch):
                control.configure(state="normal" if installed else "disabled")
            if installed:
                download.pack_forget()
            else:
                download.pack(side="right", padx=(0, 4))
            return installed

        button(setup_row, "重新检测", refresh_ego, "plain").pack(side="right")
        refresh_ego()

    def section_dialog(self):
        if not confirmed(self.p):
            raise ValueError("请先确认本期文案。")
        units=script_units(self.p["approved"])
        win=dialog(self.root,"划分视频段落",760,570)
        b=frame(win)
        b.pack(fill="both",expand=True,padx=18,pady=14)
        label(b,"划分视频段落",16,bold=True).pack(anchor="w")
        label(b, "给每段命名并指定最后一句，下一段从随后一句开始。",9,DIM).pack(anchor="w",pady=4)
        script=editor(b,"\n".join(f"{i}. {s}" for i,s in enumerate(units,1)),5)
        script.configure(state="disabled")
        script.pack(fill="both",expand=True)
        row=action_row(b)
        existing=self.p["sections"]
        initial=len(existing) or min(len(units),4 if len(units)>=8 else 3)
        count=ttk.Combobox(row,width=5,values=list(range(1,min(len(units),max(12,initial))+1)),state="readonly")
        count.set(initial)
        label(row,"段数",9,DIM).pack(side="left",padx=(0,6))
        count.pack(side="left")
        label(b,"每行填写「最后一句的编号  段落标题」",9,DIM).pack(anchor="w",pady=(10,4))
        edit=editor(b,"",5)
        edit.pack(fill="both",expand=True)
        def show(rows):
            edit.delete("1.0","end")
            edit.insert("1.0","\n".join(f"{s['end']}  {s['title']}" for s in rows))
            count.set(len(rows))
        show(existing or planning.suggested_sections(units,initial))
        count.bind("<<ComboboxSelected>>",lambda _:show(planning.suggested_sections(units,int(count.get()))))
        def ai():
            prefs,key=self.engine("shots")
            n=int(count.get())
            self.job("按内容划分段落",lambda _:planning.ai_sections(prefs,key,units,n),show,win)
        button(row,"AI 按内容分段",ai).pack(side="left",padx=8)
        def save():
            rows=[]
            for line in read(edit).splitlines():
                if not line.strip(): continue
                cells=line.strip().split(maxsplit=1)
                if len(cells)!=2 or not cells[0].isdigit():
                    raise ValueError("每行先填结束句编号，空一格后填写段落标题。")
                rows.append({"end":int(cells[0]),"title":cells[1]})
            rows=planning.valid_sections(units,rows)
            ends={s["end"] for s in self.p["shots"]}
            if ends and any(s["end"] not in ends for s in rows):
                if not messagebox.askyesno(tr("重新拆镜"),tr("新段落边界落在现有镜头内。保存后需要重新拆镜，继续？"),parent=win):
                    return
                self.p["shots"]=[]
            self.p["sections"]=rows
            win.destroy()
            self.go("shots")
        button(action_row(b),"保存段落",save,"primary").pack(side="right")

    def page_shots(self):
        self.header("分镜编排","先划分段落，再编排镜头。双击镜头可添加多个素材并调整使用顺序。")
        units=script_units(self.p["approved"])
        sections=self.p["sections"]
        row=frame(self.body)
        row.pack(fill="x",pady=(0,9))
        button(row,"划分段落",self.section_dialog,"primary" if not sections else "secondary").pack(side="left")
        def make(ai):
            if not sections or not confirmed(self.p):
                self.section_dialog()
                return
            if self.p["shots"] and not messagebox.askyesno(tr("重新拆镜"),tr("重新生成会替换已有镜头及其素材关联，继续？"),parent=self.root):
                return
            def done(shots):
                self.p["shots"]=shots
                self.go("shots")
            if ai:
                prefs,key=self.engine("shots")
                assets=self.store.assets(self.p["id"])
                script=self.p["approved"]
                target = script_timing.target(self.p, script_timing.recommend(self.p,self.current_topic()))
                self.job("逐段生成分镜",lambda _:providers.storyboard(prefs,key,script,assets,sections,target),done)
            else:
                done(shots_from_plan(units,[{"end":i+1,"kind":"voice","visual":text} for i, text in enumerate(units)]))
        button(row,"AI 逐段拆镜",lambda:make(True),"primary" if sections else "secondary").pack(side="left",padx=6)
        button(row,"按句拆镜",lambda:make(False)).pack(side="left")
        label(row,f"{len(sections)} 段  /  {len(self.p['shots'])} 镜",9,DIM).pack(side="right")
        assets=self.store.assets(self.p["id"])
        holder,tv=table(self.body,[("text","定稿台词",148,True),("visual","画面说明",148,True),("ref","引用素材",108,True)])
        tv.configure(show="tree headings")
        tv.heading("#0",text=tr("段落 / 镜头"),anchor="w")
        tv.column("#0",width=pixels(tv,160),minwidth=100,stretch=False)
        holder.pack(fill="both",expand=True)
        start=0
        for si,sec in enumerate(sections):
            grp=f"section-{si}"
            tv.insert("","end",iid=grp,text=sec["title"],open=True,values=(f"第 {start+1}–{sec['end']} 句","",""))
            for i,s in enumerate(self.p["shots"]):
                if start<=s["start"]<sec["end"]:
                    names=planning.reference_names(assets,s)
                    art = s.get("art") or {}
                    if art.get("image_file") and not shot_clips(s, "main"):
                        names.append(tr("AI 生图"))
                    if art.get("svg") and (art.get("layer") == "effect" or not shot_clips(s, "main")):
                        names.append(tr("原创动效 / 图形"))
                    auto_subject = s["kind"] == "voice" and not (shot_clips(s, "main") or shot_clips(s, "theme")
                                   or art.get("image_file") or (art.get("svg") and art.get("layer") != "effect"))
                    materials = " / ".join(([tr("自动配主体画面")] if auto_subject else []) + names) or tr("待关联")
                    if len(names) > 1:
                        materials = tr("{} 项 · {}").format(len(names), materials)
                    tv.insert(grp,"end",iid=str(i),text=tr(f"镜头 {i+1:02d}"),values=(
                        "".join(units[s["start"]:s["end"]]),s["visual"],materials))
            start=sec["end"]
        def edit():
            if not confirmed(self.p):
                raise ValueError("请先确认当前文案。")
            value=selected(tv,"请先选择段落或镜头。")
            if value.startswith("section-"): self.section_dialog()
            else: self.shot_dialog(int(value))
        def choose():
            if not confirmed(self.p) or not self.p["shots"]:
                raise ValueError("请先定稿并拆镜。")
            prefs,key=self.engine("shots")
            p=deepcopy(self.p)
            def match(report):
                samples = planning.read_assets(self.store.root, assets, report)
                return planning.choose_assets(prefs,key,p,assets,samples), samples
            def done(result):
                changes, samples = result
                planning.apply_asset_choices(self.p, changes, samples)
                self.go("shots")
                self.status.set(tr(f"已匹配 {len(changes)} 个素材位置，空缺的配音镜头将在合成时自动配图"))
            self.job("按镜头匹配可用素材", match, done)
        tv.bind("<Double-1>",lambda e:edit() if tv.identify_row(e.y) and not tv.identify_element(e.x,e.y).endswith("indicator") else None)
        bar=action_row(self.body)
        button(bar,"编辑镜头",edit).pack(side="left")
        button(bar,"AI 为镜头选材",choose).pack(side="left",padx=5)
        button(bar,"打开素材库",lambda:self.go("assets"),"plain").pack(side="left")
        button(bar,"去音频制作 →",lambda:self.go("audio"),"primary").pack(side="right")

    def asset_source_actions(self, assets, category, folder_ids=None):
        locations = sorted({Path(asset["path"]).parent for asset in assets
                            if asset["category"] == category
                            and (folder_ids is None or asset["folder_id"] in folder_ids)})
        if not locations:
            return [("暂无源文件夹", None)]
        if len(locations) == 1:
            return [("打开源文件夹", lambda: self.open_folder(locations[0]))]
        return [(f"打开 {path}", lambda path=path: self.open_folder(path)) for path in locations]

    def page_assets(self):
        self.header("素材库", "将素材拖到左侧文件夹来整理，也可从资源管理器拖入文件或文件夹。")
        cat, pid = self.library, self.p["id"]
        scope = cat, library_owner(cat, pid)
        assets = {}
        row = frame(self.body)
        row.pack(fill="x", pady=(0, 10))

        def change(key):
            self.library = key
            self.go("assets")

        tabs = {}
        for key, name in LIBRARIES.items():
            tab = button(row, name, lambda key=key: change(key), "primary" if key == cat else "secondary")
            tab.pack(side="left", padx=(0, 5))
            tabs[key] = tab
            bind_context_menu(tab, lambda _event, key=key: self.asset_source_actions(assets.values(), key))

        panes = frame(self.body)
        panes.pack(fill="both", expand=True)
        folder_holder, folders = table(panes, [])
        folder_holder.pack(side="left", fill="y", padx=(0, 8))
        folders.configure(show="tree headings")
        folders.heading("#0", text=tr("文件夹"), anchor="w")
        folders.column("#0", width=pixels(folders, 168), anchor="w")
        folders.tag_configure("drop-target", background=appearance.current["tint"], foreground=appearance.current["primary"])
        folder_icon = tk.PhotoImage(master=folders, width=pixels(folders, 16), height=pixels(folders, 16))
        folder_icon.put(appearance.current["primary"], to=tuple(pixels(folders, n) for n in (1, 3, 8, 8)))
        folder_icon.put(appearance.current["primary"], to=tuple(pixels(folders, n) for n in (1, 5, 15, 14)))
        folder_icon.put(appearance.current["tint"], to=tuple(pixels(folders, n) for n in (2, 7, 14, 13)))
        root_node = "library-root"
        folders.insert("", "end", iid=root_node, text=tr("根目录"), image=folder_icon, open=True)
        for folder in self.store.asset_folders(cat, pid):
            folders.insert(folder["parent_id"] or root_node, "end", iid=folder["id"],
                           text=folder["name"], image=folder_icon)
        current = self.library_locations.get(scope, root_node)
        if not folders.exists(current):
            current = root_node
        folders.selection_set(current)
        folders.focus(current)
        folders.see(current)

        holder, tv = table(panes, [("name", "文件名称", 175, True), ("state", "编排状态", 82, False),
                                  ("path", "源文件夹", 195, True)])
        tv.configure(selectmode="extended")
        holder.pack(side="left", fill="both", expand=True)

        def folder_id(node):
            return "" if node == root_node else node

        def show_files():
            tv.delete(*tv.get_children())
            destination = folder_id(current)
            for aid, asset in assets.items():
                if asset["category"] == cat and asset["folder_id"] == destination:
                    tv.insert("", "end", iid=aid, values=(asset["name"], tr("可选用" if asset["enabled"] else "已禁用"),
                                                           str(Path(asset["path"]).parent)))

        def reload_assets():
            nonlocal assets
            assets = self.store.assets(pid)
            for key, tab in tabs.items():
                count = sum(asset["category"] == key for asset in assets.values())
                tab.config(text=tr(f"{LIBRARIES[key]}  {count}"))
            show_files()

        def select_folder(_event):
            nonlocal current
            selection = folders.selection()
            if selection and selection[0] != current:
                current = self.library_locations[scope] = selection[0]
                show_files()

        folders.bind("<<TreeviewSelect>>", select_folder)

        def folder_actions(event):
            node = folders.identify_row(event.y)
            if not node:
                return []
            if node == root_node:
                return self.asset_source_actions(assets.values(), cat)
            folder_ids = {node}
            pending = list(folders.get_children(node))
            while pending:
                child = pending.pop()
                folder_ids.add(child)
                pending.extend(folders.get_children(child))
            return self.asset_source_actions(assets.values(), cat, folder_ids)

        bind_context_menu(folders, folder_actions)

        def new_folder():
            name = simpledialog.askstring(tr("新建文件夹"), tr("文件夹名称"), parent=self.root)
            if name is None:
                return
            name = name.strip()
            fid = self.store.create_asset_folder(cat, pid, name, folder_id(current))
            folders.insert(current, "end", iid=fid, text=name, image=folder_icon)
            folders.see(fid)
            self.status.set(tr("已新建文件夹「{}」").format(name))

        button(row, "+ 新建文件夹", new_folder).pack(side="right")

        def folder_at(event):
            x = event.x_root - folders.winfo_rootx()
            y = event.y_root - folders.winfo_rooty()
            if 0 <= x < folders.winfo_width() and 0 <= y < folders.winfo_height():
                return folders.identify_row(y)
            return ""

        pressed = None
        dragging = False
        hovered = ""
        drag_threshold = pixels(tv, 6)

        def highlight(target):
            nonlocal hovered
            if hovered == target:
                return
            if hovered:
                folders.item(hovered, tags=())
            if target:
                folders.item(target, tags=("drop-target",))
            hovered = target

        def press(event):
            nonlocal pressed
            # Let Tk handle Shift and Ctrl clicks for multiple selection.
            if event.state & 0x0005 or tv.identify_region(event.x, event.y) != "cell":
                return
            aid = tv.identify_row(event.y)
            selection = tv.selection()
            keep_selection = aid in selection and len(selection) > 1
            pressed = (aid, selection if keep_selection else (aid,), event.x_root, event.y_root)
            if keep_selection:
                tv.focus(aid)
                tv.focus_set()
                return "break"

        def drag(event):
            nonlocal dragging
            if pressed is None:
                return
            _, _, x, y = pressed
            if not dragging:
                if max(abs(event.x_root - x), abs(event.y_root - y)) < drag_threshold:
                    return
                dragging = True
                tv.configure(cursor="fleur")
            target = folder_at(event)
            highlight(target if target != current else "")
            return "break"

        def release(event):
            nonlocal pressed, dragging
            if pressed is None:
                return
            aid, ids, _, _ = pressed
            pressed = None
            if not dragging:
                if len(ids) > 1:
                    tv.selection_set(aid)
                return
            dragging = False
            tv.configure(cursor="")
            highlight("")
            target = folder_at(event)
            if target and target != current:
                moved = self.store.move_assets(cat, pid, ids, folder_id(target))
                reload_assets()
                self.status.set(tr("已将 {} 项素材移到「{}」").format(moved, folders.item(target, "text")))
            return "break"

        tv.bind("<ButtonPress-1>", press)
        tv.bind("<B1-Motion>", drag)
        tv.bind("<ButtonRelease-1>", release)

        exts = library_extensions(cat)

        def accept(paths, destination):
            if not paths or self.active_job is not None:
                return
            def done(numbers):
                reload_assets()
                self.status.set(tr(f"已新增 {numbers[0]} 项，跳过 {numbers[1]} 项重复素材"))
            self.job("导入素材", lambda _: self.store.import_paths(cat, pid, paths, exts, destination), done)

        if self.root.tk.call("info", "commands", "tkdnd::drop_target"):
            def dropped(event):
                paths = self.root.tk.splitlist(event.data)
                destination = folder_id(folder_at(event) or current)
                self.root.after_idle(accept, paths, destination)
                return "copy"

            for w in (folder_holder, folders, holder, tv):
                w.drop_target_register("DND_Files")
                w.dnd_bind("<<Drop>>", dropped)

        def select_files():
            paths = filedialog.askopenfilenames(parent=self.root, initialdir=self.source_directory(), title=tr("导入素材"),
                                              filetypes=[(tr("可用素材"), " ".join("*"+s for s in sorted(exts)))])
            accept(paths, folder_id(current))

        def import_folder():
            path = filedialog.askdirectory(parent=self.root, initialdir=self.source_directory(), title=tr("选择素材文件夹"))
            if path:
                accept([path], folder_id(current))

        def pick():
            ids = tv.selection()
            if len(ids) != 1:
                raise ValueError("请选择一项素材进行此操作。")
            return assets[ids[0]]

        def disable():
            asset = pick()
            self.store.query("UPDATE assets SET enabled=? WHERE id=?", (int(not asset["enabled"]), asset["id"]))
            reload_assets()
            tv.selection_set(asset["id"])

        def remove():
            asset = pick()
            for p in self.store.projects():
                if any(clip["id"] == asset["id"] for s in p["shots"] for slot in SHOT_SLOTS
                       for clip in shot_clips(s, slot)):
                    raise ValueError(f"《{p['title']}》正在使用它，请先解除关联。")
            self.store.query("DELETE FROM assets WHERE id=?", (asset["id"],))
            reload_assets()

        def assign():
            selection = set(tv.selection())
            self.assign_assets([assets[aid] for aid in tv.get_children() if aid in selection])

        def open_asset(event):
            aid = tv.identify_row(event.y)
            if aid:
                self.open_file(assets[aid]["path"])

        def file_actions(event):
            aid = tv.identify_row(event.y)
            return self.asset_source_actions([assets[aid]], cat) if aid else []

        bind_context_menu(tv, file_actions)
        tv.bind("<Double-1>", open_asset)
        bar = action_row(self.body)
        button(bar, "分配到镜头…", assign, "primary").pack(side="left")
        button(bar, "导入文件", select_files).pack(side="left", padx=4)
        button(bar, "导入文件夹", import_folder).pack(side="left")
        button(bar, "启用 / 禁用", disable, "plain").pack(side="left", padx=4)
        button(bar, "移出库", remove, "plain").pack(side="right")
        reload_assets()

    def assign_assets(self, chosen):
        if not chosen:
            raise ValueError("请先选择要分配的素材。")
        if any(not asset["enabled"] for asset in chosen):
            raise ValueError("请先启用所选素材。")
        if not confirmed(self.p) or not self.p["shots"]:
            raise ValueError("请先定稿并编排镜头。")
        win=dialog(self.root,"分配素材到镜头",720,410)
        body=frame(win)
        body.pack(fill="both",expand=True,padx=16,pady=12)
        title = chosen[0]["name"] if len(chosen) == 1 else tr(f"已选 {len(chosen)} 项素材")
        label(body,title,13,bold=True,localize=False).pack(anchor="w")
        cat=chosen[0]["category"]
        slot={"theme":"theme","effect":"effect","video":"main","image":"main","talk":"main"}[cat]
        note = "真人出镜画面及原声" if cat == "talk" else SHOT_SLOTS[slot]
        label(body, f"勾选要追加素材的镜头，应用位置为{note}。", 9, DIM).pack(anchor="w", pady=6)
        holder,tv=table(body,[("check","选用",52,False),("n","镜头",58,False),("visual","画面说明",210,True),("old","当前素材",140,True)])
        holder.pack(fill="both",expand=True)
        assets=self.store.assets(self.p["id"])
        selected_ids = [asset["id"] for asset in chosen]
        selected_set = set(selected_ids)
        initial, partial = set(), set()
        for i,s in enumerate(self.p["shots"]):
            if cat in {"video","image"} and s["kind"]=="talk": continue
            linked = {clip["id"] for clip in shot_clips(s, slot)}
            if selected_set <= linked:
                initial.add(i)
            elif selected_set & linked:
                partial.add(i)
            names = planning.reference_names(assets, s, (slot,))
            state = "☑" if i in initial else "部分" if i in partial else "☐"
            tv.insert("","end",iid=str(i),values=(state,f"{i+1:02d}",s["visual"]," / ".join(names) or "—"))
        picked = initial.copy()
        def toggle(key):
            if not key: return
            i=int(key)
            if i in picked: picked.remove(i)
            else: picked.add(i)
            tv.set(key,"check","☑" if i in picked else "部分" if i in partial else "☐")
        tv.bind("<ButtonRelease-1>",lambda e:toggle(tv.identify_row(e.y)))
        tv.bind("<space>",lambda _:toggle(tv.focus()))
        def apply():
            for i in picked-initial:
                s=self.p["shots"][i]
                clips = shot_clips(s, slot)
                if cat == "talk" and s["kind"] != "talk":
                    s.update(kind="talk", voice_source="camera")
                    clips = []
                linked = {clip["id"] for clip in clips}
                s[slot] = clips + [{"id": aid} for aid in selected_ids if aid not in linked]
            for i in initial-picked:
                s = self.p["shots"][i]
                s[slot] = [clip for clip in shot_clips(s, slot) if clip["id"] not in selected_set]
            self.save_now()
            win.destroy()
            self.status.set(tr("「{}」已关联 {} 个镜头").format(title, len(picked)))
        button(action_row(body),"应用到勾选镜头",apply,"primary").pack(side="right")

    def page_audio(self):
        self.header("音频制作", "按分镜生成混音和四条对齐的 WAV 分轨。" if self.p["shots"]
                    else "确认定稿后可以生成整篇配音，也可以导入带原声的口播视频。")
        actions = frame(self.body)
        actions.pack(fill="x", pady=(0, 10))
        nav = frame(self.body)
        nav.pack(side="bottom", fill="x", pady=(10, 0))
        button(nav, "去字幕制作 →", lambda: self.go("captions"), "plain").pack(side="right")
        sound=self.p["sound"]
        tabs=ttk.Notebook(self.body)
        tabs.pack(fill="both",expand=True)
        panes=[]
        for title in ("素材声","背景声","文案声"):
            outer=tk.Frame(tabs,bg=WHITE)
            tabs.add(outer,text="  "+tr(title)+"  ")
            inner=frame(outer)
            inner.pack(fill="both",expand=True,padx=15,pady=13)
            panes.append(inner)
        tabs.select(getattr(self,"audio_tab",0))
        def volume(parent,name,initial):
            row=frame(parent)
            row.pack(fill="x",pady=4)
            label(row,name,10,width=14).pack(side="left")
            var=tk.IntVar(row,value=initial)
            tk.Scale(row,from_=0,to=100,variable=var,showvalue=False,orient="horizontal",
                     length=170,sliderlength=15,bg=WHITE,highlightthickness=0).pack(side="left")
            label(row,"",9,DIM,textvariable=var,width=3).pack(side="left",padx=(6,0))
            label(row,"% ",9,DIM).pack(side="left")
            return var
        def source(parent,caption,initial):
            row=action_row(parent)
            label(row,caption,10,width=12).pack(side="left")
            var=tk.StringVar(row,value=initial)
            e=field(row,var)
            e.configure(state="readonly")
            e.pack(side="left",expand=True,fill="x")
            def browse():
                path=pick_audio(self.root, self.source_directory())
                if path: var.set(path)
            button(row,"选择文件",browse,"plain").pack(side="left")
            button(row,"清除",lambda:var.set(""),"plain").pack(side="left")
            return var
        a,b,c=panes
        label(a,"引用素材的原声与音效",13,bold=True).pack(anchor="w")
        clips=tk.BooleanVar(a,value=sound["clip_audio"])
        tk.Checkbutton(a,text=tr("使用各镜头中引用视频和动效的原声"),variable=clips,bg=WHITE,
                       activebackground=WHITE).pack(anchor="w",pady=(10,3))
        fx=source(a,"附加音效",self.p["sfx"])
        mat_gain=volume(a,"素材声音量",self.p["sfx_volume"])
        label(b,"背景音乐与环境声",13,bold=True).pack(anchor="w")
        label(b,"所选音频从片头开始，循环铺到片尾。",9,DIM).pack(anchor="w",pady=(5,0))
        bg=source(b,"背景音轨",sound["background"])
        bg_gain=volume(b,"背景声音量",sound["bg_gain"])
        profile_row=frame(c)
        profile_row.pack(fill="x",pady=(0,4))
        label(profile_row, "配音方式", 12, bold=True).pack(side="left", padx=(0, 12))
        backend = Choice(profile_row, state="readonly", values=speech_api.BACKENDS.values(), width=30)
        backend.current(tuple(speech_api.BACKENDS).index(self.prefs["speech_backend"]))
        backend.pack(side="left")
        voice_area = frame(c)
        voice_area.pack(fill="x", pady=(0, 5))
        voice_panels = {route: frame(voice_area) for route in speech_api.BACKENDS}
        api_voice_panel = voice_panels["api"]
        saved_speech = speech_api.Profile(**self.prefs["speech_api"])
        voice = entry_row(api_voice_panel, "音色", saved_speech.voice)
        voice_hint = ("填写 ElevenLabs 的 Voice ID，会随文案一起发送到配音接口。"
                      if saved_speech.provider == "elevenlabs" else "填写服务支持的音色名称或 ID，会随文案一起发送到配音接口。")
        label(api_voice_panel, voice_hint, 9, DIM).pack(anchor="w")
        address = saved_speech.endpoint or tr("尚未设置，请点右上角“配音设置”。")
        endpoint_label = label(api_voice_panel, tr("配音接口：") + address, 9, DIM, localize=False, justify="left")
        endpoint_label.pack(fill="x", pady=(3, 0))
        endpoint_label.bind("<Configure>", lambda event: endpoint_label.configure(wraplength=max(160, event.width - 4)))
        label(voice_panels["local"], "本机音色请在“配音设置”中选择 GPT-SoVITS 音色 JSON。", 9, DIM).pack(anchor="w")
        audio_model = speech_api.AudioModel(**self.prefs["speech_llm"])
        model_summary = (tr("音频模型 · ") + audio_model.model if audio_model.model
                         else tr("请在“配音设置”连接支持音频输出的大语言模型。"))
        label(voice_panels["llm"], model_summary, 9, DIM, localize=False).pack(anchor="w")

        def show_voice_input(*_):
            for panel in voice_panels.values():
                panel.pack_forget()
            voice_panels[tuple(voice_panels)[backend.current()]].pack(fill="x")

        backend.bind("<<ComboboxSelected>>", show_voice_input)
        show_voice_input()
        ai_gain=volume(c,"AI / 合成文案",sound["ai_gain"])
        human_gain=volume(c,"本人文案",sound["human_gain"])
        holder,tv=table(c,[("n","镜头",55,False),("source","文案声来源",160,False),("text","本镜台词",175,True),("file","声音文件",110,True)],4)
        holder.pack(fill="both",expand=True,pady=(5,0))
        units=script_units(self.p["approved"])
        assets=self.store.assets(self.p["id"])
        for i,s in enumerate(self.p["shots"]):
            k=production.voice_source(s)
            name=(" / ".join(planning.reference_names(assets, s, ("main",))) or tr("待选口播视频")) if k=="camera" else (
                Path(s.get("voice_file","")).name if k!="synth" else tr("按句生成"))
            tv.insert("","end",iid=str(i),values=(i+1,tr(production.VOICE_NAMES[k]),"".join(units[s["start"]:s["end"]]),name))
        def edit():
            self.save_now()
            self.audio_source_dialog(int(selected(tv,"请选择要设置声音的镜头。")))
        tv.bind("<Double-1>",lambda _:edit())
        button(action_row(c),"设置本镜声音",edit).pack(side="left")
        def capture():
            self.audio_tab=tabs.index("current")
            self.p.update(sfx=fx.get(),sfx_volume=mat_gain.get())
            sound.update(clip_audio=clips.get(),background=bg.get(),bg_gain=bg_gain.get(),
                         ai_gain=ai_gain.get(),human_gain=human_gain.get())
            prefs = {}
            route = tuple(speech_api.BACKENDS)[backend.current()]
            if route != self.prefs["speech_backend"]:
                prefs["speech_backend"] = route
            if voice.get().strip() != self.prefs["speech_api"].get("voice", ""):
                prefs["speech_api"] = {**self.prefs["speech_api"], "voice": voice.get().strip()}
            if prefs:
                self.store.set_prefs(prefs)
                self.prefs.update(prefs)
        self.capture=capture
        def tracks():
            if not self.p["shots"]:
                self.voice()
                return
            self.save_now()
            p=deepcopy(self.p)
            voice_profile=self.current_voice_profile()
            self.job("生成音频与分轨",
                     lambda report:production.export_tracks(self.store.root,p,assets,report,voice_profile=voice_profile),
                     self.exported)
        button(actions, "生成音频", tracks, "primary").pack(side="left")
        button(actions, "导入 SRT 和口播视频", self.talk_import_dialog).pack(side="left", padx=8)
        if self.p["shots"]:
            button(actions, "仅生成定稿配音", self.voice, "plain").pack(side="left")
        button(actions, "配音设置", self.audio_profile_dialog).pack(side="right")

    def talk_import_dialog(self):
        self.save_now()
        project = deepcopy(self.p)
        use_current = not (project["draft"].strip() or project["approved"] or project["shots"])
        can_match = confirmed(project) and bool(project["shots"])
        win = dialog(self.root, "导入 SRT 和口播视频", 820, 525)
        body = frame(win)
        body.pack(fill="both", expand=True, padx=18, pady=14)
        label(body, "选择带原声的视频，以及使用同一时间轴的 SRT。", 9, DIM).pack(anchor="w", pady=(4, 2))

        def choose_file(caption, pattern):
            row = action_row(body)
            label(row, caption, 10, DIM, width=10).pack(side="left")
            path = tk.StringVar(win)
            entry = field(row, path)
            entry.configure(state="readonly")
            entry.pack(side="left", fill="x", expand=True)
            def choose():
                name = filedialog.askopenfilename(parent=win, initialdir=self.source_directory(), title=tr(f"选择{caption}"), filetypes=[(tr(caption), pattern)])
                if name:
                    path.set(name)
            button(row, "选择文件", choose, "plain").pack(side="right")
            return path

        srt = choose_file("SRT 字幕", "*.srt")
        video = choose_file("口播视频", " ".join(f"*{ext}" for ext in sorted(production.VIDEOS)))
        modes = action_row(body)
        mode = tk.StringVar(win, value="match" if can_match else "full")
        options = [("match", "AI 匹配当前分镜")] if can_match else []
        options.append(("full", "为当前项目建立口播分镜" if use_current else "另建口播项目"))
        for value, text in options:
            tk.Radiobutton(modes, text=tr(text), value=value, variable=mode, bg=body["bg"],
                           activebackground=body["bg"]).pack(side="left", padx=(0, 12))

        actions = frame(body)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        note = tk.StringVar(win)
        label(body, "", 9, DIM, textvariable=note, wraplength=pixels(win, 745), justify="left").pack(
            fill="x", pady=(7, 6))
        holder, tv = table(body, [("use", "选用", 50, False), ("shot", "放入位置", 86, False),
                                  ("time", "源视频时间码", 190, False), ("text", "字幕预览", 230, True)], 5)
        holder.pack(fill="both", expand=True)
        prepared = None
        clips = {}
        picked = set()

        def reset(*_):
            nonlocal prepared
            prepared = None
            clips.clear()
            picked.clear()
            tv.delete(*tv.get_children())
            apply_button.configure(state="disabled")
            preview_button.configure(text=tr("AI 匹配并预览" if mode.get() == "match" else "读取并预览"))
            note.set(tr("AI 按字幕台词寻找对应镜头。选用后替换该镜头的主体画面和文案声。"
                     if mode.get() == "match" else "将使用完整口播视频和原声，按 SRT 时间码加入字幕。"))

        def display(result):
            nonlocal prepared
            prepared, matches = result
            if mode.get() == "match":
                clips.update(matches)
                picked.update(clips)
                if not clips:
                    note.set(tr("没有找到完整匹配的镜头。可以选择另建口播项目。"))
                    return
                note.set(tr(f"匹配到 {len(clips)} 个镜头。应用会替换勾选镜头的主体画面和文案声，并重新计算受影响的字幕时间。"))
                for index, clip in clips.items():
                    start = round(clip["offset"] * 1000)
                    end = start + round(clip["length"] * 1000)
                    time = f"{production.srt_time(start)} – {production.srt_time(end)}"
                    text = " ".join(cue["text"].replace("\n", " ") for cue in clip["srt"])
                    tv.insert("", "end", iid=str(index), values=("☑", tr(f"镜头 {index + 1}"), time, text))
            else:
                for i, cue in enumerate(prepared.cues):
                    time = f"{production.srt_time(cue['start'])} – {production.srt_time(cue['end'])}"
                    tv.insert("", "end", iid=str(i), values=("", tr("口播分镜"), time, cue["text"].replace("\n", " ")))
                note.set(tr(f"已读取 {len(prepared.cues)} 条字幕。应用后可在「字幕制作」校对。"))
            apply_button.configure(state="normal")

        def preview():
            srt_path, video_path = srt.get(), video.get()
            if not srt_path or not video_path:
                raise ValueError("请同时选择 SRT 字幕和口播视频。")
            reset()
            match = mode.get() == "match"
            if match:
                prefs, key = self.engine("audio")
            def work(report):
                report("正在检查字幕时间码和视频原声")
                source = talk_import.prepare(srt_path, video_path)
                if match:
                    report("AI 正在匹配字幕台词和分镜")
                    return source, talk_import.match_shots(prefs, key, project, source)
                return source, {}
            self.job("匹配口播分镜" if match else "读取口播素材", work, display, win)

        def toggle(row):
            if not row or mode.get() != "match":
                return
            index = int(row)
            picked.symmetric_difference_update({index})
            tv.set(row, "use", "☑" if index in picked else "☐")
            apply_button.configure(state="normal" if picked else "disabled")

        def apply():
            if prepared is None:
                return
            p = deepcopy(project)
            if mode.get() == "match":
                selected_clips = {i: clips[i] for i in sorted(picked)}
                if not selected_clips:
                    return
            else:
                if not use_current:
                    p = new_project(prepared.path.stem)
                selected_clips = talk_import.build_timeline(p, prepared)
            self.store.save_talk_import(p, prepared.path, selected_clips)
            self.capture = None
            self.p = p
            if p["id"] != project["id"]:
                self.refresh_projects()
            self.audio_tab = 2
            win.destroy()
            self.go("audio")
            self.status.set(tr(f"已为 {len(selected_clips)} 个镜头加入口播画面、原声和字幕"))

        tv.bind("<ButtonRelease-1>", lambda e: toggle(tv.identify_row(e.y)) if tv.identify_column(e.x) == "#1" else None)
        tv.bind("<space>", lambda _: toggle(tv.focus()))
        preview_button = button(actions, "读取并预览", preview)
        preview_button.pack(side="left")
        apply_button = button(actions, "应用画面、原声和字幕", apply, "primary")
        apply_button.pack(side="right")
        for variable in (srt, video, mode):
            variable.trace_add("write", reset)
        reset()

    def current_voice_profile(self):
        backend = self.prefs["speech_backend"]
        if backend == "api":
            return speech_api.Profile(**self.prefs["speech_api"])
        if backend == "llm":
            return speech_api.AudioModel(**self.prefs["speech_llm"])
        return self.prefs["sovits_profile"]

    def audio_profile_dialog(self, *, on_saved=None):
        self.save_now()
        win = dialog(self.root, "配音设置", 700, 590)
        actions = frame(win)
        actions.pack(side="bottom", fill="x", padx=18, pady=(6, 15))
        content = frame(win)
        content.pack(fill="both", expand=True, padx=18, pady=(15, 0))
        body = scroll_page(content)
        row = frame(body)
        row.pack(fill="x", pady=(0, 10))
        label(row, "配音方式", color=DIM, width=12).pack(side="left")
        mode = Choice(row, state="readonly", values=speech_api.BACKENDS.values(), width=30)
        mode.current(tuple(speech_api.BACKENDS).index(self.prefs["speech_backend"]))
        mode.pack(side="left")

        panels = frame(body)
        panels.pack(fill="x")
        local, api, llm = frame(panels), frame(panels), frame(panels)
        route_panels = {"local": local, "api": api, "llm": llm}
        path = entry_row(local, "音色配置文件", self.prefs["sovits_profile"])
        label(local, "请选择你自己的 GPT-SoVITS 音色 JSON，填写参考音频、原话和本机服务地址。",
              9, DIM).pack(anchor="w", pady=5)

        def choose():
            selected_path = filedialog.askopenfilename(parent=win, title=tr("选择音色配置"), filetypes=[(tr("音色配置"), "*.json")])
            if selected_path:
                path.set(selected_path)

        local_actions = action_row(local)
        button(local_actions, "选择 JSON", choose).pack(side="left")
        button(local_actions, "清空选择", lambda: path.set(""), "plain").pack(side="left")

        def secret_fields(parent, saved):
            secret_row = frame(parent)
            secret_row.pack(fill="x", pady=4)
            label(secret_row, "API 密钥", color=DIM, width=12).pack(side="left")
            secret = tk.StringVar(win)
            field(secret_row, secret, "●").pack(side="left", fill="x", expand=True)
            keep = tk.BooleanVar(win, value=bool(saved.encrypted_key))
            if saved.encrypted_key:
                tk.Checkbutton(parent, text=tr("密钥留空时沿用同一接口已保存的密钥"), variable=keep,
                               bg=parent.cget("bg"), activebackground=parent.cget("bg")).pack(anchor="w")
            return secret, keep

        saved = speech_api.Profile(**self.prefs["speech_api"])
        service_row = frame(api)
        service_row.pack(fill="x", pady=4)
        label(service_row, "语音服务", color=DIM, width=12).pack(side="left")
        provider = Choice(service_row, state="readonly", values=speech_api.PROVIDERS.values(), width=24)
        provider.current(tuple(speech_api.PROVIDERS).index(saved.provider))
        provider.pack(side="left")
        official = button(service_row, "打开 ElevenLabs",
                          lambda: webbrowser.open("https://elevenlabs.io/app/developers"), "plain")
        endpoint = entry_row(api, "接口地址", saved.endpoint)
        model = entry_row(api, "语音模型", saved.model)
        voice = entry_row(api, "音色", saved.voice)
        voice_help = label(api, "", 9, DIM)
        voice_help.pack(anchor="w")
        secret, keep_key = secret_fields(api, saved)
        api_help = label(api, "", 9, DIM, wraplength=pixels(win, 630), justify="left")
        api_help.pack(anchor="w", pady=(3, 0))

        saved_llm = speech_api.AudioModel(**self.prefs["speech_llm"])
        llm_service = frame(llm)
        llm_service.pack(fill="x", pady=4)
        label(llm_service, "接入方式", color=DIM, width=12).pack(side="left")
        llm_provider = Choice(llm_service, state="readonly", values=speech_api.AUDIO_MODEL_PROVIDERS.values(), width=24)
        llm_providers = tuple(speech_api.AUDIO_MODEL_PROVIDERS)
        llm_provider.current(llm_providers.index(saved_llm.provider))
        llm_provider.pack(side="left")
        llm_endpoint = entry_row(llm, "模型接口地址", saved_llm.endpoint)
        llm_model = entry_row(llm, "音频模型 ID", saved_llm.model)
        llm_voice_row = frame(llm)
        llm_voice = entry_row(llm_voice_row, "模型音色", saved_llm.voice)
        llm_auth = frame(llm)
        llm_auth.pack(fill="x")
        llm_secret, llm_keep_key = secret_fields(llm_auth, saved_llm)
        llm_hint = label(llm, "", 9, DIM, wraplength=pixels(win, 630), justify="left")
        llm_hint.pack(anchor="w", pady=(6, 0))

        def service_changed(event=None):
            selected = tuple(speech_api.PROVIDERS)[provider.current()]
            elevenlabs = selected == "elevenlabs"
            if event is not None:
                same = selected == saved.provider
                endpoint.set(saved.endpoint if same else (speech_api.ELEVENLABS_BASE if elevenlabs else ""))
                model.set(saved.model if same else (speech_api.ELEVENLABS_MODEL if elevenlabs else ""))
                voice.set(saved.voice if same else "")
                secret.set("")
                keep_key.set(same and bool(saved.encrypted_key))
            if elevenlabs:
                voice_help.configure(text=tr("从 ElevenLabs 的声音列表复制 Voice ID，粘贴到“音色”。"))
                api_help.configure(text=tr("填写 API Key 和 Voice ID 后可测试配音。模型 ID 可修改为账号可用的模型。配音会将台词发送到 ElevenLabs。"))
                official.pack(side="right")
            else:
                voice_help.configure(text=tr("直接填写服务支持的音色名称或 ID。"))
                api_help.configure(text=tr("填写 OpenAI 兼容的 Base URL 或完整 /audio/speech 地址。配音时会将台词发送到此服务。"))
                official.pack_forget()

        provider.bind("<<ComboboxSelected>>", service_changed)
        service_changed()

        llm_defaults = {
            "openai": ("https://api.openai.com/v1", "gpt-audio-1.5", "alloy"),
            "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.5-omni-flash", "Tina"),
            "vllm": ("http://127.0.0.1:8091/v1", "Qwen/Qwen2.5-Omni-7B", ""),
        }

        def llm_service_changed(event=None):
            selected = llm_providers[llm_provider.current()]
            same = selected == saved_llm.provider and bool(saved_llm.endpoint)
            if event is not None or not same:
                values = (saved_llm.endpoint, saved_llm.model, saved_llm.voice) if same else llm_defaults[selected]
                llm_endpoint.set(values[0])
                llm_model.set(values[1])
                llm_voice.set(values[2])
                llm_secret.set("")
                llm_keep_key.set(same and bool(saved_llm.encrypted_key))
            if selected == "vllm":
                llm_voice_row.pack_forget()
                note = "先启动已加载音频输出模型的本地 vLLM-Omni。使用模型默认音色，未设置服务密钥时可留空。"
            else:
                llm_voice_row.pack(fill="x", before=llm_auth)
                note = "填支持音频输出的模型 ID、音色和服务要求的密钥。生成时会将台词发送到所填接口。"
            llm_hint.configure(text=tr(note))

        llm_provider.bind("<<ComboboxSelected>>", llm_service_changed)
        llm_service_changed()

        def draft_api():
            selected = tuple(speech_api.PROVIDERS)[provider.current()]
            profile = speech_api.Profile(endpoint.get(), model.get(), voice.get(), provider=selected).validated()
            key = api_key(secret.get())
            if keep_key.get() and profile.provider == saved.provider and profile.endpoint == saved.endpoint:
                profile = replace(profile, encrypted_key=saved.encrypted_key)
            return profile, key

        def draft_llm():
            selected = llm_providers[llm_provider.current()]
            profile = speech_api.AudioModel(llm_endpoint.get(), llm_model.get(), llm_voice.get(),
                                            provider=selected).validated()
            key = api_key(llm_secret.get())
            if llm_keep_key.get() and profile.provider == saved_llm.provider and profile.endpoint == saved_llm.endpoint:
                profile = replace(profile, encrypted_key=saved_llm.encrypted_key)
            return profile, key

        drafts = {"api": draft_api, "llm": draft_llm}
        pref_keys = {"api": "speech_api", "llm": "speech_llm"}

        def test():
            backend = tuple(route_panels)[mode.current()]
            profile, key = drafts[backend]()
            key = key or profile.credential()
            title = "测试语音 API" if backend == "api" else "测试模型配音"

            def worker(report):
                production.tool("ffmpeg")
                report("正在请求测试配音")
                with tempfile.TemporaryDirectory(prefix="vat-speech-test-") as scratch:
                    return production.synthesize_api("你好，这是一段配音测试。", Path(scratch) / "test.wav", profile, key)

            self.job(title, worker,
                     lambda seconds: messagebox.showinfo(tr(title),
                         tr("接口已返回 {:.2f} 秒配音。").format(seconds), parent=win), parent=win)

        def refresh(*_):
            for panel in route_panels.values():
                panel.pack_forget()
            backend = tuple(route_panels)[mode.current()]
            route_panels[backend].pack(fill="x")
            if backend in drafts:
                test_button.configure(text=tr("测试语音 API" if backend == "api" else "测试模型配音"))
                test_button.pack(side="left")
            else:
                test_button.pack_forget()

        def save():
            backend = tuple(route_panels)[mode.current()]
            if backend in drafts:
                profile, key = drafts[backend]()
                if key:
                    profile = replace(profile, encrypted_key=protect_key(key).hex())
                prefs = {pref_keys[backend]: asdict(profile)}
            else:
                override = path.get().strip()
                if override:
                    sovits.load_profile(override)
                prefs = {"sovits_profile": override}
            voice_prefs = {"speech_backend": backend, **prefs}
            self.store.set_prefs(voice_prefs)
            # The old page's inputs must not overwrite the newly saved dialog.
            self.capture = None
            self.prefs = self.store.prefs()
            win.destroy()
            if on_saved:
                on_saved()
            else:
                self.go(self.page)
            self.status.set(tr("配音设置已保存"))

        test_button = button(actions, "测试语音 API", test)
        button(actions, "保存设置", save, "primary").pack(side="right")
        mode.bind("<<ComboboxSelected>>", refresh)
        refresh()

    def audio_source_dialog(self,i):
        if not confirmed(self.p):
            raise ValueError("请先确认当前文案。")
        s=self.p["shots"][i]
        win=dialog(self.root,"设置本镜文案声",600,355)
        body=frame(win)
        body.pack(fill="both",expand=True,padx=18,pady=15)
        label(body,f"镜头 {i+1:02d} 的文案声",14,bold=True).pack(anchor="w")
        choices=[k for k in production.VOICE_NAMES if k!="camera" or s["kind"]=="talk"]
        sel=Choice(body,state="readonly",values=[production.VOICE_NAMES[k] for k in choices])
        sel.current(choices.index(production.voice_source(s)))
        sel.pack(fill="x",pady=8)
        path=tk.StringVar(win,value=s.get("voice_file",""))
        row=action_row(body)
        f=field(row,path)
        f.configure(state="readonly")
        f.pack(side="left",fill="x",expand=True)
        def choose():
            fname=pick_audio(win, self.source_directory())
            if fname: path.set(fname)
        choose_btn=button(row,"选择配音文件",choose)
        choose_btn.pack(side="left",padx=4)
        start=entry_row(body,"文件入点（秒）",str(s.get("voice_offset",0)))
        length=entry_row(body,"取用时长（秒）",str(s.get("voice_length",0)))
        label(body,"时长填 0 表示取到文件结尾。真人出镜镜头以所选口播片段的总时长为准。",9,DIM).pack(anchor="w",pady=6)
        def swap(*_):
            choose_btn.configure(state="normal" if choices[sel.current()] in ("ai_file","record_file") else "disabled")
        swap()
        sel.bind("<<ComboboxSelected>>",swap)
        def save():
            mode=choices[sel.current()]
            if mode in ("ai_file","record_file") and not Path(path.get()).is_file():
                raise ValueError("请选择可读取的配音文件。")
            try:
                values=[float(v.get() or 0) for v in (start,length)]
            except ValueError:
                raise ValueError("请填写有效秒数。") from None
            if any(not math.isfinite(v) or v<0 for v in values):
                raise ValueError("秒数须为非负有限数。")
            s.update(voice_source=mode,voice_file=path.get(),voice_offset=values[0],voice_length=values[1])
            win.destroy()
            self.go("audio")
        button(action_row(body),"保存声音设置",save,"primary").pack(side="right")

    def page_captions(self):
        self.header("字幕制作","导入的 SRT 按口播视频时间码定位。生成后可逐条校对。")
        row=frame(self.body)
        row.pack(fill="x",pady=(0,8))
        label(row,"成片字幕",9,DIM).pack(side="left",padx=(0,6))
        mode=Choice(row,values=production.CAPTION_MODES,state="readonly",width=18)
        mode.set(self.p["captions"]["mode"])
        mode.pack(side="left")
        label(row,"烧录样式",9,DIM).pack(side="left",padx=(12,6))
        style=Choice(row,values=production.CAPTION_STYLES,state="readonly",width=12)
        style.set(self.p["captions"].get("style",production.CAPTION_STYLES[0]))
        style.pack(side="left")
        self.capture=lambda:self.p["captions"].update(mode=mode.get(),style=style.get())
        holder,tv=table(self.body,[("n","句",44,False),("shot","镜头",53,False),("start","开始",115,False),
                                   ("end","结束",115,False),("text","字幕正文",245,True)],6)
        holder.pack(fill="both",expand=True)
        templates = production.caption_rows(self.p, apply_edits=False)
        cues = None
        end_ms=0
        assets=self.store.assets(self.p["id"])
        edits=self.p["captions"]["edits"]
        def draw():
            selected_ids=tv.selection()
            tv.delete(*tv.get_children())
            rows = templates if cues is None else cues.values()
            for n, cue in enumerate(rows, 1):
                key = str(cue["i"])
                c = {**cue, **edits.get(key, {})}
                def when(k):
                    val=c[k]
                    return production.srt_time(val) if val is not None else "待生成"
                tv.insert("","end",iid=key,values=(n,c["shot"],when("start"),when("end"),c["text"].replace("\n", " ")))
            if selected_ids and tv.exists(selected_ids[0]):
                tv.selection_set(selected_ids[0])
        draw()
        def generate(after=None):
            self.save_now()
            p=deepcopy(self.p)
            p["captions"]["edits"]={}
            voice_profile=self.current_voice_profile()
            def done(result):
                nonlocal cues, end_ms
                rows,end_ms=result
                cues = {str(c["i"]): c for c in rows}
                draw()
                if after: after()
            self.job("生成字幕时间轴",
                     lambda report:production.caption_preview(self.store.root,p,assets,report,voice_profile=voice_profile),done)
        button(row,"生成 / 刷新时间轴",generate).pack(side="right")
        def edit():
            key=selected(tv,"请先选择一条字幕。")
            def open_editor():
                if key not in cues:
                    raise ValueError("该字幕已超出所选口播片段。")
                cue={**cues[key],**edits.get(key,{})}
                win=dialog(self.root,f"校对字幕 {tv.index(key) + 1}",580,350)
                b=frame(win)
                b.pack(fill="both",expand=True,padx=16,pady=12)
                start=entry_row(b,"开始（秒）",f"{cue['start']/1000:.3f}")
                end=entry_row(b,"结束（秒）",f"{cue['end']/1000:.3f}")
                body=editor(b,cue["text"],3)
                body.pack(fill="both",expand=True,pady=6)
                def save():
                    a,btime=[round(float(v.get())*1000) for v in (start,end)]
                    text=read(body).strip()
                    if not 0<=a<btime<=end_ms or not text:
                        raise ValueError("请填写正文，以及成片范围内的有效起止时间。")
                    edits[key]={k:v for k,v in {"start":a,"end":btime,"text":text}.items() if v!=cues[key][k]}
                    self.save_now()
                    win.destroy()
                    draw()
                button(action_row(b),"保存字幕",save,"primary").pack(side="right")
            if cues is not None: open_editor()
            else: generate(open_editor)
        tv.bind("<Double-1>",lambda _:edit())
        def reset():
            i=selected(tv,"请先选择一条字幕。")
            edits.pop(i,None)
            self.save_now()
            draw()
        def export():
            self.save_now()
            p=deepcopy(self.p)
            voice_profile=self.current_voice_profile()
            self.job("导出字幕",
                     lambda report:production.export_subtitles(self.store.root,p,assets,report,voice_profile=voice_profile),
                     self.exported)
        bar=action_row(self.body)
        button(bar,"校对本条",edit).pack(side="left")
        button(bar,"还原本条",reset,"plain").pack(side="left",padx=4)
        button(bar,"导出 SRT",export,"primary").pack(side="left")
        button(bar,"去样片审看 →",lambda:self.go("review"),"plain").pack(side="right")
