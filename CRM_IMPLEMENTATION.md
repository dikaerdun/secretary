> 公开基线说明：以下历史开发记录不代表当前部署状态；具体主机地址与运行账号已改为示例。本机私有配置、数据库和现场脚本不随Git发布。

# 客户跟进后台任务卡

- 用户要求：企业微信随时记录客户事项，回来用网页整理；网页可新增、查看和跟进；包含联系人、电话、商机金额及销售阶段，手机也能使用。
- Protocol mode: manual_fallback。采用 Planning / Parallel Work / Test-First / Completion Verification；用户已授权制作，先实现可运行版本。沿用 codex-warhorse-ops 工作流。
- 初始问题：Store 只有提案与提醒，缺少客户档案、原话收件箱、沟通时间线和网页。后续批次已补齐；当前交付行为以下方“R1–R16 整体修复”及使用指南为准。
- 可读写：secretary 项目与用户授权的 /opt/personal-secretary；凭据只由配置代码读取，不输出。无关目录和业务不动。

## 范围与设计

- must_fix_now：客户基础资料、每客户当前商机金额（人民币）和阶段、原始记录、归档整理、追加跟进、关键字筛选、日周月安排、登录、移动布局。
- 企业微信收到白名单单聊后先保存原文（语音保存企微转写文本），模型失败也能回来整理；命令消息不进入事项列表；重复回调只保存一次。旧提案以原提案标题补入记录，不声称能恢复历史语音原话。
- 新客户不凭模型猜测合并；网页明确归属客户。记录状态 unfiled / following / done，与正式任务状态分别展示。
- CRMStore 在相同 SQLite 中新增 crm_* 表；原表不破坏。网页和机器人同进程共享 asyncio 锁，安排/确认/完成与派发串行，避免取消同时发送竞态。
- 网页设置时间后只产生待确认提案，单独“确认并启用提醒”才创建正式日程。未填时间的记录长期保存。
- 个人后台固定绑定唯一允许成员；口令哈希、HttpOnly/SameSite 会话、CSRF、请求大小与登录限速；不接受浏览器指定owner。外网 HTTPS/VPN 入口等待用户说明，不自行公开内网端口。
- known_but_deferred：每客户多商机、团队权限、原始录音长期归档、外部日历、自动商机预测、循环提醒。短音频转写已有可选接入，实际启用条件见本次交付限制。
- out_of_scope：自动联系客户、批量导出到第三方、购买域名/服务、未经配置发布公网。

## 实现合同

- secretary/crm.py：CRMStore(path)，客户/记录 CRUD、跟进时间线、概览、只读日程，owner 隔离和参数校验。
- secretary/static/{index.html,app.css,app.js}：移动优先真实交互；列表/详情/新增/编辑/日程，不植入真实库的演示资料。
- secretary/web.py：aiohttp 应用、登录与会话、JSON API、静态白名单、提案动作复用 Store.execute；与gateway共享锁。
- secretary/gateway.py、__main__.py：收件保存、网页生命周期和配置。
- deploy：本地预览与服务器部署；迁移前 SQLite backup API 备份；静态资源纳入软件包。

## 验收判据

- 两个owner互不可读写客户/记录；原文持久且重复语音只一条；模型失败时原文仍在。
- 新增客户、金额精确到分、阶段更新、记录关联、追加沟通和完成能刷新后保留；空名称/负金额/错编号拒绝。
- 网页安排未确认不进outbox；明确确认一次建任务；已有提案可在后台确认和查看；日周月使用上海时区。
- 未登录无法读写API，跨站请求失败，登录限速，静态路径不可读.env，用户文本安全渲染。
- 原114测试保持通过；新增API/数据/真实浏览器桌面与窄屏验证；部署后只用读取验证真实数据，不写演示客户。
- 外网访问与主动推送846607分开记录，不能将网页上线等同提醒或公网验收通过。

## 当前批次

1. 数据与API合同：独立CRM数据层和前端并行，主代理做HTTP与网关集成；边界由下列API固定。
2. 合同通过后联调真实浏览器和安全测试。
3. 备份后上传、重启专用服务、验证网络入口。外网入口未提供时先完成内网版本及部署说明。

评审：判据通过则continue；失败修复当前批；范围变化才reshape。记录结论作为本项目工作记忆，不连接Obsidian。

## JSON API（写操作默认 JSON；登录后带 X-CSRF-Token）

