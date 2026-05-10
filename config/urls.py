from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('masters/', include('masters.urls')),
    # ルートは倉庫一覧へリダイレクト（将来的にダッシュボードに変更予定）
    path('', RedirectView.as_view(pattern_name='masters:warehouse_list', permanent=False), name='home'),
]
