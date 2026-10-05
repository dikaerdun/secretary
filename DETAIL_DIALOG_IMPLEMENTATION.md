# 私人秘书详情改造开发批次与验收

日期：2026-10-06。配套 [待办与安排详情交互规格](DETAIL_DIALOG_SPEC.md)。研发目标是把两个详情的首屏中心改为当前情况与本次推进，收敛重复入口，同时保留既有业务副作用、版本、权限及草稿保护。

## 任务范围与当前批次

Protocol mode为manual_fallback，采用Planning、Parallel Work、Review与Completion Verification。用户已授权继续细化设计并落成研发文档；本轮写入两份文档和两张截图副本，不修改业务代码或正式数据。后续代码开发以本文批次范围执行。

当前设计以用户认可的五区顺序为依据，规格采用前端编排、互斥提交目的、一次业务请求。首个可执行代码批次为D1：只改静态阅读层级，不改写接口、完成行为或排期归属。

必须本版解决：首屏处理中心、标题与历史去重、主操作匹配卡点、唯一时间编辑入口、进展/理解/完成区分、真实副作用说明、草稿与返回、局部刷新、手机键盘及错误可见性。

明确后续独立处理：文字与结构字段原子混合写入、只结束协调待办却保留其未来task的完成服务、activities安全自动重放、仅完成task的服务端版本保护、旧proposal自动迁移、外部通知和其他详情全面改版。不能在界面整理中隐式完成这些业务调整。现有GET记录详情的完成影响只读投影属于本版必要接入，不改变完成写入范围。

文档阶段只读当前源码、相关测试、已有安排规格和用户截图；不访问正式客户数据库、口令、模型密钥或远程服务。所有研发验证使用合成数据和独立运行目录，不能在正式库插入设计样例。

## 当前实现与必要接缝

以下函数依据2026-10-06当前源代码；实施前重新读取，其他研发可能已变更，不以行号作为稳定接口。本文源码及测试路径均相对当前clone的项目根目录，因此secretary/static位于项目内的Python包，tests位于项目根目录。

| 文件 | 当前责任与本次接入 |
|---|---|
| secretary/static/app.js | openRecord集中渲染待办；actionOriginsHTML、analysisSection、actionTermsHTML拆分归位；openOutcome与submitForm保留真实完成/进展语义；refreshDetail不能直接兜底安排 |
| secretary/static/arrangement-queue.js | arrangementDetailHTML重排五区；Snapshot在列表保留标题、在详情省去重复标题；Controls收敛；Decision抽出内联表单体；History只显示一份完整对话；保留结构提交契约 |
| secretary/static/secretary-flow.js | flowComposer/submitSecretaryFlow保留原话先保存、附件、异步处理与幂等；在新处理区去掉重复标题、当前对象自跳转及泛化问题 |
| secretary/static/secretary-workspace.js | 整理反馈与准备稿继续原核对采用流程；增加来源返回接入，不能把准备成功视为待办完成 |
| secretary/static/record-lifecycle.js | 保留预览、组范围、共享task阻挡及快照；归档影响说明不能丢失 |
| secretary/static/index.html | 原detail-dialog与edit-dialog；只增新前端资源及必要容器，不建第三个弹窗系统 |
| secretary/static/app.css、arrangement-queue.css、secretary-flow.css | 沿用token与既有控件；用新局部类实现五区、标题、主操作栏与手机布局，避免全局污染其他详情 |
| 拟新增secretary/static/detail-progress.js | 纯视图投影、focus选择、purpose分派、草稿与返回编排；不复制领域服务 |
| 拟新增secretary/static/detail-progress.css | 两类详情专用样式，scope到新详情根节点 |
| secretary/web.py、secretary/sales_workspace.py | 增补GET记录的completion_effects，与完成快照同锁读取、严格复用完成写入所影响的集合；不改变complete_record语义 |

新资源脚本必须在其依赖定义后加载，现有app全局函数调用新模块前做明确接入。D3迁移后的表单用data-detail-submit-owner=detail-progress声明唯一提交所有者；app、flow与arrangement旧监听必须在preventDefault/stopImmediatePropagation之前明确跳过该所有者，新模块再按purpose调用原提交函数。其他详情仍走原监听。当前flow与arrangement在捕获阶段拦截事件，不能只靠后加载新监听抢事件。点击事件同样检查新根的操作所有权，不能两个handler各执行一次。新模块不可用时保留原入口，不能打上所有者标记却无人处理。静态资源版本按现有资产机制更新。

