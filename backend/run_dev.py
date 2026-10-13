"""Cross-platform development launcher.

Loads ``backend/.env.dev`` (if present) *without* shell semantics, then runs
uvicorn in-process. This exists because environment files are not shell scripts:
the operator roster in `.env.dev` contains ``;`` and ``:``, which every shell
that sources it will happily execute or truncate. On 2026-10-13 that produced
`analyst.nair:…: command not found` and a launcher that never started.

One loader, used by ``run_dev.sh``, ``scripts\\start_backend.bat`` and CI, so the
development environment cannot differ between them.

Usage::

    python backend/run_dev.py                      # reads ./.env.dev
    python backend/run_dev.py --env other.env --port 8100
    python backend/run_dev.py --reload             # hot reload for UI/API work
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file into a dict, ignoring comments and blanks.

    Values are taken verbatim — no quote stripping, no variable expansion, no
    command substitution — because a secret containing ``$`` or ``;`` must survive
    exactly as written.
    """
    values: dict[str, str] = {}
    if not path.exists():
        print(f"[run_dev] no env file at {path}; using process environment and defaults")
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        values[key] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CUSTOS API for development")
    parser.add_argument("--env", default=str(BACKEND / ".env.dev"), help="environment file to load")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="reload on source changes (slow: the twin re-seeds)")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    values = load_env_file(Path(args.env))
    # The real environment wins over the file, so CI can override a single value
    # (`CUSTOS_DATABASE_URL=… python run_dev.py`) without editing a tracked file.
    for key, value in values.items():
        os.environ.setdefault(key, value)

    host = args.host or os.environ.get("CUSTOS_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("CUSTOS_PORT", "8000"))
    log_level = args.log_level or os.environ.get("CUSTOS_LOG_LEVEL", "info")

    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    # Imported *after* the environment is in place: settings are read at import.
    import uvicorn  # noqa: PLC0415

    from custos.config import settings  # noqa: PLC0415

    print(f"[run_dev] environment={settings.environment} city={settings.city_name}")
    print(f"[run_dev] database={settings.database_url.split('?')[0]}")
    print(f"[run_dev] console will be available on http://127.0.0.1:{port}/console/ once the API is up")
    if not settings.jwt_secret or settings.jwt_secret.startswith("change-me"):
        print("[run_dev] WARNING: the JWT secret looks like a placeholder. Run scripts\\setup_windows.bat (or bootstrap_env.py) to generate one.")

    uvicorn.run(
        "custos.api.main:app",
        host=host,
        port=port,
        log_level=log_level,
        reload=args.reload,
        # Trust the sandbox/EOC proxy's X-Forwarded-* headers so audit entries and
        # rate limits see the real client, not the proxy.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
