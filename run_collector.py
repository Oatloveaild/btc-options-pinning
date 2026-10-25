import sys
from src.collector import run_snapshot
from src.db import init_db
from src.settle import run_settlement


def main():
    init_db()
    action = sys.argv[1] if len(sys.argv) > 1 else "manual"

    if action == "settle":
        target_date = sys.argv[2] if len(sys.argv) > 2 else None
        print(f"Running settlement for date {target_date or 'today'}...")
        res = run_settlement(target_date)
        print("Result:", res)
    else:
        print(f"Running snapshot with label '{action}'...")
        res = run_snapshot(label=action)
        print("Result:", res)


if __name__ == "__main__":
    main()
