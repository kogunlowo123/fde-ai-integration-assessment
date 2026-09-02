# Test matrix

Requirement → test → expected result. Every row maps to a test that exists and
passes; the suite totals **472 tests**.

| Suite | Count | Scope |
|---|---|---|
| `tests/unit` | 186 | Validation, policy, PII patterns, limiter arithmetic, routing, configuration |
| `tests/streaming` | 57 | Chunk-boundary behaviour |
| `tests/rag` | 56 | Chunking, ingestion, retrieval quality, the MCP knowledge tool |
| `tests/integration` | 46 | Real subprocesses and ASGI apps |
| `tests/security` | 86 | Authentication, authorization, isolation, leakage, adversarial probes |
| `tests/e2e` | 35 | Client → gateway → provider → guardrail → client |
| `tests/concurrency` | 6 | The rate limiter under contention |

---

## Task 1, MCP server

### Customer id validation

Interpretation recorded: `CUST-XXXXX` is read as **exactly five decimal
digits**, because every example in the brief uses digits (`CUST-12345`) and the
rejection list requires an exact length. Widening to `[A-Z0-9]` is a
one-character change in `schemas.py` and nowhere else.

| Input | Expected | Test |
|---|---|---|
| `CUST-12345` | accepted | `test_accepts_well_formed_ids` |
| `CUST-123` | rejected (too few digits) | `test_rejects_malformed_strings` |
| `CUST-123456` | rejected (too many) | ″ |
| `customer-12345` | rejected (wrong prefix) | ″ |
| `CUST12345` | rejected (no separator) | ″ |
| `cust-12345` | rejected (case) | ″ |
| `CUST-1234A` | rejected (non-digit) | ″ |
| `""` | rejected | ″ |
| `" CUST-12345"` / `"CUST-12345 "` | rejected (whitespace) | ″ |
| `"CUST-12345\n"` | rejected, a non-anchored regex would accept this | ″ |
| `"CUST-12345; DROP TABLE customers"` | rejected | ″ |
| `null`, `12345`, `12.5`, `true`, `["CUST-12345"]`, `{...}` | rejected (type) | `test_rejects_wrong_json_types` |
| field missing | rejected | `test_rejects_missing_field` |
| unknown field present | rejected (`extra="forbid"`) | `test_rejects_unknown_field` |

### Refund validation

| Input | Expected | Test |
|---|---|---|
| `amount=25.50`, valid reason | accepted | `test_accepts_the_assessment_example` |
| `amount=25` (integer) | accepted | `test_accepts_integer_amount` |
| `amount=0` | rejected (not positive) | `test_rejects_out_of_range_amounts` |
| `amount=-1.0`, `-0.01` | rejected | ″ |
| `amount=NaN` | rejected | ″ |
| `amount=Infinity` / `-Infinity` | rejected | ″ |
| `amount=1000000.01` | rejected (ceiling) | ″ |
| `amount="25.50"` | rejected, lax coercion must not repair a money field | `test_rejects_wrong_amount_types` |
| `amount=true` | rejected (`bool` is an `int` subclass) | ″ |
| `amount=null` / list / object | rejected | ″ |
| `reason` = 9 characters | rejected | `test_rejects_bad_reasons` |
| `reason` = 10 characters | accepted | `test_accepts_exactly_ten_characters` |
| `reason` = 10 spaces | rejected, satisfies `min_length`, carries no audit value | `test_rejects_bad_reasons` |
| `reason` > 512 characters | rejected | ″ |
| any required field missing | rejected | `test_rejects_missing_required_field` |
| unknown field | rejected | `test_rejects_unknown_field` |

### Protocol behaviour (real stdio subprocess)

