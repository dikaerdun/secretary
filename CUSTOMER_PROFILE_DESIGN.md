# 数据安全与商用密码客户秘书：画像与语音操作

## 任务卡与交付范围

用户授权扩展现有后台：用企业微信语音建客户、补充客户与联系人的喜好/画像、记录沟通与安排跟进，并改善实际使用体验。行业为数据安全、商用密码。继续内网部署，不改公网入口。

Protocol mode: manual_fallback，采用 Planning / Parallel Work / Test-First / Completion Verification；对既有页面先做Product Design体验检查，再沿用现有深绿与浅色设计，不另起视觉方案。

项目起点：客户只有六个基础字段；公司与联系人混在一行；没有属性来源；语音仅支持事项提案；画像更新没有待确认差异。因此不能把现有“语音记事”宣称成“语音管理客户”。

## 客户属性完整设计

采用公司客户→多联系人→持续沟通→跟进任务，基础名称必填，其余逐步补齐，不要求一次填完。

| 分组 | 字段与用途 |
| --- | --- |
| 客户背景 | industry行业、region区域、organization_type机构类型、business_context业务/系统背景、lead_source线索来源、relationship_status合作现状 |
| 安全与密码场景 | data_scope数据范围、security_scenarios场景、existing_systems现有系统/供应商、crypto_needs密码应用需求、deployment部署环境、compatibility信创/接口适配、compliance_needs密评/等保等诉求、assessment_status测评或整改现状 |
| 需求与验收 | pain_points痛点、requirements范围、success_criteria验收标准、poc_plan测试验证、delivery_constraints交付约束 |
| 商机推进 | budget_notes预算来源/口径、procurement_process采购路径、decision_chain决策链、timeline项目节点、competition竞争情况、blockers阻力、next_visit_goal下次拜访目的 |
| 联系人 | name姓名/称呼、role职务角色、phone电话；同一公司多联系人，技术评审/业务使用/采购/决策人分别记录 |
| 联系人画像 | authority决策影响、concerns关注重点、communication_channel沟通渠道、contact_hours联系时段、detail_preference材料深度、interests明确提及喜好、avoidances沟通注意事项、relationship_notes关系背景 |

现有stage、amount_cents仍用于客户当前阶段与当前商机金额；完整画像字段都是可选、可追溯的文本事实，不自动计算成交率或敏感心理标签。

