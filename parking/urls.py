
from django.urls import path

from .views import (
    ParkingSpaceListCreateView,
    ParkingAvailabilityView,
    VehicleEntryView,
    ParkingReservationView,
    ParkingReservationCancelView,
    ParkingFeeView,
    VehicleExitView,
    ParkingTicketDetailView,
    ParkingRevenueReportView,
    ParkingOccupancyReportView
)

urlpatterns = [
    path("parking/spaces/",ParkingSpaceListCreateView.as_view(),name="parking-space-list-create"),
    path("parking/availability/",ParkingAvailabilityView.as_view(),name="parking-availability"),
    path("vehicles/entry/",VehicleEntryView.as_view(),name="vehicle-entry"),
    path("parking/reservations/",ParkingReservationView.as_view(),name="parking-reservations"),
    path("parking/reservations/<int:reservation_id>/",ParkingReservationCancelView.as_view(),name="parking-reservation-cancel"),
    path("parking/fees/<str:ticket_number>/",ParkingFeeView.as_view(),name="parking-fee"),
    path("vehicles/exit/",VehicleExitView.as_view(),name="vehicle-exit"),
    path("parking/tickets/<str:ticket_number>/",ParkingTicketDetailView.as_view(),name="parking-ticket-detail"),
    path("parking/reports/revenue/",ParkingRevenueReportView.as_view(),name="parking-revenue-report"),
    path("parking/reports/occupancy/",ParkingOccupancyReportView.as_view(),name="parking-occupancy-report"),
]

