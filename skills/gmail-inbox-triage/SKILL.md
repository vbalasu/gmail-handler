---
name: gmail-inbox-triage
description: Analyze unread Gmail inbox, cluster by sender/topic, recommend bulk actions, and mark messages as read after processing. Use when the user says "triage my inbox", "clean up unread", "what's in my inbox", "process my unread email", "help me get to inbox zero", or wants help acting on unread mail without a pre-built checklist.
---

# Gmail Inbox Triage

Analyze the unread **Primary** Gmail inbox, cluster messages, recommend per-cluster actions (bulk mark-read, drill in, reply), and mark messages as read after the user confirms each action. No checklist file required — the skill builds the picture from the inbox itself.

**Scope is always `is:unread in:inbox category:primary`** — only the Primary tab, never Promotions/Social/Updates/Forums. This is deliberate: Primary is where the mail that actually needs a human lives, and scoping to it keeps counts small and matchable against Gmail's UI.

**Always count threads, not individual messages.** Gmail's UI (the number next to "Inbox"/Primary) counts unread *conversations*. `gog gmail list` returns one entry per thread, so `len(threads)` is the number to report — and it should match what the user sees in Gmail. Never report a message count; if you mention a thread with multiple messages, still count it as one.

## When invoked with no arguments

Respond immediately with a short explainer (no tool calls, no inbox fetch). Roughly:

