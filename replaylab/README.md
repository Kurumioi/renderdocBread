# ReplayLab AgentHost（RenderDocUltra AI 接入层）

ReplayLab AgentHost 是 qrenderdoc 内嵌 AI 的后台源码，不是第二个需要用户切换的桌面软件。最终产品仍只有一个可见窗口：qrenderdoc。连接器、Agent、Skills、MCP Host 和厂商 CLI 都应按需作为隐藏 sidecar 启停。

当前已经同时完成可运行、可测试的后台接入层和 qrenderdoc 原生 `RenderDoc AI` 面板。`Settings → AI` 提供连接列表、API/订阅两类向导、Windows 凭据管理器长期 API key、模型发现/多选/默认项和订阅登录；聊天区可快速切换连接与模型，`Enter` 发送、`Shift+Enter` 换行。面板还支持普通对话、四个一键 Skill、精确实例 worker、TaskEvent 增量呈现和取消当前自由对话。现有 AI 菜单按钮排队 intent 后不再弹出“去外部 companion”提示，而是直接打开面板并关联结果。详细设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 模型、Agent、Skill、MCP 与 ACP

- 裸模型 API 不是完整 Agent。使用 OpenAI、Claude、Gemini 或国内模型 API 时，ReplayLab 自己的 `RenderDoc Agent` 负责问题总结、任务拆分、工具循环、状态、停止条件和验收。
- Codex app-server、Copilot/Kimi/Qwen/Gemini ACP 等入口已经是厂商 Agent。它们保留自己的内层模型/工具循环；ReplayLab 统一外层的零工具任务规划、逐 Skill 调度、证据验收、权限和 UI，不再套一个会与厂商 Agent 争抢同一工具回合的第二内层循环。
- Skill 是按需加载的领域流程：说明该怎么查、能调用哪些工具、至少取得哪些证据以及如何验收。它不常驻，也不独立推理。
- MCP 是 Agent 与 RenderDoc 工具之间的协议边界；ACP 是 UI/宿主与完整厂商 Agent 之间的协议边界。ACP 不是“通用订阅凭据协议”，不能把任意网站会员变成可嵌入登录。

Agent 不是所有模型都会识别的一种文件格式。这里的 `RenderDoc Agent` 是具体运行代码：planner、任务状态、工具循环、权限、停止条件、证据验收和 provider adapter；prompt/配置只是其中一部分。裸模型只会收到消息与工具 schema，并不会“承认一个 Agent 标准”。只要 adapter 能可靠提供文本与所需的 tool calling，ReplayLab 就能用同一套自有循环驱动它；订阅入口若只暴露 app-server/ACP，则厂商 Agent 拥有每个 Skill 的内层循环，ReplayLab 仍拥有外层 TaskPlan、Skill 选择、MCP grant、调度和结果验收。

因此这里的“统一效果”是统一产品合同，而不是承诺不同模型逐字给出相同结论：两种入口对 capture-aware 自由聊天都先由主 Agent 生成结构化 `GoalSpec` 和第一阶段计划，再由 LangGraph 外层状态图调度 Skill specialist、执行同一证据验收，并让主 Agent 在每个阶段后选择结束、询问、阻塞或继续派发下一阶段。裸模型 API 的 specialist 由 ReplayLab 驱动；订阅入口的 specialist 由厂商 Agent 驱动，但不能绕过外层合同。旧模型若仍返回 v1 `TaskPlan`，会自动降级为原来的安全单阶段流程。

简化定义如下：

```text
Agent = 模型 + 循环 + 状态 + 工具 + 权限 + 停止/验收
Skill = 一项任务的流程说明 + 工具白名单 + 证据合同
MCP   = Agent ↔ RenderDoc 工具
ACP / app-server = qrenderdoc Host ↔ 厂商完整 Agent
```

## 一体化架构

