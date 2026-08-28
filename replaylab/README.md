# ReplayLab AgentHost（RenderDocUltra AI 接入层）

ReplayLab AgentHost 是 qrenderdoc 内嵌 AI 的后台源码，不是第二个需要用户切换的桌面软件。最终产品仍只有一个可见窗口：qrenderdoc。内部 AI 使用 AgentHost 的共享 `InternalToolService`；ACP/Cursor 的 capture 专员按任务获得受过滤的 stdio MCP；qrenderdoc 默认同时持有一方 `ReplayLabMCP.exe` loopback，供外部 Codex/IDE 连接，但内部 Agent、项目恢复和 Shader 保存都不依赖该外部服务。

当前已经同时完成可运行、可测试的后台接入层和 qrenderdoc 原生 `RenderDoc AI` 面板。`Settings → AI` 提供连接列表、API/订阅两类向导、Windows 凭据管理器长期 API key、模型发现/多选/默认项和订阅登录；聊天区可快速切换连接与模型，`Enter` 发送、`Shift+Enter` 换行。四个原生按钮直接启动带冻结上下文的内部任务，不再进入 intent 队列，也不会唤起 Codex Desktop。TaskEvent 仍负责增量状态与结果呈现。详细设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 模型、Agent、Skill、MCP 与 ACP

- 裸模型 API 不是完整 Agent。使用 OpenAI、Claude、Gemini 或国内模型 API 时，ReplayLab 自己的 `RenderDoc Agent` 负责问题总结、任务拆分、工具循环、状态、停止条件和验收。
- Codex app-server、Copilot/Kimi/Qwen/Gemini ACP 等入口已经是厂商 Agent。它们保留自己的内层模型/工具循环；ReplayLab 统一外层的主路由、typed 上下文、逐 Skill 调度、语义效果、证据验收和 UI，不再套一个会与厂商 Agent 争抢同一工具回合的第二内层循环。
- Skill 是按需加载的领域流程：说明该怎么查、能调用哪些工具、至少取得哪些证据以及如何验收。它不常驻，也不独立推理。
- 模型 API 与 Codex `dynamicTools` 直接调用 AgentHost 内的高层工作流，不启动 MCP 子进程；一方 MCP 仅是 ACP/Cursor 与显式外部客户端的受过滤协议适配层。ACP 是宿主与完整厂商 Agent 之间的协议边界，不是“通用订阅凭据协议”。

`ReplayLabInternal.exe` 是 qrenderdoc 持有的私有服务。`ReplayLabMCP.exe` 是默认开启、仅供外部 Codex/IDE 的 loopback 适配服务，并继续支持 ACP/Cursor 的任务级 stdio 路径；关闭 qrenderdoc 时按 owner 生命周期释放。若部署明确不需要外部 MCP，可设置 `REPLAYLAB_EXTERNAL_MCP_AUTOSTART=0`（也接受 `false`、`no`、`off`）关闭它；该开关不会改变内部高层接口。

Agent 不是所有模型都会识别的一种文件格式。这里的 `RenderDoc Agent` 是具体运行代码：planner、任务状态、工具循环、语义效果、停止条件、证据验收和 provider adapter；prompt/配置只是其中一部分。裸模型只会收到消息与工具 schema。订阅入口若只暴露 app-server/ACP，则厂商 Agent 拥有每个 Skill 的内层循环，ReplayLab 仍拥有外层 TaskPlan、Skill 选择、上下文、工作流效果、调度和结果验收。

因此这里的“统一效果”是统一外层质量合同，而不是承诺不同模型逐字给出相同结论：两种入口对 capture-aware 自由聊天都先由主 Agent 生成结构化 `GoalSpec` 和第一阶段计划，再由 LangGraph 外层状态图调度 Skill specialist、执行同一证据验收，并让主 Agent 在每个阶段后选择结束、询问、阻塞或继续派发下一阶段。裸模型 API 的 specialist 由 ReplayLab 驱动；订阅入口的 specialist 由厂商 Agent 驱动，但不能绕过外层合同。旧模型若仍返回 v1 `TaskPlan`，会自动降级为原来的安全单阶段流程。

项目所称的子 Agent 是由 ReplayLab 创建并记账的隔离 specialist session，不是厂商 Agent 自由派生的后台工作者；当前 Codex ambient multi-agent 明确关闭。当前只保留 Shader 与 Event/Pipeline 两个专员。不需要 capture 证据的问题由主 Agent 直接回答；没有匹配专员或缺少关键目标时，由主 Agent继续处理或结构化追问，不会假装存在一个尚未实现的专员或已经检查过 capture。

