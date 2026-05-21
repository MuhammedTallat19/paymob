"""
Collection Models Module

This module contains Django models for the collection application.
"""

from django.db import models

class fglInvoices(models.Model):
    serial = models.IntegerField(primary_key=True)
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=3)
    amount_lc = models.DecimalField(max_digits=18, decimal_places=3)
    exrate = models.DecimalField(max_digits=18, decimal_places=3)
    status = models.CharField(max_length=1)
    status_date = models.DateField()
    due_amount = models.DecimalField(max_digits=18, decimal_places=3)
    fgl_trn_id = models.IntegerField()
    fgl_trd_serial = models.IntegerField()
    fcs_cst_id = models.IntegerField()
    fcr_trt_code = models.CharField(max_length=6)
    due_amount_fc = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        db_table = 'FGL_INVOICES'
        managed = False         # Tell Django not to manage this table
    
    def __str__(self):
        return str(self.serial)
class StgPolicy(models.Model):
    """
    Staging Policy Model.
    
    Represents policies in the staging table (STG_POLICIES).
    Policies are uploaded here first, then processed and moved to ACCEPT_POLICIES.
    
    Note: managed = False means Django does not manage this table's migrations.
    """
    id = models.AutoField(primary_key=True)
    element_id = models.CharField(max_length=255, blank=True)
    policy_number = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_flag = models.IntegerField(default=1)
    processed_at = models.DateTimeField(null=True, blank=True)
    error_msg = models.TextField(blank=True)
    modified = models.BooleanField(null=True, blank=True)
    modified_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'STG_POLICIES'
        managed = False


class BulkInsertJob(models.Model):
    """
    Tracks background bulk insert jobs (queued automation).

    This model is managed by Django (SQLite default DB) and is independent of
    the Oracle tables used for policies/collections.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        ERROR = "ERROR", "Error"

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    source_filename = models.CharField(max_length=255, blank=True, default="")
    source_path = models.TextField(blank=True, default="")

    total_rows = models.IntegerField(null=True, blank=True)
    success_rows = models.IntegerField(default=0)
    error_rows = models.IntegerField(default=0)

    error_message = models.TextField(blank=True, default="")
    modified = models.BooleanField(null=True, blank=True)
    modified_date = models.DateTimeField(null=True, blank=True)

    def as_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "source_filename": self.source_filename,
            "total_rows": self.total_rows,
            "success_rows": self.success_rows,
            "error_rows": self.error_rows,
            "error_message": self.error_message,
        }


class AcceptPolicy(models.Model):
    """
    Accepted Policy Model.
    
    Represents policies that have been successfully processed and moved to ACCEPT_POLICIES table.
    These are policies that have passed validation and have collections created for them.
    
    Note: managed = False means Django does not manage this table's migrations.
    """
    id = models.AutoField(primary_key=True)
    element_id = models.CharField(max_length=255, blank=True)
    policy_number = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_flag = models.IntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)
    fcm_collection_id = models.IntegerField(null=True, blank=True)
    error_msg = models.TextField(blank=True)
    modified = models.BooleanField(null=True, blank=True)
    modified_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'ACCEPT_POLICIES'
        managed = False