```text
qrenderdoc（唯一可见产品）
  ├─ 原生 RenderDoc AI 面板 / AI 按钮 / 连接与模型切换
  └─ 内置 renderdoc_mcp_bridge（冻结 UI 与 replay 上下文）
            ⇅ 本地 bridge protocol v2
ReplayLab AgentHost（隐藏后台）
  ├─ ModelConnector → RenderDoc Agent → 模型 API
  ├─ ExternalAgentConnector → Codex app-server / ACP Agent
  ├─ LangGraph Main Supervisor → GoalSpec → 多阶段有界计划/复盘
  ├─ Skill specialists → 独立会话 + 验收反馈循环 + 通过后释放
  ├─ ContextManager → 模型窗口预算 + 结构化摘要 + 最近原文
  ├─ filtered MCP proxy → ReplayLabMCP → 指定 qrenderdoc 实例
  └─ TaskEvent JSONL → 流式文本、工具状态、终态
```

`qrenderdoc` 与 `ReplayLab` 是产品层和内部能力层的命名，不应演变成两个用户应用。事件选择、像素坐标、shader identity、capture 生命周期和 replay 线程由 qrenderdoc 原生代码持有；`replaylab/renderdoc_mcp_bridge` 是同仓库首方集成模块，不经过 Python extension 扫描或用户启停流程。

规范源码和运行布局固定如下：

| 职责 | 规范源码 | 运行/发布位置 |
|---|---|---|
| qrenderdoc 原生 AI UI、生命周期与 typed intent | `qrenderdoc/` | qrenderdoc 可执行文件与资源 |
| capture bridge | `replaylab/renderdoc_mcp_bridge/` | `<appdir>/integrations/replaylab/python/renderdoc_mcp_bridge/` |
| AgentHost | `replaylab/src/replaylab_agent/` | 隐藏 sidecar |
| ReplayLab MCP Host | `replaylab/mcp_host/` | 隐藏 sidecar |

固定集成目录不包含 `extension.json`，也不会从 `%APPDATA%/qrenderdoc/extensions` 发现或覆盖同名模块。`integration.json` 是首方包的版本/入口元数据，不是扩展发现清单。qrenderdoc 通过 typed native API 启动 bridge、排队 intent 和查询 shader stage，避免依赖菜单文本或消息框文本解析。

这里同时使用“内部直调”和受限 MCP，并不矛盾：可信的 qrenderdoc 控制面直接处理窗口、capture 生命周期、当前选择和精确实例；内置 bridge 冻结上下文并管理 intent 队列/租约；可能由模型决定参数的数据读取才经过 capability-scoped MCP。模型不应持有 `ICaptureContext` 指针、跨线程直接调用 replay，也不应自动获得导出、文件、shader apply 等完整接口。MCP 在这里是进程与权限边界，不是让用户在多个软件之间切换。

原生按钮不需要先让模型猜流程：

| qrenderdoc intent | Skill | 当前证据合同 |
|---|---|---|
| `explain-current-event` | `renderdoc-explain-event` | 必须成功读取该 EID 的 draw details 与 pipeline state |
| `inspect-picked-pixel` | `renderdoc-inspect-pixel` | 必须读取纹理元数据并 inspect 精确 texture/mip/slice/sample/坐标；调用前后校验 cutoff EID |
| `reverse-edit-selected-shader` | `renderdoc-shader-workbench` | 专属 Shader Specialist：必须取得同一身份的 shader info 与 reverse evidence，区分捕获证据/推断/生成源码并完成 ABI 一致性验收；不允许写入 |
| `Performance` 面板按钮 | `renderdoc-performance` | 必须成功取得 action timings 或 counters 中至少一种测量证据 |

自由聊天才进入主 Agent。它先输出 `GoalSpec`，并可在派发前直接追问用户或报告阻塞；需要调查时，每个阶段最多生成 3 个任务，默认最多 3 个阶段、8 个总任务。只有无依赖、均为只读且技能类别不同的任务才会并行。`adaptive-supervisor-specialists/langgraph-v2` 让主 Agent 在取得阶段结果后重新决定下一步。每个 `SpecialistDescriptor` 仍提供厂商无关的独立角色、专属 system instructions、模型/推理提示、输入输出 schema 和验收标准。模型 API specialist 在验收失败时保留 Pydantic AI `message_history` 继续同一任务；外部 Agent 以宿主保存的上次回答、已成功工具和验收反馈重建同一逻辑会话。清理确认后，每个计划任务最终都产生一个已释放的 `accepted/failed/blocked` 回执；清理无法确认则致命失败，绝不伪造 `released=true`。独立专员失败不会取消已经安全运行的兄弟任务，依赖它的后继会被显式阻塞。只有 `accepted` 回执携带 `SkillResult`，失败任务同样消耗总预算并回到主 Agent 重规划；最新阶段仍失败时不能完成，后续纠正阶段全部验收通过后可正常收束。