简化定义如下：

```text
Agent = 模型 + 循环 + 状态 + 工具 + 语义效果 + 停止/验收
Skill = 一项任务的流程说明 + 候选工具 + 前后置条件 + 证据合同
InternalToolService = 内部 Agent ↔ RenderDoc 高层工具
MCP   = ACP/Cursor 或显式外部客户端 ↔ RenderDoc 的任务过滤适配器
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
  ├─ 共享 InternalToolService → 全部双专员高层合同 → 指定 qrenderdoc 实例/快照
  ├─ Codex dynamicTools → 同一 InternalToolService
  ├─ ACP specialist → 一个任务过滤的一方 MCP server
  └─ TaskEvent JSONL → 流式文本、工具状态、终态

ReplayLabMCP.exe（首方适配器）
  ├─ ACP/Cursor：每任务过滤并在调度时校验 manifest
  └─ 外部 Codex / IDE / 自动化客户端：显式启动、按任务限制
```

`qrenderdoc` 与 `ReplayLab` 是产品层和内部能力层的命名，不应演变成两个用户应用。事件选择、像素坐标、shader identity、capture 生命周期和 replay 线程由 qrenderdoc 原生代码持有；`replaylab/renderdoc_mcp_bridge` 是同仓库首方集成模块，不经过 Python extension 扫描或用户启停流程。

规范源码和运行布局固定如下：

| 职责 | 规范源码 | 运行/发布位置 |
|---|---|---|
| qrenderdoc 原生 AI UI、生命周期与 typed intent | `qrenderdoc/` | qrenderdoc 可执行文件与资源 |
| capture bridge | `replaylab/renderdoc_mcp_bridge/` | `<appdir>/integrations/replaylab/python/renderdoc_mcp_bridge/` |
| AgentHost | `replaylab/src/replaylab_agent/` | 隐藏 sidecar |
| ReplayLab Internal Host | `replaylab/mcp_host/src/internal_host.py` | qrenderdoc 私有、非 MCP 的项目/保存服务 |
| ReplayLab MCP Host | `replaylab/mcp_host/` | ACP/Cursor 与显式外部客户端的一方适配器 |

固定集成目录不包含 `extension.json`，也不会从 `%APPDATA%/qrenderdoc/extensions` 发现或覆盖同名模块。`integration.json` 是首方包的版本/入口元数据，不是扩展发现清单。qrenderdoc 通过 typed native API 冻结上下文并直接启动内部任务，避免依赖菜单文本、消息框文本或外部 intent 队列。

内部与外部接口有明确边界：可信的 qrenderdoc 控制面处理窗口、capture 生命周期、当前选择和精确实例；内置 bridge 冻结上下文；模型只能看到 AgentHost 为当前 Skill 和语义效果发现的工具 schema。模型不持有 `ICaptureContext` 指针，也不会因为工具名字或提示词自动获得任意文件/回放能力。模型 API/Codex 走内部直接调用，ACP/外部客户端走一方 MCP；两种传输都绑定同一任务范围、审计和验收合同。

原生按钮不需要先让模型猜流程：

| qrenderdoc intent | Skill | 当前证据合同 |
|---|---|---|
| `explain-current-event` | `renderdoc-explain-event` | 只暴露一次性的 `inspect_event` 高层事务 |
| `inspect-picked-pixel` | `renderdoc-explain-event` | 只暴露 `analyze_pixel`，绑定 texture/mip/slice/sample/坐标与 cutoff EID |
| `reverse-edit-selected-shader` | `renderdoc-shader-workbench` | 按用户目标执行证据、可读 HLSL、编译/验证、可选替换/回滚和项目复用，不再硬缩成单一只读调用 |
| `Performance` 面板按钮 | `renderdoc-explain-event` | 使用同一内部服务的 timing/counter 只读工具；不会隐式构建 Shader Catalog |

Shader Catalog 是跨整帧建立的确定性 Shader/绑定/原始字节身份索引，服务项目复用、跨
capture 符号匹配和可移植目录；单个 EID、像素或当前 Shader 分析不依赖它。目录 cache
miss 现在返回 `catalog-index-required`，打开 RDC、恢复项目和普通 AI 按钮都不会隐式扫描
整帧。内部 `ExportShaderCatalog` 只接受显式、唯一、最多 64 个 EID，并把结果限制在
AgentHost 自有目录；空 EID 所代表的全帧导出在分块、进度与取消作业落地前 fail closed。

