# Design: evidence-based inbox automation

*Why this tool looks the way it does — the architecture, the safety model, and the five
incidents that forced both.*

## 1. The problem

An inbox with ~8,000 messages, ~1,000 of them unread, growing daily. The first version of
this tool (early 2026) took the obvious approach: a hardcoded routing table of senders to
archive, Gmail filters to hide them at delivery, and an automated unsubscribe pass for
anything with bulk-mail headers.

It worked, in the sense that the inbox shrank. It also:

- hid a daily newsletter its owner read 40% of the time, because the routing table said
  "newsletter, skip inbox" and nothing ever checked that claim against behavior;
- sent unsubscribe requests to three editions of that same newsletter automatically;
- silently processed one mailbox twice for months while believing it was processing two.

The lesson that drives everything below: **hardcoded rules drift from reality, and
automation without evidence amplifies the drift.** The current system makes decisions from
observed engagement, gates every irreversible action, and audits its own behavior so its
actions never contaminate the evidence.

## 2. Decision model

Every unread inbox message passes through ordered gates; the first to claim it wins.
The order is a trust hierarchy — explicit user intent first, then safety nets, then
behavioral evidence, then static configuration, then heuristics:

| # | Gate | Outcome | Rationale |
|---|---|---|---|
| 1 | keep-list (`keep_senders.txt`) | stays in inbox | explicit "always show me" beats everything |
| 2 | block-list (`blocked_senders.txt`) | trash | explicit "never show me," domain-aware |
| 3 | flag keywords (invoice, lease, medical…) | stays | possible documents deserve human eyes |
| 4 | age-out (unread > 30 days) | Archive/Cleanup | month-old unread is empirically never read |
| 5 | engagement override (read rate ≥ 30% over ≥ 25 msgs) | stays | behavior outranks the routing table |
| 6 | routing table (`senders.json`) | Archive/\<category\> | curated filing for known senders |
| 7 | bulk headers (List-Unsubscribe / Precedence) | Archive/Marketing | unknown marketing self-identifies |
| 8 | none | stays in inbox | unknown non-bulk mail is presumed personal |

Gate 5 has one exception, added after receipts piled up under its protection: senders in
the notification tier (banks, shipping, statements) bypass the engagement override. The
app already notified you; the email is only the record. Reading your Venmo receipts does
not mean you want 27 of them sitting in your inbox.

The gauntlet is a pure function (`plan_inbox_archive`) taking message metadata, the lists,
and sender statistics, returning a routing plan. No I/O, no service objects — which is why
most of the test suite needs no mocks at all.

## 3. External effects and their gates

| Effect | Gate |
|---|---|
| Archive / trash | reversible by construction (labels; trash has 30-day recovery) |
| Gmail filters | rebuilt from config by an explicit phase; block filter derived from the block-list |
| Unsubscribe requests | two-phase: the scheduled job only *builds* candidates; a separate command shows the list, asks for confirmation, sends, and records every sender in a permanent ledger so nothing is ever asked twice |
| OAuth | token identity verified against the account it claims to be; interactive flows refused in headless runs |

Candidates for unsubscribing are themselves evidence-filtered: only senders active in the
last 60 days qualify (dead senders age out), the keep-list and ledger are excluded, and an
engagement triage buckets the rest — under 5% read over 15+ messages is a clear ignore,
over 25% is dropped as "you actually read this," and only the middle band goes to a human.

## 4. Evidence integrity

The engagement statistics that power gate 5 are only as good as their inputs, which is why
the system audits itself:

- Every tool action (archive, trash) is recorded in a `decisions` table with who decided
  (`weekly_archive`) and what (`AUTO_ARCHIVE`, `AUTO_TRASH`).
- Sender read-rate statistics **exclude tool-archived messages**. Without this, archiving
  marks mail as read, which raises the sender's read rate, which makes the system more
  confident the sender is fine to archive — a feedback loop that was live in production
  before it was caught (§5.1).

