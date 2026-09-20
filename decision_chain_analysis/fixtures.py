"""Minimal, in-repo fixtures for the charset-normalizer decision-chain study.

Each fixture is built deterministically from Python literals (no network, no
corpora download). Hex prefixes are asserted in the test suite so the byte
shapes cannot silently drift between interpreters.

Fixture groups and the branches they exercise
----------------------------------------------
``ascii_short``        : len <= chunk_size*steps -> steps forced to 1;
                         prioritized ascii decodes, mess==0.0 -> immediate
                         single-candidate return (early-exit #1).
``polish_iso8859_2``   : several single-byte codecs decode without error and
                         with mess 0.0, but per-encoding coherence differs;
                         ordering is decided by CharsetMatch.__lt__ on
                         coherence (chaos tie).
``french_utf8_bom``    : UTF-8 SIG detected (EF BB BF), inserted at priority 0,
                         stripped before decoding; candidate returns
                         immediately after the BOM branch.
``utf16le_bom``        : UTF-16 BOM detected and *kept* (not stripped); the
                         chunk loop inspects only the first chunk because the
                         BOM-present/not-stripped condition breaks it.
``declared_iso8859_2`` : declarative hint ``<meta charset="iso-8859-2">`` is
                         found in the first 8192 bytes; the named encoding is
                         prioritized and wins at mess 0.0.
``partial_mess``       : len > chunk_size*steps -> 5 chunks; UTF-8 decodes but
                         several chunks cross the 0.2 threshold mid-way, so
                         recorded mess values are partial early-break values
                         and the candidate soft-fails; the partial values never
                         reach CharsetMatches; with cp_isolation the fallback
                         path emits a synthetic chaos==threshold entry.
``same_payload_latin`` : bytes decode to the identical str under cp1252,
                         iso8859_15 and latin_1 (only bytes whose mapping is
                         shared are used); fingerprint dedup keeps the earliest
                         (cp1252) and parks the others as submaches.
``large_utf8``         : len >= TOO_BIG_SEQUENCE (10 MB); single-byte
                         candidates use lazy decoding and only 5 chunks are
                         probed; utf_8 wins and keeps the decoded payload in the
                         returned match.
"""

from __future__ import annotations

ASCII_SHORT = b"The road goes ever on and on, down from the door where it began."

# Polish sentence repeated; encoded in ISO 8859-2 (Central European Latin).
# Multiple Latin-2/Latin-N codecs decode these bytes without error; the Polish
# letter-frequency coherence profile differs measurably between them.
_POLISH_SENTENCE = (
    "W Polsce mamy bardzo ciep\u0142y i s\u0142oneczny dzie\u0144. "
    "G\u017ceg\u017c\xf3\u0142ka \u015bpiewa rado\u015bnie, a \u017c\xf3\u0142wie "
    "wysz\u0142y z lasu na \u0142\u0105k\u0119. \u0179r\xf3d\u0142o wody p\u0142ynie "
    "rze\u015bko mi\u0119dzy drzewami. "
)
POLISH_ISO8859_2 = _POLISH_SENTENCE.encode("iso8859_2") * 8

# Short French sentence with a UTF-8 SIG prepended.
FRENCH_UTF8_BOM = b"\xef\xbb\xbf" + "Bonjour, voici un petit essai en fran\xe7ais.".encode(
    "utf-8"
)

# UTF-16 (platform-independent LE BOM explicitly) short sentence.
UTF16LE_BOM = b"\xff\xfe" + "Witaj, to jest krotki test.".encode("utf-16-le")

# ASCII-compatible declarative hint + ISO 8859-2 Polish bytes (0xb3 = ł, 0xf1 = ń).
DECLARED_ISO8859_2 = (
    b'<html><head><meta charset="iso-8859-2"></head><body>'
    b"W Polsce jest ciep\xb3y i s\xb3oneczny dzie\xf1. " * 4
    + b"</body></html>"
)

# 2620 bytes: five blocks (> 512*5 boundary at 2560 is not crossed, but note
# length 2620 > 2560 so the default 5-step/512 sampling stays active). Each
# block mixes harmless ASCII with valid UTF-8 encodings of C1 control chars
# (0xC2 0x80..0xC2 0x9F), which are unprintable: chunk mess crosses 0.2 part
# way through, so mess_ratio() returns a partial value.
_C1_RUN = b"".join(bytes((0xC2, c)) for c in range(0x80, 0xA0))
_ASCII_RUN = b"ordinary words in a probe block. "
PARTIAL_MESS = b"".join(_ASCII_RUN * 12 + _C1_RUN * 2 for _ in range(5))

# French-like text restricted to bytes that cp1252, iso8859_15 and latin_1 map
# identically (notably: no 0x80/0x82..0x9F cp1252 specials, no 0xA4/0xA6/0xA8/
# 0xB4/0xB8/0xBC..0xBE where iso8859_15 differs from latin_1, no euro sign).
_SAME_PAYLOAD_TEXT = (
    "Caf\xe9 r\xe9serv\xe9. Cr\xe8me br\xfbl\xe9e, gar\xe7on ! "
    "Co\xfbt d\xe9j\xe0 \xe0 l'\xe9cole, s'il vous pla\xeet."
)
SAME_PAYLOAD_CP1252 = _SAME_PAYLOAD_TEXT.encode("cp1252")
SAME_PAYLOAD_ISO15 = _SAME_PAYLOAD_TEXT.encode("iso8859_15")
SAME_PAYLOAD_LATIN1 = _SAME_PAYLOAD_TEXT.encode("latin_1")

# >= 10 MB: mostly ASCII with occasional valid UTF-8 accented words.
LARGE_UTF8 = (
    b"The quick brown fox jumps over the lazy dog. " * 100
    + "caf\xe9 r\xe9sum\xe9 na\xefve. ".encode("utf-8") * 5
) * 2200

FIXTURES = {
    "ascii_short": ASCII_SHORT,
    "polish_iso8859_2": POLISH_ISO8859_2,
    "french_utf8_bom": FRENCH_UTF8_BOM,
    "utf16le_bom": UTF16LE_BOM,
    "declared_iso8859_2": DECLARED_ISO8859_2,
    "partial_mess": PARTIAL_MESS,
    "same_payload_latin": SAME_PAYLOAD_CP1252,
    "large_utf8": LARGE_UTF8,
}
