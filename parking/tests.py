from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from parking.models import (
    ParkingSession,
    ParkingSpace,
    Reservation,
    ReservationStatus,
    SessionStatus,
    SpaceStatus,
    Vehicle,
    VehicleType,
)
from parking.services.reservations import (
    create_reservation,
    expire_missed_reservations,
    validate_reservation_arrival,
)


class VehicleEntryTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def create_space(self, space_id, size=2, has_charger=False):
        return ParkingSpace.objects.create(
            space_id=space_id,
            floor_number=0,
            size_capacity=size,
            space_type="STANDARD",
            has_charger=has_charger,
            status=SpaceStatus.AVAILABLE,
        )

    def enter_vehicle(self, registration, vehicle_type="CAR", **extra):
        payload = {
            "registration_number": registration,
            "vehicle_type": vehicle_type,
            **extra,
        }
        return self.client.post(
            "/api/vehicles/entry/",
            payload,
            format="json",
        )

    def create_vehicle(self, registration="TEST-1234", vehicle_type="CAR"):
        return Vehicle.objects.create(
            registration_number=registration,
            vehicle_type=vehicle_type,
        )

    def create_reservation(
        self,
        vehicle,
        space,
        start_time=None,
        grace_period_minutes=15,
    ):
        start_time = start_time or timezone.now() + timedelta(hours=1)
        return Reservation.objects.create(
            vehicle=vehicle,
            parking_space=space,
            start_time=start_time,
            end_time=start_time + timedelta(hours=1),
            grace_period_minutes=grace_period_minutes,
            status=ReservationStatus.CONFIRMED,
        )

    def test_walk_in_entry_creates_session_and_ticket(self):
        space = self.create_space("A-1")

        response = self.enter_vehicle("CAR-1234")

        self.assertEqual(response.status_code, 201)
        session = ParkingSession.objects.get()
        self.assertEqual(session.parking_space, space)
        self.assertEqual(session.status, SessionStatus.ACTIVE)
        self.assertTrue(session.ticket_number.startswith("TKT-"))
        self.assertIsNone(session.reservation_id)
        self.assertEqual(response.data["data"]["reservation_id"], None)

    def test_rejects_invalid_registration_number(self):
        self.create_space("A-1")

        response = self.enter_vehicle("bad!")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ParkingSession.objects.exists())

    def test_rejects_duplicate_active_vehicle_session(self):
        vehicle = self.create_vehicle()
        space = self.create_space("A-1")
        ParkingSession.objects.create(
            vehicle=vehicle,
            parking_space=space,
            ticket_number="TKT-EXISTING",
        )
        self.create_space("A-2")

        response = self.enter_vehicle("TEST-1234")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(ParkingSession.objects.count(), 1)

    def test_suv_cannot_use_motorcycle_only_space(self):
        self.create_space("M-1", size=1)

        response = self.enter_vehicle("SUV-1234", vehicle_type="SUV")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(ParkingSession.objects.exists())

    def test_electric_vehicle_can_request_charging_space(self):
        self.create_space("A-1")
        charging_space = self.create_space("EV-1", has_charger=True)

        response = self.enter_vehicle(
            "EV-1234",
            vehicle_type=VehicleType.ELECTRIC,
            requires_charging=True,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            ParkingSession.objects.get().parking_space,
            charging_space,
        )

    def test_space_reported_available_can_be_reserved_for_same_times(self):
        space = self.create_space("A-1")
        start_time = timezone.now() + timedelta(hours=2)
        end_time = start_time + timedelta(hours=1)

        availability_response = self.client.get(
            "/api/parking/availability/",
            {
                "vehicle_type": VehicleType.CAR,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
            },
        )

        self.assertEqual(availability_response.status_code, 200)
        self.assertEqual(
            [
                available_space["space_id"]
                for available_space in availability_response.data["data"]["spaces"]
            ],
            [space.space_id],
        )

        reservation_response = self.client.post(
            "/api/parking/reservations/",
            {
                "registration_number": "BOOK-1234",
                "vehicle_type": VehicleType.CAR,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
            },
            format="json",
        )

        self.assertEqual(reservation_response.status_code, 201)
        reservation = Reservation.objects.get()
        self.assertEqual(reservation.parking_space, space)

    def test_full_lot_rejects_entry(self):
        vehicle = self.create_vehicle()
        space = self.create_space("A-1")
        ParkingSession.objects.create(
            vehicle=vehicle,
            parking_space=space,
            ticket_number="TKT-FULL",
        )

        response = self.enter_vehicle("CAR-5678")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(ParkingSession.objects.count(), 1)

    def test_database_rejects_two_active_sessions_for_one_space(self):
        space = self.create_space("A-1")
        first_vehicle = self.create_vehicle("FIRST-1234")
        second_vehicle = self.create_vehicle("SECOND-1234")
        ParkingSession.objects.create(
            vehicle=first_vehicle,
            parking_space=space,
            ticket_number="TKT-FIRST",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ParkingSession.objects.create(
                    vehicle=second_vehicle,
                    parking_space=space,
                    ticket_number="TKT-SECOND",
                )

    @override_settings(PARKING_RESERVATION_EARLY_ARRIVAL_MINUTES=15)
    def test_arrival_before_early_window_is_rejected(self):
        vehicle = self.create_vehicle()
        space = self.create_space("A-1")
        start_time = timezone.now() + timedelta(hours=1)
        reservation = self.create_reservation(
            vehicle,
            space,
            start_time=start_time,
        )

        with patch(
            "parking.services.reservations.timezone.now",
            return_value=start_time - timedelta(minutes=16),
        ):
            result, error = validate_reservation_arrival(
                reservation.pk,
                vehicle,
            )

        self.assertIsNone(result)
        self.assertIn("too early", error)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.CONFIRMED)

    def test_arrival_during_configured_window_checks_in(self):
        vehicle = self.create_vehicle()
        space = self.create_space("A-1")
        start_time = timezone.now() + timedelta(hours=1)
        reservation = self.create_reservation(
            vehicle,
            space,
            start_time=start_time,
        )
        arrival_time = start_time + timedelta(minutes=10)

        with patch(
            "parking.services.reservations.timezone.now",
            return_value=arrival_time,
        ):
            response = self.enter_vehicle(
                vehicle.registration_number,
                vehicle_type=vehicle.vehicle_type,
                reservation_id=reservation.pk,
            )

        self.assertEqual(response.status_code, 201)
        session = ParkingSession.objects.get()
        self.assertEqual(session.reservation_id, reservation.pk)
        self.assertEqual(session.parking_space, space)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.CHECKED_IN)

    def test_arrival_after_grace_expires_reservation(self):
        vehicle = self.create_vehicle()
        space = self.create_space("A-1")
        start_time = timezone.now() - timedelta(hours=1)
        reservation = self.create_reservation(
            vehicle,
            space,
            start_time=start_time,
            grace_period_minutes=10,
        )
        arrival_time = start_time + timedelta(minutes=11)

        with patch(
            "parking.services.reservations.timezone.now",
            return_value=arrival_time,
        ):
            result, error = validate_reservation_arrival(
                reservation.pk,
                vehicle,
            )

        self.assertIsNone(result)
        self.assertIn("expired", error)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.EXPIRED)

    def test_walk_in_cannot_take_a_reserved_space(self):
        space = self.create_space("A-1")
        vehicle = self.create_vehicle()
        self.create_reservation(vehicle, space)

        response = self.enter_vehicle("WALK-1234")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(ParkingSession.objects.exists())

    def test_walk_in_can_use_space_after_reservation_grace_expires(self):
        space = self.create_space("A-1")
        owner = self.create_vehicle()
        start_time = timezone.now() - timedelta(minutes=30)
        reservation = self.create_reservation(
            owner,
            space,
            start_time=start_time,
            grace_period_minutes=15,
        )

        response = self.enter_vehicle("WALK-1234")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            ParkingSession.objects.get().parking_space,
            space,
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.EXPIRED)

    def test_reservation_cannot_be_used_by_another_vehicle(self):
        space = self.create_space("A-1")
        owner = self.create_vehicle("OWNER-1234")
        reservation = self.create_reservation(owner, space)

        response = self.enter_vehicle(
            "OTHER-1234",
            reservation_id=reservation.pk,
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(ParkingSession.objects.exists())


class ReservationExpirationTests(TestCase):
    def test_expiration_is_safe_to_retry(self):
        vehicle = Vehicle.objects.create(
            registration_number="EXPIRE-123",
            vehicle_type=VehicleType.CAR,
        )
        space = ParkingSpace.objects.create(
            space_id="A-1",
            floor_number=0,
            size_capacity=2,
            space_type="STANDARD",
        )
        now = timezone.now()
        reservation = Reservation.objects.create(
            vehicle=vehicle,
            parking_space=space,
            start_time=now - timedelta(minutes=30),
            end_time=now + timedelta(minutes=30),
            grace_period_minutes=15,
            status=ReservationStatus.CONFIRMED,
        )

        self.assertEqual(expire_missed_reservations(now=now), 1)
        self.assertEqual(expire_missed_reservations(now=now), 0)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.EXPIRED)

    def test_expiration_management_command_runs_safely(self):
        vehicle = Vehicle.objects.create(
            registration_number="COMMAND-123",
            vehicle_type=VehicleType.CAR,
        )
        space = ParkingSpace.objects.create(
            space_id="A-1",
            floor_number=0,
            size_capacity=2,
            space_type="STANDARD",
        )
        now = timezone.now()
        Reservation.objects.create(
            vehicle=vehicle,
            parking_space=space,
            start_time=now - timedelta(minutes=30),
            end_time=now + timedelta(minutes=30),
            grace_period_minutes=15,
            status=ReservationStatus.CONFIRMED,
        )
        output = StringIO()

        call_command("expire_reservations", stdout=output)

        self.assertIn("Expired 1 missed reservation(s).", output.getvalue())

    @override_settings(PARKING_RESERVATION_GRACE_MINUTES=7)
    def test_new_reservation_uses_configured_grace_period(self):
        vehicle = Vehicle.objects.create(
            registration_number="GRACE-123",
            vehicle_type=VehicleType.CAR,
        )
        ParkingSpace.objects.create(
            space_id="A-1",
            floor_number=0,
            size_capacity=2,
            space_type="STANDARD",
        )
        start_time = timezone.now() + timedelta(hours=1)

        reservation = create_reservation(
            vehicle,
            start_time,
            start_time + timedelta(hours=1),
        )

        self.assertEqual(reservation.grace_period_minutes, 7)