密评是对密码应用合规性、正确性、有效性的评估，字段用于记录客户实际诉求，不根据行业自动断言法定义务。[国家密码管理局解读](https://www.oscca.gov.cn/sca/xxgk/2023-10/07/content_1061111.shtml)。现有密码资产与密钥管理情况用于售前摸底，参照[NIST密钥管理项目](https://csrc.nist.gov/projects/key-management/cryptographic-key-management-systems)。

## 事实与确认规则

- 画像条目保存 value、basis(reported/observation)、evidence原话依据、source_record_id、updated_at；AI推测留在建议，不自动写成事实。
- 语音提取仅白名单字段；来源证据必须是原话子串。模糊数字、称呼和时间不补全；手机号码不靠推理重建。
- 建客户/改属性先形成 C编号待确认变更，展示客户、联系人、字段前后差异。收到精确“确认客户 C1”或网页确认才执行；“确认P1”仍是日程，互不混淆。
- 客户名/联系人匹配不按近似名称强行归档。重名、简称或多个客户时列出候选，可由用户明确选择客户或补充名称；网页选定的 customer_id 作为本次显式上下文，用于理解“他/这家客户”，全局默认不选，不取最近客户作为默认指代。同一客户多人交流先保留完整纪要，不要求一人一句；不能确定对象的联系人属性不硬写入。
- 变更草稿保存目标档案快照指纹；确认前档案有变化则拒绝旧草稿，要求重新整理，防止覆盖网页人工修改。重复确认与重复回调幂等。
- 网页直接编辑是显式提交，仍保留属性历史；删除/合并客户、批量导入、自动联系客户不在本轮。

## 使用体验

1. 首页“随口记/语音用法”常驻入口，明确企业微信语音为主要录音入口；网页提供同规则文字入口，不假称内网HTTP支持浏览器录音。
2. 客户主页先给“拜访前速览”：当前需求、未完成事项、近期交流、沟通偏好和待核实内容。
3. 概览/画像/联系人/交流记录分区或折叠展开；空属性给行业示例而不堆几十个空输入框。
4. “补充画像”先选字段和联系人，再填内容、依据类型，提交即保留来源；“一句话补充”生成与企微一致的确认草稿。
5. 待确认变更在独立区域显示，明确确认/取消；同名匹配不成功时留原话并给可执行补充示例。
6. 联系人喜好只记录用户提供的信息。例如“王总希望先微信发材料”→沟通渠道；“我感觉他更关心交付周期”→观察而非事实。
7. 客户页拜访速览下独立展示“推进建议”，首页汇总相关客户。目标/依据先展开，最多三个行动可逐项查看找谁、材料、话术与成功标志；明确标为AI建议，待核实问题与风险独立展示。
8. 新记录和追加跟进触发客户建议后台刷新；也可手动生成或重算，资料无需一次填齐。采纳只生成following事项，不自动安排时间。建议过期时提示基于旧资料，重算后再采纳。
9. 语音记录显示原始转写；编辑时可对照，并提交完整修正内容重新理解。歧义候选按钮明确客户归属，原文始终保留，相关旧pending C草稿失效。

## 实现合同

- customer_schema.py（数据代理所有）：ACCOUNT_FIELDS / CONTACT_FIELDS 映射key到中文label/group，统一验证与前端schema接口。
- customer_store.py（数据代理）：CustomerStore(CRMStore)，profile(customer_id)含fields/contacts/history，保存、取回、确认/取消draft，owner隔离与snapshot检查；不会建任务/outbox。
- customer_parser.py（解析代理）：CustomerVoiceParser.parse(text,now)->intent(create/update/brief/note/none/clarify), customer_name, contact_name, basic{}, contact{}, attributes[{key,value,evidence,basis,target(account|contact)}], note_text?, question?。不得模型确认草稿或指定数据库ID；字段来自共享schema。
- customer_service.py（主代理）：唯一/歧义匹配，客户变更提案渲染和精确确认语法、单聊/网页共用；原有时间解析与任务确认保留。
- web.py/gateway.py（主代理）：新API与同锁集成、原话持久化、会前速览。
- static三文件（界面代理）：现有风格下的画像分组、多联系人、速览、待确认草稿、语音用法。

新增API仍需现有登录/CSRF；所有owner服务器绑定：
- GET /api/customer-schema -> {account_fields,contact_fields}，各字段含label/group/examples。
- GET /api/customers/{id}/profile -> {customer,fields:[Fact],contacts:[Contact with fields],history:[Fact],brief:{open_records,recent_records,next_tasks,missing_fields},drafts:[]}。
- POST /api/customers/{id}/facts {key,value,basis,evidence?,contact_id?} -> {profile}（人工确认提交）。
- POST /api/customers/{id}/contacts {name,role?,phone?} -> {contact}; PATCH /api/customers/{id}/contacts/{contact_id} 同字段。
- POST /api/customer-command {text,customer_id?} -> {message,record_id,draft?,candidates?,customer_id?}；输入文字与企微语音转写走同样逻辑。customer_id仅来自用户显式选择，候选是可确认的客户对象。
- POST /api/records/{id}/reinterpret {text?:完整修正后文字,customer_id?:显式选定客户} -> 同上；保留original_content，更新修正内容后重新理解。记录不存在或不属于当前owner返回404，旧pending C草稿失效。
- GET /api/customer-drafts?status=pending -> {items}; GET /api/customer-drafts/{id}-> {draft}。
- POST /api/customer-drafts/{id}/confirm {} 或 /reject {} -> {draft,customer_id?,message}。
- Fact: id,key,label,group,value,basis,evidence,source_record_id,updated_at,contact_id(nullable)。
- Draft: id,intent,customer_id(nullable),customer_name,contact_name,status,changes:[{target,key,label,before,after,basis,evidence}],created_at,source_text。

## 当前批次及验收

先以语音客户资料为一个闭合批次：数据/解析/界面并行，主代理集成。验收语音新建→确认→追加联系人偏好→差异确认→网页画像与会前速览；同名歧义、否定指令、伪造证据、模型确认、陈旧草稿、重复确认都不应静默改错数据。原205测试持续通过；手机390px和键盘表单检查；备份后更新原服务。真实模型仅用合成客户资料测试，不向生产库插演示客户。

若字段/匹配合同不一致则repair；不把“已设计”混同“已上线”。多商机独立项目、语音删除/合并、CRM外部联系人同步、浏览器端ASR留后续。

## 实现与体验验收记录（2026-09-30）

CustomerStore、CustomerVoiceParser、CustomerService和网页API已实现；企微同一single-chat allowlist路由支持客户草稿、确认、拜访简报和交流整理。交流建议仅保存分析、不自动采纳或设提醒。原消息幂等，草稿创建后进程中断可按source_id恢复；模型等待前持久化交流保存回执，防止重放覆盖人工编辑。来源记录的客户归属已纳入分析指纹。

Product Design当前运行检查：改造前手机客户页只有单联系人、金额、备注与交流列表，缺乏拜访速览及画像操作入口。改造后在本地隔离demo以390×844与1280×900检查；手机实际走通新建客户C草稿、差异确认、入档查看，联系人偏好保存含原话/个人观察，页面和弹层无横向溢出。截图通过浏览器工具在会话中直接检查，工具未提供保存截图到本地的文档化接口，未另存PNG。真实DeepSeek合成输入验证新建确认、联系人与密码需求提取、主观观察更新，临时数据库不影响生产。

当前范围：同一客户的多人交流可以完整记录，不要求一人一句；涉及多家公司或归属歧义时仍需用户确认客户，并按客户分别整理。无法确定对象的联系人属性先留在纪要，不硬写画像。网页随口记是文字入口，实际语音通过企微转写，可明确选客户帮助理解指代。混合客户资料与日程可打开原始记录继续整理；确认C不确认P。客户自定义属性采用可追溯文本，金额仍为一笔当前商机，未实现完整多项目销售漏斗。

上一轮画像发布结果（本轮推进建议与纠正增强的验收另记）：本地完整364项测试通过（14.00秒），服务器Python3.12完整364项通过（5.35秒）。更新后后台登录、画像schema和待确认接口200，bot_connected=true；systemd active/running，NRestarts=0。备份路径 /opt/personal-secretary/deploy/rollback-20260930-200527。内网地址与登录口令保持原值。


## 本轮扩展：主动推进建议与语音纠正

### 目标与边界

从“记下来、提取待办”扩展到“结合客户历史与当前状态，建议下一步怎样推进”。建议不得自动成为客户事实、客户承诺或日程；数据安全和密码行业背景用于准备问题、方案验证与决策链摸底，不用于自动判断法律义务或合规结论。

每轮输出 summary、objective、rationale；next_moves最多三项，每项含title、reason、contact_hint、preparation、talk_track、success_signal。questions用于待核实信息，risks提示推进中的不确定性。不生成自动日期、成交评分或直接执行指令。

### 推进建议接口与状态

- GET /api/customers/{id}/coaching -> {recommendation:null或建议对象,generating:bool}。
- POST 同路径 {} -> 请求刷新当前客户的建议。
- 建议对象包含version、created_at、stale、summary、objective、rationale、next_moves、questions、risks；next_moves可含adopted_record_id。
- POST /api/customers/{id}/coaching/{version}/actions/{index}/adopt {} -> {record}，index从1开始；采纳仅生成该客户following记录，时间待补，不创建提醒。重复采纳同一建议动作幂等。
- GET /api/coaching -> {items:[{customer_id,customer_name,summary,objective,created_at,stale}],generating:bool}，供首页汇总。

新记录和追加跟进触发相关客户建议后台刷新；人工也可点击生成。客户页只局部刷新建议区域，编辑表单时暂停刷新，保留展开状态。资料变化后标记stale，旧建议不可直接采纳；历史内容与个人观察始终作为有时效、有不确定性的依据。

### 转写与理解分开处理

当前官方机器人接入收到的是voice.content转写文字，没有原始音频输入。本轮改善的是修正文字、明确客户上下文、歧义选择和重新理解，不代表更换了语音识别引擎，也未接入新ASR。

前端保留原始转写对照；重新理解使用完整修正文字和用户显式选择的客户。请求期间锁定相关表单，避免与保存修改并发；候选归属一次明确选一位客户。识别或模型失败仍保留记录入口，便于继续人工纠正；姓名、电话、金额需要用户核对。

后续如需重新识别原音及行业热词，可评估HTTPS录音入口、专业ASR与客户/产品/密码术语热词表。此项尚未接入，当前内网HTTP页面仅提供文字入口。

### 本轮验收关注点

验证新记录/追加跟进触发建议、建议目标与依据可读、展开行动准备与话术、采纳只建未排期following、重复采纳不重复建项、旧建议不可误采纳；同时验证原转写保留、纠正后重整、显式客户上下文、同名候选选择、多人纪要、多公司归属确认。最终测试、视觉验收与部署结果由主代理补充。


### 本轮交付验证（2026-09-30）

- Windows 完整回归 440 项通过（16.81 秒）；Linux 生产运行环境完整回归 440 项通过（6.74 秒）。
- 真实 DeepSeek 合成数据联调覆盖建档确认、观察字段、多人及业务否定交流、客户归属与销售推进建议；不写真实客户数据库。
- 隔离演示完成 390px 手机及 1280px 桌面验收：生成建议、展开行动细节、采纳跟进（未创建提醒）、首页汇总、语音纠正后重新理解、保留原始转写；浏览器控制台无 error/warn。截图由受控浏览器直接展示，其接口未提供保存文件路径。
- 部署前备份：`/opt/personal-secretary/deploy/rollback-20260930-203401`。既有 HTTP 内网入口和登录方式不变；正式后台鉴权与 coaching API 返回正常，企业微信机器人已连接。
- 专业 ASR 尚未接入，本轮不宣称改善原音频的识别准确率；已改善转写后的理解、纠错和归属确认。