## 必须保留的领域接口

| 本次目的 | 路径或既有服务 | 版本及副作用 |
|---|---|---|
| 待办保存文字 | POST /api/records/{id}/activities | 追加进展，无通用CAS、request_id去重或原请求查询，不扫描完成字眼；结果未知不自动重发 |
| 整理一次交流 | POST /api/records/{id}/activities/organize | 可能保存独立交流并提炼待办，不能伪装为纯记录；仍由明确整理入口调用 |
| 待办完成 | POST /api/records/{id}/complete-outcome | request_id及completion_snapshot；还完成关联pending task并拒绝proposal |
| 完成影响只读读取 | 现有GET /api/records/{id}增量completion_effects | 同锁生成影响集合及snapshot，严格对应complete_record影响范围，不借用归档组预览 |
| 旧待办排期/确认 | 原schedule与proposal确认路径 | schedule_snapshot/proposal版本；保存只是待确认，改期前旧日程保持 |
| 安排原话 | POST /api/secretary/turns | plan_id、expected_revision、request_id及原scope；先保存turn，后处理；不伪造source_kind |
| 安排结构操作 | POST /api/secretary/plans/{id}/arrangement-decisions | 原operation白名单；plan revision，涉及有效日程时task revision；账本重放 |
| 网页提示已读 | 原notice/read | 仅已读，不消费check，不修改业务版本 |
| 生命周期归档 | GET /api/records/{id}/lifecycle后原明确操作 | 使用返回的组范围、snapshot和共享任务阻挡；恢复不恢复已停止提醒 |
| 仅完成日程 | 原POST /api/tasks/{id}/complete | 当前接口没有任务版本核对；本次保留原日程入口，不新增统一处理区调用 |

本版不新增通用detail-updates后端接口，不把多个POST组成“统一提交”。只有在选定目的接口支持时才展示相应字段。需要多个写入的新目的属于reshape，先单独定义事务与副作用，不能绕过领域约束。

## 前端实现契约

建议方法及调用责任如下，命名可调整，职责不能省略。

| 方法 | 调用点与约束 |
|---|---|
| buildDetailViewModel(subjectRef, dto) | 详情读取成功后调用；纯函数映射summary、focus、authority与baseline，不修改DTO或业务状态 |
| renderDetailShell(vm) | openRecord/action与openArrangement调用；渲染A至E并挂一个active form slot |
| resolveDetailFocus(vm) | 首次打开或无脏草稿刷新时选择默认目的；有草稿则提示新情况，不自动换目的 |
| activateDetailPurpose(root, purpose, operation) | 先保存当前目的草稿再更换表单；互斥挂载，保持对象身份；必要时显示本次目的选择 |
| submitDetailPurpose(form) | 验证当前root与subjectRef，分派一个原提交handler，不能二次写入或预先保存其他目的 |
| applyDetailReceipt(subjectRef, result, submission) | 只根据服务端结果更新B与回执；分清accepted_processing和真实effects |
| refreshDetailSubject(subjectRef) | 记录与安排分别取最新DTO；禁止arrangement进入refreshDetail客户兜底分支 |
| pushDetailReturnContext / restoreDetailReturnContext | 对象切换、进入来源/工作台/复杂edit调用；验证可见性，恢复草稿、锚点与焦点 |
| preserveDetailEditing(root) | 任何摘要/历史局部替换前保存展开与滚动；C输入与附件节点保持 |

数据接口为前端内存对象，subjectRef含准确对象类型和ID；formSession含purpose、operation、draftKey、baseline、request_id、signature和所属root；returnContext只保存导航信息。不能在sessionStorage写入整个客户档案或创建第二份可修改的active_schedule。

每次请求的submission记录发送文本/字段、附件身份、草稿版本、对象与会话；请求账本是否支持按目的标注，不能给activities虚构服务端去重。响应后只清发送时相同版本的内容；已新增的草稿不得清除。后台新revision不能写入旧表单baseline后自动重试确认。

