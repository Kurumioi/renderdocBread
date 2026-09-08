# ReplayLab AgentHost（RenderDocUltra AI 接入层）

ReplayLab 把 qrenderdoc 的当前捕获上下文、首方 MCP 工具和文件系统 Agent Skills 交给 AI，同时把模型/厂商运行时留在独立进程中。当前架构只有两条路径：

| 连接类型 | 引擎 | 行为 |
|---|---|---|
| Model API | GitHub `deepagents` | 主 Agent 负责对话，通过按 Skill 构建的受限子 Agent 调用 ReplayLab MCP |
| ACP / Codex 订阅 | 厂商原生 Agent | 厂商 CLI 自己完成单 Agent 循环，直连任务级 ReplayLab MCP |

不存在 legacy/lean 引擎选择器、ReplayLab 自研 planner/reviewer、WorkflowGuard、工具收据循环、Custom Agents 或可运行的外部 Agent 扩展。

## 模块边界

- `src/replaylab_agent/`：AgentHost、profile/凭据、deepagents 与原生厂商连接、任务事件。
- `mcp_host/src/`：RenderDoc 工具、效果控制、产物验收、统一报告表单。
- `renderdoc_mcp_bridge/`：qrenderdoc 固定加载的首方 Python 桥；不是可扫描插件。
- `../.agents/skills/`：标准文件系统 Skills；`replaylab-core` 完整注入两种引擎的所有 Agent，缺失或损坏时停止启动。
- `packaging/`：冻结 AgentHost、MCP 和内部服务并安全部署到 qrenderdoc 应用目录。

详细边界见 [ARCHITECTURE.md](ARCHITECTURE.md)，当前进度与外部验收项见 [docs/HANDOFF_2026-09-04.md](docs/HANDOFF_2026-09-04.md)。

## 三层输出约束

1. 产物层：shader、流程图、分析和导出只通过 Host 工具生成或进入项目；编译、ABI/等价、模板和固定渲染由确定性代码执行。
2. 报告层：完成捕获工作后必须调用 MCP Host 的 `report_task_result`，提交 `replaylab-task-report/v1`；Host 核对实际工具证据、推断置信度、产物路径和状态。
3. 验收层：缺少已接受报告时，Host 有界追问。deepagents 复用同一 graph 实例，ACP 复用 session，Codex 用重新携带核心契约和有界 Host 事实的新 thread。默认最多 2 次，可用 `REPLAYLAB_VERIFY_MAX_ROUNDS=0..5` 调整；未解决工作如实标为 partial/failed。

内容审核默认最多 3 次，由 `REPLAYLAB_REVIEW_MAX_ROUNDS=1..10` 统一配置。
退回给出严重程度、位置、修正目标和样例；轻微问题可直接接受，证据、产物、
schema 或完成状态错误不能因次数耗尽通过。模型填写工具提供的模板，Host
渲染流程图和分析文档；报告审核与 Shader 审核复用同一 verdict 契约。

模型的措辞可以不同，Host 验证的结构、证据和产物不能不同。

## 安装开发环境

AgentHost 要求 Python 3.12。以下命令在 `replaylab` 目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]" -e ".\mcp_host[bundle]"
```

deepagents、LangGraph、ACP/MCP 和支持的 LangChain provider 是 AgentHost 的正式依赖，不需要再选择 legacy/deep extra。

## Profile 与凭据

查看内置 preset 和当前 profile：

```powershell
.\.venv\Scripts\replaylab-ai.exe profile presets
.\.venv\Scripts\replaylab-ai.exe profile list
```

添加一个 API profile；模型 ID 应替换为供应商实际支持的值：

```powershell
.\.venv\Scripts\replaylab-ai.exe profile add api-main `
  --preset openai --model MODEL_ID --select
```

API 密钥优先写入 Windows Credential Manager，而不是 `profiles.json`：

```powershell
$env:OPENAI_API_KEY | .\.venv\Scripts\replaylab-ai.exe profile credential store `
  --profile api-main --api-key-stdin
