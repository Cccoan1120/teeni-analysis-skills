import hashlib
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import LoginThrottle


def _identifier_hash(value: str) -> str:
    normalized = value.strip().casefold()
    return hashlib.sha256(f"{settings.SECRET_KEY}\0{normalized}".encode("utf-8")).hexdigest()


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="邮箱",
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "autofocus": True, "placeholder": "name@company.com"}
        ),
    )
    password = forms.CharField(
        label="密码",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def clean(self):
        identifier = str(self.data.get("username", ""))
        key = _identifier_hash(identifier)
        throttle = LoginThrottle.objects.filter(identifier_hash=key).first()
        now = timezone.now()
        if throttle and throttle.locked_until and throttle.locked_until > now:
            raise ValidationError("登录尝试过多，请稍后再试。", code="login_locked")

        try:
            cleaned = super().clean()
        except ValidationError:
            throttle, _ = LoginThrottle.objects.get_or_create(identifier_hash=key)
            throttle.failures += 1
            if throttle.failures >= settings.TEENI_LOGIN_MAX_FAILURES:
                throttle.locked_until = now + timedelta(minutes=settings.TEENI_LOGIN_LOCK_MINUTES)
            throttle.save(update_fields=["failures", "locked_until", "updated_at"])
            raise

        LoginThrottle.objects.filter(identifier_hash=key).delete()
        return cleaned


class EmailRegistrationForm(forms.Form):
    email = forms.EmailField(
        label="邮箱",
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "autofocus": True, "placeholder": "name@company.com"}
        ),
    )
    password1 = forms.CharField(
        label="密码",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="确认密码",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        user_model = get_user_model()
        if user_model.objects.filter(username__iexact=email).exists() or user_model.objects.filter(email__iexact=email).exists():
            raise ValidationError("该邮箱已注册。", code="duplicate_email")
        return email

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", ValidationError("两次输入的密码不一致。", code="password_mismatch"))
            return cleaned
        return cleaned

    def save(self):
        email = self.cleaned_data["email"]
        return get_user_model().objects.create_user(
            username=email,
            email=email,
            password=self.cleaned_data["password1"],
        )
