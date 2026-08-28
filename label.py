"""Interactive labeling tool for building the eval golden set.

Shows each analyzed item without its analyzer score (which would anchor your
judgement) and records your own relevance score.
"""

import argparse
import sys
import textwrap

from ai_monitor.config.settings import settings
from ai_monitor.eval import golden_set
from ai_monitor.storage import db

HELP = """
Score how relevant this item is to your interest areas:
  0.0-0.2  unrelated
  0.3-0.5  tangential or routine
  0.6-0.8  solidly relevant, worth reading
  0.9-1.0  must not miss

Commands: a number (0-1) to score, 's' to skip, 'n' to add a note first, 'q' to quit.
"""


def show(row, index: int, total: int) -> None:
    print("\n" + "=" * 72)
    print(f"[{index}/{total}] {row['source']} :: {row['source_id']}")
    print(f"\n{row['title']}\n")
    body = " ".join(row["content"].split())
    print(textwrap.fill(body[:900], width=72))
    if len(body) > 900:
        print("...")
    print(f"\n{row['url']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Label items for the eval golden set")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    conn = db.connect()
    rows = golden_set.unlabeled_items(conn, limit=args.limit)
    current = golden_set.stats(conn)

    if not rows:
        print(f"Nothing left to label. Golden set: {current['labeled']} items.")
        return 0

    print(f"Interest areas: {', '.join(settings.interests)}")
    print(f"Golden set so far: {current['labeled']} labeled.")
    print(HELP)

    labeled = 0
    for i, row in enumerate(rows, 1):
        show(row, i, len(rows))
        note = ""
        while True:
            try:
                answer = input("\nscore> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"\n\nStopped. Labeled {labeled} this session.")
                return 0

            if answer == "q":
                print(f"\nLabeled {labeled} this session.")
                return 0
            if answer == "s":
                break
            if answer == "n":
                note = input("note> ").strip()
                continue
            try:
                score = float(answer)
            except ValueError:
                print("Enter a number 0-1, or s/n/q.")
                continue
            if not 0.0 <= score <= 1.0:
                print("Score must be between 0 and 1.")
                continue

            golden_set.record_label(conn, row["id"], score, note)
            labeled += 1
            break

    final = golden_set.stats(conn)
    print(f"\nDone. Labeled {labeled} this session; {final['labeled']} total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
