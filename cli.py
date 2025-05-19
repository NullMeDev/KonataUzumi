#!/usr/bin/env python3
import argparse
import time
from main import main

def run():
    parser = argparse.ArgumentParser(description="Run political news scraper")
    parser.add_argument(
        "--interval", type=int, default=24,
        help="Poll interval in minutes (default: 24)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print summaries without posting"
    )
    args = parser.parse_args()

    while True:
        main(dry_run=args.dry_run)
        time.sleep(args.interval * 60)

if __name__ == "__main__":
    run()
