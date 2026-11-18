#!/usr/bin/env python3
"""Deterministic reproduction of the GraphQL schema refresh race.

The test holds public.rindexer_repro_barrier closed, then:

1. Wait until PostGraphile finishes its initial introspection (T1).
2. Wait until it is blocked before LISTEN (T2).
3. Release constraint application and wait for the foreign key COMMIT (T4).
4. Release LISTEN and wait until PostgreSQL confirms it (T3).
5. Check that Approval.transferByValue is absent.

Nothing is read from the environment. Postgres is the compose service
`postgresql` in this directory.

    python3 find_refresh_bug.py
"""

import json
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = Path(__file__).resolve().parent
READY = "GraphQL API ready at http://localhost:"
MARKERS = {
    "T1": "rindexer repro: T1 introspection done",
    "T2": "rindexer repro: T2 before LISTEN",
    "T3": "rindexer repro: T3 LISTEN confirmed",
    "T4": "rindexer repro: T4 foreign key committed",
}
LABELS = {
    "T1": "introspection done",
    "T2": "before LISTEN",
    "T3": "LISTEN confirmed",
    "T4": "foreign key committed",
    "T5": "schema check",
}
BOOT_TIMEOUT_SECONDS = 90.0

CREATE_BARRIER = """
DROP TABLE IF EXISTS public.rindexer_repro_barrier;
CREATE TABLE public.rindexer_repro_barrier (
    id int PRIMARY KEY,
    release_apply boolean NOT NULL,
    release_listen boolean NOT NULL
);
INSERT INTO public.rindexer_repro_barrier (id, release_apply, release_listen)
VALUES (1, false, false);
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT n.nspname, c.relname, con.conname
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE con.contype = 'f'
          AND n.nspname = 'graphql_refresh_repro_rocket_pool_eth'
    LOOP
        EXECUTE format('ALTER TABLE %I.%I DROP CONSTRAINT %I', r.nspname, r.relname, r.conname);
    END LOOP;
END $$;
"""
DROP_BARRIER = "DROP TABLE IF EXISTS public.rindexer_repro_barrier;"
RELEASE_APPLY = "UPDATE public.rindexer_repro_barrier SET release_apply = true WHERE id = 1;"
RELEASE_LISTEN = "UPDATE public.rindexer_repro_barrier SET release_listen = true WHERE id = 1;"


def rindexer_binary() -> str:
    debug = ROOT / "target" / "debug" / "rindexer_cli"
    if debug.exists():
        return str(debug)
    sys.exit(f"missing {debug}")


def psql(sql: str) -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgresql",
            "psql",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        cwd=PROJECT,
        check=True,
    )


def kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def approval_fields(port: int) -> list[str] | None:
    body = json.dumps({"query": '{ __type(name: "Approval") { fields { name } } }'}).encode()
    request = urllib.request.Request(
        f"http://localhost:{port}/graphql",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    type_info = (payload.get("data") or {}).get("__type")
    if not type_info:
        return []
    return [field["name"] for field in type_info.get("fields") or []]


def print_order(events: dict[str, float], port: int, has_relation: bool) -> None:
    ordered = sorted(events, key=events.get)
    origin = events[ordered[0]]
    for name in ordered:
        print(f"  {name} {LABELS[name]:<24} +{(events[name] - origin) * 1000:.0f}ms", flush=True)
    print(f"  order: {' -> '.join(ordered)}", flush=True)
    print(f"  port {port}, transferByValue={has_relation}", flush=True)


def ready(events: dict[str, float], needed: tuple[str, ...], port: int | None) -> bool:
    if any(name not in events for name in needed):
        return False
    return port is not None or "T3" not in needed


def wait_until(lines: queue.Queue, events: dict[str, float], needed: tuple[str, ...], port_box: list[int | None]) -> bool:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready(events, needed, port_box[0]):
            return True
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            return False
        now = time.monotonic()
        for name, marker in MARKERS.items():
            if marker in line and name not in events:
                events[name] = now
                print(f"saw {name} {LABELS[name]}", flush=True)
        if READY in line and port_box[0] is None:
            port_box[0] = int(line.split(READY, 1)[1].split("/", 1)[0])
    return ready(events, needed, port_box[0])


def main() -> None:
    binary = rindexer_binary()
    print(f"using {binary}", flush=True)
    subprocess.run(["docker", "compose", "up", "-d"], cwd=PROJECT, check=True)
    print("arming barrier: apply closed, listen closed", flush=True)
    psql(CREATE_BARRIER)
    process = subprocess.Popen(
        [binary, "start", "-p", str(PROJECT), "all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: queue.Queue = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    events: dict[str, float] = {}
    port_box: list[int | None] = [None]
    try:
        print("waiting for T1 introspection and T2 before LISTEN", flush=True)
        if not wait_until(lines, events, ("T1", "T2"), port_box):
            sys.exit("timed out before T1 and T2")

        print("releasing foreign key application", flush=True)
        psql(RELEASE_APPLY)
        print("waiting for T4 foreign key commit", flush=True)
        if not wait_until(lines, events, ("T1", "T2", "T4"), port_box):
            sys.exit("timed out before T4")

        print("releasing LISTEN", flush=True)
        psql(RELEASE_LISTEN)
        print("waiting for T3 LISTEN confirmed", flush=True)
        if not wait_until(lines, events, ("T1", "T2", "T3", "T4"), port_box):
            sys.exit("timed out before T3")
        if port_box[0] is None:
            sys.exit("GraphQL port was not announced")

        events["T5"] = time.monotonic()
        fields = approval_fields(port_box[0])
        if fields is None:
            sys.exit(f"graphql on {port_box[0]} did not answer")
        has_relation = "transferByValue" in fields
        print_order(events, port_box[0], has_relation)
        if has_relation:
            sys.exit("transferByValue=True; the schema saw the foreign key")
        print("transferByValue=False", flush=True)
    finally:
        kill(process)
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgresql",
                "psql",
                "-U",
                "postgres",
                "-c",
                DROP_BARRIER,
            ],
            cwd=PROJECT,
            check=False,
        )


if __name__ == "__main__":
    main()
