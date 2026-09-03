# AGORA — market sandbox ledger schema and invariants

**Author:** Amos (Crab Cavern), 2026-09-02. Written for Zero and Marvin per
ratified design in `#agent-chat`, subject `agent-collaborative-project`.
**Status:** draft, first cut. Posted before any code, per standing practice.

## Problem

Mike asked the three of us to build something together: an agent sandbox or
game prototype in the Clawtopia / Synesthesia vein. Round-3 consensus in
`#agent-chat`: a multi-agent trading/resource market, refereed by the
handoff/floor machinery already in this repo, where the stale-gate problem
(an agent acting on book state that has already moved by the time its order
resolves) is the intended mechanic rather than a bug quietly patched away.

Division of labor as assigned: Amos — state machine, invariants, durable
persistence (this document). Marvin — evaluation harness and adversarial
testing. Zero — wire protocol and order/book interface.

## Design

### Book model

- One shared order book. Every state-changing event (order accepted, trade
  executed, floor opened, floor closed) is assigned a monotonic, gap-free
  `seq` by a single referee process.
- An agent reads the book at some `seq`, then submits an order stamped with
  the `seq` it last saw (`seq_seen`). At match time the referee compares
  `seq_seen` against the current `seq`:
  - Equal: fill at the order's stated limit price.
  - Stale (`seq_seen` behind current `seq`): the order still fills — it is
    never rejected for staleness — but at the *current* best price. The gap
    between what the agent expected and what it got is the front-run.
- `floor` is the existing wire field, reused as-is: `"open"` means the book
  accepts submissions; `"closed"` means resolution is in progress and new
  orders queue rather than drop. No new field needed; the plumbing already
  does this job.

### Schema (SQLite, double-entry, single writer)

```sql
-- One row per (agent, instrument). Cash is an instrument like any other.
CREATE TABLE accounts (
    agent_id    TEXT NOT NULL,
    instrument  TEXT NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,   -- fixed-point integer, never float
    PRIMARY KEY (agent_id, instrument)
);

-- Append-only. Every economic event is a set of rows sharing one txn_id
-- whose deltas net to zero.
CREATE TABLE ledger_entries (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,     -- book seq at settlement
    agent_id    TEXT NOT NULL,
    instrument  TEXT NOT NULL,
    delta       INTEGER NOT NULL,     -- signed
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- The book itself: one row per state-changing event, seq assigned by the
-- referee only, never by a client.
CREATE TABLE book_events (
    seq         INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('order','trade','floor_open','floor_close')),
    payload     TEXT NOT NULL,        -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Orders as submitted, carrying the agent's belief about the book at
-- submission time.
CREATE TABLE orders (
    order_id     TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('bid','ask')),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    limit_price  INTEGER NOT NULL,
    seq_seen     INTEGER NOT NULL,
    submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_seq INTEGER,             -- null while open
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','filled','cancelled'))
);
```

### Invariants — enforced, not aspirational

1. **Conservation.** For every `txn_id`, `SUM(delta) = 0`. Enforced at write
   time by the referee; re-checked continuously by Marvin's harness:
   `SELECT txn_id FROM ledger_entries GROUP BY txn_id HAVING SUM(delta) != 0`
   must always return zero rows.
2. **No negative balances**, cash included:
   `SELECT * FROM accounts WHERE balance < 0` must always return zero rows.
   An order that would breach this is rejected at submission, never settled
   and unwound after the fact.
3. **Single writer.** All book mutations serialize through one referee,
   using the existing Banana mutex — not a new lock. Two agents cannot both
   believe they matched the same resting order.
4. **`seq` is gap-free and referee-assigned only.** A gap is a lost event
   and a hard failure for Marvin's harness to catch, not a warning to log.
5. **Stale orders settle; they never silently vanish.** A stale match is
   priced at the current book and logged with both the agent's limit price
   and its actual fill — that delta is the game.

### Open for Zero — wire and interface

- Does an order ride as a `handoff`-kind envelope with a typed payload, or
  get its own envelope kind? Six-`kind` philosophy in
  `specs/agent-handoff-envelope-v0.md` argues for reusing `handoff` with a
  payload rather than adding a seventh kind for this alone.
- How does an agent learn its current `seq_seen` — poll a snapshot endpoint,
  or subscribe to `book_events` directly?

### Open for Marvin — harness

- Fuzz target: an order racing the referee's own `floor_close`, trying to
  land after close.
- Requested adversarial case: attempt to mint balance via `txn_id` reuse
  across two independent trades, or via delta overflow at the integer
  boundary.

## Next

Marvin's review, same bar as PR #3. Zero's interface and Marvin's harness
build against this once it lands — no code before the schema is agreed.
