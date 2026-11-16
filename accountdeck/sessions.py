"""Portable local Code entries only. Never rewrite conversation transcripts.

The result counts added/updated/deleted/skipped entries and lists `stripped_fields`: the names (never
values) of account-bound fields left behind by the entries written, for the switch manifest (§10.2)."""
from __future__ import annotations
import json
from pathlib import Path
import uuid
from .core import DeckError, json_bytes

PORTABLE={"sessionId","cliSessionId","cwd","originCwd","lastFocusedAt","createdAt",
          "lastActivityAt","model","effort","isArchived","title","titleSource","completedTurns",
          "titleTurn","lastAssistantUuid","latestUserFrameAt"}


def valid_uuid(value):
    try: return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError): return None


def sync_sessions(base,projects,identity,org,tx):
    if not valid_uuid(identity) or not valid_uuid(org):
        raise DeckError("The Claude account's organization is unknown. Open Code once and reconnect it.","identity")
    root=Path(base)/"claude-code-sessions"
    destination=root/identity/org
    if not root.exists(): return {"added":0,"updated":0,"deleted":0,"skipped":0,"stripped_fields":[]}
    dead=set()
    for path in root.glob("*/*/deleted_*"):
        ident=valid_uuid(path.name.removeprefix("deleted_").removesuffix(".json"))
        if ident: dead.add(ident)
    records={};skipped=0
    transcripts={p.stem for p in Path(projects).glob("*/*.jsonl")}
    for path in root.glob("*/*/local_*.json"):
        try:
            record=json.loads(path.read_bytes())
            local=valid_uuid(path.stem.removeprefix("local_"));cli=valid_uuid(record.get("cliSessionId"))
            if not local or not cli or record.get("sessionId")!=path.stem: raise ValueError()
            if cli not in transcripts: continue
            rank=(record.get("lastActivityAt") or 0,path.stat().st_mtime_ns)
            if path.name not in records or rank>records[path.name][0]: records[path.name]=(rank,record)
        except (OSError,ValueError,TypeError): skipped+=1
    result={"added":0,"updated":0,"deleted":0,"skipped":skipped}
    stripped=set()
    for name,(_,row) in records.items():
        target=destination/name
        local=name.removeprefix("local_").removesuffix(".json")
        if local in dead or row["cliSessionId"] in dead:
            if target.exists():
                tx.write(target,None);result["deleted"]+=1
            marker=destination/("deleted_"+local)
            if not marker.exists(): tx.write(marker,b"")
            continue
        clean={k:v for k,v in row.items() if k in PORTABLE}
        if not target.exists():
            clean.update({"permissionMode":"default","alwaysAllowedReasons":[],"sessionPermissionUpdates":[]})
            tx.write(target,json_bytes(clean));result["added"]+=1;stripped.update(row.keys()-PORTABLE)
        else:
            try: existing=json.loads(target.read_bytes())
            except (OSError,ValueError): result["skipped"]+=1;continue
            if existing.get("cliSessionId")!=row["cliSessionId"]:
                result["skipped"]+=1;continue
            updated={**existing,**clean}
            if updated!=existing:
                tx.write(target,json_bytes(updated));result["updated"]+=1;stripped.update(row.keys()-PORTABLE)
    result["stripped_fields"]=sorted(stripped)
    return result
