"""
Load and aggregate normal experiment logs into DataFrames.

This mirrors the blackbox loader structure so analyze scripts stay thin.

Expected layout: logs/normal/<log-dir>/<proto>/<elephant-num>/<congestion-rate>/run_*.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import pandas as pd

from csv_utils import find_retrans_column, find_spurious_column, normalize_columns

# Accept historical run IDs (run_YYYYMMDD-HHMMSS) and new ones with seed suffix
# (run_YYYYMMDD-HHMMSS_seed12345).
RUN_ID_PATTERN = re.compile(r"^run_\d{8}-\d{6}(?:_seed\d+)?$")


def list_run_dirs(log_root: Path, proto: str) -> Dict[Tuple[int, str], List[Path]]:
    proto_dir = log_root / proto
    if not proto_dir.exists():
        logging.warning("Protocol directory not found: %s", proto_dir)
        return {}
    if not proto_dir.is_dir():
        logging.warning("Protocol path is not a directory: %s", proto_dir)
        return {}

    run_dirs: Dict[Tuple[int, str], List[Path]] = {}
    for elephant_dir in proto_dir.iterdir():
        if not elephant_dir.is_dir():
            continue
        try:
            elephant_num = int(elephant_dir.name)
        except ValueError:
            logging.warning("Ignoring elephant directory with non-integer name: %s", elephant_dir)
            continue
        for congestion_dir in elephant_dir.iterdir():
            if not congestion_dir.is_dir():
                continue
            congestion_rate = congestion_dir.name
            runs: List[Path] = []
            for entry in congestion_dir.iterdir():
                if not entry.is_dir():
                    continue
                if not entry.name.startswith("run_"):
                    continue
                if not RUN_ID_PATTERN.match(entry.name):
                    logging.warning("Ignoring run dir with unexpected name: %s", entry)
                    continue
                runs.append(entry)
            if runs:
                run_dirs[(elephant_num, congestion_rate)] = sorted(runs, key=lambda path: path.name)
            else:
                logging.warning(
                    "No run_* directories found under %s", congestion_dir
                )
    return run_dirs


def select_run_dirs(
    log_root: Path,
    protos: Sequence[str],
    run_ids: Sequence[str] | None,
    latest_n: int | None,
    elephant_nums: Sequence[int] | None,
    congestion_rates: Sequence[str] | None,
) -> Dict[str, Dict[Tuple[int, str], List[Path]]]:
    if run_ids and latest_n is not None:
        raise ValueError("Cannot combine --run-id with --latest-n.")
    run_dirs_by_proto: Dict[str, Dict[Tuple[int, str], List[Path]]] = {}
    elephant_filter = set(elephant_nums) if elephant_nums else None
    congestion_filter = set(congestion_rates) if congestion_rates else None
    for proto in protos:
        available = list_run_dirs(log_root, proto)
        selected_by_combo: Dict[Tuple[int, str], List[Path]] = {}
        for (elephant_num, congestion_rate), runs in available.items():
            if elephant_filter is not None and elephant_num not in elephant_filter:
                continue
            if congestion_filter is not None and congestion_rate not in congestion_filter:
                continue
            if run_ids:
                available_by_name = {d.name: d for d in runs}
                selected = []
                for rid in run_ids:
                    path = available_by_name.get(rid)
                    if path is not None:
                        selected.append(path)
                    else:
                        logging.warning(
                            "Requested run_id %s not found under %s/%s/%s/%s",
                            rid,
                            log_root,
                            proto,
                            elephant_num,
                            congestion_rate,
                        )
            elif latest_n is not None:
                if len(runs) < latest_n:
                    raise ValueError(
                        f"Need at least {latest_n} run(s) for {proto} under {log_root} "
                        f"(elephant={elephant_num}, congestion={congestion_rate}), "
                        f"found {len(runs)}."
                    )
                selected = runs[-latest_n:]
            else:
                selected = runs[-1:]

            if selected:
                selected_by_combo[(elephant_num, congestion_rate)] = selected
        if not selected_by_combo:
            logging.warning(
                "No run_* directories found for %s under %s matching filters",
                proto,
                log_root,
            )
        run_dirs_by_proto[proto] = selected_by_combo
    return run_dirs_by_proto


def _pair_id_from_filename(path: Path, prefix: str) -> str:
    """
    Extract a pair identifier from a filename with a known prefix.
    Examples: elephant_00.csv -> "00", mouse_01_0003.csv -> "01_0003".
    """
    stem = path.stem
    if stem.startswith(prefix):
        suffix = stem[len(prefix) :]
        if suffix.startswith("_"):
            suffix = suffix[1:]
        return suffix or stem
    return stem


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

    # Goodput based on client transmit direction: Sent bytes over Duration.
    goodput_mbps = (sent[valid] * 8 / durations[valid]) / 1e6
    return goodput_mbps.tolist()


def load_mouse_fcts(csv_paths: Iterable[Path]) -> List[Dict[str, object]]:
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
        spurious_col = find_spurious_column(df.columns)
        if retrans_col is None:
            retrans_vals = pd.Series(0, index=df.index, dtype=float)
        else:
            retrans_vals = pd.to_numeric(df[retrans_col], errors="coerce").fillna(0)
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


def load_switch_tx_deltas(
    before_path: Path, after_path: Path
) -> Dict[str, float]:
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


def load_link_timeseries(path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        logging.warning("Link timeseries missing: %s", path)
        return pd.DataFrame(
            columns=["sample_idx", "elapsed_s", "if_name", "delta_tx_bytes", "u_l", "dt_s"]
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.warning("Failed to read link timeseries %s: %s", path, exc)
        return pd.DataFrame(
            columns=["sample_idx", "elapsed_s", "if_name", "delta_tx_bytes", "u_l", "dt_s"]
        )
    # Keep only expected columns if extras were added.
    expected = ["sample_idx", "elapsed_s", "if_name", "delta_tx_bytes", "u_l", "dt_s"]
    missing = [col for col in expected if col not in df.columns]
    if missing:
        logging.warning("Link timeseries %s missing columns: %s", path, ", ".join(missing))
        return pd.DataFrame(columns=expected)
    return df[expected]


def collect_all_data(
    log_root: Path,
    protos: Sequence[str],
    run_dirs_by_proto: Mapping[str, Mapping[Tuple[int, str], Sequence[Path]]] | None = None,
) -> Dict[str, pd.DataFrame]:
    elephant_rows: List[Dict[str, object]] = []
    mouse_rows: List[Dict[str, object]] = []
    link_rows: List[Dict[str, object]] = []
    link_ts_frames: List[pd.DataFrame] = []

    if run_dirs_by_proto is None:
        run_dirs_by_proto = {proto: list_run_dirs(log_root, proto) for proto in protos}

    for proto in protos:
        proto_runs = run_dirs_by_proto.get(proto, {})
        for (elephant_num, congestion_rate), run_dirs in proto_runs.items():
            for run_dir in run_dirs:
                run_id = run_dir.name
                elephant_paths = sorted(run_dir.glob("elephant_*.csv"))
                mouse_paths = sorted(run_dir.glob("mouse_*.csv"))
                before_path = run_dir / "switch_stats_before.json"
                after_path = run_dir / "switch_stats_after.json"
                link_ts_path = run_dir / "link_timeseries.csv"

                missing = []
                if not elephant_paths:
                    missing.append("elephant_*.csv")
                if not mouse_paths:
                    missing.append("mouse_*.csv")
                if not link_ts_path.exists():
                    if not before_path.exists():
                        missing.append("switch_stats_before.json")
                    if not after_path.exists():
                        missing.append("switch_stats_after.json")
                if missing:
                    logging.warning(
                        "Skipping %s/%s/%s/%s due to missing files: %s",
                        proto,
                        elephant_num,
                        congestion_rate,
                        run_id,
                        ", ".join(missing),
                    )
                    if link_ts_path.exists():
                        # Allow missing before/after when timeseries is present.
                        missing = [m for m in missing if not m.startswith("switch_stats")]
                    if missing:
                        continue

                for csv_path in elephant_paths:
                    pair_id = _pair_id_from_filename(csv_path, "elephant")
                    for val in load_elephant_goodput(csv_path):
                        elephant_rows.append(
                            {
                                "proto": proto,
                                "elephant_num": elephant_num,
                                "congestion_rate": congestion_rate,
                                "run_id": run_id,
                                "pair_id": pair_id,
                                "goodput_mbps": float(val),
                            }
                        )

                for csv_path in mouse_paths:
                    pair_id = _pair_id_from_filename(csv_path, "mouse")
                    for row in load_mouse_fcts([csv_path]):
                        mouse_rows.append(
                            {
                                "proto": proto,
                                "elephant_num": elephant_num,
                                "congestion_rate": congestion_rate,
                                "run_id": run_id,
                                "pair_id": pair_id,
                                **row,
                            }
                        )

                if link_ts_path.exists():
                    ts_df = load_link_timeseries(link_ts_path)
                    if not ts_df.empty:
                        ts_df = ts_df.copy()
                        ts_df["proto"] = proto
                        ts_df["elephant_num"] = elephant_num
                        ts_df["congestion_rate"] = congestion_rate
                        ts_df["run_id"] = run_id
                        link_ts_frames.append(ts_df)
                        totals = ts_df.groupby("if_name")["delta_tx_bytes"].sum().reset_index()
                        for _, row in totals.iterrows():
                            link_rows.append(
                                {
                                    "proto": proto,
                                    "elephant_num": elephant_num,
                                    "congestion_rate": congestion_rate,
                                    "run_id": run_id,
                                    "if_name": row["if_name"],
                                    "delta_tx_bytes": float(row["delta_tx_bytes"]),
                                }
                            )
                else:
                    deltas = load_switch_tx_deltas(before_path, after_path)
                    for if_name, delta in deltas.items():
                        link_rows.append(
                            {
                                "proto": proto,
                                "elephant_num": elephant_num,
                                "congestion_rate": congestion_rate,
                                "run_id": run_id,
                                "if_name": if_name,
                                "delta_tx_bytes": float(delta),
                            }
                        )

    elephant_df = pd.DataFrame(
        elephant_rows,
        columns=[
            "proto",
            "elephant_num",
            "congestion_rate",
            "run_id",
            "pair_id",
            "goodput_mbps",
        ],
    )
    mouse_df = pd.DataFrame(
        mouse_rows,
        columns=[
            "proto",
            "elephant_num",
            "congestion_rate",
            "run_id",
            "pair_id",
            "fct_s",
            "had_retrans",
            "had_drop_retrans",
        ],
    )
    link_df = pd.DataFrame(
        link_rows,
        columns=[
            "proto",
            "elephant_num",
            "congestion_rate",
            "run_id",
            "if_name",
            "delta_tx_bytes",
        ],
    )
    link_ts_df = (
        pd.concat(link_ts_frames, ignore_index=True)
        if link_ts_frames
        else pd.DataFrame(
            columns=[
                "elephant_num",
                "congestion_rate",
                "sample_idx",
                "elapsed_s",
                "if_name",
                "delta_tx_bytes",
                "u_l",
                "dt_s",
                "proto",
                "run_id",
            ]
        )
    )
    return {"elephant": elephant_df, "mouse": mouse_df, "link": link_df, "link_ts": link_ts_df}
