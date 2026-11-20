"""Cross-batch inventory workspace.

A workspace is a directory holding one atomically-replaced ``state.json`` plus a
``lock`` file used for ``fcntl.flock`` mutual exclusion. Plans are pure previews
pinned to the current inventory revision; they never reserve or mutate stock.
Commits are serialized by the lock, validated against the live revision, applied
in memory, and made durable with a single atomic rename, so a crashed commit is
either fully visible or fully absent and a retry is always safe. Committed
``job_id`` records make retries idempotent and reject conflicting reuse.
"""
import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from .io import load, publish, integer, text
from .layout import compile_tickets
from .model import validate_stock_rows, validate_batch

SCHEMA = 1
STATE_NAME = "state.json"
LOCK_NAME = "lock"


class StalePlanError(ValueError):
    """The plan's base revision is no longer the workspace revision."""


class ConflictError(ValueError):
    """The job_id is already committed with a different request or plan."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _fingerprint(value):
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _request_identity(data):
    """Stable semantic identity of a batch request, insensitive to row order."""
    return {
        "job_id": data["job_id"],
        "kerf_mm": data["kerf_mm"],
        "min_offcut_mm": data["min_offcut_mm"],
        "orders": [
            {"id": row["id"], "material": row["material"],
             "piece_mm": row["piece_mm"], "quantity": row["quantity"]}
            for row in sorted(data["orders"], key=lambda row: row["id"])
        ],
    }


def _stock_entry(row):
    return {
        "entry_id": f"stock/{row['id']}",
        "kind": "stock",
        "material": row["material"],
        "length_mm": row["length_mm"],
        "trim_head_mm": row["trim_head_mm"],
        "trim_tail_mm": row["trim_tail_mm"],
        "usable_mm": row["length_mm"] - row["trim_head_mm"] - row["trim_tail_mm"],
        "status": "available",
        "consumed_by": None,
        "source": {"type": "initial", "stock_id": row["id"]},
        "root_stock_id": row["id"],
    }


def _offcut_entry(job_id, ticket, parent):
    """Reusable offcut of one ticket; length is its own usable span, no trims."""
    return {
        "entry_id": f"offcut/{job_id}/{parent['entry_id']}",
        "kind": "offcut",
        "material": ticket["material"],
        "length_mm": ticket["offcut_length_mm"],
        "trim_head_mm": 0,
        "trim_tail_mm": 0,
        "usable_mm": ticket["offcut_length_mm"],
        "status": "available",
        "consumed_by": None,
        "source": {"type": "offcut", "job_id": job_id,
                   "parent_entry_id": parent["entry_id"],
                   "stock_id": parent["root_stock_id"]},
        "root_stock_id": parent["root_stock_id"],
    }


def _initial_state(stock):
    entries = {}
    for row in stock.values():
        entry = _stock_entry(row)
        entries[entry["entry_id"]] = entry
    return {
        "schema_version": SCHEMA,
        "kind": "stock-workspace",
        "workspace_id": _fingerprint(
            {"initial_entries": [entries[key] for key in sorted(entries)]}),
        "revision": 0,
        "entries": entries,
        "jobs": {},
    }


def _read_state(workspace_dir):
    path = Path(workspace_dir) / STATE_NAME
    if not path.is_file():
        raise ValueError(f"not a stock workspace: {workspace_dir}")
    state = load(path)
    if state.get("kind") != "stock-workspace" or state.get("schema_version") != SCHEMA:
        raise ValueError(f"unsupported workspace state: {path}")
    return state


def _write_state(workspace_dir, state):
    """Durable single-renamed-file update; a crash leaves old or new, never half."""
    payload = (json.dumps(state, ensure_ascii=False, indent=2,
                          sort_keys=True, allow_nan=False) + "\n").encode()
    fd, temp = tempfile.mkstemp(prefix=".state-", dir=workspace_dir)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, Path(workspace_dir) / STATE_NAME)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    dir_fd = os.open(workspace_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@contextmanager
def _locked(workspace_dir, exclusive=True):
    fd = os.open(Path(workspace_dir) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def _available_entries(state):
    return {key: entry for key, entry in state["entries"].items()
            if entry["status"] == "available"}


def _summary(tickets, unmet, kerf):
    delivered = sum(len(ticket["cuts"]) for ticket in tickets)
    reusable = sum(t["offcut_length_mm"] for t in tickets if t["offcut_reusable"])
    scrap = sum(t["trim_head_mm"] + t["trim_tail_mm"] +
                (0 if t["offcut_reusable"] else t["offcut_length_mm"]) for t in tickets)
    return {"complete": not unmet, "used_stock": len(tickets),
            "delivered_pieces": delivered, "unfilled_pieces": len(unmet),
            "kerf_loss_mm": delivered * kerf, "reusable_offcut_mm": reusable,
            "scrap_mm": scrap}


def build_plan(state, data):
    """Deterministic preview on the state's current revision; mutates nothing."""
    job_id, orders = validate_batch(data)
    entries = _available_entries(state)
    pseudo = {key: {"material": entry["material"], "length_mm": entry["length_mm"],
                    "trim_head_mm": entry["trim_head_mm"],
                    "trim_tail_mm": entry["trim_tail_mm"]}
              for key, entry in entries.items()}
    tickets, unmet = compile_tickets(pseudo, orders, data["kerf_mm"], data["min_offcut_mm"])
    for ticket in tickets:
        parent = entries[ticket["stock_id"]]
        ticket["entry_kind"] = parent["kind"]
        ticket["source"] = parent["source"]
    used = {ticket["stock_id"] for ticket in tickets}
    added = [_offcut_entry(job_id, ticket, entries[ticket["stock_id"]])
             for ticket in tickets if ticket["offcut_reusable"]]
    added.sort(key=lambda entry: entry["entry_id"])
    request = _request_identity(data)
    return {
        "schema_version": SCHEMA,
        "kind": "stock-workspace-plan",
        "workspace_id": state["workspace_id"],
        "base_revision": state["revision"],
        "job_id": job_id,
        "request": request,
        "request_sha256": _fingerprint(request),
        "tickets": tickets,
        "unfilled": unmet,
        "consumed_entry_ids": sorted(used),
        "added_entries": added,
        "unused_entry_ids": sorted(set(entries) - used),
        "summary": _summary(tickets, unmet, data["kerf_mm"]),
    }


