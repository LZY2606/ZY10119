"""Minimal regression tests pinning the charset-normalizer decision chain.

Companion to analysis/ANALYSIS.zh-CN.md and analysis/diagnose.py.
Only the public detection API is asserted; the TraceRecorder instrumentation
is used solely to observe which chunks were probed and whether soft failures
happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from charset_normalizer import from_bytes
from charset_normalizer.constant import TOO_BIG_SEQUENCE, TOO_SMALL_SEQUENCE
from charset_normalizer.md import mess_ratio
from charset_normalizer.utils import (
    any_specified_encoding,
    identify_sig_or_bom,
    should_strip_sig_or_bom,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "analysis"))

from trace_tools import TraceRecorder, huge_payload, load_fixture  # noqa: E402


def run(payload: bytes, **kwargs):
    recorder = TraceRecorder()
    results = recorder.run(payload, **kwargs)
    return results, recorder


# --- A. pure ASCII ---------------------------------------------------------

def test_ascii_immediate_return_probes_only_one_chunk():
    payload = load_fixture("ascii_plain.txt")
    assert len(payload) > TOO_SMALL_SEQUENCE
    results, rec = run(payload)
    match = results.best()
    assert match.encoding == "ascii"
    assert match.chaos == 0.0
    assert match.language == "English"
    assert len(results) == 1
    # steps was overridden to 1: exactly one chunk, and utf_8 was never tested.
    assert list(rec.chunks) == ["ascii"]
    assert len(rec.chunks["ascii"]) == 1


def test_empty_bytes_short_circuit():
    assert from_bytes(b"").best().encoding == "utf_8"


# --- B. two single-byte encodings, coherence differs -----------------------

def test_two_sbc_candidates_coherence_tiebreak_in_lt():
    payload = load_fixture("spanish_cp1252.bin")
    results, rec = run(payload, cp_isolation=["cp1252", "cp1257"])
    assert len(results) == 2
    cp1252, cp1257 = results[0], results[1]
    assert cp1252.encoding == "cp1252" and cp1257.encoding == "cp1257"
    assert cp1252.chaos == cp1257.chaos == 0.0
    assert abs(cp1252.coherence - cp1257.coherence) > 0.02
    # __lt__ first rule: equal chaos, higher coherence first; best() is [0].
    assert cp1252 < cp1257
    assert results.best() is cp1252
    # both candidates kept distinct: payloads differ, fingerprints differ
    assert cp1252.fingerprint != cp1257.fingerprint
    assert not rec.soft_failed


# --- C. BOM / declarations -------------------------------------------------

def test_utf8_bom_stripped_before_decode():
    payload = load_fixture("utf8_bom.bin")
    sig_encoding, sig_payload = identify_sig_or_bom(payload)
    assert sig_encoding == "utf_8" and sig_payload == b"\xef\xbb\xbf"
    assert should_strip_sig_or_bom("utf_8") is True
    match = from_bytes(payload).best()
    assert match.encoding == "utf_8" and match.bom is True
    assert not str(match).startswith("\ufeff")


def test_utf16_bom_kept_for_codec_single_candidate():
    payload = load_fixture("utf16be_bom.bin")
    sig_encoding, _ = identify_sig_or_bom(payload)
    assert sig_encoding == "utf_16"
    assert should_strip_sig_or_bom("utf_16") is False
    results, rec = run(payload)
    assert len(results) == 1
    match = results.best()
    assert match.encoding == "utf_16" and match.bom is True
    assert str(match) == "Hello BOM world"
    # chunk loop breaks after the first chunk for kept BOM
    assert len(rec.chunks["utf_16"]) == 1


def test_embedded_declaration_is_priority_not_truth():
    payload = load_fixture("decl_iso8859_5.bin")
    assert any_specified_encoding(payload) == "iso8859_5"
    assert identify_sig_or_bom(payload)[0] is None
    match = from_bytes(payload).best()
    assert match.encoding == "iso8859_5" and match.chaos == 0.0
    # language detection is independent of the declared name
    assert match.language in {"Ukrainian", "Russian"}


# --- D. early exits --------------------------------------------------------

def test_chunk_level_giveup_softfailure_excluded_from_comparison():
    payload = load_fixture("chunks_early_exit.bin")
    results, rec = run(payload, cp_isolation=["latin_1", "cp1252"])
    assert "latin_1" in rec.soft_failed
    assert "latin_1" not in rec.candidates
    # the soft-failed encoding never became a CharsetMatch, hence never sorted
    assert [m.encoding for m in results] == ["cp1252"]
    latin_chunks = rec.chunks["latin_1"]
    # loop stopped at gave_up == max(5//4, 2) == 2: only 3 chunks inspected,
    # the last two offsets (1800/2400) were never visited
    assert len(latin_chunks) == 3


def test_intra_chunk_partial_value_enters_sort_when_candidate_survives():
    payload = load_fixture("intra_chunk_partial.bin")
    # default threshold: chunk 1 of both candidates stops inside the first
    # 32-char block; means stay below 0.2 so both candidates are built and
    # the truncated values participate in __lt__.
    results, rec = run(payload, cp_isolation=["latin_1", "cp1252"])
    cp1252_rows = rec.chunks["cp1252"]
    latin_rows = rec.chunks["latin_1"]
    # tuple layout: (length, reported, complete, stopped_inside_chunk)
    assert cp1252_rows[0][3] is True
    assert latin_rows[0][3] is True
    # reported (truncated) strictly greater than the complete value
    assert latin_rows[0][1] > latin_rows[0][2]
    assert {m.encoding for m in results} == {"cp1252", "latin_1"}
    assert results.best().encoding == "cp1252"

    # raising the threshold lets cp1252 scan the full chunk: the same input
    # yields a complete (smaller) chaos for cp1252 only.
    results2, rec2 = run(payload, cp_isolation=["latin_1", "cp1252"], threshold=0.35)
    assert rec2.chunks["cp1252"][0][3] is False
    assert rec2.chunks["latin_1"][0][3] is True
    chaos1 = {m.encoding: m.chaos for m in results}
    chaos2 = {m.encoding: m.chaos for m in results2}
    assert chaos2["cp1252"] < chaos1["cp1252"]


def test_fallback_utf8_uses_threshold_constant_not_measured_mess():
    payload = load_fixture("boxdraw_utf8.bin")
    match = from_bytes(payload).best()
    assert match is not None and match.encoding == "utf_8"
    assert match.chaos == 0.2  # CharsetMatch(..., threshold, ...) placeholder
    assert match.coherence == 0.0 and match.language == "Unknown"
    # the actually measured chunk mess is far above the placeholder value
    assert mess_ratio(payload.decode("utf_8")[:512], 0.2) > 1.0


# --- E. fingerprint dedup --------------------------------------------------

def test_identical_payloads_dedup_into_first_tested_root():
    payload = load_fixture("french_shared_cp1252.bin")
    encodings = ["cp1252", "latin_1", "iso8859_15", "cp1254", "cp1258", "iso8859_9"]
    results, rec = run(payload, cp_isolation=encodings)
    assert len(results) == 1
    root = results.best()
    # IANA alphabetical order among these names puts cp1252 first
    assert root.encoding == "cp1252"
    leaves = {leaf.encoding for leaf in root.submatch}
    assert leaves == {"latin_1", "iso8859_15", "cp1254", "cp1258", "iso8859_9"}
    assert set(root.could_be_from_charset) == set(encodings)
    # root keeps its own identity and data
    assert root.language == "French"
    assert "Basic Latin" in root.alphabets
    # aliases resolve to the root for every absorbed leaf name
    assert results["latin1"] is root
    assert results["ISO-8859-1"] is root
    leaf = next(leaf for leaf in root.submatch if leaf.encoding == "latin_1")
    assert leaf._string is None  # payload unloaded to save RAM
    assert leaf.chaos == 0.0


# --- F. sampling sizes -----------------------------------------------------

def test_small_file_single_chunk_and_huge_file_five_chunks():
    medium = load_fixture("spanish_cp1252.bin")
    assert len(medium) <= 512 * 5
    _, rec = run(medium, cp_isolation=["cp1252"])
    assert len(rec.chunks["cp1252"]) == 1

    payload = huge_payload()
    assert len(payload) >= TOO_BIG_SEQUENCE
    results, rec = run(payload, cp_isolation=["cp1252"])
    assert results.best().encoding == "cp1252"
    assert len(rec.chunks["cp1252"]) == 5
    # dedup disabled for heavy payloads
    assert not results.best().submatch


# --- G. explain does not alter results -------------------------------------

def test_explain_changes_neither_branches_nor_values(caplog):
    payload = load_fixture("chunks_early_exit.bin")
    with caplog.at_level(5, logger="charset_normalizer"):
        plain = from_bytes(payload, cp_isolation=["latin_1", "cp1252"])
        explained = from_bytes(
            payload, cp_isolation=["latin_1", "cp1252"], explain=True
        )
    def signature(results):
        return [(m.encoding, m.chaos, m.coherence) for m in results]
    assert signature(plain) == signature(explained)
    assert caplog.text  # explain did attach the stream handler (TRACE logs exist)


@pytest.mark.parametrize("explain", [False, True])
def test_explain_invariance_on_all_fixtures(explain):
    for name in [
        "ascii_plain.txt",
        "spanish_cp1252.bin",
        "utf8_bom.bin",
        "utf16be_bom.bin",
        "decl_iso8859_5.bin",
        "french_shared_cp1252.bin",
    ]:
        a = from_bytes(load_fixture(name))
        b = from_bytes(load_fixture(name), explain=explain)
        assert [m.encoding for m in a] == [m.encoding for m in b]
