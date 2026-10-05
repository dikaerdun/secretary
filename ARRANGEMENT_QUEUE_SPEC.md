# 私人秘书待落实安排研发规格

日期：2026-10-05。读者：产品、后端、前端和测试。本文定义把模糊活动逐步落实为日程的业务契约，配套开发顺序与验收场景见 [研发批次与验收](ARRANGEMENT_QUEUE_IMPLEMENTATION.md)。本文是待实现规格，不代表功能已交付。

用户明确说“本周或本月把安排定下来”时，表示确定安排的期限；“本周去拜访”表示执行窗口，不能写成确定期限。“本周约一下”等含糊表达保留原话及未知含义，只核对这周执行还是这周定时间。实际活动可以在确定期限之后执行。用户通过一个待落实队列补信息、协调、选择方案；秘书保存进展、给出下一步并在适当时点提醒。确定安排、活动完成、事项目标达成分别处理。

用户本次已授权审查、优化并完成开发；配套批次按B1至B7推进。本文仍是实现与验收契约，文档更新不代表功能已经通过测试或交付。正式资料不用于开发演练；上线迁移须先在数据库副本验证保护契约。

## 已确定的产品原则

1. 前台使用一个待落实队列；待完善、待协调和待核对是信息卡点，可以同时存在，无需逐级走完。
2. 确定安排期限、下一推进点、实际执行时间独立保存。现有提前执行提醒另外保存，不与这三项混用。
3. 同一次活动有多个候选时段时，仍是一条安排。实际有多场活动才创建多条安排，关联同一事项。
4. 自己能决定的行动不要求对方确认。外部约定分别记录用户的决定和对方是否答应。
5. 用户明确给出完整安排指令时直接落实。只有用户要求核对、关键条件缺失或指令不明确时才等待，不追加统一确认步骤。
6. 每次追问优先解决一个关键缺口。原文、已确认部分和每次进展继续保留。
7. 推进点变更、安排落实、暂缓或取消后，旧推进提示失效。不确定改期保留原有效日程。
8. 落实一次安排不结束事项或项目。准备资料和会后跟进继续作为关联工作存在。

## 第一版范围与默认策略

第一版包括自然语言及结构化补充、候选、确定期限、推进点、暂缓恢复、改期、统一列表、持久的网页提醒、与现有日程及事项的关联。沿用现有口令、所有者隔离和本机运行方式。

以下是为研发确定的第一版默认值，可以由后续用户反馈调整；它们不是用户已经指定的个人偏好。

| 项目 | 第一版默认 |
|---|---|
| 队列需要达到的结果 | 确定具体执行时间，并满足该活动必要的约定条件 |
| 初次打开待落实队列 | 默认全部待落实，含没有确定期限或推进点的安排；今天、本周和本月是主动选择的筛选 |
| 期限强度 | “必须、最晚、截至”为 required；“希望、尽量”和普通“本周定下来”为 target，卡片展示区别 |
| 未指定提前执行提醒 | 沿用已有明确偏好；没有偏好则不发执行提醒，日程仍可成立 |
| 没有下一推进点 | 提供可调整的策略建议，首条回执展示；用户启用队列默认推进策略后可以自动采用，未启用则不静默创建提醒 |
| 提醒渠道 | 持久网页提醒；页面关闭时保留到期状态，重新打开可见。不以此宣称手机推送已送达 |
| 单次推进提示 | 一个有效推进点提示一次，处理前保留在列表，不自动每日重复催问 |
| 期限提示 | 默认在到期前一天提示一次、过期后提示一次；遵守用户明确的回看前安静要求，同日同安排与推进提示合并 |
| 外部发送 | 本版不新增企业微信连接、手机推送或通知授权；保留未来接入独立渠道的接口 |

综合健康评分、成交概率、自动联系客户、自动代约、循环活动、全天日程、多人调度和外部日历同步均不在本版范围。项目推进查漏是后续能力；本版只确保这次活动的安排能持续落实。明确全天要求可以保存，但不能伪造成00:00开始、用时30分钟的任务，也不能声称全天日程已生效。

## 对象与生命周期

复用现有 crm_secretary_plans。一条 plan 表示一次要落实的活动；原始记录、附件、客户交流、联系人、项目和事项继续使用现有 ID。不要新建第二套预约对象，也不要把推进检查伪装成 tasks。

