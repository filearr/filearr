"""Permission-snapshot ingest of an uploaded inventory result (2026-08-25).

``POST /agents/{id}/inventory-results`` stores the gzip NDJSON blob and defers
this task; it reads the blob back from ``inventory_dir`` and runs
:func:`filearr.permission_ingest.ingest_ndjson_gz` in chunked transactions.
Moving the ingest off the request path is what lets the agent's upload ack in
seconds instead of the minutes a 100k-entry permissions blob takes to land in
``permission_snapshots`` (live 2026-08-25: the agent's HTTP client timed out
waiting for the inline ingest and reported the upload as failed).
"""

from __future__ import annotations

import logging
import uuid

from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.models import AgentCommand
from filearr.worker import proc_app

log = logging.getLogger(__name__)


@proc_app.task(
    queue="maintenance",
    name="filearr.tasks.permissions.ingest_inventory_result",
)
async def ingest_inventory_result(command_id: str) -> dict[str, int]:
    from filearr import permission_ingest
    from filearr.api.agent_inventory import result_path

    cid = uuid.UUID(command_id)
    path = result_path(get_settings(), cid)
    try:
        blob = path.read_bytes()
    except OSError as err:
        log.warning("permission ingest: result blob for %s unreadable: %s", command_id, err)
        return {"seen": 0, "written": 0, "unchanged": 0, "skipped": 0}
    async with SessionLocal() as session:
        cmd = await session.get(AgentCommand, cid)
        if cmd is None:
            log.warning("permission ingest: command %s vanished", command_id)
            return {"seen": 0, "written": 0, "unchanged": 0, "skipped": 0}
        out = await permission_ingest.ingest_ndjson_gz(
            session, agent_id=cmd.agent_id, command_id=cid, blob=blob
        )
    log.info("permission ingest: command %s -> %s", command_id, out)
    return out
