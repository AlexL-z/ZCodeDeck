# ZCodeDeck 项目说明(AGENTS.md)

> 版本:v0.5.3(2026-09-04 合盖不休眠开关;v0.5.2 状态色语义重排:执行蓝/完成绿;v0.5.1 圆钉/托盘官方 Z 字形;v0.5.0 发布包:首次 Key 引导+一键打包+公众号文章;v0.4.6 聚合排除已读完成;v0.4.5 日报分享脱敏;v0.4.4 日报布局与提炼重构;v0.4.3 日报文案精修;v0.4.2 日报传播感改造;v0.4.1 停止条两行防截断;v0.4.0 今日日报长图;v0.3.15 停止条中性文案+任务标题;v0.3.14 回填时间戳裁决+完结纠偏;v0.3.13 事实源回填;v0.3.12 活跃存活窗 12 小时;v0.3.11 聚合状态改优先级最值;v0.3.10 轮次延迟二读+周期补读;v0.3.9 首轮轮次修复;v0.3.8 活跃会话 30 分钟存活窗;v0.3.7 未读完成行点击已读;v0.3.6 高度稳定性:续跑紧凑条+平滑动画+sizeHint 陈旧缓存修复;v0.3.5 会话行五列对齐;v0.3.4 修复展开出屏与位置漂移;v0.3.3 空闲会话不占行+今日用量并入标题行;v0.3.2 会话行含标题与轮次,v0.3.1 脉冲提醒+未读完成态,v0.3.0 边缘状态点,v0.2.6 审批倒计时+留痕,v0.2.5 状态统一,v0.2.4 精简,v0.2.3 速率+配色+会话模块,v0.2.2 剩余高亮,v0.2.1 进度条+/resolve,v0.2.0 二期,v0.1.0 MVP)
> 需求以 `docs/ZCodeDeck_需求文档_v4.0.md` 为权威源(FR/NFR 编号、验收判据、接口契约、运维速查;v1.0 存档于 archive/,中间版本不留档,决策见变更记录)。
> 灵感来源:OpenAI × Work Louder 的 Codex Micro 迷你键盘。本项目做它的软件版:Windows 桌面悬浮窗,监控 ZCode 会话、远程审批、续跑、展示消耗与官方剩余额度。

## 架构

```
ZCode 会话 ──hook(七事件)──> deck/hook_bridge.py(ZCode 直接调起,四种模式)
                                    │ POST 127.0.0.1:17654
                                    v
zcode_deck.py(PySide6 主程序)= 悬浮窗 UI + deck/server.py(HTTP) + 轮询状态机
                                    │ 只读 sqlite            │ HTTPS(可选 API key)
                                    v                        v
~/.zcode/cli/db/db.sqlite      open.bigmodel.cn/api/monitor/usage/quota/limit
```

- `deck/hook_bridge.py`:纯标准库,任何异常静默 exit 0,绝不阻塞会话;四种模式 status / pretooluse / permission / stop
- `deck/server.py`:ThreadingHTTPServer;permission 与 stop-continue 连接挂起等 UI 决定;pretooluse 查白名单即时返回
- `deck/usage.py`:只读查询 turn_usage,三窗口统计(今日/5h/7d)
- `deck/quota.py`:官方剩余额度,API key 三来源(环境变量 ZCODEDECK_BIGMODEL_KEY / BIGMODEL_API_KEY → ~/.zcode/deck/config.json 的 bigmodel_api_key)
- `deck/autostart.py`:开机自启,HKCU Run 键
- `deck/install_hooks.py`:幂等 + 自动升级(按 args 比对替换旧条目),`--remove` 卸载
- 悬浮窗:无边框置顶 Tool 窗,可拖动,位置 QSettings 记忆;托盘常驻,三个开关:续跑键(默认关)、只读自动放行(默认关)、开机自启

## Hook 协议(从 zcode.cjs v0.16.5 源码实锤提取,2026-09-02)

**输入**(stdin JSON):`session_id`、`hook_event_name`、`permission_mode`、`transcript_path`、`agent_type`;工具事件另有 `tool_name`、`tool_input`、`tool_use_id`;PermissionRequest 另有 `permission_suggestions`。

**输出**(stdout JSON,zod 严格校验,多任何一个键即失败;建议只输出所需键):

- PermissionRequest 批准:`{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}`
- PermissionRequest 拒绝:同结构,`decision` 为 `{"behavior":"deny","message":"原因"}`
- 输出为空 + exit 0 = 不做决定,回退终端正常提示(deny 也可走 exit 2)
- PreToolUse 决定:`{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow|ask|deny","permissionDecisionReason":"..."}}`
- Stop 续跑:`{"decision":"block","reason":"给模型的继续指令"}`(顶层 decision,非 hookSpecificOutput;引擎最多接受三次续跑)
- `hookEventName` 必须与触发事件一致,否则报 "Hook returned wrong event name"

**配置**:`~/.zcode/cli/config.json` 顶层 `hooks` 键,`{enabled:true, events:{<Event>:[{matcher?,hooks:[{type:"process",command,args,timeoutMs,statusMessage}]}]}}`。配置式 hook 默认禁用,必须 `enabled:true`。matcher 是大小写敏感正则。七个合法事件名:SessionStart / UserPromptSubmit / PreToolUse / PermissionRequest / PostToolUse / PostToolUseFailure / Stop。

