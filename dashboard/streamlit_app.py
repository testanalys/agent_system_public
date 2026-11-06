"""Live operator dashboard for the agent harness.

Answers three questions on the first screen:
  1. What is waiting on me?
  2. Which landings never happened?
  3. Is discovery volume moving?

Run locally (ADC):

    export TURNSTILE_AGENT_STORE=gs://your-bucket
    pip install -r dashboard/requirements.txt
    streamlit run dashboard/streamlit_app.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import streamlit as st

from dashboard import timeline, validate
from dashboard.lifeblood import (
    campaign_target_rows,
    display_value,
    funnel_stage_rows,
    gcs_folders,
    last_ok_or_latest,
    load_catalogue,
    metric_entry,
    metric_help,
    staleness_note,
    updated_caption,
)
from dashboard.capture_trigger import (
    refresh_stale,
    trigger_capture,
)
from dashboard.render import as_table, COLUMNS
from dashboard.story import landing_stats, operator_counts, stringify_rows
from harness import ledger, objectstore, operator, runlog

DEFAULT_STORE = "gs://your-bucket"
SUBSYSTEMS = ("discovery", "pipeline")
LIFEBLOOD_PREFIX = "lifeblood"


def _store_uri() -> str:
    return (os.getenv("TURNSTILE_AGENT_STORE") or "").strip() or DEFAULT_STORE


def _set_store(uri: str) -> None:
    os.environ["TURNSTILE_AGENT_STORE"] = uri.strip()


@st.cache_data(ttl=30, show_spinner=False)
def load_timeline(subsystem: str | None, limit: int) -> list[dict]:
    return timeline.read(subsystem, limit)


@st.cache_data(ttl=30, show_spinner=False)
def load_operator(kind: str, subsystem: str) -> list[dict]:
    if kind == "open":
        return operator.list_open(subsystem)
    if kind == "incomplete":
        return operator.list_incomplete(subsystem)
    return operator.list_hold(subsystem)


@st.cache_data(ttl=30, show_spinner=False)
def load_escalations(subsystem: str) -> list[dict]:
    prefix = f"{ledger.OPEN_PREFIX}/{subsystem}/"
    keys = objectstore.list_keys(prefix)
    rows = []
    for key in keys:
        data = objectstore.get_json(key)
        if data:
            rows.append(data)
    return sorted(rows, key=lambda r: r.get("raised_at", ""))


@st.cache_data(ttl=30, show_spinner=False)
def load_run_keys(subsystem: str, limit: int) -> list[str]:
    return runlog.recent(subsystem, limit=limit)


@st.cache_data(ttl=30, show_spinner=False)
def load_run_archive(key: str) -> dict | None:
    return objectstore.get_json(key)


@st.cache_data(ttl=30, show_spinner=False)
def list_lifeblood_metrics(subsystem: str) -> list[str]:
    remotes = _lifeblood_prefixes(subsystem)
    if not remotes:
        return []
    ids: set[str] = set()
    try:
        from google.cloud import storage

        client = storage.Client()
        for bucket_name, prefix in remotes:
            blobs = client.list_blobs(bucket_name, prefix=prefix + "/")
            for blob in blobs:
                name = blob.name.rsplit("/", 1)[-1]
                if name.endswith(".jsonl"):
                    ids.add(name[:-6])
        return sorted(ids)
    except Exception:
        return sorted(ids)


@st.cache_data(ttl=30, show_spinner=False)
def load_lifeblood_series(subsystem: str, metric_id: str, days: int = 28) -> list[dict]:
    remotes = _lifeblood_prefixes(subsystem)
    if not remotes:
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    rows = []
    seen = set()
    try:
        from google.cloud import storage

        client = storage.Client()
        for bucket_name, prefix in remotes:
            key = f"{prefix}/{metric_id}.jsonl"
            try:
                text = client.bucket(bucket_name).blob(key).download_as_text()
            except Exception:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(row["ts"])
                    stamp = (row.get("ts"), row.get("value"), row.get("status"))
                    if ts.timestamp() >= cutoff and stamp not in seen:
                        seen.add(stamp)
                        rows.append(row)
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    continue
    except Exception:
        return []
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


@st.cache_data(ttl=60, show_spinner=False)
def load_lifeblood_catalogue(subsystem: str) -> dict:
    remotes = _lifeblood_prefixes(subsystem)
    gcs_payload = None
    if remotes:
        try:
            from google.cloud import storage

            client = storage.Client()
            for bucket_name, prefix in remotes:
                blob = client.bucket(bucket_name).blob(f"{prefix}/catalogue.json")
                try:
                    gcs_payload = json.loads(blob.download_as_text())
                    break
                except Exception:
                    continue
        except Exception:
            gcs_payload = None
    return load_catalogue(subsystem, gcs_payload=gcs_payload)


def _lifeblood_prefixes(subsystem: str) -> list[tuple[str, str]]:
    uri = _store_uri()
    if not uri.startswith("gs://"):
        return []
    bucket = uri[5:].split("/", 1)[0]
    return [(bucket, f"{LIFEBLOOD_PREFIX}/{folder}") for folder in gcs_folders(subsystem)]


def _metric_chart(
    rows: list[dict], *, grain: str | None = None
) -> dict[str, float] | None:
    series: dict[str, float] = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        val = row.get("value")
        if not isinstance(val, (int, float)):
            continue
        dims = row.get("dims") or {}
        row_grain = grain or dims.get("grain")
        if row_grain == "session":
            sid = str(dims.get("session_id") or "")
            key = sid if sid else None
            if key is None:
                try:
                    key = datetime.fromisoformat(row["ts"]).date().isoformat()
                except (KeyError, ValueError, TypeError):
                    continue
            series[key] = float(val)
            continue
        try:
            day = datetime.fromisoformat(row["ts"]).date().isoformat()
        except (KeyError, ValueError, TypeError):
            continue
        series[day] = float(val)
    if len(series) < 2:
        return None
    return series or None


def _subsystem_pulse(subsystem: str, preferred: tuple[str, ...], days: int = 14) -> dict[str, Any]:
    catalogue = load_lifeblood_catalogue(subsystem)
    pulses = []
    for metric_id in preferred[:3]:
        series = load_lifeblood_series(subsystem, metric_id, days=days)
        last = series[-1] if series else {}
        entry = metric_entry(catalogue, metric_id)
        grain = (last.get("dims") or {}).get("grain")
        pulses.append({
            "id": metric_id,
            "name": entry.get("name") or metric_id,
            "last": last,
            "chart": _metric_chart(series, grain=grain),
            "help": metric_help(entry, last),
            "n": len(series),
        })
    available = list_lifeblood_metrics(subsystem)
    return {"metrics": pulses, "available": len(available)}


def _render_metric_extras(metric_id: str, last: dict) -> None:
    dims = last.get("dims") or {}
    if metric_id == "funnel_snapshot":
        rows = funnel_stage_rows(dims)
        if rows:
            st.dataframe(rows, hide_index=True, use_container_width=True)
        return
    if metric_id == "indexed_24h":
        by_source = dims.get("by_source") or {}
        if by_source:
            st.caption("Indexed in the last 24 hours by source table.")
            st.dataframe(
                [{"source": k, "indexed_24h": v} for k, v in by_source.items()],
                hide_index=True,
                use_container_width=True,
            )
        return
    if metric_id == "audio_per_tool":
        per_tool = dims.get("per_tool") or {}
        if per_tool:
            n_tools = dims.get("layer_tools") or len(per_tool)
            layer_ops = dims.get("layer_ops")
            st.caption(
                f"Mean across {n_tools} registered harvesters"
                + (f" · {layer_ops} mechanical ops in 24h" if layer_ops is not None else "")
                + ". Zeros included. agent_rd is shown, not averaged in."
            )
            st.dataframe(
                [{"harvester": k, "unique_queued": v} for k, v in per_tool.items()],
                hide_index=True,
                use_container_width=True,
            )
        return
    if metric_id == "discovery_yield":
        per = dims.get("per_harvester") or {}
        if per:
            st.caption(
                f"{dims.get('still_pending', '?')} still pending · "
                f"{dims.get('already_ingested', '?')} already ingested"
            )
            st.dataframe(
                [{"harvester": k, "unique_queued_24h": v} for k, v in per.items()],
                hide_index=True,
                use_container_width=True,
            )
        return
    if metric_id == "campaign_coverage":
        rows = campaign_target_rows(dims)
        if rows:
            st.caption(
                f"{dims.get('targets_met', '?')}/{dims.get('targets_total', '?')} "
                f"weekly targets met in {dims.get('window_days', 14)}d"
            )
            st.dataframe(rows, hide_index=True, use_container_width=True)


def _render_story(rows: list[dict], subs: list[str]) -> None:
    counts = operator_counts(
        subs, load_operator=load_operator, load_escalations=load_escalations
    )
    landings = landing_stats(rows)
    discovery = _subsystem_pulse(
        "discovery", ("discovery_yield", "audio_per_tool", "harvest_pulse")
    )
    pipeline = _subsystem_pulse(
        "pipeline", ("indexed_24h", "backlog_depth", "dispatch_pulse")
    )

    waiting = counts["open"] + counts["incomplete"] + counts["escalations"]
    if counts["incomplete"]:
        st.error(
            f"{counts['incomplete']} incomplete master card(s) — should not happen. "
            "Open the Operator tab."
        )
    elif waiting:
        st.warning(
            f"{counts['open']} PR(s) waiting, {counts['escalations']} open escalation(s), "
            f"{counts['hold']} hold(s)."
        )
    else:
        st.success("Nothing waiting on you in the operator queue or escalation ledger.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Waiting on you", waiting)
    c2.metric("Merges (window)", landings["merges"])
    c3.metric("Master cycles stalled", landings["stalled"])
    c4.metric("Discovery metrics", discovery["available"])

    latest = landings["latest"]
    if latest:
        when = str(latest.get("ts", ""))[:19]
        st.caption(
            f"Latest timeline beat: {when} · {latest.get('agent', '?')} · "
            f"{latest.get('kind', '?')} · {latest.get('headline') or latest.get('outcome') or '—'}"
        )
    else:
        st.caption("No timeline rows in this window yet.")

    if discovery["metrics"] and ("discovery" in subs):
        st.markdown("**Discovery pulse**")
        cols = st.columns(len(discovery["metrics"]))
        for col, item in zip(cols, discovery["metrics"]):
            with col:
                st.metric(item["name"], display_value(item["last"]), help=item["help"])
                cap = updated_caption(item["last"])
                if cap:
                    st.caption(cap)
                note = staleness_note([item["last"]] if item.get("last") else [])
                if note:
                    st.caption(note)
                if item["chart"]:
                    st.line_chart(item["chart"])

    if pipeline["metrics"] and ("pipeline" in subs):
        st.markdown("**Pipeline pulse**")
        cols = st.columns(len(pipeline["metrics"]))
        for col, item in zip(cols, pipeline["metrics"]):
            with col:
                st.metric(item["name"], display_value(item["last"]), help=item["help"])
                cap = updated_caption(item["last"])
                if cap:
                    st.caption(cap)
                _render_metric_extras(item["id"], item["last"])
                if item["chart"]:
                    st.line_chart(item["chart"])


def main() -> None:
    st.set_page_config(page_title="Turnstile", layout="wide")
    st.title("Agent harness observability")
    st.caption(
        "What is waiting on you · which landings never happened · is discovery volume moving"
    )

    # Apply the default store on first load — the text input shows DEFAULT_STORE
    # but objectstore only reads os.environ, so without this configured() is False.
    if not os.getenv("TURNSTILE_AGENT_STORE"):
        _set_store(DEFAULT_STORE)

    with st.sidebar:
        st.subheader("Store")
        uri = st.text_input("TURNSTILE_AGENT_STORE", value=_store_uri())
        if uri != _store_uri():
            _set_store(uri)
            st.cache_data.clear()

        subsystem = st.selectbox("Subsystem", ["all"] + list(SUBSYSTEMS))
        limit = st.number_input(
            "Timeline limit", min_value=20, max_value=500, value=100, step=20
        )
        # Default off: full-page meta refresh was breaking Streamlit JS chunks on
        # Cloud Run. Manual refresh is the reliable operator control.
        if st.button("Refresh now", type="primary"):
            st.cache_data.clear()
            st.rerun()
        if st.button("Capture lifeblood now"):
            results = []
            for sub in (["discovery", "pipeline"] if subsystem == "all"
                        else [subsystem]):
                results.append(trigger_capture(sub))
            st.session_state["lifeblood_capture"] = results
            st.cache_data.clear()
            st.rerun()

        configured = objectstore.configured()
        st.caption(f"Store configured: {configured}")
        if not configured:
            st.warning("Set TURNSTILE_AGENT_STORE to gs://… and credentials.")
        for row in st.session_state.get("lifeblood_capture") or []:
            if row.get("ok"):
                st.success(
                    f"Started {row.get('job')} ({row.get('subsystem')})"
                )
            elif row.get("skipped"):
                st.caption(f"{row.get('subsystem')}: {row.get('skipped')}")
            else:
                st.warning(
                    f"{row.get('subsystem')}: {row.get('reason') or row}"
                )

    try:
        _live_body(subsystem, int(limit))
    except Exception as exc:  # noqa: BLE001 — surface operator-visible failures
        st.exception(exc)


def _live_body(subsystem: str, limit: int) -> None:
    sub_filter = None if subsystem == "all" else subsystem
    subs = [sub_filter] if sub_filter else list(SUBSYSTEMS)
    rows = load_timeline(sub_filter, limit)

    st.subheader("Now")
    try:
        _render_story(rows, list(subs))
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Story panel failed: {exc}")

    tab_timeline, tab_lifeblood, tab_runs, tab_escalations, tab_operator, tab_flow = st.tabs(
        ["Timeline", "Lifeblood", "Run archives", "Escalations", "Operator", "Flow check"]
    )

    with tab_timeline:
        st.subheader("Event timeline")
        if rows:
            st.code(as_table(rows), language=None)
            table_rows = [
                {name: row.get(name, "") for name, _ in COLUMNS} for row in rows
            ]
            st.dataframe(
                stringify_rows(table_rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info(
                "No timeline rows yet. Rows appear when turnstile-agent runs "
                "with TURNSTILE_AGENT_STORE set and timeline writers deployed."
            )

    with tab_lifeblood:
        st.subheader("Lifeblood metrics")
        st.caption(
            "Hover the ⓘ on a number for the catalogue definition. "
            "Numbers come from GCS snapshots written by the mechanical jobs; "
            "if those are stale, use Capture lifeblood now (or wait for the "
            "next reconciler / harvest tick)."
        )
        # Auto-kick capture once per session when the headline series is stale.
        if not st.session_state.get("lifeblood_autokick"):
            samples = {}
            for sub in subs:
                preferred = {
                    "discovery": "discovery_yield",
                    "pipeline": "backlog_depth",
                }.get(sub)
                if not preferred:
                    continue
                samples[sub] = load_lifeblood_series(sub, preferred, days=7)
            kicked = refresh_stale(list(subs), series_by_sub=samples)
            st.session_state["lifeblood_autokick"] = True
            if any(r.get("ok") for r in kicked):
                st.session_state["lifeblood_capture"] = kicked
                st.info(
                    "Stale lifeblood detected — started capture jobs. "
                    "Refresh in a couple of minutes for new points."
                )

        for sub in subs:
            metrics = list_lifeblood_metrics(sub)
            catalogue = load_lifeblood_catalogue(sub)
            st.markdown(f"**{sub}** — {len(metrics)} metric(s) in GCS")
            if not metrics:
                st.caption(
                    "No durable history for this subsystem. "
                    "Capture writes to lifeblood/"
                    + "/".join(gcs_folders(sub)[:1])
                    + " when TURNSTILE_AGENT_STORE is set on the job."
                )
                continue
            cols = st.columns(min(3, len(metrics)))
            for i, metric_id in enumerate(metrics):
                series_rows = load_lifeblood_series(sub, metric_id)
                show, latest = last_ok_or_latest(series_rows)
                entry = metric_entry(catalogue, metric_id)
                note = staleness_note(series_rows)
                with cols[i % len(cols)]:
                    st.metric(
                        entry.get("name") or metric_id,
                        display_value(show),
                        help=metric_help(entry, latest or show),
                    )
                    cap = updated_caption(latest or show)
                    if cap:
                        st.caption(cap)
                    if note:
                        st.caption(note)
                    _render_metric_extras(metric_id, show)
                    chart = _metric_chart(
                        series_rows, grain=(show.get("dims") or {}).get("grain")
                    )
                    if chart:
                        st.line_chart(chart)

    with tab_runs:
        st.subheader("Run archives")
        for sub in subs:
            keys = load_run_keys(sub, 15)
            st.markdown(f"**{sub}** — {len(keys)} recent archive(s)")
            for key in keys:
                archive = load_run_archive(key)
                if not archive:
                    continue
                report = archive.get("report") or {}
                label = (
                    f"{archive.get('role', '?')} · "
                    f"{report.get('outcome', '?')} · "
                    f"{key.rsplit('/', 1)[-1]}"
                )
                with st.expander(label):
                    st.json(archive)

    with tab_escalations:
        st.subheader("Open escalations")
        total = 0
        for sub in subs:
            entries = load_escalations(sub)
            total += len(entries)
            st.markdown(f"**{sub}** — {len(entries)} open")
            if entries:
                st.dataframe(
                    stringify_rows(entries),
                    use_container_width=True,
                    hide_index=True,
                )
        if total == 0:
            st.success("No open escalations in the ledger.")
        legacy = objectstore.get_json("agent_layer/pending/pending_agents.json")
        if legacy:
            st.warning(
                "Legacy `pending_agents.json` still exists in GCS "
                f"({len(legacy)} entries). The harness now uses per-entry "
                "escalations under agent_layer/escalations/open/."
            )

    with tab_operator:
        st.subheader("Waiting on you")
        st.caption(
            "AWAITING_OPERATOR writes a GitHub PR and a card under "
            "operator/open. HOLD parks under operator/hold. "
            "INCOMPLETE parks under operator/incomplete with a sub-reason "
            "(should not happen — escalate and inspect). Slack is not used."
        )
        for sub in subs:
            opened = load_operator("open", sub)
            held = load_operator("hold", sub)
            incomplete = load_operator("incomplete", sub)
            st.markdown(
                f"**{sub}** — {len(opened)} open, {len(held)} hold, "
                f"{len(incomplete)} incomplete"
            )
            if incomplete:
                st.error("Incomplete (should not happen)")
                st.dataframe(
                    stringify_rows(incomplete),
                    use_container_width=True,
                    hide_index=True,
                )
            if opened:
                st.markdown("Open (PR)")
                st.dataframe(
                    stringify_rows(opened),
                    use_container_width=True,
                    hide_index=True,
                )
            if held:
                st.markdown("Hold")
                st.dataframe(
                    stringify_rows(held),
                    use_container_width=True,
                    hide_index=True,
                )

    with tab_flow:
        st.subheader("Flow validation")
        problems = validate.flow(rows, subsystem=sub_filter)
        if not rows:
            st.warning("No timeline rows to validate.")
        elif problems:
            st.error("Gaps detected:")
            for line in problems:
                st.write(f"- {line}")
        else:
            st.success("Flow OK: spawn → verdict → (proposal) → master wager → route")


if __name__ == "__main__":
    main()