新增completion_effects由sales_workspace的只读投影生成，返回与completion_snapshot相同读取时刻的snapshot、scope、record_id、去重后的pending_tasks/pending_proposals及数量。选择任务严格采用complete_record当前的本record历史关联proposal与COALESCE(task_id,target_task_id)语义；不要扩大为子记录任务或快照涉及的所有历史task。GET records持现有共享锁返回两者，前端提交其snapshot；只读读取本身不得调用_execute、拒绝proposal或完成记录。DD28与现有历史关联task保护覆盖集合一致及读后变化拒绝旧提交。

常见结构操作在C内联：从arrangementDecisionHTML抽出form字段与提交逻辑，避免把完整dialog HTML塞进现有form。原submit handler需接受所属root/上下文；所有全局选择器检查背景edit与当前detail是否会命中相同ID。每个目的form ID仍满足原草稿机制或做明确迁移，不更改已有其他详情的草稿键。

回执只在原对象当前会话中更新；切换对象后原请求结果可提示已保存，但不能导航回来。原详情返回若仍可见则更新摘要，草稿仍按原baseline标记；源对象已不可见时显示保留/恢复入口，禁止自动选择其他对象。

## 开发批次

| 批次 | 单一目标与允许文件 | 完成条件 | 必过验收与层级 |
|---|---|---|---|
| D1 | 静态层级；app.js、arrangement-queue.js及局部CSS，必要新shell文件与index资产引用 | A至E顺序固定；标题/来源/聊天重复收敛；现有写handler不变 | DD01–08、37–38、40、46的静态只读展示；DD07只验历史去重，不验新回执分派；DD39软键盘与固定提交在D5验 |
| D2 | 主处理与展示适配；detail-progress.js、两类详情的只读DTO/focus接入 | 缺口、当前事实、time authority与状态主操作一致；不写业务 | DD09–16、24–26、43–45的合成DTO投影、焦点优先级与入口门控；实际状态/时间写入在D3验 |
| D3 | 一次提交与短编辑；detail-progress.js、app.js原handler、arrangement-queue.js、secretary-flow.js，以及web.py/sales_workspace.py只读完成投影 | 一个active form、互斥purpose、独立草稿、原副作用、阶段回执正确，完成影响读取准确 | DD07回执、DD11/14/15提交、DD17–31、41–42、47的真实事件分派、HTTP与合成库集成；各write目的以选定接口1次/其他0次为断言 |
| D4 | 导航与刷新；详情、flow/workspace/lifecycle必要返回钩子，局部刷新分派 | 返回不丢输入/焦点/展开；来源路由准确；迟到结果不抢视图 | DD32–36、48的会话/事件/局部更新集成；资产静态引用可在D1先验 |
| D5 | 连续旅程与交付；相关前端/HTTP回归、使用指南、浏览器验证记录 | DD01–48整体覆盖；首屏、键盘、字段错误和原领域保护通过 | 全部DD的最终组合；DD37–40真实浏览器含软键盘、焦点与滚动；四旅程及相关回归 |

D1可执行清单：

- 增加仅action与arrangement使用的详情根类与A至E容器；不要改普通沟通记录、材料、客户详情。
- 在同一个页面中保留一份标题；Snapshot显式区分list/detail使用场景，不能全局删列表标题。
- 当前情况从现有DTO只读展示；来源先搬至E，记录性质/身份/日程冲突警告保留在B。
- 只移动原操作区到C；保留原handler、字段、form身份、提交与快照，不在此批合并请求。
- 原analysisSection移至来源交流入口；避免删掉原采用历史、原文或其他详情的整理能力。
- 保留原资料、历史和操作可到达；先验证重排不会丢草稿、造成嵌套form或重复事件。
- CSS仅scope新根节点，完成桌面与手机静态检查。D1不得改后端API或自动创建plan。

D1与D2只验表中明确的展示子项，不要求提前完成后续提交或返回功能；未到批次的完整DD保持待验收。D3验证业务请求次数和原领域副作用；附件上传与GET轮询单独计数，不能把它们误当第二次业务提交。D4验证会话导航和刷新；D5才能声称完整交互通过。不得用D1的截图整齐代替D3的业务正确性。

批次门禁：范围与oracle满足则continue；当前实现错误则repair；发现需原子混合写、完成语义重构、任务版本保护或新的对象关系则reshape并先单独定义契约；超出用户本轮代码授权、正式数据操作或部署时按实际授权处理。不得弱化旧保护断言来达到布局目标。