## 官方剩余额度(v0.2.0 起,v0.2.1 按实测修正)

- 端点:`https://open.bigmodel.cn/api/monitor/usage/quota/limit`,GET
- 鉴权:`Authorization: <api_key>` **裸 token 不带 Bearer**(官方 glm-plan-usage 插件同源同法;桌面版 OAuth token 与 zcodejwttoken 实测均被拒)
- key 配置:`~/.zcode/deck/config.json` 的 `bigmodel_api_key`(已配置,用户 2026-09-02 提供)
- **实测响应**(与官方插件文档的 TOKENS_LIMIT 结构不同):`{"code":200,"data":{"level":"pro","limits":[{"type":"CREDIT_LIMIT","unit":3|6,"usage":总量,"currentValue":已用,"remaining":剩余,"percentage":已用百分比,"nextResetTime":ms}]}}`;unit 3 = 5 小时窗,unit 6 = 每周窗;code 是 200 不是 0
- UI:两条着色进度条(绿 <60% / 琥珀 60-85% / 红 ≥85%),悬停显示已用/总量与重置时间;会话每轮结束自动刷新(限流 60s)
- `id.secret` 格式的 key 同时满足引擎 V4 签名要求,可作裸 CLI 的 provider apiKey 使用(见下)

## Hook 加载模型与权限链路(2026-09-02 实弹验证结论)

- **用户级 config hook 在引擎进程启动时加载一次**:桌面版 ZCode.exe 装hook前已运行 → 其所有会话(含新建)都不带 hook,**需完整重启一次桌面版**(托盘退出再开)。插件 hook 与工作区 hook 逐会话加载,不受此限。
- **裸 CLI(新进程)正常加载用户级 hook**:已实锤(SessionStart/UserPromptSubmit/Stop 三事件落入 /events)。
- **无头 `-p` 模式静默放行权限**(无交互通道,不产生 permission 事件)→ 无头下测不了审批卡。
- **项目级放行规则**("Approved for this project")存盘后对该项目所有新会话生效,命中的工具不再弹确认,悬浮窗也就无卡可弹——卡片只在"本来会弹确认框"时出现,这是正确语义。验收审批卡要用未被规则覆盖的操作或全新项目目录。
- **续跑闭环已实弹验证**:真实引擎会话 Stop → 卡片 → /resolve continue → 引擎服从继续生成(模型明确回应续跑指令)。

## 调试端点(v0.2.1)

- `GET /events?count=N`:最近 N 条已收 hook 事件(排查 hook 是否触发)
- `POST /resolve {kind, decision, pid?}`:远程决定,与卡片按钮同走 state.resolve;不带 pid 时作用于该类别唯一挂起请求

## 用量口径

`turn_usage.input_tokens` 已含缓存命中(cache_read 是其子集,今日实测 1.53 亿中 1.49 亿为缓存)。UI 的 "in" 为 API 口径原值;真实新增输入 = input − cache_read。

## 常用操作

- 启动:双击 `启动悬浮窗.bat`(自带守卫:已在运行则提示退出,不重复拉起;纯 ASCII,严禁写入中文——cmd 按 GBK 解码,UTF-8 中文会撕裂成乱码命令)
- 安装/卸载 hook:`py -3 deck/install_hooks.py` / `... --remove`(启动时若未注册会自动装)
- hook 生效范围:安装后**新开的** ZCode 会话;已运行的会话不加载
- 测试:`py -3 deck/usage.py` 看记账;echo '{}' | py -3 deck/hook_bridge.py status Stop 测桥接
- 开机自启:托盘菜单开关(注册表 HKCU Run;默认关)

## 已知边界(v0.2.6)

- **桌面版 hook 生效前置条件:完整重启一次 ZCode 桌面版**(用户级 config hook 随进程启动加载);2026-09-02 用户重启后已实弹验证,引擎日志 `Allowed by PermissionRequest hook` 为证
- 项目级"始终允许"规则会跳过审批卡(卡片只在本来会弹确认时出现,正确语义)
- **审批卡生命周期**:等待 300 秒(倒计时显示);超时静默回退 ZCode 自身确认并留 12 秒痕迹行;用户在会话内回复消息会取消挂起中的审批(引擎行为,卡片随之失效);AskUserQuestion 类提问工具不出卡,其交互在任务窗口提问框内
- **hook 配置超时预算须大于等待时长**:PermissionRequest timeoutMs=330000 > wait=300s;改等待时长时同步改 install_hooks SPECS(注意 is_installed 只比对 args,改 timeoutMs 需手动跑一次 install)
- 续跑键开启后,每次任务收尾会拖住会话至多 8 秒等待点击,默认关闭;引擎对 Stop 续跑最多三次
- 只读放行白名单:Read/Grep/Glob/LS/TodoRead/WebSearch,刻意不含 WebFetch 与一切写工具
- 端口固定 17654;跑 tests/test_roundtrip.py 前必须退出运行中的悬浮窗(测试自带守卫会提示)
- 悬浮窗进程带 `logs/deck_stderr.log` 运行,异常退出先查此日志
- 速率指示仅每周窗(unit 6);5 小时窗短周期噪音大,列为路线图可选项
