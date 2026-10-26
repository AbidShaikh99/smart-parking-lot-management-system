from rest_framework import serializers
import re
from django.utils import timezone

from .models import (ParkingSpace, SpaceStatus, Vehicle, VehicleType,Reservation, ReservationStatus)

class ParkingSpaceSerializer(serializers.ModelSerializer):
    space_id = serializers.CharField(
        max_length=30,
        required=True,
        allow_blank=False,
    )

    space_type = serializers.CharField(
        max_length=20,
        required=True,
        allow_blank=False,
    )

    floor_number = serializers.IntegerField(
        required=True,
        min_value=0,
    )

    size_capacity = serializers.IntegerField(
        required=True,
        min_value=1,
        max_value=3,
    )

    has_charger = serializers.BooleanField(
        required=True,
    )

    status = serializers.ChoiceField(
        choices=SpaceStatus.choices,
        required=True,
    )

    class Meta:
        model = ParkingSpace
        fields = [
            "id",
            "space_id",
            "floor_number",
            "size_capacity",
            "space_type",
            "has_charger",
            "status",
            "created_at",
        ]
        read_only_fields = ["id", "created_at",]

    def validate_space_id(self, value):
        value = value.strip().upper()

        queryset = ParkingSpace.objects.filter(
            space_id=value
        )

        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise serializers.ValidationError(
                "A parking space with this ID already exists."
            )

        return value

    def validate_space_type(self, value):
        value = value.strip().upper()

        allowed_types = {
            "MOTORCYCLE",
            "COMPACT",
            "STANDARD",
            "LARGE",
        }

        if value not in allowed_types:
            raise serializers.ValidationError(
                "Space type must be MOTORCYCLE, COMPACT, "
                "STANDARD, or LARGE."
            )

        return value

class VehicleEntrySerializer(serializers.Serializer):
    registration_number = serializers.CharField(
        max_length=20,
        required=True,
    )

    vehicle_type = serializers.ChoiceField(
        choices=VehicleType.choices,
        required=True,
    )

    requires_charging = serializers.BooleanField(
        default=False,
        required=False,
    )

    reservation_id = serializers.IntegerField(
        required=False,
        min_value=1,
    )

    def validate_registration_number(self, value):
        value = value.strip().upper()

        if not re.fullmatch(r"[A-Z0-9-]{4,20}", value):
            raise serializers.ValidationError(
                "Enter a valid registration number using "
                "4-20 letters, numbers, or hyphens."
            )

        return value


class ReservationCreateSerializer(serializers.Serializer):
    registration_number = serializers.CharField(max_length=20)
    vehicle_type = serializers.ChoiceField(
        choices=VehicleType.choices
    )
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    requires_charging = serializers.BooleanField(default=False)

    def validate_registration_number(self, value):
        value = value.strip().upper()

        if not re.fullmatch(r"[A-Z0-9-]{4,20}", value):
            raise serializers.ValidationError(
                "Enter a valid registration number."
            )

        return value

    def validate(self, attrs):
        start_time = attrs["start_time"]
        end_time = attrs["end_time"]

        if start_time <= timezone.now():
            raise serializers.ValidationError({
                "start_time": "Reservation must start in the future."
            })

        if end_time <= start_time:
            raise serializers.ValidationError({
                "end_time": "End time must be after start time."
            })

        return attrs
        
class VehicleExitSerializer(serializers.Serializer):
    ticket_number = serializers.CharField(max_length=40)
    lost_ticket = serializers.BooleanField(default=False)
    payment_success = serializers.BooleanField(default=True)


class ParkingTicketDetailSerializer(serializers.Serializer):
    ticket_number = serializers.CharField()
    registration_number = serializers.CharField()
    vehicle_type = serializers.CharField()
    space_id = serializers.CharField()
    floor_number = serializers.IntegerField()

    entry_time = serializers.DateTimeField()
    exit_time = serializers.DateTimeField(allow_null=True)

    session_status = serializers.CharField()
    payment_status = serializers.CharField(allow_null=True)

    lost_ticket = serializers.BooleanField()

    parking_fee = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    charging_fee = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    discount_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    penalty_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    total_fee = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
