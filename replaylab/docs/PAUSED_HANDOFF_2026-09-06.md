# RenderDoc Ultra 开发暂停与接手清单

更新时间：2026-09-06 18:06（北京时间）。仓库：`D:\Study\GitHub\renderdocUltra`。

**用户已要求暂停开发和测试，交由其他 AI 接手。不要自动恢复，也不要安排后台续跑。** 本文件是最新状态；旧文档中“已完成主线”只对应当时的构建和测试窗口。

## 0. 接手复验结果（2026-09-06 18:57，新 AI 接手后补做）

接手 AI 已在**当前最新 Development 构建**（qdoc SHA `24b1115e61120c6271b92d9cbeed4d6432766cb94e54003aadb07074c843df6b`，即 §7 `build-v17-receipt.json` 记录的 async/独立 Host 寿命修复包，比 164104/165902 的旧包 `7734...` 更新）上补齐了 §8.2/§8.3 之前暂停的两项实机复验，均通过：

- **6.rdc / EID 11329 三次真实 Ctrl+S**：`output/shader-workspace-20260906/real-save-20260906-185209/verification/summary.json` 为 `passed=true / failures=[] / hostBoundarySimulated=false`。改 alpha（源 `97f79c...`→候选 `25412d0b...`）实际替换；注入 `#error`（`7dbacf...`）编译失败、错误可见且保留上一有效替换；恢复 `593c...`/`740...` 重新替换。验证期间显式 start/compile/apply/AI-review/register 均为 0；`reviewAndProjectAssetsPreserved=true`，项目已接受源码/分析/图与 head generation 8、knowledgeId `2cbef5ce...` 未变。三次保存 UI 收据 `saved=true`，`#error` 轮 `compilerErrorsVisible=true`。
- **1.rdc / EID 61523 跨捕获默认复用**：`real-save-20260906-185420/cross-rdc-verification/summary.json` 为 `passed=true / failures=[]`。当前 RID `1000000000000000754`、新 workspaceId `a054bb820ef14e91b90a4f2f45954be3`、源 `593c...`、候选 `740...`、原始 raw `3c4afb...`，来源为 6.rdc 项目知识 `2cbef5ce...`；`sourceEdits=0`、显式 mutation 工具 0、12 节点图、generation 8 保持。
- 编排入口：`output/shader-workspace-20260906/orchestrate_real_save_verification.ps1`（`RUN_VERIFICATION.cmd` 启动），总结果 `orchestrate-20260906-185209/orchestrate-summary.json` 为 `overallPassed=true`。它内联复刻了 `start_real_save_ui.ps1` 的启动/环境，并在跑 discover 前显式等待 `startup.json` 定型与 `auto-restore.json` 的 `outcome==returned`（editing enabled≥1）。
- **已知非产品瑕疵**：编排脚本 Close-Gui 未在 25 秒窗口内确认关闭，两次 `close.json` 为 `exited=false / fixtureCleanupVerified=false`，两个 qrenderdoc 进程（pid 39748/34800）需另行关闭（`output/shader-workspace-20260906/CLOSE_LEFTOVER.cmd`）。但两个 fixture 自身均 `cleanup=true / timerDestroyed=true / cleanupReason=success`，源码已恢复 `593c...`，属编排脚本关闭时序问题，非 Host/产品回归；159902/164104 的既有正常退出收据不受影响。
- **代码静态审查（接手 AI）**：§6 关键修复均在当前工作树在位且正确——ABI 兼容门明确允许两 digest 不同（只校验 `status==compatible`、`differences==[]`）、WinHTTP 12002 仅对同 job/cursor 的 GET 有界重试不重发变更、冻结子进程 `PYINSTALLER_RESET_ENVIRONMENT=1` 寿命修复、organization 仅 blocker 提前拒绝、`shader_console` 用 `threading.local` 做取消 scope 隔离。Host/Bridge/Agent 三处 Python 语法编译通过，无 TODO/FIXME、无裸 `except:`。未发现代码缺陷。

**本轮 Development Shader 主线（含三次 Ctrl+S 与跨捕获复用）至此在最新构建上完成实证验收。** EID/检查点/拖动性能线仍按 `output/interactive-replay-20260906/PAUSED_BY_USER.md` 保持暂停，未恢复。Release 未同步本轮版本；完整厂商矩阵、所有输入语义证明与完整 HLSL 语言服务器仍是事实边界。

## 1. 先读什么

1. `AGENTS.md` 与 `CLAUDE.md`。
2. 本文件，然后 `replaylab/docs/HANDOFF_2026-09-04.md`，再读 `replaylab/docs/REBUILD_PLAN.md`。后两份包含历史排障，不能用旧失败描述覆盖新收据。
3. `.agents/skills/replaylab-core/SKILL.md` 和 `.agents/skills/renderdoc-shader-workbench/SKILL.md`。
4. EID 性能单独看 `output/interactive-replay-20260906/PAUSED_BY_USER.md`；这一任务需要用户明确恢复，不能因继续 Shader 验收而顺带恢复。

本次涉及两个现有任务：“修复Shader ABI校验与工作区流程”和“优化EID拖拽播放速度”。较早“重构 AI 模块三层输出”“修复反编译失败与权限阻塞”的决策已合并进当前 HANDOFF/REBUILD_PLAN，不需要重新启动一套架构。

## 2. 总体进度

| 开发项 | 已做内容与证据 | 尚待处理 |
|---|---|---|
| AI / Agent 架构 | API 固定走 GitHub deepagents 0.3.3；ACP/Codex 订阅走厂商 Agent。删除旧自研 planner/reviewer/orchestration/runtime 推理链；冻结包也检查无旧模块 | 各厂商完整 Shader、提问、退回耗尽的实测矩阵未完成；Claude 登录不可用，不能冒称通过 |
| 统一 Skill、模板、验收 | replaylab-core 注入 API 主/子 Agent 与厂商 Agent；Host 控制 HLSL/图/报告模板、证据和最多 3 轮修正，严重错误不能靠耗尽次数通过。REBUILD_PLAN §3.5 第 3 行已按 HANDOFF 决策改写 | 已接线并有自动测试；完整模型实测仍按上项补齐 |
| Shader 主路线 / ABI | 可直接编译的源码或工具反编译结果走快路；真正不兼容时才修 ABI。当前会话完整 ABI 与资源身份决定能否替换，源码哈希不表示语义等价 | 最新版本的 `6.rdc` 自动匹配→编译→ABI→替换→可编辑 UI 已实际通过；最后三次 Ctrl+S 与 `1.rdc` 复验被用户暂停 |
| HLSL 整理与项目复用 | 已接受整理源码保存到项目，外部 CBuffer 别名复用。该样例匿名 `_数字` 标识符已清零，主要算法分成 12 阶段；保留有精度依据的表达式 | 不能把该样例的完成推广成任意 Shader 都可自动正确还原；不同模型的新输出仍必须走同一验收 |
| 着色器工作区 | 无 RDC 不显示旧文件/图；当前 RDC 匹配并按实际 Shader 去重；隐藏项目库；原生可编辑文档、Ctrl+S Host CAS、编译错误保留最后有效替换 | 最新后台刷新补丁后，完整三次保存复验待继续 |
| 流程图 / IDE | 节点固定、默认适应视框、上下数据流；单击书签式锚点、双击函数局部展开，独立前进后退；行号、F12 定义/引用选择、基础补全/高亮/诊断 | 基础 IDE 已有测试，不是完整外部 HLSL 语言服务器；未证明任意复杂语法的全部语义引用 |
| 卡顿与退出修复 | 工作区文件/授权检查移后台，结果按捕获和编辑状态隔离；关闭可取消。DXIL 解析移出 replay 回调并去掉平方级扫描和重复解析；冻结 Host 独立解包寿命修复 | 新 `6.rdc` 窗口有约 0.594 秒最大 500ms 心跳间隔；这只是该窗口观察，不能宣称全局 UI 或 History 延迟已验收 |
| 构建 / 中文 | Development v17 原生、内置 Bridge、4 个运行 EXE 已统一部署并核验；4884 条翻译完成 | Release 尚未同步本轮版本；不要混用 Release/Development 路径、工具链和旧会话 |
| EID / 检查点 / 拖动性能 | 保留 v17 代码、Windows/Linux/Android 构建和已有单测/Material 实测 | **单独暂停，未完成“Unity 般实时拖动”目标。** v17 GPU live-forward 正例和最新 History 复验未做，详见独立暂停文档 |

## 3. 暂停时的安全状态

- 本任务最后测试进程 **11528** 已正常关闭：`real-save-20260906-180122/close.json` 的 `fixtureCleanupVerified=true / exited=true / exitCode=0`。
- 两个 fixture 均 `cleanup=true / timerDestroyed=true`；公开 discovery 也正常完成。没有继续运行的本任务 GUI/GPU、构建、验证脚本或自动续跑安排。
- 180122 的 `ui.json` 为 `commands=[]`：没有执行三次保存，没有用户源码修改。源码仍是下节 `593c...`，项目工作区 head 仍为 generation **8** / knowledgeId **2cbef5ce749b366d0e37aaf96583b2f0**。
- 最新 `ui.json` 的 `ok=false` 是未执行 fixture 的完整 finish/保存验收；不能改为 passed。自动恢复与编辑 ready 的独立收据已经通过，不等同于完整保存通过。
- 工作树包含大量当前及其他任务的未提交修改。没有创建提交；不得 reset、整仓清理、覆盖未提交工作或直接手改用户项目的授权/验收记录。

## 4. 当前样例、源码与身份

项目：`D:\Study\抓帧\ReplayLab Project`（白银）。

- 主样例：`D:\Study\抓帧\白银之城\6.rdc`，EID **11329**，fragment / MainPS，DXIL / ps_6_6。
- 跨 RDC 样例：同目录 `1.rdc`，EID **61523**。
- 原始 Shader raw：`3c4afb434934308919e884efa26523f832807f2adc839d3a3754ceb559255205`。
- 已接受 HLSL：`593cbe25a817b680426e34f3bb1f5551710b0824d26be6157a7fcfdb6433aab9`。
- 已接受编译候选：`740adf26008aff2dc7998b1b94decd20b51c0dc028022a9e3e5a743683cc6268`。
- 6.rdc 最近工作区：`12fd011e408849a38b6b765638a4be63`，目录 `workspaces/shaders/v2/6272e04b1bfa0e9a03ae3df5e5f25e076782c893044af4b084e08172f773c72e`，相对上述项目根。
- 最近 6.rdc 原始 ResourceId 为 `1000000000000000860`；最近 1.rdc 为 `1000000000000000754`。**它们是历史收据中的值，接手后必须重新发现当前 ID/session/授权，不能照抄。**
- 6.rdc 原 ABI digest `b6020009d362764b604f9abd81b0e1880ecfcca4d997816d632ccd7845a2219e` 与候选 `2efd2ed6bbfe19536f32e879b7a1260602c6291289e7e57dba1fca23b32d625b` 不同，但已由物理接口和 DXIL CBuffer 访问证据证明 compatible、differences=[]。不要恢复“两个 digest 必须相同”的错误门槛。
- HLSL 提案/统计：`output/shader-workspace-20260906/readability-proposal.hlsl` 和 `READABILITY_REVIEW.md`。5462 行、12 顶层阶段，73 个主要计算模块；总计 129 个函数，保留资源绑定/采样数量等约束。物理 CBuffer 名称与偏移不为可读性乱改，使用现有外部别名。

数值证据来自 `public-review-20260906-132912-48408/summary.json`：旧重建版→新整理版在实际 6.rdc 的 FP16 1920×1080 输出比较中，8,294,400 分量均在允许误差内，最大绝对差 0.0001983642578125；D32S8 精确相同。它不是丢失的原始高级源码证明，也不是所有输入空间证明。

## 5. 哪些实机验收已做

以下目录均相对 `output/shader-workspace-20260906/`。

