# academy/forms.py

from django import forms
from django.contrib.auth import get_user_model
from django.utils.safestring import mark_safe
from allauth.account.forms import LoginForm, SignupForm

User = get_user_model()


class CustomLoginForm(LoginForm):
    """The sign-in field, labelled with what to type in it.

    ``ACCOUNT_LOGIN_METHODS`` is ``{'username', 'email'}``, and allauth labels
    that combination with the literal word **"Login"** — a label that names the
    page rather than the field. It does know the right words: it puts "Username
    or email" in the *placeholder*, which vanishes the moment somebody types and
    is gone entirely for anyone returning to a browser-filled form.

    So the label is taken from allauth's own placeholder rather than spelled
    again here, and only for the multi-method case — narrow the setting to one
    method and allauth already labels the field "Username" or "Email address"
    correctly, and this must not talk over it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from allauth.account import app_settings as allauth_settings

        if len(allauth_settings.LOGIN_METHODS) > 1:
            self.fields["login"].label = self._get_login_field_placeholder()

class CustomSignupForm(SignupForm):
    first_name = forms.CharField(
        max_length=30,
        label="First Name",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    last_name = forms.CharField(
        max_length=30,
        label="Last Name",
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    privacy_consent = forms.BooleanField(
        required=True,
        label=mark_safe('I agree to the <a href="/privacy-policy/" target="_blank">Privacy Policy</a>'),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )

    def clean_privacy_consent(self):
        consent = self.cleaned_data.get("privacy_consent")
        if not consent:
            raise forms.ValidationError("You must agree to the Privacy Policy.")
        return consent

    def save(self, request):
        user = super().save(request)
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.save()
        return user


class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name':  forms.TextInput(attrs={'class': 'form-control'}),
        }
