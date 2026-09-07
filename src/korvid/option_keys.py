"""One way to read an option key's name, shared by the gates that judge it.

Two gates judge the same key names and have to agree about them:

* `core/config.py` refuses a credential-shaped key in a profile's
  `options`, because `config.yaml` is not a secret store.
* `providers/litellm_request.py` drops one on the way to `acompletion`,
  because an argument the provider does not consume is forwarded into the
  *request body* (measured on litellm 1.98.0) — so a credential-shaped
  option does not merely override something, it sends whatever it holds
  to the vendor as an unknown field.

They used to carry a copy of the vocabulary each, and the copies drifted.
The plural spellings were in neither: `api_keys`, `secrets`, `passwords`,
`access_tokens` and `client_secrets` passed the config gate, survived
`build_plan` on the lookup-failed path, and reached the wire.
`credentials` drifted the other way — refused by one gate, accepted by the
other — which is the same bug wearing the opposite sign.

`tach.toml` gives `korvid.core` no dependency on `korvid.providers` and
`korvid.providers` none on `korvid.core`, so the shared rule cannot live in
either. It lives here: a leaf module that depends on no other korvid
package and on nothing outside the standard library, which is what lets
both layers import it legally.

Matching is by *word segment*, never by substring. `monkey` contains
"key", `token_count` counts units of text, and `max_tokens` is a
parameter every provider supports — none of them names a credential.

The credential vocabulary lives here because both gates need it. The
tokenizer is here for the same reason: `providers/litellm_request.py`
also reads key names to spot LiteLLM's own control arguments, and that
vocabulary is the vendor's, so it stays with the vendor's module.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

#: Splits ASCII camelCase and acronym boundaries, so `apiKey`, `APIKey`
#: and `api_key` tokenize to the same two segments.
_CAMEL_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"  # lowerUpper: apiKey → api_Key
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # ACRONYMWord: APIKey → API_Key
)

#: Every run of non-alphanumerics is one boundary, so `api-key`,
#: `api.key` and `api__key` are the same name.
_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Whole segments that name a credential on their own, in their canonical
#: singular spelling. The plural of each is matched too (see `_singular`),
#: which is what the two gates disagreed about.
CREDENTIAL_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "apikey",  # compact form, common in JSON-shaped config
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
    }
)

#: Names that are a credential only as an adjacent pair. `key` alone is a
#: real parameter segment — `prompt_cache_key` is supported by several
#: providers and `client_key` is nobody's secret — so it is matched as a
#: two-segment window instead.
CREDENTIAL_SEGMENT_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("api", "key"), ("access", "key")}
)

#: Words that make an adjacent `token` a *measurement* rather than a
#: credential. `token` is the one entry in the vocabulary with an
#: innocent meaning of its own — it is the LLM unit of text — and the
#: parameters that count them have to keep working: `max_tokens` and
#: `max_completion_tokens` are reported as supported for korvid's
#: providers on litellm 1.98.0, `context_window_tokens` is korvid's own
#: catalog field, and the usage counters are named the same way.
TOKEN_QUANTITY_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "budget",
        "cached",
        "completion",
        "context",
        "cost",
        "count",
        "input",
        "length",
        "limit",
        "max",
        "min",
        "num",
        "output",
        "per",
        "prompt",
        "reasoning",
        "size",
        "thinking",
        "total",
        "usage",
        "used",
        "window",
    }
)


def key_segments(key: str) -> tuple[str, ...]:
    """*key* as lowercase word segments.

    Splits camelCase and acronym boundaries before folding case, so
    `apiKey`, `APIKey`, `API_KEY` and `api-key` all become
    `("api", "key")`. Compatibility-normalizes and strips combining marks
    first, so a decomposed spelling cannot hide a segment.

    Args:
        key: The option key as it was written.

    Returns:
        The key's word segments, in order, with empty segments removed.
    """
    camel_split = _CAMEL_BOUNDARY_RE.sub("_", key)
    normalized = unicodedata.normalize("NFKD", camel_split).casefold()
    folded = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return tuple(part for part in _SEPARATOR_RE.split(folded) if part)


def _singular(segment: str) -> str:
    """*segment* with a plural `s` removed, for vocabulary lookup only.

    Deliberately crude — it exists so `api_keys`, `secrets`, `passwords`
    and `access_tokens` cannot mean something different from their
    singulars. Short segments and `ss` endings are left alone so nothing
    is truncated into a word it never was.
    """
    if len(segment) > 3 and segment.endswith("s") and not segment.endswith("ss"):
        return segment[:-1]
    return segment


def _is_token_quantity(singulars: tuple[str, ...], index: int) -> bool:
    """Whether the `token` segment at *index* counts units of text."""
    if singulars[index] != "token":
        return False
    before = singulars[index - 1 : index]
    after = singulars[index + 1 : index + 2]
    return any(neighbour in TOKEN_QUANTITY_SEGMENTS for neighbour in before + after)


def _matched_pair(singulars: tuple[str, ...]) -> str | None:
    """The paired credential name in *singulars*, joined, or None."""
    for index in range(len(singulars) - 1):
        window = (singulars[index], singulars[index + 1])
        if window in CREDENTIAL_SEGMENT_PAIRS:
            return "_".join(window)
    return None


def normalized_segments(key: str) -> tuple[str, ...]:
    """*key* as lowercase word segments, each reduced to its singular.

    The form a vocabulary is matched against, so that a plural spelling
    can never mean something the singular does not. Callers that need the
    words exactly as written want `key_segments` instead.

    Args:
        key: The option key as it was written.

    Returns:
        The key's singularized word segments, in order.
    """
    return tuple(_singular(segment) for segment in key_segments(key))


def matched_credential_segment(key: str) -> str | None:
    """The credential name *key* carries, in canonical spelling, or None.

    The canonical spelling is what an operator-facing refusal names, so it
    has to be the offending *word* — `password`, `token`, `api_key` — and
    not the key the operator wrote.

    Args:
        key: The option key as it was written.

    Returns:
        The canonical segment that matched, or `None` when the key names
        no credential.
    """
    singulars = normalized_segments(key)
    paired = _matched_pair(singulars)
    if paired is not None:
        return paired
    for index, segment in enumerate(singulars):
        if segment in CREDENTIAL_SEGMENTS and not _is_token_quantity(singulars, index):
            return segment
    return None


def names_a_credential(key: str) -> bool:
    """Whether *key* names a credential or an auth selector.

    Matched by shape rather than against a list of vendor parameter
    names: that list is unbounded, it would go stale on the next SDK
    release, and neither gate may branch on a vendor.

    Args:
        key: The option key as it was written.

    Returns:
        True when the key names a credential.
    """
    return matched_credential_segment(key) is not None