聊天历史不再依赖可翻译的 `USER:`/`ASSISTANT:` 文本分隔符。qrenderdoc 发送带 `conversationId` 的 typed JSON；AgentHost 按所选模型分别计算上下文预算。窗口来源优先级为 profile 显式值、`genai-prices` 模型目录、保守 fallback，默认在可用输入约 75% 时压缩回约 55%。当前用户请求、冻结捕获身份、Goal/约束和证据引用不可被压缩；较老轮次由零工具结构化摘要器分块归纳，失败时使用确定性保底摘要，最近轮次保留原文。摘要以消息 ID 绑定并在本地保留 30 天，使一次性 CLI 进程的下一轮仍能延续压缩记忆。精确匹配官方域名的 OpenAI Responses 与 Anthropic API 使用 provider-native compaction；代理和其他模型 API 的每个 specialist 使用独立 `ProcessHistory`，在每次模型请求前按完整 MCP 调用/返回块压缩旧工具历史，并用唯一 call-id 及参数/结果 SHA-256 台账双向验证可保留的证据。紧凑摘要至少保留已完成工具、台账摘要和最新证据身份；失败/特殊 capability 消息保持原文。若当前请求和最新完整工具块仍放不下，会显式失败并把回执交回主 Agent，而不是静默截断。回执中的 generation 只统计宿主管理的 `ProcessHistory` 次数，provider-native 压缩记为 0。

## 连接策略

