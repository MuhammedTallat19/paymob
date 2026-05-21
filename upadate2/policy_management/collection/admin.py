from django.contrib import admin
from .models import StgPolicy, BulkInsertJob, AcceptPolicy


@admin.register(StgPolicy)
class StgPolicyAdmin(admin.ModelAdmin):
    """Admin interface for Staging Policies"""
    list_display = ('id', 'policy_number', 'element_id', 'created_at', 'processed_flag', 'error_msg')
    list_filter = ('processed_flag', 'created_at')
    search_fields = ('policy_number', 'element_id')
    readonly_fields = ('created_at', 'id')
    ordering = ('-created_at',)


@admin.register(AcceptPolicy)
class AcceptPolicyAdmin(admin.ModelAdmin):
    """Admin interface for Accepted Policies"""
    list_display = ('id', 'policy_number', 'element_id', 'created_at', 'processed_flag')
    list_filter = ('processed_flag', 'created_at')
    search_fields = ('policy_number', 'element_id')
    readonly_fields = ('created_at', 'id')
    ordering = ('-created_at',)


@admin.register(BulkInsertJob)
class BulkInsertJobAdmin(admin.ModelAdmin):
    """Admin interface for Bulk Insert Jobs"""
    list_display = ('id', 'status', 'created_at', 'started_at', 'finished_at', 'source_filename', 'total_rows', 'success_rows', 'error_rows')
    list_filter = ('status', 'created_at')
    search_fields = ('source_filename', 'source_path')
    readonly_fields = ('created_at', 'started_at', 'finished_at', 'id')
    ordering = ('-created_at',)
