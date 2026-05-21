# Database Schema Documentation

This document describes all tables in the Policy Management system database.

---

## Overview

The system uses a combination of:
- **Oracle Database**: For external policy and collection data (managed externally, `managed = False`)
- **SQLite Database**: For local application data and job tracking

---

## Complete Table Reference

### All Tables in the System

**Oracle Tables (External)**:
1. FGL_INVOICES
2. FGL_INVOICE_DETAILS
3. FGL_TRANSACTIONS
4. FGL_NETTINGS
5. FGL_NETTING_SETUP
6. FCM_COLLECTION
7. FCM_COL_HISTORY
8. FCM_COL_DETAILS
9. FCM_COL_PAYMENTS
10. FCM_COL_SUMMARY
11. FCM_VIRTUAL_COMMISSION
12. FCM_VIRTUAL_COMM_DETAILS
13. STG_POLICIES
14. ACCEPT_POLICIES

**SQLite Tables (Local)**:
1. collection_bulkinsertjob

---

## Oracle Tables (External)

### 1. FGL_INVOICES
**Description**: Stores invoice records with payment tracking information.

**Django Model**: `fglInvoices`

| Field | Type | Description |
|-------|------|-------------|
| `serial` | INT | Primary Key - Invoice serial number |
| `due_date` | DATE | Date when invoice is due |
| `amount` | DECIMAL(18,3) | Invoice amount in foreign currency |
| `amount_lc` | DECIMAL(18,3) | Invoice amount in local currency |
| `exrate` | DECIMAL(18,3) | Exchange rate used |
| `status` | VARCHAR(1) | Invoice status code |
| `status_date` | DATE | Date when status was updated |
| `due_amount` | DECIMAL(18,3) | Outstanding due amount |
| `fgl_trn_id` | INT | FGL Transaction ID |
| `fgl_trd_serial` | INT | FGL Trade serial number |
| `fcs_cst_id` | INT | Customer ID reference |
| `fcr_trt_code` | VARCHAR(6) | Treatment code |
| `due_amount_fc` | DECIMAL(18,3) | Outstanding due amount in foreign currency |

**Database Table Name**: `FGL_INVOICES`

**Managed by Django**: No (external table)

---

### 2. STG_POLICIES
**Description**: Staging table for policies being uploaded and processed. Policies are uploaded here first, then validated and moved to ACCEPT_POLICIES.

**Django Model**: `StgPolicy`

| Field | Type | Description |
|-------|------|-------------|
| `id` | INT | Primary Key - Auto-generated ID |
| `element_id` | VARCHAR(255) | External element identifier |
| `policy_number` | VARCHAR(255) | Policy number (required) |
| `created_at` | DATETIME | Timestamp when record was created |
| `processed_flag` | INT | Flag indicating processing status (default: 1) |
| `processed_at` | DATETIME | Timestamp when processing completed |
| `error_msg` | TEXT | Error message if processing failed |
| `modified` | BOOLEAN | Flag indicating if record was modified |
| `modified_date` | DATETIME | Timestamp of last modification |

**Database Table Name**: `STG_POLICIES`

**Managed by Django**: No (external table)

**Workflow**: Upload → Validate → Process → Move to ACCEPT_POLICIES

---

### 3. ACCEPT_POLICIES
**Description**: Table for policies that have been successfully processed and accepted. These policies have passed validation and collections have been created for them.

**Django Model**: `AcceptPolicy`

| Field | Type | Description |
|-------|------|-------------|
| `id` | INT | Primary Key - Auto-generated ID |
| `element_id` | VARCHAR(255) | External element identifier |
| `policy_number` | VARCHAR(255) | Policy number (required) |
| `created_at` | DATETIME | Timestamp when record was created |
| `processed_flag` | INT | Flag indicating processing status (default: 0) |
| `processed_at` | DATETIME | Timestamp when processing completed |
| `fcm_collection_id` | INT | Reference to created collection ID |
| `error_msg` | TEXT | Error message if processing failed |
| `modified` | BOOLEAN | Flag indicating if record was modified |
| `modified_date` | DATETIME | Timestamp of last modification |

**Database Table Name**: `ACCEPT_POLICIES`

**Managed by Django**: No (external table)

---

### 5. FGL_INVOICE_DETAILS
**Description**: Detailed line items for invoices. Breaks down invoice amounts by category/detail type.

**Database Table Name**: `FGL_INVOICE_DETAILS`

**Managed by Django**: No (external table)

---

### 6. FGL_TRANSACTIONS
**Description**: Transaction records associated with invoices and collections. Tracks all financial transaction events.

**Database Table Name**: `FGL_TRANSACTIONS`

**Managed by Django**: No (external table)

---

### 7. FGL_NETTINGS
**Description**: Netting records for offsetting invoices and payments.

**Database Table Name**: `FGL_NETTINGS`

**Managed by Django**: No (external table)

---

### 8. FGL_NETTING_SETUP
**Description**: Configuration table for commission calculation rules. Defines commission rates based on segment code, day differences, and cost center information.

**Key Fields**:
- Segment code
- Day difference ranges (issue date to payment date)
- Commission type (1=Early, 2=Collection, 6=Basic)
- Commission percentage/amount
- Cost center and account configurations

**Database Table Name**: `FGL_NETTING_SETUP`

