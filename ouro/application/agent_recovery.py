from __future__ import annotations

from collections.abc import Iterable

from ouro.application.dto import Diagnostic
from ouro.core.agents import DelegationRecord
from ouro.core.errors import PersistenceError
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import SessionStore


class AgentRecoveryService:
    """Reconcile durable subagent identity without replaying child execution."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def reconcile(self) -> tuple[Diagnostic, ...]:
        sessions = {session.session_id: session for session in self.store.list_sessions()}
        relations, diagnostics = self._load_relations(sessions.values())
        diagnostics.extend(self._audit_child_backlinks(sessions.values(), relations))
        for relation in relations:
            diagnostics.extend(self._reconcile_relation(relation, sessions))
        return tuple(diagnostics)

    def relation_is_consistent(self, relation: DelegationRecord) -> bool:
        child_id = relation.child_session_id
        run_id = relation.current_child_run_id
        if child_id is None or run_id is None:
            return False
        try:
            child = self.store.load_session_record(child_id)
            run = self.store.load_run(child_id, run_id)
            parent_run = self.store.load_run(
                relation.parent_session_id,
                relation.parent_run_id,
            )
        except (FileNotFoundError, PersistenceError):
            return False
        return (
            self._catalog_mismatch(relation, parent_run) is None
            and self._child_mismatch_code(relation, child) is None
            and self._run_mismatch_code(relation, run) is None
        )

    def _reconcile_relation(
        self,
        relation: DelegationRecord,
        sessions: dict[str, SessionRecord],
    ) -> list[Diagnostic]:
        try:
            parent_run = self.store.load_run(
                relation.parent_session_id,
                relation.parent_run_id,
            )
        except FileNotFoundError:
            parent_run = None
        except PersistenceError as exc:
            return [self._incompatible_runtime_diagnostic(relation, exc)]
        catalog_mismatch = self._catalog_mismatch(relation, parent_run)
        if catalog_mismatch is not None:
            self._fail_relation(relation, "agent_catalog_contract_unavailable")
            return [
                self._diagnostic(
                    relation,
                    code="agent_catalog_contract_unavailable",
                    message=catalog_mismatch,
                    run_id=relation.parent_run_id,
                )
            ]

        child_id = relation.child_session_id
        if child_id is None or child_id not in sessions:
            self._fail_relation(relation, "delegation_child_missing")
            return [
                self._diagnostic(
                    relation,
                    code="delegation_child_missing",
                    message="Delegation has no canonical child Session.",
                )
            ]

        child = sessions[child_id]
        child_mismatch = self._child_mismatch_code(relation, child)
        if child_mismatch is not None:
            self._fail_relation(relation, child_mismatch)
            return [
                self._child_mismatch_diagnostic(
                    relation,
                    child.session_id,
                    code=child_mismatch,
                )
            ]

        try:
            runs = self.store.load_runs(child.session_id)
        except PersistenceError as exc:
            return [
                self._incompatible_runtime_diagnostic(
                    relation,
                    exc,
                    session_id=child.session_id,
                )
            ]
        if relation.current_child_run_id is None:
            candidates = [run for run in runs if self._run_mismatch_code(relation, run) is None]
            if len(candidates) != 1:
                code = self._unattributed_run_code(relation, runs, candidates)
                self._fail_relation(relation, code)
                return [
                    self._diagnostic(
                        relation,
                        code=code,
                        message=(
                            "Delegation has no uniquely attributable child Run."
                            if runs
                            else "Delegation child Session has no child Run."
                        ),
                        session_id=child.session_id,
                    )
                ]
            self._commit_child_run(relation, candidates[0])
            return []

        current = next(
            (run for run in runs if run.run_id == relation.current_child_run_id),
            None,
        )
        if current is None:
            self._fail_relation(relation, "delegation_run_missing")
            return [
                self._diagnostic(
                    relation,
                    code="delegation_run_missing",
                    message="Delegation references a missing child Run.",
                    session_id=child.session_id,
                    run_id=relation.current_child_run_id,
                )
            ]
        run_mismatch = self._run_mismatch_code(relation, current)
        if run_mismatch is not None:
            self._fail_relation(relation, run_mismatch)
            return [
                self._diagnostic(
                    relation,
                    code=run_mismatch,
                    message=(
                        "Child Run frozen Agent contract does not match its Delegation."
                        if run_mismatch == "child_contract_unavailable"
                        else "Child Run provenance does not match its Delegation."
                    ),
                    session_id=child.session_id,
                    run_id=current.run_id,
                )
            ]
        if relation.spawn_state in {"requested", "child_created"}:
            self.store.save_delegation(
                relation.model_copy(update={"spawn_state": "submitted"}),
                expected_revision=relation.revision,
            )
        return []

    def _audit_child_backlinks(
        self,
        sessions: Iterable[SessionRecord],
        relations: Iterable[DelegationRecord],
    ) -> list[Diagnostic]:
        by_identity = {
            (relation.root_session_id, relation.delegation_id): relation for relation in relations
        }
        diagnostics: list[Diagnostic] = []
        for child in sorted(sessions, key=lambda item: item.session_id):
            if child.session_kind != "subagent":
                continue
            assert child.delegation_id is not None
            assert child.root_session_id is not None
            relation = by_identity.get((child.root_session_id, child.delegation_id))
            if (
                relation is None
                or relation.child_session_id != child.session_id
                or self._child_identity_mismatch(relation, child)
            ):
                diagnostics.append(
                    Diagnostic(
                        code="child_delegation_mismatch",
                        severity="error",
                        component="application.agent_recovery",
                        message="Subagent Session has no matching canonical Delegation.",
                        session_id=child.session_id,
                        resource_id=child.delegation_id,
                        recoverable=True,
                        suggested_action=(
                            "Inspect the root Session relation records; retained history was not "
                            "deleted or replayed."
                        ),
                        metadata={"root_session_id": child.root_session_id},
                    )
                )
        return diagnostics

    def _commit_child_run(self, relation: DelegationRecord, run: RunRecord) -> None:
        if relation.spawn_state == "failed":
            return
        self.store.save_delegation(
            relation.model_copy(
                update={
                    "current_child_run_id": run.run_id,
                    "spawn_state": "submitted",
                }
            ),
            expected_revision=relation.revision,
        )

    def _fail_relation(self, relation: DelegationRecord, code: str) -> None:
        if relation.spawn_state == "failed" and relation.error is not None:
            return
        self.store.save_delegation(
            relation.model_copy(
                update={
                    "spawn_state": "failed",
                    "error": relation.error or code,
                }
            ),
            expected_revision=relation.revision,
        )

    @classmethod
    def _child_mismatch_code(
        cls,
        relation: DelegationRecord,
        child: SessionRecord,
    ) -> str | None:
        if cls._child_identity_mismatch(relation, child):
            return "child_delegation_mismatch"
        if (
            child.approval_mode != relation.approval_mode
            or child.interaction_mode != relation.interaction_mode
            or child.workspace != relation.constraints.workspace
        ):
            return "child_policy_invalid"
        return None

    @staticmethod
    def _catalog_mismatch(
        relation: DelegationRecord,
        parent_run: RunRecord | None,
    ) -> str | None:
        if parent_run is None or parent_run.agent_catalog is None:
            return "Delegation parent Run has no frozen Agent catalog."
        entry = next(
            (
                item
                for item in parent_run.agent_catalog.entries
                if item.definition.agent_id == relation.agent_id
            ),
            None,
        )
        if entry is None:
            return "Delegation Agent is absent from its parent Run catalog."
        if (
            entry.definition.contract_hash != relation.definition.contract_hash
            or entry.effective_tools != relation.effective_tools
        ):
            return "Delegation contract does not match its parent Run catalog."
        return None

    @staticmethod
    def _child_identity_mismatch(
        relation: DelegationRecord,
        child: SessionRecord,
    ) -> bool:
        return not (
            child.session_kind == "subagent"
            and child.session_id == relation.child_session_id
            and child.root_session_id == relation.root_session_id
            and child.parent_session_id == relation.parent_session_id
            and child.delegation_id == relation.delegation_id
        )

    @staticmethod
    def _run_mismatch_code(
        relation: DelegationRecord,
        run: RunRecord,
    ) -> str | None:
        identity_matches = (
            run.session_id == relation.child_session_id
            and run.parent_run_id == relation.parent_run_id
            and run.initiator_id == f"ouro.subagents/{relation.delegation_id}"
            and run.metadata.get("delegation_id") == relation.delegation_id
            and run.metadata.get("root_session_id") == relation.root_session_id
            and run.metadata.get("agent_id") == relation.agent_id
        )
        if not identity_matches:
            return "child_delegation_mismatch"
        if run.metadata.get("agent_definition_contract_hash") != relation.definition.contract_hash:
            return "child_contract_unavailable"
        return None

    @classmethod
    def _unattributed_run_code(
        cls,
        relation: DelegationRecord,
        runs: list[RunRecord],
        candidates: list[RunRecord],
    ) -> str:
        if not runs:
            return "delegation_run_missing"
        if len(candidates) > 1:
            return "child_delegation_mismatch"
        if any(
            cls._run_mismatch_code(relation, run) == "child_contract_unavailable" for run in runs
        ):
            return "child_contract_unavailable"
        return "child_delegation_mismatch"

    def _child_mismatch_diagnostic(
        self,
        relation: DelegationRecord,
        child_session_id: str,
        *,
        code: str,
    ) -> Diagnostic:
        return self._diagnostic(
            relation,
            code=code,
            message=(
                "Child Session frozen policy does not match its Delegation."
                if code == "child_policy_invalid"
                else "Child Session identity does not match its Delegation."
            ),
            session_id=child_session_id,
        )

    @staticmethod
    def _diagnostic(
        relation: DelegationRecord,
        *,
        code: str,
        message: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> Diagnostic:
        return Diagnostic(
            code=code,
            severity="error",
            component="application.agent_recovery",
            message=message,
            session_id=session_id or relation.root_session_id,
            run_id=run_id,
            resource_id=relation.delegation_id,
            recoverable=True,
            suggested_action=(
                "Inspect the retained Session and Delegation records before an explicit retry."
            ),
            metadata={
                "root_session_id": relation.root_session_id,
                "child_session_id": relation.child_session_id,
            },
        )

    @staticmethod
    def _incompatible_runtime_diagnostic(
        relation: DelegationRecord,
        error: PersistenceError,
        *,
        session_id: str | None = None,
    ) -> Diagnostic:
        return Diagnostic(
            code="session_runtime_incompatible",
            severity="warning",
            component="application.agent_recovery",
            message=(
                "Agent relation recovery skipped incompatible persisted execution data: "
                f"{error}"
            ),
            session_id=session_id or relation.parent_session_id,
            resource_id=relation.delegation_id,
            recoverable=True,
            suggested_action=(
                "Inspect the retained data or remove the incompatible root Session explicitly."
            ),
            metadata={
                "root_session_id": relation.root_session_id,
                "child_session_id": relation.child_session_id,
                "error_type": type(error).__name__,
            },
        )

    def _load_relations(
        self,
        sessions: Iterable[SessionRecord],
    ) -> tuple[list[DelegationRecord], list[Diagnostic]]:
        relations: list[DelegationRecord] = []
        diagnostics: list[Diagnostic] = []
        root_ids = sorted(
            session.session_id
            for session in sessions
            if session.session_kind == "primary" and session.root_session_id == session.session_id
        )
        for root_id in root_ids:
            try:
                relations.extend(self.store.list_delegations(root_id))
            except PersistenceError as exc:
                diagnostics.append(
                    Diagnostic(
                        code="session_runtime_incompatible",
                        severity="warning",
                        component="application.agent_recovery",
                        message=(
                            "Agent relation recovery skipped incompatible persisted data "
                            f"for root Session {root_id}: {exc}"
                        ),
                        session_id=root_id,
                        recoverable=True,
                        suggested_action=(
                            "Inspect the retained data or remove the incompatible root Session."
                        ),
                        metadata={"error_type": type(exc).__name__},
                    )
                )
        return relations, diagnostics


__all__ = ["AgentRecoveryService"]
