# 企业微信私人秘书 · 第一版

## 开发基线与离线验证

本仓库的公开开发基线包含现有客户、联系人、项目、事项、资料、AI输入与安排业务，以及待办/安排详情D1至D5改版。当前范围、验收结果和后续能力见[基线说明](docs/BASELINE.md)与[详情交付记录](DETAIL_DIALOG_DELIVERY.md)。历史部署记录不能替代当前验证。

开发需Python 3.11+和Node.js；本次使用Python 3.13、Node.js 22.18.0。前端保护测试直接调用`node`，不依赖npm包；缺少Node会跳过相关测试，请先运行`node --version`确认。不要将“仅Python测试通过”当成完整前端验收。

```powershell
git clone https://github.com/dikaerdun/secretary.git
Set-Location secretary
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-tested.txt
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
node --version
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .test-tmp -ra
.\.venv\Scripts\python.exe -m secretary --demo
.\.venv\Scripts\python.exe -B deploy/training.py --check
```

Linux把Python路径改为`.venv/bin/python`即可。测试仅使用合成临时数据；离线测试和演练不需要真实密钥。`deploy/training.py`与`deploy/使用指南与案例.html`是演练和测试必要文件，已纳入公开源码；不要仅安装wheel后就期待运行整个仓库的测试和演练。

真实运行配置按`.env.example`和[本机使用说明](deploy/Local使用.md)在各开发环境单独填写；本机`.env`、`.env.local`、登录口令、客户数据库、私有截图、备份与原始审查记录均不从Git获取。

**2026-10-03 输入与推进工作台更新**：首页可直接保存一句话，语音转写也走同一条先保存、后台理解的流程。系统结合客户简称、联系人、项目与近期交流建议归属，用户确认后再落档；失败原话仍保留。新增“总览”（我的工作／项目）及持久的“AI跟进讨论”，讨论建议可采纳为项目待办，具体日程仍需确认。聆记录音导入后同样自动提出客户候选；当前 MCP 按完整标题读取，尚不能发现所有新录音。

可在独立演练入口 `http://127.0.0.1:8766/` 使用公开演练口令 `learn-secretary-2026`。当前使用指引：`http://127.0.0.1:8766/guide#quick-capture`。演练使用固定示例，不调用真实 AI／ASR／聆记／企业微信；正式版沿用本机已有模型配置。

**2026-10-02 本机测试版**：现在可以在 Windows 本机持续使用客户工作台、客户交流、录音材料、跟进和日程。运行 `deploy/start-local.ps1`，访问 `http://127.0.0.1:8765`；登录口令保存在本机私有文件 `deploy/local-access.local.json`。详细步骤见 [Windows 本机使用](deploy/Local使用.md)。本机使用独立持久数据库、真实 DeepSeek 和聆记连接，不启动企业微信消息连接；提醒显示在后台。

一次客户交流可汇集现场录音、我的复盘和后续补充，并保留各自来源。聆记当前 MCP 需完整标题读取，自动发现新录音仍等待录音列表或事件接口开放。下面保留早期企业微信版本的运行说明；当前本机测试请优先使用上述入口。

你在企业微信里随口说一件事，秘书整理成待确认事项，发给你核对。**你补充时间并确认后**，它才进入日程并在到点时提醒。使用你已有的 **Linux 服务器和 DeepSeek API**。

链路：企业微信语音/文字 → 语音转写 → DeepSeek 整理事项与要求 → 保存提案并回复 → 你补充时间/确认 → 正式日程 → 企业微信到点提醒。

按你的最新选择，**没有明确时间时先只整理，时间由你补充**。不会自行占用空闲时间。已确认安排可随时按当天、本周、本月查看。

程序使用企业微信官方 Python SDK 的长连接模式。无需设置公网回调 URL，也不需要另购语音识别服务。服务器需要持续运行并能向外连接企业微信和 DeepSeek。

## 你可以这样说

| 输入 | 结果 |
|---|---|
| 明天下午三点提醒我给张总打电话 | 回复待确认提案 P 编号和具体时间，等你确认 |
| 记录一下研究新的供应商 | 保存为待补时间的提案，不启动提醒 |
| 待确认 | 查看待补时间、待确认事项；翻页说“待确认 2” |
| 把提案P1改到明天下午三点 | 补充/修改提案时间，再次发给你核对 |
| 把提案P1用时改为15分钟 | 修改预计用时，不擅自修改时间 |
| 确认 P1 / 确认提案一 | 正式加入日程并启用提醒，回复正式任务 # 编号 |
| 取消提案 P1 | 丢弃提案，不启用提醒 |
| 今天安排 | 看当天已确认安排、时间段和完成状态 |
| 本周安排 | 看周一至周日的安排，按日期分组 |
| 本月安排 | 看自然月安排、事项数量和预计总时长 |
| 待办 | 查看未完成事项、编号和提醒时间；每页 20 项 |
| 待办 2 | 查看第二页 |
| 完成 3 | 完成 3 号任务，不再发它的旧提醒 |
| 把任务3推迟到后天下午四点 | 更新提醒时间，使旧提醒失效 |
| 取消 3 | 取消任务及其待发送提醒 |
| 帮助 | 查看用法 |

