# Custom message actions

Set `DSQRD_MESSAGE_ACTION` to an executable before launching dsqrd. When a message is selected, `a` invokes that executable with one positional argument containing a JSON payload.

```sh
export DSQRD_MESSAGE_ACTION="$HOME/.local/bin/my-dsqrd-message-action"
```

The payload uses the versioned `dsqrd.message-action.v1` schema:

```json
{
  "schema": "dsqrd.message-action.v1",
  "workspace": { "id": "…" },
  "channel": { "id": "…", "name": "…" },
  "message": {
    "id": "…",
    "author": { "id": "…", "name": "…" },
    "text": "…",
    "day": "…",
    "time": "…",
    "mine": false,
    "edited": false,
    "subtype": "",
    "threadId": "",
    "channelReference": "",
    "reply": { "id": "…", "author": "…", "text": "…" },
    "attachments": [],
    "reactions": [],
    "permalink": "…"
  }
}
```

`reply` is `null` when the message is not a reply. Attachment and reaction objects are the normalized objects already exposed by dsqrd's UI protocol.

If the variable is unset, the keybind and its help entry are hidden. The executable owns everything after invocation: it can open another app, save or transform the message, call a service, or ignore any fields it does not need.