- GET /api/session -> {authenticated, csrf?, demo?}; POST /api/login {password} -> {authenticated:true,csrf}; POST /api/logout。
- GET /api/dashboard -> {stats:{customers,unfiled,following,overdue,today,needs_time,pending_schedule,pipeline_cents,unknown_amounts},queues,recent:Record[],upcoming:Task[],bot_connected}；queues 包含 overdue/today/needs_time/pending_schedule，各有 items、total。
- GET /api/customers?q=&stage= -> {items:Customer[]}; POST /api/customers {name,contact?,phone?,stage?,amount_cents?,notes?,aliases?,contact_cycle_days?} -> {customer}。
- GET /api/customers/{id} -> {customer,records:Record[]}; PATCH /api/customers/{id} 同新增字段 -> {customer}。
- GET /api/records?q=&status=&customer_id=&kind=note|action&queue=needs_time|pending_schedule -> {items:Record[]}; POST /api/records {title,content,customer_id?,status?,kind?,parent_record_id?} -> {record}。
- GET /api/records/{id} -> {record,activities:Activity[],proposal?,task?,analysis?,active_reminders}; PATCH /api/records/{id} {title?,content?,customer_id?,status?,kind?,mode?} -> {record,...}；customer_id:null 明确清除归属，mode 为 note_only 或 sync_reminder。
- POST /api/records/{id}/activities {content} -> {activity}。
- POST /api/records/{id}/schedule {remind_at:Unix秒,duration_minutes} -> {record,proposal,message,...}；已有活动提醒时生成指向原任务的变更提案，确认前原任务继续有效。
- POST /api/records/{id}/confirm {} -> {record,proposal,task?,message}；冲突/无时间HTTP409。
- POST /api/tasks/{id}/complete {} -> {message}，有共享锁。
- GET /api/agenda?period=day|week|month&date=YYYY-MM-DD -> {items:Task[],start,end}。
- Customer: id,name,contact,phone,stage,amount_cents,amount_known,notes,aliases,contact_cycle_days,primary_contact_id,primary_contact_name,primary_contact_phone,created_at,updated_at,record_count?；amount_cents 为整数分或 null，null 表示尚不明确，显式 0 表示已知为零。
- Record: id,title,content,original_content,source(voice|text|web|legacy),status(unfiled|following|done),kind(note|action),parent_record_id,customer_id,customer_name,proposal_id,created_at,updated_at,remind_at?,proposal_remind_at?,task_status?,task_id?,superseded_by_analysis_version?。
- 日程与 upcoming 的 Task 增加 record_id、customer_id、customer_name、contact_hint。待确认变更的 proposal_remind_at 是拟定时间，remind_at 仍显示原活动任务的时间。
- Activity: id,record_id,content,created_at；Task/Proposal字段沿用Store，API不返回owner或source_id。
- 阶段值 lead/contact/qualified/proposal/negotiation/won/lost 对应新线索/已接触/需求确认/方案报价/商务沟通/已成交/暂缓跟进。
- 时间均Unix秒，前端显式按Asia/Shanghai；列表有上限与分页，最多200条一页，返回total,page,pages（前端应支持翻页）。

## 本次追加：客户交流记忆与 TODO

用户进一步明确：现场的好想法和约定不能遗忘；下次拜访能继续落实上次事项。实现 InteractionOrganizer：本次纪要/要点/待核实问题，区分明确待办与AI建议，最多六条。同客户最近五条记录用于背景，不能当成本次新承诺和日期依据。

后台“帮我整理”触发真实模型；客户语音/文字捕获也能统一返回资料草稿、纪要及行动。明确承诺且有未来具体时间的行动直接生成待确认提案；无时间承诺和 AI 建议先核对、采纳为待办，时间由用户补充。任何提案仍需单独确认才能提醒。原记录变化会标记分析过期；只存在未确认提案的旧子事项可以撤回并重新整理，旧记录及分析来源保留。已有活动提醒或手动采纳且无提案的待办受保护。当前负责人提示作为文字保留，未增加多人分派。

## 本次交付：R1–R16 整体修复（2026-09-30）

下表记录本批代码与交互已实现的行为；本批最终测试数量、浏览器验收及部署证据由主交付记录补充，不沿用下方历史批次的测试数量。

