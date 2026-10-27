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
from parking.services.allocation import (
    get_available_spaces,
    is_space_reserved_for_another_vehicle,
    is_space_occupied,
)


@transaction.atomic
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

    candidates = get_available_spaces(
        vehicle_type=vehicle.vehicle_type,
        requires_charging=requires_charging,
        start_time=start_time,
        end_time=end_time,
    )

    for candidate in candidates:
        space = (
            ParkingSpace.objects
            .select_for_update()
            .filter(
                pk=candidate.pk,
                status=SpaceStatus.AVAILABLE,
            )
            .first()
        )

        if space is None:
            continue

        # Availability can change between listing candidates and locking one.
        if is_space_occupied(space):
            continue

        if is_space_reserved_for_another_vehicle(
            space=space,
            start_time=start_time,
            end_time=end_time,
        ):
            continue

        return Reservation.objects.create(
            vehicle=vehicle,
            parking_space=space,
            start_time=start_time,
            end_time=end_time,
            grace_period_minutes=getattr(
                settings,
                "PARKING_RESERVATION_GRACE_MINUTES",
                15,
            ),
            status=ReservationStatus.CONFIRMED,
        )

    return None

@transaction.atomic
def validate_reservation_arrival(reservation_id, vehicle):
    """
    Validate a reservation and its arrival window.

    Returns:
        (reservation, None) when arrival is permitted.
        (None, error_message) when arrival is rejected.
    """

    reservation = (
        Reservation.objects
        .select_for_update()
        .select_related("parking_space", "vehicle")
        .filter(pk=reservation_id)
        .first()
    )

    if reservation is None:
        return None, "Reservation not found."

    if reservation.vehicle_id != vehicle.id:
        return None, (
            "This reservation does not belong to this vehicle."
        )

    if reservation.status == ReservationStatus.EXPIRED:
        return None, "This reservation has expired."

    if reservation.status != ReservationStatus.CONFIRMED:
        return None, (
            f"Reservation cannot be used. "
            f"Current status: {reservation.status}."
        )

    now = timezone.now()

    # The early-arrival window is configurable independently of grace.
    early_minutes = getattr(
        settings,
        "PARKING_RESERVATION_EARLY_ARRIVAL_MINUTES",
        15,
    )

    earliest_arrival = (
        reservation.start_time
        - timedelta(minutes=early_minutes)
    )

    deadline = (
        reservation.start_time
        + timedelta(
            minutes=reservation.grace_period_minutes
        )
    )

    if now < earliest_arrival:
        return None, (
            "You have arrived too early for this reservation."
        )

    if now > deadline:
        reservation.status = ReservationStatus.EXPIRED
        reservation.save(update_fields=["status"])

        return None, (
            "Reservation expired because the arrival grace "
            "period has ended. Please make a new reservation "
            "or request a walk-in space."
        )

    # Ensure the reserved space is still operational.
    space = (
        reservation.parking_space.__class__.objects
        .select_for_update()
        .get(pk=reservation.parking_space_id)
    )

    if space.status != SpaceStatus.AVAILABLE:
        return None, (
            "The reserved parking space is not operational."
        )

    # Do not create a session if this space is already occupied.
    if ParkingSession.objects.filter(
        parking_space=space,
        status__in=[
            SessionStatus.ACTIVE,
            SessionStatus.PAYMENT_PENDING,
        ],
    ).exists():
        return None, (
            "The reserved parking space is already occupied."
        )

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
