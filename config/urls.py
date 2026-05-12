from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('masters/', include('masters.urls')),
    # ルートはロケーション照会へリダイレクト（将来的にダッシュボードに変更予定）
    path('', RedirectView.as_view(pattern_name='masters:master_inquiry', permanent=False), name='home'),
]