| Scenario | Expected | Test |
|---|---|---|
| `initialize` handshake | serverInfo returned | `test_initialize_returns_server_info` |
| `tools/list` | both tools with JSON schemas | `test_tools_advertise_input_schemas_on_the_wire` |
| Valid lookup | `isError: false`, structured content | `test_valid_customer_lookup` |
| Valid refund | receipt with `REF-` id | `test_valid_refund` |
| Malformed customer id | JSON-RPC `-32602` | `test_malformed_customer_id_is_invalid_params` |
| Negative amount | `-32602` | `test_negative_refund_is_invalid_params` |
| Short reason | `-32602` | `test_short_reason_is_invalid_params` |
| Unknown tool | `-32601` | `test_unknown_tool_is_method_not_found` |
| Unknown method | `-32601` | `test_unknown_method_is_method_not_found` |
| Error message content | no traceback, path, module or library name | `test_error_messages_do_not_leak_internals` |
| Malformed frame mid-session | session keeps serving | `test_survives_a_malformed_frame_and_keeps_serving` |
| Unknown customer | `isError: true` result, **not** a JSON-RPC error | `test_unknown_customer_is_a_domain_outcome_not_a_protocol_error` |
| Suspended customer refund | domain refusal | `test_refund_for_a_suspended_customer_is_refused` |
| Handler raises with a DSN in the message | sanitised generic failure | `test_handler_exceptions_are_sanitised` |

### STDIO isolation

| Check | Expected | Test |
|---|---|---|
| Every stdout line at `LOG_LEVEL=DEBUG` | parses as JSON-RPC 2.0 | `test_every_stdout_line_is_a_jsonrpc_frame` |
| Diagnostics actually occurred | `mcp_server_starting`, `tool_validation_failed` on stderr | ″ |
| stderr format | structured JSON | `test_stderr_is_structured_json` |
| `configure_logging` output | stderr only, stdout empty | `test_configure_logging_writes_to_stderr_only` |
| structlog sink | bound to stderr, not stdout | `test_structlog_factory_is_bound_to_stderr` |
| `print(` in `src/` | none | `test_no_print_calls_in_the_source_tree` |
| `sys.stdout` in `src/` | none | `test_no_stdout_writes_in_the_source_tree` |
| **Negative control** | a deliberate `print` *does* corrupt the wire | `test_a_print_before_serving_would_corrupt_the_wire` |

---

## Task 2, MCP security gateway

| Requirement | Expected | Test |
|---|---|---|
| `Bearer <token>` parsed | token extracted | `test_extracts_the_token` |
| Missing / empty / wrong scheme / lowercase scheme | 401 | `test_rejects_malformed_headers` |
| Unknown token | 401 | `test_unknown_token_is_rejected` |
| Tampered token | 401 | `test_tampered_token_is_401` |
| Raw token on the principal | never | `test_principal_never_carries_the_raw_token` |
| 401 body | JSON-RPC shaped, no token echo | `test_401_body_is_jsonrpc_shaped_and_leaks_nothing` |
| `tools/list` | forwarded transparently | `test_tools_list_is_forwarded_transparently` |
| Viewer → `admin_reset_key` | exact `-32001 Unauthorized Tool Call` payload | `test_viewer_calling_admin_tool_is_intercepted` |
| **Downstream on denial** | **call count 0** | `test_downstream_is_never_invoked_for_a_denied_call` |
| Admin → `admin_reset_key` | forwarded, succeeds | `test_admin_calling_admin_tool_is_forwarded` |
| Viewer → non-admin tool | forwarded | `test_viewer_may_call_a_non_admin_tool` |
| Every `admin_*` name | denied for viewer | `test_every_admin_prefixed_name_is_gated` |
| `Admin_`, `ADMIN_`, ` admin_`, `x_admin_` | not treated as admin; forwarded then rejected downstream as unknown | `test_prefix_check_is_exact_and_case_sensitive` |
| `"role": "admin"` in the body, viewer token | still denied | `test_body_supplied_role_is_ignored` |
| `params.name` as an object | denied, not forwarded | `test_non_string_tool_name_is_denied` |
| Unknown method | rejected at the gateway | `test_unknown_method_is_denied` |
| Malformed envelopes (8 shapes) | `-32600`, downstream untouched | `test_malformed_envelopes_are_rejected_before_forwarding` |
| Non-JSON body | 400 `-32700` | `test_non_json_body_is_a_parse_error` |
| Oversized body | 413, downstream untouched | `test_oversized_body_is_413` |
| Correlation id echoed | `x-request-id` returned | `test_correlation_id_is_echoed` |
| Hostile correlation id | sanitised to `[A-Za-z0-9-_]` | `test_hostile_correlation_id_is_sanitised` |
| Overlong correlation id | truncated to 64 | `test_overlong_correlation_id_is_truncated` |
| Downstream timeout | safe message, no host or IP | `test_downstream_timeout_becomes_a_safe_error` |
| Downstream 500 with a traceback | no traceback or path in the response | `test_downstream_500_becomes_a_safe_error` |
| Downstream HTML / JSON array | protocol error | `test_downstream_html_becomes_a_protocol_error` |
| Connection refused | no errno in the response | `test_connection_refused_becomes_a_safe_error` |
| Status contract (ADR-011) | 401/413/400 transport, 200 for JSON-RPC outcomes | `TestStatusCodeContract` |

