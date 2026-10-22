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


def is_space_reserved_for_another_vehicle(space, vehicle_id, at_time=None):
    """
    Protect a space if another vehicle has a confirmed reservation
    that has not yet ended.
    """
    at_time = at_time or timezone.now()

    return Reservation.objects.filter(
        parking_space=space,
        status=ReservationStatus.CONFIRMED,
        end_time__gt=at_time,
    ).exclude(
        vehicle_id=vehicle_id
    ).exists()


def get_available_spaces(
    vehicle_type,
    requires_charging=False,
    vehicle_id=None,
):
    """
    Return eligible spaces that are not occupied and are not
    protected by another vehicle's reservation.
    """
    available = []

    for space in eligible_spaces(
        vehicle_type=vehicle_type,
        requires_charging=requires_charging,
    ):
        if is_space_occupied(space):
            continue

        if is_space_reserved_for_another_vehicle(
            space=space,
            vehicle_id=vehicle_id,
        ):
            continue

        available.append(space)

    return available


@transaction.atomic
def allocate_space(
    vehicle,
    requires_charging=False,
):
    """
    Lock candidate spaces and allocate the first suitable
    space that is still free.
    """
    candidates = eligible_spaces(
        vehicle_type=vehicle.vehicle_type,
        requires_charging=requires_charging,
    ).order_by("size_capacity", "floor_number", "space_id")

    for candidate in candidates:
        space = (
            ParkingSpace.objects.select_for_update()
            .filter(pk=candidate.pk, status=SpaceStatus.AVAILABLE)
            .first()
        )

        if space is None:
            continue

        if is_space_occupied(space):
            continue

        if is_space_reserved_for_another_vehicle(
            space=space,
            vehicle_id=vehicle.pk,
        ):
            continue

        return space

    return None
