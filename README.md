# Collection PyMob App - Core Modules Documentation

Comprehensive technical documentation for the 4 core Python modules that power the insurance policy collection and ERP integration system.

**Focus**: `services.py` | `repositories.py` | `utils.py` | `CommFact.py`

---

## 📋 Table of Contents

1. [Services Module](#services-module)
2. [Repositories Module](#repositories-module)
3. [Utils Module](#utils-module)
4. [CommFact Module](#commfact-module)
5. [Processing Pipeline](#processing-pipeline)
6. [Error Handling](#error-handling)
7. [Code Examples](#code-examples)

---

## 🔧 Services Module (`services.py`)

### Overview

The Services module contains the business logic orchestrating policy upload, processing, reporting, and dashboard functionality. It implements the complete 10-step collection processing pipeline.

### Classes

#### 1. **UploadService**

Parses CSV/Excel files and stages valid policy numbers for processing.

```python
class UploadService:
    """Parse a CSV file and stage valid policy numbers for processing."""
    
    def __init__(self, policy_repo: PolicyRepository):
        self.policy_repo = policy_repo
```

**Methods:**

```python
def allowed_file(self, filename: str) -> bool:
    """Check if file has allowed extension (xlsx, xls)"""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in settings.ALLOWED_EXTENSIONS

def process_excel_file(self, filepath: str) -> Dict[str, Any]:
    """
    Parse CSV and stage valid policies.
    
    Returns:
        {
            'results': [
                {'POLICY_NUMBER': '12345', 'STATUS': 'SUCCESS', 'ERROR_MSG': ''},
                {'POLICY_NUMBER': '12346', 'STATUS': 'ERROR', 'ERROR_MSG': 'رقم policy فارغ'},
            ],
            'total_count': 2,
            'success_count': 1,
            'error_count': 1,
            'duplicate_count': 0
        }
    """
```

**Processing Logic:**
1. Reads CSV with UTF-8 encoding
2. Validates required column: `POLICY_NUMBER`
3. Detects empty/null policy numbers
4. Identifies duplicate policies (same policy uploaded twice)
5. Inserts into `STG_POLICIES` table via `PolicyRepository`
6. Returns detailed per-policy results with status

**Error Messages:**
- Empty policy numbers → `ERROR`
- Duplicate policies (same month) → `DUPLICATE`
- Database insert failures → `ERROR` with exception text

---

#### 2. **ProcessService**

Orchestrates the complete 10-step policy processing pipeline transforming staged policies into ERP financial transactions.

```python
class ProcessService:
    """Execute the full 10-step collection processing pipeline."""
    
    CN_TO_RCT_DELAY_SECONDS = 60  # Delay between commission & receipt transactions
    
    def __init__(self, policy_repo: PolicyRepository):
        self.policy_repo = policy_repo
```

**Main Entry Point:**

```python
def process_single_policy(self, policy_number: str) -> Dict[str, Any]:
    """
    Process a single policy through the complete 10-step pipeline.
    
    Args:
        policy_number: Policy number to process
        
    Returns:
        {
            'success': True/False,
            'collection_id': <ID if success>,
            'error': <error message if failure>,
            'step_completed': <step number where it failed>
        }
    """
```

**The 10-Step Pipeline:**

```
Step 1  ──► Validate policy in GPD_POLICIES
           ├─ Check if policy exists
           ├─ Extract net premium, exchange rate, currency
           └─ Get payment method and customer info

Step 2  ──► Create FCM_COLLECTION header (status=1)
           ├─ Insert FCM_COLLECTION record
           ├─ Create FCM_COL_HISTORY entry
           └─ Record creation timestamp

Step 3  ──► Resolve FGL_TRN_ID and FGL_INV_SERIAL
           ├─ Lookup FGL_TRANSACTIONS for this policy
           ├─ Get transaction and invoice serial
           └─ ⚠️ CRITICAL: Validates transaction state

Step 4  ──► Insert FCM_COL_DETAILS
           ├─ Create line item for collection
           ├─ Link to FGL_TRN_ID
           └─ Record payment details

Step 5  ──► Insert FCM_COL_PAYMENTS
           ├─ Create payment record
           └─ Track payment method

Step 6  ──► Insert FCM_COL_SUMMARY
           ├─ Create GL summary entry
           └─ Record GL posting details

Step 7  ──► Fetch cost center (FGL_CPC_ID)
           └─ Get FGL_CPC_ID for GL posting

Step 8  ──► Commission eligibility check
           ├─ Query CommCalc.compute_commissions()
           └─ Branch based on results:
               ├─ Branch A: Has Commission
               │   ├─ Insert VIRTUAL_COMMISSION + DETAILS
               │   ├─ Create CN transactions per type
               │   ├─ Update status: 1→2→3
               │   └─ WAIT 60 seconds (ERP trigger)
               └─ Branch B: No Commission
                   ├─ Update status: 1→2→3
                   └─ WAIT 60 seconds

Step 9  ──► RCT financial transaction
           ├─ Create FGL_TRANSACTIONS (RCT)
           ├─ Insert FGL_TRANS_DETAILS
           ├─ Create FGL_NETTINGS entries
           └─ Update GL posting

Step 10 ──► Accept and Archive
           ├─ Insert ACCEPT_POLICIES
           ├─ Delete from STG_POLICIES
           └─ Complete transaction

[Any step failure ──► ROLLBACK entire transaction]
```

**Step Methods:**

```python
def _step1_validate_policy(self, cur, policy_number: str) -> Dict[str, Any]:
    """Validate policy exists in GPD_POLICIES"""

def _step2_create_collection(self, cur, conn, policy_number, ctx) -> Tuple[int, int]:
    """Create FCM_COLLECTION header and history"""

def _step3_resolve_fgl(self, cur, conn, ctx, new_id) -> Tuple[int, int]:
    """Resolve FGL_TRN_ID and FGL_INV_SERIAL"""

def _step4_col_details(self, cur, conn, new_id, fgl_trn_id, fgl_inv_serial, ctx):
    """Insert FCM_COL_DETAILS line item"""

def _step5_col_payments(self, cur, conn, new_id, ctx):
    """Insert FCM_COL_PAYMENTS record"""

def _step6_col_summary(self, cur, conn, new_id, fgl_trn_id, fgl_inv_serial, ctx):
    """Insert FCM_COL_SUMMARY GL entry"""

def _step7_cost_center(self, cur, ctx) -> int:
    """Fetch FGL_CPC_ID for GL posting"""

def _step8_check_commission(self, cur, conn, policy_number, ctx, fgl_trn_id, new_id):
    """Determine if policy has commissions"""

def _step8a_process_commissions(self, cur, conn, policy_number, new_id, new_serial, ...):
    """Process and insert commissions if they exist"""

def _step8b_no_commission(self, cur, conn, new_id):
    """Update status if no commission"""

def _step9_rct_transaction(self, cur, conn, new_id, new_serial, fgl_trn_id, ...):
    """Create RCT financial transaction + GL + nettings"""

def _step10_accept_policy(self, cur, conn, new_id, new_serial, policy_number, ctx):
    """Archive to ACCEPT_POLICIES, delete from STG_POLICIES"""
```

**Transaction Management:**

```python
def _rollback_collection(self, conn, cur, new_id: Optional[int]) -> None:
    """
    Rollback entire collection if any step fails.
    
    Deletes:
    - FCM_COLLECTION
    - FCM_COL_DETAILS, PAYMENTS, SUMMARY, HISTORY
    - VIRTUAL_COMMISSION, DETAILS
    - FGL_TRANSACTIONS, TRANS_DETAILS
    
    Resets: STG_POLICIES back to processed_flag=1
    """
```

**Helper Methods:**

```python
def _update_status_and_history(self, cur, conn, new_id: int, status: int) -> None:
    """Update FCM_COLLECTION status and insert history record"""

def _insert_history(self, cur, new_id: int, status: int) -> None:
    """Insert FCM_COL_HISTORY entry"""

def _calculate_commission_details(self, commission_rows: List, exrate: float, 
                                  currency_code: str) -> List[Tuple]:
    """Calculate commission details with tax"""
```

---

#### 3. **ReportService**

Retrieves and formats reports for display to users.

```python
class ReportService:
    """Retrieve and format upload / process reports."""
    
    def __init__(self, report_repo: ReportRepository):
        self.report_repo = report_repo
```

**Methods:**

```python
def get_upload_report(self, selected_date: str = None) -> Dict[str, Any]:
    """
    Get upload history with statistics.
    Returns formatted report with counts and details.
    """

def get_process_report(self, selected_date: str = None) -> Dict[str, Any]:
    """Get processing results with counts"""

def reprocess_policy(self, policy_number: str, policy_repo) -> Dict[str, Any]:
    """Manually retry a failed policy"""
```

---

#### 4. **DashboardService**

Provides daily metrics and statistics for the dashboard.

```python
class DashboardService:
    """Fetch and expose daily collection metrics."""
    
    def __init__(self, dashboard_repo: DashboardRepository):
        self.dashboard_repo = dashboard_repo
```

**Methods:**

```python
def fetch_daily_dashboard(self, target_date_str: str = None) -> Dict[str, int]:
    """
    Get daily collection metrics.
    
    Returns:
        {
            'total': 100,
            'success': 85,
            'errors': 10,
            'duplicate': 5
        }
    """

def get_stats(self) -> Dict[str, int]:
    """Get overall collection statistics"""
```

---

#### 5. **TemplateService**

Generates downloadable templates and report exports.

```python
class TemplateService:
    """Generate downloadable CSV templates and report exports."""

    def download_template(self) -> Tuple[bytes, str]:
        """
        Generate CSV template for bulk upload.
        Returns: (file_bytes, filename)
        """

    def download_upload_report(self, report: Dict[str, Any], 
                              selected_date: str) -> Tuple[bytes, str]:
        """Export report to Excel/CSV with formatting"""
```

---

### Internal Exception

```python
class _StepError(Exception):
    """
    Raised by _step_* methods to abort the whole transaction.
    When caught in process_single_policy, triggers full rollback.
    """
```

---

## 🗄️ Repositories Module (`repositories.py`)

### Overview

The Repositories module implements the Data Access Layer (DAL) providing abstraction for all Oracle database operations. Each repository class is responsible for one domain area.

### Classes

#### 1. **OracleConnectionRepository**

Manages Oracle connections and provides generic query/transaction helpers.

```python
class OracleConnectionRepository:
    """
    Manages Oracle connections and provides generic query / transaction helpers.
    
    All other repositories receive an instance and call:
    - get_connection()
    - execute_query()
    - execute_transaction()
    """
    
    def __init__(self):
        self.dsn = f"{settings.ORACLE_HOST_ALT}:{settings.ORACLE_PORT}/{settings.ORACLE_SERVICE_ALT}"
        self.user = settings.ORACLE_USER_ALT
        self.password = settings.ORACLE_PASS_ALT
```

**Methods:**

```python
def get_connection(self) -> oracledb.Connection:
    """
    Get a new Oracle connection.
    Raises Exception if connection fails.
    Prints connection details to console.
    """

def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Tuple]:
    """
    Execute a SELECT query and return rows.
    
    Features:
    - Logs query to console (first 200 chars)
    - Prints result count
    - Prints first row if <= 5 results
    - Auto-closes cursor and connection
    """

def execute_transaction(self, queries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Execute multiple SQL statements atomically.
    
    Args:
        queries: List of {'query': str, 'params': dict}
        
    Returns:
        {'success': True/False, 'error': str if failure}
        
    Features:
    - Commits all or rolls back all
    - Prints detailed logging
    - Exception handling with rollback
    """
```

---

#### 2. **PolicyRepository**

Data access for policy-related operations.

```python
class PolicyRepository:
    """
    Data access for policy-related operations.
    
    Responsibilities:
    - Policy lookup and validation
    - Staging (STG_POLICIES) management
    - Collection creation/deletion
    - Policy status management
    """
    
    def __init__(self, conn_repo: OracleConnectionRepository):
        self.conn_repo = conn_repo
```

**Policy Lookup Methods:**

```python
def get_policy_by_number(self, policy_number: str) -> Optional[Dict[str, Any]]:
    """
    Lookup policy in GPD_POLICIES.
    
    Query joins:
    - GPD_POLICIES (main)
    - GPD_SUBJECTS (subject info)
    - FCS_CUSTOMERS (customer name)
    
    Returns: Policy dict or None if not found
    """

def check_existing_policy(self, policy_number: str) -> bool:
    """Check if policy in ACCEPT_POLICIES (already processed)"""

def is_policy_duplicate(self, policy_number: str) -> bool:
    """Check if policy already in STG_POLICIES or ACCEPT_POLICIES"""
```

**Staging Management:**

```python
def insert_stg_policy(self, policy_number: str, element_id: str = "") -> bool:
    """
    Insert new STG_POLICIES record or mark as duplicate.
    
    Logic:
    1. Check if already in ACCEPT_POLICIES → return False
    2. Check if already in STG_POLICIES (same month) → return False
    3. Insert new row → return True
    
    Returns: True if staged, False if duplicate/error
    """

def get_pending_policies(self) -> List[Tuple]:
    """
    Get all pending policies from STG_POLICIES.
    
    Filter: processed_flag=1 AND not in ACCEPT_POLICIES
    Order: created_at ASC, id ASC
    """

def get_unique_pending_policies(self) -> List[Tuple]:
    """
    Get one record per policy_number (most recent).
    
    Uses ROW_NUMBER() to partition by policy_number
    Prevents duplicate processing.
    """

def cleanup_duplicate_staging(self, policy_number: str) -> None:
    """
    Keep only latest STG_POLICIES entry for a policy.
    Delete older duplicates to avoid reprocessing.
    """
```

**Collection Operations:**

```python
def create_collection(self, policy_number: str) -> Dict[str, Any]:
    """
    Create an FCM_COLLECTION + line items + ACCEPT_POLICIES record.
    
    Workflow:
    1. Lookup policy
    2. Create FCM_COLLECTION header
    3. Create related detail records
    4. Insert ACCEPT_POLICIES
    5. Delete from STG_POLICIES
    
    Returns:
        {'success': True/False, 'collection_id': <ID>, 'error': <msg>}
    """

def delete_collection(self, collection_id: str) -> Dict[str, Any]:
    """
    Delete a collection and reset all related records.
    
    Rollback steps:
    1. Find collection by ID (tries 3 formats)
    2. Get affected policies
    3. Reset STG_POLICIES for those policies
    4. Delete ACCEPT_POLICIES
    5. Delete collection + all details
    
    Handles different ID formats:
    - Exact string: TO_CHAR(ID) = :id
    - Stripped zeros: Remove leading zeros
    - Numeric: Cast to number
    
    Returns: {'success': True/False, 'message': str}
    """
```

**Private Helpers:**

```python
def __find_collection(self, cur, collection_id: str):
    """Try 3 ID lookup strategies, return first match"""

def __get_affected_policies(self, cur, collection_id: int) -> List[str]:
    """Get all policies linked to collection"""

def __reset_staging(self, cur, collection_id: int) -> None:
    """Reset STG_POLICIES and delete ACCEPT_POLICIES"""

def _get_exchange_rate(self, currency_code: str) -> float:
    """Get exchange rate for currency (default 1.0)"""

def _get_next_id(self, table: str) -> int:
    """Get next available ID in table"""
```

---

#### 3. **DashboardRepository**

Return daily collection metrics for the dashboard.

```python
class DashboardRepository:
    """Return daily collection metrics for the dashboard."""
    
    def __init__(self, conn_repo: OracleConnectionRepository):
        self.conn_repo = conn_repo

    def fetch_daily_dashboard(self, target_date_str: str = None) -> Dict[str, int]:
        """
        Get daily collection metrics.
        
        Counts:
        - Total policies uploaded
        - Successful collections
        - Errors
        - Duplicates
        
        Returns:
            {
                'total': <count>,
                'success': <count>,
                'errors': <count>,
                'duplicate': <count>
            }
        """
```

---

#### 4. **ReportRepository**

Retrieve upload/process reports and collection details.

```python
class ReportRepository:
    """Retrieve upload / process reports and collection detail data."""
    
    def __init__(self, conn_repo: OracleConnectionRepository):
        self.conn_repo = conn_repo

    def get_upload_report(self, selected_date: str = None) -> Dict[str, Any]:
        """
        Get upload history with statistics.
        
        Queries:
        - FCM_COLLECTION records
        - Filter by creation_date
        - Order by creation_date DESC
        
        Returns:
            {
                'timestamp': '2026-05-18 10:30:45',
                'rows': [...],
                'success_count': 50,
                'error_count': 5,
                'skipped_count': 0
            }
        """

    def get_collection_details(self, collection_id: int) -> Dict[str, Any]:
        """
        Return full collection info including:
        - FCM_COLLECTION header
        - FCM_COL_DETAILS items
        - FCM_COL_PAYMENTS records
        - Commission details (if applicable)
        """
```

---

### Django View Helper

```python
def delete_collection(request):
    """
    Django POST view — delegates deletion to PolicyRepository.
    
    POST parameters:
    - collection_id: ID to delete
    
    Redirects to process_result after deletion.
    """
```

---

## 🔄 Utils Module (`utils.py`)

### Overview

Provides transaction utility functions for building FGL_TRANSACTIONS headers and managing journal numbering. These utilities handle the critical header information needed before inserting financial transactions into the ERP system.

### Functions

#### 1. **fetch_main_infos**

Build the complete header block needed before inserting an FGL_TRANSACTIONS row.

```python
def fetch_main_infos(
    cur,
    conn,
    new_serial,
    new_id,
    net_premium_val: float,
    exrate: float,
    status: str,
    cn_type: int | None = None,
) -> dict:
    """
    Prepare all header information needed before inserting a FGL_TRANSACTIONS row.
    
    Args:
        cur: Active DB cursor (must be inside an open connection)
        conn: Oracle connection (used for rollback on error)
        new_serial: FCM_COLLECTION.SERIAL of the current collection
        new_id: FCM_COLLECTION.ID of the current collection
        net_premium_val: Net premium value used to calculate amounts
        exrate: Exchange rate
        status: "cashier" or "credit_note"
        cn_type: Commission type (1/2/6) — used for notes string
        
    Returns:
        {
            'success': True,
            'main_info': {
                'id_retrieved': <next transaction ID>,
                'journal_no': <journal number>,
                'segment_code': <transaction code>,
                'notes': <arabic notes>,
                'insert_amount': <amount>,
                'exrate': <rate>,
                'username': 'Paymob',
                'current_year': '2026',
                'current_month': '05',
                'journal_trt_code': <2 or 7>,
                'journal_fyr_year': 2026
            }
        }
        OR
        {
            'success': False,
            'error': <error message>
        }
    """
```

**Processing Steps:**

```
Step 1: Validate status
   ├─ "cashier" → prefix="RV-RCT-01", trt_code=2
   └─ "credit_note" → prefix="CN-COL-01", trt_code=7

Step 2: Get next FGL_TRANSACTIONS.ID
   ├─ Query MAX(ID) from FGL_TRANSACTIONS
   ├─ Add 1
   └─ Lock max_id row to prevent concurrent collisions

Step 3: Get JOURNAL_NO from FGL_JOURNAL_SEGMENT
   ├─ Query by FCR_FYR_YEAR (current year) + FCR_TRT_CODE
   ├─ If exists: stored_value + 1
   └─ If not: 1 (first of year)

Step 4: Build SEGMENT_CODE
   ├─ Format: {prefix}-{YY}-{MM}-{NNNNNN}
   ├─ Example: CN-COL-01-26-05-000001
   └─ Check uniqueness (must not exist)

Step 5: Get sub-transaction types
   ├─ Query FGL_SUB_TRANSACTION_TYPES for credit/debit
   └─ Print count for logging

Step 6: Build Arabic notes
   ├─ Commission: "{Type} الاعمولة الصادرة عن حافظة توريد رقم {serial}"
   ├─ Cashier: "سند القبض الناتج عن حافظة توريد رقم {serial}"
   ├─ Credit Note: "سند القيد الناتج عن حافظة توريد رقم {serial}"
   └─ Generic: "حافظة توريد رقم {serial}"

Step 7: Fetch TOTAL_DEBIT from FCM_COLLECTION
   └─ Fallback to net_premium_val if not found

Step 8: Return complete header dict
```

**JOURNAL_NO Rules:**

```
CN (credit_note) → FCR_TRT_CODE = 7
RV (cashier)     → FCR_TRT_CODE = 2

Both: use stored_value + 1 as journal_no for the new transaction.
After successful commit, call commit_journal_no() to increment.
```

**SEGMENT CODE Rules:**

```
Always built from JOURNAL_NO (from FGL_JOURNAL_SEGMENT), NOT from MAX(ID) PK.

Why?
- PK is globally unique
- JOURNAL_NO resets each fiscal year
- Embedding PK causes collisions across years

Example:
  FY 2025: CN-COL-01-25-01-000001, CN-COL-01-25-01-000002, ...
  FY 2026: CN-COL-01-26-01-000001, CN-COL-01-26-01-000002, ...  ← Resets to 1
```

---

#### 2. **commit_journal_no**

Persist the JOURNAL_NO increment to FGL_JOURNAL_SEGMENT after a successful FGL_TRANSACTIONS INSERT.

```python
def commit_journal_no(cur, conn, trt_code: int, fyr_year: int) -> None:
    """
    Increment JOURNAL_NO for next transaction.
    
    Args:
        cur: Active DB cursor
        conn: Oracle connection
        trt_code: Transaction type code (2=RV, 7=CN)
        fyr_year: Fiscal year (e.g., 2026)
        
    Implementation uses MERGE (safe for first-time insert):
    - MATCHED: JOURNAL_NO = JOURNAL_NO + 1
    - NOT MATCHED: INSERT with JOURNAL_NO = 1
    """
```

**Call Pattern:**

```python
# After creating CN transaction
commit_journal_no(cur, conn, 7, 2026)  # Increment CN counter

# After creating RCT transaction
commit_journal_no(cur, conn, 2, 2026)  # Increment RV counter
```

**MERGE Logic:**

```sql
MERGE INTO ERP.FGL_JOURNAL_SEGMENT tgt
USING (SELECT :trt_code, :fyr_year FROM DUAL) src
  ON (tgt.FCR_TRT_CODE = src.trt_code AND tgt.FCR_FYR_YEAR = src.fyr_year)
WHEN MATCHED THEN
  UPDATE SET tgt.JOURNAL_NO = tgt.JOURNAL_NO + 1
WHEN NOT MATCHED THEN
  INSERT (FCR_TRT_CODE, FCR_FYR_YEAR, JOURNAL_NO) VALUES (src.trt_code, src.fyr_year, 1)
```

---

## 💰 CommFact Module (`CommFact.py`)

### Overview

Calculates commissions for policies based on day difference between issue and payment dates, cost center, account, and agent information. Uses FGL_NETTING_SETUP rules to determine commission rates and applies tax calculations.

### Class

#### **CommCalc**

Commission calculator for insurance policies.

```python
class CommCalc:
    """
    Commission Calculator for policies.
    
    Calculates commissions from FGL_NETTING_SETUP using:
    - Policy segment code (policy number)
    - Payment date
    - Day difference between issue and payment dates
    - Cost center, account, and agent information
    """
    
    def __init__(self, conn, segment_code: str, payment_date: str):
        """
        Args:
            conn: Oracle database connection
            segment_code: Policy segment code (policy number)
            payment_date: Payment date in 'YYYY-MM-DD' format
        """
```

### Commission Types

| Type | Code | Description |
|------|------|-------------|
| Early | 1 | Early payment incentive (higher rate) |
| Collection | 2 | Collection fee |
| Basic | 6 | Standard commission rate |

### Public Methods

#### 1. **compute_commissions**

Calculate commission setups for all three commission types.

```python
def compute_commissions(self) -> list[dict]:
    """
    Calculate commission setups for all three commission types.
    
    Returns flat list of commission dicts with keys:
    - FROM_DAY: Day range start
    - TO_DAY: Day range end
    - PERCENTAGE: Commission percentage
    - IS_TAXABLE: Tax flag (0/1)
    - COMM_TYPE: 1=Early, 2=Collection, 6=Basic
    - TAX_PER: Tax percentage (resolved)
    
    Returns:
        List of commission dicts (empty if no policy rows)
    """
```

**Processing Flow:**

```
1. Get policy cost center / account / agent info
   ├─ Query GPD_POLICIES, GPD_INSTALLMENTS, GPD_VOUCHERS
   ├─ Join to FGL_TRANSACTIONS (serial=2, not deleted, TRT_CODE=6)
   ├─ Join to FGL_TRANS_DETAILS
   └─ Returns: FGL_CPC_ID, FGL_COA_ID, FCS_AGT_ID, EFFECTIVE_DATE

2. Get agent customer ID (ROLE_TYPE=3)
   └─ Single agent for entire policy

3. For each policy row:
   ├─ Calculate day difference (issue_date to payment_date)
   └─ For each commission type (1, 2, 6):
       ├─ Query FGL_NETTING_SETUP matching:
       │  - FGL_CPC_ID (cost center)
       │  - REV_FGL_COA_ID (account)
       │  - Day difference in FROM_DAY..TO_DAY range
       │  - COMM_TYPE
       │  - FCS_AGT_ID (with fallback to agent-less match)
       ├─ If no match with agent: retry without agent
       ├─ Get tax percentage for this customer/account
       └─ Add to results

4. Return combined list (Early + Collection + Basic)
```

---

#### 2. **calc_commission_amount**

Return commission dicts enriched with COMMISSION_AMOUNT and tax.

```python
def calc_commission_amount(self) -> list[dict]:
    """
    Return commission dicts enriched with calculated amounts.
    
    Adds to each commission dict:
    - COMMISSION_AMOUNT: (PERCENTAGE / 100) * net_premium
    - COMMISSION_AMOUNT_TAX: 
        If IS_TAXABLE=1: amount * (1 + tax_percentage/100)
        If IS_TAXABLE=0: amount
    
    Returns: List of enriched commission dicts
    """
```

**Example Calculation:**

```python
# Policy:
# - Net premium: 1000 EGP
# - Issue date: 2026-01-01
# - Payment date: 2026-03-15 (73 days later)

# Commission setup from FGL_NETTING_SETUP:
# - FROM_DAY: 60
# - TO_DAY: 90
# - PERCENTAGE: 3.5
# - IS_TAXABLE: 1
# - TAX_PER: 21

# Calculation:
# COMMISSION_AMOUNT = (3.5 / 100) * 1000 = 35.00
# TAX_AMOUNT = 35.00 * (21 / 100) = 7.35
# COMMISSION_AMOUNT_TAX = 35.00 + 7.35 = 42.35
```

---

### Private Methods

#### 1. **get_day_difference_issue_paydate**

```python
def get_day_difference_issue_paydate(self, issue_date, payment_date: str) -> int:
    """
    Return the number of days between issue_date and payment_date.
    """
```

---

#### 2. **__get_cost_center_agent_account_agent_id**

```python
def __get_cost_center_agent_account_agent_id(self) -> list[dict[str, Any]]:
    """
    Return FGL_CPC_ID, FGL_COA_ID, FCS_AGT_ID, EFFECTIVE_DATE for this policy.
    
    Uses EXISTS instead of JOIN on gpd_ins_shares to avoid duplicate rows
    when a single installment has multiple ROLE_TYPE shares.
    """
```

---

#### 3. **__get_agent_id**

```python
def __get_agent_id(self) -> int | None:
    """
    Return the agent customer ID (ROLE_TYPE=3) for this policy.
    """
```

---

#### 4. **__get_net_premium_issue_date**

```python
def __get_net_premium_issue_date(self) -> tuple:
    """
    Return (net_premium, issue_date) for this policy.
    """
```

---

#### 5. **__calc_day_commission**

```python
def __calc_day_commission(
    self,
    type_code: int,
    fgl_cpc_id,
    fgl_coa_id,
    fcs_agt_id,
    day_difference: int,
) -> list[dict[str, Any]]:
    """
    Query FGL_NETTING_SETUP for a matching commission rule.
    
    Criteria:
    - FGL_CPC_ID: Cost center
    - REV_FGL_COA_ID: Account
    - :diff_days BETWEEN FROM_DAY AND TO_DAY
    - COMM_TYPE: Commission type (1, 2, or 6)
    - FCS_AGT_ID: Agent (optional, falls back to NULL match)
    """
```

---

#### 6. **__get_tax_per**

```python
def __get_tax_per(self, customer_id, account_id) -> list[dict[str, Any]]:
    """
    Return the tax PERCENTAGE for a given customer and account.
    
    Query: FCS_CUSTOMERS → FCS_CUSTOMER_TAXES
    """
```

---

## 🔄 Processing Pipeline

### Complete Flow with Class/Method Mapping

```
User uploads CSV file
       │
       ▼
┌──────────────────────────────────┐
│ UploadService                    │
│ .process_excel_file()            │
│                                  │
│ - Parse CSV                      │
│ - Validate policy numbers        │
│ - Insert STG_POLICIES            │
│ - Return results                 │
└──────────────────────────────────┘
       │
       ▼
PolicyRepository.get_pending_policies()
       │
       ▼
┌──────────────────────────────────────────────┐
│ ProcessService.process_single_policy()       │
├──────────────────────────────────────────────┤
│                                              │
│ Step 1: _step1_validate_policy()             │
│         └─ Lookup in GPD_POLICIES            │
│                                              │
│ Step 2: _step2_create_collection()           │
│         └─ Insert FCM_COLLECTION             │
│                                              │
│ Step 3: _step3_resolve_fgl()                 │
│         └─ Get FGL_TRN_ID + FGL_INV_SERIAL   │
│         └─ VALIDATE transaction state        │
│                                              │
│ Step 4: _step4_col_details()                 │
│         └─ Insert FCM_COL_DETAILS            │
│                                              │
│ Step 5: _step5_col_payments()                │
│         └─ Insert FCM_COL_PAYMENTS           │
│                                              │
│ Step 6: _step6_col_summary()                 │
│         └─ Insert FCM_COL_SUMMARY            │
│                                              │
│ Step 7: _step7_cost_center()                 │
│         └─ Get FGL_CPC_ID                    │
│                                              │
│ Step 8: _step8_check_commission()            │
│         └─ CommCalc.calc_commission_amount() │
│                                              │
│    ├─ Branch A: Has Commission               │
│    │  _step8a_process_commissions()          │
│    │  │                                      │
│    │  ├─ VIRTUAL_COMMISSION + DETAILS        │
│    │  ├─ For each commission type:           │
│    │  │  _phase2_create_cn()                 │
│    │  │  ├─ fetch_main_infos()               │
│    │  │  ├─ Insert FGL_TRANSACTIONS (CN)     │
│    │  │  └─ commit_journal_no()              │
│    │  ├─ Status: 1→2→3                       │
│    │  └─ WAIT 60 seconds (ERP trigger)       │
│    │                                         │
│    └─ Branch B: No Commission                │
│       _step8b_no_commission()                │
│       ├─ Status: 1→2→3                       │
│       └─ WAIT 60 seconds                     │
│                                              │
│ Step 9: _step9_rct_transaction()             │
│         ├─ fetch_main_infos()                │
│         ├─ Insert FGL_TRANSACTIONS (RCT)     │
│         ├─ Insert FGL_TRANS_DETAILS          │
│         ├─ _step9_nettings()                 │
│         │  └─ Create FGL_NETTINGS            │
│         └─ commit_journal_no()               │
│                                              │
│ Step 10: _step10_accept_policy()             │
│          ├─ Insert ACCEPT_POLICIES           │
│          └─ Delete from STG_POLICIES         │
│                                              │
└──────────────────────────────────────────────┘
       │
       ▼
   [SUCCESS]
       │
       ▼
ReportService.get_process_report()
```

---

## ⚠️ Error Handling

### ORA-20074: INCORRECT TRANSACTION ID

**Prevention in Step 3:**

```python
# _step3_resolve_fgl() validates:
cur.execute("""
    SELECT COUNT(*)
      FROM ERP.FGL_TRANSACTIONS
     WHERE ID = :id 
       AND IS_DELETED = 0 
       AND IS_POSTED = 1
""", {"id": fgl_trn_id})
```

**Root Causes:**
- Transaction doesn't exist
- Transaction marked deleted (IS_DELETED=1)
- Transaction not posted (IS_POSTED!=1)
- Transaction has no detail lines

---

### Rollback on Step Failure

```python
# Any step raises _StepError:
try:
    self._stepN_...()
except _StepError as e:
    self._rollback_collection(conn, cur, new_id)
    return {'success': False, 'error': str(e)}
```

**Rollback deletes:**
- FCM_COLLECTION
- FCM_COL_DETAILS, PAYMENTS, SUMMARY
- VIRTUAL_COMMISSION, DETAILS
- FGL_TRANSACTIONS, TRANS_DETAILS

**Rollback resets:**
- STG_POLICIES back to processed_flag=1

---

## 📝 Code Examples

### Example 1: Upload CSV

```python
from collection.repositories import PolicyRepository, OracleConnectionRepository
from collection.services import UploadService

# Initialize
conn_repo = OracleConnectionRepository()
policy_repo = PolicyRepository(conn_repo)
upload_service = UploadService(policy_repo)

# Process file
result = upload_service.process_excel_file('/path/to/policies.xlsx')

print(f"Total: {result['total_count']}")
print(f"Success: {result['success_count']}")
print(f"Errors: {result['error_count']}")

for row in result['results']:
    if row['STATUS'] != 'SUCCESS':
        print(f"  {row['POLICY_NUMBER']}: {row['ERROR_MSG']}")
```

---

### Example 2: Process Single Policy

```python
from collection.services import ProcessService

process_service = ProcessService(policy_repo)

result = process_service.process_single_policy('POL-2026-001')

if result['success']:
    print(f"✓ Collection ID: {result['collection_id']}")
else:
    print(f"✗ Error at step {result['step_completed']}")
    print(f"  Message: {result['error']}")
```

---

### Example 3: Calculate Commissions

```python
from collection.CommFact import CommCalc
import oracledb

# Oracle connection
conn = oracledb.connect(
    user='ERP_DML',
    password='147@963',
    dsn='dbsim-scan.egtak.local:1521/takaful'
)

# Calculate
calc = CommCalc(conn, 'POL-2026-001', '2026-03-15')
commissions = calc.calc_commission_amount()

for comm in commissions:
    print(f"Type {comm['COMM_TYPE']}: "
          f"{comm['COMMISSION_AMOUNT']} "
          f"(Tax: {comm['COMMISSION_AMOUNT_TAX']})")
```

---

### Example 4: Transaction Header Building

```python
from collection.utils import fetch_main_infos, commit_journal_no

# Get header info for CN transaction
result = fetch_main_infos(
    cur, conn,
    new_serial=100,
    new_id=1,
    net_premium_val=1000.0,
    exrate=1.0,
    status="credit_note",
    cn_type=6  # Basic commission
)

if result['success']:
    data = result['main_info']
    print(f"ID: {data['id_retrieved']}")
    print(f"Journal No: {data['journal_no']}")
    print(f"Segment Code: {data['segment_code']}")
    
    # After INSERT succeeds:
    commit_journal_no(cur, conn, data['journal_trt_code'], data['journal_fyr_year'])
```

---

**Last Updated**: May 18, 2026  
**Version**: 1.0.0  
**Status**: Production Ready