| 运行 | 验收范围 | 结果 |
|---|---|---|
| `real-save-20260906-164104` | 较早修复包，6.rdc 自动恢复；原生 Ctrl+S 改 alpha 并替换；保存 `#error` 后编译失败仍保留上版；恢复源码/候选；正常退出 | `verification-normalized-binding/summary.json` passed=true。并非模拟 Host，显式 start/compile/apply/review/register 工具调用为 0；正常产品流程完成工作 |
| `real-save-20260906-165902` | 较早修复包，1.rdc 正常打开，从项目自动匹配新 HLSL，当前编译/ABI/替换，可编辑唯一文档及 12 节点图；无源码修改 | `cross-rdc-verification/summary.json` passed=true；三次保存没有在 1.rdc 重复执行 |
| **`real-save-20260906-180122`** | **当前 v17 + 最终异步/解析修复包**，6.rdc 正常自动恢复、公开 discovery、可编辑文档/图 | `auto-restore.json` completed=true、applied=1/failed=0、editing enabled=1/failed=0；`discovery/summary.json` passed=true；UI ready=true/readOnly=false、当前唯一文档、无项目库、12 节点。用户暂停前未开始三次保存，随后正常退出 |

180122 的最大 UI 心跳间隔约 0.594 秒，122 个 500ms timer 样本，无超过 2 秒的间隔。fixture 自身也计入间隔，窗口有限；不能当作独立公平性能基准，更不能覆盖暂停的 History/EID 测试。

## 6. 代码位置与最近修复

| 位置 | 作用 / 接手注意 |
|---|---|
| `replaylab/mcp_host/src/project_restore.py`、`shader_workspace/project_source.py` | 项目源码复用、强制当前编译/ABI；反射不完整只允许 sourcePreparationOnly，不假造完整执行缓存 |
| `shader_workspace/workspace.py`、`shader_live_edit.py`、`internal_tool_service.py` | 当前身份、保存 CAS、应用/失败保留、去重与 live 会话 |
| `replaylab/renderdoc_mcp_bridge/project_shader_workspace.py` | 后台 snapshot 同时准备授权源码/stat；UI 以 capture/session/generation/documentToken/textRevision/save 状态发布；close 停 save 后取消 snapshot |
| `replaylab/renderdoc_mcp_bridge/shader_console.py` | 严格项目/源码读取；新增 worker-local `cancellable_console_reads`，不改变原校验、不污染其他线程或保存路径 |
| `replaylab/renderdoc_mcp_bridge/__init__.py` | Catalog 的 replay 原生采集与纯 CPU finalize 两阶段；不让 SWIG 对象跨出回调；256MiB/job DXIL 快照上限，超出标 unavailable；取消/换捕获/关闭不得发布迟到结果 |
| Bridge 与 Host 的 `dxil_access_signature.py` | 逐 Shader 的 SSA 使用索引代替每次 load 全文扫描；新 `extract_dxil_cbuffer_evidence` 只做一次访问解析，layout 无法证明仍为 unavailable；旧严格 API 保留 |
| `project_shader_flow_graph.py`、`project_shader_flow_hierarchy.py`、`qrenderdoc/Widgets/ShaderCodeEditor.*` | 固定布局、锚点、函数内部层次、代码导航/引用/行号等；图和代码历史独立 |
| `internal_host.py`、`replaylab_host.py`、`packaging/tests/Test-FrozenHostLifetime.ps1` | 冻结子 Host 用独立 PyInstaller 解包；不是“缺失 idna 打包”，不要用预热 import 替代寿命修复 |
| `replaylab/src/replaylab_agent/`、`.agents/skills/` | 新引擎与共同契约；不重新引入旧 AI 调度循环 |

真实 DXIL 新旧结果完整一致，旧双 API 耗时约 11.82/18.46 秒，新约 0.215/0.238 秒；收据 `dxil-parser-before.json`、`dxil-parser-after.json`、`dxil-parser-comparison.json`。这是纯解析观察，不是 GPU 加载速度。旧 parser 在 `dxil_parser_before_async.py` 仅作为输出目录里的对照证据，不参与产品。

## 7. 当前构建与测试收据

- 使用 **`x64/Development/q_render_soco_doc.exe`**。Release 未同步。
- `output/interactive-replay-20260906/build-v17-receipt.json`：qdoc SHA `24b1115e61120c6271b92d9cbeed4d6432766cb94e54003aadb07074c843df6b`；core SHA `75bd613b41140a9c4b8dcec381efec64ad421d8ebc5962ed19c919a1ed17df70`。公共 Python ABI 未变，内部协议 5。
- Runtime 构建清单时间 **2026-09-06T09:54:23.8388421Z**；构建与部署日志为 `runtime-async-final-build.log`、`runtime-async-final-deploy.log`。
- `final-runtime-delivery-development.json` passed=true：**99 冻结模块 / 15 Skill与引用 / 20 Bridge / 4 EXE** 与最终源码一致；旧 96 模块快照另存 `final-runtime-delivery-development-before-async.json`。
- `frozen-async-host-lifetime-final/result.json`：Internal/MCP 实际长期子进程在 launcher 退出后依然具有 Python/Unicode/IDNA 资源，双方正常 ExitCode 0。
- Host 全量：`replaylab/output/host-async-final-20260906.xml`，**1868 testcase / 2579 JUnit 含子测试，0 failures/errors，13 skipped，140.899 秒**。
- 随后只改 panel 关闭顺序，专项 `panel-close-final-20260906.xml`：**67 testcase / 110 含子测试，0 failures/errors/skips**。没有把这次微调后的全量冒称重跑过。
- 异步/解析/取消专项 `async-freeze-20260906.xml`：122 testcase / 148 含子测试，无失败；覆盖线程边界、取消、换捕获、迟到源覆盖保护、严格解析结果等。
- Agent 全量 `replaylab/output/agent-final-cwd-20260906.xml`：571 个用例、4 skipped、0 failures/errors。首次错误 cwd 导致 tests 包导入失败的日志保留，随后在 `replaylab/` 正确 cwd 重跑通过。
- 原生 v17：1650 断言/75 用例；qdoc 89/4；4884 翻译全部完成。平台构建范围与 GPU 缺口以独立性能暂停文件为准。

## 8. 用户让新 AI 继续时的顺序

1. 先确认接手范围是 Shader。EID 性能继续保持暂停，除非用户另外明确恢复。
2. 不要立即重新反编译或改写已有正确 HLSL。先在当前最终包重跑 **6.rdc 三次真实 Ctrl+S**：改 alpha→编译并替换；加入受控 `#error`→显示错误且保留上版；恢复 `593c.../740...`→重新替换。全过程保护项目接受资产、审核次数与 head。
3. 再正常打开 **1.rdc / EID 61523**，验证项目源码自动复用、当前 ABI/替换、可编辑唯一文档与图。重新发现当前 resourceId/workspaceId/session，不能复用 180122 已关闭的 target。
4. 完成后更新本文件/HANDOFF/REBUILD_PLAN 的当前状态，保留所有历史失败，不覆盖旧失败收据。如果功能已通过就结束本次修复，不为追求“全部 AI 等价”扩展无限测试。
5. 完整厂商 Shader/退回矩阵、更多 API/RDC，以及 Release 同步属于后续明确范围；按实际证据逐项补，不自动把性能线重新启动。

现成脚本均在 `output/shader-workspace-20260906/`：

- `start_real_save_ui.ps1`：默认 6.rdc/11329；1.rdc 传 `-CapturePath 'D:\Study\抓帧\白银之城\1.rdc' -EventId 61523`。脚本保持运行并管理正常退出，不要强杀 GUI。
- `discover_real_save_target.py <新运行目录>`：等正常自动恢复，公开只读发现并写 fresh target。
- `verify_real_ui_save.py --ui-receipt <run>/ui.json --ui-command <run>/command.json --workspace-id <fresh-id> --target <run>/target.json --host <绝对Development/ReplayLabMCP.exe> --output <全新验证目录>`：务必显式传 Development Host，默认值仍为 Release，不能漏传。
- `verify_cross_rdc_ui.py <新运行目录>`：只适用于 1.rdc 跨捕获验证。
- 完成或取消后向 `<run>/close.request` 写入说明，等 launcher 的 `close.json` 确认 ExitCode 0、fixture timer 已销毁。

Python 用仓库 `replaylab/.venv/Scripts/python.exe -X utf8`。Host tests 在 `replaylab/mcp_host/` 运行；Agent tests 在 `replaylab/` 运行；每轮新建短 basetemp。原生构建只允许一个 `/m:2`，显式 SolutionDir。用户项目里的 managed HLSL/manifest/授权/gate 只能经公开 Host 或原生 Ctrl+S 变更。

预计：最终两组实机复验顺利约 10–20 分钟；一旦出现新故障需先根据新收据定位，不能保证时长。完整厂商矩阵、更多 API 或完整语言服务器不属于这个估时。

## 0b. 实时语义错误检测（编译级 live diagnostics，2026-09-06 新增）

用户反馈：删除一整块代码（括号仍配对、语法仍合法）时编辑器没有任何提示。定位结论：C++ 词法解析器（`localDiagnosticsFor`）本来就在脏缓冲上实时跑，能报缺引号/括号不配对/注释未闭合；缺的是**语义错误**，只有真正的编译器能发现。而编译器此前只在 Ctrl+S 保存后由 watcher 跑（watcher 只编译不应用）。因此新增「对未保存缓冲的防抖后台编译检查」。

实现（无需 C++ 重编，走 Bridge 部署 + Host 冻结）：
- `shader_workspace/workspace.py` 新增 `compile_check(workspace_path, text)`：完全复用 `compile()` 的编译算子/模板值/`run_operation`，但输入是缓冲文本、在一次性 job 目录里编译，**不发布 revision、不改 manifest、不写历史、不产生候选、不应用**，只返回 `{ok, diagnostics}`。route-plan（部分 GL/Vulkan 多步链）返回 `ok=None`，调用方回退到保存后诊断，避免半程误报。任何内部异常也返回 `ok=None`，绝不伪造报错。
- `shader_workspace/diagnostics.py` 新增共享 `parse_compiler_diagnostics(text, input_name)`（放低层避免与 watcher 循环依赖），同时解析 DXC(clang 式 `file:line:col:`) 与 FXC(MSVC 式 `file(line,col):`)，note→hint，clean→[]。已在 Linux 上单测通过。
- `shader_workspace/watcher.py`：`ShaderWorkspaceWatchManager.compile_check` 委托（新建 service，无需 watch job）。
- `shader_live_edit.py`：`ShaderLiveEditManager.check_authorized_source`，与 save 相同的授权校验，但**在管理器锁外**跑慢编译，避免阻塞 stop/save/prune。
- `internal_tool_service.py`：`parse_shader_editor_check_request`（比 save 少 `expectedRawSha256`）、`shader_editor_check_response`、`check_shader_source`；`internal_host.py`：新增 `POST /replaylab/shader-source/check`（不新增 health capability，客户端 health 校验不变）。
- Bridge `shader_editor_client.py`：`check(...)` 方法 + `CHECK_PATH` 独立 transport；`project_shader_workspace.py`：脏缓冲编辑防抖（`LIVE_CHECK_DEBOUNCE_MS=600`，单击停顿后才触发）→ 后台 check worker → 结果按 `textRevision` 守卫（缓冲已变则丢弃）→ 喂给**既有** `SetDiagnostics`（红波浪线 + 悬停提示）与问题面板。脏缓冲不再一律清空诊断，而是显示与当前编辑版本匹配的 live-check 结果；无新结果时不显示旧位置的陈旧波浪线。
- 优先级：check 与 save 共用进程级 WinHTTP 单请求槽（`_ACTIVE_REQUEST_THREAD`），互相礼让；编译多为亚秒级，Ctrl+S 至多短暂排在一次 check 之后。check 出错/超时只丢弃该次（下次停顿重试），不影响保存。

