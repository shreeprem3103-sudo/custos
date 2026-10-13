"""Export (and verify) the database schema as SQL and Prisma.

The SQLAlchemy models in ``custos.infrastructure.models`` are the single source of
truth. This script renders them into the two artefacts a deployment usually
needs, and — more importantly — can *check* that the rendered artefacts still
match the models. A hand-maintained schema document drifts within a sprint; a
checked-in generated one cannot.

Usage::

    python scripts/export_schema.py            # write backend/schema/*.sql|prisma
    python scripts/export_schema.py --check    # exit 1 if the files are stale

Run ``--check`` in CI. A drift between the code and the schema document is a
deployment defect: the DBA and the application would be creating different cities.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Any, Iterable

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from custos.infrastructure.models import Base  # noqa: E402

SCHEMA_DIR = BACKEND / "schema"
SQL_PATH = SCHEMA_DIR / "custos_schema.sql"
PRISMA_PATH = SCHEMA_DIR / "schema.prisma"

#: SQLAlchemy type name -> Prisma scalar. Anything unmapped is reported rather
#: than guessed, so the Prisma file can never silently disagree with the SQL.
PRISMA_TYPES: dict[str, str] = {
    "VARCHAR": "String",
    "TEXT": "String",
    "CHAR": "String",
    "INTEGER": "Int",
    "BIGINT": "BigInt",
    "FLOAT": "Float",
    "NUMERIC": "Decimal",
    "BOOLEAN": "Boolean",
    "DATETIME": "DateTime",
    "TIMESTAMP": "DateTime",
    "DATE": "DateTime",
    "JSON": "Json",
    "JSONB": "Json",
    "ARRAY": "Json",
}

SQL_HEADER = """-- ---------------------------------------------------------------------------
-- CUSTOS — Crisis Digital Twin schema (PostgreSQL 14+)
--
-- GENERATED FILE. Do not edit by hand: run
--     python backend/scripts/export_schema.py
-- after changing custos/infrastructure/models.py, and commit the result.
-- `python backend/scripts/export_schema.py --check` fails the build if this file
-- and the ORM models have drifted apart.
--
-- Design notes that the DDL cannot express on its own:
--   * audit_log is an append-only hash chain. Never UPDATE or DELETE a row;
--     the application verifies seq + prev_hash + hash on every health check.
--   * sensor_readings.dedupe_key is UNIQUE by construction. A store-and-forward
--     gateway replaying a batch is a no-op, so a retry cannot double-count a flood.
--   * simulations carries the full request, KPI set, uncertainty band, timeline,
--     cascade, recommendations and explainability. A run is reproducible from the
--     row alone, with no dependency on this application's memory.
--   * cascade_edges and simulations have ON DELETE CASCADE: deleting a scenario
--     removes its findings, never the audit trail.
-- ---------------------------------------------------------------------------

"""

PRISMA_HEADER = """// ---------------------------------------------------------------------------
// CUSTOS — Crisis Digital Twin schema (Prisma)
//
// GENERATED FILE from the SQLAlchemy models. Do not edit by hand: run
//     python backend/scripts/export_schema.py
// `python backend/scripts/export_schema.py --check` fails the build on drift.
//
// The application talks to the database through async SQLAlchemy; this file
// exists so that analysts can query the twin with the Prisma/Node tooling
// without reverse-engineering the models, and so a reviewer can read the data
// model in one page.
// ---------------------------------------------------------------------------

generator client {
  provider        = "prisma-client-js"
  previewFeatures = ["postgresqlExtensions"]
}

datasource db {
  provider   = "postgresql"
  url        = env("DATABASE_URL")
  extensions = [postgis]
}

