#!/usr/bin/env python3
"""
Check PostgreSQL pool budget against managed-DB connection limits.

Usage examples:
  python scripts/check_pg_pool_budget.py --connection-limit 47 --workers 2 --pool-size 8 --max-overflow 2
  python scripts/check_pg_pool_budget.py  # reads env defaults when available
"""

from __future__ import annotations

import argparse
import os
import sys


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate PostgreSQL connection budget.")
    parser.add_argument(
        "--connection-limit",
        type=int,
        default=env_int("DB_CONNECTION_LIMIT", 47),
        help="Managed PostgreSQL connection limit.",
    )
    parser.add_argument(
        "--reserve",
        type=int,
        default=env_int("DB_CONNECTION_RESERVE", 6),
        help="Reserved DB connections for admin/ops/debug.",
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=env_int("APP_INSTANCE_COUNT", 1),
        help="Gateway instance count.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=env_int("UVICORN_WORKERS", 1),
        help="Uvicorn workers per instance.",
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        default=env_int("DB_POOL_SIZE", 20),
        help="SQLAlchemy DB_POOL_SIZE.",
    )
    parser.add_argument(
        "--max-overflow",
        type=int,
        default=env_int("DB_MAX_OVERFLOW", 30),
        help="SQLAlchemy DB_MAX_OVERFLOW.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    connection_limit = max(1, args.connection_limit)
    reserve = max(0, args.reserve)
    instances = max(1, args.instances)
    workers = max(1, args.workers)
    pool_size = max(1, args.pool_size)
    max_overflow = max(0, args.max_overflow)

    usable_limit = max(1, connection_limit - reserve)
    per_worker_pool = pool_size + max_overflow
    estimated_peak = instances * workers * per_worker_pool

    print("=== PostgreSQL Pool Budget Check ===")
    print(f"connection_limit={connection_limit}")
    print(f"reserve={reserve}")
    print(f"usable_limit={usable_limit}")
    print(f"instances={instances}")
    print(f"workers={workers}")
    print(f"pool_size={pool_size}")
    print(f"max_overflow={max_overflow}")
    print(f"estimated_peak={estimated_peak}")

    if estimated_peak > usable_limit:
        safe_per_worker = max(1, usable_limit // (instances * workers))
        print("")
        print("Result: NOT SAFE")
        print(
            "Recommendation: reduce (DB_POOL_SIZE + DB_MAX_OVERFLOW) "
            f"to <= {safe_per_worker} per worker."
        )
        return 1

    remaining = usable_limit - estimated_peak
    print("")
    print("Result: SAFE")
    print(f"remaining_budget={remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