自由聊天进入主 Agent。它结合 typed 对话和选定的冻结环境整理 `GoalSpec`，先判断能否直接回答、是否需要结构化追问，或是否应派出 Shader / Event/Pipeline 专员。需要调查时，每个阶段最多生成 3 个任务，默认最多 3 个阶段、8 个总任务。只有无依赖、不同类别且不会争用效果状态的任务才会并行；workspace/project/ui/replay 等效果任务保持可验证的顺序。`adaptive-supervisor-specialists/langgraph-v2` 让主 Agent 在取得阶段结果后重新决定下一步。每个 `SpecialistDescriptor` 提供厂商无关的独立角色、专属 system instructions、模型/推理提示、输入输出 schema 和验收标准。模型 API specialist 在验收失败时保留 Pydantic AI `message_history` 继续同一任务；外部 Agent 以宿主保存的上次回答、已成功工具和验收反馈重建同一逻辑会话。清理确认后，每个计划任务最终都产生一个已释放的 `accepted/failed/blocked` 回执；清理无法确认则致命失败，绝不伪造 `released=true`。独立专员失败不会取消已经安全运行的兄弟任务，依赖它的后继会被显式阻塞。只有 `accepted` 回执携带 `SkillResult`，失败任务同样消耗总预算并回到主 Agent 重规划；最新阶段仍失败时不能完成，后续纠正阶段全部验收通过后可正常收束。

结构化追问不是普通按钮文字回填。用户点击单选/多选或填写 Other 时，前端同时发送稳定的 `clarificationId + optionIds + otherText`。问题产生时的 origin snapshot 会被保留，用户回答时再冻结 current snapshot；两者均是带 SHA-256 身份的候选。Host 在压缩和模型调用之前审视回答中的时间/目标意图，选择 origin 或 current。若两者目标已经不同而回答没有说明使用哪一个，Host 会给出“原问题 / 当前选择”的单选项，不会靠最近 UI 状态猜测。

聊天历史不再依赖可翻译的 `USER:`/`ASSISTANT:` 文本分隔符。qrenderdoc 发送带 `conversationId` 的 typed JSON；native prompt wire 上限为 16 MiB，它只是传输安全边界，不代替模型窗口。AgentHost 按当前选定模型解析经目录或 profile 显式验证的上下文窗口；目录未知的模型必须配置已验证的 `context_window_tokens`，不会猜测通用 32K fallback。预算先扣除输出、系统指令、工具 schema 和安全预留；默认在 usable input 达到 100% 时触发压缩，并压到约 25%。当前用户请求、选定冻结捕获身份、Goal/约束和证据引用不可被压缩；较老轮次转换为带来源、类别、重要度、创建/确认时间、general/capture 范围、过期时间与 `active/superseded/expired` 状态的 `MemoryFact`，最近轮次保留原文。同一语义键的新纠正会显式替代旧事实；排序优先保留有效、重要且最近确认的信息，capture 事实不会跨不兼容 session 继续冒充当前证据。摘要以消息 ID 绑定并在本地保留 30 天，使一次性 CLI 进程的下一轮仍能延续压缩记忆。精确匹配官方域名的 OpenAI Responses 与 Anthropic API 使用 provider-native compaction；代理和其他模型 API 的每个 specialist 使用独立 `ProcessHistory`，在每次模型请求前按完整工具调用/返回块压缩旧工具历史，并用唯一 call-id 及参数/结果 SHA-256 台账双向验证可保留的证据。若当前请求和最新完整工具块仍放不下，会显式失败并把回执交回主 Agent，而不是静默截断。

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

## 已实现内容（2026-08-28）

