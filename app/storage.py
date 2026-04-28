from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def schema_total_bytes(s: Session, schema: str) -> int:
    row = s.execute(text(
        "SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :sch AND c.relkind IN ('r','i','t')"
    ), {"sch": schema}).first()
    return int(row[0] or 0)


def relation_bytes(s: Session, qualified: str) -> int:
    row = s.execute(text("SELECT pg_total_relation_size(:rel)"),
                    {"rel": qualified}).first()
    return int((row[0] if row else 0) or 0)


def per_monitor_stats(s: Session, schema: str) -> list[dict]:
    """Per-monitor row counts + estimated bytes (proportional split of readings table)."""
    readings_bytes = relation_bytes(s, f'{schema}.readings')

    rows = s.execute(text(
        "SELECT m.id, m.name, m.unit, m.retention_days, "
        "       COUNT(r.id) AS cnt, "
        "       MIN(r.ts) AS oldest, "
        "       MAX(r.ts) AS newest "
        f'FROM "{schema}".monitors m '
        f'LEFT JOIN "{schema}".readings r ON r.monitor_id = m.id '
        "GROUP BY m.id, m.name, m.unit, m.retention_days "
        "ORDER BY m.name"
    )).all()

    total_rows = sum(int(r.cnt or 0) for r in rows)
    out: list[dict] = []
    for r in rows:
        cnt = int(r.cnt or 0)
        est = int(readings_bytes * cnt / total_rows) if total_rows else 0
        out.append({
            "id": r.id,
            "name": r.name,
            "unit": r.unit,
            "retention_days": r.retention_days,
            "count": cnt,
            "oldest": r.oldest,  # datetime; route formats for display
            "newest": r.newest,
            "estimated_bytes": est,
        })
    return out


def storage_overview(s: Session, schema: str) -> dict:
    return {
        "schema": schema,
        "schema_bytes": schema_total_bytes(s, schema),
        "readings_bytes": relation_bytes(s, f'{schema}.readings'),
        "monitors_bytes": relation_bytes(s, f'{schema}.monitors'),
        "monitors": per_monitor_stats(s, schema),
    }