部署：`output/shader-workspace-20260906/REBUILD_HOST_AND_BRIDGE.cmd`（bridge 部署会拷 `project_shader_workspace.py`/`shader_editor_client.py`，Host 冻结覆盖全部 Host 改动）。**尚未实机验证**——需在真实 dxc + 打开的 workspace 上确认删除一块代码后出现红波浪线；单文件解包 shader（如 593c）无 #include，include 解析非问题。

F12 单引用直接跳转仍是唯一需要 qrenderdoc(C++) 重编的项（`CodeEditorService.cpp::showReferences`，`hits.size()==1 -> navigateToDefinition` 已在位）。

## 0c. 第一次部署复盘（2026-09-06 晚）

用户跑 `REBUILD_HOST_AND_BRIDGE.cmd` 后反馈：控制台窗口仍在、流程图单击"和之前一样"。截图定位：
- **Host 冻结失败**（`build-runtime exit=1`）：`对路径 "ReplayLabInternal.exe" 的访问被拒绝` → `Test-FrozenHostLifetime.ps1:40 Frozen ensure failed`。根因是 qrenderdoc 关闭后仍有**残留 Host 进程**占着 exe。原脚本只检查 qrenderdoc，且**构建失败后第 3 步照样把旧包部署了一遍**，所以 Host 侧改动（控制台窗口）全部未生效。已修：脚本第 0 步 `taskkill` 四个 Host 进程并二次确认；任一步失败立即停止，绝不部署旧包。
- **Bridge 部署其实成功**（部署文件与源码哈希一致、live-check 代码在位）。流程图"没变"是因为 250→180ms 的差别感知不到。用户要的是"系统双击图标那种即时"。复查发现双击展开（`_flow_graph_subgraph_requested`）**只换流程图场景、从不 `GoToLine`**，与单击的编辑器跳转作用在不同控件、并不冲突；先前"双击闪跳"的实质是单击跳到了错误的声明位置，如今已跳到正确的调用位置，双击时编辑器停在调用处 + 图展开内部反而是连贯的。因此去掉了单击的等待：`_activate_node` 改为 `self._click_timer.start(0)`（下一事件循环即发出，感知即时，且不在鼠标处理器内同步重入）。`_expand_node` 保留 stop 作为兜底。

若下次 Host 冻结仍报错，先看是否需要清理被保留的 `replaylab/output/.pyinstaller-work`（上次构建因无法证明安全清理而保留了工作目录）。

## 0d. 更正：控制台窗口"修复"是误判，且它导致 Host 冻结失败（2026-09-06 晚，第二次部署）

第二次部署（脚本已先 taskkill 残留进程，且**确认无残留**）Host 冻结仍以完全相同的 `Frozen ensure failed` 失败，而 `ReplayLabInternal.exe --help` 冒烟测试通过、所有改动模块在 Linux 上 `import` 干净。时间线：上午 09:5x 最后一次成功冻结用的是原始 `_spawn_detached` 标志；今晚两次失败都含本 AI 改的标志。`ensure` 的逻辑是 `_spawn_detached` 拉起 `serve` 后轮询 `health()`，超时即非 0 退出——即 serve 子进程在新标志下起不来。

根因是本 AI 早先对 Windows 标志的理解有误：微软文档明确 *CREATE_NO_WINDOW is ignored if used with DETACHED_PROCESS*，所以原始的 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` 实际效果就是 `DETACHED_PROCESS`（子进程**无控制台**，本来就不会弹窗）。去掉 `DETACHED_PROCESS` 后子进程改为附着隐藏控制台，进程生命周期语义改变，导致 serve 不健康。**已用 git diff 精确还原 `internal_host.py`/`replaylab_host.py` 两处 `_spawn_detached` 为原始三标志**（未触碰同文件里之前 AI 的其他未提交 hunk）。

关于"启动时两个常驻命令行窗口"的真实来源：逐一排查后，Bridge 拉起两个 `ensure` 用 `_hidden_process_flags()`（隐藏）；serve 为 DETACHED（无控制台）；`replaylab-ai.exe` 隐藏；C++ 侧不拉起 ReplayLab exe；Host 内 8 处未加隐藏标志的 subprocess 调用（`tools.py:370` 为 taskkill 回退、编译器真正入口 `tools.py:786` 已隐藏）均为短命工具/探针进程，无一在启动时长驻。**代码内未能定位来源，需要用户提供两个窗口的标题栏（含 exe 路径/命令行）再定位，不要再凭猜测改动。**

## 0e. 两个控制台窗口的真实机制 + ensure 失败与之无关（2026-09-06 晚，第三次构建后）

用户提供了窗口标题：`…\x64\Development\replaylab\ReplayLabMCP.exe` 与 `…\ReplayLabInternal.exe`——即两个 **serve 长驻进程本身**。同时，把 `_spawn_detached` 标志还原为原始三标志后，Host 冻结仍以完全相同的 `Frozen ensure failed` 失败。两条事实合起来：

- **窗口机制**：`_spawn_detached` 用 `DETACHED_PROCESS` 启动的是 PyInstaller **onefile 引导器**；引导器本身无控制台，但它会再拉起真正运行 Python 的**子进程**，该子进程是控制台程序、父进程又没有控制台可继承，Windows 便为其**新分配一个可见控制台**。所以 0d 里"原始标志不会弹窗"的推断对引导器成立、对其子进程不成立。本 AI 最初"去掉 DETACHED_PROCESS、让引导器持有隐藏控制台、子进程继承之"的思路**在机制上是对的**，它没生效只是因为 Host 从未成功重新打包。**待 ensure 问题解决后应重新应用该修复**（当前为隔离变量，仍保持原始标志）。
- **ensure 失败与标志无关**：还原后照样失败，且 Internal serve 启动路径中本 AI 的全部改动仅为新增定义（方法/路由注册/函数），启动时不执行，模块导入干净；SKILL.md 只打进 agent 且其自检在探针之后。故原因不在本 AI 的 Host 代码，需拿到 serve 的真实 stderr。探针日志每次被 `Remove-ControlledBuildSession` 的递归删除先行清掉（删到被僵尸 serve 占住的 exe 才失败），因此改为让用户在 PowerShell **前台直接运行** `session-e21f…\internal\dist\ReplayLabInternal.exe serve --runtime-dir … --port 47331`（隔离 LOCALAPPDATA / REPLAYLAB_INSTANCE_ID / 注册表环境变量）以直接观察报错或卡点。每次失败的探针会遗留一个不健康的 serve 占着 exe，`REBUILD_HOST_AND_BRIDGE.cmd` 第 0 步会在下次构建前清掉。

## 0f. 根因定位：`Frozen ensure failed` 是探针脚本自身的 ExitCode 竞态，Host 完全健康（2026-09-06 深夜）

用户手动运行 `Test-FrozenHostLifetime.ps1`（Bypass）并保留探针目录后，`result.json` 给出决定性证据：
`launcherExited=true, launcherExitCode=null, childExited=true, childExitCode=0`；`ensure.json` 是 ensure **成功**输出（`started:true`），`ensure-stderr.log` 为空。即 ensure 成功、serve 健康并按要求以 0 退出，但探针读到的 `$launcher.ExitCode` 为 `$null`，PowerShell 中 `$null -ne 0` 为真，于是误抛 "Frozen ensure failed."。

根因是 PowerShell 5.1 已知坑：`Start-Process -PassThru` 得到的进程若退出很快且未提前缓存句柄，`.ExitCode` 读回 `$null`。探针对 `$child` 已正确做了 `$null = $child.Handle`，唯独漏了 `$launcher`。这是探针的潜在竞态，今早未触发、今晚 ensure 退出更快后每次触发；**与本 AI 的 Host 改动无关**（手动 ensure 三次全部成功，`/health` 正常）。

修复：`packaging/tests/Test-FrozenHostLifetime.ps1` 在 `Start-Process` 之后立即 `$null = $launcher.Handle`，并在 `WaitForExit` 后显式拒绝 `$null` ExitCode（给出明确错误而非误导性的 "ensure failed"）。

同时**重新应用**了控制台窗口修复（0e 已确认机制正确）：`internal_host.py`/`replaylab_host.py` 的 `_spawn_detached` 改为 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`（去掉 `DETACHED_PROCESS`），注释已改为准确描述 onefile 引导器/子进程的控制台继承机制。此修复由修好的探针在下次构建中验证。

验证顺序：先用修好的探针手动跑一遍当前已打包（原始标志）的 exe 确认探针通过 → 再跑 `REBUILD_HOST_AND_BRIDGE.cmd` 完整构建（含控制台修复 + 实时编译检查 + cbuffer 命名技能）。

## 0g. 第四次构建成功后的两项修复（2026-09-06 深夜）

构建成功、控制台窗口消失、单击即时。用户反馈两项：删代码无红波浪线；双击节点触发了单击的代码定位。

- **实时诊断无效的原因**：用户 shader 是 SPIRV-Cross 反编译 + 接口修复产物，其工作区走 **route-plan 多跳编译**（routePlan 在工作区创建时设定，是常态而非特例），而 `compile_check` 之前对 route-plan 直接返回 `ok=None` 回退——等于主力场景根本没启用。修复：`compile_check` 重写为双 lane：直接 lane 用 `run_operation`；route-plan lane 直接复用 `_run_route_chain(…, "compile", …)`（它只跑编译跳、不写 manifest、不发布，发布是 `_compile_route_chain` 在其返回后做的），传一次性 `history` 并跳过验证器/发布。失败时沿异常链解析 `see <job_dir>` 提示读取 stderr/stdout（与 watcher 同一线索），按"文件位于 root/jobs 内"判定缓冲本地诊断，其余折叠为 line-0 非导航错误；`ToolConfigurationError`/`ManifestValidationError`/无法启动/超时一律返回 `ok=None`（环境问题不伪造报错）。结束时按"检查前后 jobs 目录差集"清理本次新建的 job 目录（持有工作区锁，无并发干扰）。同时 Host 响应与客户端新增 `reason` 透传，Bridge 对"检查出错"与"不可检查原因"各打印一次 `[ReplayLab] Shader Workspace live check …`，便于下次定位。
- **顺手修掉一个真正致命的疏漏**：`workspace.py` 新增的模块级 `re.compile` 依赖 `re`，但该文件原本未导入 `re`（py_compile 查不出，冻结后 Host 导入即 NameError、ensure 必失败）。已补 `import re`。同理 Bridge `project_shader_workspace.py` 使用 `time.monotonic()` 补 `import time`。对全部改动文件做了"使用但未导入"静态检查 + 全模块真实 import，其余 4 处报告均为 docstring/注释/局部变量误报。
- **双击触发单击定位**：单击改为 0ms 即时后，第一击的跳转必然先于第二击发生，且双击展开只换图不动编辑器，故不引入延迟的解法是**双击时恢复单击前的光标**：`_flow_graph_node_activated` 在 `GoToLine` 前用 `GetCursorLine/GetCursorColumn` 记录 `preClickCursor`（含单调时间戳）；`_flow_graph_subgraph_requested` 在 `open_subgraph` 后若该记录在系统双击间隔 + 0.75s 内，则 `GoToLine` 回原位并清除。净效果：单击即时跳转、双击只展开图、编辑器位置不变。纯 Bridge 实现，无需重编 C++。

部署：再跑一次 `REBUILD_HOST_AND_BRIDGE.cmd`（Bridge：双击恢复 + 日志；Host 冻结：route-plan 检查 + reason）。验证：删一块导致语义错误的代码、停顿约 0.6s 应出现红波浪线；若仍无，看 qrenderdoc Python 控制台的 `[ReplayLab] Shader Workspace live check …` 行。

## 0h. 新功能：左侧面板显示使用该 Shader 的 EID + 右键跳转（item 11，2026-09-07 凌晨）

