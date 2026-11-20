"""Cross-batch inventory workspace.

A workspace is a directory holding one atomically-replaced ``state.json`` plus a
``lock`` file used for ``fcntl.flock`` mutual exclusion. Plans are pure previews
pinned to the current inventory revision; they never reserve or mutate stock.
Commits are serialized by the lock, validated against the live revision, applied
in memory, and made durable with a single atomic rename, so a crashed commit is
either fully visible or fully absent and a retry is always safe. Committed
``job_id`` records make retries idempotent and reject conflicting reuse.

Quality incidents isolate the stock roots they name. Isolation is not stored on
the entries: it is derived on every read from the active incident set, so a
consumed identity can never be revived by registering or releasing an event.
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
from .model import validate_stock_rows, validate_batch, validate_incident

SCHEMA = 1
STATE_NAME = "state.json"
LOCK_NAME = "lock"


class StalePlanError(ValueError):
    """The plan's base revision is no longer the workspace revision."""


class ConflictError(ValueError):
    """A stable identity is already recorded with different content."""


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
        "incidents": {},
    }


def _read_state(workspace_dir):
    path = Path(workspace_dir) / STATE_NAME
    if not path.is_file():
        raise ValueError(f"not a stock workspace: {workspace_dir}")
    state = load(path)
    if state.get("kind") != "stock-workspace" or state.get("schema_version") != SCHEMA:
        raise ValueError(f"unsupported workspace state: {path}")
    # Older schema-1 workspaces predate quality events; they have no incidents.
    state.setdefault("incidents", {})
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


def _active_root_incidents(state):
    """root_stock_id -> sorted ids of active quality incidents covering it."""
    covered = {}
    for incident_id in sorted(state.get("incidents", {})):
        incident = state["incidents"][incident_id]
        if incident["status"] != "active":
            continue
        for root in incident["roots"]:
            covered.setdefault(root, []).append(incident_id)
    return covered


def _available_entries(state):
    """Entries a new plan may use: available and not blocked by an active incident."""
    covered = _active_root_incidents(state)
    return {key: entry for key, entry in state["entries"].items()
            if entry["status"] == "available"
            and entry["root_stock_id"] not in covered}


def _usable_entry_ids(state):
    return set(_available_entries(state))


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


def _output_ready(output_path, result):
    """Reject a conflicting existing output before mutating state.

    Returns True when the caller still needs to publish, False when an identical
    output already exists.
    """
    target = Path(output_path)
    if not target.exists():
        return True
    try:
        existing = load(target)
    except ValueError:
        existing = None
    if existing == result:
        return False
    raise FileExistsError(f"output already exists: {output_path}")


# ---------------------------------------------------------------------------
# Quality incidents
#
# An incident covers one or more initial stock roots. Its effect -- isolating
# every *available* entry descended from those roots -- is computed on demand
# rather than written into the entries, so consumed entries stay consumed
# forever and releasing an incident can never bring one back to life.
# ---------------------------------------------------------------------------


def _require_known_roots(state, roots):
    for root in roots:
        entry = state["entries"].get(f"stock/{root}")
        if entry is None or entry["kind"] != "stock":
            raise ValueError(f"unknown stock_id: {root}")


def _entry_view(entry, covered):
    """Stored entry plus effective quality-aware status and covering incidents."""
    incidents = list(covered.get(entry["root_stock_id"], ()))
    view = {
        "entry_id": entry["entry_id"],
        "kind": entry["kind"],
        "material": entry["material"],
        "length_mm": entry["length_mm"],
        "trim_head_mm": entry["trim_head_mm"],
        "trim_tail_mm": entry["trim_tail_mm"],
        "usable_mm": entry["usable_mm"],
        "status": entry["status"],
        "consumed_by": entry["consumed_by"],
        "source": entry["source"],
        "root_stock_id": entry["root_stock_id"],
        "active_incidents": incidents,
    }
    if entry["status"] == "available" and incidents:
        view["status"] = "isolated"
    return view


def _trace_pieces(state, root):
    """Every already-committed cut piece whose inventory entry descends from root."""
    pieces = []
    for job_id in sorted(state["jobs"]):
        job = state["jobs"][job_id]
        for ticket in job["result"]["tickets"]:
            entry = state["entries"].get(ticket["stock_id"])
            if entry is None or entry["root_stock_id"] != root:
                continue
            for cut in ticket["cuts"]:
                pieces.append({
                    "job_id": job_id,
                    "root_stock_id": root,
                    "entry_id": entry["entry_id"],
                    "entry_kind": entry["kind"],
                    "material": ticket["material"],
                    "order_id": cut["order_id"],
                    "unit": cut["unit"],
                    "start_mm": cut["start_mm"],
                    "end_mm": cut["end_mm"],
                    "kerf_end_mm": cut["kerf_end_mm"],
                })
    pieces.sort(key=lambda piece: (piece["job_id"], piece["entry_id"],
                                   piece["order_id"], piece["unit"]))
    return pieces


