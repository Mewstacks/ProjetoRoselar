from django.urls import path
from . import views

app_name = "core"

urlpatterns = [
    path("health/", views.health_check, name="health_check"),
    path("", views.home, name="index"),
    path("dashboard/", views.dashboard, name="dashboard"),

    # Customer APIs
    path("api/search-customer/", views.search_customer, name="search_customer"),
    path("api/create-customer/", views.create_customer, name="create_customer"),
    path("api/search-customer-by-name/", views.search_customer_by_name, name="search_customer_by_name"),

    # Architect APIs
    path("api/search-architect/", views.search_architect, name="search_architect"),
    path("api/create-architect/", views.create_architect, name="create_architect"),

    path("api/shipping-company/<int:company_id>/payment-methods/", views.get_shipping_company_payment_methods, name="shipping_company_payment_methods"),
    path("api/search-shipping-company/", views.search_shipping_company, name="search_shipping_company"),
    path("api/create-shipping-company/", views.create_shipping_company, name="create_shipping_company"),

    # Supplier (Fornecedor) APIs
    path("api/search-supplier/", views.search_supplier, name="search_supplier"),
    path("api/create-supplier/", views.create_supplier, name="create_supplier"),

    # Supplier (Fornecedor) management
    path("fornecedores/", views.supplier_list, name="supplier_list"),
    path("fornecedores/novo/", views.supplier_create, name="supplier_create"),
    path("fornecedores/<int:supplier_id>/editar/", views.supplier_edit, name="supplier_edit"),
    path("fornecedores/<int:supplier_id>/excluir/", views.supplier_delete, name="supplier_delete"),

    # Global Search
    path("api/search/", views.global_search, name="global_search"),

    # Notifications
    path("notificacoes/", views.notifications_list, name="notifications_list"),
    path("api/notifications/", views.notifications_api, name="notifications_api"),
    path("api/notifications/<int:pk>/read/", views.notification_mark_read, name="notification_mark_read"),
    path("api/notifications/mark-all-read/", views.notification_mark_all_read, name="notification_mark_all_read"),

    # Communication History
    path("comunicacao/adicionar/", views.add_communication, name="add_communication"),

    # Reports
    path("relatorios/", views.reports_hub, name="reports_hub"),
    path("relatorios/vendas/", views.report_sales, name="report_sales"),
    path("relatorios/comissoes/", views.report_commissions, name="report_commissions"),
    path("relatorios/descontos/", views.report_discounts, name="report_discounts"),
    path("relatorios/produtos/", views.report_products, name="report_products"),
    path("relatorios/vendas/csv/", views.report_csv_export, name="report_csv_export"),

    # Audit Log
    path("auditoria/", views.audit_log_list, name="audit_log_list"),

    # Goals
    path("metas/", views.goals_list, name="goals_list"),
    path("metas/nova/", views.goal_create, name="goal_create"),
]
