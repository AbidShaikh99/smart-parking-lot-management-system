
from decimal import Decimal

from django.core.management.base import BaseCommand
from parking.models import PricingRule, VehicleType


class Command(BaseCommand):
    help = "Create default parking pricing rules"

    def handle(self, *args, **options):
        pricing_data = [
            (VehicleType.MOTORCYCLE, "20.00", "20.00", "100.00", "0.00"),
            (VehicleType.CAR, "40.00", "40.00", "200.00", "12.00"),
            (VehicleType.SUV, "60.00", "60.00", "300.00", "12.00"),
            (VehicleType.ELECTRIC, "40.00", "40.00", "200.00", "12.00"),
        ]

        for vehicle_type, first, subsequent, daily_max, charging in pricing_data:
            PricingRule.objects.update_or_create(
                vehicle_type=vehicle_type,
                defaults={
                    "first_hour_rate": Decimal(first),
                    "subsequent_hour_rate": Decimal(subsequent),
                    "daily_maximum": Decimal(daily_max),
                    "lost_ticket_penalty": Decimal("100.00"),
                    "charging_rate_per_kwh": Decimal(charging),
                },
            )
            self.stdout.write(f"Configured pricing for {vehicle_type}")

        self.stdout.write(self.style.SUCCESS("All pricing rules configured."))
