from django.db import models
from django.db.models import Q
from django.utils import timezone


class VehicleType(models.TextChoices):
    MOTORCYCLE = "MOTORCYCLE", "Motorcycle"
    CAR = "CAR", "Car"
    SUV = "SUV", "SUV"
    ELECTRIC = "ELECTRIC", "Electric Vehicle"


class SpaceStatus(models.TextChoices):
    AVAILABLE = "AVAILABLE", "Available"
    MAINTENANCE = "MAINTENANCE", "Maintenance"
    INACTIVE = "INACTIVE", "Inactive"


class SessionStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    PAYMENT_PENDING = "PAYMENT_PENDING", "Payment Pending"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class ReservationStatus(models.TextChoices):
    CONFIRMED = "CONFIRMED", "Confirmed"
    CHECKED_IN = "CHECKED_IN", "Checked In"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Expired"
    COMPLETED = "COMPLETED", "Completed"


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    REFUNDED = "REFUNDED", "Refunded"


class ChargingStatus(models.TextChoices):
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"



class Vehicle(models.Model):
    registration_number = models.CharField(
        max_length=20,
        unique=True,
    )
    vehicle_type = models.CharField(
        max_length=20,
        choices=VehicleType.choices,
    )
    is_member = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["registration_number"]

    def __str__(self):
        return f"{self.registration_number} ({self.vehicle_type})"



class ParkingSpace(models.Model):
    space_id = models.CharField(max_length=30, unique=True)
    floor_number = models.PositiveSmallIntegerField()

    # 1 = motorcycle, 2 = car, 3 = SUV-sized vehicle
    size_capacity = models.PositiveSmallIntegerField(default=2)

    # Examples: MOTORCYCLE, COMPACT, STANDARD, LARGE
    space_type = models.CharField(max_length=20)

    has_charger = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        choices=SpaceStatus.choices,
        default=SpaceStatus.AVAILABLE,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["floor_number", "space_id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(size_capacity__gte=1)
                & Q(size_capacity__lte=3),
                name="valid_parking_space_capacity",
            ),
            models.CheckConstraint(
                condition=Q(floor_number__gte=0),
                name="valid_parking_floor",
            ),
        ]

    def __str__(self):
        return f"{self.space_id} - Floor {self.floor_number}"



class ParkingSession(models.Model):
    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.PROTECT,
        related_name="parking_sessions",
    )
    parking_space = models.ForeignKey(
        ParkingSpace,
        on_delete=models.PROTECT,
        related_name="parking_sessions",
    )
    ticket_number = models.CharField(max_length=40, unique=True)
    reservation = models.OneToOneField("Reservation",on_delete=models.PROTECT,null=True,blank=True,related_name="parking_session")

    entry_time = models.DateTimeField(default=timezone.now)
    exit_time = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=SessionStatus.choices,
        default=SessionStatus.ACTIVE,
    )

    parking_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    charging_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    penalty_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    total_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )

    lost_ticket = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-entry_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle"],
                condition=Q(status__in=["ACTIVE", "PAYMENT_PENDING"]),
                name="one_open_session_per_vehicle",
            ),
            models.UniqueConstraint(
                fields=["parking_space"],
                condition=Q(status__in=["ACTIVE", "PAYMENT_PENDING"]),
                name="one_open_session_per_space",
            ),
            models.CheckConstraint(
                condition=Q(exit_time__isnull=True)
                | Q(exit_time__gte=models.F("entry_time")),
                name="session_exit_after_entry",
            ),
        ]

    def __str__(self):
        return self.ticket_number



class PricingRule(models.Model):
    vehicle_type = models.CharField(
        max_length=20,
        choices=VehicleType.choices,
        unique=True,
    )

    first_hour_rate = models.DecimalField(
        max_digits=10, decimal_places=2
    )
    subsequent_hour_rate = models.DecimalField(
        max_digits=10, decimal_places=2
    )
    daily_maximum = models.DecimalField(
        max_digits=10, decimal_places=2
    )

    weekend_multiplier = models.DecimalField(
        max_digits=5, decimal_places=2, default=1
    )
    holiday_multiplier = models.DecimalField(
        max_digits=5, decimal_places=2, default=1
    )
    member_discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0
    )
    lost_ticket_penalty = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    charging_rate_per_kwh = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )

    def __str__(self):
        return f"{self.vehicle_type} pricing"



class Reservation(models.Model):
    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.PROTECT,
        related_name="reservations",
    )
    parking_space = models.ForeignKey(
        ParkingSpace,
        on_delete=models.PROTECT,
        related_name="reservations",
    )

    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    grace_period_minutes = models.PositiveSmallIntegerField(default=15)

    status = models.CharField(
        max_length=20,
        choices=ReservationStatus.choices,
        default=ReservationStatus.CONFIRMED,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["start_time"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=models.F("start_time")),
                name="reservation_end_after_start",
            ),
        ]

    def __str__(self):
        return f"Reservation {self.pk} - {self.parking_space.space_id}"



class Payment(models.Model):
    parking_session = models.ForeignKey(
        ParkingSession,
        on_delete=models.PROTECT,
        related_name="payments",
    )

    idempotency_key = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
    )
    transaction_reference = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
    )

    amount = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="payment_amount_nonnegative",
            ),
            models.UniqueConstraint(
                fields=["parking_session"],
                condition=Q(status="SUCCEEDED"),
                name="one_successful_payment_per_session",
            ),
        ]

    def __str__(self):
        return f"Payment {self.pk} - {self.status}"



class ChargingSession(models.Model):
    parking_session = models.ForeignKey(
        ParkingSession,
        on_delete=models.PROTECT,
        related_name="charging_sessions",
    )

    energy_kwh = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=0,
    )
    rate_per_kwh = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    charging_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=ChargingStatus.choices,
        default=ChargingStatus.IN_PROGRESS,
    )

    def __str__(self):
        return f"Charging for ticket {self.parking_session.ticket_number}"

class ParkingHoliday(models.Model):
    date = models.DateField(unique=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.name} ({self.date})"
