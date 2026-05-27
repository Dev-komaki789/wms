import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, ListView, UpdateView, TemplateView, View
from django.views.generic.edit import FormView

from core.models import ErrorLog
from core.utils import PAGE_SIZE, GetPageMixin, apply_ordering, paginate

from .csv_io import DELIMITERS, MASTER_SPECS, build_csv, column_guide, import_csv
from .forms import (
    AreaForm, CategoryForm, CustomerForm, LocationBulkCreateForm, LocationForm,
    ManufacturerForm, ProductForm, SkuForm, SupplierForm,
)
from .models import (
    Area, Category, Customer, Location, Manufacturer, Product, Sku, Supplier, Warehouse,
)
from .utils import get_current_warehouse


class HomeView(LoginRequiredMixin, TemplateView):
    """メニュー画面（ホーム）。

    全画面のハブ。KPI サマリー + 機能カテゴリ別のカードグリッドで構成する。
    """

    template_name = 'a/home.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['kpi'] = {
            'product_count': Product.objects.count(),
            'sku_count': Sku.objects.count(),
            'manufacturer_count': Manufacturer.objects.count(),
            'customer_count': Customer.objects.count(),
        }
        return ctx


class CurrentWarehouseScopedMixin:
    """List/Update/Delete ビューで「現在ログイン中の倉庫のレコードのみ」に絞り込む。

    他倉庫のレコードへの URL アクセスは 404 になる。
    検索系（MasterInquiryView）は別のポリシーで複数倉庫を扱うため、このミックスインを使わない。
    """

    warehouse_lookup = 'warehouse'  # FK 名を変えるサブクラス用

    def get_queryset(self):
        qs = super().get_queryset()
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(**{self.warehouse_lookup: wh})
        return qs


class LocationFormContextMixin:
    """ロケーションフォームに「区分→エリア」連動と「区分→セグメント」連動の JSON を渡す。

    エリア一覧は現在の倉庫のものに限定する（他倉庫のエリアにロケーションを登録できないようにする）。
    """

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        current_wh = get_current_warehouse(self.request)
        # 区分ごとのエリア一覧（現在倉庫スコープ）
        areas_by_type = {}
        area_qs = (
            Area.objects.select_related('warehouse')
            .filter(is_active=True)
            .order_by('warehouse__warehouse_code', 'area_code')
        )
        if current_wh is not None:
            area_qs = area_qs.filter(warehouse=current_wh)
        for area in area_qs:
            areas_by_type.setdefault(area.location_type, []).append({
                'id': area.pk,
                'area_code': area.area_code,
                'area_name': area.area_name or '',
                'warehouse_code': area.warehouse.warehouse_code,
                'warehouse_name': area.warehouse.warehouse_name,
                'location_type': area.location_type,
            })
        ctx['areas_by_type_json'] = json.dumps(areas_by_type, ensure_ascii=False)

        # 区分ごとのセグメント定義
        segments_by_type = {
            t.value: [
                {'name': name, 'label': label, 'digits': digits}
                for name, label, digits in Area.LOCATION_CODE_SEGMENTS.get(t, [])
            ]
            for t in Area.LocationType
        }
        ctx['segments_by_type_json'] = json.dumps(segments_by_type, ensure_ascii=False)

        return ctx


class ProtectedErrorMixin:
    """DeleteView で PROTECT FK エラーをキャッチし、メッセージで通知して一覧に戻す。"""

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except ProtectedError as e:
            count = len(e.protected_objects)
            sample = ', '.join(str(o) for o in list(e.protected_objects)[:3])
            messages.error(
                request,
                f'削除できません: 関連データが {count} 件紐づいています（{sample}{"..." if count > 3 else ""}）。'
                f'先に関連データを削除または別の親に付け替えてください。',
            )
            return HttpResponseRedirect(self.success_url)


class FilterableListMixin(GetPageMixin):
    """一覧 ListView に「キーワード＋ステータス」の絞り込みを付与する。

    サブクラスで search_fields にキーワード照合対象のフィールド名を列挙する。
    一覧は常に表示し、検索条件があれば結果を絞り込む（件数の小さい業務マスタ
    向け。商品・SKU のような検索-first 画面とは別方針）。
    """

    search_fields = []
    paginate_by = PAGE_SIZE
    # 見出しクリックの並び替え（任意）。sortable を定義したサブクラスで有効化。
    sortable = None
    default_sort = None
    default_dir = 'asc'

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.GET.get('q', '').strip()
        status = self.request.GET.get('status', '')
        if q and self.search_fields:
            cond = Q()
            for field in self.search_fields:
                cond |= Q(**{f'{field}__icontains': q})
            qs = qs.filter(cond)
        if status == 'active':
            qs = qs.filter(is_active=True)
        elif status == 'inactive':
            qs = qs.filter(is_active=False)
        if self.sortable:
            qs, self._sort, self._dir = apply_ordering(
                self.request, qs, self.sortable,
                self.default_sort, self.default_dir)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['filters'] = {
            'q': self.request.GET.get('q', '').strip(),
            'status': self.request.GET.get('status', ''),
        }
        if self.sortable:
            ctx['sort'] = getattr(self, '_sort', self.default_sort)
            ctx['dir'] = getattr(self, '_dir', self.default_dir)
        return ctx


