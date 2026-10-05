> 公开发布副本：原始审查JSON、日志、旧客户截图与本机路径产物保留在忽略目录；以下仅发布验收摘要和合成数据参考图。

# 待办与安排详情改版交付记录

日期：2026-10-06。依据：DETAIL_DIALOG_SPEC.md、DETAIL_DIALOG_IMPLEMENTATION.md、ARRANGEMENT_QUEUE_SPEC.md、AGENTS.md。D1至D5代码范围已完成；DD39真实手机软键盘验收保留为部分验证，不能称48项全部通过。

## 用户可见变化

两个详情统一按“标题与对象 → 当前情况 → 本次推进 → 相关信息与准备 → 来源与历史”阅读。桌面使用居中单列，手机使用全宽详情。标题只保留一次，长标题在原位置展开；当前进展和真实日程先于原话、材料及历史。历史完整保留，来源按真实目标去重，普通摘要与关键冲突分开。

“本次推进”只挂载一个目的表单和一个主要提交按钮。目的切换后，问题和按钮明确说明当前是在记录进展、补充安排、调整确定期限、调整下一推进点、暂停、恢复还是完成。未选择的目的草稿独立保留，附件留在原本支持附件的草稿中。一次处理只调用一个原业务写接口，不把两个请求包装为统一成功。

记录“这个完成了”仍只保存普通进展，不自动结束待办。未来活动落实后仍是待执行日程，不提前完成待办。原话保存、正在整理、整理失败、正式日程生效分别反馈；已保存原话可以直接重试整理。

执行时间、确定期限和下一推进点分别显示和编辑。可靠关联plan采用唯一安排入口；独立旧待办保留proposal流程，保存提案后仍需明确确认。多活动可通过“核对全部关联活动”按真实记录和turn编号查全，再选择对应活动；查全后的普通刷新逐个重验已知plan，不退回任取最近活动。

完成预览准确列出本record实际受影响的pending日程和提案，不用归档组范围代替完成范围。暂停、放弃协调保留原有效日程；撤销日程、取消活动、归档和恢复沿用原领域语义。返回安排、来源及旧维护窗口时，保留草稿、文件、焦点、展开状态和滚动锚点；刷新B/D/E时不整体替换C的编辑节点。

## 修改文件与业务接入

| 文件 | 接入内容 |
|---|---|
| secretary/static/detail-progress.js | 新详情壳、只读视图投影与focus选择、互斥目的、独立草稿、原handler分派、真实阶段回执、版本重核对、返回栈及局部刷新 |
| secretary/static/detail-progress.css | 仅新详情根节点的居中布局、手机布局、固定头尾、主体滚动、44px触点与16px手机输入 |
| secretary/static/app.js | 待办渲染接入、原schedule片段抽取、原提交handler增加所属上下文、草稿/导航/返回必要钩子 |
| secretary/static/arrangement-queue.js | 安排详情重排、列表/详情标题分流、原decision字段抽取、owned事件分流及原权限/版本提交 |
| secretary/static/secretary-flow.js | 新详情沿用原turn保存，单独阶段回执；旧入口继续原处理 |
| secretary/static/secretary-attachments.js | 请求中新增文件排队保存到下一份草稿，防止成功回执清掉新增文件 |
| secretary/static/index.html、secretary/web.py | 新资产单次加载与版本散列；GET记录详情返回completion_effects及同次读取snapshot |
| secretary/sales_workspace.py | 唯一新增只读completion_effects投影，同共享锁读取实际受影响集合和原completion snapshot |
| tests/test_detail_progress_frontend.py | 64项真实JS投影、阅读结构与权限保护 |
| tests/test_detail_progress_events.py | 70项真实捕获/冒泡/默认submit分派、精确API次数和payload、独立草稿/附件与错误保护 |
| tests/test_detail_progress_navigation.py | 47项跨对象/来源返回、焦点、局部刷新、请求竞态、查全关联和当前目的问题/按钮保护 |
| tests/test_detail_completion_effects.py、tests/test_web_workspace.py | 同锁只读、COALESCE关联、实际写入集合差分、隐藏权限、原400快照拒绝、原完成重放保护 |
| deploy/使用指南与案例.html | 目录与“待办与安排详情：先处理这一件事”新章，旧指南内容保留 |

原有写接口没有增加：activities、原record schedule/proposal、action_terms、complete-outcome、secretary turns、arrangement-decisions、原lifecycle接口继续各自承担自己的业务目的。未新增数据库表或统一写API。独立审查对照Git基线及初始ZIP核实：complete_record、_completion_snapshot、completion_snapshot、web complete/complete_outcome、完成请求签名的方法AST未变。