| 维度 | 状态或来源 | 含义 |
|---|---|---|
| 安排推进 settling_state | pending、settled、paused、abandoned | 待落实、已落实、暂缓推进、放弃本轮协调 |
| 执行日程 | 现有 tasks 与 active_schedule | 是否有真实有效日程，以及日程是否完成或取消 |
| 事项和项目 | 现有状态 | 长期目标、动作及业务结果 |

pending 可以与有效旧日程共存：原来周四已约好，正在商量改到周五。页面并列展示“当前有效安排”和“拟改安排”。paused 只停止推进提示；abandoned 只放弃本轮协调。二者都不隐含取消有效日程。

“取消这次活动”是更强的明确操作：同时取消该 plan 的有效日程并结束本轮协调。“不再商量改期”只放弃改期，原日程继续有效。“改期，时间还没定”只表明新时间未知，不能推断原时间已取消；“原周四不去了，新时间还没定”才撤销旧日程。对象或意图不明确时先问一个问题，不能根据“先算了”取消整个项目。

已安排后的资料准备缺项在关联待办中处理，不自动把计划重新放回确定时间的队列。只有用户明确再协调时间或撤销原安排，才开启新的协调周期。

从settled或abandoned重新协调时建立新cycle，旧周期保存在历史；新确定期限、推进点及silent_until默认清空，仅按本次明确输入设置，不继承旧期限的超期标记或旧周期通知。paused的resume沿用原cycle与期限，按恢复门控检查旧推进点；pending内补候选、改期限或更新进展不新开cycle。旧有效日程继续保留，除非明确撤销。

## 时间语义与边界

| 字段 | 含义 | 允许影响 |
|---|---|---|
| settle_deadline | 最晚或希望何时定下安排 | 队列分组、临近期限及安排未落实提示 |
| next_check | 何时询问、补信息或核对回复 | 推进提示及今天要推进视角 |
| proposed_execution | 正在讨论的执行日期、时段或具体时间 | 候选与待核对方案 |
| active_schedule | 已生效执行安排 | 正式日程、冲突检查和执行状态 |
| execution_reminder | 现有 reminder_at 或提前提醒偏好 | 正式执行通知 |

相对时间以用户提交这轮输入时的北京时间为基准，异步处理、刷新或重启不得重新解释成另一天。原话和解析基准均保存。

先根据当前原话及有效上下文判断时间的作用，再解析精度和日期。下表的周/月/天期限规则只用于已明确分类为settle_deadline的表达。“本周去拜访”保存proposed_execution的window；缺钟点仍待落实，不自动赋予确定期限。“本周约一下”没有足够依据时不代填deadline，最多追问一次“想这周去，还是这周定时间？”，原文先保存。不能因为某个值只有日期或时段精度就把它分类成期限。

| 原话 | 解析规则 |
|---|---|
| 本周内 | 提交所在自然周，周一至周日；显示最晚确定的周日日期 |
| 本月内 | 提交所在自然月最后一天 |
| 下周或下月底 | 下一个自然周或自然月的结束日期 |
| N天内 | 提交本地日期加N个日历日，作为日期期限；回执展开具体日期 |
| 一个月内 | 提交本地日期加一个日历月；无对应日时取该月最后一天 |
| 周五再问 | 保存日期精度的 next_check，不声称用户指定了钟点 |
| 周五上午再问 | 保存时段精度及原词；展示上午，不能改写成用户说了9点 |
| 只需先定哪天 | 将该轮完成标准降为日期；不自动创建带猜测钟点的正式日程 |

日期期限包含该日全天，次日北京时间00:00起才判期限已过。规范化 deadline_end_at 保存为下一日的排他边界。具体钟点期限如“周三12点前定”则以该时刻为排他边界，到12点即已过，不能套次日边界。时段期限如“周三上午内定”以规范化时段结束为边界，界面继续显示上午内；“上午前”含义不明确时需核对，不能猜。当天提示“今天要定下来”；required 期限过后显示“最晚确定期限已过”，target 显示“超过希望确定期限”。活动执行时间晚于确定期限是正常情况。

时间值使用 time_spec：precision 为 date、window 或 instant；保存本地 date、timezone、原词，以及有依据时才提供的起止时刻。内部通知投递策略可以选择时段起点或汇总时点，但它的来源是 policy，不能当成用户声明的具体时间。第一版网页日期提示从当地当天开始可见，无需补造执行钟点。

