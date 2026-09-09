---
name: zcodedeck
id: zcodedeck
version: PRD-v1.0
date: 2026-09-02
status: v0.2.3 已上线(核心能力全部实弹验证)
repo: D:\ZCode\ZCodeDeck
entry: 启动悬浮窗.bat 或 py -3 zcode_deck.py
port: 17654
test: py -3 tests/test_roundtrip.py
---

# ZCodeDeck 需求文档

## 1. 一句话定义

Windows 桌面悬浮小窗:监控 ZCode 会话状态、远程批准或拒绝操作、续跑任务、展示套餐额度与消耗速率。软件版的 Codex Macro Pad。

解决三个问题:ZCode 在后台干活时状态不可见;权限确认必须在 ZCode 窗口内操作;coding plan 消耗没有常驻视图。

## 2. 用户与场景

单用户本机使用(作者本人),Python 3.14 + PySide6,单实例常驻托盘。

| 场景 | 触发 | 预期 |
|---|---|---|
| S1 后台监督 | ZCode 会话运行中,用户在做别的事 | 悬浮窗状态色 + 会话行实时反映思考/执行/出错,无需切窗口 |
| S2 远程审批 | 会话弹出权限确认 | 悬浮窗出卡片显示工具与命令摘要,点批准或拒绝,会话继续,全程不切窗口 |
| S3 额度感知 | 任何时候瞟一眼 | 5 小时窗与每周窗剩余量进度条;每周窗带速率刻度线与超前/落后判定 |

## 3. 功能需求

状态图例:✅ 已实弹验证(真实引擎/真实点击)| 🔧 已实现(离屏断言+回归)| 📋 规划

| ID | 需求 | 状态 | 验收判据(可执行) |
|---|---|---|---|
| FR-01 | 状态监控:思考/执行/出错/空闲,窗框与托盘图标着色 | ✅ | `curl :17654/events` 可见引擎亲发事件 |
| FR-02 | 远程审批:批准/拒绝/接受计划,显示工具名与命令预览 | ✅ | 引擎日志出现 `Allowed by PermissionRequest hook` |
| FR-03 | 审批超时回退:165 秒无操作静默退出,ZCode 回退自身 UI 确认 | ✅ | 回路测试场景三;用户实测未卡会话 |
| FR-04 | 续跑键:任务停止后 8 秒窗口内一键继续(托盘开关,默认关) | ✅ | CLI 实弹:模型续答明确回应续跑指令 |
| FR-05 | 只读自动放行:Read/Grep/Glob/LS/TodoRead/WebSearch 免确认(托盘开关,默认关) | 🔧 | 回路测试场景六/七 |
| FR-06 | 套餐额度:5 小时窗(蓝)与每周窗(绿)剩余进度条,悬停显总量/已用/重置时间 | 🔧 | 离屏断言两行进度条与配色 |
| FR-07 | 消耗速率:每周窗按已过时间算应用用量,刻度线 + 超前/落后/正常文案 | 🔧 | 离屏断言 marker 与 verdict |
| FR-08 | 今日用量:tokens 总量 + 对话次数 + 占近 7 天比例条(蓝) | 🔧 | 离屏断言文案与条值 |
| FR-09 | 会话模块:每个活跃会话一行(色点/状态/相对时间),最多 4 行 | 🔧 | 离屏断言会话行渲染 |
| FR-10 | 窗口形态:宽度固定,卡片出现增高、移除后收回紧凑高度 | 🔧 | 离屏断言 163→275→163 |
| FR-11 | 位置记忆 + 开机自启(HKCU Run 键,托盘开关) | 🔧 | 重启后位置保持;注册表键存在 |
| FR-12 | 调试端点:/events 最近事件、/resolve 远程决定 | 🔧 | `curl :17654/events?count=5` |
| FR-13 | 5 小时窗速率指示(同 FR-07 逻辑) | 📋 | — |
| FR-14 | 审批规则自定义(按工具/命令模式配置白名单) | 📋 | — |
| FR-15 | 会话行点击跳转对应 ZCode 会话 | 📋 | — |

