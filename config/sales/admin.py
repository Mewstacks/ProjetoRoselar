from django.contrib import admin
from .models import Quote, QuoteItem, Order, OrderItem, ProposalConfig, SaleDocument
from core.admin_helpers import AdminOnly, SellerAccess, _is_admin


class QuoteItemInline(admin.TabularInline):
    model = QuoteItem
    extra = 1
    fields = ("supplier", "product_name", "quantity", "unit_value")


@admin.register(Quote)
class QuoteAdmin(SellerAccess, admin.ModelAdmin):
    list_display = ("number", "customer", "seller", "status", "total_value_snapshot")
    list_filter = ("status",)
    search_fields = ("number", "customer__name")
    inlines = [QuoteItemInline]
    readonly_fields = ("created_at", "discount_authorized_at", "total_value_snapshot")
    fields = (
        "number", "customer", "seller", "status", "quote_date", "total_value_snapshot",
        "delivery_days_min", "delivery_days_max",
        "freight_value", "freight_responsible", "shipping_company", "shipping_payment_method",
        "discount_percent", "discount_authorized_by", "discount_authorized_at",
        "payment_type", "payment_installments", "payment_fee_percent",
        "total_override", "total_rounding_mode", "total_manual_adjustment",
        "has_architect", "notes",
    )

    def has_delete_permission(self, request, obj=None):
        return _is_admin(request.user)


@admin.register(Order)
class OrderAdmin(SellerAccess, admin.ModelAdmin):
    list_display = ("number", "quote", "supplier", "status")
    list_filter = ("status",)
    search_fields = ("number", "quote__number")
    readonly_fields = ("created_at",)
    fields = ("number", "quote", "supplier", "is_total_conference", "status", "purchase_condition_text", "notes")


@admin.register(QuoteItem)
class QuoteItemAdmin(SellerAccess, admin.ModelAdmin):
    list_display = ("quote", "product_name", "supplier", "quantity", "unit_value")
    search_fields = ("product_name", "quote__number")


@admin.register(OrderItem)
class OrderItemAdmin(AdminOnly, admin.ModelAdmin):
    list_display = ("order", "product_name", "quantity", "purchase_unit_cost")
    search_fields = ("product_name", "order__number")


@admin.register(ProposalConfig)
class ProposalConfigAdmin(AdminOnly, admin.ModelAdmin):
    fields = ("cover_image", "about_image")

    def has_add_permission(self, request):
        return not ProposalConfig.objects.exists()


@admin.register(SaleDocument)
class SaleDocumentAdmin(SellerAccess, admin.ModelAdmin):
    list_display = ("quote", "doc_type", "supplier", "uploaded_by", "uploaded_at")
    list_filter = ("doc_type",)
    search_fields = ("quote__number", "supplier__name", "description")
    readonly_fields = ("uploaded_at",)