next_check_at查询投影：date取当地该日00:00，instant取明确时刻，window取时段起点。第一版中文时段策略为上午09:00至12:00、下午14:00至18:00、晚上19:00至22:00；保存原时段词和policy来源，不把起点写成用户指定钟点。它只决定何时进入网页提示，不创建执行日程。

暂停/隐藏后明确恢复推进时，旧点能否沿用另按精度判断：date在次日00:00以前、window在窗口结束以前、instant在其明确时刻以前仍未过期。当天中途或时段内恢复，可明确沿用该planned点，已到发布边界则显示当前提示；到上述结束边界即需新选择。next_check_at不是这项恢复判定的过期边界。普通sweep及服务重启仍保留已到但未处理的有效点，不能因上述边界到达就自动吞掉待处理提示；这项规则只用于用户暂停/隐藏后的恢复选择。

next_check 晚于确定期限时返回 attention_flags，提示预计超过确定期限，提供提前回看或调整期限。保留用户原选择，不静默改两者，也不悄悄追加补救提醒。

未启用默认推进策略、也未给推进点时，卡片显示“下一推进点待定”并提供一项建议。期限尚未过时，默认建议规则为：距确定期限0至2天，建议当天回看；3至7天建议2天后；超过7天建议7天后；建议日期不得晚于确定期限。期限已过时不套用上述算法，显示现在推进、延期或暂缓的选择，不自动建立过去或当天的检查点。没有期限也没有推进点时只保留队列，不建立无限循环。上述按本地日历日计算，instant/window取其本地截止日期并另验排他边界；明确标注“秘书建议，可调整”。用户启用默认策略后才允许采用建议。已明确给出的推进点不受自动策略开关影响。

新增用户偏好arrangement_auto_check默认false，复用秘书设置的带revision读写契约，入口为队列“提醒设置”，保存后重启保持。它只允许在没有明确推进点时采用上述策略，不修改已建立计划，也不代表同意执行安排。设置表采用显式列名写入或偏好JSON，避免扩展列后破坏旧INSERT VALUES。deadline提示默认启用；单条安排可停用全部推进提示，且持久保存。

## 信息、约定与执行权限

一次安排至少有可理解的标题或活动内容。客户单位、正式联系人ID、项目金额、详细业务目标和电话号码不应成为建立私人安排的强制前提。归属有歧义时保留原话，显示需核对对象；禁止猜测正式关联。

decision_mode 为 self、external 或 unknown，依据当前明确意图及上下文确定。“我周四给他打电话”属于 self；“约他周四一起开会”属于 external。涉及外部联系人并不自动要求对方承诺。

| 活动 | 可以正式安排的条件 |
|---|---|
| 自己整理材料或主动打电话 | 当前用户明确决定，执行日期与钟点明确，内容可识别 |
| 与对方会面或预约通话 | 上述条件，加对方对当前时段同意的依据 |
| 用户明确全天活动 | 保存全天要求，本版显示尚不支持正式全天日程；不能由未知钟点推断全天或伪造定时task |
| 用户只要求先确定日期 | 本轮settlement_scope=date；达到日期及必要约定条件后settled，不创建或改动task及执行通知 |

地点只在当前活动要求时成为落实条件。自己准备、电话不要求地点；见面地点可以标注待定。用户明确要求“时间地点都定下来”时，地点纳入本轮完成标准。这样区分“时间已经确定”与“其他准备信息仍需完善”。

settlement_scope默认execution_time，只有用户明确“先定哪天即可”才改为date。日期目标落实后，原plan保留在详情和对应日期的日程视图，标记“日期已定，钟点待定”，不占时段、不参与定时冲突、不标全天或定时scheduled。无旧日程时task数量为零；若正商量旧日程改期，原有效安排继续保留，日期标记写明“拟改日期已定”。提供“继续定钟点”操作：同plan开启新的协调周期并改回execution_time，保留已同意日期，只核对钟点及受影响条件，不重催已完成日期目标。旧日期目标的期限、推进点和安静边界进入历史，新周期三者默认清空；本次可明确设置新的期限和推进点。

agreement 保存对方约定状态：unknown、pending、agreed、not_required，以及reported等依据、连续原话、所确认字段范围和对应值签名。字段范围至少区分date、time；仅在本轮要求地点为落实条件时加入place，其他必要条件同样按字段保存。已确认字段的依据独立保留；只有本轮必要字段的有效依据齐全时，才可派生整体agreed。用户明确转述“李总确认周四下午三点”可覆盖日期与钟点；“李总确认12号，几点没定”只覆盖日期。已发邀请、对方没拒绝、附件记载或AI建议不能替代同意。无上下文依据的“三点”继续核对上午/下午，只补这个时间缺口；不能重复追问已具备依据的字段，也不能把日期同意扩大为对新钟点的同意。

