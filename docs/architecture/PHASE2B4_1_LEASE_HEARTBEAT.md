# UTDA Phase 2B.4.1 — Worker Lease Lifecycle & Transaction Boundaries

**Status:** Phase 2B.4.1 Design & Lifecycle Reference  
**Baseline Commit:** `6712928` (`setup/utda-foundation`)  
**Immutability Guarantee:** Historical migrations `0001`–`0005` remain unchanged. `UTDA_DRY_RUN=true` remains enforced.

---

## 1. Existing Lease Lifecycle & State Transitions

UTDA models worker lease ownership on two tables under schema `utda`:
1. **`utda.idempotency_keys`** (lock owner & lease horizon):
   - Columns: `idempotency_key` (PK), immutable 5-tuple `(scope, project_id, feature_id, report_blob_sha, target_document_id)`, `status` (`IN_PROGRESS`, `COMPLETED`, `FAILED`, `SKIPPED`), `active_attempt_id` (FK `(idempotency_key, active_attempt_id)` $\to$ `utda.processing_attempts(idempotency_key, attempt_id)` `DEFERRABLE INITIALLY DEFERRED` from `0003`), `attempt_fence_version` (`BIGINT >= 0` from `0004`), `locked_at`, `expires_at`, `completed_at`, `result_summary`.
2. **`utda.processing_attempts`** (ordered per-attempt audit history):
   - Columns: `attempt_id` (PK), `event_receipt_id`, `idempotency_key` (FK `ON DELETE CASCADE`), `attempt_number` (`>= 1`, unique `(idempotency_key, attempt_number)`), `status` (`IN_PROGRESS`, `SUCCEEDED`, `FAILED`), `retry_state`, `next_retry_at`, `error_code`, `error_summary`, `started_at`, `finished_at`.

### Lifecycle Phases

1. **Claim (`UTDADatabaseRepository.claim_idempotency_and_attempt`):**
   - Validates canonical `project_id` (`validate_project_id`) before checkout.
   - Opens project-scoped transaction (`set_config('utda.current_project_id', pid, true)`).
   - Inserts seed row (`ON CONFLICT (idempotency_key) DO NOTHING`), then locks `idempotency_keys` `FOR UPDATE` (under RLS `0005`; an RLS-hidden cross-project collision raises `IdempotencyIdentityMismatchError`).
   - Verifies exact 5-tuple request identity immutability.
   - Reclaimable only when newly inserted, `status == 'FAILED'`, or `(status == 'IN_PROGRESS' AND expires_at < NOW())`.
   - Retires prior `IN_PROGRESS` attempts as `FAILED` (`LOCK_EXPIRED`), allocates next sequential `attempt_number` + UUID `attempt_id`, increments `attempt_fence_version += 1`, sets `active_attempt_id = attempt_id`, `status = 'IN_PROGRESS'`, `locked_at = NOW()`, and `expires_at = NOW() + lock_ttl_seconds`.
   - Reconciles `CommitOutcomeUnknownError` read-only by `(attempt_id, idempotency_key)`.

2. **Active Lease Heartbeat & Renewal (`LeaseHeartbeat` + `renew_attempt_lease`):**
   - Spawned immediately after `claim` returns `claimed=True`.
   - Background daemon-safe thread wakes every `heartbeat_interval_seconds` (`0 < interval < lock_ttl_seconds / 2` and `< lock_ttl_seconds - self_fence_margin_seconds`).
   - Calls `renew_attempt_lease()` in its **own short-lived pooled transaction** (never shares a `psycopg` connection with the main worker thread).
   - `renew_attempt_lease()` locks `idempotency_keys` `FOR UPDATE` and `processing_attempts` `FOR UPDATE`, verifying:
     1. Validated canonical `project_id` + RLS `utda.current_project_id`.
     2. Key exists and belongs to `project_id` (row check + RLS).
     3. `idempotency_keys.status == 'IN_PROGRESS'`.
     4. `active_attempt_id == attempt_id`.
     5. `attempt_fence_version == expected_fence_version` (when supplied).
     6. Lease not yet expired (`expires_at IS NULL OR expires_at >= NOW()`).
     7. `processing_attempts.status == 'IN_PROGRESS'`.
   - Extends `expires_at = NOW() + extend_ttl_seconds` without altering `attempt_fence_version` or `active_attempt_id`.
   - On `CommitOutcomeUnknownError`, reconciles via read-only query (`run_project_readonly`): confirms active ownership + fence + non-expired lease before recovering or raising `StaleAttemptError`.
   - On definitive lease loss (`StaleAttemptError`, `IdempotencyIdentityMismatchError`) **or** transient DB unavailability crossing the self-fence deadline (`last_renewed_monotonic + (lock_ttl_seconds - self_fence_margin_seconds)`), `LeaseHeartbeat` sets `abort_event` and terminates immediately.

