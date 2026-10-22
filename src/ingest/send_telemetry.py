"""
Send telemetry from a CSV file to the ingestion API in batches.

Run (server must be running: uvicorn src.api.main:app):
    python -m src.ingest.send_telemetry [--file data/telemetry_logs.csv]
                                        [--url http://127.0.0.1:8000] [--batch-size 5000]

Safe to run again: rows already stored come back as duplicates, nothing is doubled.
Exit code: 0 when every row was stored or was a duplicate, 1 when any row was
rejected, 2 when the server could not be reached.
"""
import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Protocol

import httpx

log = logging.getLogger("send_telemetry")

ENDPOINT = "/telemetry/batch"


class PostClient(Protocol):
    """Anything with .post(path, json=...) -> response: httpx.Client or FastAPI TestClient."""

    def post(self, url: str, json: object): ...


@dataclass
class SendTotals:
    sent: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    batches: int = 0
    # first few rejections, as (row number in the file, reasons)
    examples: list[tuple[int, list[str]]] = field(default_factory=list)


def read_rows(path: Path) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def chunks(rows: Iterable[dict], size: int) -> Iterator[list[dict]]:
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def send_rows(client: PostClient, rows: Iterable[dict], batch_size: int = 5000) -> SendTotals:
    totals = SendTotals()
    offset = 0
    for batch in chunks(rows, batch_size):
        resp = client.post(ENDPOINT, json=batch)
        if resp.status_code != 200:
            raise RuntimeError(f"batch {totals.batches + 1} failed: HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        totals.batches += 1
        totals.sent += body["received"]
        totals.accepted += body["accepted"]
        totals.duplicates += body["duplicates"]
        totals.rejected += len(body["rejected"])
        for r in body["rejected"]:
            if len(totals.examples) < 5:
                # +2: header line + 1-based numbering, so it matches the CSV line in an editor
                totals.examples.append((offset + r["index"] + 2, r["errors"]))
        offset += len(batch)
        log.info("batch %d: sent=%d accepted=%d duplicates=%d rejected=%d",
                 totals.batches, body["received"], body["accepted"], body["duplicates"], len(body["rejected"]))
    return totals


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--file", default="data/telemetry_logs.csv", type=Path)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--batch-size", default=5000, type=int)
    args = ap.parse_args()

    if not args.file.exists():
        log.error("%s not found. Create it first: python -m src.simulator.run", args.file)
        return 2

    started = time.perf_counter()
    try:
        with httpx.Client(base_url=args.url, timeout=60) as client:
            client.get("/health").raise_for_status()
            totals = send_rows(client, read_rows(args.file), args.batch_size)
    except httpx.ConnectError:
        log.error("cannot reach %s. Start the server first: uvicorn src.api.main:app", args.url)
        return 2
    except RuntimeError as exc:  # server answered with an error status
        log.error("%s", exc)
        return 2

    seconds = time.perf_counter() - started
    log.info("done in %.1f s: sent=%d accepted=%d duplicates=%d rejected=%d (%d batches)",
             seconds, totals.sent, totals.accepted, totals.duplicates, totals.rejected, totals.batches)
    for line_no, errors in totals.examples:
        log.warning("CSV line %d rejected: %s", line_no, "; ".join(errors))
    return 1 if totals.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