---

## Task 3, streaming PII guardrail

| Case | Expected | Test |
|---|---|---|
| Email in one chunk | `[REDACTED]` | `test_whole_text_in_one_chunk` |
| SSN in one chunk | `[REDACTED]` | ″ |
| Card in one chunk | `[REDACTED]` | ″ |
| **`john.smith@` / `example.` / `com`** | `[REDACTED]`, the brief's example | `test_the_assessment_example_split_into_three` |
| Every fixed chunk size (1, 2, 3, 5, 7, 11, 17, 64) | identical to single-chunk output | `test_every_fixed_chunk_size_matches_the_single_chunk_result` |
| **Every possible two-way split** | identical output at every cut point | `test_every_possible_two_way_split` |
| Character-by-character streaming | identical output | `test_character_by_character` |
| PII at the very start / very end | redacted | `test_pii_at_the_very_start_of_the_stream`, `..._end_...` |
| Adjacent PII values | both redacted | `test_adjacent_pii_values` |
| Empty chunks | ignored | `test_empty_chunks_are_ignored` |
| Empty stream | empty output | `test_empty_stream` |
| Whitespace-only stream | preserved | `test_whitespace_only_stream` |
| Unicode | preserved | `test_unicode_is_preserved` |
| Partial PII that never completes | emitted verbatim | `test_partial_pii_that_never_completes_is_emitted_verbatim` |
| 200 × 50-char unbroken token | carry ≤ window at every step | `test_carry_never_exceeds_the_window` |
| 2,000-chunk stream | ≥50 KB flows through, carry stays bounded | `test_long_stream_does_not_accumulate` |
| Prose tail (`"The answer is "`) | emitted immediately, carry 0 | `test_ordinary_prose_is_emitted_without_delay` |
| Match longer than the window | documented partial escape | `test_overlong_match_is_documented_to_be_truncated` |
| Client disconnect mid-stream | no exception, carry discarded | `test_client_disconnect_does_not_raise` |
| Provider error mid-stream | propagates | `test_provider_error_propagates` |
| Terminal event | carries the token total | `test_terminal_event_carries_token_total` |
| Luhn-valid cards (Visa, MC, Amex, Discover) | redacted | `TestCard` |
| Luhn-**invalid** 16-digit reference | **preserved** | `test_leaves_luhn_invalid_16_digit_numbers_alone` |
| `2024`, `12345` | preserved | `test_leaves_short_numbers_alone` |
| SSN inside a longer digit run | not matched | `test_does_not_match_inside_a_longer_digit_run` |
| Redaction applied twice | idempotent | `test_is_idempotent` |
| Over HTTP, chunk size 3 | 3 redactions, tail intact | `test_pii_split_across_provider_chunks_is_redacted` |
| Over HTTP, chunk size 1 | 3 redactions | `test_single_character_chunks_still_redact` |
| Non-streaming response | redacted too | `test_pii_is_redacted_in_the_collected_answer` |

---

## Task 4, rate limiting and fallback

### Boundary and window

