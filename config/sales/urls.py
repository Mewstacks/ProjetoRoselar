from django.urls import path
from . import views

app_name = "sales"

urlpatterns = [
    # Quotes Hub
    path("quotes/", views.quotes_hub, name="quotes_hub"),
    path("quotes/list/", views.quote_list, name="quote_list"),
    path("quotes/new/", views.quote_create, name="quote_create"),
    path("quotes/<int:quote_id>/", views.quote_detail, name="quote_detail"),
    path("quotes/<int:quote_id>/reminders/", views.quote_reminders, name="quote_reminders"),
    path("quotes/<int:quote_id>/edit/", views.quote_edit, name="quote_edit"),
    path("quotes/<int:quote_id>/simulate/", views.quote_simulate_commission, name="quote_simulate"),
    path("quotes/<int:quote_id>/convert/", views.quote_convert_to_orders, name="quote_convert"),
    path("quotes/<int:quote_id>/set-sale-date/", views.quote_set_sale_date, name="quote_set_sale_date"),
    path("quotes/<int:quote_id>/pdf/client/", views.quote_pdf_client, name="quote_pdf_client"),
    path("quotes/<int:quote_id>/pdf/supplier/", views.quote_pdf_supplier, name="quote_pdf_supplier"),
    path("quotes/<int:quote_id>/duplicate/", views.quote_duplicate, name="quote_duplicate"),
    path("quotes/<int:quote_id>/delete/", views.quote_delete, name="quote_delete"),
    path("quotes/bulk-delete/", views.quotes_bulk_delete, name="quotes_bulk_delete"),

    # Documentos / Notas Fiscais da venda
    path("quotes/<int:quote_id>/documentos/", views.quote_documents, name="quote_documents"),
    path("quotes/<int:quote_id>/documentos/<int:doc_id>/delete/", views.quote_document_delete, name="quote_document_delete"),
    
    # Standalone simulator
    path("simulador/", views.standalone_simulation, name="standalone_simulation"),

    # API endpoints
    path("api/payment-method-fees/", views.payment_method_fees_api, name="payment_method_fees_api"),
    path("api/authorize-discount/", views.authorize_discount_api, name="authorize_discount_api"),
    path("api/get-architect-commission/", views.get_architect_commission_api, name="get_architect_commission_api"),
    
    # Order URLs
    path("orders/", views.order_list, name="order_list"),
    path("orders/new/", views.order_create_standalone, name="order_create_standalone"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),
    path("orders/<int:order_id>/edit/", views.order_edit, name="order_edit"),
    path("orders/<int:order_id>/set-delivery/", views.order_set_delivery, name="order_set_delivery"),
    path("orders/<int:order_id>/approve/", views.order_approve, name="order_approve"),
    path("orders/<int:order_id>/conclude/", views.order_conclude, name="order_conclude"),
    path("orders/<int:order_id>/cancel/", views.order_cancel, name="order_cancel"),
    path("orders/<int:order_id>/pdf/", views.order_pdf, name="order_pdf"),
]
