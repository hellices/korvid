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

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from korvid.agent.outbound import OutboundPolicy, provider_prepared_messages
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
            The concatenated reply text, stripped.

        Raises:
            RuntimeError: When no provider could be built from the profile,
                or when the provider streamed no text. Both are answers the
                wizard shows the operator, so neither may be swallowed.
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
            raise RuntimeError(
                "configuration incomplete — provider could not be created;"
                " see the log for the reason"
            )
        text = ""
        try:
            prepared = self._outbound.prepare(
                provider.descriptor.model,
                provider_prepared_messages(provider, [dict(PROBE_MESSAGE)]),
                [],
                iteration=1,
            )
            async for event in provider.complete(prepared.messages, prepared.tools):
                if event.get("type") == "text_delta":
                    text += str(event.get("text", ""))
        finally:
            # Closed on every path: a probe that leaks its client would
            # leak one per keystroke in the wizard.
            await provider.aclose()
        if not text.strip():
            raise RuntimeError("provider returned no text")
        return text.strip()
