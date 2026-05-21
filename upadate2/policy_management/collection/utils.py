# -*- coding: utf-8 -*-
"""
Transaction Utilities

fetch_main_infos  — Build the header block needed before inserting a
                    FGL_TRANSACTIONS row (next ID, journal number, segment code,
                    amounts, notes).
commit_journal_no — Persist the JOURNAL_NO increment to FGL_JOURNAL_SEGMENT
                    after a successful FGL_TRANSACTIONS INSERT.

JOURNAL_NO rules
----------------
  CN  (credit_note) → FCR_TRT_CODE = 7
  RV  (cashier)     → FCR_TRT_CODE = 2

  Both: use stored_value + 1 as journal_no for the new transaction.
  After a successful commit, call commit_journal_no() so the next transaction
  gets the next number in sequence.

SEGMENT CODE
------------
  Always built from journal_no (from FGL_JOURNAL_SEGMENT), NOT from the
  MAX(ID) PK value.  The PK is globally unique but journal_no resets each
  fiscal year — embedding the PK in the segment code would cause collisions
  across years.
"""

import sys
import datetime

# Ensure UTF-8 output on Windows (charmap) environments
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# fetch_main_infos
# ─────────────────────────────────────────────────────────────────────────────

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
        cur:            Active DB cursor (must be inside an open connection).
        conn:           Oracle connection (used for rollback on error).
        new_serial:     FCM_COLLECTION.SERIAL of the current collection.
        new_id:         FCM_COLLECTION.ID of the current collection.
        net_premium_val: Net premium value used to calculate amounts.
        exrate:         Exchange rate.
        status:         "cashier" or "credit_note".
        cn_type:        Commission type (1/2/6) — used for the notes string.

    Returns:
        {'success': True,  'main_info': {...}} on success
        {'success': False, 'error':     str}   on failure
    """
    try:
        # ── Validate status ───────────────────────────────────────────────────
        if status == "cashier":
            prefix   = "RV-RCT-01"
            trt_code = 2          # RV → FCR_TRT_CODE = 2
        elif status == "credit_note":
            prefix   = "CN-COL-01"
            trt_code = 7          # CN → FCR_TRT_CODE = 7
        else:
            return {"success": False, "error": f"Invalid status: {status}"}

        # ── Next FGL_TRANSACTIONS.ID ──────────────────────────────────────────
        # Use the Oracle sequence — safe for concurrent sessions, no MAX()+1 race.
        print("[TRANSACTION] Fetching next ID from ERP.FGL_TRANSACTIONS_SEQ")
        cur.execute("SELECT ERP.FGL_TRANSACTIONS_SEQ.NEXTVAL FROM DUAL")
        id_retrieved = cur.fetchone()[0]
        print(f"[TRANSACTION]   Next Transaction ID: {id_retrieved}")

        # ── JOURNAL_NO from FGL_JOURNAL_SEGMENT ──────────────────────────────
        current_year_int = datetime.datetime.now().year
        journal_no       = None

        print(f"[TRANSACTION] Reading JOURNAL_NO from ERP.FGL_JOURNAL_SEGMENT "
              f"(FCR_TRT_CODE={trt_code}, FCR_FYR_YEAR={current_year_int})")
        try:
            cur.execute("""
                SELECT JOURNAL_NO
                  FROM ERP.FGL_JOURNAL_SEGMENT
                 WHERE FCR_FYR_YEAR = :year
                   AND FCR_TRT_CODE = :trt_code
            """, {"year": current_year_int, "trt_code": trt_code})
            row = cur.fetchone()
            if row and row[0] is not None:
                journal_no = int(row[0]) + 1
                print(f"[TRANSACTION]   Stored JOURNAL_NO={row[0]} → using next={journal_no}")
            else:
                journal_no = 1
                print(f"[TRANSACTION WARNING] No FGL_JOURNAL_SEGMENT row for "
                      f"year={current_year_int}, TRT_CODE={trt_code} — starting at 1")
        except Exception as journal_err:
            # Fall back to the globally unique PK on any DB error
            print(f"[TRANSACTION WARNING] Error reading FGL_JOURNAL_SEGMENT: {journal_err}")
            journal_no = id_retrieved
            print(f"[TRANSACTION]   Fallback: journal_no={journal_no} (from id_retrieved)")

        # ── Segment code ──────────────────────────────────────────────────────
        current_year_short = datetime.datetime.now().strftime("%y")
        current_month      = datetime.datetime.now().strftime("%m")
        segment_code_val   = f"{prefix}-{current_year_short}-{current_month}-{str(journal_no).zfill(6)}"
        print(f"[TRANSACTION]   Segment code: {segment_code_val}")

        # Uniqueness guard
        cur.execute(
            "SELECT COUNT(*) FROM ERP.FGL_TRANSACTIONS WHERE SEGMENT_CODE = :seg_code",
            {"seg_code": segment_code_val},
        )
        if cur.fetchone()[0] > 0:
            print(f"[TRANSACTION ERROR] Segment code '{segment_code_val}' already exists")
            conn.rollback()
            return {
                "success": False,
                "error":   f"Segment code '{segment_code_val}' already exists. Please retry.",
            }

        # ── Sub-transaction types (informational) ─────────────────────────────
        cur.execute("SELECT ID, NAME2 FROM ERP.FGL_SUB_TRANSACTION_TYPES WHERE FCR_TRT_CODE = 7")
        sub_credit = cur.fetchall()
        cur.execute("SELECT ID, NAME2 FROM ERP.FGL_SUB_TRANSACTION_TYPES WHERE FCR_TRT_CODE = 6")
        sub_debit  = cur.fetchall()
        print(f"[TRANSACTION]   Credit sub-types: {len(sub_credit)}, Debit sub-types: {len(sub_debit)}")

        # ── Notes ─────────────────────────────────────────────────────────────
        type_text_map = {6: "Basic", 1: "Early", 2: "Collection"}
        cn_type_name  = type_text_map.get(cn_type)

        if cn_type_name:
            notes_val = f"{cn_type_name} الاعمولة الصادرة عن حافظة توريد رقم {new_serial}"
        elif status == "cashier":
            notes_val = f"سند القبض الناتج عن حافظة توريد رقم {new_serial}"
        elif status == "credit_note":
            notes_val = f"سند القيد الناتج عن حافظة توريد رقم {new_serial}"
        else:
            notes_val = f"حافظة توريد رقم {new_serial}"

        username = "Paymob"

        # ── Amount from FCM_COLLECTION ────────────────────────────────────────
        print(f"[TRANSACTION] Fetching TOTAL_DEBIT from FCM_COLLECTION ID={new_id}")
        cur.execute("SELECT TOTAL_DEBIT FROM ERP.FCM_COLLECTION WHERE ID = :id", {"id": new_id})
        result = cur.fetchone()
        fcm_amount   = result[0] if result else net_premium_val
        insert_amount = round(float(fcm_amount), 5)

        current_year = str(current_year_int)

        print(f"[TRANSACTION]   ID={id_retrieved}, JOURNAL_NO={journal_no}, "
              f"Amount={insert_amount}, SegCode={segment_code_val}")

        main_info = {
            "id_retrieved":     id_retrieved,
            "journal_no":       journal_no,
            "segment_code":     segment_code_val,
            "notes":            notes_val,
            "note":             notes_val,
            "insert_amount":    insert_amount,
            "insert_amount_lc": insert_amount,   # EGP only
            "exrate":           exrate,
            "username":         username,
            "current_year":     current_year,
            "current_month":    current_month,
            # For commit_journal_no() calls by the caller
            "journal_trt_code": trt_code,
            "journal_fyr_year": current_year_int,
        }
        return {"success": True, "main_info": main_info}

    except Exception as e:
        print(f"[TRANSACTION ERROR] fetch_main_infos failed: {e}")
        conn.rollback()
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# commit_journal_no
# ─────────────────────────────────────────────────────────────────────────────

def commit_journal_no(cur, conn, trt_code: int, fyr_year: int) -> None:
    """
    Persist the JOURNAL_NO increment to FGL_JOURNAL_SEGMENT after a
    successful FGL_TRANSACTIONS INSERT.

    Call pattern
    ────────────
      CN loop (Phase 2):
          After conn.commit() for each CN transaction:
              commit_journal_no(cur, conn, 7, data['journal_fyr_year'])

      RV (Step 9):
          After conn.commit() for the RCT row:
              commit_journal_no(cur, conn, 2, data['journal_fyr_year'])

    Implementation
    ──────────────
      Uses MERGE so it is safe whether the row already exists or not:
        MATCHED     → JOURNAL_NO = JOURNAL_NO + 1
        NOT MATCHED → INSERT with JOURNAL_NO = 1 (first of the fiscal year)
    """
    print(f"[TRANSACTION] Incrementing FGL_JOURNAL_SEGMENT (TRT_CODE={trt_code}, year={fyr_year})")
    try:
        cur.execute("""
            MERGE INTO ERP.FGL_JOURNAL_SEGMENT tgt
            USING (
                SELECT :trt_code AS trt_code,
                       :fyr_year AS fyr_year
                  FROM DUAL
            ) src
               ON (tgt.FCR_TRT_CODE = src.trt_code AND tgt.FCR_FYR_YEAR = src.fyr_year)
             WHEN MATCHED THEN
                 UPDATE SET tgt.JOURNAL_NO = tgt.JOURNAL_NO + 1
             WHEN NOT MATCHED THEN
                 INSERT (FCR_TRT_CODE, FCR_FYR_YEAR, JOURNAL_NO)
                 VALUES (src.trt_code, src.fyr_year, 1)
        """, {"trt_code": trt_code, "fyr_year": fyr_year})
        conn.commit()
        print(f"[TRANSACTION]   FGL_JOURNAL_SEGMENT committed (TRT_CODE={trt_code}, +1)")
    except Exception as e:
        print(f"[TRANSACTION WARNING] commit_journal_no failed (TRT_CODE={trt_code}): {e} — sequence may drift")