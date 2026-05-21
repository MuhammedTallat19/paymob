from django.urls import path
from . import views

app_name = 'collection'

urlpatterns = [
    path('', views.index, name='index'),
    path('upload_policies/', views.upload_policies, name='upload_policies'),
    path('jobs/<int:job_id>/', views.bulk_job_status, name='bulk_job_status'),
    path('upload_report/', views.upload_report, name='upload_report'),
    path('process-policies/', views.process_policies, name='process_policies'),
    path('download_template/', views.download_template, name='download_template'),
    path('download_upload_report/', views.download_upload_report, name='download_upload_report'),
    path('reprocess_policy/', views.reprocess_policy, name='reprocess_policy'),
    path('find_policy/', views.find_policy, name='find_policy'),
    path('process_result/', views.process_result, name='process_result'),
    path('delete-collection/', views.delete_collection, name='delete_collection'),
]