- 21 个模型 preset 与 6 个外部 Agent preset；Profile v2 分离用户连接名称与稳定内部 ID，支持添加/编辑/删除/设默认、启用模型集合和默认模型，配置文件不保存原始 key。
- OpenAI Responses/Chat、Anthropic Messages、Google GenAI/Vertex、Azure OpenAI 与常见兼容 endpoint 的模型构造。
- 安全模型发现：识别完整 operation URL，最多尝试两个同源的有/无 `/v1` 候选，不跟随重定向、不降级 HTTPS，鉴权失败立即停止，并以流式累计上限拒绝超过 4 MiB 的响应；不支持枚举时允许手工模型/部署名。
- API/订阅统一编排：两条入口共享 LangGraph 主路由、typed 上下文、`GoalSpec`、两个 Skill 和确定性验收合同；主 Agent 可直接回答、结构化追问或按阶段产生最多 3 项有界 DAG，并在读取 accepted result 与全量 released receipt 后继续规划。宿主默认限制 3 个阶段/8 个总任务，预算也会显式传给 reviewer。
- 专员生命周期：模型 API 专员可在同一 `message_history` 中接收宿主验收反馈并重试，外部 Agent 保存有界逻辑会话；默认最多 2 次。只有确定性工具证据合同通过后才标记 accepted/released，主 Agent 不接收原始工具大结果和完整子会话。
- 结构化澄清与快照：快捷选项发送稳定的 `clarificationId + optionIds + otherText`；回答时保留 origin 并冻结 current，Host 先按意图选择快照，歧义时给出单选项，不把两份候选塞进模型上下文。
- 模型感知上下文：typed conversation v2、当前选定模型的目录/显式验证窗口、未知模型强制 `context_window_tokens`、usable input 100%→25% 压缩、16 MiB native prompt wire、最近原文和带重要性/时效/替代状态的 `MemoryFact`；同时支持跨进程摘要记忆、OpenAI Responses/Anthropic 官方原生 compaction，以及其他模型 API 的 specialist 原子工具块压缩。
- Codex app-server JSONL 客户端与基于官方 SDK 的 ACP 客户端；Codex 支持 account/model/login API。Copilot/Kimi 使用有界输出的官方登录命令，Gemini/Qwen 使用受控官方终端；完成后统一做无 prompt 握手并保存有界模型列表。文件、终端、额外审批等 Agent 请求默认拒绝。
- 2 个可组合 Skill：Shader Workbench 负责可读 HLSL 重建、算法说明、编译/ABI 验证、可选替换/回滚和项目库复用；Event/Pipeline 负责 EID/pass/frame 输入输出、证据流程图、可折叠语义 pass 面板，并可请求 Shader 专员结果。
- CapabilityMatrix 把内部工具、条件性一方 MCP、模型工具支持和真实 Host blocker 分开；`ready`/`conditional` 路径都可尝试。WorkflowGuard 按 workspace/project/ui/replay 语义效果执行顺序、回执、后置条件和回滚验收，不以永久逐工具权限表锁死工作流。
- 工具效果按实际调用参数判定，而不是按工具名一刀切：当前固定 Pass 分类会先暂存 capture catalog，因此始终属于 `project`，仅在 `publish_to_ui=true` 时再产生 `ui`；AI 语义提案则按显式发布/持久化参数分别验收。两者都不能伪装成全局只读调用。
- TaskEvent v1 统一排队、规划、工具、文本分块和终态事件；原生按钮不再走 bridge intent 租约。
- `doctor`、无生成连接 `probe` 与 profile CLI；产品内部不生成或注册 Codex MCP 配置。
- qrenderdoc 原生 `RenderDoc AI` ToolWindow 与 `Settings → AI`：连接/模型快速切换、普通聊天、事件/像素/shader/性能按钮、事件流和状态展示；API key 通过 stdin 写入 Windows 凭据管理器或保留为会话 key，聊天 prompt 通过 stdin 传递，二者均不出现在子进程命令行或 profile JSON。
- qrenderdoc 为每个窗口发现自己的 bridge manifest，并把直接任务绑定到精确 `--instance-id`；TaskEvent 只按已知 task id 订阅，避免其他窗口的任务串入当前面板。
- AgentHost 300+ 项与 MCP Host 700+ 项离线/协议测试；新增覆盖 LangGraph 两阶段重规划、专员验收反馈续跑/释放、typed envelope 分隔符碰撞、不同窗口预算、结构化压缩不变量和跨进程摘要恢复。

没有执行真实付费模型生成，也没有替用户登录任何供应商账号；只对本机既有 Codex 登录态执行了不发送 prompt 的 account/model 握手测试。

## 当前运行与验收边界

