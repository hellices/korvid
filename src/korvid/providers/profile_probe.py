"""Probe one model connection profile: the wizard's "does this work?".

The probe is a real request over the real transport. Anything less —
reaching the endpoint, listing models, checking that a key is set —
answers a different question than the one the operator asked, and the
wizard would report a connection that cannot actually complete.

It builds its provider with `create_provider_from_profile`, the same
factory the running agent uses, so a probe can never pass against a
transport the product does not ship or a credential path the runtime
does not take. The message goes out through `OutboundPolicy`, which is
what keeps the probe inside the same size budget as a real turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, Final, cast

from korvid.agent.outbound import OutboundPolicy, provider_prepared_messages
from korvid.agent.provider import OperatorSafeProviderError, append_bounded
from korvid.providers.litellm_factory import CredentialStore, create_provider_from_profile
from korvid.providers.provider_default import ProviderDefaultRegistry

if TYPE_CHECKING:
    from korvid.agent.model_profiles import ModelCatalog, ModelConnectionConfig
    from korvid.providers.special_flows import SpecialFlowRegistry

logger = logging.getLogger(__name__)

#: Short enough that a probe costs almost nothing, explicit enough that a
#: blank answer is a real failure rather than a model being terse.
PROBE_MESSAGE: Final[Mapping[str, str]] = {
    "role": "user",
    "content": "Reply with the single word: ok",
}

#: A probe sends one short message. The budget exists so a provider that
#: echoes or a policy that grows cannot turn a connectivity check into an
#: expensive request.
PROBE_MAX_REQUEST_CHARS: Final[int] = 4_096

#: And the answer is bounded the same way (issue #336). The catalog's
#: contract is "a short human-readable result", the question asked was for
#: one word, and this reply is held in memory outside any turn — so a
#: provider that answers past this is refused rather than quietly cut
#: short: truncating would end the read before the adapter could say
#: whether the stream ever finished.
PROBE_MAX_RESPONSE_CHARS: Final[int] = 4_096

_NO_PROVIDER: Final = (
    "configuration incomplete — provider could not be created; see the log for the reason"
)
_NO_TEXT: Final = "the provider answered, but returned no text"

#: What the probe answers with when a failure's own text is not one this
#: repository wrote. Building the provider resolves a credential, the
#: policy inspects the payload, and a transport failure carries the
#: response body — a 401's body quotes the key it refused, and its first
#: characters are exactly the identifying part. The wizard renders the
#: exception, so none of that text may be the exception.
PROBE_REFUSED: Final = (
    "the connection test failed — the provider's own message is withheld because it can "
    "quote a credential; see the log for the reason"
)


class ProbeFailed(OperatorSafeProviderError):
    """The probe's own answer that this profile does not work.

    The wizard renders the message directly, so it is one of three written
    constants: nothing from the profile, the endpoint or the provider's
    reply is interpolated into it.
    """

    safe_messages = frozenset({_NO_PROVIDER, _NO_TEXT, PROBE_REFUSED})


class ProfileProbe:
    """Completes one short exchange against a profile and returns the text.

    Constructor arguments mirror `create_provider_from_profile`'s, because
    the probe's whole value is that it builds the provider the same way
    the agent does. They are injected at the composition root.
    """

    def __init__(
        self,
        *,
        catalog: ModelCatalog | None = None,
        flows: SpecialFlowRegistry | None = None,
        credentials: CredentialStore | None = None,
        provider_defaults: ProviderDefaultRegistry | None = None,
        ca_bundle: str | None = None,
    ) -> None:
        self._catalog = catalog
        self._flows = flows
        self._credentials = credentials
        # The wizard's "test connection" has to resolve the same
        # credential the live agent will: a probe that passes without the
        # declared chain, or fails without it, is a probe that answers a
        # different question from the one the operator asked.
        self._provider_defaults = provider_defaults
        # network.ca_bundle (issue #168): the probe provider must be built
        # with the same trust as the live agent — the wizard's test and the
        # runtime can never disagree about the CA.
        self._ca_bundle = ca_bundle
        self._outbound = OutboundPolicy(max_request_chars=PROBE_MAX_REQUEST_CHARS)

    async def __call__(self, profile: ModelConnectionConfig) -> str:
        """Probe *profile* and return the model's reply.

        Every failure leaves here as text this repository wrote. The
        wizard renders the exception it catches, and a probe touches the
        three places a provider failure is most likely to quote a secret:
        the factory resolves a credential, the outbound policy inspects
        the payload, and the transport carries the response body. So a
        failure is either a refusal an adapter *declared* operator-safe —
        "the provider refused the credential", which is the only part an
        operator can act on — or it is `ProbeFailed(PROBE_REFUSED)`, with
        the real reason written to the log.

        Args:
            profile: The connection to test.

        Returns:
            The concatenated reply text, stripped. It is never longer than
            `PROBE_MAX_RESPONSE_CHARS` — the wizard shows this string, and
            the catalog's contract calls it a short result.

        Raises:
            ProbeFailed: No provider could be built from the profile, the
                provider streamed no text, or the probe failed for a
                reason whose own text it cannot vouch for. All three are
                answers the wizard shows the operator.
            OperatorSafeProviderError: Whatever the adapter refused the
                stream with, when that refusal's message is one the
                adapter declared safe — a missing terminal marker, a
                refusing status, an answer past
                `PROBE_MAX_RESPONSE_CHARS`. The probe reads to the end of
                the stream precisely so those reach the wizard.
            asyncio.CancelledError: The probe was cancelled. Never
                converted: a cancelled task must not look like a profile
                that does not work.
        """
        try:
            return await self._probe(profile)
        except asyncio.CancelledError:
            raise
        except OperatorSafeProviderError as exc:
            # Per-message, not per-class: the type only vouches for the
            # texts its class declared, so `ProviderProtocolError(str(sdk_exc))`
            # — which a third-party adapter is free to raise — is withheld
            # exactly like an untyped failure.
            if exc.operator_message() is None:
                raise self._withheld(exc) from exc
            raise
        except Exception as exc:
            raise self._withheld(exc) from exc

    @staticmethod
    def _withheld(exc: BaseException) -> ProbeFailed:
        """Log the real reason and return the sentence the wizard may show."""
        logger.warning("model connection probe failed", exc_info=exc)
        return ProbeFailed(PROBE_REFUSED)

    async def _probe(self, profile: ModelConnectionConfig) -> str:
        """Complete the exchange, unguarded — `__call__` owns the refusals."""
        provider = create_provider_from_profile(
            profile,
            catalog=self._catalog,
            flows=self._flows,
            credentials=self._credentials,
            provider_defaults=self._provider_defaults,
            ca_bundle=self._ca_bundle,
        )
        if provider is None:
            raise ProbeFailed(_NO_PROVIDER)
        text = ""
        try:
            prepared = self._outbound.prepare(
                provider.descriptor.model,
                provider_prepared_messages(provider, [dict(PROBE_MESSAGE)]),
                [],
                iteration=1,
            )
            # `aclosing`, not a bare `async for`: the loop below may stop
            # at the bound's refusal, and an abandoned generator has to
            # release its HTTP response then and there. `complete` is
            # declared `AsyncIterator` by the ABC and is an async
            # generator — the cast names what the object already is, as
            # `native_engine` does for the same reason.
            stream = cast(
                "AsyncGenerator[dict[str, Any], None]",
                provider.complete(prepared.messages, prepared.tools),
            )
            async with aclosing(stream) as events:
                async for event in events:
                    if event.get("type") != "text_delta":
                        continue
                    # The shared cumulative bound: it refuses *before* the
                    # accumulator grows, so nothing past the cap is kept
                    # and nothing after the refusal is read.
                    text = append_bounded(
                        text, str(event.get("text", "")), limit=PROBE_MAX_RESPONSE_CHARS
                    )
        finally:
            # Closed on every path: a probe that leaks its client would
            # leak one per keystroke in the wizard.
            await provider.aclose()
        if not text.strip():
            raise ProbeFailed(_NO_TEXT)
        return text.strip()