# ---- Master Inquiry (combined Area + Location overview) ----

class MasterInquiryView(LoginRequiredMixin, TemplateView):
    """エリアとロケーションを1画面で照会できる入口画面。

    検索フィルタ・サマリー統計・テーブルを各セクション（エリア / ロケーション）に持つ。
    フィルタは GET パラメータで受け、`area_*` / `loc_*` プレフィックスで namespace 分離。
    """

    template_name = 'a/masters/master_inquiry.html'

    SEARCH_KEYS = ('loc_q', 'loc_warehouse', 'loc_area', 'loc_type', 'loc_status')

    SORTABLE = {
        'warehouse': ['warehouse__warehouse_code', 'area__area_code', 'location_code'],
        'area': ['area__area_code'],
        'code': ['location_code'],
        'type': ['area__location_type'],
        'name': ['location_name'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        # 検索ボタンが押されたかを判定（GET にいずれかのキーが含まれていれば「検索済み」）
        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        loc_q = g.get('loc_q', '').strip()
        loc_warehouse = g.get('loc_warehouse', '')
        loc_area = g.get('loc_area', '')
        loc_type_f = g.get('loc_type', '')
        loc_status = g.get('loc_status', '')

        if searched:
            loc_qs = Location.objects.select_related('warehouse', 'area')
            if loc_q:
                loc_qs = loc_qs.filter(location_code__icontains=loc_q)
            if loc_warehouse:
                loc_qs = loc_qs.filter(warehouse_id=loc_warehouse)
            if loc_area:
                loc_qs = loc_qs.filter(area_id=loc_area)
            if loc_type_f:
                loc_qs = loc_qs.filter(area__location_type=loc_type_f)
            if loc_status == 'active':
                loc_qs = loc_qs.filter(is_active=True)
            elif loc_status == 'inactive':
                loc_qs = loc_qs.filter(is_active=False)
            loc_qs, ctx['sort'], ctx['dir'] = apply_ordering(
                self.request, loc_qs, self.SORTABLE, 'warehouse', 'asc')
            page = paginate(self.request, loc_qs)
            ctx['locations'] = page
            ctx['page_obj'] = page

            # サマリー統計は検索結果ベース（フィルタ後の件数を集計）
            ctx['loc_stats'] = {
                'total': loc_qs.count(),
                'storage': loc_qs.filter(area__location_type=Area.LocationType.STORAGE).count(),
                'large_item': loc_qs.filter(area__location_type=Area.LocationType.LARGE_ITEM).count(),
                'active': loc_qs.filter(is_active=True).count(),
                'inactive': loc_qs.filter(is_active=False).count(),
            }
        else:
            ctx['locations'] = Location.objects.none()
            ctx['loc_stats'] = None

        # ---- フィルタ選択肢 ----
        ctx['warehouses'] = Warehouse.objects.order_by('warehouse_code')
        ctx['all_areas'] = (
            Area.objects.select_related('warehouse')
            .order_by('warehouse__warehouse_code', 'area_code')
        )
        ctx['location_types'] = Area.LocationType.choices

        # ---- フィルタ値（フォームに前回値を残すため） ----
        ctx['filters'] = {
            'loc_q': loc_q,
            'loc_warehouse': loc_warehouse,
            'loc_area': loc_area,
            'loc_type': loc_type_f,
            'loc_status': loc_status,
        }

        return ctx


# ---- Area ----

class AreaListView(GetPageMixin, CurrentWarehouseScopedMixin, LoginRequiredMixin, ListView):
    model = Area
    template_name = 'a/masters/area_list.html'
    context_object_name = 'areas'
    paginate_by = PAGE_SIZE

    SORTABLE = {
        'warehouse': ['warehouse__warehouse_code', 'area_code'],
        'code': ['area_code'],
        'name': ['area_name'],
        'type': ['location_type'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }

    def get_queryset(self):
        qs = super().get_queryset().select_related('warehouse')
        qs, self._sort, self._dir = apply_ordering(
            self.request, qs, self.SORTABLE, 'warehouse', 'asc')
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['sort'] = getattr(self, '_sort', 'warehouse')
        ctx['dir'] = getattr(self, '_dir', 'asc')
        return ctx


class AreaCreateView(LoginRequiredMixin, CreateView):
    model = Area
    form_class = AreaForm
    template_name = 'a/masters/area_form.html'
    success_url = reverse_lazy('masters:area_list')


class AreaUpdateView(CurrentWarehouseScopedMixin, LoginRequiredMixin, UpdateView):
    model = Area
    form_class = AreaForm
    template_name = 'a/masters/area_form.html'
    success_url = reverse_lazy('masters:area_list')

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse')


class AreaDeleteView(CurrentWarehouseScopedMixin, LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Area
    template_name = 'a/masters/area_confirm_delete.html'
    success_url = reverse_lazy('masters:area_list')

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse')


# ---- Location ----

class LocationListView(GetPageMixin, CurrentWarehouseScopedMixin, LoginRequiredMixin, ListView):
    model = Location
    template_name = 'a/masters/location_list.html'
    context_object_name = 'locations'
    paginate_by = PAGE_SIZE

    SORTABLE = {
        'warehouse': ['warehouse__warehouse_code', 'area__area_code', 'location_code'],
        'area': ['area__area_code'],
        'code': ['location_code'],
        'name': ['location_name'],
        'type': ['area__location_type'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }

    def get_queryset(self):
        qs = super().get_queryset().select_related('warehouse', 'area')
        qs, self._sort, self._dir = apply_ordering(
            self.request, qs, self.SORTABLE, 'warehouse', 'asc')
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['sort'] = getattr(self, '_sort', 'warehouse')
        ctx['dir'] = getattr(self, '_dir', 'asc')
        return ctx


class LocationCreateView(LocationFormContextMixin, LoginRequiredMixin, CreateView):
    model = Location
    form_class = LocationForm
    template_name = 'a/masters/location_form.html'
    success_url = reverse_lazy('masters:master_inquiry')


class LocationBulkCreateView(LocationFormContextMixin, LoginRequiredMixin, FormView):
    """ロケーション一括登録画面（範囲指定で棚番を自動生成）。

    区分・エリアと各セグメントの開始〜終了範囲を指定 → 全組み合わせの棚番を
    生成して bulk_create する。同一倉庫に既存の棚番はスキップし、登録件数・
    スキップ件数を結果メッセージで報告する。
    """

    form_class = LocationBulkCreateForm
    template_name = 'a/masters/location_bulk_form.html'
    success_url = reverse_lazy('masters:master_inquiry')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['max_bulk'] = LocationBulkCreateForm.MAX_BULK
        return ctx

    def form_valid(self, form):
        area = form._area
        codes = form._codes
        is_active = form.cleaned_data['is_active']
        with transaction.atomic():
            # 同一倉庫内で既に存在する棚番はスキップする
            existing = set(
                Location.objects
                .filter(warehouse=area.warehouse, location_code__in=codes)
                .values_list('location_code', flat=True)
            )
            to_create = [c for c in codes if c not in existing]
            Location.objects.bulk_create([
                Location(
                    warehouse=area.warehouse, area=area,
                    location_code=code, location_name='', is_active=is_active,
                )
                for code in to_create
            ])
        created = len(to_create)
        skipped = len(existing)
        if created:
            msg = f'{created} 件のロケーションを一括登録しました。'
            if skipped:
                msg += f'（既存の {skipped} 件はスキップ）'
            messages.success(self.request, msg)
        else:
            messages.info(
                self.request,
                f'登録対象がありませんでした'
                f'（生成した {skipped} 件はすべて既存の棚番です）。',
            )
        return super().form_valid(form)


class LocationUpdateView(LocationFormContextMixin, CurrentWarehouseScopedMixin, LoginRequiredMixin, UpdateView):
    model = Location
    form_class = LocationForm
    template_name = 'a/masters/location_form.html'
    success_url = reverse_lazy('masters:master_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse', 'area')


class LocationDeleteView(CurrentWarehouseScopedMixin, LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Location
    template_name = 'a/masters/location_confirm_delete.html'
    success_url = reverse_lazy('masters:master_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse', 'area')


# ---- Category ----

class CategoryListView(GetPageMixin, LoginRequiredMixin, ListView):
    """カテゴリ一覧。

    検索条件が無いときはツリー（深さ優先、depth_attr / children_count 付き）。
    キーワード／ステータスで絞り込んだときは、一致カテゴリのフラットな結果リスト
    （breadcrumb_attr 付き）を表示する。ツリーは階層が深いと一致箇所が埋もれる
    ため、検索時はフラット表示に切り替える。
    """

    model = Category
    template_name = 'a/masters/category_list.html'
    context_object_name = 'categories'

    def get_paginate_by(self, queryset):
        # ツリー表示はページ送りすると階層が分断されるため、
        # 検索（フラットな結果リスト）のときだけページネーションする。
        return PAGE_SIZE if getattr(self, '_searched', False) else None

    def get_queryset(self):
        g = self.request.GET
        q = g.get('q', '').strip()
        status = g.get('status', '')
        self._searched = bool(q) or bool(status)

        all_cats = list(
            Category.objects.select_related('parent').order_by('sort_order', 'category_code')
        )
        by_parent = {}
        for c in all_cats:
            by_parent.setdefault(c.parent_id, []).append(c)
        for c in all_cats:
            c.children_count = len(by_parent.get(c.pk, []))

        if not self._searched:
            # ツリー（深さ優先）
            flat = []

            def walk(parent_id, depth):
                for c in by_parent.get(parent_id, []):
                    c.depth_attr = depth
                    flat.append(c)
                    walk(c.pk, depth + 1)

            walk(None, 0)
            return flat

        # 絞り込み: 一致カテゴリのフラットな結果（breadcrumb 付き）
        by_pk = {c.pk: c for c in all_cats}

        def breadcrumb_of(cat):
            names, cur = [], cat
            while cur is not None:
                names.insert(0, cur.category_name)
                cur = by_pk.get(cur.parent_id)
            return ' › '.join(names)

        result = all_cats
        if q:
            ql = q.lower()
            result = [
                c for c in result
                if ql in c.category_code.lower() or ql in c.category_name.lower()
            ]
        if status == 'active':
            result = [c for c in result if c.is_active]
        elif status == 'inactive':
            result = [c for c in result if not c.is_active]
        for c in result:
            c.breadcrumb_attr = breadcrumb_of(c)
        return result

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['searched'] = getattr(self, '_searched', False)
        ctx['filters'] = {
            'q': self.request.GET.get('q', '').strip(),
            'status': self.request.GET.get('status', ''),
        }
        # 階層ドリルダウン（コンボボックス）用: 全カテゴリの最小情報を JSON で渡す
        cats = list(Category.objects.order_by('sort_order', 'category_code'))
        by_pk = {c.pk: c for c in cats}

        def depth_of(cat):
            d, p = 0, cat.parent_id
            while p is not None and d < 20:
                d += 1
                p = by_pk[p].parent_id if p in by_pk else None
            return d

        nav = [
            {
                'pk': c.pk, 'name': c.category_name, 'code': c.category_code,
                'parent': c.parent_id, 'depth': depth_of(c),
                'is_leaf': c.is_leaf,
            }
            for c in cats
        ]
        ctx['category_nav_json'] = json.dumps(nav, ensure_ascii=False)
        ctx['level_labels_json'] = json.dumps(
            Category.LEVEL_LABELS, ensure_ascii=False)
        return ctx


class CategoryFormContextMixin:
    """カテゴリフォームに各親候補の階層情報（breadcrumb + depth）を JSON で渡す。"""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        cats = list(
            Category.objects.select_related('parent').order_by('sort_order', 'category_code')
        )

        def ancestors_path(c):
            path, cur = [], c
            while cur is not None:
                path.insert(0, cur.category_name)
                cur = cur.parent
            return path

        parents_info = {}
        for c in cats:
            parents_info[str(c.pk)] = {
                'depth': c.depth,
                'breadcrumb': ' › '.join(ancestors_path(c)),
                'code': c.category_code,
            }
        ctx['parents_info_json'] = json.dumps(parents_info, ensure_ascii=False)
        ctx['max_depth'] = Category.MAX_DEPTH
        ctx['level_labels_json'] = json.dumps(Category.LEVEL_LABELS, ensure_ascii=False)
        return ctx


class CategoryCreateView(CategoryFormContextMixin, LoginRequiredMixin, CreateView):
    model = Category
    form_class = CategoryForm
    template_name = 'a/masters/category_form.html'

    def get_initial(self):
        initial = super().get_initial()
        # URL クエリ ?parent=<pk> で親をプリセット（一覧の「+ 子」ボタン用）
        parent_pk = self.request.GET.get('parent')
        if parent_pk:
            try:
                initial['parent'] = Category.objects.get(pk=parent_pk).pk
            except Category.DoesNotExist:
                pass
        return initial

    def get_success_url(self):
        # 登録後は一覧で新カテゴリにハイライト + 祖先自動展開
        return f"{reverse('masters:category_list')}?highlight={self.object.pk}"


class CategoryUpdateView(CategoryFormContextMixin, LoginRequiredMixin, UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = 'a/masters/category_form.html'

    def get_success_url(self):
        return f"{reverse('masters:category_list')}?highlight={self.object.pk}"


class CategoryDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Category
    template_name = 'a/masters/category_confirm_delete.html'
    success_url = reverse_lazy('masters:category_list')


# ---- Manufacturer ----

class ManufacturerListView(FilterableListMixin, LoginRequiredMixin, ListView):
    model = Manufacturer
    template_name = 'a/masters/manufacturer_list.html'
    context_object_name = 'manufacturers'
    ordering = ['manufacturer_code']
    search_fields = ['manufacturer_code', 'manufacturer_name']
    sortable = {
        'code': ['manufacturer_code'],
        'name': ['manufacturer_name'],
        'url': ['url'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }
    default_sort = 'code'


class ManufacturerCreateView(LoginRequiredMixin, CreateView):
    model = Manufacturer
    form_class = ManufacturerForm
    template_name = 'a/masters/manufacturer_form.html'
    success_url = reverse_lazy('masters:manufacturer_list')


class ManufacturerUpdateView(LoginRequiredMixin, UpdateView):
    model = Manufacturer
    form_class = ManufacturerForm
    template_name = 'a/masters/manufacturer_form.html'
    success_url = reverse_lazy('masters:manufacturer_list')


class ManufacturerDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Manufacturer
    template_name = 'a/masters/manufacturer_confirm_delete.html'
    success_url = reverse_lazy('masters:manufacturer_list')


# ---- Product ----

class ProductInquiryView(LoginRequiredMixin, TemplateView):
    """商品の照会＋一覧。検索-first パターン（エリア・ロケーション照会と同じ規約）。

    商品は単一倉庫運用のスコープに乗らない（マスタは全社共通）ため、倉庫スコープは適用しない。
    """

    template_name = 'a/masters/product_inquiry.html'

    SEARCH_KEYS = ('p_q', 'p_category', 'p_manufacturer', 'p_status')

    SORTABLE = {
        'code': ['product_code'],
        'name': ['product_name'],
        'category': ['category__category_name'],
        'maker': ['manufacturer__manufacturer_name'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        p_q = g.get('p_q', '').strip()
        p_category = g.get('p_category', '')
        p_manufacturer = g.get('p_manufacturer', '')
        p_status = g.get('p_status', '')

        if searched:
            qs = Product.objects.select_related('category', 'manufacturer')
            if p_q:
                qs = qs.filter(Q(product_code__icontains=p_q) | Q(product_name__icontains=p_q))
            if p_category:
                qs = qs.filter(category_id=p_category)
            if p_manufacturer:
                # メーカー名 or メーカーコードの部分一致（登録数が多い前提でテキスト入力）
                qs = qs.filter(
                    Q(manufacturer__manufacturer_name__icontains=p_manufacturer)
                    | Q(manufacturer__manufacturer_code__icontains=p_manufacturer)
                )
            if p_status == 'active':
                qs = qs.filter(is_active=True)
            elif p_status == 'inactive':
                qs = qs.filter(is_active=False)
            qs, ctx['sort'], ctx['dir'] = apply_ordering(
                self.request, qs, self.SORTABLE, 'code', 'asc')
            page = paginate(self.request, qs)
            ctx['products'] = page
            ctx['page_obj'] = page
            ctx['p_stats'] = {
                'total': qs.count(),
                'active': qs.filter(is_active=True).count(),
                'inactive': qs.filter(is_active=False).count(),
                'no_manufacturer': qs.filter(manufacturer__isnull=True).count(),
            }
        else:
            ctx['products'] = Product.objects.none()
            ctx['p_stats'] = None

        # フィルタ選択肢
        ctx['leaf_categories'] = (
            Category.objects.filter(is_leaf=True)
            .select_related('parent', 'parent__parent', 'parent__parent__parent')
            .order_by('category_code')
        )
        ctx['filters'] = {
            'p_q': p_q,
            'p_category': p_category,
            'p_manufacturer': p_manufacturer,
            'p_status': p_status,
        }
        return ctx


class ProductCreateView(LoginRequiredMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = 'a/masters/product_form.html'

    def get_success_url(self):
        return reverse('masters:product_inquiry')


class ProductUpdateView(LoginRequiredMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'a/masters/product_form.html'

    def get_queryset(self):
        return super().get_queryset().select_related('category', 'manufacturer')

    def get_success_url(self):
        return reverse('masters:product_inquiry')


class ProductDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Product
    template_name = 'a/masters/product_confirm_delete.html'
    success_url = reverse_lazy('masters:product_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('category', 'manufacturer')


# ---- SKU ----

class SkuInquiryView(LoginRequiredMixin, TemplateView):
    """SKU の照会＋一覧。検索-first パターン（商品照会と同じ規約）。"""

    template_name = 'a/masters/sku_inquiry.html'

    SEARCH_KEYS = ('s_q', 's_product', 's_picking', 's_status')

    SORTABLE = {
        'code': ['sku_code'],
        'product': ['product__product_name'],
        'jan': ['jan_code'],
        'size': ['size_info'],
        'qpu': ['quantity_per_unit'],
        'ptype': ['picking_type'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        s_q = g.get('s_q', '').strip()
        s_product = g.get('s_product', '').strip()
        s_picking = g.get('s_picking', '')
        s_status = g.get('s_status', '')

        if searched:
            qs = Sku.objects.select_related(
                'product', 'product__category', 'product__manufacturer'
            )
            if s_q:
                qs = qs.filter(Q(sku_code__icontains=s_q) | Q(jan_code__icontains=s_q))
            if s_product:
                qs = qs.filter(
                    Q(product__product_name__icontains=s_product)
                    | Q(product__product_code__icontains=s_product)
                )
            if s_picking:
                qs = qs.filter(picking_type=s_picking)
            if s_status == 'active':
                qs = qs.filter(is_active=True)
            elif s_status == 'inactive':
                qs = qs.filter(is_active=False)
            qs, ctx['sort'], ctx['dir'] = apply_ordering(
                self.request, qs, self.SORTABLE, 'code', 'asc')
            page = paginate(self.request, qs)
            ctx['skus'] = page
            ctx['page_obj'] = page
            ctx['s_stats'] = {
                'total': qs.count(),
                'active': qs.filter(is_active=True).count(),
                'inactive': qs.filter(is_active=False).count(),
                'total_pick': qs.filter(picking_type=Sku.PickingType.TOTAL).count(),
                'order_pick': qs.filter(picking_type=Sku.PickingType.ORDER).count(),
            }
        else:
            ctx['skus'] = Sku.objects.none()
            ctx['s_stats'] = None

        ctx['picking_types'] = Sku.PickingType.choices
        ctx['filters'] = {
            's_q': s_q,
            's_product': s_product,
            's_picking': s_picking,
            's_status': s_status,
        }
        return ctx


class SkuCreateView(LoginRequiredMixin, CreateView):
    model = Sku
    form_class = SkuForm
    template_name = 'a/masters/sku_form.html'
    success_url = reverse_lazy('masters:sku_inquiry')


class SkuUpdateView(LoginRequiredMixin, UpdateView):
    model = Sku
    form_class = SkuForm
    template_name = 'a/masters/sku_form.html'
    success_url = reverse_lazy('masters:sku_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('product')


class SkuDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Sku
    template_name = 'a/masters/sku_confirm_delete.html'
    success_url = reverse_lazy('masters:sku_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('product')


# ---- Supplier ----

class SupplierListView(FilterableListMixin, LoginRequiredMixin, ListView):
    model = Supplier
    template_name = 'a/masters/supplier_list.html'
    context_object_name = 'suppliers'
    ordering = ['supplier_code']
    search_fields = ['supplier_code', 'supplier_name', 'contact_person']
    sortable = {
        'code': ['supplier_code'],
        'name': ['supplier_name'],
        'contact': ['contact_person'],
        'phone': ['phone_number'],
        'email': ['email'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }
    default_sort = 'code'


class SupplierCreateView(LoginRequiredMixin, CreateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'a/masters/supplier_form.html'
    success_url = reverse_lazy('masters:supplier_list')


class SupplierUpdateView(LoginRequiredMixin, UpdateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'a/masters/supplier_form.html'
    success_url = reverse_lazy('masters:supplier_list')


class SupplierDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Supplier
    template_name = 'a/masters/supplier_confirm_delete.html'
    success_url = reverse_lazy('masters:supplier_list')


# ---- Customer ----

class CustomerListView(FilterableListMixin, LoginRequiredMixin, ListView):
    model = Customer
    template_name = 'a/masters/customer_list.html'
    context_object_name = 'customers'
    ordering = ['customer_code']
    search_fields = ['customer_code', 'customer_name']
    sortable = {
        'code': ['customer_code'],
        'name': ['customer_name'],
        'ctype': ['customer_type'],
        'industry': ['industry_type'],
        'address': ['address'],
        'status': ['is_active'],
        'updated': ['updated_at'],
    }
    default_sort = 'code'

    def get_queryset(self):
        qs = super().get_queryset()
        ctype = self.request.GET.get('customer_type', '')
        if ctype.isdigit():
            qs = qs.filter(customer_type=int(ctype))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['filters']['customer_type'] = self.request.GET.get('customer_type', '')
        ctx['customer_type_choices'] = Customer.Type.choices
        return ctx


class CustomerCreateView(LoginRequiredMixin, CreateView):
    model = Customer
    form_class = CustomerForm
    template_name = 'a/masters/customer_form.html'
    success_url = reverse_lazy('masters:customer_list')


class CustomerUpdateView(LoginRequiredMixin, UpdateView):
    model = Customer
    form_class = CustomerForm
    template_name = 'a/masters/customer_form.html'
    success_url = reverse_lazy('masters:customer_list')


class CustomerDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Customer
    template_name = 'a/masters/customer_confirm_delete.html'
    success_url = reverse_lazy('masters:customer_list')


# ---- API (AJAX) ----

class CustomerSearchAPIView(LoginRequiredMixin, View):
    """顧客検索 API（AJAX 用）。

    出荷指示登録画面などで顧客マスタを検索選択するためのエンドポイント。
    顧客コード / 顧客名の部分一致で検索し最大 LIMIT 件返す（有効のみ）。
    """

    LIMIT = 30

    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        qs = Customer.objects.filter(is_active=True)
        if q:
            qs = qs.filter(
                Q(customer_code__icontains=q) | Q(customer_name__icontains=q)
            )
        qs = qs.order_by('customer_code')[: self.LIMIT]
        return JsonResponse({
            'results': [
                {
                    'id': c.pk,
                    'customer_code': c.customer_code,
                    'customer_name': c.customer_name,
                }
                for c in qs
            ]
        })


class SupplierSearchAPIView(LoginRequiredMixin, View):
    """仕入先検索 API（AJAX 用）。

    入荷指示登録画面などで仕入先マスタを検索選択するためのエンドポイント。
    仕入先コード / 仕入先名 / 担当者の部分一致で検索し最大 LIMIT 件返す（有効のみ）。
    """

    LIMIT = 30

    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        qs = Supplier.objects.filter(is_active=True)
        if q:
            qs = qs.filter(
                Q(supplier_code__icontains=q)
                | Q(supplier_name__icontains=q)
                | Q(contact_person__icontains=q)
            )
        qs = qs.order_by('supplier_code')[: self.LIMIT]
        return JsonResponse({
            'results': [
                {
                    'id': s.pk,
                    'supplier_code': s.supplier_code,
                    'supplier_name': s.supplier_name,
                    'contact_person': s.contact_person,
                }
                for s in qs
            ]
        })


class SkuSearchAPIView(LoginRequiredMixin, View):
    """SKU 検索 API（AJAX 用）。

    入荷指示画面などで SKU マスタからピッキング選択するためのエンドポイント。
    SKU コード / JAN / 商品名 / 商品コードの部分一致で検索し最大 LIMIT 件返す。
    """

    LIMIT = 30

    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        qs = (
            Sku.objects.filter(is_active=True)
            .select_related('product', 'product__manufacturer')
        )
        if q:
            qs = qs.filter(
                Q(sku_code__icontains=q)
                | Q(jan_code__icontains=q)
                | Q(product__product_name__icontains=q)
                | Q(product__product_code__icontains=q)
            )
        qs = qs.order_by('sku_code')[: self.LIMIT]
        return JsonResponse({
            'results': [
                {
                    'sku_code': sku.sku_code,
                    'jan_code': sku.jan_code,
                    'product_name': sku.product.product_name,
                    'product_code': sku.product.product_code,
                    'size_info': sku.size_info,
                    'color_info': sku.color_info,
                    'manufacturer_name': (
                        sku.product.manufacturer.manufacturer_name
                        if sku.product.manufacturer_id else ''
                    ),
                }
                for sku in qs
            ]
        })


class SkuLookupAPIView(LoginRequiredMixin, View):
    """SKU コードの実在チェック API（AJAX 用）。

    handheld 画面でスキャンした SKU をその場で検証するためのエンドポイント。
    SKU コードの完全一致で1件引き、有効（is_active）なものだけを「実在」とみなす。
    フォームの clean_sku_code と同じ条件で判定する。
    """

    def get(self, request):
        code = (request.GET.get('code') or '').strip()
        if not code:
            return JsonResponse({'found': False})
        sku = (
            Sku.objects.select_related('product')
            .filter(sku_code=code, is_active=True)
            .first()
        )
        if sku is None:
            return JsonResponse({'found': False})
        return JsonResponse({
            'found': True,
            'sku_code': sku.sku_code,
            'jan_code': sku.jan_code,
            'product_name': sku.product.product_name,
            'size_info': sku.size_info,
            'color_info': sku.color_info,
        })


class LocationLookupAPIView(LoginRequiredMixin, View):
    """棚番の実在チェック API（AJAX 用）。

    棚入れ作業など handheld 画面で、スキャンした棚番をその場で検証するための
    エンドポイント。棚番コードの完全一致で1件引き、現在倉庫スコープ・有効
    （is_active）のものだけを「実在」とみなす。
    """

    def get(self, request):
        code = (request.GET.get('code') or '').strip()
        if not code:
            return JsonResponse({'found': False})
        qs = (
            Location.objects.select_related('area')
            .filter(location_code=code, is_active=True)
        )
        wh = get_current_warehouse(request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        location = qs.first()
        if location is None:
            return JsonResponse({'found': False})
        return JsonResponse({
            'found': True,
            'location_code': location.location_code,
            'location_name': location.location_name,
            'area_name': location.area.area_name or location.area.area_code,
            # エリア区分（storage / large_item）。棚入れ時の格納先チェックに使う
            'area_location_type': location.area.location_type,
            'area_location_type_display': location.area.get_location_type_display(),
        })


# ---- マスタ CSV 入出力 ----

class MasterCsvExportView(LoginRequiredMixin, View):
    """マスタを CSV（UTF-8 BOM付）でエクスポートする。

    URL の <entity> で対象マスタを切り替える。全件を出力し、そのまま
    Excel 等で編集して取り込み直すための雛形（テンプレート）も兼ねる。
    """

    def get(self, request, entity):
        spec = MASTER_SPECS.get(entity)
        if spec is None:
            raise Http404('未知のマスタです。')
        delimiter = DELIMITERS.get(request.GET.get('delimiter'), ',')
        # 一覧/照会画面の検索条件を反映（spec.list_filter があれば適用、無ければ全件）
        qs = spec.model.objects.all()
        if spec.list_filter:
            qs = spec.list_filter(request, qs)
        content = build_csv(spec, delimiter=delimiter, qs=qs)
        response = HttpResponse(content, content_type='text/csv')
        filename = f'{entity}_{timezone.localdate():%Y%m%d}.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class MasterCsvImportView(LoginRequiredMixin, TemplateView):
    """マスタを CSV でインポート（アップサート）する。

    GET でアップロード画面、POST で取り込みを実行する。全行を検証し、
    1行でもエラーがあれば一切確定しない（all-or-nothing）。失敗時は
    ErrorLog（取り込みエラー）に1件記録し、画面に行ごとのエラーを表示する。
    """

    template_name = 'a/masters/csv_import.html'

    def get_spec(self):
        spec = MASTER_SPECS.get(self.kwargs['entity'])
        if spec is None:
            raise Http404('未知のマスタです。')
        return spec

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        spec = self.get_spec()
        ctx['spec'] = spec
        ctx['columns'] = column_guide(spec)
        ctx['auto_numbered'] = spec.auto_number is not None
        ctx['delimiter'] = self.request.POST.get('delimiter') or 'auto'
        return ctx

    def post(self, request, entity):
        spec = self.get_spec()
        upload = request.FILES.get('csv_file')
        if upload is None:
            messages.error(request, 'CSV ファイルを選択してください。')
            return self.render_to_response(self.get_context_data())

        delimiter = DELIMITERS.get(request.POST.get('delimiter'))  # auto/未指定 → None（自動判定）
        report = import_csv(spec, upload.read(), delimiter=delimiter)
        if report.ok:
            messages.success(
                request,
                f'{spec.label}マスタを取り込みました'
                f'（新規 {report.created} 件 / 更新 {report.updated} 件）。',
            )
            return HttpResponseRedirect(reverse(spec.list_url))

        # 失敗: ErrorLog に1件記録し、画面に行ごとのエラーを表示する
        detail = '\n'.join(
            f'{line}行目: {msg}' if line else msg
            for line, msg in report.errors
        )
        ErrorLog.objects.create(
            error_type=ErrorLog.ErrorType.IMPORT,
            summary=(f'CSVインポート失敗: {spec.label}マスタ '
                     f'{len(report.errors)}件のエラー')[:255],
            detail=f'ファイル名: {upload.name}\n\n{detail}',
            source=f'マスタCSVインポート（{spec.label}）',
            request_method='POST',
            user=request.user,
        )
        messages.error(
            request,
            f'取り込めませんでした。{len(report.errors)} 件のエラーがあります'
            f'（確定された変更はありません）。',
        )
        ctx = self.get_context_data()
        ctx['report'] = report
        return self.render_to_response(ctx)