def _incident_report(state, incident):
    """Stable registration report: affected entries and traced pieces per root."""
    covered = _active_root_incidents(state)
    roots = []
    total_pieces = 0
    for root in incident["roots"]:
        entries = [_entry_view(entry, covered)
                   for _, entry in sorted(state["entries"].items())
                   if entry["root_stock_id"] == root]
        pieces = _trace_pieces(state, root)
        total_pieces += len(pieces)
        jobs = sorted({piece["job_id"] for piece in pieces})
        roots.append({
            "stock_id": root,
            "active_incidents": list(covered.get(root, ())),
            "entries": entries,
            "pieces": pieces,
            "summary": {
                "entries": len(entries),
                "available_entries": sum(1 for e in entries if e["status"] == "available"),
                "consumed_entries": sum(1 for e in entries if e["status"] == "consumed"),
                "isolated_entries": sum(1 for e in entries if e["status"] == "isolated"),
                "pieces": len(pieces),
                "committed_jobs": jobs,
            },
        })
    return {
        "schema_version": SCHEMA,
        "kind": "stock-workspace-incident",
        "workspace_id": state["workspace_id"],
        "incident_id": incident["incident_id"],
        "status": "active",
        "roots": roots,
        "summary": {
            "roots": len(roots),
            "entries": sum(root["summary"]["entries"] for root in roots),
            "pieces": total_pieces,
            "isolated_entry_ids": sorted(
                entry["entry_id"] for root in roots for entry in root["entries"]
                if entry["status"] == "isolated"),
        },
    }


def register_incident(workspace_dir, source_path, output_path=None):
    data = load(source_path)
    if "stock_ids" in data:
        roots_value = data["stock_ids"]
    elif "stock_id" in data:
        roots_value = [data["stock_id"]]
    else:
        roots_value = None
    incident_id, roots = validate_incident(
        {"incident_id": data.get("incident_id"), "stock_ids": roots_value})
    with _locked(workspace_dir):
        state = _read_state(workspace_dir)
        _require_known_roots(state, roots)
        existing = state["incidents"].get(incident_id)
        if existing is not None:
            if existing["status"] != "active":
                raise ConflictError(
                    f"incident is already released and cannot be reactivated: {incident_id}")
            if existing["roots"] != roots:
                raise ConflictError(
                    f"incident_id is already registered with different stock_ids: {incident_id}")
            result = existing["report"]
            if output_path is not None and _output_ready(output_path, result):
                publish(output_path, result)
            return result
        before = _usable_entry_ids(state)
        incident = {
            "incident_id": incident_id,
            "roots": roots,
            "status": "active",
            "registered_revision": state["revision"],
            "released_revision": None,
            "report": None,
            "release_result": None,
        }
        state["incidents"][incident_id] = incident
        after = _usable_entry_ids(state)
        if after != before:
            state["revision"] += 1
        incident["registered_revision"] = state["revision"]
        result = _incident_report(state, incident)
        incident["report"] = result
        needs_publish = output_path is not None and _output_ready(output_path, result)
        _write_state(workspace_dir, state)
    if needs_publish:
        publish(output_path, result)
    return result


def release_incident(workspace_dir, incident_id, output_path=None):
    text(incident_id, "incident_id")
    with _locked(workspace_dir):
        state = _read_state(workspace_dir)
        incident = state["incidents"].get(incident_id)
        if incident is None:
            raise ValueError(f"unknown incident_id: {incident_id}")
        if incident["status"] == "released":
            result = incident["release_result"]
            if output_path is not None and _output_ready(output_path, result):
                publish(output_path, result)
            return result
        before = _usable_entry_ids(state)
        incident["status"] = "released"
        covered = _active_root_incidents(state)
        after = _usable_entry_ids(state)
        changed = after != before
        if changed:
            state["revision"] += 1
        incident["released_revision"] = state["revision"]
        result = {
            "schema_version": SCHEMA,
            "kind": "stock-workspace-incident-release",
            "workspace_id": state["workspace_id"],
            "incident_id": incident_id,
            "status": "released",
            "previous_status": "active",
            "revision": state["revision"],
            "revision_changed": changed,
            "roots": [
                {"stock_id": root,
                 "still_isolated": bool(covered.get(root)),
                 "active_incidents": list(covered.get(root, ()))}
                for root in incident["roots"]],
            "active_incidents": sorted(
                incident_key for incident_key, other in state["incidents"].items()
                if other["status"] == "active"),
        }
        incident["release_result"] = result
        needs_publish = output_path is not None and _output_ready(output_path, result)
        _write_state(workspace_dir, state)
    if needs_publish:
        publish(output_path, result)
    return result


