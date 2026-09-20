#!/usr/bin/env python3
"""Reproducible decision-chain diagnostic for charset-normalizer.

Runs a fixed set of minimal inputs through the public API (from_bytes)
with a passive TraceRecorder around internal chunk/mess helpers, and prints
or writes a JSON trace of:

  * BOM/SIG and declarative hints (public helpers)
  * which encodings became candidates, hard-failed or soft-failed
  * which chunks were probed, and whether each chunk's mess value is
    complete or an intra-chunk early-interrupt value
  * coherence merging inputs, dedup structure and final sort/best()

Usage:
    uv run python analysis/diagnose.py                 # human report
    uv run python analysis/diagnose.py --json trace.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from charset_normalizer import from_bytes
from charset_normalizer.cd import merge_coherence_ratios
from charset_normalizer.utils import (
    any_specified_encoding,
    identify_sig_or_bom,
    should_strip_sig_or_bom,
)

from trace_tools import TraceRecorder, huge_payload, load_fixture


def coherence_merge_demo(payload: bytes, encoding: str, threshold: float = 0.1):
    """Show merge_coherence_ratios inputs (chunk -> languages) for one candidate.

    Small payloads (<= chunk_size*steps) use a single whole-payload chunk,
    matching from_bytes' steps=1 override.
    """
    from charset_normalizer.cd import coherence_ratio, encoding_languages, mb_encoding_languages
    from charset_normalizer.utils import is_multi_byte_encoding

    decoded = payload.decode(encoding)
    mb = is_multi_byte_encoding(encoding)
    langs = mb_encoding_languages(encoding) if mb else encoding_languages(encoding)
    inclusion = ",".join(langs) if langs else None
    if len(payload) <= 512 * 5:
        per_chunk = [coherence_ratio(decoded, threshold, inclusion)]
    else:
        per_chunk = [
            coherence_ratio(decoded[i : i + 512], threshold, inclusion)
            for i in range(0, len(payload), len(payload) // 5)
        ]
    return per_chunk, merge_coherence_ratios(per_chunk)


def run_case(name: str, payload: bytes, **kwargs) -> dict:
    rec = TraceRecorder()
    results = rec.run(payload, **kwargs)
    sig_encoding, sig_payload = identify_sig_or_bom(payload)
    specified = any_specified_encoding(payload)
    ordered = [m.encoding for m in results]
    return {
        "case": name,
        "length": len(payload),
        "head_hex": payload[:16].hex(),
        "bom_sig": {
            "encoding": sig_encoding,
            "mark_hex": sig_payload.hex(),
            "stripped_when_strip_is_applied": (
                should_strip_sig_or_bom(sig_encoding) if sig_encoding else None
            ),
        },
        "declarative_hint": specified,
        "final_order": ordered,
        "best": results.best().encoding if results else None,
        "trace": rec.summary(),
    }


CASES = [
    ("A1 ascii pure (< TOO_SMALL? no, 65 bytes)", "ascii_plain.txt", {}),
    ("B1 two SBCs decode; coherence differs (cp1252 vs cp1257)", "spanish_cp1252.bin",
     {"cp_isolation": ["cp1252", "cp1257"]}),
    ("B2 same input unrestricted (family skip / cap may apply)", "spanish_cp1252.bin", {}),
    ("C1 utf-8 BOM", "utf8_bom.bin", {}),
    ("C2 utf-16-be BOM", "utf16be_bom.bin", {}),
    ("C3 embedded declaration iso-8859-5", "decl_iso8859_5.bin", {}),
    ("D1 chunk-level early exit, latin_1 soft-fails", "chunks_early_exit.bin",
     {"cp_isolation": ["latin_1", "cp1252"]}),
    ("D2 intra-chunk PARTIAL values survive and enter sort (threshold=0.2)",
     "intra_chunk_partial.bin", {"cp_isolation": ["latin_1", "cp1252"]}),
    ("D3 SAME bytes, threshold=0.35: partial value SURVIVES and enters sort",
     "intra_chunk_partial.bin",
     {"cp_isolation": ["latin_1", "cp1252"], "threshold": 0.35}),
    ("E1 identical decoded payload -> fingerprint dedup", "french_shared_cp1252.bin",
     {"cp_isolation": ["cp1252", "latin_1", "iso8859_15", "cp1254", "cp1258", "iso8859_9"]}),
    ("F1 utf-8 soft failure -> fallback chaos=threshold", "boxdraw_utf8.bin", {}),
]


def all_reports(include_huge: bool) -> list[dict]:
    reports = []
    for name, fixture, kwargs in CASES:
        reports.append(run_case(name, load_fixture(fixture), **kwargs))
    if include_huge:
        rec = TraceRecorder()
        payload = huge_payload()
        results = rec.run(payload, cp_isolation=["cp1252"])
        reports.append(
            {
                "case": "F2 huge payload (>= 10e6): lazy decode + 5 chunks only",
                "length": len(payload),
                "head_hex": payload[:16].hex(),
                "bom_sig": {"encoding": None, "mark_hex": "", "stripped_when_strip_is_applied": None},
                "declarative_hint": None,
                "final_order": [m.encoding for m in results],
                "best": results.best().encoding,
                "trace": rec.summary(),
            }
        )
    # coherence merge demo on the Spanish cp1252 candidate
    sp = load_fixture("spanish_cp1252.bin")
    per_chunk, merged = coherence_merge_demo(sp, "cp1252")
    reports.append(
        {
            "case": "G1 single-chunk merge (steps=1, spanish cp1252)",
            "per_chunk": per_chunk,
            "merged": merged,
        }
    )
    # multi-chunk merge on the 3000-byte D2 fixture (5 chunks, arithmetic mean)
    multi = load_fixture("intra_chunk_partial.bin")
    per_chunk_m, merged_m = coherence_merge_demo(multi, "cp1252")
    reports.append(
        {
            "case": "G2 five-chunk merge, arithmetic mean per language (spanish cp1252)",
            "per_chunk": per_chunk_m,
            "merged": merged_m,
        }
    )
    return reports


def print_report(reports: list[dict]) -> None:
    for report in reports:
        print("=" * 78)
        print(report["case"])
        print("-" * 78)
        if "trace" not in report:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            continue
        print(f"length={report['length']} head={report['head_hex']}")
        print(f"BOM/SIG={report['bom_sig']['encoding']} "
              f"declared={report['declarative_hint']}")
        trace = report["trace"]
        print(f"hard-failed: {trace['hard_failed']}")
        print(f"soft-failed: {trace['soft_failed']}")
        for enc, rows in trace["chunks_probed"].items():
            parts = []
            for row in rows:
                tag = "PARTIAL" if row["stopped_inside_chunk"] else "complete"
                parts.append(f"{row['mess_reported']}({tag})")
            print(f"  chunks[{enc}] ({len(rows)}): {', '.join(parts)}")
        for enc, info in trace["candidates_and_submatches"].items():
            where = f" -> submatch of {info['deduped_into']}" if "deduped_into" in info else ""
            print(f"  candidate {enc}: chaos={info['chaos']} coherence={info['coherence']} "
                  f"lang={info['language']} bom={info['bom']}{where}")
        print(f"final sort order: {report['final_order']}")
        print(f"best(): {report['best']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", metavar="PATH", help="write the full trace as JSON")
    parser.add_argument("--huge", action="store_true", help="include the 11 MB synthetic payload")
    args = parser.parse_args()

    reports = all_reports(include_huge=args.huge)
    if args.json:
        Path(args.json).write_text(json.dumps(reports, ensure_ascii=False, indent=2))
        print(f"wrote {args.json} ({len(reports)} cases)")
    else:
        print_report(reports)


if __name__ == "__main__":
    main()