```

订阅 profile 使用对应厂商 preset，例如 `codex`、`claude-code-acp`、`copilot-acp` 或 `cursor-acp`。登录、连接测试和进程 containment 由 qrenderdoc 设置 UI 驱动；CLI 的 `--start-gated` 接口是该 UI 的受控内部通道，不应手工绕过。

配置位置：

- profile：`%APPDATA%\ReplayLab\ai\profiles.json`；
- API secret：Windows Credential Manager；
- 引擎限额：`%LOCALAPPDATA%\ReplayLab\config\agent-engine.json`；
- 任务事件/报告 sidecar：`%LOCALAPPDATA%\ReplayLab\ai-tasks\`；
- cbuffer 别名：`%LOCALAPPDATA%\ReplayLab\aliases\cbuffer\`。

## 运行与诊断

普通无捕获对话：

```powershell
.\.venv\Scripts\replaylab-ai.exe run --profile api-main --prompt "解释 GPU overdraw"
```

连接当前唯一已加载捕获的 qrenderdoc 实例：

```powershell
.\.venv\Scripts\replaylab-ai.exe run --profile api-main --live-context `
  --prompt "解释当前事件" --json
```

离线诊断不会发起模型生成：

```powershell
.\.venv\Scripts\replaylab-ai.exe doctor --profile api-main
```

CLI 只公开 `run`、`profile`、`doctor`。qrenderdoc 正常使用会传入冻结 context file、instance id、task id、取消文件和 MCP 路径；这些参数不建议用户自行拼装。

## 安全边界

- profile 中禁止原始 secret；自定义 Agent 命令是 argv 数组，不经过 shell，并拒绝常见 secret 参数。
- 捕获任务的 MCP 路由绑定 task id、事件根、qrenderdoc instance、冻结 scope、工具 allowlist、效果上限与最大调用数。
- `policy.py` 只定义 read-only/workspace/project/ui/replay 效果；实际调用由 Host 再校验。
- 厂商原生模式不允许 shell、通用文件编辑、Apps、computer-use 或厂商 sub-agent 扩大权限。
- UI 只展示经过脱敏和大小限制的进度；原始 provider 诊断进入本地日志。
- 工具失败以有界 `ERROR: <Host diagnostic>` 返回 Agent，使其可以修正参数；不会把捕获数据当成指令执行。

## 测试

Windows 必须显式指定 pytest 临时目录，避免符号链接清理权限问题：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  --basetemp=output\pytest-tmp-local
```

其中包括真实已安装 deepagents graph 的离线集成测试，以及 ACP/Codex、报告 sidecar、第三层追问、权限、事件与 qrenderdoc 契约测试。需要真实账号或会产生费用的供应商调用不属于默认测试。

## 打包与 Development 部署

```powershell
.\packaging\tests\Test-ReplayLabPackaging.ps1
.\packaging\tests\Test-ReplayLabRuntimeDeployment.ps1
.\packaging\Build-ReplayLabRuntime.ps1
.\packaging\Deploy-ReplayLabRuntime.ps1 `
  -ApplicationDirectory ..\x64\Development
```

冻结输出在 `output/replaylab/`。部署脚本校验 manifest、大小和 SHA-256，只替换 marker 所有的运行时文件，不接管应用目录中的无关内容。完整说明见 [packaging/README.md](packaging/README.md)。

qrenderdoc 在内存受限 Windows 机器上必须使用 `MSBuild ... /m:2`。发布前还需对 x64 Release 重复冻结运行时部署、翻译完整性和完整应用构建。

## cbuffer 与 Shader Workspace 别名

Buffer Viewer 支持常量块及成员显示别名，按布局哈希跨 shader/捕获共享；用户别名优先于 AI 别名。别名不改变反射、材质字段或 shader 字节。

Shader Workspace 的主编辑缓冲始终是实际编译/替换的规范源码。`ai/organized.hlsl` 与 `ai/aliases.json` 是审阅工件，不会被悄悄当成另一份可编译源码；如需别名浏览，应以后增加显式只读副视图。
