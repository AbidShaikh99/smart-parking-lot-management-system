from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from parking.models import (
    ParkingSession,
    Payment,
    PaymentStatus,
    SessionStatus,
)
from parking.services.fees import calculate_fee_breakdown


def _exit_result(session, payment, idempotent_replay=False):
    succeeded = payment.status == PaymentStatus.SUCCEEDED
    return {
        "success": succeeded,
        "status_code": 200 if succeeded else 402,
        "message": (
            "Payment successful. Vehicle exit completed."
            if succeeded
            else "Payment failed. The parking session remains open."
        ),
        "data": {
            "ticket_number": session.ticket_number,
            "registration_number": session.vehicle.registration_number,
            "space_id": session.parking_space.space_id,
            "entry_time": session.entry_time.isoformat(),
            "exit_time": (
                session.exit_time.isoformat()
                if session.exit_time
                else None
            ),
            "parking_fee": str(session.parking_fee),
            "discount_amount": str(session.discount_amount),
            "charging_fee": str(session.charging_fee),
            "penalty_amount": str(session.penalty_amount),
            "total_fee": str(payment.amount),
            "payment_id": payment.id,
            "transaction_reference": payment.transaction_reference,
            "payment_status": payment.status,
            "session_status": session.status,
            "fee_breakdown": payment.fee_breakdown,
            # "idempotent_replay": idempotent_replay,
        },
    }


@transaction.atomic
def process_vehicle_exit(
    ticket_number,
    lost_ticket=False,
    payment_success=True,
    idempotency_key=None,
):
    session = (
        ParkingSession.objects
        .select_for_update()
        .select_related("vehicle", "parking_space")
        .filter(ticket_number=ticket_number)
        .first()
    )

    if session is None:
        return {
            "success": False,
            "status_code": 404,
            "message": "Parking ticket not found.",
        }

    if idempotency_key:
        previous_attempt = (
            Payment.objects
            .select_for_update()
            .filter(idempotency_key=idempotency_key)
            .select_related("parking_session")
            .first()
        )
        if previous_attempt is not None:
            if previous_attempt.parking_session_id != session.pk:
                return {
                    "success": False,
                    "status_code": 409,
                    "message": (
                        "This idempotency key was already used "
                        "for a different parking ticket."
                    ),
                }

            effective_lost_ticket = lost_ticket or session.lost_ticket
            if (
                previous_attempt.fee_breakdown.get("lost_ticket")
                != effective_lost_ticket
            ):
                return {
                    "success": False,
                    "status_code": 409,
                    "message": (
                        "This idempotency key was already used "
                        "with different exit details."
                    ),
                }
            return _exit_result(
                session,
                previous_attempt,
                idempotent_replay=True,
            )

    successful_payment = (
        Payment.objects
        .filter(
            parking_session=session,
            status=PaymentStatus.SUCCEEDED,
        )
        .first()
    )
    if successful_payment is not None:
        if (
            lost_ticket or session.lost_ticket
        ) != successful_payment.fee_breakdown.get("lost_ticket"):
            return {
                "success": False,
                "status_code": 409,
                "message": (
                    "This parking session was already paid with "
                    "different exit details."
                ),
            }
        return _exit_result(
            session,
            successful_payment,
            idempotent_replay=True,
        )

    if session.status == SessionStatus.COMPLETED:
        return {
            "success": False,
            "status_code": 409,
            "message": "This parking session has already been completed.",
        }

    if session.status not in (
        SessionStatus.ACTIVE,
        SessionStatus.PAYMENT_PENDING,
    ):
        return {
            "success": False,
            "status_code": 409,
            "message": "This parking session cannot be exited.",
        }

    # Preserve a previously reported lost ticket on payment retry.
    lost_ticket = lost_ticket or session.lost_ticket
    exit_time = timezone.now()

    try:
        fee = calculate_fee_breakdown(
            session=session,
            exit_time=exit_time,
            lost_ticket=lost_ticket,
        )
    except ValueError as exc:
        return {
            "success": False,
            "status_code": 400,
            "message": str(exc),
        }

    amount = Decimal(fee["total_fee"])

    payment = Payment.objects.create(
        parking_session=session,
        idempotency_key=idempotency_key,
        amount=amount,
        fee_breakdown=fee,
        status=(
            PaymentStatus.SUCCEEDED
            if payment_success
            else PaymentStatus.FAILED
        ),
    )

    # Use the database-generated payment ID, not a UUID.
    payment.transaction_reference = f"PAY-{payment.id:06d}"
    payment.save(update_fields=["transaction_reference"])

    # Remember the latest fee breakdown and lost-ticket status.
    session.parking_fee = Decimal(fee["parking_fee"])
    session.charging_fee = Decimal(fee["charging_fee"])
    session.discount_amount = Decimal(fee["discount_amount"])
    session.penalty_amount = Decimal(fee["penalty_amount"])
    session.total_fee = amount
    session.lost_ticket = lost_ticket

    if not payment_success:
        session.status = SessionStatus.PAYMENT_PENDING
        session.save(update_fields=[
            "parking_fee",
            "charging_fee",
            "discount_amount",
            "penalty_amount",
            "total_fee",
            "lost_ticket",
            "status",
        ])

        return _exit_result(session, payment)

    # Only successful payment completes the session.
    session.exit_time = exit_time
    session.status = SessionStatus.COMPLETED
    session.save(update_fields=[
        "parking_fee",
        "charging_fee",
        "discount_amount",
        "penalty_amount",
        "total_fee",
        "lost_ticket",
        "exit_time",
        "status",
    ])

    return _exit_result(session, payment)
