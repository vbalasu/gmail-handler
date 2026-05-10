---
name: gmail-inbox-triage
description: Analyze unread Gmail inbox, cluster by sender/topic, recommend bulk actions, and mark messages as read after processing. Use when the user says "triage my inbox", "clean up unread", "what's in my inbox", "process my unread email", "help me get to inbox zero", or wants help acting on unread mail without a pre-built checklist.
---

# Gmail Inbox Triage

Analyze the unread Gmail inbox, cluster messages, recommend per-cluster actions (bulk mark-read, drill in, reply), and mark messages as read after the user confirms each action. No checklist file required — the skill builds the picture from the inbox itself.

## When invoked with no arguments

Respond immediately with a short explainer (no tool calls, no inbox fetch). Roughly:

> **gmail-inbox-triage** — analyzes your unread Gmail, clusters messages by sender/topic, recommends bulk actions, and marks messages read after you approve. Marks the exact messages you reviewed — new mail that arrives during triage is left untouched.
>
> **Usage:** `/gmail-inbox-triage <optional instruction>` — e.g. "triage my inbox", "just clear marketing junk", "help me get to inbox zero", or "what's in my unread?"
>
> **Defaults:** account `vbalasu@gmail.com`, window `newer_than:3m`. Override either by saying so.

Then stop. Don't auto-run the workflow until the user gives a directive.

## Capabilities

- Fetch unread inbox via `gog gmail list` with sane recency scoping
- Cluster threads by sender/domain and category, surfacing the largest groups first
- Recommend per-cluster actions: bulk mark-read for newsletters/transactional/social noise, "needs attention" for action-required messages, "drill in" for ambiguous senders
- Bulk mark-read via `gog gmail mark-read --query` with verification by re-listing
- Help draft replies (delegates to `email-reply` skill) and mark replied threads read after sending

## Prerequisites

- `gog` CLI installed and authenticated for the target Gmail account (`gog gmail list --max 1 -a <account>`)

## Workflow

### Phase 0: Confirm scope
Confirm two things in one short message, then proceed:
1. **Account** — default `vbalasu@gmail.com` (personal). Briefly state the default; do not assume the Databricks work account.
2. **Recency window** — default `newer_than:3m`. For very full inboxes, suggest narrowing further. Bulk-cleaning years of inbox via the API is the wrong tool; point users to Gmail web UI ("select all → mark read") for that.

### Phase 1: Fetch and analyze
```bash
gog gmail list "is:unread in:inbox newer_than:3m" --max 1000 --json -a <account> > /tmp/triage_unread.json 2> /tmp/triage_err.log
```

**Always redirect stderr separately** — gog prints `403 rateLimitExceeded` and other errors to stderr; merging them into the JSON corrupts it.

If exit code is 7 (`rateLimitExceeded`, Gmail quota = 250 units/min), sleep 70s and retry once. Don't chain shorter sleeps.

If `nextPageToken` is non-empty after `--max 1000`, the inbox is too big — tell the user the count is `>1000` and recommend narrowing the recency window before continuing.

### Phase 2: Cluster and recommend
Group threads by:
- **Sender domain** (e.g., `@notify.wellsfargo.com`, `@linkedin.com`)
- **Gmail category** (`CATEGORY_UPDATES`, `CATEGORY_PROMOTIONS`, `CATEGORY_SOCIAL`, `CATEGORY_FORUMS`, `CATEGORY_PERSONAL`)
- **Topic patterns** in subjects (newsletters, receipts, alerts, security, social notifications)

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

Present like this — keep it scannable:

```
Found 87 unread (last 3 months). Recommendations:

AUTO-NOISE — recommend bulk mark-read (62 msgs):
  • LinkedIn job alerts × 18
  • Substack newsletters × 14 (Chamath, a16z, Nilesh Jasani)
  • PayPal/Amazon receipts × 12
  • Audible promotions × 8
  • True Classic, United, Delta marketing × 10

MAYBE-ACTION — review (19 msgs):
  • Wells Fargo balance alerts × 5
  • Kaiser Permanente care team × 4
  • AWS Lambda EOL notices × 3
  • OpenAI/Microsoft action-required × 4
  • UMN tuition reminders × 3

ONE-OFF (6 msgs):
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
After every mark-read call, **always** re-count rather than trusting the "Marked as read N" line (gog reports `--max` even on no-op queries):
```bash
gog gmail list "is:unread in:inbox newer_than:3m" --max 1 --json -a <account> 2>/dev/null | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('remaining unread:', len(d.get('threads',[])), 'more:', bool(d.get('nextPageToken')))"
```
Since mark-read is now ID-based, the count should drop by **exactly** `len(cluster_ids)`. If it didn't, diagnose before retrying — usually either the wrong cluster key was passed to jq, or another client (Gmail web/mobile) read messages concurrently.

### Phase 5: Wrap up
Summarize: total marked read, total remaining unread, what's left needing attention. Offer to draft replies for the "one-off" cluster.

## Resources

- `references/gog-cheatsheet.md`: Quick reference for the `gog` Gmail commands used.
- `scripts/triage.py`: Legacy end-to-end script (checklist-driven, kept for backwards-compat). The current workflow above is interactive and does not use this script.

## Important Constraints

- **Mark-read must be ID-based, not query-based.** `gog gmail mark-read --query '...'` re-evaluates the query against the live inbox at execution time, so any message that arrived after the Phase 1 snapshot and matches the query gets marked read without the user seeing it. Always pass positional message IDs from `/tmp/triage_clusters.json` instead. Use `--query` only for *listing* (Phase 1) and *verification* (Phase 4), never for mutation.
- **Scope correctly when listing**: always anchor with `in:inbox` and a recency filter (`newer_than:3m`). Without `in:inbox`, queries match archived mail too — this previously caused a 41K-message runaway.
- **Never use `-category:primary`** in any list/verify query. Gmail silently returns zero results when this is combined with other filters. Use positive forms: `category:updates`, `category:promotions`, or `(category:updates OR category:promotions OR category:social OR category:forums)`.
- **Verify with a re-list, not the "Marked as read N" output.** `gog gmail mark-read` echoes the count it attempted, not necessarily what changed. Trust counts only after re-running `gog gmail list "is:unread in:inbox …"`.
- **Don't loop blindly.** If the remaining-unread count doesn't drop after a mark-read, stop and diagnose. A loop on a no-op query wastes time and quota.
- **Rate limits**: gog exits with code 7 on Gmail's `403 rateLimitExceeded` (250 units/min). Sleep 70s and retry once; don't chain shorter sleeps. With per-sender bulk queries this rarely triggers.
- **stderr separately**: redirect to a separate file, not `2>&1` into the JSON file.

## Examples

### Example: Open-ended triage
User says: "Triage my inbox" or "Help me clean up unread email"
Result: Confirm account + window → fetch → cluster → present tiered recommendations → process clusters interactively.

### Example: Inbox-zero push
User says: "I want to get to inbox zero"
Result: Same workflow, but the wrap-up offers to draft replies for everything in the "maybe-action" and "one-off" tiers.

### Example: Just mark obvious noise
User says: "Just clear the marketing junk"
Result: Skip clustering for "maybe-action" tier; bulk-mark `category:promotions` plus known marketing senders, leave the rest untouched.
