from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('errors/', views.ErrorLogInquiryView.as_view(), name='error_log_inquiry'),
    path('errors/<int:pk>/', views.ErrorLogDetailView.as_view(), name='error_log_detail'),
]