| 编号 | 完成点 | 用户可见行为 / 数据边界 |
| --- | --- | --- |
| R1 | 完成记录与提案一致 | 完成待办时撤回关联的未确认 P；已完成记录不能通过旧 P 再启用提醒。 |
| R2 | 改期校验 | 检查过去时间、截止时间及日程冲突；修改同一任务时排除其自身，确认前再校验。 |
| R3 | 编辑活动提醒 | 展示原提醒标题、时间；当前待办的修改生成 target_task_id/expected_task_revision 变更提案，确认前旧提醒有效。源交流有多条活动子提醒时逐条打开对应待办修改确认，不能把纪要标题覆盖所有子提醒。 |
| R4 | 未提交表单保护 | 当前浏览器会话保存草稿，刷新后恢复；关闭和退出提示未提交内容；不把密码、音频文件当作表单草稿保存。 |
| R5 | 完整保存再理解 | 重新理解使用同次提交的标题、正文、类型、状态及客户归属；customer_id:null 明确清除归属，不继续暗用旧客户。 |
| R6 | 最终结果幂等缓存 | 使用完成后的统一结果更新缓存；回调重放复用最终结果，避免重复模型调用、重复档案草稿或重复待办提案。 |
| R7 | 混合输入统一结果 | 一次描述可同时产生 C 资料草稿、交流纪要、明确承诺与建议；有未来具体时间的承诺形成待确认 P，不自动启用。C 确认后同步来源及未分配客户的关联子事项。 |
| R8 | 资料草稿按字段判冲突 | 不相关客户字段变化不妨碍确认；目标字段或来源交流变更使草稿过期，未实际变更的字段不会覆盖人工新值。旧版本草稿保留保守的整体版本检查。 |
| R9 | 首页行动队列 | 可切换逾期、今天、待确认日程、待补时间并打开记录；统一待确认入口汇集 C、P 和待采纳行动。 |
| R10 | 日程连接客户跟进 | 日/周/月事项展示客户、联系人提示、来源记录；提供改期、取消、下一次拜访入口，下一次仍经提案确认。 |
| R11 | 纪要与待办分开 | kind=note/action 区分交流信息与执行事项；“保存并整理本次沟通”创建关联 parent_record_id 的独立交流，原沟通与历史待办保留。 |
| R12 | 金额未知与零分开 | 新客户仅填写名称时 amount_cents=null；显式零和正金额为已知；旧库金额保守迁移为已知，统计单列未知金额商机。 |
| R13 | 客户及联系人维护 | 支持别名、建议联系周期、主联系人及联系人归档；同名校验及 owner 隔离，归档保留画像历史；拨号/复制电话和一分钟拜访简报。联系周期是资料字段，不自动创建循环提醒。 |
| R14 | 手机可读可点 | 手机正文及操作区优化为至少 14 px 文字、44 px 触控目标；窄屏布局保留客户、记录、日程和确认操作。 |
| R15 | 推进建议形成闭环 | 优先展示下一步行动与依据来源入口；已完成、受阻、暂缓、不适用的执行反馈进入下一轮建议，采纳本身不启用提醒。 |
| R16 | 可选专业语音转写 | 录音/上传、能力状态、行业热词以及百炼 Qwen ASR 适配器已实现；缺少 API key 时明确显示未启用，不生成虚构转写。 |

### 增量接口与持久化合同

- `GET /api/review-inbox` 统一返回客户草稿、待确认日程和行动；`POST /api/proposals/{id}/confirm|reject` 必须提交 `{updated_at: 页面所展示的提案更新时间}`，按明确 P 编号和展示版本决定，提案变化时要求刷新核对。
- `POST /api/records/{id}/reinterpret` 保存完整表单后重新理解；`POST /api/records/{id}/activities/organize` 保存独立后续交流并整理。
- `POST /api/records/{id}/cancel` 取消当前活动提醒或待确认提案；`POST /api/records/{id}/next-visit` 新建关联来源的下一次拜访待办，时间可暂缺。
- 客户更新支持 aliases、contact_cycle_days、primary_contact_id；联系人更新支持 archived，画像事实历史不删除。
- 分析 action id 按版本分配，旧页面的编号不会采纳新版另一项内容；替代的子事项标记 superseded_by_analysis_version，analysis.previous_actions 提供历史入口。
- `POST /api/customers/{id}/coach-feedback` 接受 version、index、status（completed/blocked/paused/not_applicable）和可选 note；反馈影响后续建议。
- `GET /api/audio/capabilities` 返回当前是否配置；`POST /api/audio/transcribe` 是唯一 multipart 写入接口，单个 `file`，仍需登录及 CSRF；`GET/PATCH /api/voice-settings` 管理当前成员热词。
- 迁移增量执行并保留旧数据；旧联系人转成真实联系人，旧金额保守标记已知，旧分析采纳关系升级为版本化编号。部署前仍执行 SQLite backup API 备份。

### 本批启用条件与限制