1. CapabilityMatrix 是运行时事实表，不是永久授权表。`ready` 和 `conditional` Skill 都会尝试执行；模型尚未 probe、某个工具暂走一方 MCP、或提示词声称“没有权限”，都不会自行变成硬阻断。只有 Host 验证到且与当前专员任务相关的事实，例如所选 profile 明确不支持工具、没有已加载 capture、没有固定 qrenderdoc 实例或没有可用工具传输，才是 hard blocker；它们不会阻断无关的主 Agent 直接回答。
2. 主 Agent 请求的是 workspace/project/ui/replay 等语义结果；专员可以在当前 Skill 的运行时工具集中选择完成方式。Host 在调用前验证冻结目标和必要顺序，在调用后验证机器可读回执、效果后置条件和恢复记录。Shader 替换必须先对同一 workspace 完成 ABI 验证；替换后必须验证应用身份，失败或结果不确定时进入匹配的 rollback 流程。UI pass 建议必须绑定同一任务返回的 `classificationId`。
3. 用户发送自由问题或点击原生按钮时，qrenderdoc 会立即冻结带内容哈希的上下文。澄清回答会形成 origin/current 两份候选，Host 在任何上下文压缩或工具调度前按用户意图选择其一。每次工具调用仍绑定所选 capture session 与目标；若捕获被卸载、重载或身份不一致，Host 会阻断该调用并要求重新取得证据。
4. 模型 API 与 Codex `dynamicTools` 的双专员合同全部由同一 `InternalToolService` 直接执行，不启动 `ReplayLabMCP.exe`。ACP v1 没有等价的 Host 动态工具回调，因此 capture specialist 获得一个只有当前任务工具的 MCP server；这是协议适配例外。两条路径共享 Scope、WorkflowGuard、审计和验收。
5. 自有模型 Agent、Codex dynamicTools 和 ACP 的任务过滤代理都审计真实工具结果，并把错误 envelope、缺失回执或未满足后置条件视为失败。Agent 的自然语言“完成了”不能代替工具证据，Host 也不会因为未知工具名称就预先把整个专员判死。
6. 因 ACP 标准不提供通用 OS 沙箱，ACP profile 默认拒绝执行。只有用户显式设置 `allow_unsandboxed_agent=true` 后才会启动受信任的官方厂商 CLI。它仍使用最小环境，且 ReplayLab client 不提供文件或终端能力。Custom ACP 的 argv 会保存在 profile 中，因此只能放非秘密参数；ReplayLab 会拒绝常见 token/key/password/header 参数载体，但任意厂商的位置参数无法靠黑名单证明无秘密，token/password 仍必须使用厂商受管登录或显式环境变量，不能写进命令参数。
7. 每个 Codex Profile 使用独立、持久的 `CODEX_HOME`，只读取 RenderDoc AI Settings、profiles 与其自有凭据；不读取或修改用户的 `~/.codex/config.toml`。Codex app-server 通过 `dynamicTools` 回调任务工具，Apps、shell、用户 ambient MCP、hooks、多 Agent、web search 与持久历史均不进入 RenderDoc 专员合同。
8. 原生面板为当前 qrenderdoc PID 选择存活且已加载 capture 的精确 manifest，并直接启动一个受控任务进程。模型 API/Codex 不启动 MCP；仅 ACP 任务可按协议启动过滤后的一方 MCP 子进程。Windows Job 负责整个受控进程树回收。
9. `api_key_env` 不能复用 `PATH`、HOME/APPDATA/CODEX_HOME、Python/动态加载器/代理信任变量或 ReplayLab 控制变量。环境变量优先于按 stable profile id 绑定的 Windows Generic Credential；qrenderdoc 会先剥离所有已知模型凭据，仅给当前 API profile 恢复一个明确的会话 key，AgentHost 才能读取当前 profile 的 vault 条目；订阅 Host 不会继承这些模型 key。删除连接或原子替换 provider 会先清理旧 vault 条目。
10. 模型发现、Codex/ACP 连接器和统一 runtime 都有累计字节/字符/队列/最终结果硬限制；超过预算会中止当前操作，不能用 chunked 响应或许多小分块绕过上限。每次订阅登录/测试使用一个随机、空的临时工作目录，结束即删除，避免厂商 CLI 读取仓库级配置或跨厂商残留。
11. TaskEvent JSONL 与 Host 隔离目录采用 7 天本地保留期；清理只匹配专用目录中的安全任务名或 32 位十六进制隔离目录，不跟随符号链接，也不会让一次清理失败阻断 AI 请求。

## 安装与使用

后台依赖放在独立 Python 环境，不进入 qrenderdoc 进程：

