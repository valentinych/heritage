#!/usr/bin/env python3
"""Rebuild changed В1рас cells from Выпуск 1, the gazetteer, and the glossary.

Reads exported CSVs. Expands a governorate, uyezd, or volost abbreviation only
when one gazetteer entry matches, and moves a household name only when one
nearby В1рас row has the same surname and ages as that Выпуск 1 row. Everything
else is left as it is.

«г.» after a number is not a governorate. «д.» after a number is not a village.
Tokens with more than one glossary meaning are not expanded.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict

# Sheet columns on В1рас.
COL_NAME = 0
COL_GUB = 1
COL_UYEZD = 2
COL_VOL = 3
COL_PLACE = 4
COL_NOW = 5
COL_POST = 6
COL_PAGE = 7

TR = str.maketrans(
    {
        "ѣ": "е",
        "Ѣ": "е",
        "і": "и",
        "І": "и",
        "ѳ": "ф",
        "Ѳ": "ф",
        "ө": "ф",
        "Ө": "ф",
        "ѵ": "и",
        "Ѵ": "и",
        "ё": "е",
        "Ё": "е",
        "ъ": "",
        "Ъ": "",
        "ь": "",
        "Ь": "",
    }
)

# At least four letters, so «и.» and «г.» alone are not place names.
NAME_RE = (
    r"[A-Za-zА-Яа-яЁёІіѢѣѲѳӨөѴѵ]"
    r"[A-Za-zА-Яа-яЁёІіѢѣѲѳӨөѴѵ\-]{3,}"
)
# Abbreviated unit: «Минск. губ.», «Ново-Грудск. у.», «Бѣльскаго у.».
# The name must start after a boundary, not in the middle of «К.-Подол».
UNIT_RE = re.compile(
    rf"(?:^|(?<=[\s,;]))"
    rf"(?P<name>{NAME_RE})"
    rf"\.?\s*"
    rf"(?P<kind>губерн\w*|губ|уѣзд\w*|уезд\w*|волост\w*|вол|гм|г|у|в)\.",
    re.IGNORECASE,
)
BOTH_RE = re.compile(
    rf"(?:^|(?<=[\s,;]))"
    rf"(?P<name>{NAME_RE})"
    rf"\.?\s*г(?:уб)?\.?\s*и\s*у\.",
    re.IGNORECASE,
)
# «Барнаул» + ск = Барнаулск. Longer suffixes only inside a known parent district,
# so «Ново-Александр» does not jump from Lublin to Kovno.
PREFIX_SHORT = {"ск"}
PREFIX_SCOPED = {"ск", "енск", "овск", "евск", "инск"}
NOVO_GRUD_RE = re.compile(r"ново[\s\-]*груд", re.IGNORECASE)


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def fold(text):
    text = text.translate(TR).lower().replace("i", "и")
    return re.sub(r"[^а-я]", "", text)


def modern_orth(text):
    """ѣ→е, і and Latin i-as-і→и, ѳ/ө→ф, ѵ→и, drop a final ъ."""
    text = (
        text.replace("ѣ", "е")
        .replace("Ѣ", "Е")
        .replace("і", "и")
        .replace("І", "И")
        .replace("ѳ", "ф")
        .replace("Ѳ", "Ф")
        .replace("ө", "ф")
        .replace("Ө", "Ф")
        .replace("ѵ", "и")
        .replace("Ѵ", "И")
    )
    text = re.sub(
        r"(?<=[А-Яа-яЁё])[iI](?=[А-Яа-яЁё])",
        lambda m: "и" if m.group(0) == "i" else "И",
        text,
    )
    text = re.sub(r"ъ(?=$|[^А-Яа-яЁёІі])", "", text)
    return text


def stem_of(folded):
    """Drop the grammatical ending and keep -ск: Минская → минск, Новогрудский → новогрудск."""
    for suf in (
        "ского",
        "цкого",
        "аго",
        "яго",
        "ого",
        "ую",
        "ской",
        "цкой",
        "ий",
        "ый",
        "ая",
        "ое",
        "ой",
    ):
        if folded.endswith(suf) and len(folded) - len(suf) >= 3:
            return folded[: -len(suf)]
    return folded


def abbr_candidates(token):
    folded = fold(token)
    found = []
    for item in (folded,):
        if item and item not in found:
            found.append(item)
    for suf in ("аго", "яго", "ого", "ую"):
        if folded.endswith(suf) and len(folded) - len(suf) >= 4:
            cut = folded[: -len(suf)]
            if cut not in found:
                found.append(cut)
    extra = []
    for item in found:
        if item.endswith("ск") and len(item) > 5:
            extra.append(item[:-2])
        elif item.endswith("цк") and len(item) > 5:
            extra.append(item[:-2])
    for item in extra:
        if item not in found:
            found.append(item)
    return [item for item in found if len(item) >= 4]


def stems_match(candidates, official_fold, official_stem, allow_prefix):
    allowed = PREFIX_SCOPED if allow_prefix else PREFIX_SHORT
    for cand in candidates:
        if cand == official_stem or cand == official_fold:
            return True
        if len(cand) < 5:
            continue
        if official_stem.startswith(cand) and official_stem[len(cand) :] in allowed:
            return True
        if (
            len(official_stem) >= 4
            and cand.startswith(official_stem)
            and cand[len(official_stem) :] in allowed
        ):
            return True
    return False


def case_of(token):
    raw = fold(token)
    if raw.endswith(("аго", "яго", "ого")):
        return "gen"
    if raw.endswith(("ской", "цкой")) and not raw.endswith(("ский", "цкий")):
        return "gen"
    return "nom"


def gen_adj(adj):
    for suf, rep in (
        ("ский", "ского"),
        ("цкий", "цкого"),
        ("ская", "ской"),
        ("цкая", "цкой"),
        ("ый", "ого"),
        ("ий", "его"),
        ("ой", "ого"),
    ):
        if adj.endswith(suf) and len(adj) > len(suf) + 2:
            return adj[: -len(suf)] + rep
    return None


NOUNS = {
    "gub": ("губерния", "губернии"),
    "uy": ("уезд", "уезда"),
    "vol": ("волость", "волости"),
}


def phrase(official, kind, case):
    name = modern_orth(official).strip()
    low = name.lower()
    if kind == "gub" and low.endswith("область"):
        if case == "nom":
            return name
        parts = name.split()
        adj = gen_adj(parts[0])
        if not adj:
            return None
        parts[0] = adj
        parts[-1] = "области"
        return " ".join(parts)
    noun = NOUNS[kind][1 if case == "gen" else 0]
    if low.endswith(NOUNS[kind][0]) or low.endswith(NOUNS[kind][1]):
        return name
    head = gen_adj(name) if case == "gen" else name
    if not head:
        return None
    return f"{head} {noun}"


def kind_code(kind):
    k = kind.lower().rstrip(".")
    if k.startswith("губ"):
        return "gub"
    if k.startswith("у"):
        return "uy"
    if k.startswith("вол") or k == "в":
        return "vol"
    if k == "г":
        return "gub"
    if k == "гм":
        return None
    return None


class Gazetteer:
    def __init__(self, rows):
        self.gubs = []
        self.uys = []
        self.vols = []
        seen_g = set()
        seen_u = set()
        seen_v = set()
        for row in rows[1:]:
            gub = row[0].strip() if row else ""
            uy = row[1].strip() if len(row) > 1 else ""
            vol = row[2].strip() if len(row) > 2 else ""
            gf = fold(gub)
            gs = stem_of(gf)
            gkey = (gf, gs, gub)
            if gub and gkey not in seen_g:
                seen_g.add(gkey)
                self.gubs.append({"name": gub, "fold": gf, "stem": gs})
            uf = fold(uy)
            us = stem_of(uf)
            ukey = (gf, uf, uy)
            if uy and ukey not in seen_u:
                seen_u.add(ukey)
                self.uys.append(
                    {"gub_fold": gf, "gub_stem": gs, "name": uy, "fold": uf, "stem": us}
                )
            vf = fold(vol)
            vs = stem_of(vf)
            vkey = (gf, uf, vf, vol)
            if vol and vkey not in seen_v:
                seen_v.add(vkey)
                self.vols.append(
                    {
                        "gub_fold": gf,
                        "gub_stem": gs,
                        "uy_fold": uf,
                        "uy_stem": us,
                        "name": vol,
                        "fold": vf,
                        "stem": vs,
                    }
                )

    def resolve_gub(self, cell):
        if not cell or not cell.strip():
            return None
        token = UNIT_RE.search(cell)
        raw = token.group("name") if token and kind_code(token.group("kind")) == "gub" else cell
        cands = abbr_candidates(raw) or ([fold(raw)] if len(fold(raw)) >= 4 else [])
        hits = []
        for gub in self.gubs:
            if stems_match(cands, gub["fold"], gub["stem"], True):
                if gub["name"] not in hits:
                    hits.append(gub["name"])
        return hits[0] if len(hits) == 1 else None

    def resolve_uy(self, cell):
        if not cell or "." in cell:
            return None
        cands = abbr_candidates(cell) or ([fold(cell)] if len(fold(cell)) >= 4 else [])
        hits = []
        seen = set()
        for uy in self.uys:
            if not stems_match(cands, uy["fold"], uy["stem"], False):
                continue
            key = (uy["gub_fold"], uy["name"])
            if key in seen:
                continue
            seen.add(key)
            hits.append(uy)
        return hits[0] if len(hits) == 1 else None

    def _parents(self, kind, hits):
        if kind == "gub":
            return {item["name"] for item in hits}
        if kind == "uy":
            return {(item["gub_fold"], item["name"]) for item in hits}
        return {(item["gub_fold"], item["uy_fold"], item["name"]) for item in hits}

    def match(self, token, kind, gub_name=None, uy_name=None):
        """Return official names. Several parents for one spelling are not a match."""
        cands = abbr_candidates(token)
        if not cands:
            return []
        gub = None
        if gub_name:
            gub = (fold(gub_name), stem_of(fold(gub_name)))
        if kind == "gub":
            found = [
                g
                for g in self.gubs
                if stems_match(cands, g["fold"], g["stem"], True)
            ]
            if len(self._parents(kind, found)) != 1:
                return [item["name"] for item in found]
            return [found[0]["name"]]
        if kind == "uy":
            pool = self.uys
            allow_prefix = False
            if gub:
                pool = [
                    uy
                    for uy in pool
                    if uy["gub_fold"] == gub[0] or uy["gub_stem"] == gub[1]
                ]
                allow_prefix = True
            found = [
                uy
                for uy in pool
                if stems_match(cands, uy["fold"], uy["stem"], allow_prefix)
            ]
            if len(self._parents(kind, found)) != 1:
                return [item["name"] for item in found]
            return [found[0]["name"]]
        pool = self.vols
        allow_prefix = False
        if uy_name:
            uf, us = fold(uy_name), stem_of(fold(uy_name))
            scoped = [vol for vol in pool if vol["uy_fold"] == uf or vol["uy_stem"] == us]
            if gub:
                scoped = [
                    vol
                    for vol in scoped
                    if vol["gub_fold"] == gub[0] or vol["gub_stem"] == gub[1]
                ]
            pool = scoped
            allow_prefix = True
        elif gub:
            pool = [
                vol
                for vol in pool
                if vol["gub_fold"] == gub[0] or vol["gub_stem"] == gub[1]
            ]
            allow_prefix = True
        found = [
            vol
            for vol in pool
            if stems_match(cands, vol["fold"], vol["stem"], allow_prefix)
        ]
        if len(self._parents(kind, found)) != 1:
            return [item["name"] for item in found]
        return [found[0]["name"]]


def load_glossary(rows):
    """abbr -> set of meanings. Several rows for one abbr are merged."""
    meanings = defaultdict(set)
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        keys = [part.strip() for part in row[0].split(",") if part.strip()]
        raw = row[1] if len(row) > 1 else ""
        parts = [part.strip(" .") for part in raw.split(",") if part.strip(" .")]
        for key in keys:
            for part in parts:
                meanings[key.lower()].add(part)
    return meanings


def surname_key(text):
    text = modern_orth(text).lower().replace("ъ", "")
    match = re.match(r"\s*([а-яё\-]+)", text)
    if not match:
        return ""
    word = match.group(1).replace("ё", "е")
    if word.startswith("алексюк"):
        word = "алексеюк" + word[len("алексюк") :]
    return word


def ages_of(text):
    return tuple(re.findall(r"\d+", text or ""))


def cell(row, idx):
    if idx < len(row):
        return row[idx]
    return ""


def expand_text(text, gaz, gub_name, uy_name, ambiguous):
    """Replace deterministic unit abbreviations inside one cell."""
    if not text or "." not in text:
        return text, gub_name, uy_name

    def consider(name, kind, start, end):
        code = kind_code(kind)
        if code is None:
            return None
        # «г.» is gubernia only when the stem is one governorate.
        # A leading «г.» (city) is not this pattern: the name stands before «г.».
        if kind.lower().rstrip(".") == "г":
            hits = gaz.match(name, "gub")
            if len(hits) != 1:
                ambiguous[(name + ".", "г.: губерния или город")] += 1
                return None
        else:
            hits = gaz.match(name, code, gub_name, uy_name)
        if len(hits) == 1:
            # A volost title has to be a feminine adjective («Любчанская»),
            # otherwise «волость» does not agree and the gazetteer row is a noun.
            if code == "vol":
                title = modern_orth(hits[0]).strip()
                if not title.lower().endswith(("ая", "яя")):
                    ambiguous[(name + ".", "волость: не прилагательное")] += 1
                    return None
            written = phrase(hits[0], code, case_of(name))
            if not written:
                ambiguous[(name + " " + kind, "падеж")] += 1
                return None
            return written, hits[0], code
        label = name + "."
        if len(hits) == 0:
            ambiguous[(label, code + ":нет в списке")] += 1
        else:
            ambiguous[(label, code + ":" + str(len(hits)))] += 1
        return None

    # «Минск. г. и у.» is both units of one stem.
    pieces = []
    cursor = 0
    for match in BOTH_RE.finditer(text):
        if "." not in match.group(0):
            continue
        decided = consider(match.group("name"), "г.", match.start(), match.end())
        uy_decided = consider(match.group("name"), "у.", match.start(), match.end())
        if not decided or not uy_decided:
            continue
        gub_written, gub_hit, _ = decided
        uy_written, uy_hit, _ = uy_decided
        pieces.append(text[cursor : match.start()])
        pieces.append(f"{gub_written} и {uy_written}")
        cursor = match.end()
        gub_name = gub_hit
        uy_name = uy_hit
    if pieces:
        pieces.append(text[cursor:])
        text = "".join(pieces)

    # If an uyezd in this same cell does not resolve, a volost match would be
    # taken from some other district. Leave those volosts.
    uyezd_unresolved = False
    parsed = []
    for match in UNIT_RE.finditer(text):
        span = match.group(0)
        if "." not in span or not match.group("name")[:1].isupper():
            continue
        if re.search(r"\bи\s+у\.", text[match.end() : match.end() + 6] or "", re.I):
            if match.group("kind").lower().startswith("г"):
                continue
        code = kind_code(match.group("kind"))
        parsed.append((match, code))
        if code == "uy":
            hits = gaz.match(match.group("name"), "uy", gub_name, uy_name)
            if len(hits) != 1:
                uyezd_unresolved = True

    replacements = []
    for match, code in parsed:
        if code == "vol" and uyezd_unresolved and not uy_name:
            ambiguous[(match.group("name") + ".", "волость при неразобранном уезде")] += 1
            continue
        decided = consider(match.group("name"), match.group("kind"), match.start(), match.end())
        if not decided:
            continue
        written, official, code = decided
        replacements.append((match.start(), match.end(), written, official, code))

    if not replacements:
        return text, gub_name, uy_name
    out = []
    cursor = 0
    for start, end, written, official, code in replacements:
        out.append(text[cursor:start])
        out.append(written)
        cursor = end
        if code == "gub":
            gub_name = official
        elif code == "uy":
            uy_name = official
    out.append(text[cursor:])
    return "".join(out), gub_name, uy_name


def build_name_index(v1):
    index = defaultdict(list)
    for row_i, row in enumerate(v1):
        if row_i == 0:
            continue
        index[(surname_key(cell(row, COL_NAME)), ages_of(cell(row, COL_NAME)))].append(row_i)
    return index


def aligned_name(v1, index, g0_row, row_i):
    """Return the modernized household if exactly one nearby row carries it."""
    source = cell(g0_row, COL_NAME)
    key = (surname_key(source), ages_of(source))
    if not key[0]:
        return None
    near = [j for j in index.get(key, []) if abs(j - row_i) <= 3]
    if len(near) != 1:
        return None
    return cell(v1[near[0]], COL_NAME)


def ambiguous_name_tokens(names, glossary):
    counts = Counter()
    amb = {key: values for key, values in glossary.items() if len(values) > 1}
    # Longer keys first so «анас.» wins over a shorter prefix.
    keys = sorted(amb, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![А-Яа-яA-Za-z])(" + "|".join(re.escape(key) for key in keys) + r")",
        re.IGNORECASE,
    )
    for name in names:
        if not name:
            continue
        for match in pattern.finditer(name):
            counts[match.group(1).lower()] += 1
    return counts


def self_check_uyezd(g0, v1, gaz):
    """Already-expanded uyezd cells must agree with a unique gazetteer hit."""
    disagree = []
    for row_i in range(1, min(len(g0), len(v1))):
        current = cell(v1[row_i], COL_UYEZD).strip()
        if not current or "." in current or re.search(r"\bу(?:езд|ѣзд)", current, re.I):
            continue
        source = cell(g0[row_i], 1)
        found = UNIT_RE.findall(source)
        uy_names = [name for name, kind in found if kind_code(kind) == "uy"]
        if len(uy_names) != 1:
            continue
        gub_name = gaz.resolve_gub(cell(v1[row_i], COL_GUB))
        hits = gaz.match(uy_names[0], "uy", gub_name)
        if len(hits) != 1:
            continue
        if stem_of(fold(hits[0])) != stem_of(fold(current)):
            disagree.append((row_i + 1, uy_names[0], hits[0], current))
    return disagree


def rebuild(vypusk, current, gaz, glossary):
    index = build_name_index(current)
    ambiguous = Counter()
    changes = []
    novogrud = []
    name_moves = 0
    place_cells = 0
    limit = min(len(vypusk), len(current))
    final_names = []

    for row_i in range(1, limit):
        grow = vypusk[row_i]
        crow = list(current[row_i])
        while len(crow) <= COL_PAGE:
            crow.append("")
        new_name = aligned_name(current, index, grow, row_i)
        if new_name is not None and new_name != crow[COL_NAME]:
            changes.append((row_i + 1, COL_NAME, crow[COL_NAME], new_name))
            crow[COL_NAME] = new_name
            name_moves += 1
        elif new_name is None and surname_key(cell(grow, COL_NAME)) and (
            surname_key(crow[COL_NAME]) != surname_key(cell(grow, COL_NAME))
            or ages_of(crow[COL_NAME]) != ages_of(cell(grow, COL_NAME))
        ):
            ambiguous[("household:" + surname_key(cell(grow, COL_NAME)), "имя не сведено")] += 1
        final_names.append(crow[COL_NAME])

        # Former home (B–E) shares one district. Current home (F–G) is a different place.
        gub_name = gaz.resolve_gub(crow[COL_GUB])
        uy_hit = gaz.resolve_uy(crow[COL_UYEZD])
        uy_name = uy_hit["name"] if uy_hit else None
        for col in (COL_GUB, COL_UYEZD, COL_VOL, COL_PLACE, COL_NOW, COL_POST):
            if col == COL_NOW:
                gub_name, uy_name = None, None
            updated, gub_name, uy_name = expand_text(
                crow[col], gaz, gub_name, uy_name, ambiguous
            )
            if updated != crow[col]:
                changes.append((row_i + 1, col, crow[col], updated))
                if NOVO_GRUD_RE.search(crow[col]) and "Новогрудский" in updated:
                    novogrud.append((row_i + 1, col, crow[col], updated))
                crow[col] = updated
                place_cells += 1
    return changes, ambiguous, novogrud, name_moves, place_cells, final_names


def col_letter(idx):
    return "ABCDEFGH"[idx]


def write_patch(path, changes):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["row", "col", "old", "new"])
        for row, col, old, new in changes:
            writer.writerow([row, col_letter(col), old, new])


def chunks_of(changes, size=40):
    """Contiguous same-column runs, at most `size` rows."""
    by_col = defaultdict(dict)
    for row, col, _old, new in changes:
        by_col[col][row] = new
    groups = []
    for col, mapping in by_col.items():
        rows = sorted(mapping)
        if not rows:
            continue
        run = [rows[0]]
        for row in rows[1:]:
            if row == run[-1] + 1 and len(run) < size:
                run.append(row)
            else:
                groups.append((col, run[0], run[-1], [mapping[r] for r in run]))
                run = [row]
        groups.append((col, run[0], run[-1], [mapping[r] for r in run]))
    groups.sort(key=lambda item: (item[1], item[0]))
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gazetteer", required=True)
    parser.add_argument("--vypusk", required=True)
    parser.add_argument("--glossary", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--patch", required=True)
    args = parser.parse_args()

    gaz = Gazetteer(load_csv(args.gazetteer))
    vypusk = load_csv(args.vypusk)
    current = load_csv(args.current)
    glossary = load_glossary(load_csv(args.glossary))

    disagree = self_check_uyezd(vypusk, current, gaz)
    if disagree:
        print(f"ОСТАНОВ: сверка уезда разошлась в {len(disagree)} уже раскрытых ячейках", file=sys.stderr)
        for item in disagree[:10]:
            print(" ", item, file=sys.stderr)
        return 2

    changes, ambiguous, novogrud, name_moves, place_cells, final_names = rebuild(
        vypusk, current, gaz, glossary
    )
    write_patch(args.patch, changes)

    buyvol_rows = {}
    for row, col, old, new in changes:
        if row in (630, 631):
            buyvol_rows.setdefault(row, {})[col_letter(col)] = (old, new)

    print(f"ячеек к записи: {len(changes)}")
    print(f"из них имена (сдвиг к строке Выпуска 1): {name_moves}")
    print(f"из них места (губерния/уезд/волость): {place_cells}")
    print(f"раскрытий Ново-Грудск. у. → Новогрудский уезд: {len(novogrud)}")
    for item in novogrud:
        print(f"  строка {item[0]} столбец {col_letter(item[1])}: {item[2]!r} → {item[3]!r}")

    print("Буйвол, строки 630–631, меняемые ячейки:")
    for row in (630, 631):
        print(f"  {row}:")
        for col, (old, new) in sorted(buyvol_rows.get(row, {}).items()):
            print(f"    {col}: {old!r} → {new!r}")

    # Acceptance: the two households sit on their Выпуск 1 places,
    # and Ново-Грудск. у. is the expanded uyezd.
    expected = {
        630: {
            "A": "Буйвол, Мар. Григ. 45 л., дети Екат. 13 л., Агапия 9 л.",
            "C": "Новогрудский уезд",
        },
        631: {
            "A": "Буйвол, Николай Семенович",
            "C": "Новогрудский уезд",
        },
    }
    failed = False
    for row, cols in expected.items():
        got = {col: new for col, (_old, new) in buyvol_rows.get(row, {}).items()}
        for col, text in cols.items():
            if got.get(col) != text:
                print(f"ПРИЁМКА НЕ СОШЛАСЬ {row}{col}: {got.get(col)!r} != {text!r}", file=sys.stderr)
                failed = True
    if failed:
        return 1

    name_amb = ambiguous_name_tokens(final_names, glossary)
    print(f"неоднозначные сокращения имён, оставлены как есть ({len(name_amb)} разных):")
    for token, count in name_amb.most_common(25):
        meanings = ", ".join(sorted(glossary[token]))
        print(f"  {token} × {count}: {meanings}")

    print("места без однозначного раскрытия (группы):")
    place_groups = [(key, count) for key, count in ambiguous.items() if not key[0].startswith("household:")]
    place_groups.sort(key=lambda item: -item[1])
    shown = 0
    for (token, why), count in place_groups:
        if count < 3 and shown > 30:
            continue
        print(f"  {token} [{why}] × {count}")
        shown += 1
        if shown >= 40:
            break
    households = sum(count for key, count in ambiguous.items() if key[0].startswith("household:"))
    print(f"строк без однозначной привязки имени: {households}")
    print(f"кусков вставки по ~40 строк: {len(chunks_of(changes))}")
    print(f"patch: {args.patch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
