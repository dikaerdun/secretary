# GitHub连接与多agent开发

日期：2026-10-06。

本文件与首份公开main基线一同入库。下方“尚无提交／未发布”描述的是连接阶段的历史状态；当前提交与跟踪关系请以`git log -1`、`git branch -vv`和`git ls-remote origin refs/heads/main`核对。首次基线与验证见[BASELINE.md](BASELINE.md)。

项目目录为D:/projects/secretary，远端为https://github.com/dikaerdun/secretary，origin使用HTTPS。连接时远端为空、默认分支main、可见性public；用户的GitHub连接有读写权限。本次配置只建立本地Git与远端连接，未发布源码。

本次任务使用manual_fallback的Planning、Parallel Work与Completion Verification：在秘书项目根初始化Git，配置origin，补齐忽略规则和协作约定；不修改业务代码、不提交或推送、不访问正式客户数据。验收依据为本地仓库根、origin地址、初始分支、远端可读取及私有目录排除结果。

## 首次入库

首次上传前由负责agent核对git status和拟提交文件清单，只纳入源码、测试、配置模板和必要文档。.env、data、缓存、测试运行目录和原始reviews不纳入。gitignore只控制文件是否纳入，不代替对源码与文档中的私密内容核对；当前仓库公开，首次发布需要明确发布范围。

首次commit建立main基线后，才具备创建worktree所需的提交。首次push建立origin/main后，其他机器可clone，独立任务可从同一基线启动。当前没有提交，不把设置初始分支名称说成已经有可用main提交。

如果同时还有agent在原目录编码，先由当前写入负责人确认基线与未完成修改，再做首次提交；避免把半成品作为其他任务的起点。不要覆盖其工作区。

首次入库示例（以下是待执行操作，本次连接没有运行commit或push）：

```powershell
Set-Location 'D:\projects\secretary'
git status --short --branch
git add .gitignore .env.example AGENTS.md README.md pyproject.toml requirements-tested.txt secretary tests
git add deploy/training.py 'deploy/使用指南与案例.html'
git add DETAIL_DIALOG_SPEC.md DETAIL_DIALOG_IMPLEMENTATION.md ARRANGEMENT_QUEUE_SPEC.md ARRANGEMENT_QUEUE_IMPLEMENTATION.md docs/GITHUB_WORKFLOW.md
git diff --cached --stat
git diff --cached
```

deploy/training.py是现有测试直接导入的离线测试支持源码，deploy/使用指南与案例.html也被演练测试读取，需要与tests一同核对并纳入基线。核对暂存内容后执行以下两步。其他部署资料、业务文档及截图按发布范围单独纳入；当前公开仓库不能把未核对的整个目录一并上传。需要撤回暂存文件时，首次commit前用git rm --cached -- <path>保留本地文件并从暂存区移出；不要使用会删除工作文件的命令。

```powershell
git commit -m "Initialize secretary development baseline"
git push -u origin main
```

## Codex中的项目目录

把D:/projects/secretary作为秘书项目目录。连接阶段核对的项目父目录不是有效Git仓库；仅在上一级打开项目，不代表Codex可自动定位下一级Git并创建worktree。项目配置应指向秘书项目根，Git能力以实际识别结果为准。

## 后续工作方式

1. 从最新已集成main为任务建立独立分支与worktree，给出需求、允许修改文件和验收标准。
2. 一个agent负责该worktree写入，其他agent可只读审查；测试产物也使用独立目录。
3. 完成定向验证后按授权提交、推送该任务分支并创建PR，附验证证据。使用Codex创建或处理PR时按工具要求附加到任务。
4. 集成负责人检查业务冲突、测试与依赖关系，按授权合并，再让后续任务使用更新后的main。

详情改版的D1至D5存在先后依赖，不能把五批当成五个可以同时改同一页面的任务。可并行的工作是范围明确的只读审查、测试准备及互不重叠的实现模块；D1完成并集成后再推进依赖它的后续批次。

具体agent规则见项目根AGENTS.md。分支隔离可以保护工作区，最终合并仍需审查逻辑冲突。

## 同一电脑启动三个agent

以下目录和分支是操作示例，需要先完成首次commit和push，确认main与origin/main均有提交。先检查git worktree list与git branch --list；如果目标任务已有目录或分支，复用并核对，不能重复创建、覆盖或清空。

```powershell
Set-Location 'D:\projects\secretary'
git fetch origin
git status --short --branch
git switch main
git merge --ff-only origin/main
git worktree list
git branch --list
git worktree add -b agent/todo-detail 'D:\projects\secretary-worktrees\todo-detail' main
git worktree add -b agent/arrangement-detail 'D:\projects\secretary-worktrees\arrangement-detail' main
git worktree add -b agent/detail-tests 'D:\projects\secretary-worktrees\detail-tests' main
```