用户选定下一项为 item 11。实现（纯 Bridge，`project_shader_workspace.py`）：
- **数据源**：不用 RenderDoc `GetUsage`（shader 资源无 usage 条目）也不逐 draw 回放（太慢），而是读每个 workspace 的 `<workspacePath>/manifest.json` 取 `capture.catalogPath` 与 `shader.resourceId`，再读 capture catalog（`renderdoc-ultra-capture-symbols/v1`）的 `uses[]`（每条 = `eventId` + ShaderKey 字段），按 `resourceId`（capture 内唯一）分组去重排序。放在**已有的文件工作线程**（`_build_workspace_snapshot_files`）里完成，结果以 `eventIds` 随快照返回；catalog 按 `(path, mtime_ns, size)` 缓存（≤32 条），manifest/catalog 读取有大小上限，任何异常只导致该 shader 无 EID 列表，绝不影响快照。
- **显示**：叶子标签追加 `[EID a, b, c, d +n]`（前 4 个），tooltip 列出前 64 个与总数；EID 集合并入树签名，变化时自动重绘。
- **右键菜单**：`Open Shader`；`Used by N event(s)`；每个 EID 一项 `Jump to EID N`（最多 48 项，超出提示看 tooltip）。跳转 = `ctx.SetEventID([], eid, eid)`（Bridge 已有先例）。仅当 workspace 的 `capturePath` 与当前 capture binding 的 `capturePath`（均 `_normal_path`）一致时可用，否则置灰并提示先打开对应 capture——EID 是 capture 局部的，跨 capture 跳转无意义。
- 新增翻译串（`Used by %1 event(s): %2` / `Right-click to jump to an event` / `Open Shader` / `Jump to EID %1` / `… and %1 more (see tooltip)` / `Open the capture this Shader belongs to first` / `No event uses this Shader in the capture catalog`）当前为英文回退，待下次 QM 重建补中文。
- 单测：合成 manifest+catalog 验证分组/去重/坏数据过滤/备用键名/缓存命中与失效，通过。
- 限制：EID 列表来自 catalog 的覆盖范围——单事件 catalog 只知道那一个事件，完整列表需全帧 catalog（`prepare_active_capture_project`）。

## 0i. 第五次部署后的三项反馈处理（2026-09-07 凌晨）

用户反馈：删代码仍无波浪线但问题面板出现 "The Shader does not compile (error outside the edited source)"；双击节点触发单击定位；EID 只显示一个。处理：

1. **实时诊断位置映射 bug（Host `workspace.py` `compile_check`）**：面板那条提示证明检查已在跑、编译器也报了错，只是位置未被识别为缓冲本地。原因：route 跳的输入名为 `input.hlsl`，但编译器报的是相对/混合斜杠路径，`Path(...).resolve()` 按 Host cwd 解析落在 jobs 外，而完整字符串又不等于 `"input.hlsl"`。改为按**文件名**判定（`input.<文本扩展名>`/`shader.hlsl`），并仅当扩展名为文本源（.hlsl/.glsl/.slang/.frag/.vert/.comp…）才视为缓冲本地——二进制中间产物（input.spv）的行号对 HLSL 缓冲无意义，仍折叠为 line-0。
2. **本地即时语义分析（Bridge，新增，无需编译）**：用户指出 IDE 的"瞬间"提示来自本地语法/引用分析。新增 `hlsl_local_diagnostics(text)`：去注释/字符串/预处理行（保留偏移，收集 `#define` 名）→ 标识符 token（正则带负向后顾，排除数字后缀 `0.5f/1e5f/0x1F` 与 `.member` 访问）→ 第一遍收集用户类型（struct/class/interface/cbuffer/typedef）→ 第二遍收集声明（`<类型>[修饰符] NAME`、`Texture2D<float4> NAME`、同深度逗号续声明、宏）→ 第三遍报告"使用但全文件从未声明"的标识符，跳过 keyword/intrinsic/类型/semantic（`:` 后）/`register()`/`packoffset()`/语句级 `[attribute]`/`::`。**刻意 scope-insensitive**（任意处声明即算声明），以换取几乎零误报；编译检查兜底其余。单测：SPIRV-Cross 风格样例零误报；删声明/拼错名/删函数均定位到行列；4680 行 40ms。集成：独立分析线程（latest-wins）+ 150ms 防抖（`LOCAL_ANALYSIS_DEBOUNCE_MS`），结果按 `documentToken`+`textRevision` 守卫，`_current_problems` 在脏缓冲时先列本地分析、再合并同版本编译诊断（按行+文本去重），带 `length` 使波浪线覆盖整个标识符。
3. **流程图交互重做（Bridge `project_shader_flow_graph.py`）**：撤销 0h 的"双击复位光标"临时方案（删除 `_remember_pre_click_cursor`/`_restore_pre_click_cursor_if_double_click`）。取消节点主体单击跳转：主体单击只选中；节点**内部右缘**新增 → 图标（`_NODE_ARROW_STRIP=26`，标题/描述宽度相应收窄），悬停高亮 + 手型光标，**点击箭头才跳转**；双击主体展开图，双击落在箭头上视为再次跳转而非展开；回车键仍可跳转。跳转仍经 `_flow_graph_node_activated` 的行锚点（Scintilla marker）并校验锚点仍在所属函数内且仍为该调用，满足"书签式绑定、不受编辑偏移影响"。
4. **EID 全帧列表（Bridge）**：RenderDoc 有 `ResourceUsage::Shader`，对当前打开的 capture 用 `ctx.GetResources()` 把 int 映射回 `ResourceId`，`Replay().AsyncInvoke('replaylab-shader-usage', cb)` 在回放线程调 `ctrl.GetUsage(rid)`（帧记录查表，便宜，且替换 shader 不影响——usage 按原始 id 记录），结果经队列在 30ms 定时器落回 UI，按 `(capturePath, resourceId)` 缓存直到 capture 切换；live 结果覆盖 catalog 列表（catalog 可能只是单事件目录）并重绘树、保留选中项。回答用户：**是，整帧所有绑定该 shader 的事件都会列出，含替换后**。

部署：再跑一次 `REBUILD_HOST_AND_BRIDGE.cmd`（Host：位置映射修复；Bridge：本地分析 + 箭头 + 全帧 EID）。

可选下一步（用户提到"开源实现"）：C++ `CodeEditorService` 已内置 LSP 客户端（`ShaderEditor_LanguageServer` 配置项，didOpen/didChange/publishDiagnostics/definition），接入开源 `shader-language-server`（antaalt，MIT，Windows 二进制）可获得进程内 DXC 诊断 + 悬停类型 + 补全 + 转到定义，即 item 12 的大部分。需用户下载二进制并配置路径，未做。

## 0j. 悬浮为空/行尾无波浪线（C++）+ item 10 流程图 main→阶段→输出（2026-09-07）

用户截图证实两层诊断均已工作（本地 `Undefined identifier` + 编译器四条、行号正确、有波浪线）。两个残留问题与修法：
- **悬浮提示空白**：`showDiagnosticAt` 确实查了 local+lsp+external 三个列表并 `callTipShow`，问题是**从未设置 calltip 颜色**——深色主题下默认白底 + 继承的浅色前景，文字不可见。`CodeEditorService.cpp::configureIndicators` 末尾按 `dark` 设置 `callTipUseStyle(0)`/`callTipSetBack`/`callTipSetFore`/`callTipSetForeHlt`。
- **缺分号无波浪线**：`ShaderCodeEditor::SetDiagnostics` 把超出行内容的列 `qBound` 到 `lineEnd`（换行符位置），指示器画在无字形处不可见。改为 `start >= lineEnd && lineEnd > lineStart` 时退到 `lineEnd - 1`（最后一个可见字符），与主流 IDE 一致。
- 以上两项 + 之前的 F12 单引用直接跳转需重编 qrenderdoc：新增 `output/shader-workspace-20260906/REBUILD_QRENDERDOC.cmd`（只编 `qrenderdoc_local.vcxproj` Development x64 `/m:2 SkipReplayLabRuntime=true`，不编核心/pyd/cmd，跳过 i18n 门禁——无新增 C++ tr()；随后重部署 Bridge）。

**item 10（用户选 B）**：流程图 main → 阶段 → 输出，且内部节点标明输入/输出，用背景色区分。全部 Bridge：
- `project_shader_flow_hierarchy.py` 新增数据流推导：`file_scope()`（文件级全局/资源/cbuffer 成员，跟踪大括号+小括号深度、按语句起始词识别 `cbuffer … {`）、`struct_fields()`、`function_params()`、`span_io(start,end,params)`（token 区间内 `base.field` 读/写、swizzle 折叠到 base、资源单列、局部写单列；赋值判定覆盖单 token `= += -= *= /=` 与双 token `%= &= |= ^= <<= >>=` 及 `++/--`）、`function_io()`（函数级：参数+函数体，`out` 参数保底计入写；不含局部）、`entry_interface()`（结构体参数/返回值展开为字段与 semantic，如 `gl_FragCoord : SV_Position` / `SV_Target : SV_Target0`）。
- 节点：`_FunctionProjection._node` 为每个节点附 `io`（function-call 节点用被调函数的 `function_io`，语句节点用自身区间 `span_io`）；`build()` 新增 `_boundary_nodes()`：`Input · <fn>`（入口接口，无参数则用读取的全局/资源）与 `Output`（输出接口，void 则用写出的全局/字段），Input 锚在声明、Output 锚在右大括号，边：Input→无入边节点、无出边/return 节点→Output；`project_root()` 对 AI 接受图为阶段节点附 `function_io` 并 `_wrap_accepted_graph()` 同样加首尾节点（幂等）。
- 视图 `project_shader_flow_graph.py`：规范化放行 `io`（`_normalized_io` 有界），Input/Output 标签在视图侧本地化；节点内描述下方渲染 `in: … / res: … / out: …`（各最多 5 项 +n，语句节点的 out 含其定义的局部；边界节点不重复渲染）；`_NODE_TINTS` 按 kind 混入调色板 Base 色（input 蓝 / output 琥珀 / function-call 绿 / condition·loop 紫，强度 0.16，深浅主题均可读），左侧色条同色作图例。
- 单测：SPIRV-Cross 风格样例的 file_scope/params/struct_fields/function_io/entry_interface 全部符合预期；函数图首尾为 input/output 且边完整；AI 接受图包裹正确；原有 4 个 call-site 测试通过。

## 0k. 行尾波浪线加宽 + 节点内容重构 + item 3：AI 全自动跑通 ABI（2026-09-07）

**行尾波浪线**：用户"删 `;` 仍无波浪线，或者多夹几个字符"。C++ `ShaderCodeEditor::SetDiagnostics` 与 Bridge `_clamp_diagnostics_to_text` 都改为：诊断列落在行尾/超出时，回退覆盖**最后一个 token**（≤40 字符的字母数字/`_`/`()[].`），而不是只画 1 个字符。

**流程图节点内容**（Bridge `project_shader_flow_hierarchy.py` / `project_shader_flow_graph.py`）：去掉内部节点下方 in/out 小字（只保留 Input/Output 边界节点的接口说明）；节点标题 = 紧邻上方的 `//` 注释（无则一行代码摘录），描述 = 去缩进的 ≤3 行代码 + `...`；function-call 节点 = 调用点/被调函数上方注释 + `Call(args)` 一行；`leading_comment()` 新增。

