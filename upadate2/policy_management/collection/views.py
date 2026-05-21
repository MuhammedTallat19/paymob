"""
Collection Views Module

This module contains all view functions and utilities for handling HTTP requests.
Views handle user interactions, form processing, and template rendering.
"""

from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.http import JsonResponse
from django.contrib import messages
from django.utils import timezone
from django.core.cache import cache
from typing import Dict, Any, Optional
import pandas as pd
import io
import os
import uuid

from .forms import UploadFileForm
from .services import (
    UploadService, 
    ProcessService, 
    ReportService, 
    DashboardService, 
    TemplateService
)
from .repositories import (
    OracleConnectionRepository, 
    PolicyRepository, 
    DashboardRepository, 
    ReportRepository
)
from .models import BulkInsertJob

# Initialize repositories and services
conn_repo = OracleConnectionRepository()
policy_repo = PolicyRepository(conn_repo)
dashboard_repo = DashboardRepository(conn_repo)
report_repo = ReportRepository(conn_repo)

class ViewUtils:
    @staticmethod
    def handle_error(request, error_msg: str, redirect_url: str):
        messages.error(request, error_msg)
        return redirect(redirect_url)

    @staticmethod
    def get_selected_date(request) -> str:
        return request.GET.get("date", timezone.now().strftime("%Y-%m-%d"))

    @staticmethod
    def create_excel_response(df: pd.DataFrame, filename: str) -> HttpResponse:
        output = io.BytesIO()
        try:
            # Prefer xlsxwriter for richer formatting
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df.to_excel(writer, index=False, sheet_name='Policies')
                workbook = writer.book
                worksheet = writer.sheets['Policies']
                
                header_format = workbook.add_format({
                    'bold': True,
                    'bg_color': '#CCE5FF',
                    'border': 1,
                    'align': 'center'
                })
                
                for col_num, value in enumerate(df.columns):
                    worksheet.write(0, col_num, value, header_format)
                    worksheet.set_column(col_num, col_num, 20)
        except ModuleNotFoundError:
            # xlsxwriter not available → fall back to openpyxl (no custom header formatting)
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Policies')

        output.seek(0)
        response = HttpResponse(
            output.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename={filename}'
        return response

def index(request):
    selected_date = ViewUtils.get_selected_date(request)
    dashboard_service = DashboardService(dashboard_repo)
    stats = dashboard_service.fetch_daily_dashboard(selected_date)
    
    policy = None
    message = None
    if policy_number := request.GET.get("policy_number"):
        if not (policy := policy_repo.get_policy_by_number(policy_number)):
            message = f"لم يتم العثور على بوليصة {policy_number}"

    return render(request, "index.html", {
        "stats": stats,
        "selected_date": selected_date,
        "policy": policy,
        "message": message,
        "db_connected": True
    })

def find_policy(request):
    if request.method != "GET":
        return redirect('collection:index')
        
    if policy_number := request.GET.get("policy_number"):
        policy = policy_repo.get_policy_by_number(policy_number)
        if not policy:
            messages.error(request, f"لم يتم العثور على بوليصة {policy_number}")
        return render(request, "index.html", {
            "policy": policy,
            "message": None if policy else f"لم يتم العثور على بوليصة {policy_number}",
            "db_connected": True,
            "stats": DashboardService(dashboard_repo).fetch_daily_dashboard(),
            "selected_date": ViewUtils.get_selected_date(request)
        })
    return redirect('collection:index')

def upload_policies(request):
    """
    Automation/Queue version:
    - save uploaded file to MEDIA_ROOT
    - create BulkInsertJob
    - enqueue Celery task to insert many policies into STG_POLICIES
    """
    if request.method != "POST" or not request.FILES.get("policy_file"):
        return render(request, "upload_policies.html")

    try:
        excel_file = request.FILES["policy_file"]
        if not excel_file.name.endswith((".xlsx", ".xls")):
            return ViewUtils.handle_error(
                request,
                "يجب أن يكون الملف بصيغة Excel (.xlsx, .xls)",
                "collection:upload_policies",
            )

        # Persist file first (so we don't pass large payload to queue)
        from django.conf import settings

        job_token = uuid.uuid4().hex
        upload_dir = os.path.join(settings.MEDIA_ROOT, "bulk_jobs", job_token)
        os.makedirs(upload_dir, exist_ok=True)
        saved_path = os.path.join(upload_dir, excel_file.name)

        with open(saved_path, "wb") as f:
            for chunk in excel_file.chunks():
                f.write(chunk)

        job = BulkInsertJob.objects.create(
            status=BulkInsertJob.Status.PENDING,
            source_filename=excel_file.name,
            source_path=saved_path,
        )

        # Enqueue via Celery if available, otherwise run synchronously
        try:
            from .tasks import bulk_insert_policies_from_file  # local import to avoid hard dependency

            bulk_insert_policies_from_file.delay(job.id)
        except Exception:
            # Fallback: process immediately (no queue)
            from .repositories import OracleConnectionRepository, PolicyRepository

            job.status = BulkInsertJob.Status.RUNNING
            job.started_at = timezone.now()
            job.save(update_fields=["status", "started_at"])

            df = pd.read_excel(saved_path)
            required_columns = ["POLICY_NUMBER"]
            missing = [c for c in required_columns if c not in df.columns]
            if missing:
                raise ValueError(f"الأعمدة المطلوبة غير موجودة: {', '.join(missing)}")

            has_element = "ELEMENT_ID" in df.columns
            total = int(df.shape[0])
            success = 0
            error = 0

            conn_repo_local = OracleConnectionRepository()
            policy_repo_local = PolicyRepository(conn_repo_local)

            for _, row in df.iterrows():
                policy_number = str(row.get("POLICY_NUMBER", "") or "").strip()
                element_id = str(row.get("ELEMENT_ID", "") or "").strip() if has_element else ""
                if not policy_number or policy_number.lower() in ("nan", "none"):
                    error += 1
                    continue
                if policy_repo_local.insert_stg_policy(policy_number, element_id):
                    success += 1
                else:
                    error += 1

            job.total_rows = total
            job.success_rows = success
            job.error_rows = error
            job.status = BulkInsertJob.Status.SUCCESS
            job.finished_at = timezone.now()
            job.save(
                update_fields=[
                    "total_rows",
                    "success_rows",
                    "error_rows",
                    "status",
                    "finished_at",
                ]
            )

        messages.success(request, f"تم إرسال الملف إلى قائمة الانتظار. رقم العملية: {job.id}")
        return render(request, "upload_policies.html", {"job_id": job.id})

    except Exception as e:
        return ViewUtils.handle_error(request, f"حدث خطأ: {str(e)}", "collection:upload_policies")


def bulk_job_status(request, job_id: int):
    """Simple JSON status endpoint for queued bulk insert jobs."""
    try:
        job = BulkInsertJob.objects.get(id=job_id)
        return JsonResponse(job.as_dict())
    except BulkInsertJob.DoesNotExist:
        return JsonResponse({"error": "job_not_found"}, status=404)

def upload_report(request):
    selected_date = request.GET.get("date")
    report_service = ReportService(report_repo)
    report = report_service.get_upload_report(selected_date)
    return render(request, "upload_report.html", {"report": report, "selected_date": selected_date})

def process_policies(request):
    try:
        process_service = ProcessService(PolicyRepository(OracleConnectionRepository()))
        results = process_service.process_policies(request)
        
        # حساب الإحصائيات
        context = {
            'total': results['total_count'],
            'success': results['success_count'],
            'errors': results['error_count'],
            'duplicates': results['duplicate_count'],
            'results': results['results']
        }
        
        return render(request, 'process_result.html', context)
    except Exception as e:
        messages.error(request, str(e))
        return redirect('collection:index')

def reprocess_policy(request):
    if request.method == 'POST':
        policy_number = request.POST.get("policy_number")
    else:
        policy_number = request.GET.get("policy_number")

    report_service = ReportService(report_repo)
    result = report_service.reprocess_policy(policy_number, policy_repo)

    last_report = cache.get('LAST_UPLOAD_REPORT', {"rows": []})
    rows = last_report.get("rows", [])
    for row in rows:
        if row.get("POLICY_NUMBER") == policy_number:
            row["STATUS"] = "SUCCESS" if result.get("success") else "ERROR"
            row["COLLECTION_ID"] = result.get("collection_id", "")
            row["ERROR_MSG"] = "" if result.get("success") else result.get("error", "")
    last_report["success_count"] = sum(1 for r in rows if r.get("STATUS") == "SUCCESS")
    last_report["error_count"] = sum(1 for r in rows if r.get("STATUS") == "ERROR")
    last_report["skipped_count"] = sum(1 for r in rows if r.get("STATUS") == "PENDING")
    cache.set('LAST_UPLOAD_REPORT', last_report, timeout=86400)

    if result.get("success"):
        messages.success(request, f"تمت إعادة المعالجة لبوليصة {policy_number} بنجاح")
    else:
        messages.error(request, f"إعادة المعالجة فشلت: {result.get('error')}")
    
    return redirect('collection:upload_report')

def download_template(request):
    # Create DataFrame with the required columns
    df = pd.DataFrame(columns=['ELEMENT_ID', 'POLICY_NUMBER'])
    
    # Create Excel writer
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Policies')
        
        # Get workbook and add formatting
        workbook = writer.book
        worksheet = writer.sheets['Policies']
        
        # Add header formatting
        header_format = workbook.add_format({
            'bold': True,
            'bg_color': '#CCE5FF',
            'border': 1,
            'align': 'center'
        })
        
        # Format headers
        for col_num, value in enumerate(df.columns):
            worksheet.write(0, col_num, value, header_format)
            worksheet.set_column(col_num, col_num, 20)
    
    # Prepare response
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename=policy_template.xlsx'
    
    return response

def download_upload_report(request):
    selected_date = request.GET.get("date")
    last_report = cache.get('LAST_UPLOAD_REPORT')
    if not last_report:
        messages.error(request, "لا يوجد تقرير تحميل للتنزيل.")
        return redirect('collection:upload_policies')

    template_service = TemplateService()
    content, filename = template_service.download_upload_report(last_report, selected_date)
    content_type = (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        if filename.endswith('.xlsx') else 'text/csv'
    )
    response = HttpResponse(content, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

def process_result(request):
    try:
        process_service = ProcessService(PolicyRepository(OracleConnectionRepository()))
        results = process_service.process_policies(request)
        
        # الحصول على العمليات الناجحة السابقة من الكاش
        successful_operations = cache.get('successful_operations', [])
        
        # إضافة العمليات الناجحة الجديدة
        new_successful = []
        for result in results.get('results', []):
            if result.get('STATUS') == 'SUCCESS':
                # إضافة توقيت العملية
                result['PROCESSED_AT'] = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
                new_successful.append(result)
        
        # دمج العمليات الناجحة الجديدة مع القديمة
        successful_operations = new_successful + successful_operations
        
        # الاحتفاظ بآخر 100 عملية ناجحة فقط
        successful_operations = successful_operations[:100]
        
        # حفظ العمليات الناجحة في الكاش لمدة 30 يوم
        cache.set('successful_operations', successful_operations, 60 * 60 * 24 * 30)
        
        context = {
            'stats': {
                'total': results.get('total_count', 0),
                'success': results.get('success_count', 0),
                'errors': results.get('error_count', 0),
            },
            'processed_policies': results.get('results', []),
            'successful_operations': successful_operations,  # إضافة العمليات الناجحة للسياق
            'db_connected': True
        }
        
        return render(request, 'process_result.html', context)
    except Exception as e:
        return render(request, 'process_result.html', {
            'message': str(e),
            'db_connected': False,
            'stats': {'total': 0, 'success': 0, 'errors': 0},
            'processed_policies': [],
            'successful_operations': []
        })

def delete_collection(request):
    if request.method == 'POST':
        collection_id = request.POST.get('collection_id')
        print(f"Attempting to delete collection: {collection_id}")
        
        if collection_id:
            try:
                collection_id = str(collection_id)
                policy_repo = PolicyRepository(OracleConnectionRepository())
                result = policy_repo.delete_collection(collection_id)
                
                if result.get('success'):
                    # تحديث سجل العمليات الناجحة
                    successful_operations = cache.get('successful_operations', [])
                    # إزالة العمليات المرتبطة بالحافظة المحذوفة
                    successful_operations = [
                        op for op in successful_operations 
                        if op.get('COLLECTION_ID') != collection_id
                    ]
                    cache.set('successful_operations', successful_operations, 60 * 60 * 24 * 30)
                    
                    affected_policies = result.get('affected_policies', [])
                    if affected_policies:
                        messages.success(request, 
                            f'تم حذف الحافظة {collection_id} بنجاح وإعادة تعيين policyes للمعالجة مرة أخرى')
                    else:
                        messages.success(request, f'تم حذف الحافظة {collection_id} بنجاح')
                else:
                    messages.error(request, f'فشل حذف الحافظة: {result.get("error")}')
            except Exception as e:
                messages.error(request, f'حدث خطأ: {str(e)}')
        else:
            messages.error(request, 'لم يتم تحديد رقم الحافظة')
    
    print("Redirecting back to process result page")
    return redirect('collection:process_result')

def process_upload_rows(df: pd.DataFrame) -> Dict[str, Any]:
    success_count = error_count = 0
    errors = []
    
    for idx, row in df.iterrows():
        try:
            element_id = str(row.get('ELEMENT_ID', '') or '')
            policy_number = str(row.get('POLICY_NUMBER', '') or '')
            
            if not policy_number:
                error_count += 1
                errors.append(f"صف {idx+2}: رقم policy مطلوب")
                continue

            if policy_repo.insert_stg_policy(policy_number, element_id):
                success_count += 1
            else:
                error_count += 1
                errors.append(f"صف {idx+2}: فشل في إدخال policy {policy_number}")
        
        except Exception as e:
            error_count += 1
            errors.append(f"صف {idx+2}: {str(e)}")

    return {
        'success': success_count,
        'error': error_count,
        'errors': errors
    }