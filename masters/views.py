import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import ProtectedError, Q
from django.http import HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView, TemplateView

from .forms import (
    AreaForm, CategoryForm, CustomerForm, LocationForm,
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


# ---- Master Inquiry (combined Area + Location overview) ----

class MasterInquiryView(LoginRequiredMixin, TemplateView):
    """エリアとロケーションを1画面で照会できる入口画面。

    検索フィルタ・サマリー統計・テーブルを各セクション（エリア / ロケーション）に持つ。
    フィルタは GET パラメータで受け、`area_*` / `loc_*` プレフィックスで namespace 分離。
    """

    template_name = 'a/masters/master_inquiry.html'

    SEARCH_KEYS = ('loc_q', 'loc_warehouse', 'loc_area', 'loc_type', 'loc_status')

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
            loc_qs = loc_qs.order_by(
                'warehouse__warehouse_code', 'area__area_code', 'location_code'
            )
            ctx['locations'] = loc_qs

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

class AreaListView(CurrentWarehouseScopedMixin, LoginRequiredMixin, ListView):
    model = Area
    template_name = 'a/masters/area_list.html'
    context_object_name = 'areas'

    def get_queryset(self):
        return (
            super().get_queryset()
            .select_related('warehouse')
            .order_by('warehouse__warehouse_code', 'area_code')
        )


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

class LocationListView(CurrentWarehouseScopedMixin, LoginRequiredMixin, ListView):
    model = Location
    template_name = 'a/masters/location_list.html'
    context_object_name = 'locations'

    def get_queryset(self):
        return (
            super().get_queryset()
            .select_related('warehouse', 'area')
            .order_by('warehouse__warehouse_code', 'area__area_code', 'location_code')
        )


class LocationCreateView(LocationFormContextMixin, LoginRequiredMixin, CreateView):
    model = Location
    form_class = LocationForm
    template_name = 'a/masters/location_form.html'
    success_url = reverse_lazy('masters:master_inquiry')


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

class CategoryListView(LoginRequiredMixin, ListView):
    """カテゴリ一覧をツリー順（深さ優先）で表示する。各ノードに depth_attr と children_count を付与。"""

    model = Category
    template_name = 'a/masters/category_list.html'
    context_object_name = 'categories'

    def get_queryset(self):
        all_cats = list(
            Category.objects.select_related('parent').order_by('sort_order', 'category_code')
        )
        by_parent = {}
        for c in all_cats:
            by_parent.setdefault(c.parent_id, []).append(c)

        flat = []

        def walk(parent_id, depth):
            for c in by_parent.get(parent_id, []):
                c.depth_attr = depth
                c.children_count = len(by_parent.get(c.pk, []))
                flat.append(c)
                walk(c.pk, depth + 1)

        walk(None, 0)
        return flat


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

class ManufacturerListView(LoginRequiredMixin, ListView):
    model = Manufacturer
    template_name = 'a/masters/manufacturer_list.html'
    context_object_name = 'manufacturers'

    def get_queryset(self):
        return Manufacturer.objects.order_by('manufacturer_code')


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
            qs = qs.order_by('product_code')
            ctx['products'] = qs
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
            qs = qs.order_by('sku_code')
            ctx['skus'] = qs
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

class SupplierListView(LoginRequiredMixin, ListView):
    model = Supplier
    template_name = 'a/masters/supplier_list.html'
    context_object_name = 'suppliers'

    def get_queryset(self):
        return Supplier.objects.order_by('supplier_code')


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

class CustomerListView(LoginRequiredMixin, ListView):
    model = Customer
    template_name = 'a/masters/customer_list.html'
    context_object_name = 'customers'

    def get_queryset(self):
        return Customer.objects.order_by('customer_code')


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
