"""
Collection Forms Module

This module contains Django forms for user input.
"""

from django import forms


class UploadFileForm(forms.Form):
    """
    Form for uploading policy files.
    
    Fields:
    - file: Excel file (.xlsx, .xls) containing policy numbers
    """
    file = forms.FileField(label='اختر ملف الإكسل (.xlsx, .xls)')