## 5. Incidents

Every safety mechanism above exists because of a specific failure. These are the five
worth writing down.

### 5.1 The engagement feedback loop

**Symptom:** sender read-rates looked implausibly high for senders nobody read.
**Root cause:** the archive pass marked archived messages `is_read=true` in the local
database — so every robot action counted as human engagement, compounding weekly.
**Fix:** actions moved to the audit table; statistics exclude audited message IDs.
**Test:** seeds a sender with genuine and robot reads, asserts the robot read is excluded.

### 5.2 The wrong-mailbox token

**Symptom:** "both accounts" runs produced identical numbers for both accounts.
**Root cause:** an OAuth token file named for account B had been authorized as account A —
Google's consent screen defaults to the browser's active session, one click approved the
wrong identity, and nothing ever checked. The second mailbox had *never* been processed.
**Fix:** `get_service` calls `getProfile` and raises if the authorized address doesn't
match the requested account. Silent wrong-mailbox processing became a loud failure.

### 5.3 The 43-day hang

**Symptom:** the weekly launchd job stopped running; no errors anywhere.
**Root cause:** a run had hit a missing token and launched the interactive OAuth browser
flow — in a headless context, where it blocked forever. launchd skips a scheduled job
while the previous instance lives, so one hung run silently disabled the schedule for six
weeks.
**Fix:** headless runs (no TTY) refuse to start OAuth and fail fast with instructions; an
explicit environment variable permits supervised re-auth from a non-TTY shell.

### 5.4 Silent batch attrition

**Symptom:** the sync phase listed 1,110 unread messages and cached 619 — no errors.
**Root cause:** Gmail batch requests can fail *per item* (rate limiting) while the batch
call itself succeeds; failed items were simply dropped, so up to 45% of the inbox vanished
from the decision set on a bad day.
**Fix:** retry rounds re-request stragglers with shrinking batch sizes and growing pauses
until none remain, with a loud warning if any survive. A fake batch that fails items on
first attempt pins the behavior in tests.

### 5.5 Overwritten history

**Symptom:** senders already unsubscribed kept reappearing as candidates.
**Root cause:** send results were written to a single file, clobbered on every send —
the system had no durable memory of what it had already done.
**Fix:** a cumulative append-only ledger of unsubscribed senders, consulted at candidate
build time, plus the 60-day activity window that lets silenced senders age out naturally.

## 6. Testing

78 tests, all hermetic (CI runs them with only the example configs):

- **Pure gauntlet tests** exercise every gate and every precedence pair that has ever
  mattered (keep beats block, keep beats bulk-headers, age-out beats engagement, …).
- **Fake Gmail service** records every mutation, so execution tests assert the exact API
  bodies — including that protected messages appear in *no* batch.
- **Negative-path gates**: the unsubscribe build phase is tested to send *nothing*, the
  send phase to abort without confirmation, and a stale pending list to re-filter against
  the current keep-list at send time.
- **Regression tests** for each incident in §5.

## 7. Results

Over roughly ten weeks of operation on the primary account: inbox from ~8,000 messages
(~1,050 unread) to a few hundred, all reversible; 250+ unsubscribes sent, every one
user-approved; a political spam network (12 domains, ~1,400 messages) identified by
volume-and-engagement evidence, purged, and blocked at delivery; and a weekly schedule
that maintains the state unattended on two accounts.

## 8. Known limitations

- A legacy full-corpus classifier (`main.py`, rule tiers + LLM) predates the live pipeline
  and is kept unscheduled for occasional batch work; the two share the database but not
  decision logic.
- `reply_count` in sender statistics is never populated (Sent-folder analysis was never
  built), so "has replied" plays no role in decisions.
- Historical `emails.date` values are stored in mixed formats; the live pipeline uses
  Gmail's `internalDate` and is unaffected.
- Single-user by design: no concurrency control beyond SQLite's, no multi-tenant anything.