## 验收矩阵

以下为待实现的DD01至DD48验收。每项同时检查用户可见内容、请求目的/次数及持久副作用；纯布局项使用只读合成DTO，不为了布局测试写业务数据。默认时区Asia/Shanghai，测试时钟固定且与页面日期一致。

| ID | 场景 | 必须观察 | 绝不能发生 |
|---|---|---|---|
| DD01 | 打开普通待办/安排详情 | A→B→C→D→E一致，当前情况与处理入口先于来源 | 先翻过原记录或历史才能处理 |
| DD02 | 安排列表进入详情 | 列表仍有标题，详情只显示一份标题 | 改共用Snapshot后列表标题消失 |
| DD03 | 长标题与大字号 | 原位置展开、关键内容可滚动、主要操作可找 | 独立大块重复完整标题或截断冲突 |
| DD04 | 无最新进展但有原话日期 | 显示原话提到及待核对性质 | 日期当有效预约、虚构最新结果 |
| DD05 | 已核对对象但旧回复曾追问身份 | 当前以最新DTO为准，旧追问只在历史 | 过时追问重复挡住处理 |
| DD06 | 待办来自已采纳AI建议 | 目标可推进，来源与建议性质可回溯 | AI观察写成客户承诺 |
| DD07 | 最近五轮对话与完整历史 | 完整内容保留且只展示一次，新回执在C | 正文与历史重复堆聊天 |
| DD08 | 多条原来源含同一目标 | 去重入口，仍能看原话/原讨论/采用依据 | 以标题去重丢不同真实来源 |
| DD09 | 缺钟点且还等待用户核对 | 显示具体缺口、补充目的 | 暗示点确认就可安排 |
| DD10 | 日期含义或self/external不清 | 问一项关键问题，可一次补齐其他信息 | 猜日期用途、强制所有人走固定阶段 |
| DD11 | 多候选未选，另有必要缺口 | 选候选目的，其他缺口保持可见 | 一选就约好或直接创建task |
| DD12 | 候选已发等待回复 | 默认记回复/进展，不误要求重复约定 | 催用户确认代替对方答应 |
| DD13 | 完整明确个人指令已真实执行 | 显示正式回执与task，不再统一确认 | 再建一次任务或增确认门槛 |
| DD14 | 用户要求先核对，必要条件齐全但can_apply=false仅缺授权 | 可进入核对并由确认提交建立当前授权；其他缺口仍先补 | 要求先有授权才允许取得授权、旧签名生效 |
| DD15 | settled日期目标同时有missing_clock，后续继续定钟点 | 生命周期先匹配，默认继续定钟点；提交同plan新周期、保留日期，旧期限/check不继承 | 普通缺clock规则重催日期、伪造全天/零点task/新plan |
| DD16 | paused/abandoned/done/隐藏来源同时缺钟点且有旧草稿 | 生命周期先匹配默认目的；只读无可写form，禁止旧草稿越过当前权限 | 普通pending卡点覆盖暂停、刷新自动恢复或复活提醒 |
| DD17 | 待办记录“这个完成了” | activities只追加进展，task和record状态不变 | 根据字眼调用complete-outcome |
| DD18 | 安排自然输入“问过了”与“还在等” | 按原语义前者可处理check、后者不处理 | 把所有续说都当纯保存或全消费check |
| DD19 | 安排选择只记录进展 | 单次update_progress且无check_handled/check_id | 走turn理解、伪造source_kind或消费check |
| DD20 | 同时有未提交文字和结构字段 | 选择本次目的、仅一个写请求，另一份保持未提交 | 两POST半成功或静默遗漏另一草稿 |
| DD21 | 文字与结构草稿时间矛盾 | 指出矛盾，保留双方，重新核对 | 自动以最后编辑者覆盖 |
| DD22 | 切换记录/结构/完成目的 | 独立草稿、baseline与请求身份恢复，只有一个active form | 互相覆盖草稿、嵌套form、旧handler双提交 |
| DD23 | 附件后切换不支持附件的目的 | 文件留原草稿，明确不随本次发送 | 丢文件、错传对象或伪称附件已提交 |
| DD24 | 修改确定期限/推进点/action_terms | 各字段绑定对应对象；正式执行时刻不动 | “周五再问”创建周五会面 |
| DD25 | 同一活动有可靠plan与旧proposal | plan唯一时间编辑来源，旧区只读或需核对 | 两套可写时间表或按标题自动迁移 |
| DD26 | 独立旧待办无plan；或多个关联plan | 保留proposal确认；多plan明确选活动，不新建影子plan | 保存即生效、任取第一plan或覆盖下一次活动 |
| DD27 | 已约好下周会面 | plan落实、未来task仍pending，待办不自动完成 | complete-outcome提前结束未来会面 |
| DD28 | 本record历史proposal关联多task，另有不受完成影响的子记录task | completion_effects与快照同锁，准确列全部实际影响；变化后旧snapshot被拒绝；只读无写副作用 | 拿active_reminders/归档组代替、漏历史task或错误列入子记录task |
| DD29 | 暂缓/放弃改期、撤销日程、取消活动 | 各自实际影响准确，暂停/放弃保留旧日程 | 取消项目、模糊停催取消活动 |
| DD30 | lifecycle归档组内有task/proposal或共用task | 展示停止范围，共用阻挡保留，恢复不复活 | 包装成纯隐藏或绕过共享保护 |
| DD31 | 归入客户交流、拒绝旧proposal、安排下一次 | 原关联/提案/新活动语义分别保持 | 称活动已取消、覆盖本次活动 |
| DD32 | 待办进入安排再返回 | 对象、滚动锚点、焦点、展开状态和各草稿恢复 | 叠第三层、返回归零或误写来源对象 |
| DD33 | 来源实际有data-record与raw-record两种路由 | 关联事项与原记录去向准确，能返回 | 统一按钮导致打不开原文 |
| DD34 | 请求处理中切换对象/关闭/重登录 | 旧结果不抢回当前详情，不清新对象输入 | late callback更新错对象或泄露会话内容 |
| DD35 | 后台更新与安排局部刷新 | B/D/E更新，C原输入/附件/焦点/展开保留 | arrangement调用客户兜底、整个详情innerHTML替换 |
| DD36 | 来源返回时原对象已隐藏或版本变化 | 检查可见性，旧草稿明确需核对、不自动升级baseline | 对不可见对象写入或旧确认自动重试 |
| DD37 | 1366×768正常合成案例 | 首屏可见当前情况、本次提示、输入入口与主操作 | 来源和空说明卡占满首屏 |
| DD38 | 320×640和390×844键盘关闭 | 阅读顺序一致、时间字段可读、触点≥44px | 三列挤压、横向溢出、关键动作不可达 |
| DD39 | 手机软键盘与底栏 | 一处主提交、输入末行/错误可到达，正文主要滚动唯一 | 键盘/底栏遮住错误或按钮，重复提交栏 |
| DD40 | 键盘打开/编辑/取消/返回及读屏 | 标签、折叠状态、回执/错误、焦点正确 | 只靠颜色或背景控件意外获焦 |
| DD41 | turn已接受但未处理/处理失败 | 区分原话保存、正在整理、未落实；原话可重试 | API成功就标已安排，失败吞原话 |
| DD42 | 网络未知/双击/同内容重试/内容改变；分别turn/decision/outcome与activities | 有账本目的沿用原请求重试；activities提示可能已保存、读历史人工核对且不自动重发；新内容新身份 | activities复用前端ID就声称去重、新ID盲重试完成、捕获监听截断新路由或双handler写 |
| DD43 | 曾启用/未启用/settled/恢复可见的推进提示 | 启用/恢复/无需协调文案准确，服务端门控保持 | 所有停用都叫恢复，客户端直接开启 |
| DD44 | 有日程但执行提醒关闭；改期商量中 | 正式日程仍可有效；原安排与拟改并列 | 无提醒说无日程、拟改替换旧安排 |
| DD45 | 准备资料未完成但安排已落实 | 资料或待办独立，可继续准备 | 重新进入催定时间、自动结束准备待办 |
| DD46 | 更多展开及移除重复按钮 | 维护/危险操作可找，主操作仍清楚，同目的不重复 | “更多”变成另一排同级主按钮或丢原能力 |
| DD47 | 409/422后重新核对，提交中新增文字/文件 | 错误具体，草稿保留，新输入不清；不自动套新版本 | 半提交、清新草稿、旧授权复用 |
| DD48 | 返回栈回环、多个相似活动、首批资产发布 | 同对象不重复入栈，最多五节点、ID准确，新资产只初始化一次 | 标题匹配错活动、无限回环或重复监听 |