**Managed by Django**: No (external table)

**Usage**: Used by CommCalc for calculating commissions on policies.

---

### 9. FCM_COLLECTION
**Description**: Main collection records created from accepted policies. Tracks collection lifecycle for each policy.

**Database Table Name**: `FCM_COLLECTION`

**Managed by Django**: No (external table)

**Reference**: `AcceptPolicy.fcm_collection_id` links to this table.

---

### 10. FCM_COL_HISTORY
**Description**: History/audit trail for collection status changes. Records all state transitions for collections.

**Database Table Name**: `FCM_COL_HISTORY`

**Managed by Django**: No (external table)

---

### 11. FCM_COL_DETAILS
**Description**: Detailed line items for collections. Breaks down collection amounts by policy/invoice detail.

**Database Table Name**: `FCM_COL_DETAILS`

**Managed by Django**: No (external table)

---

### 12. FCM_COL_PAYMENTS
**Description**: Payments received against collections. Tracks partial and full payments.

**Database Table Name**: `FCM_COL_PAYMENTS`

**Managed by Django**: No (external table)

---

### 13. FCM_COL_SUMMARY
**Description**: Summary statistics for collections. Aggregates data for reporting purposes.

**Database Table Name**: `FCM_COL_SUMMARY`

**Managed by Django**: No (external table)

---

### 14. FCM_VIRTUAL_COMMISSION
**Description**: Virtual commission records calculated but not yet realized. Used for reporting projected commissions.

**Database Table Name**: `FCM_VIRTUAL_COMMISSION`

**Managed by Django**: No (external table)

---

### 15. FCM_VIRTUAL_COMM_DETAILS
**Description**: Detailed line items for virtual commissions. Breaks down virtual commission amounts.

**Database Table Name**: `FCM_VIRTUAL_COMM_DETAILS`

**Managed by Django**: No (external table)



## SQLite Tables (Local)

### 16. collection_bulkinsertjob
**Description**: Tracks background bulk insert jobs for queued policy uploads and processing automation.

**Django Model**: `BulkInsertJob`

| Field | Type | Description |
|-------|------|-------------|
| `id` | INT | Primary Key - Auto-generated |
| `created_at` | DATETIME | When the job was created |
| `started_at` | DATETIME | When the job started executing |
| `finished_at` | DATETIME | When the job completed |
| `status` | VARCHAR(16) | Job status: PENDING, RUNNING, SUCCESS, ERROR |
| `source_filename` | VARCHAR(255) | Name of the source file uploaded |
| `source_path` | TEXT | Full path to the source file |
| `total_rows` | INT | Total rows in the source file |
| `success_rows` | INT | Number of successfully processed rows |
| `error_rows` | INT | Number of rows that failed processing |
| `error_message` | TEXT | Detailed error message if job failed |
| `modified` | BOOLEAN | Flag indicating if record was modified |
| `modified_date` | DATETIME | Timestamp of last modification |

**Database Table Name**: `collection_bulkinsertjob`

**Managed by Django**: Yes (Django creates/manages this table)

**Status Values**:
- `PENDING` - Job waiting to be processed
- `RUNNING` - Job currently executing
- `SUCCESS` - Job completed successfully
- `ERROR` - Job encountered an error

---

## Data Flow Diagram

```
Upload File
    ↓
Create BulkInsertJob (status=PENDING)
    ↓
Process Job (status=RUNNING)
    ↓
Parse File → Insert to STG_POLICIES
    ↓
Validate & Calculate Commissions
    ↓
Move to ACCEPT_POLICIES / Handle Errors
    ↓
Update BulkInsertJob (status=SUCCESS/ERROR)
    ↓
Create Collections (fcm_collection_id)
```

---

## Key Relationships

- **STG_POLICIES** → **ACCEPT_POLICIES**: Policies move here after successful processing
- **ACCEPT_POLICIES** → **FGL_INVOICES**: References invoices for commission calculation
- **AcceptPolicy.fcm_collection_id** → **FCM_COLLECTION**: Links to created collections
- **BulkInsertJob** → **STG_POLICIES**: Tracks which job inserted which policies
- **FGL_INVOICES** → **FGL_INVOICE_DETAILS**: Invoice header to line items
- **FGL_INVOICES** → **FGL_TRANSACTIONS**: Invoice transaction history
- **FGL_NETTING_SETUP**: Used by commission calculation engine (CommCalc)
- **FCM_COLLECTION** → **FCM_COL_HISTORY**: Collection status audit trail
- **FCM_COLLECTION** → **FCM_COL_DETAILS**: Collection to line items
- **FCM_COLLECTION** → **FCM_COL_PAYMENTS**: Collection to payments received
- **FCM_COLLECTION** → **FCM_COL_SUMMARY**: Collection statistics and aggregates
- **FCM_VIRTUAL_COMMISSION** → **FCM_VIRTUAL_COMM_DETAILS**: Virtual commission breakdown

---

## Notes

- All Oracle tables are managed externally; Django does not handle their migrations
- SQLite table (BulkInsertJob) is managed by Django and follows Django migration patterns
- All timestamps are automatically managed by the application
- Commission calculations are based on FGL_NETTING_SETUP using policies, invoices, and day differences
- Total of **15 Oracle tables** + **1 SQLite table** = **16 tables** in the system
