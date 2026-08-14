"""Remove the synthetic accounts the pre-launch audit created in the production database.

WHY NOT delete_user(): that function ANONYMISES events rather than deleting them, because
it implements a real person exercising erasure — the ledgers derived from the event log are
meant to reset while the log itself survives. These 25 rows are not people. Anonymising 384
synthetic events would leave them in every count the log feeds, which is the thing being
cleaned up. They are deleted outright instead.

BY EXPLICIT ID, NEVER BY RANGE. Real Telegram chat ids are also above 9,000,000 —
357127133, 134026439, 731424910 — so `WHERE chat_id >= 9000000` would delete every real
learner on the service. The ids are listed one by one below and asserted against the set of
accounts that must survive.

Order matters: `PRAGMA foreign_keys` is 0 on this database, so the ON DELETE CASCADE
declarations do not fire. Children are deleted explicitly, parents last.

Run with --apply to commit. Without it, nothing is written.
"""
import sqlite3
import sys

DB = "/data/patente.db"

# The accounts three audit passes created: the eight-lens launch audit, the rankings recon,
# and the leaderboard fixtures.
AUDIT_IDS = [9000001, 9000002, 9000003, 9000004, 9000005, 9000006, 9000007, 9000008,
             9000009, 9000012, 9000013, 9000101, 9000102, 9000103, 9000104, 9000105,
             9000200, 9000201, 9000202, 9000203, 9000204, 9000205, 9000206, 9000207,
             9000401]

# Named, not derived. If a future audit adds an account and forgets to add it here, this
# script leaves it alone — which is the safe direction to fail in.
MUST_SURVIVE = [134026439, 357127133, 369821398, 453252730, 731424910,
                1228942039, 5785921166, 6574108964, 8671490207]

# One synthetic dictionary entry, from a word-lookup test. word_forms cascades off lemma,
# and again the cascade is not enforced, so it is deleted first.
TEST_LEMMA = "zzqxwv"

apply = "--apply" in sys.argv
db = sqlite3.connect(DB, timeout=15)
db.execute("PRAGMA busy_timeout = 15000")
q = ",".join(str(i) for i in AUDIT_IDS)


def count(sql, *a):
    return db.execute(sql, a).fetchone()[0]


# --- what the real learners have now, so it can be proved unchanged afterwards ---------
real_q = ",".join(str(i) for i in MUST_SURVIVE)
before = {}
for t in ("users", "progress", "events", "quiz_sessions", "league_score", "streak_days",
          "reports", "suggestions"):
    cols = [r[1] for r in db.execute("PRAGMA table_info(%s)" % t)]
    if "chat_id" in cols:
        before[t] = count("SELECT COUNT(*) FROM %s WHERE chat_id IN (%s)" % (t, real_q))

sessions = [r[0] for r in db.execute("SELECT id FROM quiz_sessions WHERE chat_id IN (%s)" % q)]
sq = ",".join(str(s) for s in sessions) or "-1"

PLAN = [
    ("quiz_session_items", "session_id IN (%s)" % sq),
    ("quiz_sessions",      "chat_id IN (%s)" % q),
    ("progress",           "chat_id IN (%s)" % q),
    ("vocab_progress",     "chat_id IN (%s)" % q),
    ("league_slot",        "chat_id IN (%s)" % q),
    ("league_day",         "chat_id IN (%s)" % q),
    ("league_score",       "chat_id IN (%s)" % q),
    ("streak_days",        "chat_id IN (%s)" % q),
    ("reports",            "chat_id IN (%s)" % q),
    ("suggestions",        "chat_id IN (%s)" % q),
    ("analyses",           "chat_id IN (%s)" % q),
    ("events",             "chat_id IN (%s)" % q),
    ("word_forms",         "lemma = '%s'" % TEST_LEMMA),
    ("word_glosses",       "lemma = '%s'" % TEST_LEMMA),
    ("users",              "chat_id IN (%s)" % q),
]

print("%-22s %8s" % ("table", "to delete"))
print("-" * 32)
total = 0
for table, where in PLAN:
    n = count("SELECT COUNT(*) FROM %s WHERE %s" % (table, where))
    total += n
    if n:
        print("%-22s %8d" % (table, n))
print("-" * 32)
print("%-22s %8d" % ("TOTAL", total))

# --- the guard rails ------------------------------------------------------------------
overlap = set(AUDIT_IDS) & set(MUST_SURVIVE)
assert not overlap, "an id is in both lists: %s" % overlap
doomed_users = [r[0] for r in db.execute("SELECT chat_id FROM users WHERE chat_id IN (%s)" % q)]
assert set(doomed_users) <= set(AUDIT_IDS), "about to delete an unlisted account"
print("\nwould delete %d accounts; %d real accounts untouched"
      % (len(doomed_users), len(MUST_SURVIVE)))

if not apply:
    print("\nDRY RUN — nothing written. Re-run with --apply.")
    raise SystemExit(0)

db.execute("BEGIN IMMEDIATE")
for table, where in PLAN:
    db.execute("DELETE FROM %s WHERE %s" % (table, where))

# Prove the real learners are intact BEFORE committing. A mistake here is only recoverable
# from a backup, and the whole point of a transaction is not to need one.
after = {}
for t in before:
    after[t] = count("SELECT COUNT(*) FROM %s WHERE chat_id IN (%s)" % (t, real_q))
if after != before:
    db.execute("ROLLBACK")
    raise SystemExit("ROLLED BACK — real learners' rows changed: %s -> %s" % (before, after))

left = count("SELECT COUNT(*) FROM users WHERE chat_id IN (%s)" % q)
if left:
    db.execute("ROLLBACK")
    raise SystemExit("ROLLED BACK — %d synthetic accounts survived the delete" % left)

db.execute("COMMIT")
print("\nCOMMITTED. real learners unchanged:", before)
print("users now:", count("SELECT COUNT(*) FROM users"))
