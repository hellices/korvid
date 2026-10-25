# Capability-Aware Diagnostics Foundation Design

**Issue:** [#409](https://github.com/hellices/korvid/issues/409) (design: [#484](https://github.com/hellices/korvid/issues/484))  
**Release:** v0.7.0  
**Status:** Proposed — awaiting maintainer review

## Purpose

Nine planned diagnostics (#410–#418) read evidence that a given cluster may
not serve, may serve only in another API version or group, may refuse to the
current principal, or may return only in part. Without a shared contract each
diagnostic decides for itself what a 403, a 404, a truncated list, an expired
event or a missing metrics provider means. The first diagnostics to land
would then set inconsistent precedents.

This design defines that contract:

- a pure vocabulary for the state of each evidence source;
- one classifier for read outcomes;
- order-independent rules for combining source states, so that absence
  counts as proof only when it really is;
- API-version resolution from discovery instead of server-version strings;
- bounded LIST and GET reads that classify every outcome;
- run tokens that discard results from an old context, credential, run or
  selection.

It is the foundation #410 and #411 build on in v0.7.0, and the later
diagnostics extend it.

The design adds no diagnostic, screen, write probe, cluster-wide scan or
permission.

## Decisions to confirm in review

This design was written without a clarifying conversation, so these choices
are stated rather than assumed agreed:

1. **Evidence first, not probes first.** Reads happen when a diagnostic runs,
   at the scope the user selected, and their outcomes *are* the capability
   facts (approach 1 below).
2. **No permission review on the read path in v0.7.0.** The read itself is
   authoritative: a 403 from the real read is what makes a source `DENIED`.
   `KubeClient.can_i` stays as it is, a fail-open precheck for writes, and
   this design does not reuse it.
3. **No cache of read results in v0.7.0.** Every diagnostic run reads, within
   bounds. Only discovery is cached, as a snapshot with a generation and a
   TTL. Caching read results would need the items cached with them and
   scoped to the object's identity, which no v0.7.0 consumer needs.
4. **Identity is a session generation, not a user name.** `KubeClient` keeps
   an opaque authentication-session generation, bumped on connect, context
   switch, kubeconfig reload and credential replacement. A token refresh that
   changes the subject without any of those events is not detected, and the
   support document says so.
5. **Pulse, the relationship graph and the outcome tracker keep their own
   state enums for now.** This design defines how their states map to the
   new vocabulary, and a test pins that mapping. Converging on one enum is a
   follow-up.
6. **This design does not set a minimum Kubernetes version.** It defines how
   versions are verified and documented. The maintainer chooses the oldest
   supported version when the version matrix (S5) lands, from what the matrix
   verifies.
7. **Only what #410 and #411 need is built in v0.7.0.** The vocabulary admits
   node- and provider-scoped sources, but no collector is written for them
   until a consumer needs one.

## Existing foundations

- **Discovery.** `KubeClient.discover_resources()` in `src/korvid/k8s/client.py`:
  - enumerates every advertised group version, preferred version first
    (#345);
  - bounds concurrency and per-request time;
  - keeps only resources that support `list`;
  - skips a group version whose discovery fails, but records that failure
    only in a log.

  `ResourceMeta` in `src/korvid/k8s/discovery.py` is the shared resource
  identity.
- **Pulse.** `src/korvid/core/pulse.py` and `src/korvid/core/pulse_collector.py`
  already collect bounded snapshots (two pages, a five-second deadline) and:
  - map 401/403 to `forbidden`, 404/405 to `unavailable`, other failures to
    `failed`, and limits to `capped`;
  - keep data read before a failure as `partial`;
  - mark retained data `stale` after 30 seconds.
- **Relationship graph.** `CoverageRecord` in `src/korvid/core/relationships.py`
  and `ImpactSummary.incomplete` in `src/korvid/core/impact.py` already treat
  non-complete coverage as "absence is not proof".
- **Late results.** The context-switch coordinator's epoch
  (`src/korvid/ui/context_switch_coordinator.py`) and
  `ContextGuard.crossed(epoch)` (`src/korvid/ui/workspace_controller.py`) let
  workers discard results from an old context. Write flows also re-check the
  selection.
- **Cluster identity.** `ClusterIdentity(context, server)` in
  `src/korvid/core/namespace_slot_store.py` already keys persistent data by
  more than the context name.
- **Availability.** `UnavailableReason` and `ActionAvailability` in
  `src/korvid/ui/action_availability.py` give the palette, bindings and help
  one availability answer.
- **Errors.** `src/korvid/k8s/errors.py` has `ApiStatusError(status, body, …)`
  and `KubeClientError`. Nothing classifies them centrally.

The gaps this design closes:

- Three models describe one idea: Pulse coverage, graph coverage, and the
  outcome tracker's `partial_evidence`. None of them separates "this cluster
  does not serve it" from "korvid could not ask".
- A forbidden or failed discovery group looks the same as an absent one,
  because the failure is only logged.
- Discovery keeps one version per resource and no verbs, so a diagnostic
  cannot choose a version it knows how to parse, or tell "not served" from
  "not listable".
- There is no bounded GET and no rule that keeps configuration payloads out
  of diagnostics.
- No document states which Kubernetes versions korvid supports, and the only
  live verification is one AKS cluster.

## Considered approaches

### 1. Evidence-first observations with a shared vocabulary (recommended)

Each diagnostic declares the sources it reads. The reads happen when the
diagnostic runs, at the scope the user selected, and each one returns an
`Observation` whose state and basis come from one classifier.
Order-independent combining rules decide what the diagnostic may conclude.
Capability facts are derived from observations and discovery, never asserted
from a version number.

- **For:** it matches #409's rules that real reads are authoritative, that
  capabilities are detected at their real scope, and that there are no hidden
  scans. It mirrors Pulse's proven limits and status mapping, and the core is
  small, pure and testable.
- **Against:** each run pays for its reads (bounded), and consumers must
  handle per-source states, which is the point of the design.

### 2. An upfront capability probe service

Probe at connect time (discovery, access reviews, server version, feature
checks) and expose a cluster-wide capability matrix of booleans.

- **Rejected.** A single yes/no hides differences between namespaces, nodes
  and principals. A probe that is not the real read goes stale or disagrees
  with it, and it invites cluster-wide scans. With today's fail-open `can_i`,
  an error would read as "allowed".

### 3. Generalize Pulse's coverage model in place

Make `PulseCoverage` the shared type and route diagnostics through Pulse.

- **Rejected as the home, adopted as the reference.** It would couple
  diagnostics to Pulse's lifecycle: loading, and snapshots tied to an epoch
  and one namespace scope. It would also couple them to Pulse's naming, where
  `unavailable` means "not served" while #409 uses the same word for errors.
  This design reuses Pulse's mapping and limits in a neutral module instead.

## Architecture

| Unit | Module | May import | Purpose |
| --- | --- | --- | --- |
| Vocabulary | `korvid.core.capability` (new) | stdlib, `korvid.k8s.errors` | states, bases, sources, observations, combining rules, the classifier, run tokens |
| Version resolution | `korvid.core.api_versions` (new) | `korvid.core.capability`, `korvid.k8s.discovery` | turns the client's raw discovery outcomes into observations; logical-source resolution |
| Evidence reads | `korvid.core.evidence_reads` (new) | the two above, `korvid.k8s` | bounded, cancellable LIST and GET that return observations |
| Client additions | `korvid.k8s.client`, `korvid.k8s.discovery`, `korvid.k8s.errors` | — | the raw discovery snapshot (per-endpoint outcomes and verbs), byte-bounded responses, metadata projections, error categories, the session generation |

`tach.toml` already allows `ui`, `tools` and `agent` to import `core`, so
consumers there read the types without new layer rules. Nothing in `k8s`
imports `core`: the client's discovery snapshot holds plain outcomes, and
`core` turns them into observations. None of the new modules imports
Textual or an optional extra.

### Evidence vocabulary

`EvidenceState` says what korvid knows about one source:

| State | Meaning |
| --- | --- |
| `UNKNOWN` | not observed yet, or a read is in flight |
| `AVAILABLE` | the source answered completely at its scope |
| `PARTIAL` | some evidence, known to be incomplete |
| `UNSUPPORTED` | evidence establishes that the source or operation is not served, or not configured, here |
| `DENIED` | the server or provider refused this principal |
| `UNAVAILABLE` | could not be determined: transport, timeout, throttling, a server error, an unexpected, oversized or malformed answer, or discovery that contradicts the read |

`Basis` says why a state holds, and each state allows only some bases:

| State | Allowed bases |
| --- | --- |
| `UNKNOWN` | `NOT_STARTED`, `IN_FLIGHT` |
| `AVAILABLE` | `OBSERVED`, `OBJECT_ABSENT`, `NO_DATA` |
| `PARTIAL` | `TRUNCATED`, `DEADLINE`, `TOO_LARGE`, `CONTINUATION_EXPIRED`, `SUBSCOPE_FAILED`, `STALE_STATUS`, plus, when the failure came after at least one accepted page, any other basis the classifier gives a failure: a `DENIED` or `UNAVAILABLE` basis, or `PROJECTION_UNSUPPORTED` |
| `UNSUPPORTED` | `NOT_SERVED`, `OPERATION_UNSUPPORTED`, `NO_KNOWN_VERSION`, `PROJECTION_UNSUPPORTED`, `PROVIDER_NOT_CONFIGURED`, `FEATURE_DISABLED` |
| `DENIED` | `FORBIDDEN`, `UNAUTHENTICATED`, `DISCOVERY_FORBIDDEN` |
| `UNAVAILABLE` | `THROTTLED`, `SERVER_ERROR`, `UNEXPECTED_STATUS`, `TRANSPORT`, `MALFORMED`, `DEADLINE`, `TOO_LARGE`, `DISCOVERY_FAILED`, `DISCOVERY_STALE` |

**Conclusiveness.** A diagnostic may treat absence as a fact, for example "no
quota constrains this workload", only when `conclusive(observation, now)` is
true. That requires all of these:

- the state is `AVAILABLE` and the basis is `OBSERVED` or `OBJECT_ABSENT`.
  `NO_DATA` is never conclusive: a provider that returns no series has not
  shown that a value is zero;
- the observation is fresh: younger than the source's maximum age, which
  defaults to Pulse's 30 seconds;
- the source is not retention-limited. A source declared retention-limited,
  such as Events, can confirm a recorded fact but never the absence of one,
  because old entries expire.

Staleness is not a state. A stale `AVAILABLE` observation is shown as stale
and is not conclusive.

**Domain validity.** Collection time is not the same as semantic freshness.
A consumer that can see in the payload that the data lags may downgrade an
`AVAILABLE` observation to `PARTIAL`/`STALE_STATUS`. For example, #411 does
this when a `ResourceQuota`'s `status.hard` disagrees with its `spec.hard`.

Rules that keep a failure from being reported as "unsupported":

- `NOT_SERVED`, `NO_KNOWN_VERSION` and `OPERATION_UNSUPPORTED` come only
  from resolution, and only from complete discovery, as defined under API
  version resolution. Otherwise the source is `DENIED` or `UNAVAILABLE` with
  a discovery basis.
- `NOT_SERVED` does not name a cause (a feature gate, a version, a missing
  CRD). It records only that the server did not serve the resource.
  `FEATURE_DISABLED` is used only when an observed field or condition says so
  explicitly.
- A read that contradicts discovery is `UNAVAILABLE`/`DISCOVERY_STALE`, and
  it schedules a discovery refresh. Readers accept only a resolved source,
  and resolution only returns one whose resource and verb discovery listed.
  So a 404 that does not name the requested object, or any 405, is such a
  contradiction. The source becomes `UNSUPPORTED` only when the refreshed
  discovery no longer lists the resource or the verb. If it still does, the
  source stays `UNAVAILABLE`.

The source types:

- **`SourceScope`**: `cluster`, `namespace(name)`, `node(name)` or
  `provider(id)`.
- **`ApiSource`**: group, resource, the selected version (`None` while
  unresolved), optional subresource, scope, projection (`object` or
  `metadata`), and for a GET the target object's name and the expected UID,
  if the caller holds one.
- **`DiscoverySource`**: one discovery endpoint, which is the core root
  `/api`, the group root `/apis`, or one group version (`/api/v1` or
  `/apis/<group>/<version>`). It lets every discovery outcome be an
  `Observation` like any other.
- **`ProviderSource`**: provider id, query kind and scope. It is vocabulary
  only in v0.7.0.

An **`Observation`** carries:

- the source, state and basis;
- `observed_at`: UTC, and `None` while `UNKNOWN`;
- `items`: how many objects were read;
- `observed_uid`: the UID a GET actually returned;
- `detail`: at most 200 characters, built from fixed templates and resource
  identities, never from response bodies or field values;
- `token`: the run token the read started under (see below).

When `observed_uid` differs from the source's expected UID, the object was
recreated. The consumer reports it as a different incarnation and never
carries the old one's evidence over (#410).

### Combining observations

An **`EvidenceSet`** is an immutable collection of observations, keyed by
source. A diagnostic declares each source it reads as `required` or
`optional`, and with a role:

- **`rules_out`**: the diagnostic may conclude something from the source's
  absence, for example "no quota constrains this workload".
- **`confirms`**: only what the source contains matters, for example a
  recorded rejection event. Retention-limited sources are always `confirms`.

`baseline(declarations, now)` takes those declarations, is
order-independent, and returns a state together with every observation that
keeps the baseline from being complete:

1. **`AVAILABLE`** when every required `rules_out` source is conclusive and
   every required `confirms` source is `AVAILABLE` and fresh.
2. **`PARTIAL`** when at least one required source contributed evidence
   (`AVAILABLE` or `PARTIAL`) but the first case does not hold.
3. **Otherwise**, by fixed precedence: `DENIED` over `UNAVAILABLE` over
   `UNKNOWN` over `UNSUPPORTED`. A failure is never summarized as a missing
   capability, and the result is the same in any declaration order.

The UI lists every blocking observation, not only the one the precedence
picked.

Optional sources never lower the baseline. Their states are shown next to the
detail they feed, so the basic diagnostic stays usable when optional APIs are
missing.

`by_scope()` counts states across node- or namespace-scoped observations, for
example "available on 3 of 5 nodes". It never collapses a mixed cluster into
one answer.

### Outcome classification

`classify(failure, request)` maps a raised exception and a `ReadRequest` to
a state, a basis and a detail template. The request carries what the
exception cannot:

- the read kind: `LIST`, `GET`, `DISCOVERY` or `PROVIDER`;
- the source: an `ApiSource` names the group, resource, projection and, for
  a GET, the object, and a `DiscoverySource` names the endpoint;
- whether the request was a continuation.

Where the status code alone is ambiguous, the classifier reads a bounded part
of the Kubernetes `Status` body: `reason`, and `details.name`,
`details.group` and `details.kind`. Readers keep at most 16 KiB of an error
body for this.

| Outcome | `LIST` | `GET` | `DISCOVERY` |
| --- | --- | --- | --- |
| 401 | `DENIED`/`UNAUTHENTICATED` | same | same |
| 403 | `DENIED`/`FORBIDDEN` | same | `DENIED`/`DISCOVERY_FORBIDDEN` |
| 404 whose `NotFound` details name the requested object (name, group and resource) | — | `AVAILABLE`/`OBJECT_ABSENT` | — |
| any other 404 | `UNAVAILABLE`/`DISCOVERY_STALE` (refresh discovery) | same | `UNAVAILABLE`/`DISCOVERY_FAILED` |
| 405 | `UNAVAILABLE`/`DISCOVERY_STALE` (refresh discovery: resolution had found the verb listed) | same | `UNAVAILABLE`/`DISCOVERY_FAILED` |
| 406 for a metadata projection | `UNSUPPORTED`/`PROJECTION_UNSUPPORTED` | same | — |
| 410 `Expired` on a continuation | `PARTIAL`/`CONTINUATION_EXPIRED` (a continuation always follows an accepted page) | — | — |
| 429 | `UNAVAILABLE`/`THROTTLED`, after at most one retry that fits the deadline | same | same |
| 5xx | `UNAVAILABLE`/`SERVER_ERROR` | same | `UNAVAILABLE`/`DISCOVERY_FAILED` |
| any other status, including a 406 or 410 outside the cases above | `UNAVAILABLE`/`UNEXPECTED_STATUS` | same | `UNAVAILABLE`/`DISCOVERY_FAILED` |
| response over its byte or object budget | `UNAVAILABLE`/`TOO_LARGE` | same | — |
| timeout, TLS or connection failure | `UNAVAILABLE`/`TRANSPORT` | same | `UNAVAILABLE`/`DISCOVERY_FAILED` |
| malformed body, cyclic continuation | `UNAVAILABLE`/`MALFORMED` | same | `UNAVAILABLE`/`DISCOVERY_FAILED` |
| `asyncio.CancelledError` | re-raised, never classified | same | same |

`KubeClientError` gains a category (`timeout`, `transport`, `malformed` or
`too_large`), set where the error is raised, so that the classifier never
parses messages.

The table gives the outcome of a failure before any page was accepted. A
failure after at least one accepted page keeps what was read and yields
`PARTIAL` with the same basis, even when those pages held no items.

Pulse keeps its own collector for now. A table test pins how each
`PulseCoverageState` maps to the new vocabulary. The mapping is lossy, so
where a Pulse state is ambiguous it maps to the weaker claim:

| Pulse state | New state |
| --- | --- |
| `loading` | `UNKNOWN` |
| `complete` | `AVAILABLE` |
| `partial`, `capped` | `PARTIAL` when the result kept objects; otherwise `UNAVAILABLE`, because Pulse also reports a byte cap hit on the first page as `capped` |
| `forbidden` | `DENIED` |
| `unavailable` | `UNAVAILABLE`: Pulse folds a 405 together with a 404 that may only mean stale discovery, so it never becomes `UNSUPPORTED` |
| `failed` | `UNAVAILABLE` |
| `stale` | `UNAVAILABLE`: the state it replaced is not kept |

Telling `UNSUPPORTED` apart takes the structured outcome that the
convergence follow-up brings. If either side changes, the test fails until
they are reconciled.

### API version resolution

Discovery is split along the layer boundary, because `k8s` may not import
`core`.

**In `k8s`**, `KubeClient` keeps a raw discovery snapshot for each session
generation:

- the outcomes of `/api`, of `/apis`, and of each advertised group version,
  core `v1` included. Each outcome is either the resources served, with their
  verbs, or the error the request raised. A malformed or skipped entry is a
  failure, never an absent resource;
- when it was fetched. The snapshot expires after 5 minutes. It is refreshed
  then, on an explicit refresh, and when a read reports `DISCOVERY_STALE`, and
  a new session generation starts a new one.

The raw snapshot knows nothing of observations. `discover_resources()` keeps
its signature for existing callers and is projected from it into
`ResourceMeta`.

**In `core`**, `api_versions` classifies each raw outcome into an
`Observation` of its `DiscoverySource` and builds the **`DiscoverySnapshot`**
that resolution reads:

- **`core_root`** and **`group_root`**: the observations of `/api` and
  `/apis`, kept apart because either can fail while the other succeeds;
- **`groups`**: for each advertised group version, its observation, and on
  success the resources it serves with their verbs;
- **`generation`**: the session generation the raw snapshot belongs to.

A diagnostic declares a **logical source**: an ordered list of
**candidates**, each with a group, a resource, the versions korvid can parse
and a parser. Events are one logical source with two candidates,
`events.k8s.io/v1` and then core `v1`.

`resolve(logical_source, snapshot)` takes the candidates in order, and for
each one returns the first case that applies:

1. **A known version is served.** An `ApiSource` with the most preferred
   version that was discovered successfully, that the candidate can parse,
   and that lists the verb the read needs. The version may be non-preferred
   (#345). A positive answer does not depend on other group versions, so a
   broken one does not block a working one.
2. **Incomplete discovery.** If the candidate's own root (`/api` for a core
   candidate, `/apis` for a named group), or any advertised version of the
   candidate's group, was forbidden or failed, the candidate is
   `DENIED`/`DISCOVERY_FORBIDDEN` or `UNAVAILABLE`/`DISCOVERY_FAILED`. Any
   negative conclusion would need the part korvid could not read. A failure
   of the other root does not affect the candidate.
3. **Served without the needed verb.** `UNSUPPORTED`/`OPERATION_UNSUPPORTED`.
4. **Only unknown versions are served.** `UNSUPPORTED`/`NO_KNOWN_VERSION`:
   an unfamiliar schema is never read as a known one.
5. **Not served.** `UNSUPPORTED`/`NOT_SERVED`.

The first candidate that resolves to an `ApiSource` is read. If that read is
`DENIED` or `UNSUPPORTED`, the next candidate is tried; RBAC treats the two
event groups as separate resources. The observation records which candidate
answered. When every candidate fails, the logical source reports the failure
the baseline precedence picks, with every candidate's outcome attached.

Server version strings are never consulted, so a newer server cannot enable
anything on its own.

### Evidence reads

Two bounded, cancellable functions, both returning the data read and one
`Observation`:

- `read_list(reader, source, *, page_size, max_pages, deadline)`;
- `read_object(reader, source, *, deadline)`, a named GET that records
  `observed_uid`.

Their behaviour:

- **Limits.** The defaults mirror Pulse: pages of 100 (`LIST_PAGE_SIZE`), at
  most 2 pages, and a 5-second deadline. A consumer may raise them up to the
  hard maxima of 10 pages (1,000 objects) and 15 seconds. Page size times
  pages is the object cap.
- **Byte budgets.** As in Pulse, each response is read through a byte budget
  before any JSON is parsed. The budget applies to the bytes on the wire and
  again after decompression. It defaults to Pulse's 256 KiB per response and
  may be raised to a hard maximum of 2 MiB. An error body is kept to 16 KiB.
  A response over budget, or a page with more items than requested, is
  closed unparsed and classified `TOO_LARGE`. With the page cap, this bounds
  both the memory a read holds and the time spent parsing it.
- **Completeness.** A LIST is `AVAILABLE` only when the server returned an
  empty continue token. Kubernetes may return a page with no items and a
  continue token, so zero items with a token pending never counts as an
  empty collection. After at least one accepted page, whatever its item
  count:
  - the page cap, reached with a continue token still pending, gives
    `PARTIAL`/`TRUNCATED`;
  - the deadline gives `PARTIAL`/`DEADLINE`;
  - the byte budget gives `PARTIAL`/`TOO_LARGE`.

  Before the first accepted page, the deadline and the byte budget give
  `UNAVAILABLE` with the same basis. Any other outcome is classified as in
  the table above.
- **Cancellation** propagates. There are no retries except one for 429,
  honouring `Retry-After` only within the deadline.
- **Metadata projection.** A `metadata` source asks for
  `PartialObjectMetadata` (GET) or `PartialObjectMetadataList` (LIST). If the
  server refuses it, the source is `UNSUPPORTED`/`PROJECTION_UNSUPPORTED`,
  and there is no fallback to an object read. A projection carries no
  top-level fields, so a ConfigMap's or Secret's `immutable` flag and a
  Secret's `type` are not available. A consumer reports them as not
  established and does not read the object for them.
- **Configuration payloads stay out.** The reader refuses the `object`
  projection for `configmaps` and `secrets`, so a diagnostic can read their
  metadata but never their data (#410). A test pins this for both resources
  and both functions.
- **Metadata reduction.** The metadata on the wire is not what a diagnostic
  receives. For every resource, the reader removes `managedFields` and the
  `kubectl.kubernetes.io/last-applied-configuration` annotation, which embeds
  the whole object, as `mask_secret_manifest` already does for Secrets. For
  ConfigMaps and Secrets it keeps only:
  - the identity: name, namespace, UID and resource version;
  - the creation and deletion timestamps;
  - labels;
  - owner references.

  Every annotation is dropped, since any of them can carry configuration
  data. A consumer that needs another field adds it to that allowlist with a
  test.
- **Resolved sources only.** Both functions refuse an `ApiSource` whose
  version is unset, and only `resolve` sets it. So every read uses a
  resource and verb that discovery listed.
- **Read-only.** The reader protocol the functions depend on has only GET and
  LIST methods.

### Run tokens and late results

A **`RunToken`** is `(epoch, session generation, run generation)`:

- the **epoch** comes from the context-switch coordinator;
- the **session generation** comes from `KubeClient`;
- the **run generation** is a counter each diagnostic controller increments
  when it reruns, or when the user changes the selection it explains.

Each read records the token it started under. The controller applies a result
only when the token still matches its current one; otherwise it drops the
result, as write flows do today. This covers:

- a context switch;
- a credential replacement;
- an older run of the same diagnostic finishing after a newer one;
- a selection that changed while a read was in flight.

There is no cache of read results to scope or invalidate. The discovery
snapshot is keyed by session generation, which is bumped on every
identity-changing event korvid can see: connect, context switch, kubeconfig
reload and credential replacement. So a snapshot is never used across those
events. The one gap is the case decision 4 names: a token refresh that
changes the subject without any of them.

### Presentation contract

This lands with the first consumer.

- **Availability.** A diagnostic's action stays available when some of its
  sources are missing. The palette, bindings and help use `UnavailableReason`
  only when the diagnostic cannot run at all (no selection, wrong view), so
  the three keep giving one answer.
- **One line per source.** Panels show
  `<kind> <group/version> in <scope>: <state> — <basis sentence> · observed <age>`.
  Stale and partial evidence are visible. Denied, unsupported and unavailable
  sources get distinct sentences.
- **Conclusions from absence.** A conclusion that rests on absence is shown
  only when its source is conclusive. Otherwise the panel says
  "not established".
- **One renderer.** A shared renderer in `ui/` turns an `Observation` into
  text, so widgets never interpret bases themselves. Cluster-provided names
  are rendered literally, without markup.
- **Agent and MCP exposure** is out of scope. When it is added, it goes
  through `tools/` and the existing masking pipeline.

### Security and privacy

- **No payloads.** Diagnostics never request a ConfigMap or Secret object,
  only its metadata. Metadata can still carry the payload, through the
  `last-applied-configuration` annotation, so what crosses the wire is not
  what a diagnostic receives: the reader reduces it first, as described
  under Evidence reads.
- **No sensitive text.** Details come from fixed templates and resource
  identities, never from response bodies, field values or configuration
  data. The classifier reads only a Status body's `reason` and `details`.
- **Read-only.** No write, dry-run or mutation probe exists, and access
  reviews are not used on the read path.
- **No hidden scans.** Reads happen at the scope the user selected. A
  cluster-scoped source is read only by a cluster-scoped diagnostic the user
  opened.
- **Optional extras stay optional.** The new modules import only the
  standard library, each other and `korvid.k8s`. Provider sources reach
  them later through `obs/`, which already imports `core`.

## Kubernetes support policy

Definitions:

- **Verified:** korvid's integration tests ran against a live API server of
  that minor version. The server may be provisioned in CI or be the AKS
  contract cluster.
- **Fixture-compatible:** covered only by unit tests with constructed
  payloads.

The policy:

- korvid claims support only for versions the version matrix verifies. The
  maintainer records the oldest supported version in the support document
  when S5 lands. This design does not choose it.
- Each release's notes list the versions verified for it.
- Features that need a newer API keep working through discovery and version
  resolution, never through a version check.
- Until S5 lands there is no support document, and korvid claims nothing
  beyond the AKS contract cluster, its only live verification today.

The version matrix runs on GitHub-hosted runners with kind, so it adds no
cloud cost. It runs on demand and when the capability modules change. Its
starting set of minor versions to *run* is 1.33, 1.35 and 1.36. That set is
not a support claim. 1.35 is also the AKS contract cluster's minor version,
and 1.36 is the API generation `kubernetes_asyncio` 36.1 targets. Each
version runs the same read-only scenarios:

- a resource served in a preferred and in a non-preferred version only;
- a resource served only in a version no adapter knows, using a test CRD;
- an optional API present (a test CRD installed) and absent;
- an aggregated API that is registered but unavailable;
- metadata-only LIST and GET of ConfigMaps and Secrets created with
  `kubectl apply`, so that the server returns `last-applied-configuration`;
- a principal denied a list, and another denied discovery of one group;
- Events through either group alone and through both.

## Testing

- **Vocabulary.**
  - Every state allows only its bases.
  - `conclusive` is false for `NO_DATA`, for stale observations and for
    retention-limited sources.
  - `baseline` returns the same result for every permutation of the
    declarations.
  - A required `confirms` source never needs to be conclusive, and a
    `rules_out` source always does.
  - Optional sources never lower the baseline.
  - Mixed scopes are counted, never collapsed.
  - A `STALE_STATUS` downgrade is never conclusive.
- **Classifier.** One table test covers every status, `Status` reason and
  error category for every read kind, including:
  - 404 with a `NotFound` naming the requested object, naming another
    object, and with no parseable body;
  - 405, always `DISCOVERY_STALE` for a LIST or GET, and 406 and 410 both
    inside and outside a metadata projection or a continuation;
  - an error body over its 16 KiB budget;
  - cancellation propagating.
- **Resolution.**
  - A preferred version is served.
  - Only a non-preferred version is served.
  - Only unknown versions are served.
  - The needed verb is missing.
  - Nothing is served.
  - `/api` failed or was forbidden while `/apis` succeeded, and the reverse:
    each failure affects only its own candidates, so complete core discovery
    is never `DENIED` because of `/apis`, and a named group is never
    `NOT_SERVED` because `/api` succeeded.
  - Every discovery request, successful or not, yields an `Observation` of
    its `DiscoverySource`.
  - A known version resolves even when a sibling group version failed
    discovery.
  - A group version was forbidden or failed while another succeeded, which
    must never yield `NOT_SERVED`, `NO_KNOWN_VERSION` or
    `OPERATION_UNSUPPORTED`.
  - Candidate fall-through, for Events through either group, both groups,
    a denied group and absence.
  - Snapshot expiry and refresh after `DISCOVERY_STALE`.
  - A 405 followed by a refresh: if the refresh drops the verb, the result is
    `OPERATION_UNSUPPORTED`; if the verb is still listed, the source stays
    `UNAVAILABLE`.
  - `discover_resources()`, projected from the raw snapshot, returns what it
    returns today for the same discovery answers.
- **Evidence reads.** With a fake reader:
  - page and deadline caps;
  - empty pages with a continue token pending, which stay `PARTIAL` and are
    never `AVAILABLE`;
  - a failure after an accepted page, with and without items, including a
    405 on a continuation and a 406 on a metadata continuation, which keep
    the accepted pages as `PARTIAL`;
  - an oversized response and a gzip expansion past the budget, both closed
    unparsed, and a page with more items than requested;
  - a 429 retry inside and outside the deadline;
  - an expired continuation;
  - cancellation;
  - the metadata projection, including a server that refuses it;
  - `observed_uid` against an expected UID.
- **Run tokens.** A result is dropped after a context switch, a credential
  replacement, a newer run or a selection change, and applied otherwise.
- **Guards.**
  - The reader protocol has no write method.
  - Both functions refuse an unresolved `ApiSource`.
  - An object-projection read of a ConfigMap or a Secret fails, through both
    functions.
  - Through both GET and LIST, ConfigMap and Secret metadata reaches the
    diagnostic with no annotation and no `managedFields`. No metadata of any
    resource carries `last-applied-configuration`.
  - `tests/test_optional_extras.py` covers the new modules.
  - `tach check` passes.
  - The Pulse mapping table test fails on drift.
- **Version matrix** (S5): the scenarios above against kind clusters of each
  listed version.

## Delivery

Each slice gets its own sub-issue under #409 and its own pull request, in
this order. #410 and #411 may start once S4 has merged, but #409 and v0.7.0
are not complete until S5 has also landed.

| Slice | Content |
| --- | --- |
| S1 | `core/capability`: states, bases, sources (API, discovery and provider) and roles, observations and the `RunToken` value they carry, `EvidenceSet` rules, `ReadRequest` and the classifier with `Status` parsing, `KubeClientError` categories, the Pulse mapping test |
| S2 | the session generation and the raw discovery snapshot in `KubeClient` (separate `/api` and `/apis` outcomes, group-version outcomes with verbs, TTL, refresh triggers), with `discover_resources()` projected from it; `core/api_versions`: the observation snapshot and logical-source resolution |
| S3 | `core/evidence_reads`: `read_list` and `read_object`, byte budgets, metadata projections and their reduction, the configuration-payload guard |
| S4 | the run generation and the late-result helper that applies a result only under the current token |
| S5 | the Kubernetes support document and the kind version-matrix workflow |
| S6 | the shared UI evidence renderer, in the first consumer's pull request |

After #410 and #411 ship, a follow-up issue converges Pulse, the graph and
the outcome tracker on this vocabulary.

## Non-goals

- New diagnostics, screens or palette entries. The consumers add those.
- Upfront capability probing, or access and rules reviews on the read path.
- Caching read results.
- Server-version gates, inferring feature gates, or choosing a minimum
  Kubernetes version in this design.
- Write, dry-run or mutation probes, and cluster-wide scans the user did not
  ask for.
- Collectors for node- or provider-scoped sources. Consumers from v0.8.0 on
  add them.
- Migrating Pulse, the graph or the outcome tracker in v0.7.0.
- Changing `KubeClient.can_i`, the fail-open write precheck.