completion_effects按原完成写入的历史关联proposal与COALESCE(task_id,target_task_id)选择pending任务；pending提案还包含Store完成task时实际拒绝的同owner target提案。集合去重，与snapshot在同一个共享锁下读取；GET不完成记录、不执行proposal，也不把子记录task扩进完成范围。

## D1至D5与验收结果

| 批次 | 完成内容 | 实际验证 |
|---|---|---|
| D1 | 只重排结构、去重、局部样式及资产引用，保留当批原写handler | 103项原前端保护；17项新渲染；当批最终57项定向；桌面及320/390布局与长标题截图 |
| D2 | 只读DTO/focus矩阵、生命周期优先、时间权威与入口门控 | 当批98项展示与原安排保护通过；后续新增只读反例继续在最终64项投影中验证 |
| D3 | 一次业务目的提交、原handler接入、草稿/附件/版本和准确完成只读投影 | 当批142项前端/事件保护通过；完成投影及相关HTTP/领域保护136项唯一测试通过；发现的retry事件和隐藏写入口反例已修复 |
| D4 | 返回栈、精确对象导航、局部刷新与处理中恢复 | 当批235项前端/导航/历史/lifecycle通过；最终导航新增真实反例后47项通过 |
| D5 | 四旅程、真实页面、最终反例修复、指南与交付 | 20组相关回归480 passed / 74.85s；最后两次提示修正后详情定向181 passed / 24.75s；来源/事项补充保护85 passed / 17.99s；业务/采纳来源保护50 passed / 7.65s；5个JS语法检查通过 |

上述运行有重叠，不能把次数相加称为独立测试总量。480项回归在最终提示优先级/文案调整前启动；这些最后前端改动随后由181项详情定向重新验证，领域写入代码没有再改动。所有运行均用各自合成临时目录，未弱化原保护断言。

DD01–DD48逐项记录、测试节点及证据见[公开验收汇总](docs/detail-dialog/ACCEPTANCE.md)。最终分层结论：47项在所列自动化/HTTP/浏览器层面通过；DD39部分通过，真实软键盘未验证；没有遗留已知失败项。不能把“有自动化覆盖”说成每个DD都在实体手机上运行。

独立审查和浏览器找出的额外问题已经修复并补回归：多个已核对plan在普通刷新中丢失；返回时已重建来源按钮的焦点丢失；同created_at最新进展选择错误；切换目的后问题仍指向默认目的；等待回复覆盖缺钟点/地点的关键问题；跨详情迟到请求抢视图；隐藏来源的finally重启提交；暂停编辑后整理轮询不恢复；未知进展网络结果的再次追加入口失效。

## 四条真实浏览器旅程

只访问8787临时合成QA。完整页面加载原app、matter-routing、flow、attachments、arrangement与新detail模块，不通过脚本调用内部handler冒充点击。

1. 待办29保存含“完成了”的普通进展 → 原activities一次 → 进入精确plan4补10月12日15点 → 原turn一次 → 看到原话保存/整理中及真实已落实 → 返回原待办，下一份草稿、展开和链接焦点保留。task7仍pending，record29仍following。
2. plan3个人安排、plan5外部约定均只有user_review_required且can_apply=false → 由当前核对表单取得明确授权；外部约定显式勾选本次时段转述 → 各一次原confirm decision → 已落实，tasks8/9仍pending，原记录仍following。执行提醒未启用不等于没有日程。
3. plan6当前周四10/8 14点、拟改周五10/9 14点并列 → 明确pause → 原task4保留 → resume选择清空旧点且不勾选启用 → 明确新check为10/7再次核对 → 原执行时间不动，拟改保留，followup_enabled=false，旧提示没有复活。
4. record35完成预览列1个task/1个proposal；填写结果草稿后切换、回来仍保留，取消操作不写完成 → 原lifecycle预览列2条输入、2个有效task/1个pendingproposal → 明确归档 → 原归档列表恢复 → 两记录重新可见，tasks5/6仍cancelled、proposal8仍rejected。浏览器未执行complete-outcome；该写入副作用由合成HTTP/领域测试验证，不冒称浏览器实写完成。

浏览器旅程共9次业务写请求，每次选择的业务目的各调用原接口一次；登录、GET与fixture建立不计入。HTTP只读审查核实持久结果，见[公开验收汇总](docs/detail-dialog/ACCEPTANCE.md)。共享task阻挡通过原合成领域/HTTP测试验证，没有用公开API之外的数据库造关系。

