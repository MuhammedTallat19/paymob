#!/usr/bin/env python
"""Check available sequences in Oracle database"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'policy_management.settings')
django.setup()

import oracledb
from django.conf import settings

db_config = settings.DATABASES['default']

try:
    conn = oracledb.connect(
        user=db_config['USER'],
        password=db_config['PASSWORD'],
        dsn=f"{db_config['HOST']}:{db_config['PORT']}/{db_config['NAME']}"
    )
    cur = conn.cursor()
    
    # Query to find all sequences containing FCM or FGL
    cur.execute("""
        SELECT SEQUENCE_OWNER, SEQUENCE_NAME 
        FROM DBA_SEQUENCES 
        WHERE (SEQUENCE_NAME LIKE '%FCM%' OR SEQUENCE_NAME LIKE '%FGL%')
        ORDER BY SEQUENCE_OWNER, SEQUENCE_NAME
    """)
    
    print('Available FCM/FGL sequences:')
    print('=' * 70)
    sequences = cur.fetchall()
    for owner, seq in sequences:
        print(f"{owner}.{seq}")
    
    if not sequences:
        print("No sequences found. Trying alternate query...")
        cur.execute("""
            SELECT SEQUENCE_NAME 
            FROM USER_SEQUENCES 
            ORDER BY SEQUENCE_NAME
        """)
        print("\nAll available sequences:")
        print('=' * 70)
        sequences = cur.fetchall()
        for seq in sequences:
            print(seq[0])
    
    cur.close()
    conn.close()
    
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()
