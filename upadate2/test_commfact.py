#!/usr/bin/env python
"""
Test script to run CommFact commission calculations for a specific policy
"""
import sys
import datetime
import oracledb

# Add the policy_management directory to path
sys.path.insert(0, r'd:\pymob\Collection_pymob_App\upadate2\policy_management\collection')

from CommFact import CommCalc

# Database configuration
ORACLE_HOST = "dbsim-scan.egtak.local"
ORACLE_PORT = 1521
ORACLE_SERVICE = "takaful"
ORACLE_USER = "ERP_DML"
ORACLE_PASSWORD = "147@963"

def get_db_connection():
    """Establish database connection"""
    try:
        dsn = f"{ORACLE_HOST}:{ORACLE_PORT}/{ORACLE_SERVICE}"
        print(f"[TEST] Connecting to database: {dsn}")
        print(f"[TEST] User: {ORACLE_USER}")
        conn = oracledb.connect(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=dsn
        )
        print(f"[TEST] Database connection established successfully")
        return conn
    except Exception as e:
        print(f"[TEST ERROR] Failed to connect to database: {str(e)}")
        print(f"[TEST ERROR] Exception type: {type(e).__name__}")
        raise

def main():
    """Main function to test CommFact"""
    policy_number = "P/GA/FG/01/2023/748227/R2"
    payment_date = "2026-04-27"
    
    print(f"\n{'='*80}")
    print(f"[TEST] CommFact Test Script")
    print(f"[TEST] Policy Number: {policy_number}")
    print(f"[TEST] Payment Date: {payment_date}")
    print(f"{'='*80}\n")
    
    conn = None
    try:
        # Get database connection
        conn = get_db_connection()
        
        # Initialize CommCalc
        print(f"\n[TEST] Initializing CommCalc...")
        comm_calc = CommCalc(conn, policy_number, payment_date)
        
        # Calculate commissions
        print(f"\n[TEST] Calculating commissions...")
        commissions = comm_calc.calc_commission_amount()
        
        # Display results
        print(f"\n{'='*80}")
        print(f"[TEST] Commission Calculation Results")
        print(f"{'='*80}")
        print(f"[TEST] Total commissions calculated: {len(commissions)}")
        
        if commissions:
            print(f"\n[TEST] Commission Details:")
            for idx, comm in enumerate(commissions, 1):
                print(f"\n[TEST] Commission {idx}:")
                print(f"[TEST]   - Type: {comm.get('COMM_TYPE')}")
                print(f"[TEST]   - From Day: {comm.get('FROM_DAY')}")
                print(f"[TEST]   - To Day: {comm.get('TO_DAY')}")
                print(f"[TEST]   - Percentage: {comm.get('PERCENTAGE')}%")
                print(f"[TEST]   - Commission Amount: {comm.get('COMMISSION_AMOUNT')}")
                print(f"[TEST]   - Commission Amount with Tax: {comm.get('COMMISSION_AMOUNT_TAX')}")
                print(f"[TEST]   - Is Taxable: {comm.get('IS_TAXABLE')}")
                print(f"[TEST]   - Tax Percentage: {comm.get('TAX_PER')}%")
        else:
            print(f"\n[TEST] No commissions found for this policy")
        
        print(f"\n{'='*80}")
        print(f"[TEST] Test completed successfully")
        print(f"{'='*80}\n")
        
    except Exception as e:
        print(f"\n[TEST ERROR] Test failed with error: {str(e)}")
        print(f"[TEST ERROR] Exception type: {type(e).__name__}")
        import traceback
        print(f"[TEST ERROR] Traceback:\n{traceback.format_exc()}")
        return 1
    finally:
        if conn:
            try:
                conn.close()
                print(f"[TEST] Database connection closed")
            except Exception as e:
                print(f"[TEST WARNING] Error closing connection: {str(e)}")
    
    return 0

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
