from __future__ import annotations

from typing import Sequence

import pandas as pd


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _normalize_column_name(name: str) -> str:
    key = str(name).strip().lower()
    if key.endswith("."):
        key = key[:-1]
    return key


def pick_column(columns: Sequence[str], candidates: Sequence[str]) -> str | None:
    normalized = {_normalize_column_name(col): col for col in columns}
    for cand in candidates:
        key = _normalize_column_name(cand)
        if key in normalized:
            return normalized[key]
    return None


RETRANS_COLUMN_CANDIDATES = (
    "retrans.",
    "retrans",
    "retransmissions",
    "retransmission",
)
SPURIOUS_COLUMN_CANDIDATES = ("spurious", "spurious retransmissions")


def find_retrans_column(columns: Sequence[str]) -> str | None:
    return pick_column(columns, RETRANS_COLUMN_CANDIDATES)


def find_spurious_column(columns: Sequence[str]) -> str | None:
    return pick_column(columns, SPURIOUS_COLUMN_CANDIDATES)
