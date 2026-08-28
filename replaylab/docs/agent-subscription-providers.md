# Agent subscription provider matrix

Verified against the vendors' official documentation on 2026-08-27. These are
subscription/CLI connections, not API-key adapters. ReplayLab invokes only the
fixed command in its built-in provider catalog, stores no vendor subscription
token in the profile JSON, and performs an ACP/app-server handshake without
sending a chat prompt. Vendor CLIs are launched with a profile-scoped state
directory; ReplayLab neither parses nor copies their credentials. A vendor may
still use operating-system credential storage independently of that directory.

| Provider | Official authentication path used by ReplayLab | Agent transport | Model selection |
| --- | --- | --- | --- |
| OpenAI Codex | Codex app-server account login; the app-server returns the browser/device authorization information. | `codex app-server` | app-server model catalog |
| GitHub Copilot CLI | `copilot login --web-flow` or `copilot login --device-code` | `copilot --acp` over stdio | Imported when the ACP session exposes a model config option; otherwise use the CLI default or an exact supported model. ACP itself cannot run `/login`. |
| Cursor CLI | `agent login` opens Cursor's official browser flow and uses the saved Cursor subscription. ReplayLab invokes the equivalent unambiguous `cursor-agent` alias and then selects the ACP `cursor_login` authentication method. | `cursor-agent acp` over stdio | Imported when the ACP session exposes model configuration; otherwise use Cursor's configured default. |
| Google Gemini CLI | Start `gemini`, choose **Sign in with Google**, and complete the browser flow in the contained terminal. | `gemini --acp` over stdio | Imported when exposed in ACP session configuration; otherwise use the configured default. |
| Qwen Code | Start `qwen`, run `/auth`, choose Alibaba ModelStudio Coding Plan or another supported API provider, then exit after verification. The former Qwen OAuth path was discontinued on 2026-04-15. | `qwen --acp` over stdio | Imported when exposed in ACP session configuration; otherwise use the configured default or an exact provider model. |
| Kimi Code CLI | `kimi login`; the CLI uses an RFC 8628 device-code flow and prints the verification URL/code. | `kimi acp` over stdio | Kimi ACP can return model/session configuration options, which ReplayLab imports when present. |
| Anthropic Claude Code | Start `claude`; first launch opens its browser login, and `/login` re-authenticates an existing session. | **Not a built-in subscription preset:** current official Claude Code documentation does not define an ACP server mode. A future integration needs a dedicated Claude CLI connector and an authentication/terms review; the Agent SDK documentation says third-party products must use API keys unless separately approved to offer claude.ai login. | Not applicable to the ACP selector. Claude API remains available through the Anthropic API preset. |
| Custom ACP | ReplayLab never guesses a login command. Authentication must already be configured by the user. | User-entered ACP stdio command | Handshake discovery when available, or manual exact model. The command is never automatically trusted. |

For a new built-in provider connection, qrenderdoc visibly checks trust only for
the read-only official catalog command and **Save and Connect** immediately runs
the relevant login and no-prompt handshake. Custom ACP continues to require an
explicit trust checkbox after the user reviews every argument. A missing CLI is
reported with its exact official installation command/link.

ACP standardizes session configuration options, including a `model` category,
but it does not guarantee that every agent will publish a model list. The UI
therefore leaves manual entry available and says when the handshake exposed no
standard list instead of treating an empty list as a failure.

Cursor runs with profile-scoped `CURSOR_CONFIG_DIR`, `HOME`, and `USERPROFILE`
values plus a fresh, ReplayLab-owned working directory for every Agent turn.
Both user-level and project-level `mcp.json` files are pinned empty so Cursor
does not discover the user's editor MCP servers. ReplayLab rewrites Cursor's CLI
policy before each launch so ambient shell, arbitrary file access, web fetch,
and user-configured MCP servers do not enter the RenderDoc task. ACP v1 does not
expose a host-owned dynamic-tool callback, so a capture-aware specialist receives
exactly one first-party `ReplayLabMCP` server filtered to that Skill task. The
Cursor permission layer allows that server namespace (`Mcp(ReplayLab:*)`) instead
of duplicating a static tool list; the per-turn Host filter remains authoritative.
live tool manifest, frozen capture scope, semantic workflow effects, audit,
postconditions and rollback requirements are checked by ReplayLab; the ACP
Agent's prose cannot grant or revoke them. Text-only planning and direct answers
need no MCP server. An unrelated ACP permission request is cancelled rather than
being approved by matching a human-readable title or metadata.

Model API, Codex app-server and ACP connections all enter the same ReplayLab main
router and use the same typed conversation, selected origin/current snapshot,
Shader or Event/Pipeline descriptor, acceptance contract and terminal receipt.
The provider transport and model can change wording and reasoning quality, but
not the Host-owned evidence required to call a specialist result complete.

## Official references

- [Codex CLI](https://developers.openai.com/codex/cli/)
- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [GitHub Copilot ACP server](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server)
- [Install GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)
- [Cursor CLI ACP](https://cursor.com/docs/cli/acp)
- [Cursor CLI MCP discovery](https://cursor.com/docs/cli/mcp)
- [Cursor CLI authentication](https://cursor.com/docs/cli/reference/authentication)
- [Cursor CLI installation](https://cursor.com/docs/cli/installation)
- [Cursor CLI configuration](https://cursor.com/docs/cli/reference/configuration)
- [Cursor CLI permissions](https://cursor.com/docs/cli/reference/permissions)
- [Gemini CLI installation](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/installation.mdx)
- [Gemini CLI authentication](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/authentication.mdx)
- [Gemini CLI ACP mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md)
- [Qwen Code quickstart](https://qwenlm.github.io/qwen-code-docs/en/users/quickstart/)
- [Qwen Code authentication](https://qwenlm.github.io/qwen-code-docs/zh/users/configuration/auth/)
- [Kimi CLI command reference](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command)
- [Kimi ACP reference](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [Claude Agent SDK overview](https://platform.claude.com/docs/en/agent-sdk/overview)
- [ACP session configuration options](https://agentclientprotocol.com/announcements/session-config-options-stabilized)
- [ACP authentication methods](https://agentclientprotocol.com/rfds/auth-methods)
