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

from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, Final, cast

from korvid.agent.outbound import OutboundPolicy, provider_prepared_messages
from korvid.agent.provider import OperatorSafeProviderError
from korvid.providers.litellm_factory import CredentialStore, create_provider_from_profile
from korvid.providers.provider_default import ProviderDefaultRegistry

if TYPE_CHECKING:
    from korvid.agent.model_profiles import ModelCatalog, ModelConnectionConfig
    from korvid.providers.special_flows import SpecialFlowRegistry

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
#: provider that echoes, loops or answers with a document is read up to
#: here and no further.
PROBE_MAX_RESPONSE_CHARS: Final[int] = 4_096

_NO_PROVIDER: Final = (
    "configuration incomplete — provider could not be created; see the log for the reason"
)
_NO_TEXT: Final = "the provider answered, but returned no text"


class ProbeFailed(OperatorSafeProviderError):
    """The probe's own answer that this profile does not work.

    The wizard renders the message directly, so it is one of two written
    constants: nothing from the profile, the endpoint or the provider's
    reply is interpolated into it.
    """

    safe_messages = frozenset({_NO_PROVIDER, _NO_TEXT})


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

        Args:
            profile: The connection to test.

        Returns:
            The concatenated reply text, stripped, and never longer than
            `PROBE_MAX_RESPONSE_CHARS` — the wizard shows this string, and
            the catalog's contract calls it a short result.

        Raises:
            ProbeFailed: No provider could be built from the profile, or
                the provider streamed no text. Both are answers the wizard
                shows the operator, so neither may be swallowed.
        """
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
            # at the bound, and an abandoned generator has to release its
            # HTTP response then and there. `complete` is declared
            # `AsyncIterator` by the ABC and is an async generator — the
            # cast names what the object already is, as `native_engine`
            # does for the same reason.
            stream = cast(
                "AsyncGenerator[dict[str, Any], None]",
                provider.complete(prepared.messages, prepared.tools),
            )
            async with aclosing(stream) as events:
                async for event in events:
                    if event.get("type") != "text_delta":
                        continue
                    text += str(event.get("text", ""))
                    if len(text) >= PROBE_MAX_RESPONSE_CHARS:
                        # Reading stops as well as the string: bounding
                        # only the answer would still let the provider
                        # bill for everything it went on to send.
                        text = text[:PROBE_MAX_RESPONSE_CHARS]
                        break
        finally:
            # Closed on every path: a probe that leaks its client would
            # leak one per keystroke in the wizard.
            await provider.aclose()
        if not text.strip():
            raise ProbeFailed(_NO_TEXT)
        return text.strip()
