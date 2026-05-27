"""One-time script: reclassify existing articles and recompute is_highlighted."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.classification.classifier import classify
from app.ranking.ranker import _should_highlight
from app.database import SessionLocal, init_db
from app.models import Article

# Reclassify these verticals — either legacy values or the fallback 'industry'
# which may have been assigned when keywords didn't match
RECLASSIFY_VERTICALS = {"tech", "business", "both", "industry"}

def main():
    init_db()
    db = SessionLocal()

    articles = db.query(Article).all()
    reclassified = 0
    highlighted = 0

    for a in articles:
        if a.vertical in RECLASSIFY_VERTICALS:
            a.vertical = classify(a.title or "", a.summary)
            reclassified += 1

        a.is_highlighted = _should_highlight(a.vertical or "industry", a.rank_score or 0.0)
        if a.is_highlighted:
            highlighted += 1

    db.commit()
    db.close()
    print(f"Reclassified: {reclassified}  |  Highlighted: {highlighted}  |  Total: {len(articles)}")

if __name__ == "__main__":
    main()