日期目标已落实后继续定钟点，原来的date依据在日期未变时继续有效。用户补“安排12号15点”只表达自己的决定；若没有对方同意15点的依据，只核对对方对钟点的回复，不重新问12号是否答应。改为13号则date依据失效；保留原依据进入历史，不把旧同意静默移到新日期。结构化确认绑定页面实际展示的字段和值，不能笼统勾选“已同意”来覆盖未展示的新条件。

application_authority 保存用户是否允许落实：none、direct_user、user_reviewed，以及当前turn或页面核对记录和确认内容签名。明确“就这样安排”或完整个人执行指令可直接落实。用户说“先给我核对、别安排”时必须保留待核对。选择一个候选不自动代表对方同意。

blocking_reasons 是服务端派生的信息卡点，可以同时包括缺内容、缺日期、缺钟点、等待对方、候选待选、请用户核对、身份需核对及日程冲突。waiting_for 保存在等谁、等什么、当前依据；last_progress 保存最新进展。不要让用户维护一套必走阶段。

候选使用稳定 candidate_id。候选日期、钟点、地点或必要约定条件变化时，仅对应字段的确认依据失效，未变字段继续有效；候选总签名用于检测内容变化，但不能据此全清或全沿用局部依据。补一个附件或改展示标题不得无条件重新问对方是否答应。

## 数据与写入契约

crm_secretary_plans 增量增加 schema_version、settling_state、settling_cycle_id、followup_version、deadline_end_at、next_check_at、followup_enabled、followup_disable_reason、followup_dirty、followup_hold_until、hold_turn_id、hold_generation。现有 revision 继续控制整条计划的并发修改；followup_version 只控制推进通知有效性，不能借用 task revision。

data_json 增加 settle_deadline、next_check、silent_until、decision_mode、settlement_scope、proposed_execution、candidates、selected_candidate_id、agreement、application_authority、waiting_for、last_progress、execution_reminder。silent_until保存下一回看的发布边界及来源，不接受无依据的隐式延后。现有 active_schedule 从真实task派生，禁止在新JSON中另存一份可独立修改的“正式日程”。blocking_reasons 和 can_apply 每次由服务端计算。

settling_state 和版本标量列是权威状态；时间对象以规范化JSON为权威，deadline_end_at、next_check_at 是同事务更新的查询投影，写入后校验一致性。所有写入经过同一 ArrangementQueue 服务，禁止前端直接拼SQL或各接口自行维护投影。

next_check 至少包含稳定 check_id、time_spec、action、origin、source_turn_id、status和handled_at；status为planned、handled、obsolete或needs_selection。deadline 保存 strength、time_spec、原话和依据。版本历史要能解释谁改了什么、为何修改、哪些旧提示作废。settling_cycle_id 只在重新开启协调时增加，不因“还在等”或页面刷新增加。

用户报告该点的实际进展，如“问过了，仍没回复”，或选择明确的延期、暂停及落实操作，才消费当前检查点。打开详情、标记已读、改标题、追加附件不消费。处理后没有新点时显示下一推进点待定并给一项建议；默认策略未启用则不循环。仅说“还在等”而未说明是否做过当前行动时保存等待进展，不自动声称已询问；有必要时只问一次是否已联系。

followup_enabled是持久的发布门控。暂缓、放弃、隐藏来源、明确停催及恢复后需重新选择推进点时停用；worker不得仅因状态变回pending而启用。恢复前未过期且仍planned的检查点可在明确恢复协调操作后沿用；过期点标obsolete并保持停用，只有选择现在处理或新点才启用。归档恢复只恢复可见性，停用原因source_hidden保持直到明确恢复推进。

data_json持久保存followup_epoch，新计划及旧计划缺省为0。明确resume推进时在同一cycle内递增epoch；若沿用未过期检查时间，也生成新check_id，旧检查与旧通知仍留历史。期限提示去重键包含epoch，使同cycle的新明确恢复可产生新里程而不复活obsolete行。普通进展、刷新、服务重启、仅恢复来源可见性均不递增epoch或启用门控。过期点仍按恢复门控要求选择，不能用新epoch绕过；已过期限仅保留当前overdue，不补near。