```powershell
cd replaylab
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[all,test]" -e ".\mcp_host"
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

无需修改用户全局 Codex 配置，也无需禁用其中已有的 MCP。ReplayLab 为每个连接使用 `%LOCALAPPDATA%/ReplayLab/codex/<profile-id>` 专属 `CODEX_HOME`，`Settings → AI` 通过 app-server account API 在该目录完成登录；全部 capture 工具通过 app-server `dynamicTools` 回调同一 `InternalToolService`，Codex 不会发现用户的 ambient MCP inventory，也不会为内置任务启动 ReplayLabMCP。ReplayLab 会跳过通常不能由子进程启动的 WindowsApps alias，并依次检查 `PATH`、受控 Codex 安装布局以及固定 npm shim。若 CLI 安装在其他位置，请按[官方 Codex CLI 文档](https://developers.openai.com/codex/cli/)安装或将官方可执行文件所在目录加入 `PATH`；内置厂商 preset 不接受 profile 命令覆盖，只有明确选择的 Custom ACP 才能使用自定义命令。

内置 ACP 厂商会在界面中明确展示并默认确认其只读目录命令；Custom ACP
始终要求用户逐项检查命令后显式确认信任。各厂商的实际登录命令、浏览器/
device flow、ACP 启动参数和模型枚举限制见
[Agent subscription provider matrix](docs/agent-subscription-providers.md)：

```powershell
.\.venv\Scripts\replaylab-ai profile add copilot-sub --preset copilot-acp --option allow_unsandboxed_agent=true --select
.\.venv\Scripts\replaylab-ai doctor
.\.venv\Scripts\replaylab-ai probe
```

连接切换与直接任务诊断：

```powershell
.\.venv\Scripts\replaylab-ai profile list
.\.venv\Scripts\replaylab-ai profile select openai-main
.\.venv\Scripts\replaylab-ai run --skill renderdoc-explain-event --prompt "解释当前事件" --live-context
.\.venv\Scripts\replaylab-ai run --profile <id> --skill renderdoc-explain-event --prompt "Explain EID 10" --live-context --instance-id <instance>
```

原生面板和 `--instance-id` 路径允许同时打开多个带 capture 的 qrenderdoc 窗口。原生按钮总是显式固定实例；内部工具不会按“最近活动”静默猜测目标。

## 发布与平台边界

AgentHost、ReplayLabInternal、ReplayLabMCP 与 `renderdoc_mcp_bridge` 的规范源码现已纳入当前仓库；bridge 使用固定首方集成路径，qrenderdoc 原生按钮以冻结上下文直接启动 AgentHost 任务，不再依赖 intent 队列、扩展清单、扩展扫描、用户目录副本或 UI 文本回调。三个后台可执行程序由 `packaging/Build-ReplayLabRuntime.ps1` 可复现构建，再由 `packaging/Deploy-ReplayLabRuntime.ps1` 校验清单、大小和 SHA-256 后显式部署；AgentHost 管理主路由、直接工具和所有任务约束，ReplayLabInternal 提供私有非 MCP 服务，ReplayLabMCP 仅提供 ACP/外部协议适配。当前发行运行时仅支持 Windows；其他平台在具备等价进程树隔离前会 fail closed，不会退化为只终止父进程或使用共享 `/tmp` IPC。

后续工作集中在能力与跨平台边界：

1. 将共享高层工作流从当前历史包名 `src.server` 抽成稳定的 `replaylab_tool_core` 包，并给 Host 增加原生 `--no-resume`；新增效果工具应归入现有语义效果，补齐前置校验、机器回执、后置条件和匹配 rollback，而不是扩张永久逐工具权限表。
2. 当前面板已经展示文本、工具状态和终态；仍需补结构化 EID/resource 可点击引用、重试、provider 用量信息，以及自由聊天的 provider 级取消确认。常驻 worker 已有 stdin start/shutdown 控制、运行中 asyncio cancellation 与 Windows Job 兜底；自由聊天的“取消”当前是受控进程树终止，不等于厂商 API 已确认取消。
3. Windows API key 已接入凭据管理器；Linux Secret Service 与 macOS Keychain adapter 尚未实现，在这些平台不会降级为明文保存。
4. 将外部 Codex/ACP Agent 的细粒度 tool progress 全部归一化为 TaskEvent。当前外部 Agent 最终文本已统一，但部分厂商的内部工具过程只能显示粗粒度状态。
5. 当前进程树硬隔离使用 Windows `KILL_ON_JOB_CLOSE`。移植到 Linux/macOS 时应给 AgentHost、provider、ACP 与 MCP 建独立进程组，并使用独立 control FD/pipe 与 parent-death 机制；不能把 Windows 下的取消/回收保证直接宣称为跨平台保证。

后续仍应直接修改 qrenderdoc/RenderDocUltra 源码来深化集成，而不是另做一个 ReplayLab 桌面应用。后台 sidecar 是隔离网络 SDK 和 Python 依赖的实现细节，不是用户需要切换的软件。