| Case | Expected | Test |
|---|---|---|
| First request | admitted | `test_first_request_is_admitted` |
| **Exactly 50,000 tokens** | **admitted** (inclusive limit) | `test_exactly_the_limit_is_admitted` |
| **50,001 tokens** | **rejected** | `test_one_token_over_the_limit_is_rejected` |
| 49,999 then 1 then 1 | admit, admit, reject | `test_the_boundary_across_two_requests` |
| Rejected request | not charged | `test_a_rejected_request_is_not_charged` |
| Request larger than the whole limit | always rejected, `Retry-After` set | `test_a_single_request_larger_than_the_limit_can_never_pass` |
| Zero tokens | admitted | `test_zero_token_request_is_admitted` |
| Negative tokens | `ValueError` (programming error) | `test_negative_tokens_are_rejected_as_a_programming_error` |
| 61 s later | budget available again | `test_tokens_age_out_of_the_window` |
| Sliding, not fixed | no 2× burst across a boundary | `test_window_slides_rather_than_resetting` |
| Expired rows | evicted inside the admission transaction | `test_expired_rows_are_evicted` |
| `Retry-After` | derived from the oldest in-window event | `test_retry_after_reflects_the_oldest_event` |
| Two tenants | independent budgets | `test_tenants_have_independent_budgets` |
| Over-estimate | refunded by reconciliation | `test_over_estimate_is_refunded` |
| Under-estimate | charged | `test_under_estimate_is_charged` |
| Reconnect to the same file | usage survives | `test_state_survives_a_reconnect` |

### Concurrency

| Case | Expected | Test |
|---|---|---|
| 100 concurrent × 1,000 tokens, 50,000 budget | **exactly 50 admitted**, total exactly 50,000 | `test_exact_budget_is_never_exceeded` |
| 50 concurrent, uneven sizes | total never exceeds the limit | `test_no_overshoot_with_uneven_request_sizes` |
| Two tenants hammering concurrently | 10 admitted each, no interference | `test_concurrent_tenants_do_not_interfere` |
| 500 concurrent single-token requests, 1,000 budget | all 500 admitted (no spurious rejection) | `test_high_contention_burst` |
| **Two connections to one file** | shared budget respected | `test_two_connections_share_one_budget` |
| 50 writers + 50 readers | completes without `database is locked` | `test_mixed_reads_and_writes_complete` |

### Routing and fallback

| Case | Expected | Test |
|---|---|---|
| Healthy primary | used; secondary never called | `test_primary_is_used_when_healthy` |
| **Primary 429** | failover; `fallback_total` incremented | `test_primary_429_fails_over` |
| Unavailable / protocol error | failover | `test_every_retryable_failure_fails_over` |
| **Primary exceeds the first-token deadline** | failover, without waiting for the hung call | `test_primary_timeout_fails_over` |
| Hung primary after failover | **cancelled** | `test_the_hung_primary_is_actually_cancelled` |
| Slow but responsive primary | kept | `test_a_slow_but_responsive_primary_is_kept` |
| Long generation that starts promptly | not aborted (deadline is TTFT) | `test_the_deadline_covers_first_token_not_whole_generation` |
| Invalid request from the provider | **no failover** | `test_a_non_retryable_failure_does_not_touch_the_secondary` |
| Both providers failing | sanitised error, both codes recorded | `test_secondary_failure_surfaces_a_sanitised_error` |
| Empty primary stream | protocol error → failover | `test_an_empty_primary_stream_is_a_protocol_error` |
| Provider raising with a DSN and password | normalised, nothing leaked | `test_a_non_gateway_exception_is_normalised` |
| Failure **after** the first token | no failover; partial answer preserved | `test_failure_after_the_first_token_does_not_fail_over` |

### Gateway behaviour

| Case | Expected | Test |
|---|---|---|
| Missing / unknown API key | 401 | `TestAuthentication` |
| `X-API-Key` header | accepted | `test_x_api_key_header_is_accepted` |
| 8 invalid request shapes | 422 `INVALID_PARAMS` | `test_invalid_bodies_are_422` |
| Oversized body | 413 | `test_oversized_body_is_413` |
| Over budget | 429 + `Retry-After` + limit headers | `test_over_budget_request_is_429` |
| Rejected request | provider never called | `test_a_rejected_request_never_reaches_the_provider` |
| Tenant isolation over HTTP | independent budgets | `test_tenants_are_isolated` |
| Both providers down | 502, no DSN or host in the body | `test_both_providers_down_returns_a_sanitised_502` |
| Failure after streaming started | terminal SSE error frame, same envelope | `test_stream_failure_is_delivered_as_a_terminal_sse_frame` |

