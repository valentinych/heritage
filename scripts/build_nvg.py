#!/usr/bin/env python3
"""Assemble the Novogrudok grid from В1рас and В2рас.

Reads the two exported CSVs and writes the same nine columns as the reference
sheet «Новогрудский уезд»: the eight В1рас columns, then «Том» (1 or 2).

A row is kept when its uyezd cell is Novogrudok uyezd, including the source
spellings «Новогрудский», «Новогрудский уезд», «Ново-Грудск. у.»,
«Новогрудск. у.», «Новогр. у.», «Новогрудок. у.», «Новогрудек. у.»,
«Новоградск. у.», and the same words in old orthography. It is also kept when
the place cell is the town of Novogrudok and the governorate cell is Minsk.
Novograd-Volynsk, a Lomza Novogrudok, and a different place that only shares
the stem are left out.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys

COL_GUB = 1
COL_UYEZD = 2
COL_PLACE = 4
COL_PAGE = 7
N_COLS = 8

HEADER = [
    "Фамилія, имя, отчество главы семьи и имена или количество состоящихъ при немъ членовъ семьи.",
    "Губерния",
    "Уезд",
    "Волость",
    "Нас пункт",
    "Мѣсто жительства въ настоящее время.",
    "Почтовый адресъ.",
    "Страница",
    "Том",
]

TR = str.maketrans(
    {
        "ѣ": "е",
        "Ѣ": "е",
        "і": "и",
        "І": "и",
        "ѳ": "ф",
        "Ѳ": "ф",
        "ё": "е",
        "Ё": "е",
        "ъ": "",
        "Ъ": "",
    }
)

# Whole uyezd cell, after the trailing «у.» / «уезд» is removed.
UYEZD_KEYS = {
    "новогрудский",
    "новогрудского",
    "новогрудскаго",
    "новогрудском",
    "новогрудск",
    "новогруд",
    "новогрудок",
    "новогрудек",
    "новогр",
    "новоградск",
}

# Whole place cell. The town, not «д. …» and not a hyphen broken across rows.
TOWN_KEYS = {
    "гор. новогрудск",
    "г. новогрудск",
    "гор. новогрудок",
    "г. новогрудок",
    "новогрудск",
    "новогрудок",
    "новогр",
}


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def norm(text):
    text = (text or "").translate(TR).replace("i", "и").replace("I", "И")
    return re.sub(r"\s+", " ", text.strip())


def is_volyn(text):
    return re.search(r"вол[ыи]н", norm(text).lower()) is not None


def is_minsk(text):
    return re.search(r"минс|минек", norm(text).lower()) is not None


def uyezd_key(text):
    token = norm(text).lower()
    token = re.sub(r"(?:\s+уезд|\s*у)\.?\s*$", "", token)
    return re.sub(r"[^а-я]", "", token)


def mentions_novogrudok(row):
    """Novogrudok uyezd, or the town of Novogrudok in Minsk governorate."""
    gub = row[COL_GUB]
    uyezd = row[COL_UYEZD]
    place = row[COL_PLACE]
    if not is_volyn(gub) and not is_volyn(uyezd) and uyezd_key(uyezd) in UYEZD_KEYS:
        return True
    if is_minsk(gub) and not is_volyn(uyezd):
        return norm(place).lower() in TOWN_KEYS
    return False


def cells_of(row):
    row = list(row) + [""] * N_COLS
    return row[:N_COLS]


def sort_key(item):
    name = norm(item["cells"][0]).lower()
    return (name, item["tom"], item["source_row"])


def collect(rows, tab, tom):
    found = []
    for sheet_row, raw in enumerate(rows[1:], start=2):
        cells = cells_of(raw)
        if not any(cells):
            continue
        if not mentions_novogrudok(cells):
            continue
        found.append(
            {
                "tab": tab,
                "tom": tom,
                "source_row": sheet_row,
                "cells": cells,
            }
        )
    return found


def grid_of(items):
    body = []
    for item in items:
        body.append(item["cells"] + [item["tom"]])
    return [HEADER, *body]


def write_csv(path, grid):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(grid)


def write_json(path, items, grid):
    payload = {
        "header": HEADER,
        "rows": [
            {
                "source_tab": item["tab"],
                "source_row": item["source_row"],
                "tom": item["tom"],
                "cells": row,
            }
            for item, row in zip(items, grid[1:])
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1", required=True, help="CSV export of В1рас")
    parser.add_argument("--v2", required=True, help="CSV export of В2рас")
    parser.add_argument("--csv", required=True, help="where to write the grid CSV")
    parser.add_argument("--json", required=True, help="where to write the grid JSON")
    args = parser.parse_args()

    items = collect(load_csv(args.v1), "В1рас", "1")
    items += collect(load_csv(args.v2), "В2рас", "2")
    items.sort(key=sort_key)
    grid = grid_of(items)
    write_csv(args.csv, grid)
    write_json(args.json, items, grid)

    print(f"rows {len(items)}")
    for item in items:
        name = item["cells"][0].replace("\n", " ")
        print(f"{item['tab']}\t{item['source_row']}\t{item['cells'][COL_PAGE]}\t{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
