# academy/urls.py

from django.urls import path
from . import views
from allauth.account.views import LoginView
from .views import (
    AcademyLogoutView,
    ProfileUpdateView,
    CustomSignupView,
    delete_account,
)

app_name = "academy"

urlpatterns = [
    # Home & Lessons
    path('', views.home, name='academy_home'),
    path('lesson/<int:lesson_id>/', views.lesson_detail, name='lesson_detail'),
    path('lesson/mark_section/', views.mark_section_viewed, name='mark_section'),

    # Quizzes & Certificates
    path('quiz/<int:quiz_id>/', views.quiz_view, name='quiz'),
    path('certificate/<int:cert_id>/', views.certificate_view, name='certificate'),
    path('certificate/<int:cert_id>/pdf/', views.generate_pdf, name='generate_pdf'),
    path('certificate/verify/<uuid:code>/', views.certificate_verify,
         name='certificate_verify'),

    # In-app AI assistant (behind the login) — hosted chat surfaces
    path('assistant/<str:mode>/', views.assistant_page, name='assistant'),
    path('assistant-api/', views.assistant_api, name='assistant_api'),

    # Accounts
    path('signup/', CustomSignupView.as_view(), name='signup'),
    path('login/', LoginView.as_view(template_name='academy/login.html'), name='login'),
    path('logout/', AcademyLogoutView.as_view(), name='logout'),
    path('account/', views.account_view, name='account'),
    path("account/edit/", ProfileUpdateView.as_view(), name="edit_account"),
    path("account/delete/", delete_account, name="delete_account"),
    path('privacy/', views.privacy_policy, name='privacy_policy'),
]