---

## Configuration

| Case | Expected | Test |
|---|---|---|
| Every `Settings` field documented in `.env.example` | yes | `test_every_setting_is_documented` |
| No stale keys in `.env.example` | yes (`extra="ignore"` would hide them) | `test_no_undocumented_or_stale_keys` |
| Generator output current | matches the committed file | `test_the_generator_is_up_to_date` |
| 10 invalid configurations | rejected at startup | `test_invalid_configuration_is_rejected_at_startup` |
| `APP_ENV=production` with dev credentials | refuses to start, names each offender | `test_production_refuses_development_credentials` |
| One leftover default | still refuses | `test_a_single_leftover_default_still_blocks_production` |
| Default bind host | `127.0.0.1` | `test_binds_loopback_by_default` |
| Pepper in `repr(settings)` | absent | `test_the_pepper_is_not_printed_by_accident` |

---

## RAG (Production Enhancement)

| Case | Expected | Test |
|---|---|---|
| Tenant A retrieves its own document | yes | `test_a_tenant_retrieves_its_own_document` |
| **Tenant B queries A's content verbatim** | **A's document never returned** | `test_a_tenant_never_retrieves_another_tenants_document` |
| Reverse direction | isolated | `test_isolation_holds_in_both_directions` |
| Unknown tenant | nothing | `test_an_unknown_tenant_gets_nothing` |
| Store queried directly with another tenant | isolated in SQL | `test_the_store_itself_refuses_to_cross_tenants` |
| Retrieval without a tenant | `ValueError` | `test_retrieval_without_a_tenant_is_a_programming_error` |
| Classification filter | restricted documents excluded | `test_classification_filter_excludes_restricted_documents` |
| Filtered content in the prompt | absent before the model sees it | `test_filtering_happens_before_the_model_sees_anything` |
| Injected `</retrieved_context><system>` | neutralised; exactly one open/close marker | `test_injected_document_cannot_close_the_context_block` |
| Context labelling | system prompt declares it untrusted data | `test_retrieved_text_is_labelled_as_untrusted_data` |
| Injection inducing `admin_reset_key` | still denied by the MCP gateway | `test_injection_cannot_reach_a_privileged_tool` |
| Citations | only passages actually in the prompt | `test_only_included_passages_are_cited` |
| No hits | no citations invented | `test_no_hits_produces_no_citations` |
| Unchanged document re-ingested | **not re-embedded** | `test_unchanged_document_is_not_re_embedded` |
| Whitespace-only reformatting | still skipped | `test_reformatting_alone_does_not_trigger_re_embedding` |
| Changed document | re-embedded | `test_changed_document_is_re_embedded` |
| Removed section | no longer retrievable | `test_removed_content_stops_being_retrievable` |
| Chunking determinism, size bound, overlap | held | `TestChunking` |
| Recall@1 / Recall@3 / MRR@3 | ≥ 0.60 / 0.85 / 0.60 (measured: 0.63 / 1.00 / 0.79 MRR@5) | `TestRetrievalQuality` |
| `top_k` above the configured max | capped | `test_top_k_is_capped_by_configuration` |
| Irrelevant query | ≤1 hit (score floor) | `test_irrelevant_query_returns_little_or_nothing` |
| MCP tool argument surface | `query`, `top_k`, `document_type` only, no path, URL or tenant | `TestToolContract` |
| `top_k` = 0, −5, 26, 10000 | rejected by the schema | `test_out_of_range_top_k_is_rejected_by_the_schema` |
| Tool over real stdio with a corpus | answers, stdout stays pure | `test_tool_appears_and_answers_over_the_wire` |
| Two tenants, same question, over HTTP | different context, no bleed | `test_each_tenant_gets_only_its_own_context` |
| `tenant_id` in the request body | 422 | `test_tenant_cannot_choose_its_own_scope` |

