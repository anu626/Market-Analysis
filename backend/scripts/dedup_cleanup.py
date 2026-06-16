"""Deduplication cleanup script for the Market Analysis DB.

Finds articles that describe the same news story but landed with different
story_hashes (because their titles were phrased differently across sources).

Default mode: dry-run — prints what it would do, touches nothing.

Actions (choose one):
  --fix-hashes   Unify story_hashes within each duplicate group so the
                 display-level dedup in routes.py can merge them into one card.
                 Non-destructive: all article rows are preserved.
  --delete       Delete the lower-ranked duplicates, keeping one winner per
                 story group. Destructive — back up the DB first.

Usage:
  # from the backend/ directory
  python scripts/dedup_cleanup.py                       # dry-run, last 7 days
  python scripts/dedup_cleanup.py --window-days 14      # scan 2 weeks
  python scripts/dedup_cleanup.py --threshold 85        # stricter matching
  python scripts/dedup_cleanup.py --fix-hashes          # unify story_hashes
  python scripts/dedup_cleanup.py --delete              # delete dupes (be careful)
"""

import argparse
import sys
import os
import hashlib
import re
import logging
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Allow running from backend/ without installing the package
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from rapidfuzz import fuzz
except ImportError:
    sys.exit("rapidfuzz is not installed. Run: pip install rapidfuzz==3.9.3")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Quality rank for picking the "winner" in each duplicate group.
# Higher is better.
# ---------------------------------------------------------------------------
def _quality_rank(row) -> tuple:
    """Returns a comparable tuple (higher = better article to keep)."""
    has_ai_image  = 1 if row.ai_image_url else 0
    has_ai_title  = 1 if row.ai_title else 0
    has_ai_summary = 1 if row.ai_summary else 0
    return (has_ai_image, has_ai_title, has_ai_summary, row.rank_score or 0.0)


def _title_for(row) -> str:
    return (row.ai_title or row.title or "").lower()


# ---------------------------------------------------------------------------
# Core duplicate detection (mirrors _dedup_by_story logic)
# ---------------------------------------------------------------------------
def find_duplicate_groups(session, window_days: int, threshold: int) -> list[list]:
    """Returns list of groups; each group is a list of article rows ordered
    best-first (winner at index 0)."""
    from app.models import Article

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    rows = (
        session.query(Article)
        .filter(Article.created_at >= cutoff)
        .order_by(Article.rank_score.desc())
        .all()
    )
    log.info("Loaded %d articles from last %d days", len(rows), window_days)

    # Stage 1: group by story_hash
    by_hash: dict[str, list] = {}
    for r in rows:
        key = r.story_hash or str(r.id)
        by_hash.setdefault(key, []).append(r)

    log.info("Unique story_hash groups: %d", len(by_hash))

    # Stage 2: fuzzy second pass across groups
    keys = list(by_hash.keys())
    absorbed: dict[str, str] = {}  # absorbed_key -> canonical_key

    for i in range(len(keys)):
        ki = keys[i]
        if ki in absorbed:
            continue
        ti = _title_for(by_hash[ki][0])
        for j in range(i + 1, len(keys)):
            kj = keys[j]
            if kj in absorbed:
                continue
            tj = _title_for(by_hash[kj][0])
            if fuzz.token_set_ratio(ti, tj) >= threshold:
                # Merge kj into ki
                by_hash[ki].extend(by_hash[kj])
                absorbed[kj] = ki

    # Collect only groups that contain 2+ distinct articles
    dup_groups = []
    for key, articles in by_hash.items():
        if key in absorbed:
            continue
        if len(articles) < 2:
            continue
        # Sort best-first
        articles.sort(key=_quality_rank, reverse=True)
        dup_groups.append(articles)

    return dup_groups


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def action_dry_run(groups: list[list]) -> None:
    total_dupes = sum(len(g) - 1 for g in groups)
    print(f"\n{'='*72}")
    print(f"  DRY RUN — {len(groups)} duplicate groups, {total_dupes} articles to act on")
    print(f"{'='*72}\n")

    for i, group in enumerate(groups, 1):
        winner = group[0]
        print(f"Group {i:>3}  ({len(group)} articles)")
        print(f"  KEEP  [{winner.source_name}]  \"{(winner.ai_title or winner.title)[:80]}\"")
        print(f"        rank={winner.rank_score:.3f}  hash={winner.story_hash}  id={winner.id}")
        for dupe in group[1:]:
            print(f"  dup   [{dupe.source_name}]  \"{(dupe.ai_title or dupe.title)[:80]}\"")
            print(f"        rank={dupe.rank_score:.3f}  hash={dupe.story_hash}  id={dupe.id}")
        print()

    print(f"Run with --fix-hashes to unify story_hashes (non-destructive).")
    print(f"Run with --delete to remove duplicate rows (destructive).")


