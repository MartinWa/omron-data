from django.contrib import admin

from .models import ApiToken, ImportRun, Measurement


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff


@admin.register(Measurement)
class MeasurementAdmin(ReadOnlyAdmin):
    list_display = ("at", "metric", "value", "unit", "owner", "note", "import_run")
    list_filter = ("metric", "owner")
    search_fields = ("note",)
    date_hierarchy = "at"
    list_select_related = ("owner", "import_run")


@admin.register(ImportRun)
class ImportRunAdmin(ReadOnlyAdmin):
    list_display = (
        "imported_at",
        "file_name",
        "owner",
        "readings",
        "created",
        "skipped",
        "conflicts",
        "overwritten",
        "panels",
    )
    list_filter = ("owner",)
    search_fields = ("file_name", "content_sha256")
    date_hierarchy = "imported_at"


@admin.register(ApiToken)
class ApiTokenAdmin(ReadOnlyAdmin):
    list_display = ("name", "prefix", "owner", "created_at", "revoked")
    list_filter = ("revoked", "owner")
    search_fields = ("name", "prefix")
