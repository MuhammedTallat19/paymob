import os

import pandas as pd
from celery import shared_task
from django.utils import timezone

from .models import BulkInsertJob
from .repositories import OracleConnectionRepository, PolicyRepository


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def bulk_insert_policies_from_file(self, job_id: int) -> dict:
    """
    Background job:
    - reads an uploaded Excel file
    - inserts each policy into STG_POLICIES using PolicyRepository.insert_stg_policy
    - updates BulkInsertJob progress counters
    """
    job = BulkInsertJob.objects.get(id=job_id)
    job.status = BulkInsertJob.Status.RUNNING
    job.started_at = timezone.now()
    job.error_message = ""
    job.save(update_fields=["status", "started_at", "error_message"])

    try:
        if not job.source_path or not os.path.exists(job.source_path):
            raise FileNotFoundError(f"Upload file not found: {job.source_path}")

        df = pd.read_excel(job.source_path)
        required_columns = ["POLICY_NUMBER"]
        missing = [c for c in required_columns if c not in df.columns]
        if missing:
            raise ValueError(f"Missing columns in file: {', '.join(missing)}")

        has_element = "ELEMENT_ID" in df.columns

        total = int(df.shape[0])
        success = 0
        error = 0

        conn_repo = OracleConnectionRepository()
        policy_repo = PolicyRepository(conn_repo)

        for _, row in df.iterrows():
            policy_number = str(row.get("POLICY_NUMBER", "") or "").strip()
            element_id = str(row.get("ELEMENT_ID", "") or "").strip() if has_element else ""

            if not policy_number or policy_number.lower() in ("nan", "none"):
                error += 1
                continue

            ok = policy_repo.insert_stg_policy(policy_number, element_id)
            if ok:
                success += 1
            else:
                error += 1

        job.total_rows = total
        job.success_rows = success
        job.error_rows = error
        job.status = BulkInsertJob.Status.SUCCESS
        job.finished_at = timezone.now()
        job.save(update_fields=["total_rows", "success_rows", "error_rows", "status", "finished_at"])

        return {"success": True, "job_id": job.id, "total": total, "success_rows": success, "error_rows": error}

    except Exception as e:
        job.status = BulkInsertJob.Status.ERROR
        job.error_message = str(e)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_message", "finished_at"])
        raise