def incident_status(workspace_dir, incident_id=None):
    with _locked(workspace_dir, exclusive=False):
        state = _read_state(workspace_dir)
        covered = _active_root_incidents(state)
        if incident_id is not None:
            incident = state["incidents"].get(incident_id)
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            roots = set(incident["roots"])
            # Currently isolated identities descended from this incident's roots.
            affected = sorted(
                entry_id for entry_id, entry in state["entries"].items()
                if entry["status"] == "available"
                and entry["root_stock_id"] in roots
                and covered.get(entry["root_stock_id"]))
            return {
                "schema_version": SCHEMA,
                "kind": "stock-workspace-incident-status",
                "workspace_id": state["workspace_id"],
                "revision": state["revision"],
                "incident_id": incident["incident_id"],
                "status": incident["status"],
                "roots": list(incident["roots"]),
                "registered_revision": incident["registered_revision"],
                "released_revision": incident["released_revision"],
                "active_incidents": sorted(
                    key for key, other in state["incidents"].items()
                    if other["status"] == "active"),
                "active_incidents_by_root": {
                    root: list(covered.get(root, ())) for root in incident["roots"]},
                "isolated_entry_ids": affected,
                "report": incident["report"],
            }
        return {
            "schema_version": SCHEMA,
            "kind": "stock-workspace-incident-list",
            "workspace_id": state["workspace_id"],
            "revision": state["revision"],
            "active_incidents": sorted(
                key for key, other in state["incidents"].items()
                if other["status"] == "active"),
            "incidents": [
                {"incident_id": incident["incident_id"],
                 "status": incident["status"],
                 "roots": list(incident["roots"]),
                 "registered_revision": incident["registered_revision"],
                 "released_revision": incident["released_revision"],
                 "isolated_entry_ids": [
                     entry_id for entry_id, entry in sorted(state["entries"].items())
                     if entry["status"] == "available"
                     and entry["root_stock_id"] in incident["roots"]
                     and covered.get(entry["root_stock_id"])]}
                for _, incident in sorted(state["incidents"].items())],
        }


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
        covered = _active_root_incidents(state)
        entries = [_entry_view(state["entries"][key], covered)
                   for key in sorted(state["entries"])]
        by_material = {}
        isolated_by_material = {}
        for entry in entries:
            if entry["status"] == "available":
                bucket = by_material.setdefault(
                    entry["material"], {"entries": 0, "usable_mm": 0})
                bucket["entries"] += 1
                bucket["usable_mm"] += entry["usable_mm"]
            elif entry["status"] == "isolated":
                bucket = isolated_by_material.setdefault(
                    entry["material"], {"entries": 0, "usable_mm": 0})
                bucket["entries"] += 1
                bucket["usable_mm"] += entry["usable_mm"]
        incidents = [
            {"incident_id": incident["incident_id"],
             "status": incident["status"],
             "roots": list(incident["roots"]),
             "registered_revision": incident["registered_revision"],
             "released_revision": incident["released_revision"]}
            for _, incident in sorted(state["incidents"].items())]
        return {
            "schema_version": SCHEMA,
            "kind": "stock-workspace-status",
            "workspace_id": state["workspace_id"],
            "revision": state["revision"],
            "entries": entries,
            "committed_jobs": sorted(state["jobs"]),
            "incidents": incidents,
            "active_incidents": sorted(key for key in covered),
            "active_incidents_by_root": {
                root: list(incident_ids) for root, incident_ids in sorted(covered.items())},
            "summary": {
                "available_entries": sum(1 for e in entries if e["status"] == "available"),
                "consumed_entries": sum(1 for e in entries if e["status"] == "consumed"),
                "isolated_entries": sum(1 for e in entries if e["status"] == "isolated"),
                "available_by_material": by_material,
                "isolated_by_material": isolated_by_material,
            },
        }
