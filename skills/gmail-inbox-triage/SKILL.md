---
name: gmail-inbox-triage
description: Mark unread Gmail inbox emails as read in bulk based on an action checklist file. Use when the user has an action-checklist.md (or similar) listing emails that DO require attention, and wants everything else in the unread inbox marked as read. Triggers: "mark non-checklist emails as read", "clean up inbox based on checklist", "mark as read everything not on my action list", "triage unread inbox".
---

# Gmail Inbox Triage

Efficiently mark unread Gmail inbox threads as read in bulk, keeping only the threads referenced in a user-provided action checklist still unread.

## Capabilities

- Parse an action checklist (markdown) for senders, subjects, or thread IDs that should stay unread
- Fetch all unread inbox threads in one paginated call via `gog gmail list`
- Match unread threads against checklist entries (sender domain, subject keywords, explicit thread IDs)
- Mark non-checklist threads as read using `gog gmail batch modify` (up to 1000 message IDs per API call)
- Respect Gmail API rate limits (250 quota units/min/user; batchModify costs 50 each)

## Prerequisites

- `gog` CLI installed and authenticated for the target Gmail account (check: `gog gmail list --max 1 -a <account>`)
- An action checklist file (markdown) listing emails that need attention. Default path: `./action-checklist.md`

## Workflow

### Phase 0: Confirm account
Before fetching anything, confirm which Gmail account to triage. **Default: `vbalasu@gmail.com`** (the user's personal account; action checklists are typically personal). Briefly state the default and proceed unless the user redirects. Do not assume the Databricks work account.

### Phase 1: Load checklist
1. Read the checklist file (default `./action-checklist.md`)
2. Extract identifying signals from each item:
   - Sender email/domain (e.g., `junadesaius@gmail.com`, `wellsfargo.com`)
   - Subject keywords (e.g., "Wells Fargo", "JK Cricket Academy", "VFS Global")
   - Explicit thread IDs if present
3. Build a `keep_unread` matcher

### Phase 2: Fetch unread inbox
Run **one** command — do NOT page manually:
```bash
gog gmail list "is:unread in:inbox category:primary" --json --all -a <account> > /tmp/triage_unread.json 2> /tmp/triage_err.log
```
Default to `category:primary` since action checklists almost always concern the primary inbox. Drop `category:primary` only if the user explicitly wants all categories OR if the user references the total inbox unread count (sidebar shows e.g. "Inbox 452") that exceeds the primary-only count — that means they want Promotions/Updates/Social cleaned too.

**Sanity-check the count.** After Phase 1 returns N threads, compare against the user's reported unread count. If N << reported, you're scoped too narrowly — re-run without `category:primary`.

**Always redirect stderr to a separate file** (not `2>&1` into the JSON file). gog prints errors like `403 rateLimitExceeded` to stderr; mixing them into the JSON corrupts it.

**Rate limit handling.** gog exits with code 7 on `403 rateLimitExceeded` (Gmail quota: 250 units/min). On exit 7, sleep 70s and retry. Never chain manual sleeps to bypass — use a single retry block. Quota costs: messages.list = 5, batchModify = 50, so a fresh quota window can do ~5 batch-modifies of 1000 IDs each.

### Phase 3: Classify
Load the JSON, walk each thread, and split into `keep_unread` (matches checklist) vs `mark_read` (doesn't match).

Show the counts to the user before mutating: `"Keep unread: X, Mark read: Y. Proceed?"` — but if the request was unambiguous ("mark non-checklist as read"), skip confirmation.

### Phase 4: Bulk mark as read
Use **message-level batch modify**, NOT per-thread modify:

```bash
# 1. Get message IDs (not thread IDs) for the threads to mark read
gog gmail messages search "is:unread in:inbox category:primary" --json --all -a <account> > /tmp/triage_msgs.json

# 2. Filter out messages whose threadId is in keep_unread
# 3. Batch modify in chunks of 1000:
gog gmail batch modify <id1> <id2> ... <id1000> --remove=UNREAD -a <account> -y
```

Per-thread `gog gmail thread modify` is ~5x slower and more rate-limit-prone. Always prefer `batch modify` on message IDs.

### Phase 5: Verify
```bash
gog gmail list "is:unread in:inbox category:primary" --json -a <account> | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('threads',[])),'remaining unread')"
```
Should equal the `keep_unread` count from Phase 3.

## Resources

- `scripts/triage.py`: End-to-end script that runs all phases. Pass `--account <email>` and `--checklist <path>`.
- `references/gog-cheatsheet.md`: Quick reference for the gog Gmail commands used.

## Important Constraints

- **Scope correctly**: Use `in:inbox`, NEVER `-category:primary` alone (the latter matches all unread anywhere in the mailbox including archived mail — this is what caused a 41K-message runaway in the original session). Always anchor to `in:inbox`.
- **Don't use `-category:primary` to mean "non-primary tabs"** — Gmail's negated-category syntax silently returns zero results when combined with other filters. Use the positive form `(category:updates OR category:promotions OR category:social OR category:forums)` instead. Symptom of the bug: gog reports "Marked as read 500 messages" repeatedly but the inbox count never drops — gog is reporting `--max`, not actual changes, and the underlying query matched nothing.
- **Scope by recency**: For large backlogs, add `newer_than:3m` (or similar) to bound the work. Bulk-cleaning years of inbox via the API is the wrong tool — point users to Gmail web UI's "select all → mark read" for that.
- **Verify with a count, not a "marked N" line**: Always sanity-check by re-listing `is:unread in:inbox` after the run. Gmail/gog can report success on a no-op query.
- **Batch, don't loop**: One `batch modify` call with 1000 IDs replaces 1000 individual `thread modify` calls.
- **Pace if needed**: If hitting `403 rateLimitExceeded`, sleep 60s before retry. With proper batching this rarely triggers.

## Examples

### Example: Cleanup using default checklist
User says: "Mark non-checklist primary unread as read"
Result: Reads `./action-checklist.md`, fetches unread primary inbox, classifies, batch-marks non-matches as read in one or two API calls.

### Example: Different account / checklist
User says: "Use my-cleanup.md to triage work@example.com inbox"
Result: Loads `my-cleanup.md`, runs against `work@example.com`.

### Example: Inspection only (dry-run)
User says: "Show what would get marked read but don't do it"
Result: Run Phases 1-3, print the would-mark-read list, stop before Phase 4.