新建且可见的pending计划默认followup_enabled=true，用于默认期限提示及用户明确设置的推进点；没有推进点不因此生成check。迁移计划默认关闭。仅停用原因legacy_no_opt_in时，本版首次明确设置期限或推进点可启用；paused、source_hidden或user_disabled等原因不会因补期限、改标题或恢复可见而被覆盖，必须明确恢复推进/启用。set_check=null只清空检查点，期限提示仍按门控处理；followup_enabled=false才停用全部推进提示。

新增crm_arrangement_operations保存结构化写入账本：owner、request_id、plan_id、operation、payload_hash、base_revision、result_revision、response_json、before_json、after_json和created_at，唯一键为owner与request_id。账本与副作用同事务提交。自然语言仍使用现有turn账本和历史；统一详情按时间合并两类历史，不为页面按钮伪造口述原话或硬塞一个不存在的turn_id。

安排与现有客户、联系人、项目、事项、来源关系在写入和落实前重验。事项生成的动作应继承已核对项目关联；同一目标下的完成反馈下一步应保留事项关联。跨目标下一步需核对归属，不笼统吸收所有子记录。

## 操作与状态变化

| 操作或用户表达 | 状态和效果 |
|---|---|
| 新建一条未来安排 | 保存来源与plan；进入pending；无执行task或执行通知，除非输入本身完整明确 |
| 更新进展或补信息 | 更新原plan，不新建重复等待事项；期限及候选保持，除非本轮明确更改 |
| 选候选 | 更新proposed_execution和所选ID；重算约定与权限；仍可能pending |
| 改确定期限 | 只改settle_deadline；不改执行时间；重算期限提示 |
| 改下一推进点 | 只改next_check；旧推进通知作废，新提示按新点生成 |
| 暂缓协调 | paused；停止推进提示；保留候选、期限、原文和有效执行日程 |
| 恢复协调 | pending；未过期planned点可在明确恢复操作后沿用；过期点持久标obsolete且followup_enabled=false，选择新点前不发布旧提示 |
| 放弃本轮协调 | abandoned；停止推进提示；旧有效执行安排不变 |
| 明确取消这次活动 | abandoned；原子取消该plan有效task及其执行通知；不结束项目或其他活动 |
| 可能改期，仍等回复 | 开启pending协调周期；原task和执行通知继续有效；拟改候选单独显示 |
| 明确原时间不去了，新时间未定 | 取消旧task和执行通知；该plan进入pending等待重新落实 |
| 确定首次安排 | 通过全部条件后建立一项task，settled，结束本轮推进提示 |
| 仅确定日期目标 | settlement_scope=date时settled；不新建或改动task，保留日期标记、可能存在的旧有效安排及继续定钟点入口 |
| 继续定钟点 | 同plan开启新周期，保留已同意日期，pending；旧期限/推进点/安静边界进入历史，新周期默认清空并按本次明确输入设置 |
| 确定改期 | 原子更新同一有效task及执行通知；保留历史，settled |
| 新安排有冲突 | 保留原task和候选；pending显示冲突，不能先取消旧安排 |
| 到了确定期限 | 保持pending，派生期限已过标记；不自动取消、完成或执行 |
| 日程完成或记录会后结果 | 更新执行/结果状态，保留事项及后续行动；不因旧确定期限过了重新催约 |

操作提交立即返回“原话已保存、正在整理”或结构化操作成功回执。只有日程事务真实提交后，才能说“已安排”；资料保存、方案选择、模型输出都不等同于落实成功。

## 正式日程与推进提醒分离

正式task的执行时刻仍由现有日程契约表示，执行通知使用已有notifications。建立正式日程不依赖能否计算提前通知时刻。新服务必须显式区分默认通知、明确不提醒和具体提前提醒，并保留旧调用方行为。

Store的propose→confirm契约持久保存execution_notification_at的三态：字段缺省表示旧调用方的既有默认策略；显式null表示不创建执行通知；具体时刻表示该执行通知时刻。新安排没有明确提醒偏好时必须提交并保存显式null，不得在确认时回退成默认提醒。提案保存、调整、确认、重启恢复和同请求重放都保留三态；不能只在即时_schedule调用中区分、却在提案JSON或确认路径中丢失。该字段不影响是否建立日程。