"""


def render_sql() -> str:
    dialect = postgresql.dialect()
    chunks = [SQL_HEADER]
    for table in Base.metadata.sorted_tables:
        chunks.append(f"-- {table.comment or table.name}\n" if table.comment else f"-- {table.name}\n")
        chunks.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";\n")
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            chunks.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";\n")
        chunks.append("\n")
    return "".join(chunks)


def _prisma_type(column: Any) -> str:
    python_type = column.type.python_type if hasattr(column.type, "python_type") else object
    raw = type(column.type).__name__.upper()
    for key, value in PRISMA_TYPES.items():
        if key in raw:
            return value
    return {
        int: "Int",
        float: "Float",
        str: "String",
        bool: "Boolean",
        dict: "Json",
        list: "Json",
    }.get(python_type, "String")


def render_prisma() -> str:
    lines: list[str] = [PRISMA_HEADER]
    for table in Base.metadata.sorted_tables:
        model = "".join(part.capitalize() for part in table.name.split("_"))
        lines.append(f"/// {table.comment or table.name}\n" if table.comment else "")
        lines.append(f"model {model} {{\n")
        primary_keys = [column.name for column in table.primary_key.columns]
        for column in table.columns:
            prisma_type = _prisma_type(column)
            # Optionality in Prisma mirrors NULL-ability exactly. A required column
            # with a default is still required: the database will fill it in.
            optional = "?" if column.nullable else ""
            attributes: list[str] = []
            if column.name in primary_keys:
                attributes.append("@id")
            if column.unique:
                attributes.append("@unique")
            if column.index:
                attributes.append("@index")
            literal = _literal_default(column, prisma_type)
            if literal is not None:
                attributes.append(literal)
            if column.foreign_keys:
                target = next(iter(column.foreign_keys))
                target_table = "".join(part.capitalize() for part in target.column.table.name.split("_"))
                attributes.append(f"@relation(fields: [{column.name}], references: [id])")
                del target_table  # relation fields are declared as plain scalars below
            field_line = f"  {column.name} {prisma_type}{optional}"
            if attributes:
                field_line += " " + " ".join(attributes)
            lines.append(field_line + "\n")
        lines.append(f"\n  @@map(\"{table.name}\")\n")
        lines.append("}\n\n")
    return "".join(lines)


def _literal_default(column: Any, prisma_type: str) -> str | None:
    """Render a *literal* column default for Prisma, or nothing at all.

    Callable defaults (``default=utcnow``) and SQL-side functions are deliberately
    skipped: emitting ``repr(<function …>)`` would produce a Prisma file that does
    not compile, and inventing a value would be worse than saying nothing.
    """
    default = column.default
    if default is None or column.primary_key:
        return None
    arg = getattr(default, "arg", None)
    if arg is None or callable(arg) or not isinstance(arg, (str, int, float, bool)):
        return None
    if prisma_type == "String":
        return f"@default({arg!r})"
    if prisma_type in {"Int", "Float", "Boolean"}:
        return f"@default({arg!r})"
    return None


def write(path: Path, content: str, check: bool) -> bool:
    """Write (or verify) one artefact. Returns True when it is up to date."""
    if check:
        if not path.exists():
            print(f"MISSING {path.relative_to(BACKEND)}")
            return False
        current = path.read_text()
        if current == content:
            print(f"ok      {path.relative_to(BACKEND)}")
            return True
        diff = "\n".join(
            difflib.unified_diff(current.splitlines()[:80], content.splitlines()[:80], fromfile=str(path), tofile="generated", lineterm="")
        )
        print(f"STALE   {path.relative_to(BACKEND)}\n{diff[:2000]}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"wrote   {path.relative_to(BACKEND)} ({content.count(chr(10))} lines)")
    return True


def summary() -> Iterable[str]:
    for table in Base.metadata.sorted_tables:
        yield f"{table.name}: {len(table.columns)} columns, {len(table.indexes)} indexes"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export or verify the CUSTOS schema documents")
    parser.add_argument("--check", action="store_true", help="verify the committed files match the models (exit 1 on drift)")
    args = parser.parse_args()

    sql = render_sql()
    prisma = render_prisma()
    ok = write(SQL_PATH, sql, args.check)
    ok = write(PRISMA_PATH, prisma, args.check) and ok

    if not args.check:
        print("\ntables:")
        for line in summary():
            print(f"  {line}")

    if args.check and not ok:
        print("\nschema documents are stale: run `python backend/scripts/export_schema.py` and commit the result")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
