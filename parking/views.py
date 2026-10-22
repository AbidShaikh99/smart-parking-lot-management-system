from rest_framework.views import APIView
from rest_framework import status
from django.db import IntegrityError, transaction
from django.utils import timezone
from decimal import Decimal
from django.db.models import Sum
from datetime import date

from utils.responses import success_response, error_response
from .models import (ParkingSpace,Vehicle,VehicleType,SessionStatus,SpaceStatus,ParkingSession,Reservation,ReservationStatus, Payment, PaymentStatus)
from .serializers import (ParkingSpaceSerializer,VehicleEntrySerializer,ReservationCreateSerializer,VehicleExitSerializer)
from .services.allocation import get_available_spaces, allocate_space
from .services.reservations import create_reservation
from .services.fees import calculate_fee_breakdown
from .services.payments import process_vehicle_exit

class ParkingSpaceListCreateView(APIView):
    def get(self, request):
        spaces = ParkingSpace.objects.all().order_by(
            "floor_number", "space_id"
        )

        serializer = ParkingSpaceSerializer(spaces, many=True)

        return success_response(
            message="Parking spaces retrieved successfully.",
            data=serializer.data,
        )

    def post(self, request):
        serializer = ParkingSpaceSerializer(data=request.data)

        if not serializer.is_valid():
            return error_response(
                message="Parking space validation failed.",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        space = serializer.save()

        return success_response(
            message="Parking space created successfully.",
            data=ParkingSpaceSerializer(space).data,
            status_code=status.HTTP_201_CREATED,
        )



class ParkingAvailabilityView(APIView):
    def get(self, request):
        vehicle_type = request.query_params.get("vehicle_type")
        requires_charging_raw = request.query_params.get(
            "requires_charging", "false"
        ).lower()

        valid_types = [choice[0] for choice in VehicleType.choices]

        if vehicle_type not in valid_types:
            return error_response(
                message="A valid vehicle_type query parameter is required.",
                errors={"vehicle_type": valid_types},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if requires_charging_raw not in ("true", "false"):
            return error_response(
                message="requires_charging must be true or false.",
                errors={"requires_charging": "Invalid boolean value."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        requires_charging = requires_charging_raw == "true"

        spaces = get_available_spaces(
            vehicle_type=vehicle_type,
            requires_charging=requires_charging,
        )

        return success_response(
            message="Parking availability retrieved successfully.",
            data={
                "vehicle_type": vehicle_type,
                "requires_charging": requires_charging,
                "available_count": len(spaces),
                "spaces": ParkingSpaceSerializer(
                    spaces, many=True
                ).data,
            },
        )


class VehicleEntryView(APIView):
    def post(self, request):
        serializer = VehicleEntrySerializer(data=request.data)

        if not serializer.is_valid():
            return error_response(
                message="Vehicle entry validation failed.",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data

        try:
            with transaction.atomic():
                vehicle, _ = Vehicle.objects.get_or_create(
                    registration_number=data["registration_number"],
                    defaults={
                        "vehicle_type": data["vehicle_type"],
                    },
                )

                # An existing registration must retain its registered type.
                if vehicle.vehicle_type != data["vehicle_type"]:
                    return error_response(
                        message=(
                            "This registration number is already "
                            "registered with a different vehicle type."
                        ),
                        errors={
                            "vehicle_type": vehicle.vehicle_type,
                        },
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

                # Lock the vehicle record before checking for an open session.
                vehicle = Vehicle.objects.select_for_update().get(
                    pk=vehicle.pk
                )

                active_session_exists = ParkingSession.objects.filter(
                    vehicle=vehicle,
                    status__in=[
                        SessionStatus.ACTIVE,
                        SessionStatus.PAYMENT_PENDING,
                    ],
                ).exists()

                if active_session_exists:
                    return error_response(
                        message="This vehicle already has an active parking session.",
                        errors={
                            "registration_number": (
                                data["registration_number"]
                            )
                        },
                        status_code=status.HTTP_409_CONFLICT,
                    )

                space = allocate_space(
                    vehicle=vehicle,
                    requires_charging=data["requires_charging"],
                )

                if space is None:
                    return error_response(
                        message="No suitable parking space is available.",
                        errors={
                            "vehicle_type": data["vehicle_type"],
                            "requires_charging": data["requires_charging"],
                        },
                        status_code=status.HTTP_409_CONFLICT,
                    )

                session = ParkingSession.objects.create(
                    vehicle=vehicle,
                    parking_space=space,
                    ticket_number="TEMP",
                    status=SessionStatus.ACTIVE,
                )

                session.ticket_number = f"TKT-{session.id:06d}"
                session.save(update_fields=["ticket_number"])

                return success_response(
                    message="Vehicle parked successfully.",
                    data={
                        "ticket_number": session.ticket_number,
                        "registration_number": vehicle.registration_number,
                        "vehicle_type": vehicle.vehicle_type,
                        "space_id": space.space_id,
                        "floor_number": space.floor_number,
                        "entry_time": session.entry_time.isoformat(),
                        "session_status": session.status,
                    },
                    status_code=status.HTTP_201_CREATED,
                )

        except IntegrityError:
            return error_response(
                message=(
                    "The vehicle or parking space was allocated "
                    "by another request. Please retry."
                ),
                errors=None,
                status_code=status.HTTP_409_CONFLICT,
            )


class ParkingReservationView(APIView):
    def post(self, request):
        serializer = ReservationCreateSerializer(data=request.data)

        if not serializer.is_valid():
            return error_response(
                message="Reservation validation failed.",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data

        with transaction.atomic():
            vehicle, created = Vehicle.objects.get_or_create(
                registration_number=data["registration_number"],
                defaults={
                    "vehicle_type": data["vehicle_type"],
                },
            )

            if vehicle.vehicle_type != data["vehicle_type"]:
                return error_response(
                    message=(
                        "This registration number is already "
                        "registered with a different vehicle type."
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            reservation = create_reservation(
                vehicle=vehicle,
                start_time=data["start_time"],
                end_time=data["end_time"],
                requires_charging=data["requires_charging"],
            )

            if reservation is None:
                return error_response(
                    message=(
                        "No suitable parking space is available "
                        "for the requested time."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )

            return success_response(
                message="Parking reservation created successfully.",
                data={
                    "reservation_id": reservation.id,
                    "registration_number": vehicle.registration_number,
                    "vehicle_type": vehicle.vehicle_type,
                    "space_id": reservation.parking_space.space_id,
                    "floor_number": reservation.parking_space.floor_number,
                    "start_time": reservation.start_time.isoformat(),
                    "end_time": reservation.end_time.isoformat(),
                    "status": reservation.status,
                },
                status_code=status.HTTP_201_CREATED,
            )


class ParkingReservationCancelView(APIView):
    def delete(self, request, reservation_id):
        with transaction.atomic():
            reservation = (
                Reservation.objects
                .select_for_update()
                .filter(pk=reservation_id)
                .first()
            )

            if reservation is None:
                return error_response(
                    message="Reservation not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            if reservation.status != ReservationStatus.CONFIRMED:
                return error_response(
                    message="Only confirmed reservations can be cancelled.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if reservation.start_time <= timezone.now():
                return error_response(
                    message="A reservation cannot be cancelled after it starts.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            reservation.status = ReservationStatus.CANCELLED
            reservation.cancelled_at = timezone.now()
            reservation.save(
                update_fields=["status", "cancelled_at"]
            )

            return success_response(
                message="Reservation cancelled successfully.",
                data={
                    "reservation_id": reservation.id,
                    "status": reservation.status,
                    "cancelled_at": reservation.cancelled_at.isoformat(),
                },
            )


class ParkingFeeView(APIView):
    def get(self, request, ticket_number):
        session = (
            ParkingSession.objects
            .select_related("vehicle")
            .filter(ticket_number=ticket_number)
            .first()
        )

        if session is None:
            return error_response(
                message="Parking ticket not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        lost_ticket_param = request.query_params.get(
            "lost_ticket", "false"
        ).lower()

        if lost_ticket_param not in ("true", "false"):
            return error_response(
                message="lost_ticket must be true or false.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # A completed session uses its saved exit time and lost-ticket flag.
        # An active session receives a current fee estimate.
        if session.exit_time is not None:
            exit_time = session.exit_time
            lost_ticket = session.lost_ticket
        else:
            exit_time = timezone.now()
            lost_ticket = lost_ticket_param == "true"

        try:
            breakdown = calculate_fee_breakdown(
                session=session,
                exit_time=exit_time,
                lost_ticket=lost_ticket,
            )
        except ValueError as exc:
            return error_response(
                message=str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return success_response(
            message="Parking fee calculated successfully.",
            data=breakdown,
        )


class VehicleExitView(APIView):
    def post(self, request):
        serializer = VehicleExitSerializer(data=request.data)

        if not serializer.is_valid():
            return error_response(
                message="Vehicle exit validation failed.",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        result = process_vehicle_exit(
            ticket_number=serializer.validated_data["ticket_number"],
            lost_ticket=serializer.validated_data["lost_ticket"],
            payment_success=serializer.validated_data["payment_success"],
        )

        if not result["success"]:
            return error_response(
                message=result["message"],
                errors=result.get("data"),
                status_code=result["status_code"],
            )

        return success_response(
            message=result["message"],
            data=result["data"],
            status_code=result["status_code"],
        )

class ParkingTicketDetailView(APIView):
    def get(self, request, ticket_number):
        session = (
            ParkingSession.objects
            .select_related("vehicle", "parking_space")
            .filter(ticket_number=ticket_number)
            .first()
        )

        if session is None:
            return error_response(
                message="Parking ticket not found.",
                status_code=404,
            )

        data = {
            "ticket_number": session.ticket_number,
            "registration_number": session.vehicle.registration_number,
            "vehicle_type": session.vehicle.vehicle_type,
            "space_id": session.parking_space.space_id,
            "floor_number": session.parking_space.floor_number,
            "entry_time": session.entry_time,
            "exit_time": session.exit_time,
            "session_status": session.status,
            "lost_ticket": session.lost_ticket,
            "parking_fee": str(session.parking_fee),
            "charging_fee": str(session.charging_fee),
            "discount_amount": str(session.discount_amount),
            "penalty_amount": str(session.penalty_amount),
            "total_fee": str(session.total_fee),
        }

        return success_response(
            message="Parking ticket details retrieved successfully.",
            data=data,
        )


class ParkingRevenueReportView(APIView):
    def get(self, request):
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            start = date.fromisoformat(start_date) if start_date else None
            end = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            return error_response(
                message="Dates must use YYYY-MM-DD format.",
                status_code=400,
            )

        if start and end and start > end:
            return error_response(
                message="start_date must not be after end_date.",
                status_code=400,
            )

        payments = Payment.objects.filter(
            status=PaymentStatus.SUCCEEDED
        )

        if start:
            payments = payments.filter(created_at__date__gte=start)

        if end:
            payments = payments.filter(created_at__date__lte=end)

        result = payments.aggregate(
            total_revenue=Sum("amount"),
        )

        total_revenue = result["total_revenue"] or Decimal("0.00")
        payment_count = payments.count()

        data = {
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "successful_payment_count": payment_count,
            "total_revenue": str(total_revenue),
        }

        return success_response(
            message="Parking revenue report retrieved successfully.",
            data=data,
        )

class ParkingOccupancyReportView(APIView):
    def get(self, request):
        # Get all operational parking spaces.
        operational_spaces = ParkingSpace.objects.filter(
            status=SpaceStatus.AVAILABLE
        )

        total_spaces = operational_spaces.count()

        # Find spaces with an active parking session.
        occupied_space_ids = ParkingSession.objects.filter(
            status__in=[
                SessionStatus.ACTIVE,
                SessionStatus.PAYMENT_PENDING,
            ],
            parking_space__status=SpaceStatus.AVAILABLE,
        ).values_list("parking_space_id", flat=True).distinct()

        occupied_count = len(set(occupied_space_ids))
        available_count = max(total_spaces - occupied_count, 0)

        # Calculate occupancy percentage safely.
        occupancy_percentage = (
            round((occupied_count / total_spaces) * 100, 2)
            if total_spaces > 0
            else 0.0
        )

        # Prepare occupancy details for each floor.
        floor_report = []

        floor_numbers = (
            operational_spaces.order_by()
            .values_list("floor_number", flat=True)
            .distinct()
            .order_by("floor_number")
        )

        for floor_number in floor_numbers:
            floor_spaces = operational_spaces.filter(
                floor_number=floor_number
            )

            floor_total = floor_spaces.count()

            floor_occupied = ParkingSession.objects.filter(
                parking_space__floor_number=floor_number,
                parking_space__status=SpaceStatus.AVAILABLE,
                status__in=[
                    SessionStatus.ACTIVE,
                    SessionStatus.PAYMENT_PENDING,
                ],
            ).values("parking_space_id").distinct().count()

            floor_available = max(floor_total - floor_occupied, 0)

            floor_percentage = (
                round((floor_occupied / floor_total) * 100, 2)
                if floor_total > 0
                else 0.0
            )

            floor_report.append({
                "floor_number": floor_number,
                "total_spaces": floor_total,
                "occupied_spaces": floor_occupied,
                "available_spaces": floor_available,
                "occupancy_percentage": floor_percentage,
            })

        data = {
            "total_operational_spaces": total_spaces,
            "occupied_spaces": occupied_count,
            "available_spaces": available_count,
            "occupancy_percentage": occupancy_percentage,
            "floor_wise_occupancy": floor_report,
        }

        return success_response(
            message="Parking occupancy report retrieved successfully.",
            data=data,
        )