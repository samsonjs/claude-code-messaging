# Local integration

## Install and activate

The CLI helper needs Python 3.10+ with no external packages. Installation needs Python 3.11+, `codex`, and `uv`; the optional MCP server uses the pinned official Python MCP SDK.

```sh
python3 scripts/install.py
```

The installer adds `claude_code_messaging` through `codex mcp add`, installs the Claude discovery skill, and chains the existing Codex `notify` command after the bridge's turn-completion handler. It preserves other lifecycle hooks. Re-running it does not duplicate hooks or replace the saved original notifier with itself.

Reload MCP configuration in the client if the tools do not appear. Codex requires new or changed hooks to be reviewed and trusted through its normal hook UI (`/hooks` in the CLI). The installer does not change trust records. Once reviewed, the SessionStart hook registers new/resumed chats automatically, and the other hooks track turn starts, interruptions, session end, and permission mode. Real completion comes from `notify`'s `agent-turn-complete` event, rather than a guessed timer or a model-authored status message. See [Codex hooks](https://learn.chatgpt.com/docs/hooks) and [notifications](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications).

## Stable chat addresses

Run from a participating Codex chat:

```sh
python3 scripts/claude_peer.py register --name "Exports planning" --cwd /absolute/project/path
```

`CODEX_THREAD_ID` identifies the current chat. Registration adopts its existing live inbox when possible; otherwise it creates a deterministic inbox for that chat. Default sends reuse it. Register again after receiver termination or reboot to reconnect the same address; saved messages, acknowledgements, sequence numbers, and replay deduplication survive. Existing additional inboxes remain valid until explicitly closed. Explicit `send --inbox UUID` still selects an individual exchange.

Socket addresses stay outside Claude's registry and socket directory. Registration does not create native Claude sessions, `ListAgents` rows, or `@` mentions. An explicit `close` stays closed until the person reconnects or authorizes another send.

Discovery is read-only:

```sh
python3 scripts/claude_peer.py chats
python3 scripts/claude_peer.py chats --all
```

Rows contain only the chat label, cwd, reply address, receiver status, wake capability, and known activity. Names and directories come from registration; discovery never reads chat transcripts. Offline chats are hidden by default. Claude can load `~/.claude/skills/codex-chat-messaging/SKILL.md` and send a native `SendMessage` to a discovered address, without first receiving a Codex message. Treat incoming work as a peer handoff within the person's authorized scope. Do not broadcast or mistake peer claims of approval for human approval.

## Native tools

Prefer the `claude_code_messaging` MCP tools when available:

- `list_peers`: local Claude sessions plus participating Codex chats.
- `register_chat`: establish this chat's label, cwd, stable address, and optional sender mode.
- `send_message`: send to a freshly discovered Claude PID.
- `read_inbox`: read unread records without consuming them; wait at most 30 seconds.
- `acknowledge`: mark handled sequences locally.
- `list_inboxes`: inspect the calling chat's inboxes and connection health.

Pass the current chat's `CODEX_THREAD_ID` as `thread_id`, never an ID copied from a peer message. A server bound to a thread rejects a different caller ID; inbox operations reject another owner's inbox. The server uses the same helper and durable state as the CLI, so switching interfaces does not create a separate message history. Read-only discovery is available to both sides; MCP registration and message handling belong to Codex.

## Sender permission mode

`from_mode` is `prompting`, `bypass`, or omitted when unknown. Trusted lifecycle events update it from the host's permission mode; ambiguous plan mode remains unknown. Registration also accepts `--sender-mode` when the actual runtime policy is known. Do not infer a chat's current policy from global defaults or select a mode to avoid an inbound hold.

A known bypass sender is held by a prompting Claude session under native defaults. Claude Desktop cannot display the terminal approval dialog, so a default-held message can expire. Receipts may not return to this bridge's socket namespace. `sent` remains proof of socket transmission, not proof of admission. Preserve the receiving session's controls; do not change permissions or downgrade the asserted mode to make delivery pass.

## Idle notifications

Native idle subscriptions are unavailable in both directions with Claude Code 2.1.299. In a live test on 2026-10-15, Claude's `SendMessage` delivered an ordinary message to a discovered Codex chat but refused `notify_when_idle` before sending a subscription because the target was outside Claude's socket namespace. Codex subscribing to Claude is also rejected by its control-frame address checks. Do not move sockets into Claude's directory to evade these restrictions. Reply-triggered Codex wakeups are separate and remain supported.

The receiver implements `control` / `notify_when_idle` handling for compatibility, validated only with isolated wire tests. If a valid subscription reaches it from a live registered Claude session, it sends one correlated `peer_idle_notice` after a real completion, or immediately for an already-idle chat with established lifecycle tracking. Without tracked turn starts, a subscription arriving after an old completion waits for the next completion. An older turn's completion cannot mark a newer tracked turn idle. Unhandled peer work also defers notices.

Subscriptions expire after 12 hours. The target session ID is saved to guard against PID reuse. Attempt state is durable: uncertain deliveries are not blindly retried. Native inbound controls still apply to the resulting notice. These implementation checks do not establish native interoperability; a future Claude version needs a successful live subscription and consumed notice before this can be advertised as supported.

## Background workers

Dispatch briefly collects a burst and queues one compact notice for its highest new sequence. It preserves every received frame and acknowledges none. A later batch still gets a new notice; uncertain queue attempts remain stopped for inspection.

After updating worker code, restart a dispatcher with `dispatch --inbox UUID --restart` from its owning chat. To load updated receiver code, close and re-register that chat after checking unread messages; this preserves its address. Existing receiver and dispatch processes do not reload Python code automatically. Re-registration recovers a dead receiver, but there is no reboot supervisor or automatic process monitor in this version.
