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

You can request `notify_when_idle` from the discovered raw address. Support depends on a real Codex completion notifier being installed; do not infer support from a successful socket send. The bridge answers pending subscriptions on real completion and only reports an already-idle chat immediately when lifecycle tracking is available. The reverse direction, Codex subscribing to Claude idle notices, is not supported by this bridge's socket location.