> **gmail-inbox-triage** — analyzes your unread **Primary** inbox, clusters threads by sender/topic, recommends bulk actions, and marks threads read after you approve. Marks the exact threads you reviewed — new mail that arrives during triage is left untouched. Counts match Gmail's Primary unread badge (threads, not messages).
>
> **Usage:** `/gmail-inbox-triage <optional instruction>` — e.g. "triage my inbox", "just clear marketing junk", "help me get to inbox zero", or "what's in my unread?"
>
> **Defaults:** account `vbalasu@gmail.com`, scope `is:unread in:inbox category:primary` (no recency filter, so the count matches Gmail's UI). Override the account by saying so; add a `newer_than:` window only if you want to narrow further.

Then stop. Don't auto-run the workflow until the user gives a directive.

## Capabilities

- Fetch unread Primary inbox via `gog gmail list "is:unread in:inbox category:primary"`
- Cluster threads by sender/domain and topic, surfacing the largest groups first
- Recommend per-cluster actions: bulk mark-read for newsletters/transactional/social noise that slipped into Primary, "needs attention" for action-required threads, "drill in" for ambiguous senders
- Bulk mark-read by thread/message ID with verification by re-listing (thread counts)
- Help draft replies (delegates to `email-reply` skill) and mark replied threads read after sending

## Prerequisites

- `gog` CLI installed and authenticated for the target Gmail account (`gog gmail list --max 1 -a <account>`)

## Workflow

### Phase 0: Confirm scope
State the scope in one short line, then proceed:
1. **Account** — default `vbalasu@gmail.com` (personal). Briefly state the default; do not assume the Databricks work account.
2. **Scope** — always `is:unread in:inbox category:primary` (Primary tab only, no recency filter so the thread count matches Gmail's UI). Only add a `newer_than:` window if the user asks to narrow, or if Phase 1 paginates (see below).

### Phase 1: Fetch and analyze
```bash
gog gmail list "is:unread in:inbox category:primary" --max 1000 --json -a <account> > /tmp/triage_unread.json 2> /tmp/triage_err.log
```

**Always redirect stderr separately** — gog prints `403 rateLimitExceeded` and other errors to stderr; merging them into the JSON corrupts it.

The number of entries in `threads` is the **thread count** — report this, and it should match the unread number on Gmail's Primary tab. If it doesn't match, say so and reconcile before acting (usually pagination, or another client read mail concurrently).

If exit code is 7 (`rateLimitExceeded`, Gmail quota = 250 units/min), sleep 70s and retry once. Don't chain shorter sleeps. Avoid firing several `list` calls back-to-back — Primary is small, so a single fetch is usually enough.

If `nextPageToken` is non-empty after `--max 1000`, Primary has >1000 unread threads — tell the user and offer to narrow with a `newer_than:` window before continuing.

### Phase 2: Cluster and recommend
Everything here is already Primary, so cluster by:
- **Sender domain** (e.g., `@notify.wellsfargo.com`, `@linkedin.com`)
- **Topic patterns** in subjects (newsletters, receipts, alerts, security, social notifications)

Count **threads** in every cluster (one snapshot entry = one thread), and make the cluster sizes sum to the total thread count you reported in Phase 1.

**While clustering, also write the snapshot IDs per cluster** to `/tmp/triage_clusters.json` as `{"<cluster_label>": ["<id>", ...], ...}`. Phase 3 uses these IDs to mark read exactly the messages the user reviewed — this is what prevents new arrivals during triage from being swept up. Example sidecar build:

```python
import json, collections
clusters = collections.defaultdict(list)
for t in threads:
    label = classify(t)   # e.g. "promotions", "linkedin.com", "paypal.com"
    clusters[label].append(t['id'])
json.dump(clusters, open('/tmp/triage_clusters.json','w'))
```

For each cluster of size ≥ 3, classify into a recommendation tier:

| Tier | What it is | Default action |
|------|-----------|----------------|
| **Auto-noise** | Marketing, newsletters, social digests, receipts/order confirmations, "your X is ready" — repeating senders with no required action | Recommend bulk mark-read |
| **Maybe-action** | Bills, security alerts, account warnings, healthcare, tuition, travel, calendar invites | Show subjects; ask user per cluster |
| **One-off** | Singletons or ambiguous senders | List individually; let user pick |

Present like this — keep it scannable. Counts are **threads** (matches Gmail's Primary unread badge):

```
Found 42 unread threads in Primary. Recommendations:

AUTO-NOISE — recommend bulk mark-read (24 threads):
  • LinkedIn notifications × 9
  • PayPal/Amazon receipts × 7
  • Audible / order confirmations × 5
  • Newsletters (Substack, JamesClear) × 3

MAYBE-ACTION — review (12 threads):
  • Wells Fargo balance alerts × 5
  • Kaiser Permanente care team × 4
  • OpenAI/Microsoft action-required × 3

ONE-OFF (6 threads):
  • Sarah Chaplin — Bay Area visit
  • Tom Linton (Atlan) — meeting request
  • ...

Reply 'go' to mark all AUTO-NOISE as read, or pick clusters.
```

### Phase 3: Process per cluster
For each cluster the user approves, mark read **by message ID** from the snapshot — never by query. Query-based mark-read re-evaluates against the live inbox and will sweep up mail that arrived after the snapshot (see Constraints).

**Bulk mark-read (small cluster, ≤ ~50 IDs)** — pass IDs directly:
```bash
gog gmail mark-read <id1> <id2> <id3> ... -y -a <account>
```

**Bulk mark-read (larger cluster)** — stream IDs through xargs to stay under arg-length limits:
```bash
jq -r '.["promotions"][]' /tmp/triage_clusters.json \
  | xargs -n 50 gog gmail mark-read -y -a <account>
```

Cluster labels are whatever keys you wrote into `/tmp/triage_clusters.json` in Phase 2 (sender domain, category name, or combined label). To mark several clusters in one go, union the ID lists with jq:
```bash
jq -r '(.["promotions"] + .["social"] + .["linkedin.com"])[]' /tmp/triage_clusters.json \
  | xargs -n 50 gog gmail mark-read -y -a <account>
```

**Reply needed** — delegate drafting to the `email-reply` skill, then mark-read the specific thread ID (from the snapshot) after the user sends.

**Drill in** — fetch headers/snippet for a single thread:
```bash
gog gmail get <messageId> --format=metadata --headers=Subject,From,Date,To --json -a <account>
```

**Snapshot staleness:** if more than ~10 minutes pass between the Phase 1 fetch and user approval — especially before a broad "mark all" action — re-run Phase 1 to refresh the snapshot before mark-read. The IDs themselves don't go stale, but new mail won't be in the user's review.

### Phase 4: Verify after each batch
After every mark-read call, **always** re-count rather than trusting the "Marked as read N" line (gog reports `--max` even on no-op queries). Count **threads**, and pull the full list so the number matches Gmail's UI:
```bash
gog gmail list "is:unread in:inbox category:primary" --max 1000 --json -a <account> 2>/dev/null | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('remaining unread threads:', len(d.get('threads',[])), 'more:', bool(d.get('nextPageToken')))"
```
The thread count should drop by `len(cluster_ids)` — **but multi-message threads are an exception.** `gog gmail mark-read <id>` marks only that one message; a thread with other unread messages stays unread (and stays in the count). When a cluster's count doesn't drop fully, check the snapshot's `messageCount` for the leftover IDs — threads with `messageCount > 1` are the usual reason. Either leave them (often they're real conversations worth seeing) or mark every message in the thread. If the leftovers are *not* multi-message threads, diagnose before retrying — usually the wrong cluster key was passed to jq, or another client (Gmail web/mobile) read mail concurrently.

### Phase 5: Wrap up
Summarize: total marked read, total remaining unread, what's left needing attention. Offer to draft replies for the "one-off" cluster.

## Resources

- `references/gog-cheatsheet.md`: Quick reference for the `gog` Gmail commands used.
- `scripts/triage.py`: Legacy end-to-end script (checklist-driven, kept for backwards-compat). The current workflow above is interactive and does not use this script.

## Important Constraints

- **Mark-read must be ID-based, not query-based.** `gog gmail mark-read --query '...'` re-evaluates the query against the live inbox at execution time, so any message that arrived after the Phase 1 snapshot and matches the query gets marked read without the user seeing it. Always pass positional message IDs from `/tmp/triage_clusters.json` instead. Use `--query` only for *listing* (Phase 1) and *verification* (Phase 4), never for mutation.
- **Scope is always `is:unread in:inbox category:primary`** — Primary tab only, every list and verify query. Keep `in:inbox`: Primary category labels persist on messages even after they're archived, so `category:primary` alone can match archived mail and cause a runaway. With `in:inbox` the thread count matches Gmail's Primary unread badge exactly. Don't default to a recency filter (it would undercount vs. the UI); add `newer_than:` only when the user asks to narrow or Phase 1 paginates.
- **Count threads, not messages.** Gmail's UI counts unread conversations. `gog gmail list` returns one entry per thread, so `len(threads)` is the number to report — never sum messages. A thread with several unread messages still counts as one.
- **Never use `-category:primary`** in any query. Gmail silently returns zero results when this *negated* form is combined with other filters. The positive `category:primary` used here is safe and is the intended scope.
- **Verify with a re-list, not the "Marked as read N" output.** `gog gmail mark-read` echoes the count it attempted, not necessarily what changed. Trust counts only after re-running `gog gmail list "is:unread in:inbox category:primary"`.
- **Don't loop blindly.** If the remaining-unread count doesn't drop after a mark-read, stop and diagnose. A loop on a no-op query wastes time and quota.
- **Rate limits**: gog exits with code 7 on Gmail's `403 rateLimitExceeded` (250 units/min). Sleep 70s and retry once; don't chain shorter sleeps. With per-sender bulk queries this rarely triggers.
- **stderr separately**: redirect to a separate file, not `2>&1` into the JSON file.

## Examples

### Example: Open-ended triage
User says: "Triage my inbox" or "Help me clean up unread email"
Result: State account + scope (`is:unread in:inbox category:primary`) → fetch → report the thread count (matches Gmail's Primary badge) → cluster → present tiered recommendations → process clusters interactively.

### Example: Inbox-zero push
User says: "I want to get to inbox zero"
Result: Same workflow, but the wrap-up offers to draft replies for everything in the "maybe-action" and "one-off" tiers. "Inbox zero" here means zero unread in Primary.

### Example: Just mark obvious noise
User says: "Just clear the marketing junk"
Result: Within Primary, bulk-mark the auto-noise clusters (newsletters, receipts, notifications that slipped into Primary) by ID; leave maybe-action and one-off untouched.
