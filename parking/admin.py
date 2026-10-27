from django.contrib import admin

from .models import ParkingHoliday, PricingRule


admin.site.register((PricingRule, ParkingHoliday))
