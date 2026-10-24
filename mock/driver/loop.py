"""Optional schedule driver:  python -m driver.loop  - ticks the five-minute sweep every MOCK_SWEEP_SECONDS (default 20)."""

import os
import time

import requests


def main():
    base = os.environ.get("MOCK_EDGE_URL", "http://edge:4566")
    every = float(os.environ.get("MOCK_SWEEP_SECONDS", "20"))
    while True:
        time.sleep(every)
        try:
            requests.post(base + "/__mock__/tick/sweep", timeout=330)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