**item 3（Cursor 反编译 EID 11402 卡在 ABI）根因与修复**：
- 现象链：反编译 → Host 固定修复 `repair_spirv_cross_hlsl_interface` 抛 "unsupported SPIRV-Cross input declaration tail" → 基线编译过但回放 ABI 不符 → `begin_shader_workspace_organization` 建了 session 目录后在基线门禁失败（只写 `failure.json`，无 `session.json`）→ 后续用该 id 报 "session.json 不是正规文件" → 诊断给出 `repair-source-within-captured-abi` 但**没有任何工具能让 AI 提交修好的源码**（`promote_shader_workspace_explanation` 只对无 source 的 workspace 有效，`write_shader_workspace_organization_file` 需要先 begin）→ AI 只能问用户。
- 固定修复加宽：`adapters/hlsl_interface_abi_repair.py::_signature` 的字段正则原只认 `(float|uint|int)[1-4]?`，SPIRV-Cross 对 PS 会输出 `bool gl_FrontFacing : SV_IsFrontFace;`（以及 half/min16float/precise 等），整个 struct 尾巴因此不匹配。现在：类型表 `_INPUT_TYPE_COMP_TYPES`（bool→uint/int，half/min16float/float16_t→half/float，double，min16int/uint，dword…）、限定词加 `precise`、struct 体内注释先抹空、错误信息带上未识别的那段文本。单测 +2（bool frontface/half/precise/注释；float4x4 报错文本）。
- **新 Host 工具 `repair_shader_workspace_source_abi(workspace_path, expected_source_sha256, source, acknowledge_semantic_equivalence_unverified)`**（`organization.py::ShaderOrganizationManager.repair_baseline_source`，server.py 注册）：AI 提交完整修复后的 HLSL。流程：校验文本/`#include|#line`/大小 → 取 `WorkspaceSourceTransactionLease`（有 watcher/live-edit/review 时拒绝）→ 源哈希 CAS 校验 → 首次前保存 `ai/decompiler-original.hlsl`（与 organization begin 同一规则，保证后续 begin 的溯源链）→ 先 `compile_check`（编不过直接返回行列诊断，**不碰源文件**）→ `_source_compare_and_swap` 换入 → 复用 `_compile_and_validate`（编译 + 回放反射 ABI）→ 通过则保留并返回 `nextTool=begin_shader_workspace_organization`；否则**换回原字节**并返回 `failure`/`evidence.abiComparison`/`baseline.compiledInterface`。每次尝试写 `ai/abi-repair-attempts/<id>.hlsl|.json` 回执。单测 4 项（拒绝并还原、编译检查拦截、接受后 begin 成功、旧 session 提示）。
- `diagnostics.py`：凡 `allowedActions` 含 `repair-source-within-captured-abi` 且 AI 可写源，附 `sourceRepairTool` 与 `autonomousNext`。
- `organization.py::_read_session_file`：session 目录只有 `failure.json` 时给出可执行的提示（"never opened: begin failed at baseline-compile-abi；先 repair 再 begin 取新 id"），替代 "不是正规文件"。
- **全 AI 路线统一行为**：`replaylab-core` SKILL v3 新增 §6 自主性（一轮内跑完 prepare→decompile→compile/ABI→repair→organize→review→register→apply；失败门禁是工作不是提问；只在缺 grant / 预算耗尽 / 请求真的留了不可逆选择时停）与 §7 用户可读回答（1–3 段对话式中文/用户语言：结果、做了什么及为何修复、失败/省略原因、至多一个下一步；不重复、不用标题/列表/表格/代码块/emoji/工具名/哈希/路径，除非用户要）。`renderdoc-shader-workbench` SKILL v14：步骤 5 的 ABI 失败改为"当轮自修最多 3 次"并给出典型修法（按捕获行序重声明 `SPIRV_Cross_Input`、`bool : SV_IsFrontFace` 反射为 uint、补齐未用输入、cbuffer 44→48 用尾部 pad 保持成员偏移）；步骤 6 说明 `organization-abi-failed` 回到修复通道；§3 禁止以"如何处理？"结束；§6 报告分为 Host 表单与用户摘要。`src/replaylab_agent/agent_prompts.py` 三个系统提示（vendor-native/Cursor/Codex/Claude ACP、API main、API child）同步：ask 规则限定为"请求留下的真实选择"，明确"是否继续/如何处理失败门禁"不是用户问题；Answer 段改为对话式摘要规则。六个角色的提示装配自检通过（core 只出现一次且含新工具名）。

未完成/待用户：仍未拿到 `D:\Study\抓帧\ReplayLab Project\workspaces\shaders\v2\MainPS-dxil-spirv-hlsl-abi-02` 的实际源码（文件夹授权对话框超时），bool/SV_IsFrontFace 是基于代码结构的高置信推断；即使固定修复仍不覆盖，新工具已让 AI 能自己修。部署：`REBUILD_HOST_AND_BRIDGE.cmd`（Host + agent + Bridge）与 `REBUILD_QRENDERDOC.cmd`（C++）。

## 0l. ABI 失败"哪里、为什么、怎么改"+ 固定修复分阶段（2026-09-07）

用户两点：反编译时就该拿捕获的 cbuffer 大小/偏移去对声明；Host 校验失败时要说清哪里失败、为什么、差异是什么，再交给 AI 修。

- **固定修复分阶段**（`workspace.py::_repair_spirv_cross_hlsl_from_dxil_evidence`）：原来 cbuffer 修复和接口修复在同一个 try 里，接口修复认不出一个字段就整体放弃，连已证明的 cbuffer 修复也丢掉（这正是 44→48 没被修的原因）。现在接口修复独立 try：成功 → `interfaceRepairVersion=1` + `interfaceRepair={"status":"applied"}`；失败 → 保留 cbuffer 修复，`interfaceRepair={"status":"not-applied","reason":"unsupported … tail: 'bool front : SV_IsFrontFace;'"}`。溯源校验 `_validated_hlsl_cbuffer_repair_provenance` 放行并校验这个可选字段（applied 必须配 version 1；not-applied 必须无 version 且 reason ≤4096）。状态里 `source.hlslCBufferAbiRepair.interfaceRepair` 直接告诉 AI 哪一条声明没修。
- **ABI 差异解释器**（新模块 `shader_workspace/abi_explain.py::explain_abi_differences(baseline, candidate)`）：原 `compare_shader_abi` 只给按数组下标的 path diff（少一行签名后面全部错位，AI 很难读）。解释器按稳定身份重新配对——签名按 `semanticIdxName`（+stream），cbuffer/资源按 `space:bind`，成员按 `byteOffset`——输出 findings：`missing-row / extra-row / moved-row / type-mismatch / system-value-mismatch / missing-block / size-mismatch / member-missing / member-type-mismatch / member-extra / missing-binding / binding-type-mismatch / extra-binding / changed`，每条含 `identity / captured / candidate / message / hint`（hint 是具体 HLSL 改法：补 `uint <name> : SV_IsFrontFace;`、按 regIndex 顺序重排、尾成员改 float3 使大小停在 44、`packoffset(c2.x)`、`StructuredBuffer<float2>` 代替 `Buffer<uint4>` 等）。opaque/ambiguous 布局只报大小。只在读取时派生、不入库、不影响报告哈希。
- **接入点**：`status.lastAbiAttempt.explanation`（`_current_rejected_abi_attempt`）；`validate_shader_workspace_candidate` 不兼容时返回 `explanation` + `sourceRepairTool`；`repair_shader_workspace_source_abi` 失败结果带 `evidence.abiExplanation` 与扁平的 `whatToFix[{where,why,how}]`；organization begin 的 ABI 失败改为 `_abi_failure_error`：错误文本追加摘要与首条 finding，异常携带 `abi_explanation`，`_shader_err` 输出 `abiExplanation` + `whatToFix`。
- **技能**：`renderdoc-shader-workbench` 步骤 4 新增"首次编译前先对声明"：看 `hlslCBufferAbiRepair.interfaceRepair`，为 not-applied 或 DXIL 路线无记录时，直接把 `SPIRV_Cross_Input/Output`、cbuffer、资源声明与 `baseline.compiledInterface` 对齐后走修复通道，省掉编译→失败→修一轮；步骤 5 说明 `whatToFix` 的三个字段并要求逐条处理。
- 单测：`test_abi_explain.py` 3 项；`test_shader_abi` 31 项、organization 58 项、repair 4 项通过；routes 全套在跑。

## 0m. 解包资产进入固定修复（Host 侧）+ 智能匹配设计（2026-09-07）

用户决定把"捕获反射 → 解包资产 → 反编译代码"的顺序做进 Host，并指出：资产匹配不能只靠哈希强校验（不同引擎格式不同、常常对不上），应当是检索器 + AI 的智能匹配。参考资产：`E:\SilverPalace_Unpacked`（与现有 rdc 同一游戏，UE5，已授权读取）。

**已做（Host）：**
- `shader_workspace/asset_layout.py`：`renderdoc-ultra-asset-cbuffer-layout-hint/v1`（按 `"space:bind"` 给出资产声明的成员布局 + relation 溯源），`normalize_layout_hint`、`hint_from_asset_blocks`、`hint_from_relation_view`（候选优先级：程序同一性 > layoutHash 相等且元数据可信 > AI/规则已选中的匹配）。
- `adapters/hlsl_cbuffer_abi_repair.py`：`repair_spirv_cross_hlsl_cbuffers(..., asset_layout=None)`。命名抽象为 `_LaneNaming`（原行为）与 `_AssetNaming`：块字节数必须等于捕获值、每个 DXIL 访问 lane 必须落在资产成员内且标量类别一致（f32↔float/half，i32↔int/uint/bool）、成员名不得与 HLSL 已有标识符冲突、矩阵/结构体成员不支持——任一不满足即该块回退 lane 命名并在 `RepairedCBuffer.asset_fallback_reason` 说明原因（如"asset block byte size differs from the captured block"= 资产版本不对）。通过时声明就是资产的真实成员（含代码未读的成员），引用改写为 `BaseColor.xyz`/`asfloat(BlendMode)` 等。
- `workspace.py`：`ShaderWorkspaceService(asset_layout_provider=…)`；`_asset_layout_hint` 取 hint 后**固定副本**写入 `source-inputs/asset-cbuffer-layout-<sha>.json`；记录 `hlslCBufferAbiRepair.assetLayout{path,sha256,relation}`（仅在真的用到时）；`repairedBlocks[]` 新增 `namingSource/assetMemberCount/assetFallbackReason`（lane 且无原因时省略，兼容旧记录）；溯源重算 `_validated_hlsl_cbuffer_repair_provenance` 从固定副本读 hint 并要求重算结果确实使用了它。
- `server.py`：`_workspace_asset_layout_provider(manifest)` 只读活动项目的 `symbols-v3.sqlite3`（前置：资产根已配置可用、库中有该 capture shader、rawSha256 一致），经 `_shader_asset_relation_view` → `hint_from_relation_view`；任何缺失都返回 None，不阻塞反编译。
- 单测 `test_hlsl_cbuffer_asset_naming.py` 4 项（命名与未用成员保留、错版本/尺寸/类型/重名回退原因、无 hint 结果逐字节一致、relation view 选择）。

**现状诊断（为什么哈希在这个游戏上永远对不上）：** SilverPalace 是 UE5（Substrate 配置），shader 在 `Content/ShaderArchive-*.ushaderbytecode`，`r.Shaders.CompressionFormat=2`（Oodle）。对 Global-SM6 归档前 40MB 跑 Host 的 `_find_dxbc_containers` 结果为 **0 个有效容器**——扫描器索引不到任何 shader，symbol DB 为空，所有基于 raw/code/layout hash 的匹配都不可能触发。真正的"完整内容"分布在：(1) 归档（需 Oodle 解压，游戏自带 oo2core DLL）；(2) cooked 材质 `.uexp`——实测 `M_CharacterPBR.uexp` 含 `FMaterialShaderMapContent`、`FMaterialNumericParameterInfo`、`FMaterialUniformPreshaderField`（= Material cbuffer 的成员布局与参数名）、`FMaterialTextureParameterInfo`、shader 类型名（`FDepthOnlyPS/FOutlinePS/FLumenCardPS/FAnisotropyPS`…）、顶点工厂名；(3) 引擎固定 UB（View/Primitive/Scene）按 UE 版本的已知布局；(4) `analysis/inventory.db` 是 UEX 报告库 schema（assets/relations 含 material→texture 的 slot/parameter），当前为空。

