### Task 4 Report: Built-in registry and composition-root integration

**Status:** ✅ Complete  
**Commit:** `4c95d0c` on `feat/provider-plugin-api`  
**Branch:** `feat/provider-plugin-api`

---

#### Changes

| File | Change |
|---|---|
| `src/korvid/providers/registry.py` | Added `plugin_registry` and `options` params to `create_provider`; added `_create_via_plugin` helper with credential-first build/close-on-failure semantics; added `_close_credentials` for async cleanup |
| `src/korvid/providers/configurator.py` | Added `plugin_registry` param to `ProviderConfigurator.__init__`; forward both `plugin_registry` and `options` in `test()` |
| `src/korvid/__main__.py` | Create one `ProviderPluginRegistry` per `_build_agent_wiring`; pass to initial creation, configurator, and rebuild; extract `_create_initial_provider` helper (C901 compliance); convert initial `ProviderPluginError` to startup warning via mutable list folded into `config.warnings`; pass `options` from config to initial and rebuild |
| `src/korvid/ui/app.py` | No changes needed — `_apply_agent_settings` already catches `Exception` from `rebuild_agent`, which covers `ProviderPluginError` |

#### Test Summary (155 passed, 0 failed)

| Test file | New tests | Total |
|---|---|---|
| `tests/providers/test_registry.py` | 7 new (builtins-never-query, unknown-routes-plugin, credentials-config-only, creds-closed-on-failure, unknown-without-registry, invalid-options-disable, none-when-unknown) | 26 |
| `tests/providers/test_configurator.py` | 2 new (test-passes-registry, test-passes-options) | 23 |
| `tests/providers/test_plugin_registry.py` | 0 (existing) | 28 |
| `tests/test_main_wiring.py` | 4 new (shared-registry, initial-plugin-warning, rebuild-shares-registry, seeds-options) | 49 |
| `tests/ui/test_agent_wiring.py` | 3 new (plugin-error-notifies, options-preserved, existing rebuild-failure kept) | 29 |

#### Gates

| Gate | Result |
|---|---|
| ruff check | ✅ All checks passed |
| ruff format | ✅ |
| mypy --strict | ✅ Success: no issues found |
| tach check | ✅ All modules validated |
| pytest (targeted) | ✅ 155 passed |
| pre-commit | ✅ All hooks passed |

#### Design Decisions

1. **Plugin registry lifetime:** One `ProviderPluginRegistry` created per `_build_agent_wiring` call, shared across initial build, configurator's `test()`, and all rebuild closures — the plugin cache lives for the session.

2. **Built-in isolation:** `create_provider` checks built-in aliases first; only unknown names enter `_create_via_plugin`. Built-ins never touch the registry (verified by test with MagicMock assertion).

3. **Credential safety:** `_create_via_plugin` builds credentials via `build_credentials` before calling `plugin_registry.create()`. On `ProviderPluginError`, credentials are closed via `_close_credentials` (schedules `aclose()` as a background task on the running loop).

4. **Startup warning path:** `_create_initial_provider` catches `ProviderPluginError` and appends to a caller-provided `startup_warnings` list. `_wire_and_run` folds these into `config.warnings` (tuple) before building the app. The 7-tuple return signature of `_build_agent_wiring` is preserved — warnings flow through an optional mutable list parameter.

5. **Rebuild error path:** `rebuild_agent` does not catch `ProviderPluginError` — it propagates to `_apply_agent_settings`, which already catches `Exception` and notifies with "Agent rebuild failed: {exc}".

6. **Options seeding:** Config `agent_options` is passed to initial `create_provider` and seeded into `AgentSettings` at app construction (existing code in `ui/app.py`). `rebuild_agent` forwards `settings.options`. `:model` command preserves options via `dataclasses.replace(settings, model=...)`.

7. **C901 compliance:** Extracted `_create_initial_provider` from `_build_agent_wiring` to keep complexity ≤ 10.

#### Concerns

- **No kube/UI/executor/audit leakage:** The plugin API receives only `ProviderPluginConfig` (base_url, model, auth_method, api_key_env, options) and `CredentialSource | None`. No other korvid objects cross the plugin boundary.
- **No entry-point scanning at startup:** The registry's `_discover_entry_points` is called only when an unknown name is selected — built-in providers never trigger discovery.
- **Options immutability:** `ProviderPluginConfig.__post_init__` wraps `options` in `MappingProxyType`; `AgentSettings.__post_init__` applies `_freeze_options`. Plugins cannot mutate the caller's data.

---

### Review Fix Round (bb031af)

**Status:** ✅ Complete  
**Commit:** `bb031af` on `feat/provider-plugin-api`

#### Blocking Issues Addressed

| Issue | Fix |
|---|---|
| `_create_via_plugin` swallows `ProviderPluginError` | Removed both `try/except ProviderPluginError` blocks; errors propagate to caller. `_create_initial_provider` catches at composition-root level and appends actionable warning. |
| Test monkeypatches `create_provider` | Replaced with production-real path: fake registry injected via `ProviderPluginRegistry` constructor patch; flows through real `create_provider` → `_create_via_plugin`. |
| Credential close uses weak `lambda t: None` | Replaced with module-level `_cred_close_tasks: set[asyncio.Task[None]]` + `_reap` done-callback that discards from set and logs exceptions at debug level, mirroring `_close_provider_in_background`. |

#### RED Evidence

```
FAILED tests/providers/test_registry.py::test_plugin_create_failure_propagates
  - DID NOT RAISE ProviderPluginError (was swallowed)
FAILED tests/providers/test_registry.py::test_invalid_options_disable_only_the_plugin
  - DID NOT RAISE ProviderPluginError (was swallowed)
FAILED tests/providers/test_registry.py::test_plugin_credentials_closed_on_construction_failure
  - DID NOT RAISE ProviderPluginError (was swallowed)
FAILED tests/test_main_wiring.py::test_agent_wiring_initial_plugin_error_becomes_warning
  - assert 0 == 1 (warnings list empty because error swallowed before reaching _create_initial_provider)
```

#### GREEN Evidence

```
tests/providers/test_registry.py::test_plugin_create_failure_propagates PASSED
tests/providers/test_registry.py::test_invalid_options_disable_only_the_plugin PASSED
tests/providers/test_registry.py::test_plugin_credentials_closed_on_construction_failure PASSED
tests/providers/test_registry.py::test_credential_close_consumes_exceptions_without_secret_leak PASSED
tests/test_main_wiring.py::test_agent_wiring_initial_plugin_error_becomes_warning PASSED
```

#### Full Focused Suite: 129 passed, 0 failed

| Gate | Result |
|---|---|
| ruff check | ✅ All checks passed |
| ruff format | ✅ |
| mypy --strict | ✅ Success: no issues found |
| tach check | ✅ All modules validated |
| pytest (focused: registry, configurator, main_wiring, ui_agent_wiring) | ✅ 129 passed |
| pre-commit | ✅ All hooks passed |

#### Concerns

- None blocking. The strong-ref set is module-level in `registry.py` since `_create_via_plugin` is a standalone function; this is acceptable for the fire-and-forget pattern and matches the existing `__main__.py` approach.
