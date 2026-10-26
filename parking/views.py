from rest_framework.views import APIView
from rest_framework import serializers, status
from django.db import IntegrityError, transaction
from django.utils import timezone
from decimal import Decimal
from django.db.models import Count, Sum, Q
from datetime import date
from django.utils.crypto import get_random_string
from django.db.models.functions import TruncDate
from utils.responses import success_response, error_response
from .models import (ParkingSpace,Vehicle,VehicleType,SessionStatus,SpaceStatus,ParkingSession,Reservation,ReservationStatus, Payment, PaymentStatus,ChargingSession,ChargingStatus)
from .serializers import (ParkingSpaceSerializer,VehicleEntrySerializer,ReservationCreateSerializer,VehicleExitSerializer,ParkingTicketDetailSerializer)
from .services.allocation import get_available_spaces, allocate_space
from .services.reservations import create_reservation, validate_reservation_arrival
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
        floor_number = request.query_params.get("floor_number")

        start_time_raw = request.query_params.get("start_time")
        end_time_raw = request.query_params.get("end_time")

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

        # Both times must be supplied, or neither.
        if bool(start_time_raw) != bool(end_time_raw):
            return error_response(
                message="Provide both start_time and end_time.",
                errors={
                    "start_time": "Both time parameters are required together."
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        requires_charging = requires_charging_raw == "true"
        start_time = None
        end_time = None

        if start_time_raw and end_time_raw:
            try:
                date_field = serializers.DateTimeField()
                start_time = date_field.run_validation(start_time_raw)
                end_time = date_field.run_validation(end_time_raw)
            except serializers.ValidationError:
                return error_response(
                    message="Invalid date/time format.",
                    errors={
                        "start_time": "Use ISO 8601 with a timezone.",
                        "end_time": "Use ISO 8601 with a timezone.",
                    },
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if timezone.is_naive(start_time) or timezone.is_naive(end_time):
                return error_response(
                    message="Both times must include a timezone.",
                    errors={
                        "start_time": "Timezone is required.",
                        "end_time": "Timezone is required.",
                    },
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if start_time <= timezone.now():
                return error_response(
                    message="Availability time must be in the future.",
                    errors={"start_time": "Choose a future date and time."},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if end_time <= start_time:
                return error_response(
                    message="end_time must be after start_time.",
                    errors={"end_time": "Must be after start_time."},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        spaces = get_available_spaces(
            vehicle_type=vehicle_type,
            requires_charging=requires_charging,
            floor_number=floor_number,

            start_time=start_time,
            end_time=end_time,
        )

        return success_response(
            message="Parking availability retrieved successfully.",
            data={
                "vehicle_type": vehicle_type,
                "floor_number": floor_number,

                "requires_charging": requires_charging,
                "start_time": (
                    start_time.isoformat() if start_time else None
                ),
                "end_time": (
                    end_time.isoformat() if end_time else None
                ),
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
                vehicle, created = Vehicle.objects.get_or_create(
                    registration_number=data["registration_number"],
                    defaults={
                        "vehicle_type": data["vehicle_type"],
                    },
                )

                if vehicle.vehicle_type != data["vehicle_type"]:
                    return error_response(
                        message=(
                            "Vehicle registration already exists "
                            "with a different vehicle type."
                        ),
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

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
                        message=(
                            "This vehicle already has an active "
                            "parking session."
                        ),
                        status_code=status.HTTP_409_CONFLICT,
                    )

                reservation = None

                # Reserved entry: validate the reservation.
                if data.get("reservation_id"):
                    reservation, arrival_error = (
                        validate_reservation_arrival(
                            reservation_id=data["reservation_id"],
                            vehicle=vehicle,
                        )
                    )

                    if arrival_error:
                        return error_response(
                            message=arrival_error,
                            status_code=status.HTTP_409_CONFLICT,
                        )

                    space = reservation.parking_space

                # Walk-in entry: allocate a non-reserved space.
                else:
                    space = allocate_space(
                        vehicle=vehicle,
                        requires_charging=data["requires_charging"],
                    )

                    if space is None:
                        return error_response(
                            message=(
                                "No suitable parking space is available."
                            ),
                            status_code=status.HTTP_409_CONFLICT,
                        )

                ticket_number = (
                    f"TKT-{get_random_string(12).upper()}"
                )

                session = ParkingSession.objects.create(
                    vehicle=vehicle,
                    parking_space=space,
                    reservation=reservation,
                    ticket_number=ticket_number,
                    entry_time=timezone.now(),
                    status=SessionStatus.ACTIVE,
                )

                # Mark the reservation as used after creating the session.
                if reservation is not None:
                    reservation.status = ReservationStatus.CHECKED_IN
                    reservation.save(update_fields=["status"])

                return success_response(
                    message="Vehicle entry successful.",
                    data={
                        "ticket_number": session.ticket_number,
                        "registration_number": vehicle.registration_number,
                        "vehicle_type": vehicle.vehicle_type,
                        "parking_space": space.space_id,
                        "floor_number": space.floor_number,
                        "entry_time": session.entry_time,
                        "status": session.status,
                        "reservation_id": (
                            reservation.pk
                            if reservation is not None
                            else None
                        ),
                    },
                    status_code=status.HTTP_201_CREATED,
                )

        except IntegrityError:
            return error_response(
                message=(
                    "Vehicle entry could not be completed because "
                    "of a conflicting parking session or reservation."
                ),
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

        start_time = data["start_time"]
        end_time = data["end_time"]

        if end_time <= start_time:
            return error_response(
                message="End time must be after start time.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # 1. Get the vehicle or create it if it does not exist.
            vehicle, created = Vehicle.objects.get_or_create(
                registration_number=data["registration_number"],
                defaults={
                    "vehicle_type": data["vehicle_type"],
                },
            )

            # 2. Ensure the registration number has the correct vehicle type.
            if vehicle.vehicle_type != data["vehicle_type"]:
                return error_response(
                    message=(
                        "This registration number is already "
                        "registered with a different vehicle type."
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # 3. Check for an exact existing confirmed reservation.
            existing_reservation = (
                Reservation.objects.select_for_update()
                .filter(
                    vehicle=vehicle,
                    start_time=start_time,
                    end_time=end_time,
                    status=ReservationStatus.CONFIRMED,
                )
                .first()
            )

            if existing_reservation:
                return success_response(
                    message="This vehicle already has the same reservation.",
                    data={
                        "reservation_id": existing_reservation.id,
                        "registration_number": vehicle.registration_number,
                        "vehicle_type": vehicle.vehicle_type,
                        "space_id": existing_reservation.parking_space.space_id,
                        "floor_number": (
                            existing_reservation.parking_space.floor_number
                        ),
                        "start_time": (
                            existing_reservation.start_time.isoformat()
                        ),
                        "end_time": (
                            existing_reservation.end_time.isoformat()
                        ),
                        "status": existing_reservation.status,
                    },
                    status_code=status.HTTP_200_OK,
                )

            # 4. Create a new reservation if no exact duplicate exists.
            reservation = create_reservation(
                vehicle=vehicle,
                start_time=start_time,
                end_time=end_time,
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

            # 5. Return the newly created reservation.
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

        payments = Payment.objects.filter(
            parking_session_id=session.id
        ).order_by("-id")

        # Prefer a successful payment; otherwise return the latest attempt.
        payment = payments.filter(
            status=PaymentStatus.SUCCEEDED
        ).first()

        if payment is None:
            payment = payments.first()

        data = {
            "ticket_number": session.ticket_number,
            "registration_number": session.vehicle.registration_number,
            "vehicle_type": session.vehicle.vehicle_type,
            "space_id": session.parking_space.space_id,
            "floor_number": session.parking_space.floor_number,
            "entry_time": session.entry_time,
            "exit_time": session.exit_time,
            "session_status": session.status,
            "payment_status": payment.status if payment else None,
            "lost_ticket": session.lost_ticket,
            "parking_fee": session.parking_fee,
            "charging_fee": session.charging_fee,
            "discount_amount": session.discount_amount,
            "penalty_amount": session.penalty_amount,
            "total_fee": session.total_fee,
        }

        serializer = ParkingTicketDetailSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        return success_response(
            message="Parking ticket details retrieved successfully.",
            data=serializer.data,
        )



class ParkingRevenueReportView(APIView):

    def get(self, request):
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        # 1. Both dates are required for a selected date range.
        if not start_date or not end_date:
            return error_response(
                message="Both start_date and end_date are required.",
                status_code=400,
            )

        # 2. Validate the date format.
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            return error_response(
                message="Dates must use YYYY-MM-DD format.",
                status_code=400,
            )

        # 3. Validate the date range.
        if start > end:
            return error_response(
                message="start_date must not be after end_date.",
                status_code=400,
            )

        # 4. Only payments that are currently successful count.
        # REFUNDED, FAILED and PENDING payments are excluded.
        payments = Payment.objects.filter(
            status=PaymentStatus.SUCCEEDED,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )

        # 5. Calculate total revenue and successful payment count.
        summary = payments.aggregate(
            total_revenue=Sum("amount"),
            successful_payment_count=Count("id"),
        )

        total_revenue = (
            summary["total_revenue"] or Decimal("0.00")
        )

        payment_count = summary["successful_payment_count"]

        # 6. Break down revenue by payment date.
        daily_rows = (
            payments
            .annotate(payment_date=TruncDate("created_at"))
            .values("payment_date")
            .annotate(
                revenue=Sum("amount"),
                payment_count=Count("id"),
            )
            .order_by("payment_date")
        )

        revenue_by_date = [
            {
                "date": row["payment_date"].isoformat(),
                "revenue": str(row["revenue"]),
                "payment_count": row["payment_count"],
            }
            for row in daily_rows
        ]

        # 7. Break down revenue by vehicle type.
        vehicle_rows = (
            payments
            .values(
                "parking_session__vehicle__vehicle_type"
            )
            .annotate(
                revenue=Sum("amount"),
                payment_count=Count("id"),
            )
            .order_by(
                "parking_session__vehicle__vehicle_type"
            )
        )

        revenue_by_vehicle_type = [
            {
                "vehicle_type": row[
                    "parking_session__vehicle__vehicle_type"
                ],
                "revenue": str(row["revenue"]),
                "payment_count": row["payment_count"],
            }
            for row in vehicle_rows
        ]

        # 8. Return the report.
        data = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "successful_payment_count": payment_count,
            "total_revenue": str(total_revenue),
            "revenue_by_date": revenue_by_date,
            "revenue_by_vehicle_type": revenue_by_vehicle_type,
        }

        return success_response(
            message="Parking revenue report retrieved successfully.",
            data=data,
        )


class ParkingOccupancyReportView(APIView):

    def get(self, request):
        # 1. Validate optional date filters.
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        if bool(start_date) != bool(end_date):
            return error_response(
                message="Provide both start_date and end_date.",
                status_code=400,
            )

        start = end = None

        if start_date and end_date:
            try:
                start = date.fromisoformat(start_date)
                end = date.fromisoformat(end_date)
            except ValueError:
                return error_response(
                    message="Dates must use YYYY-MM-DD format.",
                    status_code=400,
                )

            if start > end:
                return error_response(
                    message="start_date must not be after end_date.",
                    status_code=400,
                )

        # 2. Get all spaces and operational spaces.
        all_spaces = ParkingSpace.objects.all()

        operational_spaces = all_spaces.filter(
            status=SpaceStatus.AVAILABLE
        )

        total_spaces = all_spaces.count()
        total_operational = operational_spaces.count()

        unavailable_spaces = all_spaces.exclude(
            status=SpaceStatus.AVAILABLE
        ).count()

        # 3. Find spaces occupied by open parking sessions.
        open_sessions = ParkingSession.objects.filter(
            status__in=[
                SessionStatus.ACTIVE,
                SessionStatus.PAYMENT_PENDING,
            ]
        )

        occupied_space_ids = (
            open_sessions
            .filter(
                parking_space__status=SpaceStatus.AVAILABLE
            )
            .values_list("parking_space_id", flat=True)
            .distinct()
        )

        occupied_count = len(set(occupied_space_ids))

        available_count = max(
            total_operational - occupied_count, 0
        )

        occupancy_percentage = (
            round(occupied_count / total_operational * 100, 2)
            if total_operational else 0.0
        )

        # 4. Count vehicles currently parked.
        # Count vehicles, not payment attempts or completed sessions.
        vehicles_parked = (
            open_sessions
            .values("vehicle_id")
            .distinct()
            .count()
        )

        # 5. Prepare floor-wise occupancy.
        floor_report = []

        floor_numbers = (
            all_spaces.order_by()
            .values_list("floor_number", flat=True)
            .distinct()
            .order_by("floor_number")
        )

        for floor_number in floor_numbers:
            floor_spaces = all_spaces.filter(
                floor_number=floor_number
            )

            floor_operational = floor_spaces.filter(
                status=SpaceStatus.AVAILABLE
            )

            floor_total = floor_operational.count()

            floor_occupied = (
                open_sessions
                .filter(
                    parking_space__floor_number=floor_number,
                    parking_space__status=SpaceStatus.AVAILABLE,
                )
                .values("parking_space_id")
                .distinct()
                .count()
            )

            floor_available = max(
                floor_total - floor_occupied, 0
            )

            floor_unavailable = floor_spaces.exclude(
                status=SpaceStatus.AVAILABLE
            ).count()

            floor_percentage = (
                round(floor_occupied / floor_total * 100, 2)
                if floor_total else 0.0
            )

            floor_report.append({
                "floor_number": floor_number,
                "total_spaces": floor_spaces.count(),
                "operational_spaces": floor_total,
                "occupied_spaces": floor_occupied,
                "available_spaces": floor_available,
                "unavailable_spaces": floor_unavailable,
                "occupancy_percentage": floor_percentage,
            })

        # 6. Calculate reservation fulfillment for the selected
        # reservation start-date range, if provided.
        reservations = Reservation.objects.all()

        if start and end:
            reservations = reservations.filter(
                start_time__date__gte=start,
                start_time__date__lte=end,
            )

        reservation_summary = reservations.aggregate(
            completed=Count(
                "id",
                filter=Q(status=ReservationStatus.COMPLETED),
            ),
            expired=Count(
                "id",
                filter=Q(status=ReservationStatus.EXPIRED),
            ),
            confirmed=Count(
                "id",
                filter=Q(status=ReservationStatus.CONFIRMED),
            ),
            cancelled=Count(
                "id",
                filter=Q(status=ReservationStatus.CANCELLED),
            ),
        )

        completed_reservations = (
            reservation_summary["completed"] or 0
        )
        expired_reservations = (
            reservation_summary["expired"] or 0
        )

        resolved_reservations = (
            completed_reservations + expired_reservations
        )

        reservation_fulfillment_percentage = (
            round(
                completed_reservations
                / resolved_reservations * 100,
                2,
            )
            if resolved_reservations else 0.0
        )

        # 7. Calculate current charging-space utilization.
        operational_charging_spaces = operational_spaces.filter(
            has_charger=True
        )

        total_charging_spaces = (
            operational_charging_spaces.count()
        )

        occupied_charging_spaces = (
            open_sessions
            .filter(
                parking_space__has_charger=True,
                parking_space__status=SpaceStatus.AVAILABLE,
            )
            .values("parking_space_id")
            .distinct()
            .count()
        )

        available_charging_spaces = max(
            total_charging_spaces - occupied_charging_spaces,
            0,
        )

        charging_space_utilization = (
            round(
                occupied_charging_spaces
                / total_charging_spaces * 100,
                2,
            )
            if total_charging_spaces else 0.0
        )

        # Count spaces with an active charging record.
        actively_charging_spaces = (
            ChargingSession.objects
            .filter(
                status=ChargingStatus.IN_PROGRESS,
                parking_session__status__in=[
                    SessionStatus.ACTIVE,
                    SessionStatus.PAYMENT_PENDING,
                ],
                parking_session__parking_space__has_charger=True,
            )
            .values("parking_session__parking_space_id")
            .distinct()
            .count()
        )

        # 8. Return the report.
        data = {
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "report_type": "current_occupancy_snapshot",
            "total_spaces": total_spaces,
            "total_operational_spaces": total_operational,
            "occupied_spaces": occupied_count,
            "available_spaces": available_count,
            "unavailable_spaces": unavailable_spaces,
            "vehicles_currently_parked": vehicles_parked,
            "occupancy_percentage": occupancy_percentage,
            "floor_wise_occupancy": floor_report,
            "reservation_report": {
                "completed_reservations": completed_reservations,
                "expired_reservations": expired_reservations,
                "confirmed_reservations": (
                    reservation_summary["confirmed"] or 0
                ),
                "cancelled_reservations": (
                    reservation_summary["cancelled"] or 0
                ),
                "reservation_fulfillment_percentage": (
                    reservation_fulfillment_percentage
                ),
            },
            "charging_space_report": {
                "total_operational_charging_spaces": (
                    total_charging_spaces
                ),
                "occupied_charging_spaces": (
                    occupied_charging_spaces
                ),
                "available_charging_spaces": (
                    available_charging_spaces
                ),
                "actively_charging_spaces": (
                    actively_charging_spaces
                ),
                "charging_space_utilization_percentage": (
                    charging_space_utilization
                ),
            },
        }

        return success_response(
            message="Parking occupancy report retrieved successfully.",
            data=data,
        )