**智能匹配设计（待做，需用户确认数据源）：**
1. 捕获侧特征：stage、pass 分类、入口名、cbuffer 大小向量、纹理/采样器数量、**绑定纹理的调试名**（UE 会 SetName 为资产名）、RT 名、输入布局（骨骼权重→Skeletal）、shader model。
2. 资产侧"材质文档"：材质名/父材质、domain/blend/shading model、纹理参数名 + 引用的纹理资产名、标量/向量参数名（有序）、shader 类型集合、顶点工厂、`bUsedWithSkeletalMesh` 等（来源：UEX inventory/material_json 和/或 Host 自带的 cooked 材质读取器）。
3. 检索打分（不需要哈希）：纹理名重叠（IDF 加权 Jaccard）、参数数量 ↔ Material cbuffer 大小（按 UE 打包规则预测）、pass ↔ shader 类型一致性、网格类型一致性、名字 token 相似度；输出 top-k 与逐信号证据。
4. AI 裁决：复用 `get_symbol_match_cases`/`submit_symbol_match`，决策落为该游戏 build 的规则，跨 shader/capture 复用；选中后写入 asset_shaders 元数据 → 上面的 hint provider 自动生效。
5. 引擎 UB 字典（View/Primitive…）按 UE 版本，首次由 AI 确认后成为规则。
哈希同一性保留为 tier 0（若某 provider 能解压归档则直接精确链接）。

## 0n. 智能资产匹配 v1：引擎感知目录 + 检索器 + 决策记忆（2026-09-07）

用户进一步要求：不只 UE，任何引擎的解包都要能正确读取；用 AI 找到对应资产文件；匹配成功后建立查找表避免重复搜索。选择"Host 自带读取器 + UEX 关系并用"。

**已做：**
- `symbols/engines/`：`detect_engine(relative_paths)`（按文件名判定 unreal/unity/godot/unknown，UE 时解析 `/Game`、`/Engine`、`/<Plugin>` 挂载 → 目录相对路径）；`parse_engine_asset(context, relative, data)` 分发。
- `symbols/engines/unreal_package.py`：只读 UE 4.2x–5.5 稳定的包头/名字表/导入表/导出表（不解 tagged property、不解冻结 shader map、不解 bulk）：主导出类名、导入的纹理/父材质/材质函数/参数集合/网格（对象路径经挂载映射为目录内 `.uasset` 相对路径 → 目录关系 `uses-texture`/`parent-material`/…，SilverPalace 实测 200/200 解析成功）、"有意义的名字"（参数名等，过滤属性类型/引擎样板）。`.uexp` 做字符串扫描：shader 类型（`FDepthOnlyPS`、`TBasePassPSFNoLightMapPolicy`…）、顶点工厂（`FGPUSkinVertexFactoryShaderParameters`）、宏、是否含内联 shader map。每文件约 10ms。
- `asset_catalog.py`：新 kind `package`（.uasset/.umap/.uexp）；扫描前先做一次只看文件名的引擎探测，引擎与挂载写入目录 metadata；`PARSER_REVISION` 进入每条记录，增量扫描对旧解析版本重新解析；新增 FTS5 令牌表 `asset_tokens`（路径词、标识符、驼峰拆分、纹理名、shader 类型…，无 FTS5 时退化为 LIKE）与 `search_tokens(tokens, kinds, class_names, limit)`（IDF 权重：稀有令牌分高；返回每个候选命中的令牌）；`status()` 报告 `engine`/`tokenIndex`。
- `shader_asset_retrieval.py`：`texture_name_tokens`（RenderDoc 纹理调试名 → 去前后缀的检索令牌，过滤 white/default/SceneDepth 等泛名）；`retrieve(catalog, features, top_k)`：令牌检索 → 候选文档（类、名字、纹理、父材质、shader 类型、宏）→ 一致性检查（stage 有对应 PS/VS、pass 提示 ↔ shader 类型、skeletal ↔ GPUSkin 工厂、纹理数量合理）→ `adjustedScore` 与 `why`、`decisionHint`（clear leader / ambiguous）。`ShaderAssetResolutionStore`（`shared/dictionaries/assets/shader-asset-resolutions-v1.sqlite3`）：按 `(gameKey, rawSha256, stage, entry)` 记正/负决策，按 `structuralKey`（stage + cbuffer 大小集合 + 纹理/采样器/UAV 数 + 入口）给同形状建议；`lookup/record/forget`。
- server 工具：`retrieve_shader_asset_candidates(event_id, stage, top_k, pass_hint, mesh_hint, extra_tokens, force_search)`（先查记忆，命中直接返回 `resolvedDocument`；否则检索）、`submit_shader_asset_resolution(event_id, stage, asset_id, confidence, rationale, reject)`、`forget_shader_asset_resolution`。特征来自活动目录选中的 shader（raw hash、ABI 的 cbuffer 大小）与 Bridge `get_bound_textures` 的 `texName`。gameKey = `scope.gameId@buildId`（未配置则资产根哈希）。
- 技能：`renderdoc-shader-workbench` v15 步骤 4(b) 加入检索通道与记忆流程；`renderdoc-unpacked-asset-intelligence` v4 的 Capture correlation 说明检索器与记忆。
- 单测：`test_shader_asset_retrieval.py` 4 项（合成 UE5.4 包：包头/名字/导入/挂载/令牌；.uexp 字符串；目录扫描的引擎/关系/FTS；纹理名驱动检索 + 记忆命中/否决/同形状建议/遗忘）；`test_asset_catalog` 6 项仍通过。实测 SilverPalace 角色材质子树：绑定纹理 `T_Crow_Body_D/N + T_Matcap` → `M_CharacterPBR_Crow` 为 clear leader，四项一致性全部吻合。

**下一步（未做）：** 引擎 UB 字典（View/Primitive 按 UE 版本）与"AI 确认一次即成规则"；把已解决的材质文档接进 organization 的 `cbufferAliases`/hint provider（参数名 ↔ 成员由 AI 按用法对齐；若 UEX/CUE4Parse 提供 preshader 布局则可直接给偏移）；Unity/Godot 专用提取器（目前靠通用文本/路径令牌）；`.ushaderbytecode` 解压 provider（需游戏 oo2core）以获得 tier-0 哈希同一性。

## 0o. 按需索引、分层深度、置信度与局部匹配（2026-09-07）

用户要求：配置解包目录时不要立刻索引；AI 反编译/分析 EID 需要资产时自动触发查找与分析，并记录正确匹配；没有强规则，靠 AI 结合参数、cbuffer、纹理数量、甚至纹理内容比对来匹配；要有置信度，部分匹配（如只有纹理或部分函数对上）也应作为反编译、cbuffer 别名、整理 HLSL 的参考，而不是因为一小部分不匹配就全部丢弃。

- `asset_catalog.scan(depth=full|headers|names, scope_prefixes=[...])`：`names` 只遍历（路径令牌，秒级）；`headers` 只哈希+解析小的描述性文件（包头 ≤2MB、材质/着色器/元数据、材质类的 `.uexp` ≤8MB——同目录 `.uasset` 先于 `.uexp` 排序，用 `material_stems` 判定）并把 bulk/大文件记为 `deferred`（hashStatus/parseStatus=deferred，仍进令牌索引）；`full` 保持原行为。`scope_prefixes` 只对前缀内的文件做深度工作，其余复用旧记录或 deferred。`hydrate(root, relative_paths)` = 增量 + full + scope，用于按需补齐候选。`resolve` 对未哈希资产给出明确拒绝。
- server：`index_active_unpacked_assets(depth="headers")` 默认改为 headers；新增 `_ensure_shader_asset_catalog()`：`retrieve_shader_asset_candidates` 首次调用时若无目录（或目录早于令牌/引擎解析）自动做 headers 索引，结果里 `indexing` 回执说明动作与耗时；候选的 `.uexp` 若为 deferred 则自动 hydrate（≤16 个）后重新检索。`configure_active_project_asset_root` 不触发索引。
- 置信度与局部匹配（`shader_asset_retrieval.retrieve`）：每个候选 `confidence`（令牌 IDF 覆盖率 + 一致性比例 + 领先度 + 唯一领先加成；纹理 facet <0.5 时封顶 0.75，整体上限 0.95）与 `facets`：`textures.explained/unexplained`（绑定纹理名是否出现在该材质或其父材质的纹理引用里）、`shaderTypes.agrees/disagrees`、`names.available`；文档新增 `parent`（父材质名字/纹理）、`instanceCount/instances`（反向关系，`AssetCatalogService.referrers`）、`exportParseStatus`。`policy.partialEvidenceUsable=true`。
- 记忆：`lookup` 返回全部正决策 `resolutions`（实例 + 父材质 + 共享函数可并存）；`submit_shader_asset_resolution(..., facets_json)` 记录分面置信度。
- 技能 `renderdoc-shader-workbench` v16：无需预先索引；"局部匹配是证据不是失败"规则；分面置信度进入推断命名的置信度；允许多条正决策。
- 待做：纹理内容比对（RenderDoc `get_texture_data` 最小 mip 的感知哈希 ↔ 资产侧解码；UE 需 BC 解码器，PNG/DDS 引擎可直接做）；Unity/Godot 提取器；引擎 UB 字典；与 organization 命名的自动衔接。

## 0p. 数值匹配、失败说明、成员规则记忆（2026-09-07）

用户三点：解析失败（加密等）要说明原因；用 cbuffer 数值与资产数值比对（复杂数字 + 相邻若干个都对上 ≈ 确定），由此顺着关联资产扩展，纹理同理（抽像素）；每一步给 AI 足够说明；并做"AI 确认一次的参数↔成员对应落成该游戏的规则"。

- **Bridge** 新增 IPC `get_constant_buffer_values(event_id, stage)`：按反射的每个常量块取绑定缓冲原始字节（`GetBufferData`，≤64KB），返回 float32/uint32 lane 数组、bind/space/byteSize、缓冲名；无绑定/失败按块给 `reason`。
- **资产侧数值指纹**（`unreal_package.numeric_fingerprints`）：对材质类 `.uexp` 前 2MB 按 4 种字节对齐扫 float32，`is_distinctive_value`（有限、1e-4~1e6、不是 3 位小数以内的"圆"数）过滤，重复 >32 次视为填充剔除；产出 `numericValues`（`%.7g` 键，≤512）与 `numericRuns`（连续可辨值的串，含偏移）。文本资产（Unity YAML/.mat/.mtl）用 `_text_numeric_values` 提取 ≥4 位小数的字面量并按 float32 归一。目录新表 `asset_values(value_key, asset_id)`；`AssetCatalogService.search_values(values)` 返回命中键、键流行度与命中的 runs。
- **检索融合**（`shader_asset_retrieval.value_evidence` + `retrieve`）：Host 侧 `_shader_asset_capture_features` 从 Bridge 取 lanes（只留可辨值，≤1024，含 `constantBufferValueStatus` 说明）；`.uexp` 命中映射到所属 `.uasset`；`rareMatchedCount`（流行度 ≤3 的值）、`adjacentRuns`（资产连续值 ↔ cbuffer 连续 lane）；强度 = rare + 2×adjacency，置信 = 1−0.5^强度（上限 0.98；只命中流行值封顶 0.2）；仅靠数值命中的资产也会加入候选；`adjustedScore` 加成 3×rare + 4×adjacency；candidate 的 `valueEvidence` / `facets.values`（含 `laneRefs`、`viaParent`）与 `why` 文本。无纹理名但有 lanes 时照样检索（合成测试：3 个连续复杂值 → 置信 ≥0.9，单个 0.5 → 无候选）。
- **失败/覆盖说明**：`AssetCatalogService.coverage()`：按 kind×parseStatus 计数、解析失败原因 Top（含示例路径）、仍打包的归档、deferred 数、引擎信息，生成 `limitations` 文案（"N 个 .ushaderbytecode 为压缩归档，哈希同一性不可用，改用名字/关系/数值"、"N 个文件解析失败：package tag mismatch（可能加密/仍压缩/IoStore 缺上下文）…"）。`retrieve` 结果带 `coverage`、`limitations`，每个候选带 `parseIssues`。UE 包头 tag 不匹配时错误信息给出可能原因。
- **成员规则记忆**：`aliases_from_renames` 输出带 bind/space/byteSize/original；`_publish_cbuffer_aliases` 返回 `memberRenames`；server `rename_shader_workspace_symbols` 之后 `_remember_member_rules`：若该 shader 已有资产决策，把成员重命名按 `(gameKey, familyKey, space, bind, byteOffset)` 写入 `member_rules`（familyKey = 决策资产路径 + 其父材质路径；高置信覆盖低置信）；结果 `memberRules` 说明记录情况或为何没记。hint provider `_workspace_asset_layout_provider` 先查记忆（`_shader_asset_memory` → 家族规则 → 用捕获 `compiledInterface.constantBlocks` 的 byteSize 组成 hint，`relation.retriever="member-rules"`），再回落 symbol DB。
- **局部资产命名**：`_AssetNaming(allow_partial=True)`：资产/规则未覆盖的访问 lane 生成 `__replaylab_*` 合成成员，其余按资产名声明，`naming_source="asset-partial"`；不再因一个 lane 无对应而整块放弃。
- 技能 v17：步骤 2b（有资产根时反编译前先检索/提交，让固定修复带上真实名字；读 `limitations`）；"数值证据优先于名字"；"你确认的名字会成为规则"。
- 单测：retrieval 8 项、asset naming 9 项、catalog 6 项、organization 58 项、repair 4 项通过。未做：纹理内容比对（需 RenderDoc 读像素 + 资产侧解码；UE 需 BC 解码器）。