新增 crm_arrangement_notifications 专门保存推进提醒：id、owner、plan_id、settling_cycle_id、followup_version、dedupe_key、kind、due_at、available_at、status、token、lease_until、attempts、published_at、read_at、payload_json。kind 支持 check、deadline_near、deadline_overdue，status 支持 queued、leased、published、obsolete。

唯一键为UNIQUE(owner,plan_id,dedupe_key)：check 使用cycle与check_id；期限提示使用cycle、followup_epoch、规范化期限签名和kind。obsolete行永不复活；明确resume建立新epoch及必要的新check_id，区别于旧里程。不同plan同一天到期不能碰撞。普通进展更新或通知失效重建不能让同一已发布里程点再次发布。更换期限或明确设置新推进点可以产生新提示。

每个check、deadline_near或deadline_overdue原因保留各自的里程行、去重键、版本、发布/已读及失效记录，不物理折叠为一行。网页按owner、plan_id和提示可见的北京时间local_date聚合成一张卡；同日两条不同安排仍各有一张。聚合只包含当前仍有效的已发布原因，原因之一失效不能吞掉其他有效原因。未读数按可见组计算，标记一组已读只更新该组当前已发布成员的read_at，保留各行published_at与原依据；它不消费检查点、不预读以后才出现的原因，也不通过普通进展把已读原因重新变为未读。

reconcile对仍有效的同里程点：未发布行更新为最新followup_version并清除旧lease，不能在唯一键下重复INSERT；已发布且尚未处理行保留published_at/read_at并绑定最新有效版本，前台结合最新plan显示，不新增未读或再发布。检查点处理、期限改变、暂停/落实等导致里程点失效时才标obsolete。历史payload保留原发布依据。

有效提示同时满足plan及来源可见、settling_state为pending、followup_enabled=true、版本一致、没有有效处理中的进展更新、对应检查点仍planned或期限仍有效，并满足回看前安静要求。领取、发布前和ack/retry都检查。进展修改与领取/发布使用现有共享业务锁；网页发布的版本检查与持久写入还须同一SQLite事务，跨worker不能靠各自内存锁保证。仅在ack检查不能阻止旧提示已被发出。

对明确plan_id提交进展时，在保存turn的同一事务暂挂旧推进通知，标记followup_dirty，并保存hold_turn_id、单调hold_generation及到期时间。应用、失败和超时释放均须条件匹配当前turn及generation。超时sweep同时失效该generation的处理租约；迟到结果不得提交。旧失败回调不能释放新turn的hold。模型失败或租约过期后，按最后已提交的业务状态和持久门控恢复可见到期项，并显示新补充未处理成功。不能永久吞掉提醒；新的人工操作优先。

第一版发布是把有效提示持久写入网页提醒列表，不代表用户已经读到或手机响铃。read_at只是看过，不代表处理完成。网页暂停使用或服务重启后，按当前状态恢复一份有效提示；不补发每一个已错过的历史核对点。

推进worker仅由web.cleanup_ctx创建、启动并在退出时取消和await关闭；每个应用实例注册一次。ArrangementQueue/提醒服务的构造、迁移、normalize、get/list/count及只读DTO不得启动线程、异步任务或调度器；local.py不再另外启动同一worker。同步构造服务不要求存在事件循环，cleanup后不留后台任务或未释放资源。

deadline_near对日期期限默认在期限前一日本地日期出现，对instant期限默认在明确时刻前24小时出现；window期限按规范化窗口结束前24小时。deadline_overdue在相应排他边界后出现。创建或恢复时临近时点已过去，不补发旧near；已过期只保留当前一份overdue。若同时到了next_check，合并理由。

用户明确“周五再问/周五再看”时，保存silent_until为这次next_check的发布边界，check及期限提示都不能提前打断。期限已过的卡片标记仍更新；回看点早晚冲突在本轮回执解释，不另发催问。到回看点才合并当前仍适用的原因，已经过时的near不补发。普通附件/标题编辑不取消silent_until；新的明确推进选择可覆盖它。

## 接口契约

自然语言沿用 POST /api/secretary/turns，增加安排相关意图和有证据的字段；既有plan_id、expected_revision、request_id继续生效。相对日期解析使用turn提交时间。保留附件与他人引用不能触发安排的保护。

