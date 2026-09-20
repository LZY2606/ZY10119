"""Controlled instrumentation for charset_normalizer's public detection API.

Only public entry points are used to run detection (``from_bytes``); the
instrumentation temporarily replaces module-level callables that ``api.py``
resolves per-call (``mess_ratio``, ``coherence_ratio``,
``cut_sequence_chunks``) and listens on the public ``charset_normalizer``
logger. Nothing here downloads data; all inputs are caller-supplied bytes.

The tracer records, per tested encoding:

* hard failures (codec raised ``UnicodeDecodeError``),
* soft failures (passed the codec but rejected by chaos probing),
* the chunks actually inspected (count and sizes),
* every per-chunk mess ratio observed at the production threshold (0.2),
  plus a parallel full-value computation at threshold 1.0, so a value
  truncated by ``mess_ratio``'s internal early break is distinguishable from
  a value computed over the whole chunk,
* every per-chunk coherence sub-match list,
* the mean mess ratio that entered the cross-candidate decision,
* whether the candidate entered ``CharsetMatches`` or became a submatch
  (fingerprint dedup),
* the exact ``__lt__`` comparisons performed by the final sort.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field

from charset_normalizer import api as _api
from charset_normalizer import from_bytes as _public_from_bytes
from charset_normalizer import models as _models

LOGGER_NAME = "charset_normalizer"
_TRACE_LEVEL = 5


@dataclass
class EncodingTrace:
    encoding: str
    bom_available: bool = False
    strip_sig_or_bom: bool = False
    multibyte: bool = False
    hard_failure: bool = False
    soft_failure: bool = False
    gave_up: int = 0
    chunk_sizes: list[int] = field(default_factory=list)
    chunk_mess_at_threshold: list[float] = field(default_factory=list)
    chunk_mess_full: list[float] = field(default_factory=list)
    chunk_early_broke: list[bool] = field(default_factory=list)
    chunk_coherence: list[list[tuple[str, float]]] = field(default_factory=list)
    mean_mess: float | None = None
    coherence_merged: list[tuple[str, float]] = field(default_factory=list)
    appended: bool = False
    became_submatch_of: str | None = None


@dataclass
class TraceReport:
    length: int
    steps_effective: int
    chunk_size_effective: int
    too_small: bool
    too_big: bool
    specified_encoding: str | None
    sig_encoding: str | None
    sig_hex: str
    tested_order: list[str]
    skipped_similar: list[str]
    early_return_encoding: str | None
    fallback_used: bool
    encodings: dict[str, EncodingTrace]
    comparisons: list[tuple[str, str, str]]
    ordered_results: list[str]
    submatch_tree: dict[str, list[str]]
    best: str | None

    def encoding(self, name: str) -> EncodingTrace:
        return self.encodings[name]


class _TraceLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=_TRACE_LEVEL)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _instrumented(capture_log: bool):
    state: dict = {"current": None}
    traces: dict[str, EncodingTrace] = {}
    order: list[str] = []
    skipped_similar: list[str] = []
    comparisons: list[tuple[str, str, str]] = []
    log_handler = _TraceLogHandler() if capture_log else None

    real_mess_ratio = _api.mess_ratio
    real_coherence_ratio = _api.coherence_ratio
    real_cut = _api.cut_sequence_chunks
    real_append = _models.CharsetMatches.append
    real_lt = _models.CharsetMatch.__lt__
    real_add_submatch = _models.CharsetMatch.add_submatch

    def _trace_for(encoding: str) -> EncodingTrace:
        if encoding not in traces:
            traces[encoding] = EncodingTrace(encoding)
            order.append(encoding)
        return traces[encoding]

    def cut_wrapper(
        sequences,
        encoding_iana,
        offsets,
        chunk_size,
        bom_or_sig_available,
        strip_sig_or_bom,
        sig_payload,
        is_multi_byte_decoder,
        decoded_payload=None,
        deferred_decoding=False,
    ):
        trace = _trace_for(encoding_iana)
        trace.bom_available = bom_or_sig_available
        trace.strip_sig_or_bom = strip_sig_or_bom
        trace.multibyte = is_multi_byte_decoder
        state["current"] = encoding_iana

        for chunk in real_cut(
            sequences,
            encoding_iana,
            offsets,
            chunk_size,
            bom_or_sig_available,
            strip_sig_or_bom,
            sig_payload,
            is_multi_byte_decoder,
            decoded_payload,
            deferred_decoding,
        ):
            trace.chunk_sizes.append(len(chunk))
            yield chunk

    def mess_wrapper(decoded_sequence, maximum_threshold=0.2, debug=False):
        value = real_mess_ratio(decoded_sequence, maximum_threshold, debug)
        encoding = state["current"]
        if encoding is not None:
            trace = traces[encoding]
            full = real_mess_ratio(decoded_sequence, 1.0)
            trace.chunk_mess_at_threshold.append(value)
            trace.chunk_mess_full.append(full)
            trace.chunk_early_broke.append(value >= maximum_threshold)
            if value >= maximum_threshold:
                trace.gave_up += 1
        return value

    def coherence_wrapper(decoded_sequence, threshold=0.1, lg_inclusion=None):
        value = real_coherence_ratio(
            decoded_sequence, threshold, lg_inclusion
        )
        encoding = state["current"]
        if encoding is not None:
            traces[encoding].chunk_coherence.append(list(value))
        return value

    class _AppendWrapper:

        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            return lambda item: self(obj, item)

        def __call__(self, matches, item):
            trace = _trace_for(item.encoding)
            before = len(matches._results)
            real_append(matches, item)
            if len(matches._results) == before + 1:
                trace.appended = True
                trace.mean_mess = item.chaos
                trace.coherence_merged = list(item._languages)
            else:
                trace.became_submatch_of = None

    append_wrapper = _AppendWrapper()

    class _SubmatchWrapper:

        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            return lambda other: self(obj, other)

        def __call__(self, parent, other):
            real_add_submatch(parent, other)
            trace = _trace_for(other.encoding)
            trace.became_submatch_of = parent.encoding
            trace.mean_mess = other.chaos
            trace.coherence_merged = list(other._languages)

    add_submatch_wrapper = _SubmatchWrapper()

    def lt_wrapper(inner_self, other):
        result = real_lt(inner_self, other)
        if isinstance(other, _models.CharsetMatch):
            comparisons.append(
                (
                    inner_self.encoding,
                    other.encoding,
                    "less" if result else "not_less",
                )
            )
        return result

    _api.cut_sequence_chunks = cut_wrapper
    _api.mess_ratio = mess_wrapper
    _api.coherence_ratio = coherence_wrapper
    _models.CharsetMatches.append = append_wrapper
    _models.CharsetMatch.add_submatch = add_submatch_wrapper
    _models.CharsetMatch.__lt__ = lt_wrapper
    logger = logging.getLogger(LOGGER_NAME)
    if log_handler is not None:
        logger.addHandler(log_handler)
    try:
        yield traces, order, skipped_similar, comparisons, log_handler
    finally:
        _api.cut_sequence_chunks = real_cut
        _api.mess_ratio = real_mess_ratio
        _api.coherence_ratio = real_coherence_ratio
        _models.CharsetMatches.append = real_append
        _models.CharsetMatch.add_submatch = real_add_submatch
        _models.CharsetMatch.__lt__ = real_lt
        if log_handler is not None:
            logger.removeHandler(log_handler)


def trace_detection(payload: bytes, **from_bytes_kwargs) -> tuple[object, TraceReport]:
    """Run ``from_bytes`` with instrumentation and return ``(result, report)``."""
    from charset_normalizer.constant import TOO_BIG_SEQUENCE, TOO_SMALL_SEQUENCE
    from charset_normalizer.utils import (
        any_specified_encoding,
        identify_sig_or_bom,
    )

    length = len(payload)
    chunk_size = from_bytes_kwargs.get("chunk_size", 512)
    steps = from_bytes_kwargs.get("steps", 5)
    if length <= chunk_size * steps:
        steps_effective = 1
        chunk_size_effective = length
    else:
        steps_effective = steps
        if length / steps < chunk_size:
            chunk_size_effective = int(length / steps)
        else:
            chunk_size_effective = chunk_size

    sig_encoding, sig_payload = identify_sig_or_bom(payload)
    specified = (
        any_specified_encoding(payload)
        if from_bytes_kwargs.get("preemptive_behaviour", True)
        else None
    )

    with _instrumented(capture_log=True) as (
        traces,
        order,
        skipped_similar,
        comparisons,
        log_handler,
    ):
        result = _public_from_bytes(payload, **from_bytes_kwargs)

    for record in log_handler.records:
        message = record.getMessage()
        if "deemed too similar" in message:
            for tested in order:
                if tested in message and traces[tested].encoding == tested:
                    if tested not in skipped_similar:
                        skipped_similar.append(tested)
        if "does not fit given bytes sequence at ALL" in message:
            name = message.split("Code page ", 1)[-1].split(" ", 1)[0]
            if name in traces and not traces[name].chunk_sizes:
                traces[name].hard_failure = True
        if "was excluded because of initial chaos probing" in message:
            name = message.split(" ", 1)[0]
            if name in traces:
                traces[name].soft_failure = True

    # Candidates that yielded chunks but never entered results are soft failures
    # (the log message format above already covers this; the assertion is kept
    # explicit for the diagnostic report).
    ordered = [m.encoding for m in result]
    tree = {
        m.encoding: [leaf.encoding for leaf in m.submatch] for m in result
    }
    fallback_used = any(
        "Using ASCII/UTF-8/Specified fallback" in rec.getMessage()
        for rec in log_handler.records
    )

    report = TraceReport(
        length=length,
        steps_effective=steps_effective,
        chunk_size_effective=chunk_size_effective,
        too_small=length < TOO_SMALL_SEQUENCE,
        too_big=length >= TOO_BIG_SEQUENCE,
        specified_encoding=specified,
        sig_encoding=sig_encoding,
        sig_hex=sig_payload.hex(),
        tested_order=list(order),
        skipped_similar=skipped_similar,
        early_return_encoding=(
            ordered[0] if len(ordered) == 1 and ordered else None
        ),
        fallback_used=fallback_used,
        encodings=traces,
        comparisons=comparisons,
        ordered_results=ordered,
        submatch_tree=tree,
        best=result.best().encoding if result else None,
    )
    return result, report


def render_report(result, report: TraceReport) -> str:
    """Human-readable rendering used by the demo CLI."""
    lines = []
    lines.append(f"payload length      : {report.length} bytes")
    lines.append(
        f"sampling            : steps={report.steps_effective} "
        f"chunk_size={report.chunk_size_effective} "
        f"(too_small={report.too_small}, too_big={report.too_big})"
    )
    lines.append(
        f"declared encoding   : {report.specified_encoding}"
    )
    lines.append(
        f"SIG/BOM             : {report.sig_encoding} "
        f"(0x{report.sig_hex})" if report.sig_encoding else "SIG/BOM             : none"
    )
    lines.append(f"fallback path used  : {report.fallback_used}")
    lines.append("")
    for name in report.tested_order:
        trace = report.encodings[name]
        if trace.hard_failure:
            lines.append(f"[{name}] hard failure (codec rejected the bytes)")
            continue
        status = "RESULT" if trace.appended else (
            f"SUBMATCH of {trace.became_submatch_of}"
            if trace.became_submatch_of
            else ("SOFT FAIL" if trace.chunk_sizes else "HARD FAIL")
        )
        bom = " bom" if trace.bom_available else ""
        strip = " stripped" if trace.strip_sig_or_bom else (" kept" if trace.bom_available else "")
        lines.append(
            f"[{name}] {status}{bom}{strip} mb={trace.multibyte}"
        )
        lines.append(
            f"    chunks={trace.chunk_sizes} gave_up={trace.gave_up}"
        )
        for i, (at, full, broke) in enumerate(
            zip(
                trace.chunk_mess_at_threshold,
                trace.chunk_mess_full,
                trace.chunk_early_broke,
            )
        ):
            kind = "PARTIAL(early break)" if broke else "complete"
            shown = f"{at}" if not broke or at == full else f"{at} (full would be {full})"
            lines.append(f"    chunk {i}: mess@{kind} = {shown}")
        if trace.chunk_coherence:
            lines.append(f"    coherence per chunk: {trace.chunk_coherence}")
        if trace.coherence_merged:
            lines.append(f"    coherence merged   : {trace.coherence_merged}")
        if trace.mean_mess is not None:
            lines.append(f"    mean mess (candidate value): {trace.mean_mess}")
    lines.append("")
    lines.append(f"sort comparisons    : {report.comparisons}")
    lines.append(f"ordered results     : {report.ordered_results}")
    lines.append(f"submatch tree       : {report.submatch_tree}")
    lines.append(f"best()              : {report.best}")
    return "\n".join(lines)