## 0q. 纹理内容比对（2026-09-07）

用户：数值匹配作为额外补充；继续做纹理内容比对（要考虑压缩格式、镜像等，用快速的开源式算法判断"是不是同一张"），也是高价值信号；这些都由 Host 快速处理以省 token；但 AI 仍要自己检索，不能完全依赖。

- `symbols/imaging.py`（纯 Python，冻结 Host 无第三方依赖）：BC1/BC2/BC3/BC4/BC5 块解码、B8G8R8A8/R8G8B8A8/G8/R8/A8/R8G8/G16、PNG（8 位、非交错）与 DDS（fourcc/DX10）解码；`gray_thumbnail` 16×16 亮度缩略图（盒滤波，支持上采样）+ 均色；`dhash`（9×8 差分哈希 64 位）在 8 个二面体变换（镜像/旋转）上取最小汉明距离，≤6 视为同图、≤12 可疑；`flat` 标记纯色图（能匹配一切，不作身份证据）。`symbols/bc7.py`：BC7 全 8 模式解码（分区表/锚点/权重按 D3D11 规范）。用 SilverPalace 真实纹理做自洽验证：同一纹理 mip32 与 mip16 的缩略图哈希距离 0–5，BC7 与 DXT1/DXT5 分布一致、均色一致 → 解码正确。
- `symbols/engines/unreal_texture.py`：在 Texture2D 的 `.uexp` 中定位 `FTexturePlatformData`（PF_ 像素格式串 → 前 12 字节 SizeX/SizeY/PackedData），遍历 mip 表——UE5.3+ 的"数据资源索引 + 内联载荷"样式与旧式 `FByteBulkData` 头都支持，通过"下一组 (SizeX,SizeY,1) 是否等于预期尺寸"对齐——选 8~128px 间最接近 32px 的内联 mip 解码指纹（~1ms）；.ubulk 中的大 mip 不读。150 个真实纹理样本：DXT1 56、DXT5 53、BC5 22、BC7 15、G8 4 全部成功。不可解码格式（ASTC/浮点）按名给出原因。
- 目录：`.uexp` 依同目录先解析的 `.uasset` 类（`stem_classes`）判定纹理导出，header 深度即指纹化（不做数值扫描，避免像素噪声）；PNG/DDS 文件 ≤2MB 直接指纹化；新表 `asset_thumbnails`（8 个变换哈希 + 均色 + 尺寸 + flat + 格式）；`search_thumbnails(dhash, mean_color)` 全表汉明比较（4 万纹理级别 <1s），均色差 >96 的"精确命中"降级为 plausible（亮度哈希分不清通道互换）。`coverage()` 报告 `thumbnailRows` 与未指纹化纹理的原因分布。`PARSER_REVISION=3`。
- Bridge 新增 IPC `get_texture_fingerprints(event_id, resource_ids ≤32)`：选最大边最接近 32（≤64）的 mip，用 RenderDoc `SaveTexture` 转 PNG（任何 GPU 格式含 BC7/浮点都由 RenderDoc 转换），base64 回传（≤256KB），逐纹理给失败原因。Host 侧用自己的 PNG 解码算指纹。
- 检索融合：`texture_identity_evidence`：每个绑定纹理 → 命中的资产（距离、变换、verdict、被多少材质引用）→ 经 `uses-texture` 反向关系归到材质；`facets.textureIdentity`（sameImage/plausible/textures/confidence，父材质命中标 viaParent）；`adjustedScore` +6/同图 +2/可疑；仅靠像素命中的材质也进候选；数值与像素同时 ≥0.75 时置信 0.97；`signals.textureIdentity` 列出每张绑定纹理的结论或原因（纯色、快照失败、无命中）。`policy.aiMustStillSearch=true`。
- 技能 v18："内容同一性是最强的廉价信号"；"这些 Host 信号是省 token 的补充，不替代 AI 自己的检索"。
- 单测 `test_texture_identity.py` 4 项（BC1/DDS、PNG 往返 + 镜像/缩放不变、合成 UE 纹理导出解析、目录扫描指纹 + 镜像捕获匹配 + 仅像素检索）；retrieval 8、catalog 6 通过。

## 0r. 通用优先：引擎无关的内容探针（2026-09-07）

用户问为什么要分 UE/Unity 专用提取器，希望通用提取 + 识别到引擎时的专项增强。结构改为 **通用层是底座、引擎层只加精度**：
- `symbols/generic_probe.py`：对任何文件（前 4MB）提取：标识符字符串（ASCII + UTF-16LE，频次排序，≤384）、路径样字符串、可辨数值指纹（复用 `numeric_fingerprints`，仅 ≤1MB 的文件——大文件多为几何/动画）、内嵌图片（PNG/DDS 魔数定位 → 指纹，≤512²）、内嵌 shader 容器计数（DXBC/SPIR-V/MetalLib/#version）；`mention_references`：文件内字符串等于目录中另一文件的 stem（≥6 字符、全树唯一、同目录 .uasset/.uexp 视为一体）→ `mentions-asset` 启发式关系（confidence 0.5、标 heuristic）。
- 目录：`EngineContext.stem_index` 在文件名预扫描时建好；`_parse_asset` 对 other/binary/未被引擎解析的 package/非文本 metadata 走通用探针；header 深度也解析 ≤512KB 的 other/binary/metadata（Unity .asset/.prefab、自定义描述文件）；`asset_tokens` 纳入 `strings`/`pathReferences`。
- 检索：类过滤只在引擎为 unreal 时默认启用（其它引擎没有 className），kinds 覆盖 other/binary/scene/mesh；未知引擎合成测试（二进制材质 blob + PNG 纹理）：名字令牌、数值相邻串、mentions 关系、纹理指纹全部生效。
- 数值置信收敛：单个孤立命中在大目录里可能是巧合（网格/动画浮点也"罕见"），改为 strength = (rare−1) + 2×adjacency；单命中 0.25、两个 0.5、三个 0.75、相邻串主导；UE 导出仅对材质/函数/参数集合类做数值扫描。
- 技能 `renderdoc-unpacked-asset-intelligence` v5 说明通用层/引擎层关系。单测 19 项通过。

## 0s. 结构/数值/像素/网格/代码逻辑优先，名字只作参考；后台索引；打包前状态（2026-09-07）

用户：匹配主要靠参数结构、参数数值、纹理同一性、网格、代码逻辑结构（Host 查找 + AI 查询）；名字在真实抓帧里通常不存在，只能参考；引擎专项可选，主要靠 AI 与 Host 协作（Host 给调试信息，AI 调方向）；接下来打包找一个 EID 测试，要求内置 AI 自动跑完整条链路。

- Bridge 新增 `get_draw_geometry(event_id)`：索引/顶点/实例数、拓扑、已用顶点输入（semantic/format/slot/perInstance）、顶点/索引缓冲身份与名字、`meshHint`（BLENDINDICES/WEIGHT→skeletal，perInstance→instanced，否则 static）。
- server 特征新增 `drawGeometry`（自动补 `mesh_hint`）与 `codeHints`（若该 shader 的 workspace 已存在：`usesDiscard`、`textureSampleCount`、`usesDerivatives`、`outputTargetCount`）。
- UE 提取：非材质包（网格/蓝图）导入的材质记为 `uses-material`（材质实例的才是 `parent-material`），元数据 `usesMaterials`；文档新增 `usedByMeshes/usedByMeshCount`（反向关系）。
- 一致性检查新增：`blend-mode`（discard ↔ 资产名含 BLEND_Masked/OpacityMaskClipValue 或宏 MATERIALBLENDING_MASKED；discard 却只有 Translucent/Additive → 矛盾）、`sample-count-plausible`、`usage-flag`（skeletal ↔ bUsedWithSkeletalMesh/GPUSkin）。
- 名字降权：仅名字命中（纹理未被解释或根本没有纹理名）置信上限 0.6。
- 结构记忆进候选：`memory.sameShapeSuggestions`（同 cbuffer 大小集合/纹理数的已决 shader）在 server 侧合并为 `facets.structure` 与 `retrieval.structureSuggestions`，加分并抬置信。
- `retrieval.debug`：逐通道（names/texturePixels/cbufferValues/geometry/codeHints）说明可观测与失败原因，`nextSteps` 给 AI 下一步（无名字→靠像素/数值/结构；无指纹命中→查 coverage.limitations；无可辨数值→数值通道失效；无候选→自己 query_active_unpacked_assets；候选接近→比父链/usedByMeshes/blend-mode；workspace 尚无→prepare 后再调用以获得 codeHints）。
- 无资产根：`retrieve_shader_asset_candidates` 返回 `ready:false` + 原因，而非错误。
- **后台索引**：首次使用时的 headers 扫描放到后台线程（`_asset_index_job`，带 progress 回调：walked/hashed/reused/deferred），前台最多等 25s；未完成则返回 `ready:false, indexing:{status:running, elapsedSeconds, progress, retryAfterSeconds:30}`，AI 继续后续步骤、命名前重试（技能 v20 已写明），避免 MCP 客户端超时。
- 单测 28 项通过。**打包前检查清单**：`REBUILD_HOST_AND_BRIDGE.cmd`（Host+agent+Bridge，含新 IPC）→（可选）`REBUILD_QRENDERDOC.cmd`（早前 C++ 改动）→ 配置活动项目 `configure_active_replay_project`（gameId/buildId）与 `configure_active_project_asset_root(E:\SilverPalace_Unpacked)`（不索引）→ 选一个 EID 让内置 AI"反编译并整理"。预期链路：context → workspace path → retrieve（首次触发后台索引，返回 running）→ prepare/decompile（固定修复）→ 声明对照/ABI 修复通道 → compile/validate → retrieve 重试 → submit 决策 → begin organization → rename（成员规则落库）→ review → register → apply → live edit → report + 对话式总结。


## 0t. 2026-09-07 运行时故障定位与"常驻 Host 统一"决策（未实施）

