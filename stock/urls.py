from django.urls import path

from . import views

app_name = 'stock'

urlpatterns = [
    path('', views.StockInquiryView.as_view(), name='inquiry'),
]
