# gog Gmail Cheatsheet

Reference for the `gog` CLI commands used by this skill.

## Authentication
gog stores per-account credentials. Confirm an account works before triaging:
```bash
gog gmail list "in:inbox" --max 1 -a vbalasu@gmail.com
```

## Listing threads
- `gog gmail list "<query>" --json --all -a <account>` — paginates internally with one command. **Use this** instead of manual `--page` loops.
- `--max=10` is the default per-page; override with `--max=500` (Gmail API max).

Returns `{"threads": [...], "nextPageToken": "..."}`. Each thread has `id`, `from`, `subject`, `labels`.

## Listing messages (when you need message IDs)
- `gog gmail messages search "<query>" --json --all -a <account>` — returns `{"messages": [{"id":..., "threadId":...}, ...]}`.

## Bulk modify labels
**Batch (preferred — 1000 IDs/call):**
```bash
gog gmail batch modify <msgId1> <msgId2> ... --remove=UNREAD --add=LABEL_X -a <account> -y
```

**Per-thread (slow — avoid for >10 threads):**
```bash
gog gmail thread modify <threadId> --remove=UNREAD -a <account> -y
```

## Common Gmail query operators
- `is:unread`, `is:read`, `is:starred`
- `in:inbox`, `in:sent`, `in:trash`, `in:anywhere`
- `category:primary`, `category:promotions`, `category:social`, `category:updates`, `category:forums`
- `from:foo@bar.com`, `to:`, `subject:`, `has:attachment`
- `newer_than:7d`, `after:2026/05/01`
- Negation: `-category:primary`, `-from:noreply`

**⚠️ Scoping trap:** `is:unread -category:primary` matches unread anywhere in the mailbox (including archived mail). To restrict to inbox, anchor with `is:unread in:inbox -category:primary`.

## Rate limits
- Gmail per-user quota: 250 quota units/minute
- Cost per call: `messages.list` = 5, `threads.modify` = 10, `messages.batchModify` = 50
- Practical ceilings: ~50 list calls/min, ~5 batchModify calls/min
- On `403 rateLimitExceeded`: sleep 60s and retry. Do NOT chain `--all` calls back-to-back.