- 现象：内置 AI（Cursor ACP 连接）`ConnectorError: ACP session/new timed out`（60 s 预算，`acp_session.py`）。
- 探针 `output/shader-workspace-20260906/PROBE_MCP_ROUTE.cmd` 结果：冻结 `ReplayLabMCP.exe --help` 0.8 s，源代理→冻结 Host initialize 1.8 s / list_tools 1.9 s，144 工具与 `policy.py` 清单一致。**MCP 路线不是原因**，超时在 Cursor 自身的 `session/new`。探针第二阶段现已改为自动测试全部 `*-acp` 连接（Cursor 优先），分别测"不带 / 带 ReplayLab MCP"两种 `session/new` 并打印 cursor-agent stderr；等待用户回传。
- 架构现状：qrenderdoc 启动时拉起 `ReplayLabInternal.exe`（私有服务）和 `ReplayLabMCP.exe ensure`（全机回环单例，供外部 IDE）；**内置 AI 不用该单例**，而是由厂商进程在 `session/new` 时启动 `replaylab_agent.mcp_proxy`，代理再 spawn 任务私有的 `ReplayLabMCP.exe stdio`，任务上下文（`REPLAYLAB_TASK_ID`/`REPLAYLAB_TASK_EVENT_ROOT`、qrenderdoc 实例 pin、live-edit 注册表、`REPLAYLAB_TOOL_SCOPE`）经进程环境变量传入。
- 用户决策：**先修 Cursor、先完成 SilverPalace EID 端到端测试，之后再做"常驻 Host 统一"重构。** 重构方案要点（已与用户确认方向）：每个 qrenderdoc 进程一个常驻 Host（项目打开即就绪），内置 AI / 外部 IDE / 外部 agent 全部经代理连接同一 Host；工具调用记录移入代理；任务上下文改为请求头按会话传递，Host 从当前请求上下文读取而非环境变量；后台资产索引、工具链与目录缓存、工作区监听随 Host 常驻；需补每次调用的取消/超时。改动面：`replaylab_host.py`（按实例 serve）、`server.py`（请求级上下文）、`mcp_proxy.py`（连接模式）、`renderdoc_mcp_bridge`（按实例拉起）、`native_mcp.py`（URL 路由）、打包脚本、文档，随后重新冻结。

- 第二次探针（全部 ACP 连接）：Cursor `session/new` 不带 MCP 5.3 s、带 MCP 7.1 s；Claude Code ACP 2.0 s / 0.5 s。**本地与厂商侧当前都正常**，20:15/20:17 的两次超时是瞬时故障（Cursor 后端/网络/首启自更新），日志（`agent-core.log`）在 02:12–20:15 之间无任何记录，无法进一步归因。
- 加固（源码 Agent，无需重新冻结）：`acp_session.py` 的 `open()` 改为按 `_ACP_SESSION_NEW_TIMEOUT=60 s` 首试，`initialize`/`session/new` 超时则用**全新厂商进程**重试一次（`_ACP_SESSION_NEW_RETRY_TIMEOUT=90 s`）；两次都超时时先跑 `_diagnose_local_mcp_route`（20 s 内 initialize+list_tools 本地 ReplayLab MCP 路线），错误信息明确指出是"本地路线健康→厂商侧停滞"还是"本地路线失败：<真实错误>"。测试 `tests/test_agent_acp_session.py` +3（19 通过，在云容器 Python 3.11 + pydantic/mcp 下运行）。
- 观察：cursor-agent.cmd 正常关闭后 node 子进程仍短暂持有工作目录（探针清理 WinError 32），与 `_force_stop_agent_process_tree` 文档描述一致，非本次故障原因。


## 0u. 2026-09-07 EID 11402 复测复盘 → 五项改造（已实现，待重新冻结 + 复测）

复测事实（`ai-tasks/a925…` 8.5 min/24 调用，`5186…` 29 min/31 调用 + 3 个 Cursor 子 agent 共 15.5 min）：RenoDX 拒绝 `Unexpected dynamic cbuffer offset` → 候选 1（dxil-spirv+SPIRV-Cross）成功；第一次工作区建在 `%LOCALAPPDATA%` 导致注册被拒、第二次在工程内重跑整条链；live-edit 持久授权 8 个满；组织评审 3 轮全被模板格式错误吃掉，用尽后 AI 又连写 5 次；资产索引在任务进程内后台跑、随进程死亡；344 KB HLSL 被分块读 8 次 + 子 agent 啃读。

1. **路线记忆**（`shader_workspace/route_memory.py`，`shared/dictionaries/shaders/route-memory.json`）：key=(api|encoding|stage|profile|editable)；`apply_shader_workspace_shared_resource` 成功 → `record_success`；`decompile_shader_workspace` / `try_shader_workspace_recovery_candidate` 失败 → `record_decompile_failure`；`ShaderWorkspaceService(route_memory_provider=…)` 在 `create()` 自动选路时 `prefer()`（成功优先 > 跳过已知失败 > 目录默认），manifest/返回值含 `routeMemory`；`plan_shader_recovery` 也返回 `routeMemory`。测试 `test_shader_route_memory.py`（7）。
2. **工作区位置 + 授权回收**：`server._enforce_active_project_workspace_location`（create/prepare[无 binding 时]/recovery attempt 三处，`ShaderWorkspaceLocationError` 给出正确路径）；`shader_live_edit._PersistentLiveEditRegistry.reclaim_for(identity, protected_keys)`（dead → 同物理 shader 旧尝试 → 其他捕获最久未用），`_start` 在 `authorize_for_start` 前调用，结果含 `reclaimedAuthorizations`。测试 `test_shader_live_edit_reclaim.py`（6）。
3. **评审预算**：`equivalence.ReviewRounds` 新增 `templateErrors`（独立计数，limit=max(6,3×base)）、`extraRounds`/`grants`（用户授予，≤3 次/≤9 轮）、`userSoftPublishAllowed`；`build_verdict(workspace_path=…)` 用尽时附 `userDecision{required, question, options[extend|publish-baseline|stop], tool, toolArguments}`；`organization.review` 在用户选择 publish-baseline 时 `_waive_blockers` 并 soft-accept；`server.write_shader_workspace_organization_file` 模板错误走 `record_template_error`（不耗内容轮次），用尽后 `_ReviewBudgetStop` 不再计数；新工具 `decide_shader_organization_review_budget(workspace_path, decision, user_confirmation, extra_rounds=3)`；模板错误信息列出缺失/多余键。Agent：`policy.py` 登记；`agent_prompts.py` ask 规则加"userDecision.required 是唯一例外"。测试 `test_shader_equivalence.py` +3，`test_shader_template_contract.py` 改为新契约。
4. **资产索引持久化**：`asset_catalog.scan(checkpoint_seconds=90)` 周期性发布部分目录（metadata `complete=false`+`progressJson`，未走到的文件沿用上次记录，覆盖不回退），`status()` 返回 `complete`/`progress`；`asset_index_jobs.py`（跨进程通用 job 表）；`ReplayLabInternal` 新路由 `POST /replaylab/asset-index`（`ensure|status`，不接受任何路径，自己解析活动工程），health capabilities 增加 `asset-index`；`asset_index_client.py`；`server._asset_index_job` 优先内部服务、不可达时回退进程内；`_ensure_shader_asset_catalog` 对 checkpoint 目录返回 `partial:true` 可用结果；`get_active_unpacked_asset_index_status` 增加 `backgroundJob`。测试 `test_asset_catalog.py` +1，`test_asset_index_jobs.py`（4）。**需要重启 qrenderdoc 才有新的 ReplayLabInternal；否则自动回退旧行为。**
5. **大 shader 结构化视图**：`shader_workspace/outline.py` + 工具 `get_shader_workspace_outline(workspace_path, relative_path, function, lines)`（声明/函数/调用图/采样点/成员使用点/匿名度 + 按函数或行段切片，≤400 行）；policy 只读清单登记。测试 `test_shader_outline.py`（2）。技能：`renderdoc-shader-workbench` v21、`replaylab-core` v4、`renderdoc-unpacked-asset-intelligence` v6（路线记忆、位置、授权、预算提问、outline 优先、禁子 agent/grep 读 Host 文件、索引进行中不放弃资产线）。

验证：云容器（Py3.11 + pydantic/mcp）跑 Host 相关 223 unittest + 61 pytest 风格（shim）全部通过；Agent `test_agent_acp_session` 19 通过、`test_native_engine` 13 通过（需 PATH 上有 claude-agent-acp）；`test_task_mcp_proxy` 中一个用例因容器 `sys.executable` 是符号链接被代理拒绝（环境问题）；Host 工具 146 个，policy 清单模拟校验通过。

下一步：`REBUILD_HOST_AND_BRIDGE.cmd` 重新冻结（Host + Internal），重启 qrenderdoc，再对 EID 11402 复测；观察 `routeMemory.applied`、工作区路径、`reclaimedAuthorizations`、`userDecision` 提问、`partialCatalog`、outline 使用与总 token。

- 追加（同日）：**路线记忆按物理特征分键**——`route_memory.dxil_route_features(ll)` 从 DXIL 反汇编识别动态下标的 cbuffer 访问（`cbufferLoadLegacy/cbufferLoad` 第三操作数为 `%值`），签名 `dyncb|static|nocb` 进入 route key；`ShaderWorkspaceService.create()` 在 staging 中先跑一次专用反汇编探针（`_probe_route_features`，不留 ABI 证据记录、删除探针 job），再按 (key+特征) 应用记忆并在需要时重建 routePlan/recoveryPlan；manifest/status 含 `routeFeatures`。内置 `KNOWN_LANE_INCOMPATIBILITIES = {(community-renodx-dxil-hlsl, dyncb)}`：即使没有工程记忆，动态 cbuffer 的 DXIL 也直接跳过 RenoDX；静态的仍然 RenoDX 优先（用户的原始排序）。结论：EID 11402 之前从未在 RenoDX 成功过（09-05 诊断已记录 `Unexpected dynamic cbuffer offset`），成功的是别的 EID；流程与以前相同（decomp.exe 直接吃 DXIL 容器，内部用 dxcompiler.dll 反汇编）。
- 追加：**组织文档草稿模式**——`write_shader_workspace_organization_file(..., mode="draft")` 把部分 JSON 合并到 `ai/organization-sessions/<id>/drafts/<kind>.json`（modules/stages 按名合并），返回全部问题（≤64）且不计数；`mode="final"` + 空 content 发布草稿；flow_spec/analysis 的最终错误信息也改为列出全部问题。测试 `test_shader_template_contract.py` +1。修了两个早前漂移的测试（`test_internal_host` 路由集合含 shader-source/check 与 asset-index；`test_cbuffer_aliases` 条目含 byteSize/original）。
- 待用户决定：**Host 侧结构性重构操作**（按行段抽函数、Host 做活跃变量分析并回编译验证），因为 300 KB 级单函数 shader 无法由 AI 整体重写提交（输出 token 上限）。

- 追加：**Host 侧结构性重构**（`shader_workspace/restructure.py`）：`extract_function(source, start, end, name, comment)` 在一个函数体内按完整语句范围抽函数——校验大括号平衡/语句边界/if-else 切割/`return`/逃逸的 `break`、`continue`；数据流分析得到 in / inout / out / 单返回值，参数类型取自声明或函数签名，数组局部量拒绝，参数上限 12；新函数插到调用者上方，范围替换为调用；`apply_operations` 顺序重放；`structure_report` 给每个函数尺寸/参数/调用/匿名量与启发式 flags（large/tiny/many-parameters/anonymous-locals/placeholder-name）。会话集成：`ShaderOrganizationManager._regenerate_proposal`（baseline → renames → structureOps → comments，固定顺序，`rename()` 也走它），`restructure(workspace, org, operation=extract|undo|remove|rename_function|list, ...)` 重放后 `service.compile_check` 通过才落盘并封入 session（`structureOps`），拒绝时会话不变、不计预算。工具 `restructure_shader_workspace_source`（policy: workspace）。评审：结构变更多数编译成同一份可执行码（exact-code），否则走既有的回放观测比较。测试 `test_shader_restructure.py`（5）、`test_shader_organization.py` +2。技能 step 7 改为迭代流程（切分 → 结构自审 2–3 轮 → 内部命名/注释），明确"AI 永远不整体重写文件"。Host 工具 147 个，清单校验通过。
- 追加：**函数账本（ledger）**——`restructure` 新增 `note` 操作（session `functionNotes`），每次结果返回 `ledger`：每函数一行 {purpose, lines, origin, inputs/inouts/outputs, calls, callers, globalsUsed, anonymousLocals, flags, status}；`structure_report` 增加 `globalsUsed` 与 `many-globals` 标记；`list` 只返回账本与汇总。技能 step 7(b) 改为"从账本而非代码做合并/拆分决策"。
