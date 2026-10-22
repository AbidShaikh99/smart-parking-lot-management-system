
from django.db import transaction
from django.utils import timezone

from parking.models import (
    ParkingSession,
    Reservation,
    ReservationStatus,
    SessionStatus,
    ParkingSpace
)
from parking.services.allocation import eligible_spaces


@transaction.atomic
def create_reservation(
    vehicle,
    start_time,
    end_time,
    requires_charging=False,
):
    candidates = eligible_spaces(
        vehicle.vehicle_type,
        requires_charging=requires_charging,
    ).order_by("size_capacity", "floor_number", "space_id")

    for candidate in candidates:
        # Lock this space while checking and creating the reservation.
        space = (
            ParkingSpace.objects
            .select_for_update()
            .filter(pk=candidate.pk, status="AVAILABLE")
            .first()
        )

        if space is None:
            continue

        # Do not reserve a space currently occupied by a vehicle.
        occupied = ParkingSession.objects.filter(
            parking_space=space,
            status__in=[
                SessionStatus.ACTIVE,
                SessionStatus.PAYMENT_PENDING,
            ],
        ).exists()

        if occupied:
            continue

        # Half-open intervals: a reservation ending at 11:00
        # does not conflict with another starting at 11:00.
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
        )

    return None
