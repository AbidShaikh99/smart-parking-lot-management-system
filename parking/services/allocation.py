from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from parking.models import (
    ParkingSession,
    ParkingSpace,
    Reservation,
    ReservationStatus,
    SessionStatus,
    SpaceStatus,
    VehicleType,
)



def eligible_spaces(vehicle_type=None, requires_charging=False):
    """
    Return operational parking spaces.

    If vehicle_type is provided, filter by vehicle size.
    If vehicle_type is None, return all operational spaces
    without applying a vehicle-size filter.
    """
    spaces = ParkingSpace.objects.filter(
        status=SpaceStatus.AVAILABLE
    )

    # Apply charging filter only when requested.
    if requires_charging:
        spaces = spaces.filter(has_charger=True)

    # No vehicle type means no vehicle-size restriction.
    if vehicle_type is None:
        return spaces.order_by(
            "floor_number",
            "space_id",
        )

    if vehicle_type == VehicleType.MOTORCYCLE:
        allow_larger = getattr(
            settings,
            "PARKING_MOTORCYCLES_CAN_USE_LARGER_SPACES",
            True,
        )

        if allow_larger:
            spaces = spaces.filter(size_capacity__gte=1)
        else:
            spaces = spaces.filter(size_capacity=1)

    elif vehicle_type == VehicleType.CAR:
        spaces = spaces.filter(size_capacity__gte=2)

    elif vehicle_type == VehicleType.SUV:
        spaces = spaces.filter(size_capacity__gte=3)

    elif vehicle_type == VehicleType.ELECTRIC:
        spaces = spaces.filter(size_capacity__gte=2)

    else:
        return spaces.none()

    return spaces.order_by(
        "size_capacity",
        "floor_number",
        "space_id",
    )



def is_space_occupied(space):
    """
    Check whether a space has an open parking session.
    """
    return ParkingSession.objects.filter(
        parking_space=space,
        status__in=[
            SessionStatus.ACTIVE,
            SessionStatus.PAYMENT_PENDING,
        ],
    ).exists()




def is_space_reserved_for_another_vehicle(
    space,
    vehicle_id=None,
    at_time=None,
    start_time=None,
    end_time=None,
):
    """
    Return True if another vehicle has a confirmed reservation
    that conflicts with the requested parking time.

    For walk-in entry, protect spaces with any confirmed
    reservation that has not ended yet.
    """
    at_time = at_time or timezone.now()

    reservations = Reservation.objects.filter(
        parking_space=space,
        status=ReservationStatus.CONFIRMED,
    )

    # Ignore reservations belonging to the same vehicle.
    if vehicle_id is not None:
        reservations = reservations.exclude(
            vehicle_id=vehicle_id
        )

    if start_time is not None and end_time is not None:
        # Check whether a reservation overlaps the requested period.
        reservations = reservations.filter(
            start_time__lt=end_time,
            end_time__gt=start_time,
        )
    else:
        # Walk-in: protect current and future reservations
        # until their reserved end time.
        reservations = reservations.filter(
            end_time__gt=at_time,
        )

    for reservation in reservations.only(
        "start_time",
        "grace_period_minutes",
    ).iterator():
        arrival_deadline = reservation.start_time + timedelta(
            minutes=reservation.grace_period_minutes
        )
        if reservation.start_time > at_time or at_time <= arrival_deadline:
            return True

    return False




def get_available_spaces(
    vehicle_type=None,
    requires_charging=False,
    vehicle_id=None,
    floor_number=None,
    start_time=None,
    end_time=None,
):
    """
    Return operational spaces that are not occupied
    or reserved during the requested time.
    """

    # This already filters by status, charging requirements,
    # and vehicle size when a vehicle type is provided.
    spaces = eligible_spaces(
        vehicle_type=vehicle_type,
        requires_charging=requires_charging,
    )

    # Apply floor filter only when provided.
    if floor_number is not None:
        spaces = spaces.filter(
            floor_number=floor_number
        )

    available = []

    for space in spaces:
        # Exclude spaces with an active parking session.
        if is_space_occupied(space):
            continue

        # Exclude spaces reserved by another vehicle
        # during the requested period.
        if is_space_reserved_for_another_vehicle(
            space=space,
            vehicle_id=vehicle_id,
            start_time=start_time,
            end_time=end_time,
        ):
            continue

        available.append(space)

    return available




@transaction.atomic
def allocate_space(vehicle, requires_charging=False):
    """
    Find and lock the first eligible parking space.

    Recheck its status, active occupancy, and reservations
    before returning it.

    The caller must create the parking session inside the
    same outer transaction.
    """
    candidates = eligible_spaces(
        vehicle_type=vehicle.vehicle_type,
        requires_charging=requires_charging,
    ).order_by(
        "size_capacity",
        "floor_number",
        "space_id",
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

        # Check whether another active session uses this space.
        if is_space_occupied(space):
            continue

        # Do not take a space reserved for another vehicle.
        if is_space_reserved_for_another_vehicle(
            space=space,
        ):
            continue

        return space

    return None