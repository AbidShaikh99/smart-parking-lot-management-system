from rest_framework import serializers
import re
from django.utils import timezone

from .models import (ParkingSpace, Vehicle, VehicleType,Reservation, ReservationStatus)




class ParkingSpaceSerializer(serializers.ModelSerializer):
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
        read_only_fields = ["id", "created_at"]

    def validate_space_id(self, value):
        value = value.strip().upper()

        queryset = ParkingSpace.objects.filter(space_id=value)

        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise serializers.ValidationError(
                "A parking space with this ID already exists."
            )

        return value

    def validate_floor_number(self, value):
        if value < 0:
            raise serializers.ValidationError(
                "Floor number cannot be negative."
            )
        return value

    def validate_size_capacity(self, value):
        if value not in (1, 2, 3):
            raise serializers.ValidationError(
                "Size capacity must be 1, 2, or 3."
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
    registration_number = serializers.CharField(max_length=20)
    vehicle_type = serializers.ChoiceField(
        choices=VehicleType.choices
    )
    requires_charging = serializers.BooleanField(default=False)

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