## 现有保护测试与新增验证

| 现有套件 | 复用的保护 |
|---|---|
| tests/test_arrangement_queue_frontend.py | 三种时间、候选、check消费、恢复、409、重复提交与附件/焦点 |
| tests/test_recent_history_frontend.py | 请求期间下一份文字与附件草稿保留 |
| tests/test_agenda_layout_frontend.py | 完成日程与记录跟进结果分开 |
| tests/test_lifecycle_frontend.py | 导航、重新登录、迟到结果不抢当前视图 |
| tests/test_web_workspace.py | 完成结果重放、快照与排期确认 |
| tests/test_arrangement_web.py | 鉴权、CSRF、重放、版本竞争 |
| tests/test_arrangement_async_manual_guard.py | 迟到AI不能覆盖人工暂停或改期 |
| tests/test_record_lifecycle_web.py | 记录组、task/proposal、共享范围与恢复规则 |
| tests/test_record_lifecycle.py | 共享task阻挡的实际领域保护，不能被新入口绕过 |
| tests/test_round07_followup_boundaries.py | 本record历史关联task的完成范围，支持completion_effects一致性反例 |
| tests/test_arrangement_semantics.py | 时间用途、明确检查行动与等待语义 |
| tests/test_secretary_flow_attachments.py | 附件不成为用户执行授权 |