def _validate_plan_shape(plan):
    if plan.get("kind") != "stock-workspace-plan" or plan.get("schema_version") != SCHEMA:
        raise ValueError("not a supported stock-workspace plan")
    for key in ("workspace_id", "base_revision", "job_id", "request",
                "request_sha256", "tickets", "unfilled", "consumed_entry_ids",
                "added_entries", "unused_entry_ids", "summary"):
        if key not in plan:
            raise ValueError(f"plan is missing {key}")
    integer(plan["base_revision"], "base_revision")
    text(plan["job_id"], "job_id")
    if plan["request_sha256"] != _fingerprint(plan["request"]):
        raise ValueError("plan request does not match its recorded hash")


def _apply_or_replay(state, plan):
    """Validate the plan against the live state and mutate state in memory.

    Returns (result, replayed). Callers persist state only when not replayed.
    """
    _validate_plan_shape(plan)
    if plan["workspace_id"] != state["workspace_id"]:
        raise ValueError("plan belongs to a different workspace")
    job_id = plan["job_id"]
    existing = state["jobs"].get(job_id)
    if existing is not None:
        if (existing["plan_sha256"] == _fingerprint(plan)
                and existing["request_sha256"] == plan["request_sha256"]):
            return existing["result"], True
        raise ConflictError(
            f"job_id is already committed with a different request or plan: {job_id}")
    if plan["base_revision"] != state["revision"]:
        raise StalePlanError(
            f"plan is stale: based on revision {plan['base_revision']}, "
            f"workspace is at revision {state['revision']}")
    if build_plan(state, plan["request"]) != plan:
        raise ValueError("plan content does not match the current workspace state")
    revision = state["revision"] + 1
    for entry_id in plan["consumed_entry_ids"]:
        entry = state["entries"][entry_id]
        entry["status"] = "consumed"
        entry["consumed_by"] = job_id
    for entry in plan["added_entries"]:
        state["entries"][entry["entry_id"]] = dict(entry)
    state["revision"] = revision
    result = {
        "schema_version": SCHEMA,
        "kind": "stock-workspace-commit",
        "workspace_id": state["workspace_id"],
        "job_id": job_id,
        "base_revision": plan["base_revision"],
        "revision": revision,
        "consumed_entry_ids": plan["consumed_entry_ids"],
        "added_entries": plan["added_entries"],
        "tickets": plan["tickets"],
        "unfilled": plan["unfilled"],
        "unused_entry_ids": plan["unused_entry_ids"],
        "summary": plan["summary"],
    }
    state["jobs"][job_id] = {
        "job_id": job_id,
        "request_sha256": plan["request_sha256"],
        "plan_sha256": _fingerprint(plan),
        "base_revision": plan["base_revision"],
        "revision": revision,
        "result": result,
    }
    return result, False


