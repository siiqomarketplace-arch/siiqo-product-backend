"""
event_logger.py — Siiqo 2.0 Lean Observability & Event System
Provides non-blocking, asynchronous logging for platform events
and verifiable trust evidence.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from flask import current_app

logger = logging.getLogger(__name__)

# Lightweight thread pool for fire-and-forget database writes
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="siiqo_event_worker")


def _record_event_async(app, event_data):
    """Worker task to write platform event within Flask app context."""
    try:
        with app.app_context():
            from app.extensions import db
            from app.models.telemetry import PlatformEvent

            event = PlatformEvent(
                event_name=event_data.get("event_name"),
                session_id=event_data.get("session_id"),
                user_id=event_data.get("user_id"),
                business_id=event_data.get("business_id"),
                storefront_id=event_data.get("storefront_id"),
                product_id=event_data.get("product_id"),
                order_id=event_data.get("order_id"),
                source=event_data.get("source"),
                properties=event_data.get("properties") or {},
                timestamp=event_data.get("timestamp") or datetime.now(timezone.utc),
            )
            db.session.add(event)
            db.session.commit()
    except Exception as e:
        logger.error(f"[EVENT_LOGGER] Failed to write platform event: {e}")
        try:
            with app.app_context():
                from app.extensions import db
                db.session.rollback()
        except Exception:
            pass


def _record_trust_evidence_async(app, evidence_data):
    """Worker task to write trust evidence within Flask app context."""
    try:
        with app.app_context():
            from app.extensions import db
            from app.models.telemetry import TrustEvidence

            evidence = TrustEvidence(
                business_id=evidence_data.get("business_id"),
                evidence_type=evidence_data.get("evidence_type"),
                provenance=evidence_data.get("provenance", "business_claimed"),
                source=evidence_data.get("source"),
                source_id=str(evidence_data.get("source_id")) if evidence_data.get("source_id") is not None else None,
                status=evidence_data.get("status", "ACTIVE"),
                confidence=evidence_data.get("confidence", 1.00),
                properties=evidence_data.get("properties") or {},
                verified_at=evidence_data.get("verified_at"),
                created_at=datetime.now(timezone.utc),
            )
            db.session.add(evidence)
            db.session.commit()
    except Exception as e:
        logger.error(f"[TRUST_LOGGER] Failed to write trust evidence: {e}")
        try:
            with app.app_context():
                from app.extensions import db
                db.session.rollback()
        except Exception:
            pass


def log_platform_event(
    event_name: str,
    session_id: str = None,
    user_id: int = None,
    business_id: int = None,
    storefront_id: int = None,
    product_id: int = None,
    order_id: int = None,
    source: str = None,
    properties: dict = None,
):
    """
    Non-blocking public API to record a platform event.
    Dispatches to background thread pool instantly.
    """
    try:
        app = current_app._get_current_object()
        data = {
            "event_name": event_name,
            "session_id": session_id,
            "user_id": user_id,
            "business_id": business_id,
            "storefront_id": storefront_id,
            "product_id": product_id,
            "order_id": order_id,
            "source": source,
            "properties": properties or {},
            "timestamp": datetime.now(timezone.utc),
        }
        _executor.submit(_record_event_async, app, data)
    except Exception as e:
        logger.error(f"[EVENT_LOGGER] Failed to submit async event {event_name}: {e}")


def log_trust_evidence(
    business_id: int,
    evidence_type: str,
    source: str,
    source_id: str = None,
    provenance: str = "siiqo_transaction_verified",
    status: str = "ACTIVE",
    confidence: float = 1.00,
    properties: dict = None,
    verified_at: datetime = None,
):
    """
    Non-blocking public API to append verifiable commercial trust evidence.
    """
    try:
        app = current_app._get_current_object()
        data = {
            "business_id": business_id,
            "evidence_type": evidence_type,
            "provenance": provenance,
            "source": source,
            "source_id": source_id,
            "status": status,
            "confidence": confidence,
            "properties": properties or {},
            "verified_at": verified_at or datetime.now(timezone.utc),
        }
        _executor.submit(_record_trust_evidence_async, app, data)
    except Exception as e:
        logger.error(f"[TRUST_LOGGER] Failed to submit trust evidence for business {business_id}: {e}")