拟新增tests/test_detail_progress_frontend.py，使用现有JS执行测试方式验证视图投影、purpose切换、一次业务请求、草稿身份、导航和回执。通过真实submit/click事件分派检查每个purpose的所有者和原handler调用；捕获监听不能拦截新路由，旧详情仍按原处理。扩展test_web_workspace.py验证completion_effects与实际影响、快照变化和只读无副作用。行为测试关注结果与不应发生的请求，不能只匹配函数名或镜像实现。桌面/手机首屏、软键盘、滚动和焦点仍需浏览器演练。

每批先运行新定向测试与对应保护套件；D5再完整相关回归。没有新变化、失败或未解风险时不重复全量测试。文档交付只做引用、场景覆盖、领域一致性和独立审查，不据此声称新界面或测试已经通过。

## 连续旅程与交付记录

1. 旧待办推进：打开约见待办 → 看原话日期性质 → 记录已联系 → 在关联安排补具体时间 → 返回待办保留位置 → 未来日程仍待执行。
2. 安排落实：打开缺钟点且约定方式未知的安排 → 一句补清意图 → 选候选/记录回复 → 条件齐全后按原授权落实 → 当前情况与真实日程同步，历史后移。
3. 旧日程改期：查看有效周四安排 → 提出周五候选 → 原日程继续 → 暂缓协调或处理冲突 → 恢复后明确新的推进点，旧提示不复活。
4. 管理与恢复：进入待办完成或归档 → 看全部影响范围 → 取消操作保留草稿或明确提交 → 返回有准确回执 → 恢复只按原规则恢复可见，不恢复已停止日程。

交付证据包括源码摘要/版本、合成DTO与数据库场景、每批定向结果、相关回归结果、四旅程结果、桌面和手机对照截图、软键盘/焦点记录、一次请求计数与实际副作用、未验证或后续能力。不同object的成功回执与当前页面行为都要核对，不能只看截图样式。

## 进入代码开发的就绪检查

- [ ] 五区、默认展开与主处理矩阵明确，首屏信息预算有可观察基准。
- [ ] 每种purpose对应一个现有接口，进展、理解、完成和整理不会隐式混用。
- [ ] 完成/归档实际副作用已进入核对提示，未来日程反例有验收。
- [ ] scheduleAuthority、关联plan选择、旧proposal保留与多活动归属明确。
- [ ] 独立草稿、原版本基线、一次请求、回执阶段、局部刷新与返回栈有契约。
- [ ] D1只改阅读结构；后续原子混合写、完成服务和task版本保护没有进入本批。
- [ ] 现有测试保留，浏览器验收不由源码字符串断言代替。

文档定稿后的下一代码批次为D1。实施前核对当前文件与同仓库其他工作，按范围推进，避免整体重写大型app.js。