- 当前没有专业 ASR 的 API key，专业转写尚未启用，真实服务转写准确率和费用没有完成实测。企业微信原有转写、手机键盘语音输入和文本整理可继续使用。
- 接入后网页录音需要 HTTPS 和麦克风授权；当前内网 HTTP 地址不能使用浏览器麦克风。配置 ASR 后可先上传手机录音文件。浏览器 WebM 转换需要服务器 ffmpeg；缺少时改用 WAV 或 MP3。
- 音频入口限制为每次 6 MB、5 分钟；转写后先核对文字，再“保存并整理”。本版不长期保存原始音频，不能回放或重新识别企业微信未提供的原音。
- 行业热词及客户/有效联系人自动词表只用于本后台专业 ASR，不能改变企业微信自带识别；不保证热词一定识别正确。
- 原始记录和人工修改仍保留；信息不足时先列待核实问题，不推断法定合规义务，不给商机概率，不自动联系客户。
- 外部映射和 HTTPS 入口仍按用户安排配置；本批代码完成不代表公网入口或专业语音服务已经启用。

专业语音配置由管理员在服务器 `/opt/personal-secretary/.env` 中填写，key 不需要发到聊天。配置变量为：

```dotenv
DASHSCOPE_API_KEY=
SECRETARY_ASR_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SECRETARY_ASR_MODEL=qwen3-asr-flash
```

`DASHSCOPE_API_KEY` 留空即禁用专业转写。填写可用 key 后重启 `personal-secretary.service`，再在后台“语音”检查能力状态并进行一次短录音验证；不要输出配置内容到日志。

## Completion Verification / 历史批次已部署证据

### 2026-09-30 本次 R1–R16 验收与部署

- Windows 完整回归：485 passed in 20.80s；Linux Python 3.12 完整回归：485 passed in 10.71s。
- 已更新原服务，状态 active/running，NRestarts=0；认证后台验证 bot_connected=true。
- 后台队列、统一待确认、日/周/月、语音能力与热词接口均通过；部署的 JS/CSS 与本地包哈希一致。
- 隔离浏览器验收：新增待办、客户归属、P1确认、P2改期确认前保留旧提醒、周/月日程、未保存草稿恢复、新客户未知金额。390×844 手机检查无横向溢出，客户操作按钮均达到44px高度。
- 真实 DeepSeek 临时库联调通过客户新建/确认、观察资料待确认、多联系人否定语义、混合客户资料＋待办＋精确时间待确认P、完整结果重放、销售建议。最初一次严格断言失败未定位阶段；加入阶段诊断后重跑全部通过。模型输出存在变化，验证器仍会将不明确的承诺/时间留待用户核对，未用重试自动确认提醒。
- 生产库没有加入验收客户或验收提醒。与备份比较，原 tasks 的标题、时间、状态、revision 和约束全部保留，PRAGMA foreign_key_check无异常。
- 回滚快照：`/opt/personal-secretary/deploy/rollback-20260930-212227`，包含升级前源码、配置和数据库。
- 专业 ASR 因没有 API key 未启用，适配协议由 MockTransport 验证，没有声称真实识别准确率。企业微信转写仍由企业微信处理。

以下为此前历史批次记录：

- 已部署 `/opt/personal-secretary`，后台 `http://<your-server>:8765`；口令哈希写入600权限配置，初始登录信息保存在本地忽略文件 `deploy/web-access.local.json`。
- 备份：`/opt/personal-secretary/deploy/rollback-20260930-192648`，包含旧源码、配置、SQLite backup API快照。原有一条确认任务与提醒记录保留，导入为一条历史记录；生产库未加入演示客户。
- Linux Python3.12：205 tests passed in4.04s；旧114项与新增CRM/HTTP/AI整理/收件持久化测试均通过。
- 真实DeepSeek合成拜访记录返回两项后续动作（其中一项有明确承诺依据），没有擅自填时间。
- 真实内网HTTP登录和dashboard通过，bot_connected=true；服务active/running、enabled、NRestarts=0。
- Chrome隔离演示验证：登录、新增交流关联客户、AI整理、采纳TODO、设置时间、明确确认；390×844手机布局无横向溢出，新增客户金额12345.67和阶段正确，控制台无错误。演示明确标记、临时库、未发真实消息。
- 手机日/周/月切换验证：同一已确认事项在10月1日、跨月自然周、10月自然月内正确显示；临时viewport已恢复。
- 独立复核采纳：并发登录先占限速额度；待确认提案标题随人工编辑同步；历史提案关系保留避免二次导入；提交后分类失败从已缓存回执恢复，原话不丢失。
- 提醒链路新证据：真实服务19:06:09记录reminder_sent，notifications.sent=1。首次846607后实际到点提醒已获成功回执，不推断原错误码原因；手机是否弹出未由用户反馈。
- 外部访问按用户最新要求留给其后续映射；指南提供HTTPS反代和Cookie配置。未修改路由器或购买服务。
- 该历史批次只做用户点击触发的 AI 整理。后续已扩展为客户输入自动整理和推进建议刷新，当前以本次 R1–R16 交付说明为准；仍不自动重整全部历史记录。
