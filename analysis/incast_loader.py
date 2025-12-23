"""
Load and aggregate incast experiment logs into DataFrames.

This module centralizes I/O to avoid redundant CSV/JSON reads.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

import pandas as pd

from csv_utils import find_retrans_column, find_spurious_column, normalize_columns


RUN_ID_PATTERN = re.compile(r"^run_\d{8}-\d{6}$")
PROGRESS_INTERVAL = 5


def list_run_dirs(log_root: Path, proto: str) -> List[Path]:
    proto_dir = log_root / proto
    if not proto_dir.exists():
        logging.warning("Protocol directory not found: %s", proto_dir)
        return []
    if not proto_dir.is_dir():
        logging.warning("Protocol path is not a directory: %s", proto_dir)
        return []
    run_dirs: List[Path] = []
    for entry in proto_dir.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.startswith("run_"):
            continue
        if not RUN_ID_PATTERN.match(entry.name):
            logging.warning("Ignoring run dir with unexpected name: %s", entry)
            continue
        run_dirs.append(entry)
    return sorted(run_dirs, key=lambda path: path.name)


def select_run_dirs(
    log_root: Path,
    protos: Sequence[str],
    run_ids: Sequence[str] | None,
    latest_n: int | None,
) -> Dict[str, List[Path]]:
    if run_ids and latest_n is not None:
        raise ValueError("Cannot combine --run-id with --latest-n.")
    run_dirs_by_proto: Dict[str, List[Path]] = {}
    for proto in protos:
        available = list_run_dirs(log_root, proto)
        if latest_n is not None and len(available) < latest_n:
            raise ValueError(
                f"Need at least {latest_n} run(s) for {proto} under {log_root}, "
                f"found {len(available)}."
            )
        if not available:
            logging.warning("No run_* directories found for %s under %s", proto, log_root)
            run_dirs_by_proto[proto] = []
            continue

        if run_ids:
            available_by_name = {d.name: d for d in available}
            selected = []
            for rid in run_ids:
                path = available_by_name.get(rid)
                if path is not None:
                    selected.append(path)
                else:
                    logging.warning("Requested run_id %s not found under %s", rid, proto)
        elif latest_n is not None:
            selected = available[-latest_n:]
        else:
            selected = available[-1:]
        run_dirs_by_proto[proto] = selected
    return run_dirs_by_proto


def load_elephant_goodput(csv_path: Path) -> List[float]:
    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        logging.warning("Elephant CSV not found: %s", csv_path)
        return []
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.warning("Failed to read %s: %s", csv_path, exc)
        return []

    df = normalize_columns(df)
    if "Duration" not in df or "Sent" not in df:
        logging.warning("Required columns missing in %s", csv_path)
        return []

    durations = pd.to_numeric(df["Duration"], errors="coerce")
    sent = pd.to_numeric(df["Sent"], errors="coerce")
    valid = durations.notna() & sent.notna() & (durations > 0)
    if valid.sum() == 0:
        logging.warning("No valid rows in %s", csv_path)
        return []

    # Goodput is based on client transmit direction: Sent bytes over Duration.
    goodput_mbps = (sent[valid] * 8 / durations[valid]) / 1e6
    return goodput_mbps.tolist()


def load_mouse_flows(csv_paths: Iterable[Path]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for csv_path in csv_paths:
        try:
            df = pd.read_csv(csv_path)
        except FileNotFoundError:
            logging.warning("Mouse CSV not found: %s", csv_path)
            continue
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.warning("Failed to read %s: %s", csv_path, exc)
            continue
        df = normalize_columns(df)
        if "Duration" not in df:
            logging.warning("Duration column missing in %s", csv_path)
            continue
        durations = pd.to_numeric(df["Duration"], errors="coerce")
        valid = durations.notna() & (durations > 0)
        if valid.sum() == 0:
            continue

        retrans_col = find_retrans_column(df.columns)
        if retrans_col is None:
            retrans_vals = pd.Series(0, index=df.index, dtype=float)
        else:
            retrans_vals = pd.to_numeric(df[retrans_col], errors="coerce").fillna(0)

        spurious_col = find_spurious_column(df.columns)
        if spurious_col is None:
            spurious_vals = pd.Series(0, index=df.index, dtype=float)
        else:
            spurious_vals = pd.to_numeric(df[spurious_col], errors="coerce").fillna(0)

        drop_retrans = (retrans_vals - spurious_vals).clip(lower=0)
        for duration, retrans, drop in zip(
            durations[valid], retrans_vals[valid], drop_retrans[valid]
        ):
            rows.append(
                {
                    "fct_s": float(duration),
                    "had_retrans": bool(float(retrans) > 0),
                    "had_drop_retrans": bool(float(drop) > 0),
                }
            )
    return rows


def _extract_tx_bytes(entry: Mapping[str, float]) -> float:
    try:
        return float(entry.get("tx", 0.0))
    except Exception:
        return 0.0


def load_switch_tx_deltas(before_path: Path, after_path: Path) -> Dict[str, float]:
    try:
        with before_path.open() as f:
            before = json.load(f)
        with after_path.open() as f:
            after = json.load(f)
    except FileNotFoundError:
        logging.warning("Switch stats missing: %s or %s", before_path, after_path)
        return {}
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.warning("Failed to read switch stats: %s", exc)
        return {}

    deltas: Dict[str, float] = {}
    for if_name in set(before.keys()) | set(after.keys()):
        delta = _extract_tx_bytes(after.get(if_name, {})) - _extract_tx_bytes(
            before.get(if_name, {})
        )
        deltas[if_name] = float(delta)
    # TODO: Narrow to uplinks only if interface naming allows reliable detection.
    return deltas


def collect_all_data(
    log_root: Path,
    protos: Sequence[str],
    run_dirs_by_proto: Mapping[str, Sequence[Path]] | None = None,
) -> Dict[str, pd.DataFrame]:
    elephant_rows: List[Dict[str, object]] = []
    mouse_rows: List[Dict[str, object]] = []
    link_rows: List[Dict[str, object]] = []

    if run_dirs_by_proto is None:
        run_dirs_by_proto = {proto: list_run_dirs(log_root, proto) for proto in protos}
    total_runs = sum(len(runs) for runs in run_dirs_by_proto.values())
    processed_runs = 0

    for proto in protos:
        run_dirs = run_dirs_by_proto.get(proto, [])
        for run_dir in run_dirs:
            processed_runs += 1
            if total_runs and (
                processed_runs % PROGRESS_INTERVAL == 0
                or processed_runs == total_runs
            ):
                logging.info("Collecting runs [%d/%d]", processed_runs, total_runs)
            run_id = run_dir.name
            elephant_path = run_dir / "elephant_client.csv"
            mouse_paths = sorted(run_dir.glob("mouse_client_*.csv"))
            before_path = run_dir / "switch_stats_before.json"
            after_path = run_dir / "switch_stats_after.json"

            missing = []
            if not elephant_path.exists():
                missing.append("elephant_client.csv")
            if not mouse_paths:
                missing.append("mouse_client_*.csv")
            if not before_path.exists():
                missing.append("switch_stats_before.json")
            if not after_path.exists():
                missing.append("switch_stats_after.json")
            if missing:
                logging.warning(
                    "Skipping %s/%s due to missing files: %s",
                    proto,
                    run_id,
                    ", ".join(missing),
                )
                continue

            goodputs = load_elephant_goodput(elephant_path)
            for val in goodputs:
                elephant_rows.append(
                    {"proto": proto, "run_id": run_id, "goodput_mbps": float(val)}
                )

            flows = load_mouse_flows(mouse_paths)
            for row in flows:
                mouse_rows.append({"proto": proto, "run_id": run_id, **row})

            deltas = load_switch_tx_deltas(before_path, after_path)
            for if_name, delta in deltas.items():
                link_rows.append(
                    {
                        "proto": proto,
                        "run_id": run_id,
                        "if_name": if_name,
                        "delta_tx_bytes": float(delta),
                    }
                )

    elephant_df = pd.DataFrame(
        elephant_rows, columns=["proto", "run_id", "goodput_mbps"]
    )
    mouse_df = pd.DataFrame(
        mouse_rows,
        columns=["proto", "run_id", "fct_s", "had_retrans", "had_drop_retrans"],
    )
    link_df = pd.DataFrame(
        link_rows, columns=["proto", "run_id", "if_name", "delta_tx_bytes"]
    )
    return {"elephant": elephant_df, "mouse": mouse_df, "link": link_df}