## 桌面、手机和焦点证据

下面尺寸是独立DOM记录的CSS视口测量。公开图片的实际像素另见[合成图片说明](docs/detail-dialog/README.md)，二者分开记录，不从截图像素推断手机软键盘状态。

| 实测 | 观察与证据 |
|---|---|
| 1366×768待办首屏 | A–E顺序，单一h2，C提示top336、输入366–462、主按钮687–731，横向溢出0；[截图](docs/detail-dialog/todo-desktop-synthetic.jpg) |
| 1366×768安排首屏 | 当前事实、三种时间、关键缺口、C输入及唯一主操作；[截图](docs/detail-dialog/arrangement-desktop-synthetic.jpg) |
| 320×640 / 390×844 | 待办/安排两类详情实页面；横向溢出0，手机输入16px，已测控件触点最小44px，主按钮一处；320实测截图保留在本机；公开[390待办](docs/detail-dialog/todo-mobile-synthetic.jpg)、[390安排](docs/detail-dialog/arrangement-mobile-synthetic.jpg) |
| 长标题 | 320尺寸在原h2展开，字号18px，标题一份、主体可滚动、主按钮可达；本机保留截图（未纳入公开Git） |
| 来源返回 | 原草稿“最终返回验证草稿”、E展开、scroll662保留，焦点回到真实raw13来源按钮；本机保留截图（未纳入公开Git） |
| 待办→安排→返回 | 下一份草稿与D展开保留；焦点回“在安排中补充或调整时间”，scroll529；真实未来日程pending |
| 键盘 | 真实Tab从textarea到保存按钮，焦点仍在详情；Escape关闭后回到原入口；重开恢复草稿；AX验证标签、折叠状态、polite回执，最终控制台error列表为空 |
| 输入及错误 | 真实浏览器恢复策略空值显示原生必选提示并聚焦该select；必选项补齐后可提交。服务409/422、未知网络、提交中新增文本/文件由真实DOM事件和HTTP保护测试验证，未称真机键盘下字段错误已通过 |
| 缩短视口模拟 | 320×360聚焦输入，检查输入和固定主按钮及主体滚动；本机保留截图（未纳入公开Git）。这是遮挡模拟，不是真实软键盘 |

测量见[公开验收汇总](docs/detail-dialog/ACCEPTANCE.md)。读取的是当前DOM几何与可见控制，不用源码字符串断言代替布局、滚动和焦点验证。临时QA工具条只为合成入口，未进入产品源码；一次在手机背景页面遮挡点击后，改用真实键盘进入归档列表，未把该遮挡归为产品按钮缺陷。

## 边界、剩余验证与后续能力

- DD39：Windows内嵌浏览器只能调整视口，无法触发真实手机软键盘；需要iOS Safari与Android Chrome真机验证输入末行、服务错误位置、键盘收放和固定按钮。已实现visualViewport/resize聚焦可见处理，但本轮不声称真机验收通过。
- 原完成snapshot算法未改变。只读preview包含Store当前实际影响的同owner外部target-only提案，但原snapshot未将所有外部target-only提案的后续变化纳入签名。本轮不宣称冻结该额外范围。若需更强并发保护，须另行定义原完成契约，不能在本次前端改版中偷偷扩充。
- 跨接口文字＋结构原子混合写、全新的仅完成task版本契约、全天任务、自动联系客户、手机推送均属明确后续能力。
- 多活动“查全”目前为明确用户入口，利用原列表分页和精确plan GET，不添加新关系表或全量自动扫描。初次打开仍显示当前GET已知关联，查全后刷新重验，不按标题猜关系。
- 未启动正式模型、聆记、语音、企业微信或公网集成；本轮验证合成场景和既有规则，不据此宣称外部服务联调成功。

## 保存、集成与数据保护

初始234份源码/测试文件已留baseline-source.zip和SHA。当主目录新增Git/AGENTS后，继续使用独立checkout及detail-dialog-d1-d5分支，以已审查ZIP为基线。独立审查记录方法AST与原完成契约一致。

16个授权路径（含新交付报告）已回写指定目录。回写前核对当前hash，回写后与经过测试的独立目录逐一核对一致；225个非授权基线文件保持原样，现有新文件和管理规则未被复制覆盖。只集成本次reviewed路径和指南/报告；保留正式配置、数据库、其他工作文件及原居中弹窗/密度改进。不重启8765，不部署、不推送；运行中的原服务需在用户自行重启后加载完整新后端。
