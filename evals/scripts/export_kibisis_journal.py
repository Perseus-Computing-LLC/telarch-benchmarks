#!/usr/bin/env python3
"""Export a Kibisis vault's audit journal and entity index (no content) for ``telarch-evals ledger-scenario``.

Runs with whichever interpreter has ``kibisis`` and the vault passphrase file reachable through
KIBISIS_PASSPHRASE_FILE (the live plugin's default key file is used when the variable is unset). Writes two
jsonl files: ``journal.jsonl`` (every audit_journal row, metadata as a JSON string) and ``entities.jsonl``
(category, key, scope, epistemic state, valid interval, archived flag, supersession pointers, created_at,
version, author — never the content, never the metadata). Opens the vault read-only.

    python3 scripts/export_kibisis_journal.py --vault /path/vault.sqlite --out /path/exportdir
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_KEY_FILE = "/opt/data/keys/kibisis.key"   # the live plugin's default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if "KIBISIS_PASSPHRASE_FILE" not in os.environ and "KIBISIS_PASSPHRASE" not in os.environ \
            and os.path.exists(DEFAULT_KEY_FILE):
        os.environ["KIBISIS_PASSPHRASE_FILE"] = DEFAULT_KEY_FILE
    from kibisis.vault import SQLiteMemoryVault   # noqa: E402

    vault = SQLiteMemoryVault(str(args.vault))
    try:
        cur = vault._conn.cursor()
        cur.execute("SELECT sequence, timestamp, action, category, key, scope, value_sha256, prev_hash, event_hash, "
                    "author_id, metadata_json FROM audit_journal ORDER BY sequence")
        cols = ["sequence", "timestamp", "action", "category", "key", "scope", "value_sha256", "prev_hash",
                "event_hash", "author_id", "metadata_json"]
        journal = [dict(zip(cols, tuple(r))) for r in cur.fetchall()]
        ents = []
        for e in vault.list_entities(include_archived=True, scope="*", include_unverified=True):
            ents.append({"category": e.category, "key": e.key, "scope": e.scope,
                         "epistemic_state": getattr(e.epistemic_state, "value", str(e.epistemic_state)),
                         "valid_from": e.valid_from, "valid_until": e.valid_until, "archived": bool(e.archived),
                         "supersedes": e.supersedes, "superseded_by": e.superseded_by, "created_at": e.created_at,
                         "version": e.version, "author_id": getattr(e, "author_id", None)})
    finally:
        vault.close()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "journal.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in journal), "utf-8")
    (args.out / "entities.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in ents), "utf-8")
    for p in (args.out / "journal.jsonl", args.out / "entities.jsonl"):
        os.chmod(p, 0o600)
    print(json.dumps({"journal_rows": len(journal), "entities": len(ents), "journal_head": journal[-1]["event_hash"] if journal else None,
                      "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