日期和时间均按北京时间。新增先获得 **P 提案编号**，确认后获得 **# 正式任务编号**。两种编号不同；修改提案说“提案P1”，修改已确认任务说“任务1”。识别不对就在确认前修改，或取消提案重新说。

没指定用时先按 30 分钟估计，并在提案中注明。确认时会检查与已确认未完成事项的时间冲突；存在重叠时先改时间或预计用时。未确认提案间的重叠只提示，不会占用正式日程。提醒时间同时作为日程的开始时间。

日、周、月展示已确认事项，包括已完成的记录；已取消的事项不显示。未确认提案不进入日程。“待办”只列未完成正式任务。“本周安排 2”等命令可以翻页，每页 20 项。

第一版每次处理一个事项、一次性提醒。含糊时间会保留事项和要求、等待补时间；没有编号的“这件事完成了”、重复提醒，会要求补充信息或说明暂不支持。当前没有跨轮指代记忆，补充时间请带提案编号。它根据你的完成/推迟回复维护进度，不会自动知道现实中的事情是否已经办完。

## 当前验证状态

- **114 项离线测试通过**，完整模拟流程通过，覆盖提案、补时间、确认、日/周/月、任务保存、服务重启、去重、提醒回执、重试、完成/推迟、用户隔离和模型输出校验。
- 已使用官方 SDK 1.0.1 与本地 TLS WebSocket 服务验证认证、语音事件、回复及主动推送协议；发现并补上 SDK 静默断线后的恢复检查。该测试使用假账号，不代表企业平台已联调。
- `--demo` 使用模拟的企业微信和模型结果，加速时间，不调用真实账号。
- 本机测试版已验证真实 DeepSeek 与聆记 MCP 元数据读取。本次不进行远程部署或企业微信手机送达验证；手机语音和跨日送达需在服务器连接启用后单独验收。

## 先在本地看效果

需要 Python 3.11 或更新版本。

Windows PowerShell：

```powershell
cd D:\projects\secretary
.\.venv\Scripts\python.exe -m secretary --demo
```

