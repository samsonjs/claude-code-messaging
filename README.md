# Claude Code Messaging

A Codex skill for talking to existing Claude Code sessions on the same machine. It discovers sessions, sends messages through Claude's native inbox sockets, and collects replies in persistent local inboxes.

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

For ongoing communication, the skill sets up a Codex desktop heartbeat in the same chat to check unread messages, normally every minute. The receiver only stores messages; the heartbeat wakes Codex to handle them after a turn ends. The app must remain running and the machine awake. If scheduling is unavailable, the skill reports that limitation instead of claiming it is listening. Short requests can still use the foreground wait without scheduling.

Leave the inbox and its watch open when a turn finishes. Close the inbox with `close --inbox UUID` and disable the heartbeat only when the person explicitly ends the ongoing exchange. Receivers remain running until explicitly closed, forcibly terminated, or the machine restarts. Saved messages and acknowledgement positions survive closure. State and logs are kept under `~/.claude-scratchpad/claude-peer-bridge/`, outside the checkout. Set `CLAUDE_PEER_DATA_DIR` to isolate them elsewhere.

## Development

This checkout uses jj. Run the integration tests with:

```sh
python3 scripts/test_claude_peer.py
```

Tests exercise real local sockets and CLI processes with isolated session fixtures. They cover delayed replies, acknowledgements followed by completion, cancelled waits, inbox reuse, closure, durable unread tracking, and follow-up sends with queued replies. They do not message live Claude sessions or test the Codex app scheduler. Forgejo CI runs the same suite.

Push changes to `origin` on Forgejo. Forgejo mirrors updates to GitHub automatically, with an eight-hour fallback sync. The mirror copies repository refs and code; issues and pull requests remain on their respective hosts.

## Protocol references

The wire format was verified with Claude Code 2.1.280 on macOS. It is an implementation detail that may change with Claude updates.

- [Anthropic's cross-session messaging documentation](https://code.claude.com/docs/en/cross-session-messaging)
- [Independent Go implementation of the socket transport](https://github.com/PeterSR/claude-code-socket-transport)
