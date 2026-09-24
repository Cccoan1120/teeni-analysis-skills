from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from dashboard.forms import EmailAuthenticationForm
from dashboard.views import register


urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            authentication_form=EmailAuthenticationForm,
        ),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/register/", register, name="register"),
    path(
        "accounts/password/change/",
        auth_views.PasswordChangeView.as_view(
            template_name="dashboard/password_change_form.html"
        ),
        name="password_change",
    ),
    path(
        "accounts/password/change/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="dashboard/password_change_done.html"
        ),
        name="password_change_done",
    ),
    path("", include("topics.urls")),
    path("", include("interests.urls")),
    path("", include("dashboard.urls")),
]