## 4. 非功能需求

| ID | 需求 | 验收判据 |
|---|---|---|
| NFR-01 | 绝不阻塞会话:桥接脚本任何异常静默退出 0;服务不在时 3 秒超时让行 | 回路测试;手工 `echo '{}' \| py -3 deck/hook_bridge.py status Stop` 退出码 0 |
| NFR-02 | 本地优先:用量数据只读本地 sqlite;API key 只存 `~/.zcode/deck/config.json`,不入库不外发(官方配额端点除外) | 文件检查 |
| NFR-03 | 降级:无托盘/无屏环境照常启动;额度接口失败显示错误不崩 | 离屏(QT_QPA_PLATFORM=offscreen)全量断言通过 |
| NFR-04 | 性能:status/pretooluse hook ≤3 秒返回;UI 轮询 200ms | 引擎日志 hook 时长 |
| NFR-05 | 可回归:`py -3 tests/test_roundtrip.py` 七场景退出码 0(跑前须退出悬浮窗) | 命令执行 |

## 5. 接口契约

Hook 输入(stdin JSON):`session_id` `hook_event_name` `tool_name` `tool_input` `permission_mode` `transcript_path`。

Hook 输出(stdout JSON,zod 严格校验,多键即败):

```
PermissionRequest 批准  {"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}
PermissionRequest 拒绝  同上,decision={"behavior":"deny","message":"原因"}
PreToolUse 放行         {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"..."}}
Stop 续跑               {"decision":"block","reason":"继续指令"}(顶层键,最多三次)
无输出 + exit 0         不做决定,回退 ZCode 默认行为
```

HTTP(127.0.0.1:17654):`GET /health` `GET /events?count=N` `POST /event` `POST /permission` `POST /stop-continue` `POST /pretooluse` `POST /resolve{kind,decision,pid?}`。

## 6. 运维速查

| 操作 | 命令 |
|---|---|
| 启动 | 双击 `启动悬浮窗.bat` |
| 退出 | 托盘右键 → 退出 |
| 卸载 hook | `py -3 deck/install_hooks.py --remove` |
| 回归测试 | `py -3 tests/test_roundtrip.py`(先退悬浮窗) |
| 错误日志 | `deck_stderr.log`(项目根) |
| 升级后确认 | `curl :17654/health` 看 version 字段 |

已知运维事实:用户级 hook 随引擎进程启动加载,ZCode 桌面版升级 hook 后需完整重启一次;项目级"始终允许"规则会跳过审批卡(正确语义);跑测试前须退出悬浮窗(SO_REUSEADDR 流量劫持)。

## 7. 边界与不做

不做手机端与多用户;不改 ZCode 本体(只用官方 hook/数据库/接口);不推算每日额度上限(官方无此数据,不造基准线);悬浮窗不做输入式交互(保持一瞥即得)。

## 8. 路线图候选

v0.3:审批规则自定义(FR-14)、会话点击跳转(FR-15)、5 小时窗速率(FR-13)、历史用量留存与日视图。

## 9. 风险与依赖

| 风险 | 影响 | 对冲 |
|---|---|---|
| ZCode 版本升级改动 hook 协议 | 审批/续跑失效 | 协议实锤记录在 AGENTS.md,升级后跑回路测试即可定位 |
| 官方配额接口变更或 key 失效 | 额度面板不可用 | 优雅降级为错误文案,其余功能不受影响;key 可随时更换 |
| 端口 17654 被占 | 双实例流量错乱 | /health 看 version 确认;测试自带守卫 |

## 10. 变更记录

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0 | 2026-09-02 | 初版;对应软件 v0.2.3,核心能力(FR-01~04)全部实弹验证 |
