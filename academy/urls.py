# academy/urls.py

from django.urls import path
from . import calibration_views, views
from .views import (
    AcademyLoginView,
    AcademyLogoutView,
    ProfileUpdateView,
    CustomSignupView,
    delete_account,
)

app_name = "academy"

urlpatterns = [
    # Western blot calibration study (academy/calibration_views.py)
    path('calibrate/', calibration_views.start, name='calibrate'),
    path('calibrate/criteria/', calibration_views.criteria, name='calibrate_criteria'),
    path('calibrate/rate/', calibration_views.rate, name='calibrate_rate'),
    path('calibrate/done/', calibration_views.done, name='calibrate_done'),
    path('calibrate/results/', calibration_views.results, name='calibrate_results'),
    path('calibrate/results.csv', calibration_views.results_csv, name='calibrate_results_csv'),
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
    path('login/', AcademyLoginView.as_view(), name='login'),
    path('logout/', AcademyLogoutView.as_view(), name='logout'),
    path('account/', views.account_view, name='account'),
    path("account/edit/", ProfileUpdateView.as_view(), name="edit_account"),
    path("account/delete/", delete_account, name="delete_account"),
    path('privacy/', views.privacy_policy, name='privacy_policy'),
]
