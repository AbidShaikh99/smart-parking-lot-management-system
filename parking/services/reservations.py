from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from parking.models import (
    ParkingSession,
    Reservation,
    ReservationStatus,
    SessionStatus,
    ParkingSpace,
    SpaceStatus,
)
from parking.services.allocation import eligible_spaces


def create_reservation(
    vehicle,
    start_time,
    end_time,
    requires_charging=False,
):
    if end_time <= start_time:
        raise ValueError("End time must be after start time.")

    existing_reservation = Reservation.objects.filter(
        vehicle=vehicle,
        start_time=start_time,
        end_time=end_time,
        status=ReservationStatus.CONFIRMED,
    ).first()

    if existing_reservation:
        return existing_reservation

    candidates = eligible_spaces(
        vehicle.vehicle_type,
        requires_charging=requires_charging,
    ).order_by(
        "size_capacity",
        "floor_number",
        "space_id",
    )

    for candidate_id in candidates.values_list("pk", flat=True):
        space = (
            ParkingSpace.objects
            .select_for_update()
            .filter(
                pk=candidate_id,
                status=SpaceStatus.AVAILABLE,
            )
            .first()
        )

        if space is None:
            continue

        occupied = ParkingSession.objects.filter(
            parking_space=space,
            status__in=[
                SessionStatus.ACTIVE,
                SessionStatus.PAYMENT_PENDING,
            ],
        ).exists()

        if occupied:
            continue

        overlapping = Reservation.objects.filter(
            parking_space=space,
            status=ReservationStatus.CONFIRMED,
            start_time__lt=end_time,
            end_time__gt=start_time,
        ).exists()

        if overlapping:
            continue

        return Reservation.objects.create(
            vehicle=vehicle,
            parking_space=space,
            start_time=start_time,
            end_time=end_time,
            status=ReservationStatus.CONFIRMED,
        )

    return None


@transaction.atomic
def validate_reservation_arrival(
    reservation_id,
    vehicle,
    now=None,
):
    """
    Validate whether this vehicle can check in using its reservation.

    Returns:
        (reservation, None) when check-in is permitted.
        (None, error_message) when check-in is not permitted.
    """
    now = now or timezone.now()

    reservation = (
        Reservation.objects
        .select_for_update()
        .filter(pk=reservation_id)
        .first()
    )

    if reservation is None:
        return None, "Reservation not found."

    if reservation.vehicle_id != vehicle.pk:
        return None, "This reservation belongs to another vehicle."

    if reservation.status != ReservationStatus.CONFIRMED:
        return None, (
            f"This reservation cannot be used because its status "
            f"is {reservation.status}."
        )

    early_minutes = getattr(
        settings,
        "RESERVATION_EARLY_ARRIVAL_MINUTES",
        15,
    )

    earliest_arrival = reservation.start_time - timedelta(
        minutes=early_minutes
    )

    latest_arrival = reservation.start_time + timedelta(
        minutes=reservation.grace_period_minutes
    )

    if now < earliest_arrival:
        return None, "Vehicle arrived too early for this reservation."

    if now > latest_arrival:
        reservation.status = ReservationStatus.EXPIRED
        reservation.save(update_fields=["status"])
        return None, "The reservation has expired because the vehicle arrived late."

    space = (
        ParkingSpace.objects
        .select_for_update()
        .filter(pk=reservation.parking_space_id)
        .first()
    )

    if space is None or space.status != SpaceStatus.AVAILABLE:
        return None, "The reserved parking space is not operational."

    occupied = ParkingSession.objects.filter(
        parking_space=space,
        status__in=[
            SessionStatus.ACTIVE,
            SessionStatus.PAYMENT_PENDING,
        ],
    ).exists()

    if occupied:
        return None, "The reserved parking space is currently occupied."

    return reservation, None


@transaction.atomic
def expire_missed_reservations(now=None):
    """
    Expire confirmed reservations whose grace period has passed.

    Repeated runs are safe: only CONFIRMED reservations are considered.
    """
    now = now or timezone.now()
    expired_count = 0

    reservations = (
        Reservation.objects
        .select_for_update()
        .filter(
            status=ReservationStatus.CONFIRMED,
            start_time__lte=now,
        )
        .order_by("pk")
    )

    for reservation in reservations:
        expiry_time = reservation.start_time + timedelta(
            minutes=reservation.grace_period_minutes
        )

        if now > expiry_time:
            reservation.status = ReservationStatus.EXPIRED
            reservation.save(update_fields=["status"])
            expired_count += 1

    return expired_count
