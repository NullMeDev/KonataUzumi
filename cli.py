#!/usr/bin/env python3
import time
import argparse
from main import main

def run():
    parser = argparse.ArgumentParser(
        description="Run the Political News Bot (live mode)"
    )
    parser.add_argument(
        "--interval", type=int, default=24,
        help="Poll interval in minutes (default: 24)"
    )
    args = parser.parse_args()

    while True:
        main()  # no dry_run, no extra flags
        time.sleep(args.interval * 60)

if __name__ == "__main__":
    run()
