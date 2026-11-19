"""Local Tk wizard. File operations run on one worker; Tk stays on its own thread."""
from filesystem import Path, extended
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import desktop_service as service
import porter
import transfer
from adapters import antigravity

MODES = {'原生对话迁入 Antigravity（实验）': antigravity.ADAPTER,
         '仅保存聊天档案（同机，不写入客户端）': 'portable'}


class Window:
    def __init__(self, root):
        self.root, self.busy = root, False
        self.events, self.controls = queue.Queue(), []
        self.progress_events = queue.Queue(maxsize=1)
        self.started_at = None
        self.projects, self.nodes, self.selected_roots, self.selected_threads = [], {}, set(), set()
        self.contexts, self.job_path, self.current_plan, self.last_session = [], None, None, None
        root.title('AgentCarry · 项目与对话迁移')
        width = min(1000, max(740, root.winfo_screenwidth() - 80))
        height = min(800, max(580, root.winfo_screenheight() - 120))
        root.geometry(f'{width}x{height}')
        root.minsize(min(820, width), min(640, height))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.colors = {'canvas': '#F4F5F2', 'paper': '#FFFFFF', 'ink': '#24332C',
                       'muted': '#738078', 'line': '#E2E7E1', 'accent': '#456653', 'wash': '#EDF3ED'}
        c = self.colors
        root.configure(background=c['canvas'])
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background=c['paper'], foreground=c['ink'])
        style.configure('TFrame', background=c['paper'])
        style.configure('Shell.TFrame', background=c['canvas'])
        style.configure('TLabel', background=c['paper'], foreground=c['ink'])
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('Sub.TLabel', foreground=c['muted'], font=('Microsoft YaHei UI', 9))
        style.configure('Brand.TLabel', background=c['canvas'], font=('Segoe UI', 15, 'bold'))
        style.configure('Shell.TLabel', background=c['canvas'], foreground=c['muted'], font=('Microsoft YaHei UI', 9))
        style.configure('Section.TLabel', font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('TButton', padding=(14, 8), relief='flat', borderwidth=1,
                        background=c['paper'], bordercolor=c['line'], lightcolor=c['paper'], darkcolor=c['paper'])
        style.map('TButton', background=[('active', c['wash']), ('pressed', '#E0E9DF')],
                  foreground=[('disabled', '#A4ADA7')], bordercolor=[('focus', c['accent'])])
        style.configure('Accent.TButton', background=c['accent'], foreground='white', borderwidth=0,
                        font=('Microsoft YaHei UI', 10, 'bold'), padding=(22, 9))
        style.map('Accent.TButton', background=[('disabled', '#E1E7E1'), ('pressed', '#314D3B'), ('active', '#53775E')],
                  foreground=[('disabled', '#97A499'), ('!disabled', 'white')])
        style.configure('Quiet.TButton', borderwidth=0, background=c['paper'], foreground=c['muted'], padding=(8, 6))
        style.configure('TMenubutton', padding=(12, 8), relief='flat', borderwidth=0, background=c['paper'])
        style.map('TMenubutton', background=[('active', c['wash'])])
        style.configure('TEntry', padding=8, fieldbackground='#FAFBF8', bordercolor=c['line'],
                        lightcolor=c['line'], darkcolor=c['line'])
        style.map('TEntry', bordercolor=[('focus', c['accent'])])
        style.configure('TCombobox', padding=8, fieldbackground='#FAFBF8', bordercolor=c['line'], arrowsize=14)
        style.map('TCombobox', fieldbackground=[('readonly', '#FAFBF8')], selectbackground=[('readonly', '#FAFBF8')],
                  selectforeground=[('readonly', c['ink'])])
        style.configure('TCheckbutton', background=c['paper'], padding=(0, 5))
        style.map('TCheckbutton', background=[('active', c['paper'])], indicatorbackground=[('selected', c['accent'])])
        style.configure('TNotebook', background=c['canvas'], borderwidth=0, tabmargins=(0, 0, 0, 8))
        style.layout('TNotebook', [])
        style.layout('TNotebook.Tab', [('Notebook.padding', {'sticky': 'nswe', 'children': [
            ('Notebook.focus', {'sticky': 'nswe', 'children': [('Notebook.label', {'sticky': 'nswe'})]})]})])
        style.configure('TNotebook.Tab', padding=(18, 10), background=c['canvas'], foreground=c['muted'],
                        font=('Microsoft YaHei UI', 10, 'bold'), focuscolor=c['accent'])
        style.map('TNotebook.Tab', padding=[('selected', (18, 10))], foreground=[('selected', c['accent'])])
        style.configure('Treeview', rowheight=38, background=c['paper'], fieldbackground=c['paper'],
                        foreground=c['ink'], borderwidth=0, bordercolor=c['paper'], lightcolor=c['paper'], darkcolor=c['paper'])
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 9), padding=(10, 9),
                        background='#F7F9F5', foreground=c['muted'], relief='flat', borderwidth=0)
        style.map('Treeview', background=[('selected', '#E8F0E7')], foreground=[('selected', c['ink'])])
        style.layout('Vertical.TScrollbar', [('Vertical.Scrollbar.trough', {'sticky': 'ns', 'children': [
            ('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
        style.configure('Vertical.TScrollbar', arrowsize=8, width=8, background='#D4DDD3', troughcolor=c['paper'],
                        borderwidth=0, bordercolor=c['paper'], lightcolor='#D4DDD3', darkcolor='#D4DDD3', relief='flat')
        style.configure('TSeparator', background=c['line'], bordercolor=c['line'], lightcolor=c['line'], darkcolor=c['line'])
        style.configure('Horizontal.TProgressbar', background=c['accent'], troughcolor='#E3E9E1',
                        borderwidth=0, bordercolor='#E3E9E1', thickness=5, lightcolor=c['accent'], darkcolor=c['accent'])
        frame = ttk.Frame(root, padding=(24, 16), style='Shell.TFrame')
        frame.pack(fill='both', expand=True)
        header = ttk.Frame(frame, style='Shell.TFrame')
        header.pack(fill='x', pady=(0, 12))
        ttk.Label(header, text='AgentCarry', style='Brand.TLabel').pack(side='left')
        ttk.Label(header, text='同一份项目，继续下一段对话', style='Shell.TLabel').pack(side='left', padx=18)
        ttk.Label(header, text='LOCAL  /  本机迁移', style='Shell.TLabel').pack(side='right')
        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill='both', expand=True)
        self.pages = [ttk.Frame(self.tabs, padding=(24, 18)) for _ in range(3)]
        for page, name in zip(self.pages, ('01  选择对话', '02  迁移设置', '03  检查与导入')):
            self.tabs.add(page, text=name)
        settings = service.defaults()
        self.vars = {k: tk.StringVar(root, value=v) for k, v in settings.items()}
        self.mode = tk.StringVar(root, value=next(iter(MODES)))
        self.include_internal, self.include_archived = tk.BooleanVar(root), tk.BooleanVar(root)
        self.lossy, self.activate = tk.BooleanVar(root), tk.BooleanVar(root)
        self.source_note = tk.StringVar(root, value='读取本机项目后，选择要继续的对话。')
        self.selection_note = tk.StringVar(root, value='尚未选择')
        self.context_note = tk.StringVar(root, value='未选择额外记忆/规则文件；原项目规则保留在原目录。')
        self.status = tk.StringVar(root, value='就绪 · ' + service.VERSION)
        self._source_page()
        self._target_page()
        self._review_page()
        footer = ttk.Frame(frame, style='Shell.TFrame', padding=(4, 4, 4, 0))
        footer.pack(side='bottom', fill='x', pady=(10, 0), before=self.tabs)
        self.progress_note = tk.StringVar(root, value='所有迁移操作均在本机完成')
        self.elapsed = tk.StringVar(root, value='')
        top = ttk.Frame(footer, style='Shell.TFrame')
        top.pack(fill='x')
        ttk.Label(top, textvariable=self.elapsed, style='Shell.TLabel').pack(side='right')
        self.status_label = ttk.Label(top, textvariable=self.status, wraplength=650, style='Shell.TLabel')
        self.status_label.pack(side='left', fill='x', expand=True)
        progress_track = ttk.Frame(footer, height=5, style='Shell.TFrame')
        progress_track.pack(fill='x', pady=(7, 6))
        progress_track.pack_propagate(False)
        self.progress = ttk.Progressbar(progress_track, mode='determinate', length=150)
        self.progress.pack(fill='both', expand=True)
        self.detail_label = ttk.Label(footer, textvariable=self.progress_note, wraplength=850, style='Shell.TLabel')
        self.detail_label.pack(anchor='w')
        footer.bind('<Configure>', lambda e: (self.status_label.configure(wraplength=max(200, e.width - 110)),
                                              self.detail_label.configure(wraplength=max(200, e.width - 8))))
        for var in [*self.vars.values(), self.mode, self.lossy, self.activate]:
            var.trace_add('write', lambda *_: self.invalidate())
        self.root.after(100, self.poll)

    def button(self, parent, text, command, style='TButton'):
        widget = ttk.Button(parent, text=text, command=command, style=style)
        self.controls.append(widget)
        return widget

    def field(self, parent, label, var, browse=True):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text=label, width=17).pack(side='left')
        entry = ttk.Entry(row, textvariable=var)
        entry.pack(side='left', fill='x', expand=True, padx=(0, 7))
        self.controls.append(entry)
        if browse:
            self.button(row, '选择…', lambda: self.pick_dir(var)).pack(side='right')

    def check(self, parent, text, var):
        box = ttk.Checkbutton(parent, text=text, variable=var)
        box.pack(anchor='w', pady=6)
        self.controls.append(box)

    def page_title(self, parent, title, subtitle):
        ttk.Label(parent, text=title, style='Title.TLabel').pack(anchor='w')
        ttk.Label(parent, text=subtitle, style='Sub.TLabel', wraplength=720).pack(anchor='w', pady=(6, 12))

    def disclosure(self, parent, title):
        holder = ttk.Frame(parent)
        holder.pack(fill='x', pady=(3, 8))
        body = ttk.Frame(holder, padding=(0, 6, 0, 4))
        opened = [False]
        def toggle():
            opened[0] = not opened[0]
            button.configure(text=('−  ' if opened[0] else '+  ') + title)
            if opened[0]:
                body.pack(fill='x')
            else:
                body.pack_forget()
        button = self.button(holder, '+  ' + title, toggle, 'Quiet.TButton')
        button.pack(anchor='w')
        return body

    def menu_button(self, parent, label):
        widget = ttk.Menubutton(parent, text=label)
        menu = tk.Menu(widget, tearoff=False, background=self.colors['paper'], foreground=self.colors['ink'],
                       activebackground=self.colors['wash'], activeforeground=self.colors['ink'],
                       font=('Microsoft YaHei UI', 10), borderwidth=0)
        widget.configure(menu=menu)
        self.controls.append(widget)
        return widget, menu

    def _source_page(self):
        page = self.pages[0]
        bottom = ttk.Frame(page, padding=(0, 14, 0, 0))
        bottom.pack(side='bottom', fill='x')
        ttk.Separator(bottom).pack(fill='x', pady=(0, 12))
        row = ttk.Frame(bottom)
        row.pack(fill='x')
        self.next_button = self.button(row, '下一步 →', lambda: self.tabs.select(1), 'Accent.TButton')
        self.next_button.pack(side='right')
        self.button(row, '添加记忆 / 规则', self.pick_context, 'Quiet.TButton').pack(side='left')
        self.button(row, '清除', self.clear_context, 'Quiet.TButton').pack(side='left', padx=6)
        ttk.Label(bottom, textvariable=self.context_note, style='Sub.TLabel', wraplength=700).pack(anchor='w', pady=(6, 0))
        self.page_title(page, '选择要带走的对话', '项目留在原目录。选择对话，带着已有的上下文继续工作。')
        actions = ttk.Frame(page)
        actions.pack(fill='x', pady=(0, 8))
        self.button(actions, '读取项目', self.scan, 'Accent.TButton').pack(side='left')
        self.button(actions, '添加项目', self.add_project).pack(side='left', padx=8)
        more, menu = self.menu_button(actions, '更多  ⋯')
        more.pack(side='left')
        for label, command in [('全选可用会话', lambda: self.choose_all(True)), ('取消全选', lambda: self.choose_all(False)),
                               ('仅关联项目，不选会话', self.choose_files_only), ('展开全部', lambda: self.expand_projects(True)),
                               ('收起全部', lambda: self.expand_projects(False))]:
            menu.add_command(label=label, command=command)
        menu.add_separator()
        menu.add_checkbutton(label='显示归档对话（重读并清空勾选）', variable=self.include_archived, command=self.scan)
        menu.add_checkbutton(label='显示内部 / 自动任务（重读并清空勾选）', variable=self.include_internal, command=self.scan)
        menu.add_separator()
        menu.add_command(label='设置 Codex 数据目录…', command=lambda: self.pick_dir(self.vars['codex_home']))
        menu.add_command(label='打开本机迁移记录…', command=self.open_job)
        ttk.Label(actions, textvariable=self.selection_note, style='Sub.TLabel').pack(side='right')
        ttk.Label(page, textvariable=self.source_note, style='Sub.TLabel', wraplength=740).pack(anchor='w', pady=(0, 10))
        area = ttk.Frame(page)
        area.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(area, columns=('detail',), selectmode='browse', height=5)
        self.tree.heading('#0', text='项目与对话  ·  点击勾选')
        self.tree.heading('detail', text='位置 / 状态')
        self.tree.column('#0', width=500, minwidth=260)
        self.tree.column('detail', width=260, minwidth=120)
        self.tree.tag_configure('project', background='#F2F6F0', foreground=self.colors['accent'], font=('Microsoft YaHei UI', 10, 'bold'))
        self.tree.tag_configure('unavailable', foreground='#A28B76')
        scroll = ttk.Scrollbar(area, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<ButtonRelease-1>', self.toggle)
        self.tree.bind('<space>', self.toggle)

    def _target_page(self):
        outer = self.pages[1]
        row = ttk.Frame(outer, padding=(0, 12, 0, 0))
        row.pack(side='bottom', fill='x')
        ttk.Separator(row).pack(fill='x', pady=(0, 12))
        actions = ttk.Frame(row)
        actions.pack(fill='x')
        self.button(actions, '← 返回', lambda: self.tabs.select(0), 'Quiet.TButton').pack(side='left')
        self.button(actions, '准备并检查迁移 →', self.prepare, 'Accent.TButton').pack(side='right')
        canvas = tk.Canvas(outer, highlightthickness=0, background=self.colors['paper'])
        scroll = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        page = ttk.Frame(canvas)
        window = canvas.create_window(0, 0, window=page, anchor='nw')
        page.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        self.root.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(-int(e.delta / 120), 'units')
                           if self.tabs.select() == str(outer) else None, add='+')
        self.page_title(page, '让对话在新平台继续', '确认迁移方式和资料位置，然后生成检查报告。')
        ttk.Label(page, text='迁移方式', style='Section.TLabel').pack(anchor='w')
        combo = ttk.Combobox(page, textvariable=self.mode, values=list(MODES), state='readonly', width=46)
        combo.pack(fill='x', pady=(8, 8), padx=(0, 10))
        self.controls.append(combo)
        ttk.Label(page, text='原生导入支持 Windows Antigravity 2.32.2。准备前请保存工作并退出客户端。',
                  wraplength=700, style='Sub.TLabel').pack(anchor='w', pady=(0, 20))
        ttk.Label(page, text='迁移资料', style='Section.TLabel').pack(anchor='w', pady=(0, 5))
        self.field(page, '资料保存位置', self.vars['output'])
        ttk.Label(page, text='这里保存聊天档案、报告和备份。代码与项目文件继续使用原目录。',
                  wraplength=700, style='Sub.TLabel').pack(anchor='w', pady=(5, 14))
        paths = self.disclosure(page, '高级路径与维护')
        for key, label in [('home', '用户根目录'), ('ui', 'App UI 数据目录'), ('app_root', 'Antigravity 安装目录')]:
            self.field(paths, label, self.vars[key])
        self.button(paths, '整理已关闭的数据库…', self.checkpoint, 'Quiet.TButton').pack(anchor='w', pady=5)
        ttk.Separator(page).pack(fill='x', pady=(8, 14))
        self.check(page, '接受报告列出的转换差异', self.lossy)
        ttk.Label(page, text='工具调用、图片等仅保留在历史档案中；请先阅读报告，再决定是否接受。',
                  wraplength=700, style='Sub.TLabel').pack(anchor='w', pady=(0, 12))
        ttk.Label(page, text='原规则保持原样；额外记忆 / 规则单独存档。客户端可能以首条消息显示会话名称。',
                  wraplength=700, style='Sub.TLabel').pack(anchor='w', pady=(0, 10))

    def _review_page(self):
        page = self.pages[2]
        row = ttk.Frame(page, padding=(0, 14, 0, 0))
        row.pack(side='bottom', fill='x')
        ttk.Separator(row).pack(fill='x', pady=(0, 12))
        actions = ttk.Frame(row)
        actions.pack(fill='x')
        self.button(actions, '查看资料目录', self.open_output, 'Quiet.TButton').pack(side='left')
        self.button(actions, '恢复一次导入…', self.recover, 'Quiet.TButton').pack(side='left', padx=8)
        self.apply_button = self.button(actions, '执行导入', self.apply, 'Accent.TButton')
        self.apply_button.pack(side='right')
        self.apply_button.state(['disabled'])
        self.page_title(page, '确认后，继续对话', '检查项目目录和转换差异。执行导入后，再打开 Antigravity 验证。')
        tools = ttk.Frame(page)
        tools.pack(fill='x', pady=(0, 14))
        self.button(tools, '退出 Antigravity', lambda: self.manage_app('close')).pack(side='left')
        self.button(tools, '打开 Antigravity', lambda: self.manage_app('open')).pack(side='left', padx=8)
        self.button(tools, '重启', lambda: self.manage_app('restart'), 'Quiet.TButton').pack(side='left')
        self.report = ScrolledText(page, wrap='word', font=('Microsoft YaHei UI', 10), relief='flat',
                                  background='#F8FAF6', foreground=self.colors['ink'], insertbackground=self.colors['accent'],
                                  selectbackground='#DEEADC', borderwidth=0, padx=20, pady=18, spacing1=3, spacing3=6)
        self.report.pack(fill='both', expand=True)
        self.report.tag_configure('heading', font=('Microsoft YaHei UI', 13, 'bold'), foreground=self.colors['accent'], spacing3=12)
        self.report.tag_configure('status', font=('Microsoft YaHei UI', 10, 'bold'))
        self.show_report('还没有生成迁移报告\n\n选择对话并完成设置后，点击“准备并检查迁移”。\n这里会显示项目目录、会话数量和转换差异。')

    def show_report(self, text):
        self.report.configure(state='normal')
        self.report.delete('1.0', 'end')
        self.report.insert('end', text)
        self.report.tag_add('heading', '1.0', '1.end')
        for number, line in enumerate(text.splitlines(), 1):
            if line.startswith(('状态：', '需处理：')):
                self.report.tag_add('status', f'{number}.0', f'{number}.end')
        self.report.configure(state='disabled')

    def invalidate(self):
        self.current_plan = None
        if hasattr(self, 'apply_button'):
            self.apply_button.state(['disabled'])

    def pick_dir(self, var):
        if not self.busy:
            path = filedialog.askdirectory(parent=self.root, title='选择目录', initialdir=var.get())
            if path:
                var.set(path)

    def scan(self):
        if self.busy:
            return
        home = self.vars['codex_home'].get()
        include_internal, include_archived = self.include_internal.get(), self.include_archived.get()
        def done(projects):
            self.projects, self.job_path = projects, None
            self.selected_roots.clear()
            self.selected_threads.clear()
            self.invalidate()
            self.draw_projects()
            missing = sum(not t.get('available', True) for p in projects for t in p['threads'])
            self.source_note.set(f'{len(projects)} 个项目 · {sum(len(p["threads"]) for p in projects)} 条对话' + (f' · {missing} 条记录缺失，已单独标注' if missing else ' · 展开项目，可逐条选择'))
        self.start(lambda: service.source_projects(home, include_internal, include_archived), done)
        self.show_progress({'stage': '读取项目与会话索引', 'detail': '检查会话类型及普通/压缩记录路径，不读取聊天正文'})

    def draw_projects(self):
        opened = {self.nodes[n][1]['path']: bool(self.tree.item(n, 'open'))
                  for n in self.tree.get_children() if n in self.nodes and self.nodes[n][0] == 'project'}
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        self.selection_note.set(f'已选 {len(self.selected_roots)} 个项目 · {len(self.selected_threads)} 条对话')
        for i, project in enumerate(self.projects):
            node = f'p{i}'
            chosen = project['path'] in self.selected_roots
            count = sum(t['id'] in self.selected_threads for t in project['threads'])
            available = sum(t.get('available', True) for t in project['threads'])
            label = ('☑ ' if chosen else '☐ ') + project['name'] + f'   ·   {count} / {available} 已选' + (f' · {len(project["threads"]) - available} 缺失' if available != len(project['threads']) else '') + ('' if project['available'] else '（目录不存在）')
            self.tree.insert('', 'end', iid=node, text=label, values=(project['path'],), open=opened.get(project['path'], True), tags=('project',))
            self.nodes[node] = ('project', project)
            for j, chat in enumerate(project['threads']):
                child = f'{node}-c{j}'
                kind = {'guardian': '【内部审查】', 'subagent': '【子代理】', 'automation': '【自动任务】'}.get(chat.get('task_kind'), '')
                label = ('☑ ' if chat['id'] in self.selected_threads else '☐ ') + kind + chat['title'] + ('（已归档）' if chat.get('archived') else '') + ('' if chat.get('available', True) else '（聊天文件缺失）')
                self.tree.insert(node, 'end', iid=child, text=label, values=(('已归档 · ' if chat.get('archived') else '') + ('压缩记录' if chat.get('compressed') else '聊天记录') if chat.get('available', True) else chat.get('rollout_path', '文件缺失'),), tags=() if chat.get('available', True) else ('unavailable',))
                self.nodes[child] = ('chat', (project, chat))

    def toggle(self, event):
        if self.busy or self.job_path:
            return
        if event.keysym != 'space' and self.tree.identify('element', event.x, event.y).endswith('indicator'):
            return
        node = self.tree.focus() if event.keysym == 'space' else self.tree.identify_row(event.y)
        if node not in self.nodes:
            return
        kind, value = self.nodes[node]
        if kind == 'project':
            if not value['available']:
                return
            selected = value['path'] not in self.selected_roots
            (self.selected_roots.add if selected else self.selected_roots.discard)(value['path'])
            for chat in value['threads']:
                if not chat.get('available', True):
                    continue
                (self.selected_threads.add if selected else self.selected_threads.discard)(chat['id'])
        else:
            project, chat = value
            if not project['available'] or not chat.get('available', True):
                return
            self.selected_roots.add(project['path'])
            (self.selected_threads.discard if chat['id'] in self.selected_threads else self.selected_threads.add)(chat['id'])
        self.invalidate()
        opened = [n for n in self.tree.get_children() if self.tree.item(n, 'open')]
        self.draw_projects()
        for n in opened:
            self.tree.item(n, open=True)

    def expand_projects(self, opened):
        if not self.busy:
            for node in self.tree.get_children():
                self.tree.item(node, open=opened)

    def choose_files_only(self):
        if not self.busy and not self.job_path:
            self.selected_roots = {p['path'] for p in self.projects if p['available']}
            self.selected_threads.clear()
            self.invalidate()
            self.draw_projects()

    def choose_all(self, selected):
        if self.busy or self.job_path:
            return
        self.selected_roots = {p['path'] for p in self.projects if p['available']} if selected else set()
        self.selected_threads = {t['id'] for p in self.projects if p['available'] for t in p['threads'] if t.get('available', True)} if selected else set()
        self.invalidate()
        self.draw_projects()

    def add_project(self):
        if self.busy or self.job_path:
            return
        chosen = filedialog.askdirectory(parent=self.root, title='选择项目根目录')
        if not chosen:
            return
        root = Path(chosen).resolve()
        absorbed = [p for p in self.projects if porter.inside(Path(p['path']), root)]
        if any(porter.inside(root, Path(p['path'])) for p in self.projects):
            messagebox.showinfo('已在列表中', '该目录已属于列表中的项目，请直接勾选。', parent=self.root)
            return
        self.projects = [p for p in self.projects if p not in absorbed] + [
            {'path': str(root), 'name': root.name, 'available': True,
             'threads': [t for p in absorbed for t in p['threads']]}]
        self.choose_all(False)

    def open_job(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title='打开本机迁移记录中的 job.json', filetypes=[('迁移任务', 'job.json')])
        if not path:
            return
        def done(parts):
            self.job_path = path
            self.invalidate()
            self.tree.delete(*self.tree.get_children())
            self.nodes.clear()
            for part in parts:
                self.tree.insert('', 'end', text=part['name'], values=(f'{len(part["manifest"]["threads"])} 条会话',))
            self.source_note.set('已打开本机迁移记录，项目将直接关联原目录：' + path)
            self.tabs.select(1)
        self.start(lambda: transfer.load_job(Path(path), progress=self.report_progress), done)

    def pick_context(self):
        if self.busy or self.job_path:
            return
        files = filedialog.askopenfilenames(parent=self.root, title='选择需要单独存档的额外文本记忆/规则',
                                            filetypes=[('UTF-8 文本', '*.md *.txt')])
        self.contexts = list(dict.fromkeys([*self.contexts, *files]))
        self.context_note.set(f'已添加 {len(self.contexts)} 份记忆 / 规则文件 · 单独存档')
        self.invalidate()

    def clear_context(self):
        if not self.busy:
            self.contexts.clear()
            self.context_note.set('未选择额外记忆/规则文件。')
            self.invalidate()

    def target_config(self):
        return {k: str(Path(self.vars[k].get()).expanduser().absolute()) for k in ('home', 'ui', 'app_root')}

    def prepare(self):
        if self.busy:
            return
        try:
            selection = None if self.job_path else service.selection_spec(self.vars['codex_home'].get(), self.projects,
                                        self.selected_roots, self.selected_threads, self.contexts)
            kwargs = dict(output=self.vars['output'].get(), target=MODES[self.mode.get()], config=self.target_config(),
                          selection=selection, existing_job=self.job_path, allow_lossy=self.lossy.get(),
                          activate_context=self.activate.get(), progress=self.report_progress)
        except Exception as exc:
            self.error(exc)
            return
        self.invalidate()
        self.tabs.select(2)
        def done(result):
            self.last_session = result['session']
            self.show_report(result['summary'])
            if result['status'] == 'ready-to-apply':
                self.current_plan = result['plan']
                self.apply_button.state(['!disabled'])
            self.status.set({'blocked': '需要处理报告中的问题，可返回修改选项后重新准备。',
                             'portable-restored': '聊天档案已保存；项目仍在原目录。',
                             'ready-to-apply': '准备完成，请检查报告后执行导入。'}[result['status']])
        self.start(lambda: service.prepare(**kwargs), done)

    def apply(self):
        if self.busy or not self.current_plan:
            return
        plan = self.current_plan
        def done(result):
            self.invalidate()
            self.show_report(self.report.get('1.0', 'end') + '\n导入完成，文件核验通过。现在可启动 Antigravity 检查续聊和项目文件；检查后重启再确认。\n')
            self.status.set('导入完成；客户端续聊和重启检查尚待你验证。')
            self.progress_note.set('导入与文件核验完成。请打开 Antigravity 检查。')
        self.start(lambda: service.apply(Path(plan), self.report_progress), done)

    def recover(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title='选择要恢复的 plan.json', filetypes=[('迁移计划', 'plan.json')])
        if path and messagebox.askyesno('恢复导入', '将恢复这次导入修改的 App 数据。原项目文件不会改动；存在后续修改时会拒绝覆盖。请先退出 Antigravity。', parent=self.root):
            self.start(lambda: service.recover(Path(path)), lambda _: self.show_report('已恢复这次原生导入。原项目文件未改动。'))

    def checkpoint(self):
        if self.busy:
            return
        if messagebox.askyesno('整理关闭后的数据库', '仅在完全退出 Antigravity 后使用。程序先备份数据库及残留日志，再由 SQLite 合并已提交的数据。备份放在迁移资料目录中。', parent=self.root):
            config, output = self.target_config(), self.vars['output'].get()
            self.start(lambda: service.checkpoint(config, output), lambda result: messagebox.showinfo(
                '整理完成', '数据校验通过，备份保存在：\n' + result['backup'], parent=self.root))

    def manage_app(self, action):
        if self.busy:
            return
        if action in ('close', 'restart') and not messagebox.askyesno(
                '请先保存 Antigravity 中的工作',
                '请先保存未保存的文件，并等待正在生成的回复结束。程序会先请求退出，8 秒后结束残留后台进程。仅处理所选安装目录下、当前 Windows 会话的进程。是否继续？', parent=self.root):
            return
        config = self.target_config()
        self.invalidate()
        def done(_):
            self.status.set('Antigravity 已退出，请重新准备迁移。' if action == 'close' else '已请求启动 Antigravity。迁移前需再次退出并重新准备。')
        self.start(lambda: service.app_action(config, action), done)
        self.show_progress({'stage': {'close': '退出 Antigravity 并检查后台', 'open': '启动 Antigravity', 'restart': '重启 Antigravity'}[action]})

    def open_output(self):
        path = Path(self.last_session or self.vars['output'].get())
        if path.is_dir() and hasattr(os, 'startfile'):
            os.startfile(path)

    def report_progress(self, event):
        # Keep only the newest update; large file sets cannot flood the Tk queue.
        try:
            self.progress_events.put_nowait(event)
        except queue.Full:
            try:
                self.progress_events.get_nowait()
            except queue.Empty:
                pass
            self.progress_events.put_nowait(event)

    def show_progress(self, event):
        if isinstance(event, str):
            event = {'stage': event}
        self.progress.stop()
        total, completed = event.get('total', 0), event.get('completed', 0)
        label = event.get('stage', '处理中')
        if total > 0:
            self.progress.configure(mode='determinate', maximum=total, value=completed)
            label += f" · 当前步骤 {completed:,}/{total:,} {event.get('unit', '')}（{completed / total:.0%}）"
        else:
            self.progress.configure(mode='indeterminate', maximum=100, value=0)
            self.progress.start(12)
            label += ' · 处理中（总量未知）'
        project = event.get('project')
        if project:
            label = project + (f" [{event['project_index']}/{event['project_total']}]" if 'project_index' in event else '') + ' · ' + label
        self.status.set(label)
        self.progress_note.set(str(event.get('detail', ''))[-300:])

    def start(self, action, done):
        if self.busy:
            return
        self.busy = True
        self.previous_states = [(w, w.state()) for w in self.controls]
        for w, _ in self.previous_states:
            w.state(['disabled'])
        self.started_at = time.monotonic()
        self.elapsed.set('已用时 00:00')
        self.show_progress({'stage': '开始处理', 'detail': '请等待当前操作完成'})
        def work():
            try:
                self.events.put(('done', (done, action())))
            except Exception as exc:
                self.events.put(('error', exc))
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        if self.busy and self.started_at is not None:
            seconds = int(time.monotonic() - self.started_at)
            self.elapsed.set(f'已用时 {seconds // 60:02d}:{seconds % 60:02d}')
        try:
            self.show_progress(self.progress_events.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'progress':
                    self.show_progress(payload)
                    continue
                self.busy = False
                self.progress.stop()
                for w, previous in self.previous_states:
                    w.state(['!disabled'])
                    w.state(previous)
                if not self.current_plan:
                    self.apply_button.state(['disabled'])
                self.progress.configure(mode='determinate', maximum=1, value=0 if kind == 'error' else 1)
                self.progress_note.set('操作失败，请查看错误说明。' if kind == 'error' else '本次操作已完成；准备完成不代表已经执行导入。')
                self.status.set('处理完成')
                if kind == 'error':
                    self.error(payload)
                else:
                    callback, result = payload
                    callback(result)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def error(self, exc):
        self.status.set('操作未完成，原始数据和已有备份保留。')
        text = service.explain_error(exc)
        self.show_report('操作未完成\n\n' + text)
        messagebox.showerror('需要处理', text, parent=self.root)

    def close(self):
        if self.busy:
            messagebox.showinfo('任务正在进行', '请等待当前操作完成后再关闭窗口。', parent=self.root)
        else:
            self.root.destroy()


def launch():
    root = tk.Tk()
    Window(root)
    root.mainloop()


def smoke_test():
    import tempfile
    import time
    from self_test import fixture
    root = tk.Tk()
    root.withdraw()
    try:
        app = Window(root)
        root.attributes('-alpha', 0)
        root.geometry('820x640')
        root.deiconify()
        root.update()
        for widget in (app.next_button, app.progress):
            assert widget.winfo_rooty() >= root.winfo_rooty()
            assert widget.winfo_width() >= 60
            assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
            assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height(), 'Bottom controls clipped'
        root.withdraw()
        root.update_idletasks()
        assert len(app.tabs.tabs()) == 3
        assert app.apply_button.instate(['disabled'])
        assert not app.projects and app.current_plan is None
        app.show_progress({'stage': '测试阶段', 'completed': 2, 'total': 4, 'unit': '条'})
        assert str(app.progress['mode']) == 'determinate' and float(app.progress['value']) == 2
        assert '50%' in app.status.get()
        app.show_progress({'stage': '测试校验'})
        assert str(app.progress['mode']) == 'indeterminate' and '总量未知' in app.status.get()
        for i in range(1000):
            app.report_progress({'stage': '队列测试', 'completed': i, 'total': 1000})
        assert app.progress_events.qsize() == 1
        assert app.progress_events.get_nowait()['completed'] == 999
        app.progress.stop()
        app.show_report('中文界面测试\nNo user data read.')
        assert '中文界面测试' in app.report.get('1.0', 'end')
        # Exercise the same callbacks and worker/event handoff as the GUI, with
        # generated source records and an owned disposable target only.
        with tempfile.TemporaryDirectory(prefix='porter-gui-test-', dir=extended(tempfile.gettempdir(), force=True)) as temporary:
            folder = Path(temporary)
            selection, _ = fixture(folder)
            target = antigravity.initialize_sandbox(folder / 'target')
            app.vars['codex_home'].set(selection['codex_home'])
            app.vars['output'].set(str(folder / 'output'))
            app.target_config = lambda: target
            errors = []
            app.error = lambda exc: errors.append(str(exc))
            def finish():
                deadline = time.monotonic() + 45
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)
                assert not app.busy, 'GUI worker timed out'
                assert not errors, str(errors)
            app.scan()
            finish()
            assert len(app.projects) == 2
            assert all(app.tree.item(n, 'open') for n in app.tree.get_children())
            app.choose_files_only()
            assert len(app.selected_roots) == 2 and not app.selected_threads
            app.expand_projects(False)
            assert not any(app.tree.item(n, 'open') for n in app.tree.get_children())
            app.expand_projects(True)
            child = app.tree.get_children(app.tree.get_children()[0])[0]
            app.tree.focus(child)
            from types import SimpleNamespace
            app.toggle(SimpleNamespace(keysym='space'))
            assert len(app.selected_threads) == 1
            app.choose_all(True)
            assert len(app.selected_threads) == 4
            app.prepare()
            finish()
            assert app.current_plan and not app.apply_button.instate(['disabled'])
            plan = app.current_plan
            app.apply()
            finish()
            assert app.current_plan is None and app.apply_button.instate(['disabled'])
            assert '导入完成' in app.report.get('1.0', 'end')
            service.recover(Path(plan))
            missing_id = selection['projects'][0]['threads'][0]
            (Path(selection['codex_home']) / 'sessions' / (missing_id + '.jsonl')).unlink()
            app.scan()
            finish()
            app.choose_all(True)
            assert len(app.selected_threads) == 3 and missing_id not in app.selected_threads
            assert any('聊天文件缺失' in app.tree.item(child, 'text')
                       for parent in app.tree.get_children() for child in app.tree.get_children(parent))
        return {'gui_initialized': True, 'tabs': 3, 'native_apply_disabled_until_plan': True,
                'tk': tk.TkVersion, 'user_data_read': False,
                'wizard_flow': ['scan', 'select', 'prepare', 'apply', 'verify', 'recover'],
                'disposable_profile_only': True, 'projects': 2, 'conversations': 4}
    finally:
        root.destroy()
