"""Controlled instrumentation helpers for the charset-normalizer trace study.

Only public API is used for detection (``charset_normalizer.from_bytes``).
The instrumentation is deliberately narrow: we wrap two *internal* helper
functions that the public workflow calls (``cut_sequence_chunks`` and
``mess_ratio``) purely to *observe* inputs/outputs, never to alter them.
Wrappers delegate unmodified to the originals.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import charset_normalizer.api as api
from charset_normalizer import from_bytes
from charset_normalizer.constant import TOO_BIG_SEQUENCE, TOO_SMALL_SEQUENCE
from charset_normalizer.md import mess_ratio as _real_mess_ratio
from charset_normalizer.utils import cut_sequence_chunks as _real_cut

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def huge_payload() -> bytes:
    """Synthesized in-memory; intentionally NOT committed (11 MB)."""
    clean = (
        "Molière et Racine écrivirent des comédies et tragédies célèbres, "
        "jouées à Paris et à Versailles devant le roi toute l'année durant. "
    ).encode("cp1252")
    return (clean * (11_000_000 // len(clean) + 1))[:11_000_000]


def ratio_with_early_stop_visible(sequence: str, threshold: float) -> tuple[float, float, bool]:
    """Return (reported, complete, stopped_inside_chunk).

    ``reported`` is what mess_ratio() returns with the workflow threshold
    (it may break per 32/64/128-char block early); ``complete`` is the value
    computed with a threshold that can never be crossed; ``stopped_inside``
    is True when the two differ.
    """
    reported = _real_mess_ratio(sequence, threshold)
    complete = _real_mess_ratio(sequence, 1e9)
    # The block-level early stop in mess_ratio() only over-estimates: every
    # per-detector ratio is a prefix count divided by a smaller prefix, so
    # the truncated value is >= the complete value.
    return round(reported, 3), round(complete, 3), round(reported, 3) > round(complete, 3)


class TraceRecorder:
    """Observer for one from_bytes() run.

    Records, per tested encoding, in encounter order:
      chunks        [(byte length, reported mess, complete mess, stopped_inside)]
      hard_failed   encoding raised UnicodeDecodeError during probing
      candidates    encodings that became CharsetMatch objects, with stats
    """

    def __init__(self) -> None:
        self.chunks: dict[str, list[tuple[int, float, float, bool]]] = {}
        self.hard_failed: set[str] = set()
        self.soft_failed: dict[str, str] = {}
        self.candidates: dict[str, dict] = {}
        self.params: dict = {}
        self.log_lines: list[str] = []
        self._cut = None
        self._mr = None
        self._handler = None

    def install(self) -> None:
        recorder = self
        real_mr = _real_mess_ratio
        real_cut = _real_cut

        def observed_mr(sequence: str, threshold: float = 0.2, debug: bool = False):
            return real_mr(sequence, threshold, debug)

        def observed_cut(
            sequences,
            encoding_iana,
            offsets,
            chunk_size,
            bom_available,
            strip_sig,
            sig_payload,
            is_mb,
            payload,
            deferred,
        ):
            # Wrap lazily: the consumer loop breaks on gave-up, so only the
            # chunks it actually pulled get recorded.
            rows = recorder.chunks.setdefault(encoding_iana, [])
            for chunk in real_cut(
                sequences,
                encoding_iana,
                offsets,
                chunk_size,
                bom_available,
                strip_sig,
                sig_payload,
                is_mb,
                payload,
                deferred,
            ):
                reported, complete, stopped = ratio_with_early_stop_visible(
                    chunk, recorder.params.get("threshold", 0.2)
                )
                rows.append((len(chunk), reported, complete, stopped))
                yield chunk

        # api.from_bytes resolves both names as module globals inside the call.
        api.cut_sequence_chunks = observed_cut
        api.mess_ratio = observed_mr
        self._cut, self._mr = observed_cut, observed_mr

    def restore(self) -> None:
        api.cut_sequence_chunks = _real_cut
        api.mess_ratio = _real_mess_ratio

    def run(self, payload: bytes, **kwargs) -> "object":
        import re as _re

        self.params = dict(kwargs)
        logger = logging.getLogger("charset_normalizer")

        class _Capture(logging.Handler):
            def __init__(self, sink):
                super().__init__(level=5)
                self.sink = sink

            def emit(self, record):
                self.sink.append(self.format(record))

        self._handler = _Capture(self.log_lines)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        old_level = logger.level
        logger.addHandler(self._handler)
        logger.setLevel(5)
        self.install()
        try:
            results = from_bytes(payload, **kwargs)
        finally:
            self.restore()
            logger.removeHandler(self._handler)
            logger.setLevel(old_level)
        hard = _re.compile(r"Code page (\S+) does not fit given bytes sequence")
        soft = _re.compile(r"^(\S+) was excluded because of initial chaos probing")
        for line in self.log_lines:
            m = hard.search(line)
            if m:
                self.hard_failed.add(m.group(1))
            m = soft.match(line)
            if m:
                self.soft_failed[m.group(1)] = line
        for match in results:
            self.candidates[match.encoding] = {
                "chaos": match.chaos,
                "coherence": round(match.coherence, 4),
                "language": match.language,
                "bom": match.bom,
                "submatches": [leaf.encoding for leaf in match.submatch],
            }
            for leaf in match.submatch:
                self.candidates[leaf.encoding] = {
                    "chaos": leaf.chaos,
                    "coherence": round(leaf.coherence, 4),
                    "language": leaf.language,
                    "bom": leaf.bom,
                    "submatches": [],
                    "deduped_into": match.encoding,
                }
        return results

    def summary(self) -> dict:
        return {
            "kwargs": self.params,
            "length_bands": {
                "too_small_below": TOO_SMALL_SEQUENCE,
                "too_big_at_or_above": TOO_BIG_SEQUENCE,
            },
            "hard_failed": sorted(self.hard_failed),
            "soft_failed": sorted(self.soft_failed),
            "chunks_probed": {
                enc: [
                    {
                        "len": length,
                        "mess_reported": reported,
                        "mess_complete": complete,
                        "stopped_inside_chunk": stopped,
                    }
                    for length, reported, complete, stopped in rows
                ]
                for enc, rows in self.chunks.items()
            },
            "candidates_and_submatches": self.candidates,
        }


@contextmanager
def instrumentation():
    rec = TraceRecorder()
    yield rec


def dump_json(obj, path: str | Path) -> None:
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))
