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

The send result includes an inbox UUID and cursor. Use `read --inbox UUID --after CURSOR` to collect later replies without resending the request. Follow-up sends can reuse the same inbox. Close it explicitly with `close --inbox UUID` when the exchange is finished.

The receiver stores messages; it does not automatically wake Codex. Receivers remain running until explicitly closed, forcibly terminated, or the machine restarts. Saved messages survive closure. State and logs are kept under `~/.claude-scratchpad/claude-peer-bridge/`, outside the checkout. Set `CLAUDE_PEER_DATA_DIR` to isolate them elsewhere.

## Development

This checkout uses jj. Run the integration tests with:

```sh
python3 scripts/test_claude_peer.py
```

Tests exercise real local sockets and CLI processes with isolated session fixtures. They cover delayed replies, acknowledgements followed by completion, cancelled waits, inbox reuse, closure, and retained messages. They do not message live Claude sessions. Forgejo CI runs the same suite.

Push changes to `origin` on Forgejo. Forgejo mirrors updates to GitHub automatically, with an eight-hour fallback sync. The mirror copies repository refs and code; issues and pull requests remain on their respective hosts.

## Protocol references

The wire format was verified with Claude Code 2.1.280 on macOS. It is an implementation detail that may change with Claude updates.

- [Anthropic's cross-session messaging documentation](https://code.claude.com/docs/en/cross-session-messaging)
- [Independent Go implementation of the socket transport](https://github.com/PeterSR/claude-code-socket-transport)
