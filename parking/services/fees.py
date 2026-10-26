from decimal import Decimal, ROUND_HALF_UP
from math import ceil

from django.utils import timezone

from parking.models import (
    ChargingSession,
    ParkingHoliday,
    PricingRule,
)


ZERO = Decimal("0.00")
ONE = Decimal("1.00")
HOUR_SECONDS = 3600
DAY_SECONDS = 24 * HOUR_SECONDS


def money(value):
    return Decimal(str(value)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def calculate_fee_breakdown(
    session,
    exit_time=None,
    lost_ticket=False,
):
    exit_time = exit_time or timezone.now()

    if timezone.is_naive(session.entry_time) or timezone.is_naive(exit_time):
        raise ValueError(
            "Entry and exit times must be timezone-aware."
        )

    if exit_time < session.entry_time:
        raise ValueError(
            "Exit time cannot be before entry time."
        )

    try:
        rule = PricingRule.objects.get(
            vehicle_type=session.vehicle.vehicle_type
        )
    except PricingRule.DoesNotExist:
        raise ValueError(
            "Pricing rule is not configured for this vehicle type."
        )

    # Validate pricing configuration.
    numeric_values = {
        "first-hour rate": rule.first_hour_rate,
        "subsequent-hour rate": rule.subsequent_hour_rate,
        "daily maximum": rule.daily_maximum,
        "weekend multiplier": rule.weekend_multiplier,
        "holiday multiplier": rule.holiday_multiplier,
        "membership discount": rule.member_discount_percent,
        "lost-ticket penalty": rule.lost_ticket_penalty,
        "charging rate": rule.charging_rate_per_kwh,
    }

    for name, value in numeric_values.items():
        if value is None or Decimal(str(value)) < ZERO:
            raise ValueError(
                f"Invalid pricing configuration: {name}."
            )

    if rule.member_discount_percent > Decimal("100"):
        raise ValueError(
            "Membership discount cannot exceed 100%."
        )

    # Calculate the stay duration and billable time.
    duration_seconds = (
        exit_time - session.entry_time
    ).total_seconds()

    billable_hours = max(
        1,
        ceil(duration_seconds / HOUR_SECONDS),
    )

    billing_days = max(
        1,
        ceil(duration_seconds / DAY_SECONDS),
    )

    entry_local = timezone.localtime(session.entry_time)
    exit_local = timezone.localtime(exit_time)

    # Get holidays covering the parking period.
    holiday_dates = set(
        ParkingHoliday.objects.filter(
            date__gte=entry_local.date(),
            date__lte=exit_local.date(),
        ).values_list("date", flat=True)
    )

    # Calculate each started hour and its date-specific multiplier.
    daily_amounts = {}
    daily_details = {}

    for hour_index in range(billable_hours):
        hour_start = session.entry_time + timezone.timedelta(
            seconds=hour_index * HOUR_SECONDS
        )
        local_date = timezone.localtime(hour_start).date()

        if hour_index == 0:
            base_rate = rule.first_hour_rate
        else:
            base_rate = rule.subsequent_hour_rate

        if local_date in holiday_dates:
            multiplier = rule.holiday_multiplier
            pricing_type = "HOLIDAY"
        elif local_date.weekday() >= 5:
            multiplier = rule.weekend_multiplier
            pricing_type = "WEEKEND"
        else:
            multiplier = ONE
            pricing_type = "NORMAL"

        hourly_charge = money(
            Decimal(str(base_rate))
            * Decimal(str(multiplier))
        )

        # Each started 24-hour block gets its own daily cap.
        block_number = hour_index // 24

        daily_amounts[block_number] = (
            daily_amounts.get(block_number, ZERO)
            + hourly_charge
        )

        daily_details.setdefault(block_number, []).append({
            "hour_number": hour_index + 1,
            "date": local_date.isoformat(),
            "base_rate": str(money(base_rate)),
            "pricing_type": pricing_type,
            "multiplier": str(multiplier),
            "charge": str(hourly_charge),
        })

    # Apply the daily maximum after multipliers.
    daily_charges = []
    parking_fee = ZERO

    for block_number in range(billing_days):
        uncapped_amount = money(
            daily_amounts.get(block_number, ZERO)
        )

        daily_cap = money(rule.daily_maximum)
        charged_amount = money(
            min(uncapped_amount, daily_cap)
        )

        parking_fee += charged_amount

        daily_charges.append({
            "billing_block": block_number + 1,
            "uncapped_amount": str(uncapped_amount),
            "daily_maximum": str(daily_cap),
            "charged_amount": str(charged_amount),
            "hourly_details": daily_details.get(
                block_number, []
            ),
        })

    parking_fee = money(parking_fee)

    # Membership discount applies to parking only.
    discount_amount = ZERO

    if session.vehicle.is_member:
        discount_amount = money(
            parking_fee
            * Decimal(str(rule.member_discount_percent))
            / Decimal("100")
        )

    # Use the recorded charging-session fees.
    charging_sessions = ChargingSession.objects.filter(
        parking_session=session
    )

    charging_details = []
    charging_fee = ZERO

    for charging in charging_sessions:
        energy = Decimal(str(charging.energy_kwh))
        rate = Decimal(str(charging.rate_per_kwh))

        if energy < ZERO or rate < ZERO:
            raise ValueError(
                "Charging energy and rate cannot be negative."
            )

        line_fee = money(energy * rate)
        charging_fee += line_fee

        charging_details.append({
            "energy_kwh": str(energy),
            "rate_per_kwh": str(money(rate)),
            "charging_fee": str(line_fee),
        })

    if not charging_details:
        charging_fee = money(session.charging_fee)

    charging_fee = money(charging_fee)

    # Lost-ticket penalty is separate from parking and charging.
    penalty_amount = (
        money(rule.lost_ticket_penalty)
        if lost_ticket
        else ZERO
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
        "is_member": session.vehicle.is_member,
        "entry_time": session.entry_time.isoformat(),
        "calculated_exit_time": exit_time.isoformat(),
        "duration_hours": round(
            duration_seconds / HOUR_SECONDS, 2
        ),
        "billable_hours": billable_hours,
        "billing_days": billing_days,
        "pricing_rule": {
            "first_hour_rate": str(money(rule.first_hour_rate)),
            "subsequent_hour_rate": str(
                money(rule.subsequent_hour_rate)
            ),
            "daily_maximum": str(money(rule.daily_maximum)),
            "weekend_multiplier": str(rule.weekend_multiplier),
            "holiday_multiplier": str(rule.holiday_multiplier),
            "member_discount_percent": str(
                rule.member_discount_percent
            ),
            "lost_ticket_penalty": str(
                money(rule.lost_ticket_penalty)
            ),
            "charging_rate_per_kwh": str(
                money(rule.charging_rate_per_kwh)
            ),
        },
        "daily_charges": daily_charges,
        "parking_fee": str(parking_fee),
        "discount_amount": str(discount_amount),
        "charging_fee": str(charging_fee),
        "charging_details": charging_details,
        "lost_ticket": lost_ticket,
        "penalty_amount": str(penalty_amount),
        "total_fee": str(total_fee),
    }