def _publish_result(output_path, result):
    """Create-once publish; an identical existing file is an idempotent retry."""
    target = Path(output_path)
    if target.exists():
        try:
            existing = load(target)
        except ValueError:
            existing = None
        if existing == result:
            return
        raise FileExistsError(f"output already exists: {output_path}")
    publish(target, result)


def init_workspace(workspace_dir, source_path):
    stock = validate_stock_rows(load(source_path))
    state = _initial_state(stock)
    target = Path(workspace_dir)
    if target.exists():
        raise FileExistsError(f"workspace already exists: {workspace_dir}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".init-", dir=target.parent))
    try:
        _write_state(staging, state)
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "schema_version": SCHEMA,
        "kind": "stock-workspace-init",
        "workspace_id": state["workspace_id"],
        "revision": state["revision"],
        "entries": [state["entries"][key] for key in sorted(state["entries"])],
        "summary": {"stock_entries": len(state["entries"]),
                    "materials": sorted({e["material"] for e in state["entries"].values()})},
    }


def plan(workspace_dir, source_path, output_path):
    data = load(source_path)
    with _locked(workspace_dir, exclusive=False):
        result = build_plan(_read_state(workspace_dir), data)
    publish(output_path, result)
    return result


def commit(workspace_dir, plan_path, output_path):
    plan_data = load(plan_path)
    with _locked(workspace_dir):
        state = _read_state(workspace_dir)
        result, replayed = _apply_or_replay(state, plan_data)
        if not replayed:
            _write_state(workspace_dir, state)
    # Canonical key order keeps a fresh commit byte-identical to later replays.
    result = json.loads(_canonical(result))
    _publish_result(output_path, result)
    return result


def status(workspace_dir, job_id=None):
    with _locked(workspace_dir, exclusive=False):
        state = _read_state(workspace_dir)
        if job_id is not None:
            job = state["jobs"].get(job_id)
            if job is None:
                raise ValueError(f"unknown job_id: {job_id}")
            return job["result"]
        entries = [state["entries"][key] for key in sorted(state["entries"])]
        by_material = {}
        for entry in entries:
            if entry["status"] != "available":
                continue
            bucket = by_material.setdefault(entry["material"], {"entries": 0, "usable_mm": 0})
            bucket["entries"] += 1
            bucket["usable_mm"] += entry["usable_mm"]
        return {
            "schema_version": SCHEMA,
            "kind": "stock-workspace-status",
            "workspace_id": state["workspace_id"],
            "revision": state["revision"],
            "entries": entries,
            "committed_jobs": sorted(state["jobs"]),
            "summary": {
                "available_entries": sum(1 for e in entries if e["status"] == "available"),
                "consumed_entries": sum(1 for e in entries if e["status"] == "consumed"),
                "available_by_material": by_material,
            },
        }
