from django.urls import path

from routes.views import RoutePlanView

app_name = "routes"

urlpatterns = [
    path("routes/", RoutePlanView.as_view(), name="route-plan"),
]