def action_fix_hashes(session, groups: list[list]) -> None:
    """Assign the winner's story_hash to every article in the group so the
    display-level dedup in routes.py collapses them into one card."""
    from app.models import Article

    updated = 0
    for group in groups:
        winner = group[0]
        canonical_hash = winner.story_hash
        if not canonical_hash:
            # Generate one from the winner's title if missing
            words = re.sub(r"[^a-z0-9\s]", "", (winner.title or "").lower()).split()
            _STOPWORDS = frozenset("a an the and or but in on at to for of is are was were be been have has had do does did will would could should may might can this that with from by as how what who which when where why its it not no new also just".split())
            kws = sorted(
                (w for w in words if w not in _STOPWORDS and len(w) > 2 and not w.isdigit()),
                key=lambda w: (-len(w), w),
            )[:5]
            canonical_hash = hashlib.md5(" ".join(kws).encode()).hexdigest()[:12]
            winner.story_hash = canonical_hash

        for article in group[1:]:
            if article.story_hash != canonical_hash:
                log.debug(
                    "  id=%d  %s → %s", article.id, article.story_hash, canonical_hash
                )
                article.story_hash = canonical_hash
                updated += 1

    session.commit()
    log.info("story_hash unified for %d articles across %d groups.", updated, len(groups))


def action_delete(session, groups: list[list]) -> None:
    """Delete all articles in each group except the winner."""
    from app.models import Article

    ids_to_delete = [a.id for g in groups for a in g[1:]]
    if not ids_to_delete:
        log.info("Nothing to delete.")
        return

    log.info("Deleting %d duplicate articles...", len(ids_to_delete))
    session.query(Article).filter(Article.id.in_(ids_to_delete)).delete(
        synchronize_session=False
    )
    session.commit()
    log.info("Deleted %d articles.", len(ids_to_delete))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find and remove duplicate articles in the Market Analysis DB."
    )
    parser.add_argument(
        "--window-days", type=int, default=7,
        help="How many days back to scan (default: 7)",
    )
    parser.add_argument(
        "--threshold", type=int, default=80,
        help="token_set_ratio threshold for same-story detection (default: 80)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fix-hashes", action="store_true",
        help="Unify story_hashes so display-level dedup collapses duplicates. Non-destructive.",
    )
    mode.add_argument(
        "--delete", action="store_true",
        help="Delete lower-ranked duplicate rows. DESTRUCTIVE — back up DB first.",
    )
    args = parser.parse_args()

    from app.database import init_db
    init_db()  # run pending column migrations before querying

    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {},
    )
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        groups = find_duplicate_groups(session, args.window_days, args.threshold)
        log.info("Found %d duplicate groups.", len(groups))

        if not groups:
            print("No duplicates found. DB is clean.")
            return

        if args.fix_hashes:
            # Still print summary first
            action_dry_run(groups)
            print("\n--- Executing: unify story_hashes ---\n")
            action_fix_hashes(session, groups)
        elif args.delete:
            action_dry_run(groups)
            print("\n--- Executing: delete duplicate rows ---\n")
            confirm = input("Type 'yes' to confirm deletion: ").strip().lower()
            if confirm != "yes":
                print("Aborted.")
                sys.exit(0)
            action_delete(session, groups)
        else:
            action_dry_run(groups)

    finally:
        session.close()


if __name__ == "__main__":
    main()