| 接口 | 用途 |
|---|---|
| GET /api/secretary/arrangements | SQL筛选、分页、排序及全量counts；不使用先LIMIT200再过滤的旧plan列表 |
| GET /api/secretary/plans/{id} | 沿用详情，增量返回arrangement、active_schedule、blocking_reasons、can_apply |
| POST /api/secretary/plans/{id}/arrangement-decisions | 结构化操作；与自然语言调用同一服务，不另写状态机 |
| GET /api/secretary/arrangement-notices | 已持久发布且仍有效的网页提示、未读数及分页 |
| POST /api/secretary/arrangement-notices/{id}/read | 以owner所属的当前有效提示成员定位可见组，标记该组当前已发布成员看过；不完成检查点、不修改安排 |

arrangement-decisions 的operation枚举：update_progress、set_deadline、set_check、select_candidate、confirm_arrangement、pause、resume、abandon_coordination、start_reschedule、continue_set_time、withdraw_execution、cancel_activity。每项使用严格的字段白名单。新增安排继续通过turn建立，避免再造来源和对话入口。

| operation | 除通用请求字段外允许的输入 | 日程版本要求 |
|---|---|---|
| update_progress | progress_text、check_id、check_handled、waiting_for、next_check | 不写日程时无需task版本 |
| set_deadline | settle_deadline或明确null清空 | 无需task版本 |
| set_check | next_check或明确null清空、followup_enabled | 无需task版本 |
| select_candidate | candidate_id | 无需task版本；只选候选不改日程 |
| confirm_arrangement | candidate_id或proposed_execution、agreement_attestation、settlement_scope | 有有效task时必须expected_task_revision；日期目标不建task |
| pause | reason | 无需task版本，不取消执行 |
| resume | next_check或沿用未过期点的明确选择、followup_enabled | 无需task版本 |
| abandon_coordination | reason | 无需task版本，不取消执行 |
| start_reschedule | proposed_execution或candidates、settle_deadline、next_check | 带有效task版本，确保用户看到的是当前旧安排 |
| continue_set_time | next_check、settle_deadline；省略时新周期清空旧值 | 日期目标新周期；无有效task时无需task版本 |
| withdraw_execution | reason、继续协调的明确选择 | 必须有效task版本 |
| cancel_activity | reason | 有有效task时必须task版本 |

结构化勾选对方已同意属于用户当前reported attestation，保存页面展示的确认字段范围及值签名，不能伪装成外部平台回执，也不能扩大局部同意范围。write响应统一返回plan_id、revision、arrangement、active_schedule、receipt、effect和attention_flags；effect分别说明saved、date_settled、scheduled、rescheduled、paused或cancelled等实际结果。

安排业务写入携带request_id、expected_revision；涉及有效日程按操作表带expected_task_revision。同请求同内容先从账本返回原回执，不因当前revision已变而重做；同ID不同内容返回409。不同请求的过期revision返回409并带最新版本摘要；语义或字段无效返回422并保留原话；对象不可见或不属于该owner返回404，权限失败沿用401/403。页面保留用户尚未提交的草稿。notice/read仅按owner和notice_id幂等，不要求plan revision，也不修改业务版本。

列表view为today、week、month、all，未指定view时默认all；state默认pending，可显式查paused和abandoned。today包含到本日应推进和仍未处理的旧推进点，以及已过确定期限的安排；week/month按确定期限归属自然周/月，绝不按执行时间分组。无期限项在all的“期限待定”分组可见；只有将来执行时间、没有确定期限，不应猜一个期限。当前today/week/month为空时，仍展示同一组客户/项目等归属筛选下的全部待落实总数及“查看全部”入口，不能暗示没有未完成安排。新安排回执提供进入同一plan的入口及全部队列入口。

客户、联系人、项目、事项、卡点可组合筛选，全部在SQL层分页前应用。counts从完整匹配集计算；排序为期限已过、今日应推进、临近期限、其他待落实，再按有效推进点/确定期限与plan_id稳定排序。相同plan可以属于多个视角，但每个视角内只出现一次。

## 页面与交互契约

保留现有导航，在日程入口提供“待落实安排”和“已安排日程”两个视角。初次进入队列默认全部待落实；今天要推进、本周要定、本月要定是主动筛选，保留查看全部总数与入口。首页只摘要需要处理的安排并链接同一队列，不复制第二套状态。没有期限/回看点的新安排也必须能从回执和默认队列找到。

卡片包含活动标题、希望或最晚确定日期、当前进展/卡点、下一步和处理时间。已有旧日程时另显示当前有效安排与拟改安排。执行时刻未知就明确写未知；不要用确定期限冒充活动日期。

