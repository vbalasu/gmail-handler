#!/usr/bin/env python3
"""Mark unread Gmail inbox threads as read in bulk, except those matching an action checklist.

Usage:
    triage.py --account <email> [--checklist ./action-checklist.md] [--scope category:primary] [--dry-run]
"""
import argparse, json, re, subprocess, sys, time
from pathlib import Path


def gog(*args, account):
    cmd = ['gog', *args, '-a', account]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"gog failed: {r.stderr.strip()[:300]}")
    return r.stdout


def parse_checklist(path: Path):
    """Extract sender emails and entity-name keywords from a markdown checklist.

    Heuristic: any email address, plus the entity portion of bold "**X**" runs in
    list items. Bold runs are split on em-dash/slash/colon/parens to capture each
    distinct entity name (e.g., "Tom Linton / Atlan" -> {"tom linton", "atlan"}).
    """
    text = path.read_text()
    senders, keywords = set(), set()
    SPLIT = re.compile(r'\s*[—–\-/:|()"]\s*')
    for line in text.splitlines():
        if not line.lstrip().startswith(('-', '*')):
            continue
        for email in re.findall(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', line, re.I):
            senders.add(email.lower())
        for run in re.findall(r'\*\*([^*]+)\*\*', line):
            for piece in SPLIT.split(run):
                piece = piece.strip().lower().rstrip('.,;!')
                if len(piece) < 4 or piece.startswith(('is ', 'has ', 'have ', 'will ', 'are ', 'was ')):
                    continue
                keywords.add(piece)
                # Also add distinctive single words (≥6 chars) — catches brand
                # names that show up in sender domains, e.g., "uipath", "anthropic".
                for word in re.findall(r'[a-z]{6,}', piece):
                    if word not in {'account', 'ready', 'approved', 'pending', 'hidden'}:
                        keywords.add(word)
    return senders, keywords


def matches_checklist(thread, senders, keywords):
    sender = (thread.get('from') or '').lower()
    subject = (thread.get('subject') or '').lower()
    for s in senders:
        if s in sender:
            return True
    for kw in keywords:
        if kw in subject or kw in sender:
            return True
    return False


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--checklist', default='./action-checklist.md')
    ap.add_argument('--scope', default='category:primary',
                    help='Extra Gmail query (default: category:primary). Pass empty string to triage all categories.')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    checklist_path = Path(args.checklist)
    if not checklist_path.exists():
        print(f"Checklist not found: {checklist_path}", file=sys.stderr)
        sys.exit(1)

    senders, keywords = parse_checklist(checklist_path)
    print(f"Checklist signals: {len(senders)} senders, {len(keywords)} keywords", file=sys.stderr)

    query = f"is:unread in:inbox {args.scope}".strip()

    # Fetch all unread threads (one --all paginated call)
    print(f"Fetching threads matching: {query!r}", file=sys.stderr)
    out = gog('gmail', 'list', query, '--json', '--all', account=args.account)
    data = json.loads(out)
    threads = data.get('threads', []) if isinstance(data, dict) else data
    print(f"Total unread threads: {len(threads)}", file=sys.stderr)

    keep, mark_read_threads = [], []
    for t in threads:
        (keep if matches_checklist(t, senders, keywords) else mark_read_threads).append(t)

    print(f"Keep unread: {len(keep)}", file=sys.stderr)
    print(f"Mark as read: {len(mark_read_threads)}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] Would mark as read:")
        for t in mark_read_threads:
            print(f"  {t['id']}  {t.get('subject','')[:70]}")
        return

    if not mark_read_threads:
        print("Nothing to do.")
        return

    # Get message IDs for the threads to mark read
    print("Fetching message IDs...", file=sys.stderr)
    msg_out = gog('gmail', 'messages', 'search', query, '--json', '--all', account=args.account)
    msg_data = json.loads(msg_out)
    messages = msg_data.get('messages', []) if isinstance(msg_data, dict) else msg_data

    keep_thread_ids = {t['id'] for t in keep}
    msg_ids = [m['id'] for m in messages if m.get('threadId') not in keep_thread_ids]
    print(f"Marking {len(msg_ids)} messages across {len(mark_read_threads)} threads as read...", file=sys.stderr)

    ok = 0
    for batch in chunked(msg_ids, 1000):
        gog('gmail', 'batch', 'modify', *batch, '--remove=UNREAD', '-y', account=args.account)
        ok += len(batch)
        print(f"  Batched {ok}/{len(msg_ids)}", file=sys.stderr)
        time.sleep(15)  # Stay under 250 quota units/minute (batchModify = 50 each)

    print(f"\nDone: {ok} messages marked read across {len(mark_read_threads)} threads.")


if __name__ == '__main__':
    main()