3. **Worker Completion / Failure & Finalization (`finalize_processing_attempt`):**
   - Worker checks `heartbeat.raise_if_lost()` at processing checkpoints and before finalization; stops processing immediately if `abort_event` is set.
   - Context exit (`with LeaseHeartbeat(...)`) unconditionally signals `stop_event` and joins the heartbeat thread on both normal completion and exceptions.
   - If the lease was **lost**, the worker aborts and propagates `LeaseLostError` / `StaleAttemptError` without overwriting the new owner's key state.
   - Otherwise, `finalize_processing_attempt` verifies `project_id`, `status == 'IN_PROGRESS'`, `active_attempt_id == attempt_id`, `attempt_fence_version == expected_fence_version`, and non-expired lease, then atomically updates `processing_attempts` and clears `active_attempt_id = NULL` (`COMPLETED` or `FAILED`).

---

## 2. Database Transaction Boundaries Inventory

| Step | Method / Component | Boundary & Isolation | `set_config` Scope | Commit / Failure Semantics |
| --- | --- | --- | --- | --- |
| **TB-1: Project Sync** | `UTDADatabaseRepository.upsert_project` | Short project tx (`run_in_project_transaction`) | `utda.current_project_id = pid` (`is_local=true`) | Retries pre-commit transient errors; reconciles `CommitOutcomeUnknownError` by `project_id`. |
| **TB-2: Event Receipt** | `UTDADatabaseRepository.record_event_receipt` | Short project tx (`ON CONFLICT (delivery_id) DO NOTHING`) | `utda.current_project_id = pid` | Reconciles `CommitOutcomeUnknownError` by `delivery_id`. |
| **TB-3: Report / State Correlation** | `record_final_report`, `correlate_feature_state` | Separate short project txs | `utda.current_project_id = pid` | Idempotent unique constraints + ambiguous-commit reconciliation. |
| **TB-4: Fenced Claim** | `claim_idempotency_and_attempt` | Single atomic project tx (`FOR UPDATE` + attempt insert + key fence update) | `utda.current_project_id = pid` | Releases DB transaction & lock immediately on commit; reconciles by `attempt_id`. |
| **TB-5: Periodic Lease Renewal** | `LeaseHeartbeat` $\to$ `renew_attempt_lease` | Separate short pooled project tx per tick (`FOR UPDATE` key + attempt check + expiry extend) | `utda.current_project_id = pid` | Isolated from main worker connection; reconciles `CommitOutcomeUnknownError`; self-fences on sustained outage before TTL expires. |
| **TB-6: Worker Computation** | In-process work between claim and finalize (`on_claimed_work` / Phase 2 stages) | **No open DB transaction held** across long work | N/A | Avoids idle-in-transaction locks and statement timeouts; periodically checked via `heartbeat.raise_if_lost()`. |
| **TB-7: Fenced Finalize** | `finalize_processing_attempt` | Single atomic project tx (`FOR UPDATE` key + attempt update + `active_attempt_id = NULL`) | `utda.current_project_id = pid` | Rejects stale/expired workers via `StaleAttemptError`; reconciles `CommitOutcomeUnknownError` by `attempt_id`. |
| **TB-8: Audit Trail** | `append_audit_event` | Short project tx | `utda.current_project_id = pid` | Executed only after non-aborted finalization; reconciles by `event_id`. |