主要操作优先级：有冲突先核对冲突；等对方时记回复；多个候选时选方案；需要用户核对时核对安排；缺具体时间时确定时间。其他字段与改期限、改回看点、暂缓、放弃协调、取消活动放在详情或更多。明确区分后两者的影响。

继续说话、语音转写、附件和结构化按钮都带同一plan及版本。保存一次，同步刷新队列、日程、客户与事项相关视图；后台刷新保留输入焦点、草稿、筛选和滚动位置。提醒点击进入同一安排，不新建事项。

同request重放返回原成功回执；新request指向当前revision且当前已落实内容签名相同，可以返回已落实而不重建。其他页面已更新的过期确认仍须409，不能因为候选ID相同就绕过版本检查。展示最新内容与保留的输入。320和390像素宽度应可完整阅读主要信息和操作，键盘可以打开详情、提交和关闭。

## 现有实现接入点与兼容要求

以下是2026-10-05扫描所得，实施前需重读当前文件，避免其他研发并行修改造成行号漂移。

| 文件与方法 | 本版接入职责 |
|---|---|
| secretary/secretary_flow.py 的submit、_context、_checked_changes、_apply_turn | 保存解析基准与证据；调用统一安排服务；处理hold、版本与回执 |
| secretary/secretary_flow.py 的_schedule、_public_plan | 分开候选与有效安排；修正暂定改期撤旧安排、无提醒不建日程的分支；兼容旧详情DTO |
| secretary/secretary_interpreter.py 的SYSTEM、FIELDS、interpret | 增加确定期限/推进点/候选/决定方式等语义，逐字段验证当前原话 |
| 新secretary/arrangement_queue.py | 数据升级、时间规范化、派生卡点、操作、筛选、安排落实和状态历史 |
| 新secretary/arrangement_reminders.py | 独立outbox、去重、发布、当前版本检查与恢复 |
| secretary/store.py 的_schedule及提案确认 | 明确执行时刻与执行通知参数；旧调用缺新参数时保留旧行为 |
| secretary/web.py | 结构化接口、提醒读取；唯一通过cleanup_ctx管理推进worker；共享锁与CSRF沿用 |
| secretary/matter_flow_integration.py、sales_workspace.py | 确认安排/动作的事项和项目关联，以及同目标下一步的归属继承 |
| secretary/static/secretary-flow.js、app.js与新arrangement-queue.js | 统一队列、卡片动作、候选、刷新与现有日程投影 |
| secretary/record_lifecycle.py、matters.py、local.py | 隐藏/恢复联动、事项关系、本机能力说明；local不重复启动推进worker |

旧数据迁移只新增列/表和保守状态映射。有效或已完成旧task对应settled；旧待定计划pending；明确取消且无有效task的旧计划为abandoned；只有复盘原文却无明确取消/完成证据时不推断结束协调。未知期限、推进点、约定证据继续未知，不借用tasks.deadline_at或旧行动check_date填充新字段。迁移的旧计划followup_enabled=false并标legacy_no_opt_in，隐藏来源以source_hidden为停用原因；本版首次明确设置推进点或期限的启用条件遵守上述持久门控，恢复可见不能启用，不自动补发历史提醒。

原始记录、task/plan/customer/matter ID、口令及数据库路径保持；已有正式提醒不重置。归档或回收的来源停止队列提示，恢复只恢复可见性，不悄悄重新启用提醒或恢复已取消日程。当前已获开发授权；交付迁移先在正式数据库的副本演练并比较业务行摘要，由主线程按验收门禁执行，不能将正式客户资料用于构造数据或浏览器演练。

## 研发完成标准

必须通过配套AQ01至AQ65验收矩阵，尤其是时间意图与三个时间独立、局部同意不被扩大、模糊改期保留旧日程、旧提醒不复活、候选不会多建安排、无提醒也可有日程、无期限项默认可见、超过200条仍不漏项、以及安排落实不结束项目。旧“改期时间还没定即撤旧日程”的断言属于此次明确调整的需求，必须替换为模糊改期保留、明确撤旧取消的成对测试，不能简单删除回归保护。

产品验收以四条连续旅程为主：自己决定时间；等待对方答复；有旧安排的候选改期；超过确定期限后延期或暂缓。每条旅程均需从输入、队列、提醒、日程和事项视图检查同一份事实。实际外部模型质量及手机送达单独验收，不以离线断言代替。
