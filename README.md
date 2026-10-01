# Claude Code Messaging

A Codex skill for talking to existing Claude Code sessions on the same machine. It discovers sessions, sends messages through Claude's native inbox sockets, and dispatches persistent replies into the owning Codex chat.

[Forgejo](https://git.samhuri.net/sjs/claude-code-messaging) is the primary repository. [GitHub](https://github.com/samsonjs/claude-code-messaging) is an automatic push mirror.

## Install

Requires macOS or Linux, Python 3.10+, and a running Claude Code session with cross-session messaging available. The helper has no third-party Python dependencies.

```sh
jj git clone https://git.samhuri.net/sjs/claude-code-messaging.git \
  "$HOME/Developer/claude-code-messaging"
mkdir -p "$HOME/.codex/skills"
ln -s "$HOME/Developer/claude-code-messaging" \
  "$HOME/.codex/skills/claude-code-messaging"
```

If an older copy is already installed at the destination, move it aside before creating the link. Keeping the installed skill linked to the checkout makes edits available without copying files between directories.

Ask Codex:

> Use $claude-code-messaging to ask the Claude session working on the build for a status update.

The full workflow is in [SKILL.md](SKILL.md).

## Reply lifetime

`send --wait 30` waits in the foreground for up to 30 seconds. Its detached reply receiver stays open after that wait expires, after the first reply, and after the foreground command is interrupted. `--wait 0` returns immediately while retaining a return address.

The send result includes an inbox UUID. Use `read --inbox UUID --unread` to collect later replies without resending the request. Once handled, use `ack --inbox UUID --through SEQ` to save your place across turns. Reading and sending do not acknowledge messages, so a follow-up cannot hide an unread backlog. `read --after CURSOR` remains available for replay.

While collaborating, Codex keeps the active turn open for unfinished work, continues independent work between replies, and uses bounded inbox waits when waiting on Claude. An acknowledgement or a foreground timeout does not finish the exchange. Scheduled polling is an explicit fallback, never an automatic consequence of asking Codex to collaborate or stay awake.

New inboxes automatically start a detached dispatcher when `CODEX_THREAD_ID` and a Codex CLI with the `queue` command are available. It queues replies into the owning chat: idle chats start a turn, while active chats receive a notification in their next turn. `receiver_wakes_agent` and `dispatch_status` report whether dispatch is running. `send --no-dispatch` creates a storage-only inbox. Use `dispatch --inbox UUID` from the owning chat to enable an existing inbox without changing its return address.

Queueing and handling have separate durable journals. A queued reply stays unread until explicitly acknowledged, and restarting dispatch does not queue it again. Replayed frames are deduplicated. Queue errors and uncertain interrupted attempts stop dispatch with their saved messages intact; handle and acknowledge the pending reply before restarting. Notifications include a bounded excerpt; `read --unread` returns the complete frames.

Keep the inbox open for later replies. Close it with `close --inbox UUID` when the person explicitly ends the exchange; this stops both receiver and dispatcher. They remain running until explicitly closed, forcibly terminated, or the machine restarts. Saved messages and acknowledgement positions survive closure. State and logs are kept under `~/.claude-scratchpad/claude-peer-bridge/`, outside the checkout. Set `CLAUDE_PEER_DATA_DIR` to isolate them elsewhere.

Claude replies to the raw `uds:` return address; “Codex” is a display name, not a registered Claude session. Idle subscriptions can reach that address, but the current helper does not send idle notices.

Dispatch uses the supported `codex queue` CLI, without a scheduled automation, another chat, or a private desktop socket. Verify automatic consumption on each host with a live peer reply arriving after a final response, without another human message. A running dispatcher alone does not prove host consumption.

Live acceptance passed in Codex desktop on 2026-10-01 with Claude session “Skill fix with Codex” [7a7e6c]. Codex finished at 11:07:12 Vancouver time; Claude's native reply arrived at 11:08:20 and resumed the same chat at 11:08:28 without human input. A separate probe also verified queueing while a turn was active.

## Development

This checkout uses jj. Run the integration tests with:

```sh
python3 scripts/test_claude_peer.py
```

Tests exercise real local sockets and CLI processes with isolated session and queue fixtures. They cover reply lifetimes, unread tracking, dispatch routing, deduplication, restarts, uncertain delivery, failures, blocking, closure, notification size, and inbox migration. They never message live Claude or Codex sessions; real host consumption needs the separate live checks above. Forgejo CI runs the same suite.

Push changes to `origin` on Forgejo. Forgejo mirrors updates to GitHub automatically, with an eight-hour fallback sync. The mirror copies repository refs and code; issues and pull requests remain on their respective hosts.

## Protocol references

The wire format was verified with Claude Code 2.1.280 on macOS. It is an implementation detail that may change with Claude updates.

- [Anthropic's cross-session messaging documentation](https://code.claude.com/docs/en/cross-session-messaging)
- [Independent Go implementation of the socket transport](https://github.com/PeterSR/claude-code-socket-transport)