Linux / 新环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m secretary --demo
.venv/bin/python -m pytest -q
```

演示模拟说话、重复回调、补时间、确认、日周月查看、重开数据库、到期主动提醒和完成任务。没有配置密钥也能运行。

## 企业微信准备

1. 在你有管理权限的企业中，创建仅供自己使用的**智能机器人**，选择 API 的**长连接**方式，以后台实际可用入口为准。
2. 从机器人配置取得 **Bot ID** 和 **Secret**。这里用的是智能机器人的配置，不是自建应用的 CorpID、AgentID 或群机器人 Webhook。
3. 把机器人使用范围设为你本人，并取得自己在企业通讯录中的成员账号 **userid**。这个值用于程序白名单，不是手机号、昵称或个人微信号。
4. 在企业微信客户端进入与机器人的单聊。语音输入使用这个会话。

如果管理后台没有 API 长连接选项，需要先确认该企业的功能开放和权限；不要把其他类型机器人的 Secret 填到本项目里。本项目只实现已核实的智能机器人长连接协议。

官方依据：[Python SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk)、[Node SDK 的语音类型和主动发送说明](https://github.com/WecomTeam/aibot-node-sdk)。

## 配置

复制 `.env.example` 为 `.env`，在服务器本地编辑这几个值：

```dotenv
WECOM_BOT_ID=你的机器人ID
WECOM_BOT_SECRET=你的机器人Secret
WECOM_ALLOWED_USER_IDS=你的成员userid
DEEPSEEK_API_KEY=你的DeepSeek密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
SECRETARY_DB_PATH=data/secretary.sqlite3
```

模型名称可修改。默认值依据 2026-09-30 查询到的 DeepSeek 官方接口文档；如果你的账号使用其他模型名称，以账号实际支持值为准。代码使用 JSON 输出和关闭思考模式的请求参数。[DeepSeek JSON 文档](https://api-docs.deepseek.com/guides/json_mode/)、[请求参数](https://api-docs.deepseek.com/api/create-chat-completion/)。

`.env` 不应上传到代码仓库。运行服务会把每次需要理解的事项文本发给你配置的 DeepSeek 服务；机器人语音由企业微信先转成文字。程序日志不打印事项正文、API 密钥或机器人 Secret。

配置检查只检查字段和依赖，不会调用账号：

```bash
.venv/bin/python -m secretary --check
```

前台连接试用：

```bash
.venv/bin/python -m secretary --run
```

启动后日志出现 `wecom_authenticated` 表示企业微信认证完成。“已整理，待你确认：P编号……”表示提案已保存；“已确认，提醒已启用：#编号……”表示正式日程生效。“正在整理……”只是处理进度提示。

## Linux 常驻部署

以下 `/opt/secretary` 与 `secretary.service` 是新服务器通用示例；[历史部署说明](deploy/DEPLOYMENT.md)已去除私有主机信息，仅供理解既有升级路径，不能作为当前服务状态。

以下以服务器已安装 Python 3.11+、支持 systemd 为前提。将本目录的源码放在 `/opt/secretary`，不要上传 Windows `.venv`，也不需要上传 `.tmp-tests` 和缓存。打包文件已排除这些内容。

在服务器运行一次依赖安装：

```bash
cd /opt/secretary
python3 -m venv .venv
.venv/bin/python -m pip install .
cp -n .env.example .env
```

在服务器本地填好 `.env` 后，配置专用运行用户和数据目录（如已有同名用户可跳过创建）：

```bash
id secretary || sudo useradd --system --user-group --home-dir /opt/secretary --shell /usr/sbin/nologin secretary
sudo install -d -o secretary -g secretary -m 700 /opt/secretary/data
sudo chown root:secretary /opt/secretary/.env
sudo chmod 640 /opt/secretary/.env
sudo -u secretary /opt/secretary/.venv/bin/python -m secretary --check --env /opt/secretary/.env
sudo install -m 644 deploy/secretary.service /etc/systemd/system/secretary.service
sudo systemctl daemon-reload
sudo systemctl enable --now secretary.service
sudo systemctl status secretary.service
```

用下面的命令看连接和发送状态：

```bash
sudo journalctl -u secretary.service -n 100 --no-pager
```

更新配置后重启：

```bash
sudo systemctl restart secretary.service
```

`secretary.service` 会在异常退出后自动重启。程序对数据库加进程锁，防止同一台机器启动两个发送实例；不要在两台服务器分别运行同一个机器人。

## 手机端验收

1. 发语音“研究一下新的供应商”，应收到待确认 P 编号、待补时间，不应自动安排。
2. 发语音“把提案P编号安排到2分钟后”，核对时间，再说“确认提案编号”。应收到正式任务 # 编号，并到点在手机收到提醒。
3. 分别发“今天安排”“本周安排”“本月安排”，检查该事项在对应日期与时间下；未确认的提案不应出现。
4. 另建一个未确认、近期到期的提案，确认不会收到该提案的到点提醒，然后取消提案。
5. 给已确认任务发“完成任务编号”，确认停止待发提醒，并且日程中标“已完成”；再验证一次推迟。
6. 确认一个 5 分钟后的任务，重启服务，确认任务仍在并能收到提醒。
7. 确认一个次日提醒，完成跨日验收；同时确认手机端企业微信通知已开启。上述“编号”均替换成回复里的实际数字。

“发送成功”指企业微信接口确认接收，不等于用户已经读到或手机一定响铃。

## 保存与故障恢复

- 任务、提醒状态和回调处理结果保存在 `data/secretary.sqlite3`。请保留整个数据目录。
- 如需拷贝数据库备份，先停止服务，复制数据目录，完成后再启动；直接拷贝运行中的 SQLite 单文件可能遗漏 WAL 内容。
- 断线后自动重连，服务恢复后会补发仍未发送的到期提醒；服务停止期间不能实时提醒。
- 发送失败从 30 秒开始退避重试，最多间隔 1 小时。若权限或密钥有误，需修复配置。
- 消息回调按用户和消息 ID 去重。同样的话如果你主动另发一次，会被视为新提案；重复确认同一提案不会重复建任务。
- 到期提醒只发一次；你没回复完成的事项仍留在待办。第一版没有每日催办和重复提醒。
- 如果企业微信已收到消息、但回执或数据库确认恰好丢失，恢复重试可能重复提醒一次；无法承诺网络条件下绝对不重复。
- 第一版收到语音后到提案保存前若发生故障，需重新发送；不要把处理中提示当作已保存回执。

## 下一版

客户跟进后台已加入：客户资料、销售阶段、商机金额、原话待整理箱、沟通时间线、AI 纪要与 TODO、日周月日程。内网部署、口令和使用流程见 [客户后台指南](deploy/CRM_GUIDE.md)。AI 整理由“帮我整理”触发，待办逐项采纳，提醒仍需明确确认。

等语音、确认和提醒真实跑通后，再加入每日待办汇总、重复提醒、项目拆解、根据截止日期建议排期、日历空闲时间及超期跟进。自动选择时间需按你之后的偏好启用；当前保持时间由你补充。
