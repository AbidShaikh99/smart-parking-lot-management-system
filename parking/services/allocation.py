from django.conf import settings
from django.db import transaction
from django.db.models import Q
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


def eligible_spaces(vehicle_type, requires_charging=False):
    """
    Return operational spaces that match the vehicle's size
    and charging requirements.
    """
    spaces = ParkingSpace.objects.filter(
        status=SpaceStatus.AVAILABLE
    )

    if requires_charging:
        spaces = spaces.filter(has_charger=True)

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

        return spaces.order_by("size_capacity", "floor_number", "space_id")

    if vehicle_type == VehicleType.CAR:
        spaces = spaces.filter(size_capacity__gte=2)
        return spaces.order_by("size_capacity", "floor_number", "space_id")

    if vehicle_type in (VehicleType.SUV, VehicleType.ELECTRIC):
        spaces = spaces.filter(size_capacity__gte=3 if vehicle_type == VehicleType.SUV else 2)
        return spaces.order_by("size_capacity", "floor_number", "space_id")

    return spaces.none()


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
    Return True when another vehicle has a confirmed reservation
    that overlaps the requested parking time.
    """
    at_time = at_time or timezone.now()

    reservations = Reservation.objects.filter(
        parking_space=space,
        status=ReservationStatus.CONFIRMED,
    )

    if vehicle_id is not None:
        reservations = reservations.exclude(
            vehicle_id=vehicle_id
        )

    if start_time is not None and end_time is not None:
        reservations = reservations.filter(
            start_time__lt=end_time,
            end_time__gt=start_time,
        )
    else:
        reservations = reservations.filter(
            start_time__lte=at_time,
            end_time__gt=at_time,
        )

    return reservations.exists()


def get_available_spaces(
    vehicle_type,
    requires_charging=False,
    vehicle_id=None,
    floor_number=None,
    start_time=None,
    end_time=None,
):
    """
    Return spaces suitable for the vehicle that are operational,
    not occupied, and not reserved during the requested time.
    """
    spaces = eligible_spaces(
        vehicle_type=vehicle_type,
        requires_charging=requires_charging,
    )

    if floor_number is not None:
        spaces = spaces.filter(floor_number=floor_number)

    available = []

    for space in spaces:
        # Active parking sessions occupy a space.
        if is_space_occupied(space):
            continue

        # Exclude spaces reserved for another vehicle.
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