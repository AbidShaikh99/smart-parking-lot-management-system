
from decimal import Decimal, ROUND_HALF_UP
from math import ceil

from django.db.models import Sum
from django.utils import timezone

from parking.models import (
    ChargingSession,
    ParkingHoliday,
    ParkingSession,
    PricingRule,
)


def money(value):
    return Decimal(value).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def calculate_fee_breakdown(
    session,
    exit_time=None,
    lost_ticket=False,
):
    exit_time = exit_time or timezone.now()

    if exit_time < session.entry_time:
        raise ValueError("Exit time cannot be before entry time.")

    try:
        rule = PricingRule.objects.get(
            vehicle_type=session.vehicle.vehicle_type
        )
    except PricingRule.DoesNotExist:
        raise ValueError(
            "Pricing rule is not configured for this vehicle type."
        )

    # Calculate duration in seconds.
    duration_seconds = (
        exit_time - session.entry_time
    ).total_seconds()

    # Every started hour counts as a complete billable hour.
    billable_hours = max(1, ceil(duration_seconds / 3600))

    # Each started 24-hour period gets a daily maximum allowance.
    billing_days = max(
        1,
        ceil(duration_seconds / (24 * 3600)),
    )

    # Calculate the normal hourly parking charge.
    parking_before_cap = (
        rule.first_hour_rate
        + (
            max(0, billable_hours - 1)
            * rule.subsequent_hour_rate
        )
    )

    # Apply the daily maximum.
    maximum_allowed = rule.daily_maximum * billing_days
    parking_before_multiplier = min(
        parking_before_cap,
        maximum_allowed,
    )

    # Choose the multiplier using the local entry date.
    entry_date = timezone.localtime(
        session.entry_time
    ).date()

    is_holiday = ParkingHoliday.objects.filter(
        date=entry_date
    ).exists()

    if is_holiday:
        multiplier = rule.holiday_multiplier
    elif entry_date.weekday() >= 5:
        multiplier = rule.weekend_multiplier
    else:
        multiplier = Decimal("1.00")

    parking_fee = money(
        parking_before_multiplier * multiplier
    )

    # Membership discount applies to parking only.
    discount_amount = Decimal("0.00")

    if session.vehicle.is_member:
        discount_amount = money(
            parking_fee
            * rule.member_discount_percent
            / Decimal("100")
        )

    # Add recorded charging fees, if charging sessions exist.
    charging_total = ChargingSession.objects.filter(
        parking_session=session
    ).aggregate(total=Sum("charging_fee"))["total"]

    if charging_total is None:
        charging_total = session.charging_fee

    charging_fee = money(charging_total)

    # Add the configured penalty for a lost ticket.
    penalty_amount = (
        money(rule.lost_ticket_penalty)
        if lost_ticket
        else Decimal("0.00")
    )

    total_fee = money(
        parking_fee
        - discount_amount
        + charging_fee
        + penalty_amount
    )

    return {
        "ticket_number": session.ticket_number,
        "registration_number": session.vehicle.registration_number,
        "vehicle_type": session.vehicle.vehicle_type,
        "entry_time": session.entry_time.isoformat(),
        "calculated_exit_time": exit_time.isoformat(),
        "duration_hours": round(duration_seconds / 3600, 2),
        "billable_hours": billable_hours,
        "billing_days": billing_days,
        "parking_fee": str(parking_fee),
        "discount_amount": str(discount_amount),
        "charging_fee": str(charging_fee),
        "lost_ticket": lost_ticket,
        "penalty_amount": str(penalty_amount),
        "total_fee": str(total_fee),
    }
