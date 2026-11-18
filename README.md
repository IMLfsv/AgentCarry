# AgentCarry

**带上项目和对话，换个 AI 接着干。**

Your projects. Your conversations. Your next AI.

[下载 Windows 测试版](https://github.com/IMLfsv/AgentCarry/releases/tag/v0.4.2-experimental) · [快速开始](QUICKSTART.txt) · [已知限制与验证范围](VALIDATION.txt)

把所选 **Codex 项目文件、对话、可识别附件和显式选择的规则/记忆文件** 一起迁移。Python 3.11+，标准库，无网络请求、无 API Key。

“通用”指：输入由用户选择，换项目、会话、账号目录、恢复路径无需改源码；多个项目统一处理；不依赖固定测试词、会话 UUID 或手工修改数据库。其他平台通过适配器扩展，不代表所有平台和版本已经支持。

当前为 **0.4 Windows 下载测试版**。Windows x64 发布包内含 Python 和图形界面依赖；解压整个文件夹后双击 `AgentCarry.exe`，不需要安装 Python、输入命令或手写 JSON。图形向导支持项目/会话选择、打开跨电脑迁移包、迁移报告、原生导入和恢复。命令行入口仍是 `migrate.py`。旧的 `antigravity_trial.py` / `native_install.py` 保留为历史实验工具。

## 下载包的使用流程

1. 解压 Windows x64 ZIP，保留 `_internal` 文件夹，双击 EXE。
2. 读取本机 Codex 项目并勾选会话，或打开已有迁移包的 `job.json`。
3. 选择目标方式和输出目录，点击“准备并检查迁移”。
4. 阅读报告；原生模式通过检查后点击“执行导入”。
5. 启动目标 App 检查能否续聊、访问正确项目，重启后再确认。

原生检查不通过时不会提前创建恢复项目，修改选项后可重试。App 已退出但残留 SQLite 日志时，向导提供独立的“整理数据库”操作，先备份，再核验合并结果。迁移中的窗口不能直接关闭，以便保留完成或恢复记录。

发布包构建脚本会解压成品到新的中文路径，在新建的子进程用户目录、且 PATH 不含 Python 的环境中运行迁移自检和隐藏窗口初始化检查。这验证可移植打包，不能代替另一台物理电脑或多个用户的实际客户端验收。具体结果以该包的 `release-report.json` 为准。

**完整无损迁移尚未完成。** Windows Antigravity App 2.32.2 的一个短文本样本已在真实客户端续聊并通过重启验证；新增批量入口通过隔离文件测试，尚未经多个真实用户项目的客户端验收。原标题仍可能被客户端替换。历史工具调用、图片及其他非文本项只进入档案，不重建为原生执行记录；默认阻止存在这些差异的原生导入，需明确使用 `--allow-lossy`。详见 `VALIDATION.txt`。

## 使用

以下是源码/命令行用法；普通下载用户可直接使用图形向导，见 `QUICKSTART.txt`。

测试均使用合成输入，不读取实际账户数据：

```powershell
python -m unittest discover -s tests -v
python migrate.py --help
python migrate.py scan --codex-home "C:/Users/YOUR_NAME/.codex"
```

创建 `selection.json`，参考 `examples/selection.example.json`。不写 `threads` 时选择该物理目录内的所有聊天；写入 ID 列表时仅选择这些聊天。相对路径以 selection.json 所在目录为基准。

```json
{
  "codex_home": "~/.codex",
  "projects": [
    {"name": "项目 A", "path": "D:/Work/project-a"},
    {"name": "项目 B", "path": "D:/Work/project-b"}
  ]
}
```

每项可增加 `context_files: ["D:/Notes/preferences.md"]`，显式选择文本记忆/规则。支持 UTF-8 `.md` / `.txt`，每份最多 20 KB。不自动扫描外部隐藏记忆，不复制登录凭据。

```powershell
python migrate.py export --spec selection.json --out "D:/Migration/private-job"
python migrate.py verify-job "D:/Migration/private-job/job.json"
```

输出目录必须是新目录，并位于源项目和 Codex 数据目录之外。导出期间暂停源文件及聊天修改。`private-job` 可以复制到另一台电脑，目标电脑无需访问旧账号或原路径。每个项目 ZIP 包含文件清单、SHA-256、原始记录、可读对话、附件映射和排除报告。

### 可移植项目

```powershell
python migrate.py plan --job "D:/Migration/private-job/job.json" --target portable --to "D:/Restored" --out "D:/Migration/portable-plan"
```

此命令实际恢复文件，不是只读预览。每个项目有独立的 `project/`、`history/`、`assets/` 和路径映射。其他工具可打开 `project/`，从档案继续工作；此模式不向其他平台原生历史列表注册聊天。

### Antigravity 桌面 App

当前只允许已验证的 Windows 2.32.2 安装文件指纹。首次安装应先启动 App 建立数据目录，然后完全退出客户端及后台进程。未知版本不会强行写入。

```powershell
python migrate.py plan --job "D:/Migration/private-job/job.json" --target antigravity-desktop --to "D:/Restored" --out "D:/Migration/native-plan"
```

`plan` 恢复项目，在工作目录生成原生候选、备份和报告，不修改目标 App 数据。检查 `native-plan/report.json` 的源/目标 ID、路径、排除项、转换差异及 blockers。只有 `ready-to-apply` 才能发布：

```powershell
python migrate.py apply "D:/Migration/native-plan/plan.json"
python migrate.py verify-plan "D:/Migration/native-plan/plan.json"
```

随后启动 App 验收：会话可见、旧上下文可用、能续聊、文件操作落在新目录、重启仍保留。`verify-plan` 只检查发布快照及目录存在，不替代客户端验收。App 续聊会正常改变数据库，快照哈希可能不再相同。

可选参数：

- `--target-home`、`--target-ui`、`--app-root`：指定目标账户根目录、App UI 数据目录、安装目录；默认从当前用户环境解析，没有固定用户名。
- `--allow-lossy`：接受报告中的非文本/工具事件仅存档、部分源历史等差异。这些内容不会伪装成助手发言或自动重新执行。
- `--activate-context`：把显式选择的 context_files 转为本项目规则；未指定时只存档。项目中的 AGENTS.md/GEMINI.md 随文件复制。

规则格式依据 [Antigravity 官方文档](https://antigravity.google/docs/rules)：`.agents/rules/*.md`，`trigger: always_on`。平台存在规则长度和上下文预算限制，复制成功不保证全部始终进入模型上下文。生成的路径规则帮助识别旧目录；不会改写原始历史文字或源代码中的路径。

### 重复导入与恢复

相同内容再次导入相同目录会复用映射，不生成重复会话，也不覆盖已恢复文件的用户修改。源快照变更、目标位置改变或映射缺失时明确报错。当前不做增量合并。

```powershell
python migrate.py recover "D:/Migration/native-plan/plan.json"
```

恢复要求 App 退出，且相关目标文件仍匹配原始或安装后哈希。有后续修改或续聊时拒绝回滚；恢复的项目文件保留。发布采用逐文件原子更新和恢复日志，不是跨文件的操作系统事务。中断目录不会自动删除；检查 journal.json，使用新工作目录重试。

## 支持范围

| 内容 | 当前处理 |
| --- | --- |
| 多项目、中文/空格路径、任意会话 ID | 批量选择、自动分配目标 ID 和路径 |
| 同名项目、无聊天项目 | 按源身份区分目录；可恢复文件和注册项目 |
| Codex 目录 | 探测最高编号 state_*.sqlite，检查 schema，未知格式拒绝 |
| Codex 历史 | legacy / paginated，以及分支所需父记录前缀 |
| 多根项目/worktree | 显式选择物理根，各自恢复为目标项目；其他根的关联聊天列入 scope_notes |
| 用户/助手文字 | 原文进入原生消息；源时间留在档案，原生时间重新生成 |
| 原标题 | 档案和映射保留；客户端显示保留仍未解决 |
| 工具调用、图片及非文本 | 档案保留，原生还原未实现，逐项报告差异 |
| 附件 | 结构化路径且在允许根内；不抓取任意文本路径或远程 URL |
| 子代理 | 保留并标记本地片段，未重建完整父上下文 |
| 规则/文本记忆 | 项目文件复制、显式选择文件可激活为项目规则；不等同于内部运行状态 |
| Git、环境及权限 | 默认排除 .git/依赖；不恢复服务、ACL、执行权限和空目录 |
| 其他平台/版本 | 可移植包可用；原生迁移需新增并验证适配器 |

默认总解压上限 20 GiB、JSONL 单行 64 MiB、原生 protobuf 缓存解码上限 32 MiB。ZIP 校验拒绝路径穿越、大小写碰撞、重复项和链接。独占发布要求支持硬链接的文件系统；已在 Windows NTFS / Python 3.12 测试。

## 代码与开源范围

- `app.py`、`desktop.py`、`desktop_service.py`：独立程序入口、图形向导与用户操作流程。
- `self_test.py`：可随独立 EXE 运行的隔离自检，不扫描实际账户。
- `build_release.py`、`requirements-build.txt`：固定构建依赖、Windows ZIP、源码 ZIP、校验值和成品验证。
- `.github/workflows/build.yml`：Windows 自动测试和构建；本仓库 main 分支通过检查后发布实验版预发布下载，已有公开版本不覆盖。

- `porter.py`：Codex 格式读取与项目 ZIP。
- `transfer.py`：批量任务、内容身份、路径/规则与转换差异。
- `migrate.py`：适配器注册表、导出、计划、发布、核验和恢复入口。
- `adapters/`：目标存储、编码及版本约束。
- `publication.py`：与平台无关的文件发布和恢复。
- `tests/`：合成输入测试。

任务、计划、备份和恢复目录包含私有数据。文件名排除不等于密钥扫描或内容脱敏；SHA-256 检测变化，不验证发布者身份。GitHub 只发布程序、测试和文档，不能提交真实迁移数据。

格式研究参考：[session-migrate](https://github.com/xhluca/session-migrate)、[Antigravity Conversation Migrator](https://github.com/NightMean/Antigravity-Conversation-Migrator)、[interoperability fixtures](https://github.com/xhluca/antigravity-session-interoperability)。代码独立实现，MIT 许可。其他项目的 CLI/Linux 证据不替代本适配器的桌面 App 验证。

构建方式：在 Windows x64 上用独立虚拟环境安装 `requirements-build.txt`，运行 `python build_release.py --out 新构建目录`。打包方式遵循 [PyInstaller 官方分发说明](https://pyinstaller.org/en/stable/operating-mode.html)，运行库许可随包保留。其他操作系统需要分别构建和测试。