---

## Adversarial probes (`tests/security/test_adversarial.py`)

Attacks run against the finished system. Where the outcome is "allowed but
harmless", the test says so rather than dressing it up as a defence.

| Attack | Outcome | Test |
|---|---|---|
| `admin_` prefix padded with a space, newline, tab or zero-width space | still denied | `test_prefix_evasion_by_whitespace_or_padding_still_denies` |
| Cyrillic homoglyph prefix (`аdmin_reset_key`) | not treated as admin; forwarded, rejected downstream as unknown, safe *because* no such tool exists | `test_homoglyph_prefix_is_not_treated_as_admin` |
| Duplicate `name` keys in `params` | last-value-wins; still denied | `test_duplicate_json_keys_do_not_confuse_the_policy` |
| `TOOLS/CALL` method casing | unknown method, rejected by the allowlist | `test_method_case_variation_is_not_a_policy_hole` |
| Two `Authorization` headers (viewer + admin) | no role upgrade | `test_second_authorization_header_does_not_upgrade_the_role` |
| Null byte inside the tool name | denied | `test_null_byte_in_the_tool_name` |
| RLO direction-override in the tool name | denied | `test_unicode_direction_override_in_a_tool_name` |
| 2,000-deep JSON nesting bomb | clean error; gateway keeps serving | `test_deeply_nested_json_is_rejected_or_survived` |
| 300 KB tool name | 413 before parsing | `test_enormous_string_field_is_bounded` |
| `__proto__` key in tool arguments | rejected by `extra="forbid"` | `test_prototype_pollution_style_keys_are_rejected` |
| **Full-width digits in `customer_id`** | **rejected, was accepted before this pass (F-1)** | `test_control_characters_and_homoglyph_digits_are_rejected` |
| CRLF header-injection attempt in `customer_id` | rejected | ″ |
| `1e308`, `1e400`, `10**20`, `"1e5"`, `"0x10"` as an amount | all rejected | `test_extreme_refund_amounts_are_rejected` |
| 100 KB refund reason | rejected | `test_a_refund_reason_cannot_smuggle_a_huge_payload` |
| Email in brackets, angle brackets, uppercase, comma-adjacent | still redacted | `test_formatting_variations_are_still_caught` |
| Spaced-out, "AT/DOT", base64 and defanged emails | **not** redacted, the documented recall limit, asserted | `test_obfuscation_defeats_the_regex_guardrail` |
| PII interleaved across single-character chunks | all three redacted | `test_interleaved_pii_across_many_tiny_chunks` |
| Match starting exactly at the emit cut | redacted | `test_pii_repeated_at_the_window_boundary` |
| `max_tokens` above the schema ceiling | 422 | `test_max_tokens_above_the_schema_ceiling_is_rejected` |
| 500-message conversation | 422 | `test_a_huge_message_list_is_rejected` |
| Five different error paths across both gateways | no path, traceback, module name or drive letter in any body | `test_no_response_anywhere_contains_a_file_path` |
| Error body shape | exactly `{error: {type, code, message, request_id}}`, under 300 bytes | `test_error_bodies_are_small_and_structured` |

---

## Not tested here

Stated so the matrix is not read as complete coverage:

- **Real provider behaviour.** The mock cannot catch prompt-formatting bugs or
  tokenizer edge cases (ADR-008). Shadow-mode rollout covers this.
- **Ollama paths.** Marked `@pytest.mark.ollama`, excluded by default.
- **Multi-process rate limiting.** Two connections to one file are tested; two
  machines are out of scope by design (ADR-006).
- **Load and soak.** `scripts/benchmark.py` measures latency, not sustained
  throughput or memory over hours.
- **TLS, mTLS and network policy.** Deployment concerns; not exercised by the
  suite.
- **PII recall against a real corpus.** The suite proves the patterns that are
  implemented work, not that they are sufficient (SECURITY.md, "Not claimed").