| 入口 | 当前接法 | 谁是 Agent | 状态/边界 |
|---|---|---|---|
| OpenAI | Responses API | ReplayLab | 已实现；API key 可保存到 Windows 凭据管理器，也可由会话或环境变量覆盖 |
| Anthropic Claude | Messages API | ReplayLab | 已实现 API；Claude 订阅需单独接官方 Agent SDK，当前尚未加入可选项 |
| Gemini / Vertex | Google GenAI / ADC | ReplayLab | 已实现 |
| Azure OpenAI | v1 或 legacy Azure endpoint | ReplayLab | 已实现并区分 v1 与 `api_version` 路径 |
| DeepSeek、Qwen、Kimi、MiniMax、GLM、豆包、混元、千帆、SiliconFlow | 厂商官方 API/套餐 Key，经原生或兼容协议适配 | ReplayLab | 已有 preset；兼容接口的能力不能仅按名称推断，需 probe/显式配置 |
| Ollama / 自管 endpoint | 本地 OpenAI-compatible | ReplayLab | 已实现；默认保守能力集 |
| ChatGPT/Codex 订阅 | [Codex app-server](https://developers.openai.com/codex/app-server/) | Codex | 已实现连接器、`model/list`、浏览器/设备码登录、取消与登出；不读取其 token 文件 |
| GitHub Copilot | [Copilot ACP](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) | Copilot | 自动运行 `copilot login --web-flow/--device-code`，只提取安全登录 URL/验证码，随后做无 prompt ACP 握手 |
| Kimi Code | 官方 [`kimi login` + `kimi acp`](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command) | Kimi | 自动运行设备码登录并做无 prompt ACP 握手；也可用官方套餐 Key 走模型 API |
| Qwen Coding Plan / Qwen Code | 专用 API endpoint，或官方 `qwen --acp` | ReplayLab / Qwen Code | Coding Plan 是套餐 Key，不是 OAuth；已有一键 API preset。ACP 模式为厂商交互式 `/auth` 配置后握手 |
| Gemini CLI | 官方 [`gemini` 登录 + `gemini --acp`](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/authentication.mdx) | Gemini CLI | 自动打开受控厂商终端完成 Google 浏览器登录，退出后做 ACP 握手；个人、Pro/Ultra 与组织账号按官方规则使用 |
| MiniMax Token Plan | 官方 [`sk-cp`/API key](https://platform.minimax.io/docs/token-plan/other-tools) | ReplayLab | 走 API；目前不宣称 MiniMax Code 有可嵌入的官方 ACP/app-server |
| 其他订阅 | 仅接公开 app-server、ACP 或 Agent SDK | 视协议而定 | 不扫描 Cookie、localStorage、CLI token cache 或私有本地配置 |

Claude 订阅的第三方产品路径应使用 Anthropic 官方 Agent SDK；当前官方状态仍允许 Agent SDK、`claude -p` 与第三方 Agent SDK 应用计入订阅用量，参考[官方说明](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)。Qwen Coding Plan 需要专用 endpoint 与套餐 Key，参考[Qwen 认证说明](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/)。ACP 客户端复用[官方 Python SDK](https://github.com/agentclientprotocol/python-sdk)，模型层复用 [Pydantic AI](https://github.com/pydantic/pydantic-ai)，Skills 遵循开放的 [Agent Skills 规范](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)。没有复制 GPL/AGPL 聊天项目源码。

## 已实现内容（2026-08-26）

- 21 个模型 preset 与 6 个外部 Agent preset；Profile v2 分离用户连接名称与稳定内部 ID，支持添加/编辑/删除/设默认、启用模型集合和默认模型，配置文件不保存原始 key。
- OpenAI Responses/Chat、Anthropic Messages、Google GenAI/Vertex、Azure OpenAI 与常见兼容 endpoint 的模型构造。
- 安全模型发现：识别完整 operation URL，最多尝试两个同源的有/无 `/v1` 候选，不跟随重定向、不降级 HTTPS，鉴权失败立即停止，并以流式累计上限拒绝超过 4 MiB 的响应；不支持枚举时允许手工模型/部署名。
- API/订阅统一编排：LangGraph 主 Agent 生成 `GoalSpec`，可先追问/阻塞，或按阶段产生最多 3 项有界 DAG；它读取 accepted result 和全量 released receipt 后继续规划。宿主默认限制 3 个阶段/8 个总任务，预算也会显式传给 reviewer。旧 `TaskPlan` 有单阶段兼容适配器；Shader 仍有专属角色、分阶段重建流程与显式 I/O/验收合同。
- 专员生命周期：模型 API 专员可在同一 `message_history` 中接收宿主验收反馈并重试，外部 Agent 保存有界逻辑会话；默认最多 2 次。只有确定性工具证据合同通过后才标记 accepted/released，主 Agent 不接收原始工具大结果和完整子会话。
- 模型感知上下文：typed conversation v2、显式/目录/fallback 窗口解析、75%→55% 高低水位压缩、分块结构化摘要、最近原文、跨进程摘要记忆、OpenAI Responses/Anthropic 官方原生 compaction，以及其他模型 API 的 specialist 原子工具块压缩。
- Codex app-server JSONL 客户端与基于官方 SDK 的 ACP 客户端；Codex 支持 account/model/login API。Copilot/Kimi 使用有界输出的官方登录命令，Gemini/Qwen 使用受控官方终端；完成后统一做无 prompt 握手并保存有界模型列表。文件、终端、额外审批等 Agent 请求默认拒绝。
- 6 个当前可运行 Skill：事件/管线、像素/纹理、shader、材质绑定、性能、资源；导出规范已建档但在原生 capability ticket 完成前不会提供给 planner。
- authoritative MCP 只读工具策略、每 Skill 工具过滤、按钮参数绑定、工具错误 envelope 识别，以及 required-all/required-any 证据验收。
- bridge intent 的 claim/renew/complete 租约；TaskEvent v1 统一排队、规划、工具、文本分块和终态事件。仅在厂商连接器提供 delta 时实时转发，普通模型 API 当前在生成完成后分块提交最终文本。
- `doctor`、无生成连接 `probe`、profile CLI、持续 worker 与 Codex MCP 配置输出。
- qrenderdoc 原生 `RenderDoc AI` ToolWindow 与 `Settings → AI`：连接/模型快速切换、普通聊天、事件/像素/shader/性能按钮、事件流和状态展示；API key 通过 stdin 写入 Windows 凭据管理器或保留为会话 key，聊天 prompt 通过 stdin 传递，二者均不出现在子进程命令行或 profile JSON。
- qrenderdoc 为每个窗口发现自己的 bridge manifest，并用 `worker --instance-id` 固定路由；TaskEvent 只按已知 task id 订阅，避免其他窗口的任务串入当前面板。
- AgentHost 300+ 项与 MCP Host 700+ 项离线/协议测试；新增覆盖 LangGraph 两阶段重规划、专员验收反馈续跑/释放、typed envelope 分隔符碰撞、不同窗口预算、结构化压缩不变量和跨进程摘要恢复。

没有执行真实付费模型生成，也没有替用户登录任何供应商账号；只对本机既有 Codex 登录态执行了不发送 prompt 的 account/model 握手测试。

## 当前安全边界

1. 模型可见工具必须属于 ReplayLabMCP authoritative `READ_ONLY_TOOLS`。未知工具和所有 effectful 工具默认拒绝，包括导出、workspace prepare/validate、shader apply/rollback、material update 等。
2. 当前 qrenderdoc intent 中的 `replayMutationAuthorized` 等布尔值不是能力票据，不能解锁写入。effectful 工具要等 qrenderdoc 实现短期、nonce-bound、参数/路径绑定的原生 capability ticket 后才可逐项开放。
3. intent 的 context SHA-256、capture session/path、event、resource、pixel/subresource、shader stage/resource/entry/raw identity 会校验。每次 scoped MCP 调用前后还会重新检查 capture session，防止长任务中切换 `.rdc` 后继续读错捕获。当前检查是调用前/后的防护，不是 Host 内原子快照；像素工具若要完全消除检查与读取之间的竞态，仍需在原生 Host 中加入显式 `event_id`/context token 并在一次调用内验证。
4. 每个 MCP Host 都通过 `REPLAYLAB_INSTANCE_ID` 绑定一个 qrenderdoc 实例，并使用一次性、此前不存在的 `RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY`。这是对现有 Host 启动时自动恢复 live-edit 的隔离缓解；正式发布前仍应在 Host 源码增加原生 `--no-resume`。
5. 自有模型 Agent 直接审计真实 MCP 返回，并把 Host 的 `{"error": ...}` 视为失败。外部 Agent 经代理记录弱证据审计并检查工具名/终态/非空回答；ACP 进程拥有普通用户 OS 权限，理论上可伪造自己的审计文件，所以它不是权限边界。
6. 因 ACP 标准不提供通用 OS 沙箱，ACP profile 默认拒绝执行。只有用户显式设置 `allow_unsandboxed_agent=true` 后才会启动受信任的官方厂商 CLI。它仍使用最小环境，且 ReplayLab client 不提供文件或终端能力。Custom ACP 的 argv 会保存在 profile 中，因此只能放非秘密参数；ReplayLab 会拒绝常见 token/key/password/header 参数载体，但任意厂商的位置参数无法靠黑名单证明无秘密，token/password 仍必须使用厂商受管登录或显式环境变量，不能写进命令参数。
7. 每个 Codex Profile 使用独立、持久的 `CODEX_HOME`，登录凭据、配置与状态不读取用户全局 Codex Home；删除连接或把 Codex 原子替换为其他 provider 时会安全清除此专属目录。运行时按 app-server 方言协商 schema-compatible read-only sandbox，并逐进程注入唯一的过滤 ReplayLab MCP；Apps、shell、unified exec、hooks、ambient multi-agent/goals、web search 和持久历史均关闭。`mcp-config` 仅保留为旧版/手工诊断输出，正常连接无需全局注册。
8. intent 一旦 claim 就会立即启动独立续租器，覆盖上下文预检、模型执行和收尾；续租失败会取消当前阶段，避免另一个 worker 重领后仍启动重复调用。原生面板为当前 qrenderdoc PID 选择最新、存活且已加载 capture 的 manifest，并为该实例只启动一个 pinned worker。Windows 下 worker 与 Codex 登录都必须先进入 start gate，成功绑定 `KILL_ON_JOB_CLOSE` Job 后才会收到 `start` 并创建下游进程；绑定失败会 fail closed。TaskEvent 仍没有通用跨进程文件锁，因此不要在面板之外再为同一实例启动第二个 worker。
9. `api_key_env` 不能复用 `PATH`、HOME/APPDATA/CODEX_HOME、Python/动态加载器/代理信任变量或 ReplayLab 控制变量。环境变量优先于按 stable profile id 绑定的 Windows Generic Credential；qrenderdoc 会先剥离所有已知模型凭据，仅给当前 API profile 恢复一个明确的会话 key，AgentHost 才能读取当前 profile 的 vault 条目；订阅 Host 不会继承这些模型 key。删除连接或原子替换 provider 会先清理旧 vault 条目。
10. 模型发现、Codex/ACP 连接器和统一 runtime 都有累计字节/字符/队列/最终结果硬限制；超过预算会中止当前操作，不能用 chunked 响应或许多小分块绕过上限。每次订阅登录/测试使用一个随机、空的临时工作目录，结束即删除，避免厂商 CLI 读取仓库级配置或跨厂商残留。
11. TaskEvent JSONL 与 Host 隔离目录采用 7 天本地保留期；清理只匹配专用目录中的安全任务名或 32 位十六进制隔离目录，不跟随符号链接，也不会让一次清理失败阻断 AI 请求。

## 安装与使用

后台依赖放在独立 Python 环境，不进入 qrenderdoc 进程：

```powershell
cd replaylab
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[all,test]"
.\.venv\Scripts\python -m pytest
```

普通用户在 qrenderdoc 中打开 `Settings → AI` 即可完成配置：

1. 添加连接并选择 `Model API` 或 `Agent Subscription (ACP / app-server)`。
2. API 模式填写连接名称、供应商、地址和 key；默认勾选 **Securely remember** 保存到 Windows 凭据管理器，也可取消勾选只在本次 qrenderdoc 会话使用。选择 `Fetch Models`，勾选要显示的模型并设默认值。用户不需要填写 Profile ID；内部 ID 自动生成且不会因改名而改变。
3. 订阅模式保存后立即进入受控连接向导；支持的厂商由后台检测 CLI、启动官方登录并在完成后测试协议，用户不再需要手工复制命令。CLI 自己持有订阅 token，ReplayLab 不读取或复制其缓存。
4. 在 `RenderDoc AI` 聊天框的 `Send` 旁切换连接和模型。这里的切换只影响当前 qrenderdoc 会话，持久默认项只在 `Settings → AI` 修改。`Enter` 发送，`Shift+Enter` 换行；按钮任务与聊天都会使用当前模型。

以下 CLI 主要用于自动化、诊断和开发。

API profile 示例：

```powershell
$env:OPENAI_API_KEY = "..."
.\.venv\Scripts\replaylab-ai profile add openai-main --preset openai --model <model-id> --capability tools --select
.\.venv\Scripts\replaylab-ai doctor
.\.venv\Scripts\replaylab-ai probe
```

`probe` 对普通 API 做鉴权/模型发现，不发送生成请求；Azure/Vertex 当前只构造 adapter，不验证实际 deployment/IAM。它不会推断或保存 tools/JSON Schema/vision 能力。只有核对模型官方能力后，才应使用 `--capability tools` 或 `--option supports_tools=true` 显式启用 RenderDoc Agent 工具循环。`--preset` 可替换为 `anthropic`、`gemini`、`deepseek`、`qwen`、`kimi`、`minimax`、`glm`、`doubao` 等。

不需要真实账号的协议自检可单独运行，且不会读取 `profiles.json`、系统凭据库或厂商登录缓存：

```powershell
.\.venv\Scripts\replaylab-ai self-test --json
```

它只使用 loopback HTTP 与内置 fake Codex app-server/ACP peer 验证 OpenAI-compatible、Anthropic-compatible、Google GenAI、Codex app-server 和 ACP 的协议拼接与解析；HTTP 检查还会主动设置一个敌对环境代理，确认本机凭据不会因缺少 `NO_PROXY` 而发给代理。可选 ACP runtime 未安装时会明确返回 `warning`/`skipped`。此结果不等于真实厂商账号、套餐额度、远端模型或网络可用性检查；真实连接仍应通过 `Settings → AI` 的测试连接或显式 `probe` 验证。

Codex/ChatGPT 订阅通过官方受管登录与 app-server：

```powershell
.\.venv\Scripts\replaylab-ai profile add codex-sub --preset codex --select
.\.venv\Scripts\replaylab-ai doctor
.\.venv\Scripts\replaylab-ai probe
```

无需修改用户全局 Codex 配置，也无需禁用其中已有的 MCP。ReplayLab 为每个连接使用 `%LOCALAPPDATA%/ReplayLab/codex/<profile-id>` 专属 `CODEX_HOME`，`Settings → AI` 通过 app-server 官方 account login API 在该目录完成登录；运行时只向该进程注入过滤后的 ReplayLab MCP，并验证最终 inventory。`mcp-config` 仅用于旧版/手工诊断。ReplayLab 会跳过通常不能由子进程启动的 WindowsApps alias，并依次检查 `PATH`、Codex Desktop 的受控 `%LOCALAPPDATA%/OpenAI/Codex/bin/<version>/codex.exe` 布局以及固定的 npm shim。若 CLI 安装在其他位置，请按[官方 Codex CLI 文档](https://developers.openai.com/codex/cli/)安装或将官方可执行文件所在目录加入 `PATH`；内置厂商 preset 不接受 profile 命令覆盖，只有明确选择的 Custom ACP 才能使用自定义命令。

内置 ACP 厂商会在界面中明确展示并默认确认其只读目录命令；Custom ACP
始终要求用户逐项检查命令后显式确认信任。各厂商的实际登录命令、浏览器/
device flow、ACP 启动参数和模型枚举限制见
[Agent subscription provider matrix](docs/agent-subscription-providers.md)：

```powershell
.\.venv\Scripts\replaylab-ai profile add copilot-sub --preset copilot-acp --option allow_unsandboxed_agent=true --select
.\.venv\Scripts\replaylab-ai doctor
.\.venv\Scripts\replaylab-ai probe
```

连接切换与 worker：

```powershell
.\.venv\Scripts\replaylab-ai profile list
.\.venv\Scripts\replaylab-ai profile select openai-main
.\.venv\Scripts\replaylab-ai run --skill renderdoc-explain-event --prompt "解释当前事件" --live-context
.\.venv\Scripts\replaylab-ai worker
```

原生面板和 `--instance-id` 路径允许同时打开多个带 capture 的 qrenderdoc 窗口，每个 worker 只消费自己的 intent。手工 CLI 若省略 `--instance-id`，仍要求恰好一个已加载 capture 的实例，且不会按“最近活动”静默猜测。

## 发布与平台边界

ReplayLabMCP、AgentHost 与 `renderdoc_mcp_bridge` 的规范源码现已纳入当前仓库；bridge 使用固定首方集成路径和 typed native intent API，不再依赖扩展清单、扩展扫描、用户目录副本或 UI 文本回调。双 sidecar 由 `packaging/Build-ReplayLabRuntime.ps1` 可复现构建，再由 `packaging/Deploy-ReplayLabRuntime.ps1` 校验清单、大小和 SHA-256 后显式部署。当前发行运行时仅支持 Windows；其他平台在具备等价进程树隔离前会 fail closed，不会退化为只终止父进程或使用共享 `/tmp` IPC。

后续工作集中在能力与跨平台边界：

1. 在 Host 增加原生 `--no-resume`，并实现 nonce-bound capability ticket 验证后再开放任何 effectful tool。
2. 当前面板已经展示文本、工具状态和终态；仍需补结构化 EID/resource 可点击引用、重试、provider 用量信息，以及自由聊天的 provider 级取消确认。常驻 worker 已有 stdin start/shutdown 控制、运行中 asyncio cancellation 与 Windows Job 兜底；自由聊天的“取消”当前是受控进程树终止，不等于厂商 API 已确认取消。
3. Windows API key 已接入凭据管理器；Linux Secret Service 与 macOS Keychain adapter 尚未实现，在这些平台不会降级为明文保存。
4. 将外部 Codex/ACP Agent 的细粒度 tool progress 全部归一化为 TaskEvent。当前外部 Agent 最终文本已统一，但部分厂商的内部工具过程只能显示粗粒度状态。
5. 当前进程树硬隔离使用 Windows `KILL_ON_JOB_CLOSE`。移植到 Linux/macOS 时应给 AgentHost、provider、ACP 与 MCP 建独立进程组，并使用独立 control FD/pipe 与 parent-death 机制；不能把 Windows 下的取消/回收保证直接宣称为跨平台保证。

后续仍应直接修改 qrenderdoc/RenderDocUltra 源码来深化集成，而不是另做一个 ReplayLab 桌面应用。后台 sidecar 是隔离网络 SDK 和 Python 依赖的实现细节，不是用户需要切换的软件。
