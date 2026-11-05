"""The signing service.

It signs a request only when all of these hold:

1. the action is on the phase whitelist (phase 1: place and cancel orders only; never
   withdraw, transfer, approve agents or change leverage);
2. a risk-gate approval is attached, its MAC verifies, and it has not expired;
3. the approval matches the request exactly: intent id, action, intent hash, client id;
4. the approval has not been used before (one approval, one signature);
5. a backend exists for the venue.

Every decision, signed or refused, is written to the audit log.
"""

from __future__ import annotations

from collections.abc import Mapping

from humpback.audit.log import AuditLog
from humpback.core.clock import Clock
from humpback.core.errors import SignerRefused
from humpback.domain.models import Venue
from humpback.risk.gate import Approval, verify_approval
from humpback.signer.backends import SigningBackend
from humpback.signer.nonces import NonceAllocator
from humpback.venues.base import Action, SignedRequest, UnsignedRequest

PHASE_1_ACTIONS = frozenset({Action.PLACE_ORDER, Action.CANCEL_ORDER})


class SignerService:
    def __init__(
        self,
        *,
        approval_key: bytes,
        clock: Clock,
        nonces: NonceAllocator,
        backends: Mapping[Venue, SigningBackend],
        nonce_units: Mapping[Venue, str],
        audit: AuditLog,
        allowed_actions: frozenset[Action] = PHASE_1_ACTIONS,
    ) -> None:
        forbidden = allowed_actions & {Action.WITHDRAW, Action.TRANSFER, Action.APPROVE_AGENT}
        if forbidden:
            raise ValueError(f"signer may never whitelist {sorted(forbidden)}")
        self._key = approval_key
        self._clock = clock
        self._nonces = nonces
        self._backends = dict(backends)
        self._units = dict(nonce_units)
        self._audit = audit
        self._allowed = allowed_actions
        self._used: set[str] = set()

    def sign(self, request: UnsignedRequest, approval: Approval, *, trace_id: str) -> SignedRequest:
        try:
            signed = self._sign(request, approval)
        except SignerRefused as exc:
            self._audit.record(
                "signer_refused",
                trace_id,
                code=exc.code,
                reason=str(exc),
                venue=request.venue.value,
                action=request.action.value,
                intent_id=request.intent_id,
            )
            raise
        self._audit.record(
            "signer_signed",
            trace_id,
            venue=request.venue.value,
            action=request.action.value,
            intent_id=request.intent_id,
            client_id=request.client_id,
            nonce=signed.nonce,
            signer=signed.signer_address,
        )
        return signed

    def _sign(self, request: UnsignedRequest, approval: Approval) -> SignedRequest:
        if request.action not in self._allowed:
            raise SignerRefused(
                f"action {request.action} is not whitelisted", code="ACTION_NOT_WHITELISTED"
            )
        verify_approval(self._key, approval, now_ms=self._clock.now_ms())
        mismatches = [
            name
            for name, ours, theirs in (
                ("intent_id", request.intent_id, approval.intent_id),
                ("action", request.action, approval.action),
                ("intent_hash", request.intent_hash, approval.intent_hash),
                ("client_id", request.client_id, approval.client_id),
            )
            if ours != theirs
        ]
        if mismatches:
            raise SignerRefused(
                f"approval does not match request: {mismatches}", code="APPROVAL_MISMATCH"
            )
        if approval.mac in self._used:
            raise SignerRefused("approval already used", code="APPROVAL_REPLAY")
        backend = self._backends.get(request.venue)
        if backend is None:
            raise SignerRefused(f"no signing backend for {request.venue}", code="NO_BACKEND")
        unit = self._units.get(request.venue, "ms")
        nonce = self._nonces.next(backend.address, "us" if unit == "us" else "ms")
        signature, extra = backend.sign(request, nonce)
        self._used.add(approval.mac)
        return SignedRequest(request, backend.address, nonce, signature, extra)
