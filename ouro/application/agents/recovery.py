from __future__ import annotations

from collections.abc import Iterable

from ouro.application.contracts.base import Diagnostic
from ouro.core.agents import DelegationRecord
from ouro.core.errors import PersistenceError
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import SessionStore


class AgentRecoveryService:
    """Reconcile durable subagent identity without replaying child execution."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def reconcile(self) -> tuple[Diagnostic, ...]:
        """Run an explicit full-store maintenance reconciliation."""

        sessions = {session.session_id: session for session in self.store.list_sessions()}
        relations, diagnostics = self._load_relations(sessions.values())
        diagnostics.extend(self._audit_child_backlinks(sessions.values(), relations))
        for relation in relations:
            diagnostics.extend(self._reconcile_relation(relation, sessions))
        return tuple(diagnostics)

    def reconcile_root(
        self,
        root_session_id: str,
        *,
        target_session_id: str | None = None,
    ) -> tuple[Diagnostic, ...]:
        """Reconcile one bounded Agent aggregate without scanning other Sessions."""

        root = self.store.load_session_record(root_session_id)
        if root.session_kind != "primary":
            raise ValueError("Agent recovery root must be a primary Session")

        try:
            relations = self.store.list_delegations(root_session_id)
        except PersistenceError as exc:
            return (self._root_runtime_diagnostic(root_session_id, exc),)

        sessions = {root.session_id: root}
        diagnostics: list[Diagnostic] = []
        unavailable_children: set[str] = set()
        child_ids = {
            relation.child_session_id
            for relation in relations
            if relation.child_session_id is not None
        }
        if target_session_id is not None:
            child_ids.add(target_session_id)
        child_ids.discard(root_session_id)
        for child_id in sorted(child_ids):
            try:
                sessions[child_id] = self.store.load_session_record(child_id)
            except FileNotFoundError:
                continue
            except PersistenceError as exc:
                unavailable_children.add(child_id)
                diagnostics.append(
                    Diagnostic(
                        code="session_runtime_incompatible",
                        severity="warning",
                        component="application.agent_recovery",
                        message=(
                            "Agent relation recovery skipped incompatible persisted Session "
                            f"{child_id}: {exc}"
                        ),
                        session_id=child_id,
                        recoverable=True,
                        suggested_action=(
                            "Inspect the retained Session data before an explicit retry."
                        ),
                        metadata={
                            "root_session_id": root_session_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                )

        diagnostics.extend(self._audit_child_backlinks(sessions.values(), relations))
        for relation in relations:
            if relation.child_session_id in unavailable_children:
                continue
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
        except FileNotFoundError, PersistenceError:
            return False
        return (
            self._parent_mismatch(relation, parent_run) is None
            and self._child_mismatch_code(relation, child) is None
            and self._run_mismatch_code(relation, run) is None
        )

    def load_run_delegation(
        self, session: SessionRecord, run: RunRecord
    ) -> DelegationRecord | None:
        """Point-read and validate the Run's frozen authority, without reconciliation."""
        if session.session_id != run.session_id:
            raise PersistenceError("Run does not belong to its Session")
        if session.session_kind == "primary":
            if run.metadata.get("delegation_id") is not None:
                raise PersistenceError("Primary Run cannot use a child Delegation")
            return None
        if session.root_session_id is None or session.delegation_id is None:
            raise PersistenceError("Subagent Session has no Delegation identity")
        try:
            relation = self.store.load_delegation(session.root_session_id, session.delegation_id)
        except FileNotFoundError as exc:
            raise PersistenceError("Run's frozen Delegation is unavailable") from exc
        mismatch = self._child_mismatch_code(relation, session)
        if mismatch is None and run.kind == "agent":
            mismatch = self._run_mismatch_code(relation, run)
        if mismatch is not None:
            raise PersistenceError(f"Run's frozen Delegation is inconsistent: {mismatch}")
        return relation

    def bind_current_child(
        self, relation: DelegationRecord, child: SessionRecord
    ) -> DelegationRecord:
        """Finish the root reference to a committed child successor, without executing it."""
        with self.store.session_mutation(relation.root_session_id):
            relation = self.store.load_delegation(relation.root_session_id, relation.delegation_id)
            child = self.store.load_session_record(child.session_id)
            if (
                relation.current_child_run_id is None
                or child.last_run_id is None
                or child.last_run_id == relation.current_child_run_id
            ):
                return relation
            run = self.store.load_run(child.session_id, child.last_run_id)
            if run.metadata.get("previous_child_run_id") != relation.current_child_run_id:
                return relation
            if (
                self._child_identity_mismatch(relation, child)
                or self._run_mismatch_code(relation, run) is not None
                or run.history_epoch != child.history_epoch
            ):
                raise PersistenceError("committed child successor does not match its Delegation")
            input_id = run.metadata.get("follow_up_input_id")
            if input_id is not None:
                accepted = self.store.load_input(child.session_id, str(input_id))
                if (
                    run.trigger != "subagent_follow_up"
                    or accepted is None
                    or accepted.item.prospective_run_id != run.run_id
                    or accepted.item.source != "host"
                    or accepted.item.mode != "follow_up"
                    or accepted.item.semantic_tag != "subagent-message"
                    or accepted.item.history_epoch != run.history_epoch
                ):
                    raise PersistenceError("committed child successor has no matching input")
            elif run.trigger != "continue":
                raise PersistenceError("child successor requires an input or history continuation")
            return self.store.save_delegation(
                relation.model_copy(
                    update={
                        "current_child_run_id": run.run_id,
                        "result": None,
                        "control_state": "open",
                    }
                ),
                expected_revision=relation.revision,
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
        parent_mismatch = self._parent_mismatch(relation, parent_run)
        if parent_mismatch is not None:
            self._fail_relation(relation, "delegation_parent_missing")
            return [
                self._diagnostic(
                    relation,
                    code="delegation_parent_missing",
                    message=parent_mismatch,
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
            relation = self.bind_current_child(relation, child)
        except PersistenceError as exc:
            return [
                self._incompatible_runtime_diagnostic(relation, exc, session_id=child.session_id)
            ]

        try:
            runs = (
                [self.store.load_run(child.session_id, relation.current_child_run_id)]
                if relation.current_child_run_id is not None
                else self.store.load_runs(child.session_id)
            )
        except FileNotFoundError:
            runs = []
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
    def _parent_mismatch(
        relation: DelegationRecord,
        parent_run: RunRecord | None,
    ) -> str | None:
        if parent_run is None or parent_run.session_id != relation.parent_session_id:
            return "Delegation parent Run is unavailable."
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
                f"Agent relation recovery skipped incompatible persisted execution data: {error}"
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
            session.agent_root_session_id
            for session in sessions
            if session.session_kind == "primary"
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

    @staticmethod
    def _root_runtime_diagnostic(
        root_session_id: str,
        error: PersistenceError,
    ) -> Diagnostic:
        return Diagnostic(
            code="session_runtime_incompatible",
            severity="warning",
            component="application.agent_recovery",
            message=(
                "Agent relation recovery skipped incompatible persisted data "
                f"for root Session {root_session_id}: {error}"
            ),
            session_id=root_session_id,
            recoverable=True,
            suggested_action="Inspect the retained root Session data before an explicit retry.",
            metadata={"error_type": type(error).__name__},
        )


__all__ = ["AgentRecoveryService"]