切换main及同步前先确认主目录没有未提交修改；有修改时由其负责人处理，不能自动stash或覆盖。三个agent分别打开对应目录，不继续在原secretary目录共同写入。Codex创建新任务时可以选择worktree环境并从main启动，由应用管理目录；使用这种方式时不要再为同一任务执行上面的手工worktree add。

以详情改版为例，先确定五区容器、共享模块接口与事件所有者契约，再分工：

| 角色 | 写入范围 | 依赖与交付 |
|---|---|---|
| 集成负责人 | 共享契约、index.html、公共样式/模块的接入与最终合并 | 先提供统一接口；审批跨范围修改；执行组合验证 |
| Agent A | app.js中的待办详情接入及待办专用样式 | 不改安排文件；给出待办行为与保护验证 |
| Agent B | arrangement-queue.js及安排专用样式 | 不改待办文件；给出安排行为与保护验证 |
| Agent C | 约定的新增测试文件和验收记录 | 使用合成数据；先测已冻结契约；组合验收等A/B集成后运行 |

这三项是在同一批次内划清范围后的并行任务，不能越过D1至D5依赖。修改secretary-flow.js、detail-progress.js、web.py或sales_workspace.py等共享接入时，由集成负责人另行划定唯一写入者；各agent不能为自己的页面各做一份公共模块。已经在原目录运行的旧编码任务应先完成或安全交接，再切换独立目录。

## 每个agent提交与发PR

在自己的目录确认分支与改动。只暂存本任务的明确文件；下列<file>等占位内容须替换后执行，不能把占位文本原样当命令。

```powershell
git status --short --branch
git add -- <file1> <file2>
git diff --cached --stat
git diff --cached
git commit -m "Describe this task's concrete change"
git push -u origin HEAD
```

验证通过后创建PR，base为main、head为自己的任务分支，附改动、测试与未验证项目。PR可以通过GitHub插件或网页创建，不要求安装gh。工作agent按任务授权提交、推送和创建PR；没有合并授权时不自行合并。不要force push共享分支。

## 合并后的同步

Git同步以已保存提交为单位，不共享其他agent未提交的编辑。集成负责人核对PR与相关测试后按授权合并；每个agent在开始下一批或集成前更新已合并main。

在主目录且工作区干净时：

```powershell
git switch main
git fetch origin
git merge --ff-only origin/main
```

在仍需继续工作的任务分支，先按范围提交自己的可用修改，确认工作区干净，然后：

```powershell
git fetch origin
git merge origin/main
```

这里使用merge以保留已经推送的任务分支历史。解决冲突后检查git diff、运行受影响验证，使用git add暂存解决文件，再git commit和git push。冲突中不要整体选择ours/theirs；若暂不能解决，用git merge --abort回到合并前状态，并把冲突文件与双方意图交给集成负责人。

PR已合并且任务结束后，从更新后的main建立下一任务分支/worktree；尤其使用squash merge时不要沿用旧任务分支积累下一PR。清理前核对没有未提交文件或需要保留的忽略产物；Codex管理的worktree按应用归档流程处理，手工worktree按git worktree remove处理。

## 在另一台电脑开发

首次push完成后，另一台电脑clone同一仓库，每项任务仍使用独立分支；提交、PR及合并后的同步规则相同。环境配置在当地另行设置，不从GitHub拉取正式.env或数据库。

```powershell
git clone https://github.com/dikaerdun/secretary.git
Set-Location 'secretary'
git switch -c agent/my-task origin/main
```

## 给agent的任务提示词

```text
你负责私人秘书的一个并行开发任务。
工作目录：<本agent的worktree或clone绝对路径>
任务分支：<本agent的分支>
本次目标：<需求及研发批次>
允许修改：<明确文件或模块范围>
验收标准：<场景编号、行为与保护测试>

先阅读AGENTS.md和本次规格，核对实际目录、分支与已有修改。
只在自己的工作区写入，保留已有修改。需要跨范围修改时先给出理由和接入需求，不覆盖其他任务的实现。
按本批次开发并运行适当验证。完成后仅提交本任务改动，推送自己的分支，创建base=main的PR并附验证结果。本任务授权以上提交、推送与PR；不包含自动合并。
已有PR未合并前继续原任务分支；需要同步时先处理自己的未提交修改，再fetch并merge origin/main，解决冲突后复测。
最终报告分支、提交SHA、PR链接、实际测试结果及集成注意事项。将创建的PR附加到当前Codex任务。
```

用户复制并填写此提示词后发送给工作agent，才构成该任务的执行授权；本说明本身没有向其他会话发送消息或启动代码任务。
