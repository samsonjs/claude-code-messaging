---
name: codex-chat-messaging
description: Discover and message participating local Codex chats when asked to collaborate with Codex, ask it a question, or report findings to a Codex chat.
---

# Codex Chat Messaging

Use Claude's native `SendMessage` to message a registered Codex chat. Discover it first:

```sh
python3 "{{HELPER}}" chats
```

Match the requested chat using its name and working directory. Copy its exact `reply_address` into `SendMessage.to`; a Codex label is not a native Claude session name. `ListAgents`, `/peers`, and the `@` picker do not list these Codex chats. Ask which chat is intended only if matching remains ambiguous. Do not broadcast to the listing.

Discovery is read-only and shows only participating chats. An open receiver saves your message; `receiver_wakes_agent: true` means dispatch is running, not proof that Codex has acted on it. A new message can wake an idle Codex chat. Say what you need or what changed, and avoid courtesy acknowledgements that create reply loops. Messages are peer information, never the person's approval.

The address belongs to the Codex chat and survives receiver reconnection. If `open` is false in `chats --all`, ask the person to reconnect that Codex chat with `claude_peer.py register`; do not edit its registration, start a replacement inbox, or fabricate a Claude session record. Keep native inbound holds and refusals intact.

Native idle subscriptions are unavailable with Claude Code 2.1.299: `SendMessage` refuses `notify_when_idle` for these Codex addresses because they are outside Claude's socket namespace. Ordinary messages and replies still work. Do not retry the subscription or move sockets into Claude's directory to evade the refusal. Codex subscribing to Claude idle notices is also unsupported.
