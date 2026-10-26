from django.core.management.base import BaseCommand

from parking.services.reservations import expire_missed_reservations


class Command(BaseCommand):
    help = "Expire confirmed reservations whose arrival grace period has ended."

    def handle(self, *args, **options):
        expired_count = expire_missed_reservations()
        self.stdout.write(
            self.style.SUCCESS(
                f"Expired {expired_count} missed reservation(s)."
            )
        )
