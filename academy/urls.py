# academy/urls.py

from django.shortcuts import redirect
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

    # The in-app AI assistant (tutor and database search) was retired on
    # 14 Oct 2026: the tutor had been offline and the search was linked from no
    # page. A bookmark goes to connecting your own AI, which is what the
    # assistant itself offered once its daily limit ran out. No url name, so no
    # template can link to the old address.
    path('assistant/<str:mode>/',
         lambda request, mode: redirect('connect_your_ai', permanent=True)),

    # Accounts
    path('signup/', CustomSignupView.as_view(), name='signup'),
    path('login/', AcademyLoginView.as_view(), name='login'),
    path('logout/', AcademyLogoutView.as_view(), name='logout'),
    path('account/', views.account_view, name='account'),
    path("account/edit/", ProfileUpdateView.as_view(), name="edit_account"),
    path("account/delete/", delete_account, name="delete_account"),
    path('privacy/', views.privacy_policy, name='privacy_policy'),
]
