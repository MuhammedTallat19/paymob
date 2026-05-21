"""
Collection Services Module

Business logic for processing insurance policies into ERP collections.

Classes:
    UploadService    — parse & stage CSV uploads
    ProcessService   — full 10-step policy processing pipeline
    ReportService    — upload / process report retrieval
    DashboardService — daily metrics
    TemplateService  — CSV template / report download

ProcessService.process_single_policy flow
─────────────────────────────────────────
  Step 1  Validate policy in GPD_POLICIES
  Step 2  Create FCM_COLLECTION (status=1) + history
  Step 3  Resolve FGL_TRN_ID and FGL_INV_SERIAL
  Step 4  Insert FCM_COL_DETAILS
  Step 5  Insert FCM_COL_PAYMENTS
  Step 6  Insert FCM_COL_SUMMARY
  Step 7  Fetch cost center (FGL_CPC_ID)
  Step 8  Commission branch
            A — commission exists: VIRTUAL_COMMISSION + DETAILS, status 1→2→3,
                create one CN transaction per commission type
            B — no commission: status 1→2→3 directly
          *** 60-second wait before Step 9 (ERP trigger requirement) ***
  Step 9  RCT financial transaction + FGL_TRANS_DETAILS + FGL_NETTINGS
  Step 10 Insert ACCEPT_POLICIES, delete from STG_POLICIES

Any step failure rolls back the whole collection.
"""

import io
import os
import datetime
import traceback
import time

import pandas as pd
from django.core.cache import cache
from django.utils import timezone
from django.conf import settings
from typing import Any, Dict, List, Optional, Tuple

from .repositories import PolicyRepository, DashboardRepository, ReportRepository
from .CommFact import CommCalc
from .utils import fetch_main_infos, commit_journal_no


# ─────────────────────────────────────────────────────────────────────────────
# Internal sentinel exception
# ─────────────────────────────────────────────────────────────────────────────

class _StepError(Exception):
    """Raised by _step_* methods to abort the whole transaction."""


# ─────────────────────────────────────────────────────────────────────────────
# UploadService
# ─────────────────────────────────────────────────────────────────────────────

class UploadService:
    """Parse a CSV file and stage valid policy numbers for processing."""

    def __init__(self, policy_repo: PolicyRepository):
        self.policy_repo = policy_repo

    def allowed_file(self, filename: str) -> bool:
        return "." in filename and filename.rsplit(".", 1)[1].lower() in settings.ALLOWED_EXTENSIONS

    def process_excel_file(self, filepath: str) -> Dict[str, Any]:
        df = pd.read_csv(filepath, encoding="utf-8-sig")
        if "POLICY_NUMBER" not in df.columns:
            raise ValueError("الأعمدة المطلوبة غير موجودة: POLICY_NUMBER")

        has_element    = "ELEMENT_ID" in df.columns
        success_count  = 0
        error_count    = 0
        results        = []

        for _, row in df.iterrows():
            policy_number = str(row["POLICY_NUMBER"]).strip()
            element_id    = str(row.get("ELEMENT_ID", "")).strip() if has_element else ""

            if not policy_number or policy_number.lower() in ("nan", "none", ""):
                error_count += 1
                results.append({"POLICY_NUMBER": policy_number, "STATUS": "ERROR", "ERROR_MSG": "رقم policy فارغ"})
                continue

            if self.policy_repo.check_existing_policy(policy_number):
                error_count += 1
                results.append({"POLICY_NUMBER": policy_number, "STATUS": "ERROR", "ERROR_MSG": "تم رفع هذه الوثيقة مسبقاً خلال هذا الشهر"})
                continue

            if self.policy_repo.insert_stg_policy(policy_number, element_id):
                success_count += 1
                results.append({"POLICY_NUMBER": policy_number, "STATUS": "SUCCESS", "ERROR_MSG": ""})
            else:
                error_count += 1
                results.append({"POLICY_NUMBER": policy_number, "STATUS": "ERROR", "ERROR_MSG": "خطأ في الإدراج"})

        cache.set("LAST_UPLOAD_REPORT", {
            "timestamp":     timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filename":      os.path.basename(filepath),
            "success_count": success_count,
            "error_count":   error_count,
            "rows":          results,
        }, timeout=86400)

        return {"success_count": success_count, "error_count": error_count, "rows": results}


# ─────────────────────────────────────────────────────────────────────────────
# ProcessService
# ─────────────────────────────────────────────────────────────────────────────

class ProcessService:
    """
    Execute the full 10-step collection processing pipeline for a single policy.

    See module docstring for the step-by-step flow.
    """

    # Seconds to wait between the last CN commit and the RCT INSERT
    CN_TO_RCT_DELAY_SECONDS = 0

    def __init__(self, policy_repo: PolicyRepository):
        self.policy_repo = policy_repo

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    def process_single_policy(self, policy_number: str) -> Dict[str, Any]:
        conn = cur = None
        new_id = new_serial = None

        try:
            print(f"\n{'=' * 80}")
            print(f"[TRANSACTION] process_single_policy — {policy_number}")
            print(f"{'=' * 80}")

            conn = self.policy_repo.conn_repo.get_connection()
            cur  = conn.cursor()

            # Duplicate guard
            cur.execute(f"SELECT COUNT(*) FROM {self.policy_repo.conn_repo._stg}ACCEPT_POLICIES WHERE POLICY_NUMBER = :pn", {"pn": policy_number})
            if cur.fetchone()[0] > 0:
                return {"success": False, "error": "Policy already processed"}

            # Steps 1–7
            policy_ctx           = self._step1_validate_policy(cur, policy_number)
            new_id, new_serial   = self._step2_create_collection(cur, conn, policy_number, policy_ctx)
            fgl_trn_id, fgl_inv_serial = self._step3_resolve_fgl(cur, conn, policy_ctx, new_id)
            self._step4_col_details(cur, conn, new_id, fgl_trn_id, fgl_inv_serial, policy_ctx)
            self._step5_col_payments(cur, conn, new_id, policy_ctx)
            self._step6_col_summary(cur, conn, new_id, fgl_trn_id, fgl_inv_serial, policy_ctx)
            cost_center_id = self._step7_cost_center(cur, policy_ctx)

            # Step 8 — commission branch
            skip_commission, commission_rows = self._step8_check_commission(
                cur, conn, policy_number, policy_ctx, fgl_trn_id, new_id
            )
            if not skip_commission:
                self._step8a_process_commissions(
                    cur, conn, policy_number, new_id, new_serial, fgl_trn_id,
                    fgl_inv_serial, cost_center_id, policy_ctx, commission_rows
                )
            else:
                self._step8b_no_commission(cur, conn, new_id)

            calc_flag = 0 if skip_commission else 1
            cur.execute(
                "UPDATE ERP.FCM_COLLECTION SET calc_flag = :f WHERE ID = :id",
                {"f": calc_flag, "id": new_id},
            )
            conn.commit()
            print(f"[TRANSACTION] calc_flag={calc_flag} committed")

            # Mandatory delay before RCT
            print(f"\n[TRANSACTION] Waiting {self.CN_TO_RCT_DELAY_SECONDS}s before RCT (ERP trigger requirement)...")
            time.sleep(self.CN_TO_RCT_DELAY_SECONDS)
            print("[TRANSACTION] Delay complete — proceeding to Step 9")

            # Steps 9–10
            self._step9_rct_transaction(
                cur, conn, new_id, new_serial, fgl_trn_id, fgl_inv_serial, policy_ctx, cost_center_id
            )
            self._step10_accept_policy(cur, conn, new_id, new_serial, policy_number, policy_ctx)

            conn.commit()
            print(f"[TRANSACTION] Policy {policy_number} complete!")
            print(f"{'=' * 80}\n")
            return {"success": True, "collection_id": new_id, "serial": new_serial}

        except _StepError as se:
            print(f"[TRANSACTION ERROR] Step failure: {se}")
            if conn:
                self._rollback_collection(conn, cur, new_id)
            return {"success": False, "error": str(se)}

        except Exception as e:
            print(f"[TRANSACTION ERROR] Unexpected error: {e}\n{traceback.format_exc()}")
            if conn:
                self._rollback_collection(conn, cur, new_id)
            return {"success": False, "error": str(e)}

        finally:
            if cur:  cur.close()
            if conn: conn.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Rollback helper
    # ─────────────────────────────────────────────────────────────────────────

    def _rollback_collection(self, conn, cur, new_id: Optional[int]) -> None:
        """Roll back the current transaction and hard-delete all written rows."""
        try:
            conn.rollback()
            print("[ROLLBACK] Transaction rolled back")
        except Exception as e:
            print(f"[ROLLBACK ERROR] rollback() failed: {e}")

        if new_id is None:
            print("[ROLLBACK] new_id is None — nothing to clean up")
            return

        cleanup_tables = [
            ("ERP.FCM_VIRTUAL_COMM_DETAILS", "FCM_COL_ID"),
            ("ERP.FCM_VIRTUAL_COMMISSION",   "FCM_COL_ID"),
            ("ERP.FCM_COL_HISTORY",          "FCM_COL_ID"),
            ("ERP.FCM_COL_PAYMENTS",         "FCM_COL_ID"),
            ("ERP.FCM_COL_SUMMARY",          "FCM_COL_ID"),
            ("ERP.FCM_COL_DETAILS",          "FCM_COL_ID"),
            ("ERP.FCM_COLLECTION",           "ID"),
        ]
        try:
            for table, col in cleanup_tables:
                try:
                    cur.execute(f"DELETE FROM {table} WHERE {col} = :id", {"id": new_id})
                    print(f"[ROLLBACK] Deleted {cur.rowcount} row(s) from {table}")
                except Exception as e:
                    print(f"[ROLLBACK WARNING] Could not delete from {table}: {e}")
            conn.commit()
            print("[ROLLBACK] Cleanup committed")
        except Exception as e:
            print(f"[ROLLBACK ERROR] Cleanup commit failed: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1 — Validate policy
    # ─────────────────────────────────────────────────────────────────────────

    def _step1_validate_policy(self, cur, policy_number: str) -> Dict[str, Any]:
        print(f"\n[TRANSACTION] Step 1: Validating policy {policy_number}")
        cur.execute("SELECT * FROM IGENERAL.GPD_POLICIES WHERE SEGMENT_CODE = :1", [policy_number])
        row = cur.fetchone()
        if not row:
            raise _StepError("بوليصة غير موجودة")

        cols   = [d[0] for d in cur.description]
        policy = dict(zip(cols, row))
        print(f"[TRANSACTION] Policy found — ID: {policy.get('ID')}")

        policy_id  = policy.get("ID") or policy.get("POLICY_ID")
        cst_id_acc = policy.get("FCS_CST_ID_ACC") or policy.get("FCS_CST_ID")

        if not policy_id:
            raise _StepError("معرف policy غير موجود")
        if not cst_id_acc:
            raise _StepError("FCS_CST_ID غير موجود في policy")

        cur.execute("SELECT COUNT(*) FROM ERP.FCS_CUSTOMERS WHERE ID = :1", [cst_id_acc])
        if cur.fetchone()[0] == 0:
            raise _StepError(f"FCS_CST_ID {cst_id_acc} غير موجود في FCS_CUSTOMERS")

        seg_code      = policy.get("SEGMENT_CODE") or policy_number
        currency_code = policy.get("CRG_CUR_CODE") or "EGP"

        try:
            net_premium_val = round(float(policy.get("NET_PREMIUM") or 0), 5)
        except Exception:
            net_premium_val = 0.0

        # Payment method
        payment_method = 4
        for key in ("PAYMENT_METHOD", "PAY_METHOD", "PAYMENT_METHOD_ID"):
            val = policy.get(key)
            if val not in (None, ""):
                payment_method = val
                break

        # Exchange rate
        exrate = 1.0
        try:
            cur.execute(
                "SELECT RATE FROM ERP.EXCHANGE_RATES WHERE CUR_CODE = :1 AND RATE_DATE = TRUNC(SYSDATE)",
                [currency_code],
            )
            rate_row = cur.fetchone()
            if rate_row and rate_row[0] is not None:
                exrate = float(rate_row[0])
        except Exception:
            pass

        # Customer name
        payee = "Unknown"
        try:
            cur.execute("SELECT NAME FROM ERP.FCS_CUSTOMERS WHERE ID = :1", [cst_id_acc])
            r = cur.fetchone()
            if r:
                payee = r[0]
        except Exception:
            pass

        # Branch ID
        crg_brn_id = 1241
        try:
            cur.execute("""
                SELECT bra.id FROM ICORE3.CRG_BRANCHES bra
                  JOIN IGENERAL.GPD_POLICIES pol ON bra.id = pol.CRG_BRN_ID
                 WHERE pol.SEGMENT_CODE = :1
            """, [policy_number])
            r = cur.fetchone()
            if r:
                crg_brn_id = r[0]
        except Exception:
            pass

        # Agent ID
        fcs_agt_id = None
        try:
            cur.execute("""
                SELECT FCS_CST_ID FROM IGENERAL.GPD_PLC_SHARES
                 WHERE GPD_PLC_ID = :1 AND ROLE_TYPE = 3
            """, [policy_id])
            r = cur.fetchone()
            if r:
                fcs_agt_id = r[0]
                if fcs_agt_id:
                    cur.execute("SELECT COUNT(*) FROM ERP.FCS_CUSTOMERS WHERE ID = :1", [fcs_agt_id])
                    if cur.fetchone()[0] == 0:
                        raise _StepError(f"FCS_AGT_ID {fcs_agt_id} غير موجود")
        except _StepError:
            raise
        except Exception:
            pass

        print(f"[TRANSACTION] Step 1 complete — id={policy_id}, cst={cst_id_acc}, "
              f"net_premium={net_premium_val}, exrate={exrate}")
        return {
            "policy_id":       policy_id,
            "seg_code":        seg_code,
            "net_premium_val": net_premium_val,
            "cst_id_acc":      cst_id_acc,
            "currency_code":   currency_code,
            "payment_method":  payment_method,
            "exrate":          exrate,
            "payee":           payee,
            "crg_brn_id":      crg_brn_id,
            "fcs_agt_id":      fcs_agt_id,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2 — Create FCM_COLLECTION
    # ─────────────────────────────────────────────────────────────────────────

    def _step2_create_collection(self, cur, conn, policy_number: str, ctx: Dict) -> Tuple[int, int]:
        print(f"\n[TRANSACTION] Step 2: Creating FCM_COLLECTION")
        try:
            cur.execute("SELECT ERP.FCM_COLLECTION_SEQ.NEXTVAL FROM DUAL")
            new_id = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(MAX(SERIAL), 0) + 1 FROM ERP.FCM_COLLECTION")
            new_serial = cur.fetchone()[0]
            print(f"[TRANSACTION] Collection ID={new_id}, Serial={new_serial}")

            cur.execute("""
                INSERT INTO ERP.FCM_COLLECTION (
                    id, serial, status_date, status, total_debit, total_credit,
                    fcs_cst_id, crg_com_id, created_by, creation_date,
                    modified_by, modification_date, collection_type, fgl_trn_id,
                    include_jv, fcs_agt_id, trans_type, fgl_csh_id, notes,
                    fcr_col_code, source, include_rv, include_pv, policy_no,
                    fcm_col_id, discard_exceeded_amount, notes2, crg_brn_id,
                    approval_notes, using_claims_refund, calc_flag,
                    is_subsidary_cst, group_result_by, import_fc_lc_flag
                ) VALUES (
                    :id, :serial, SYSDATE, 1, :total_debit, 0,
                    :fcs_cst_id, 1, :created_by, SYSDATE,
                    NULL, NULL, 1, NULL,
                    0, NULL, 3, NULL, NULL,
                    NULL, 'COL', 0, 0, :policy_no,
                    NULL, 1, NULL, :crg_brn_id,
                    NULL, 0, 0,
                    0, 1, 0
                )
            """, {
                "id":          new_id,
                "serial":      new_serial,
                "total_debit": ctx["net_premium_val"],
                "fcs_cst_id":  ctx["cst_id_acc"],
                "created_by":  "Paymob",
                "policy_no":   ctx["seg_code"],
                "crg_brn_id":  ctx["crg_brn_id"],
            })
            conn.commit()
            print("[TRANSACTION] FCM_COLLECTION inserted and committed")

            # History status=1
            cur.execute("SELECT ERP.FCM_COL_HISTORY_SEQ.NEXTVAL FROM DUAL")
            hist_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO ERP.FCM_COL_HISTORY (ID, FCM_COL_ID, USERNAME, STATUS, CREATION_DATE)
                VALUES (:id, :col_id, 'Paymob', 1, SYSDATE)
            """, {"id": hist_id, "col_id": new_id})

            # Default cashier
            cur.execute("UPDATE ERP.FCM_COLLECTION SET fgl_csh_id = 1923 WHERE ID = :id", {"id": new_id})
            conn.commit()
            print("[TRANSACTION] Step 2 complete")
            return new_id, new_serial

        except _StepError:
            raise
        except Exception as e:
            raise _StepError(f"Step 2 failed (FCM_COLLECTION): {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3 — Resolve FGL_TRN_ID and FGL_INV_SERIAL
    # ─────────────────────────────────────────────────────────────────────────

    def _step3_resolve_fgl(self, cur, conn, ctx: Dict, new_id: int) -> Tuple[int, int]:
        print(f"\n[TRANSACTION] Step 3: Resolving FGL_TRN_ID and FGL_INV_SERIAL")
        policy_id      = ctx["policy_id"]
        fgl_trn_id     = None
        fgl_inv_serial = None
        try:
            cur.execute("""
                SELECT trn.ID
                  FROM IGENERAL.GPD_POLICIES pol
                  JOIN IGENERAL.GPD_INSTALLMENTS ins ON pol.ID   = ins.GPD_PLC_ID
                  JOIN IGENERAL.GPD_VOUCHERS     vou ON ins.ID   = vou.GPD_INS_ID
                  JOIN ERP.FGL_TRANSACTIONS      trn ON vou.FGL_TRN_ID = trn.ID
                 WHERE pol.ID = :1 AND trn.FCR_TRT_CODE = 6 AND ROWNUM = 1
            """, [policy_id])
            r = cur.fetchone()
            if r:
                fgl_trn_id = r[0]
                print(f"[TRANSACTION] FGL_TRN_ID={fgl_trn_id}")

                # Primary invoice lookup
                cur.execute("""
                    SELECT inv.SERIAL
                      FROM ERP.FGL_TRANSACTIONS  trn
                 LEFT JOIN IGENERAL.GPD_VOUCHERS vsh ON trn.ID    = vsh.FGL_TRN_ID
                 LEFT JOIN IGENERAL.GPD_INSTALLMENTS ins ON vsh.GPD_INS_ID = ins.ID
                 LEFT JOIN IGENERAL.GPD_POLICIES pol ON ins.GPD_PLC_ID = pol.ID
                 LEFT JOIN ERP.FGL_INVOICES      inv ON trn.ID    = inv.FGL_TRN_ID
                     WHERE pol.ID = :1 AND trn.FCR_TRT_CODE = 6
                """, [policy_id])
                r2 = cur.fetchone()
                if r2:
                    fgl_inv_serial = r2[0]
                else:
                    # Fallback invoice lookup
                    cur.execute("""
                        SELECT i.SERIAL
                          FROM ERP.FGL_INVOICES       i
                     LEFT JOIN ERP.FGL_TRANSACTIONS   trn ON i.FGL_TRN_ID   = trn.ID
                     LEFT JOIN IGENERAL.GPD_VOUCHERS  vsh ON trn.ID          = vsh.FGL_TRN_ID
                     LEFT JOIN IGENERAL.GPD_INSTALLMENTS ins ON vsh.GPD_INS_ID = ins.ID
                     LEFT JOIN IGENERAL.GPD_POLICIES   pol ON ins.GPD_PLC_ID = pol.ID
                         WHERE pol.ID = :1
                    """, [policy_id])
                    fb = cur.fetchone()
                    if fb:
                        fgl_inv_serial = fb[0]
                        print(f"[TRANSACTION] FGL_INV_SERIAL (fallback): {fgl_inv_serial}")

        except Exception as e:
            raise _StepError(f"Step 3 failed (resolve FGL): {e}")

        if not fgl_trn_id or not fgl_inv_serial:
            raise _StepError("لم يتم العثور على FGL_TRN_ID او FGL_INV_SERIAL")

        print(f"[TRANSACTION] Step 3 complete — FGL_TRN_ID={fgl_trn_id}, FGL_INV_SERIAL={fgl_inv_serial}")
        return fgl_trn_id, fgl_inv_serial

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4 — FCM_COL_DETAILS
    # ─────────────────────────────────────────────────────────────────────────

    def _step4_col_details(self, cur, conn, new_id: int, fgl_trn_id: int,
                           fgl_inv_serial: int, ctx: Dict) -> None:
        print(f"\n[TRANSACTION] Step 4: Creating FCM_COL_DETAILS")
        try:
            cur.execute("SELECT ERP.FCM_COL_DETAILS_SEQ.NEXTVAL FROM DUAL")
            detail_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO ERP.FCM_COL_DETAILS (
                    id, fcm_col_id, fgl_trn_id, fgl_trd_serial, fgl_inv_serial,
                    amount, due_amount, paid_amount, exrate, virtual_due_amount
                ) VALUES (
                    :id, :col_id, :trn_id, 1, :inv_serial,
                    :amount, :due, :paid, :exrate, :vda
                )
            """, {
                "id":         detail_id,
                "col_id":     new_id,
                "trn_id":     fgl_trn_id,
                "inv_serial": fgl_inv_serial,
                "amount":     ctx["net_premium_val"],
                "due":        ctx["net_premium_val"],
                "paid":       ctx["net_premium_val"],
                "exrate":     ctx["exrate"],
                "vda":        ctx["net_premium_val"],
            })
            conn.commit()
            print(f"[TRANSACTION] Step 4 complete — FCM_COL_DETAILS (ID={detail_id})")
        except Exception as e:
            raise _StepError(f"Step 4 failed (FCM_COL_DETAILS): {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5 — FCM_COL_PAYMENTS
    # ─────────────────────────────────────────────────────────────────────────

    def _step5_col_payments(self, cur, conn, new_id: int, ctx: Dict) -> None:
        print(f"\n[TRANSACTION] Step 5: Creating FCM_COL_PAYMENTS")
        try:
            cur.execute("""
                INSERT INTO ERP.FCM_COL_PAYMENTS (
                    SERIAL, FCM_COL_ID, PAYMENT_METHOD, AMOUNT, AMOUNT_LC, EXRATE,
                    CRG_CUR_CODE, BANK_REF, BANK_DATE, EFT_NO, FGL_CRCD_CODE, CREDIT_CARD_NO,
                    CHEQUE_NO, DUE_DATE, FGL_BNK_CODE, FGL_BBR_CODE, PDC_FLAG, PAYEE, FGL_COA_ID,
                    CREATED_BY, CREATION_DATE, TOLERANCE_AMOUNT, TOLERANCE_FGL_COA_ID,
                    REFERENCE_NO, TOLERANCE_FGL_CPC_ID, PAYMENT_FLAG, IS_BENEFICIARY,
                    BANK_NAME, DUE_AMOUNT, AMOUNT_AFTER_EXLUDE, FCS_CST_ID, FGL_CHQ_OUT_ID,
                    NOTES, TOLERANCE_AMOUNT_LC, TOLERANCE_EXRATE, TOLERANCE_CRG_CUR_CODE,
                    DUE_AMOUNT_FC, ON_OUR_BEHALF, EXPORT_PAYMENT, DEPOSIT_IN_ACCOUNT,
                    FGL_DRAWEE_BNK_CODE, FGL_DRAWEE_BBR_CODE, FGL_DRAWEE_ID, ENDORSED_TO,
                    FGL_CARD_HOL_BNK, DEPOSIT_DATE, TRANSLATION_DATE, FCS_CST_BNK_ID
                ) VALUES (
                    1, :col_id, :pay_method, :amount, :amount_lc, :exrate,
                    :cur_code, NULL, SYSDATE, NULL, NULL, NULL, NULL,
                    SYSDATE, 142, 182, NULL, :payee, 4374,
                    :created_by, SYSDATE, NULL, NULL,
                    NULL, NULL, 0, NULL,
                    NULL, :due, :amount_after, :cst_id, NULL,
                    NULL, NULL, NULL, NULL,
                    0, NULL, NULL, 0,
                    NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL
                )
            """, {
                "col_id":       new_id,
                "pay_method":   ctx["payment_method"],
                "amount":       ctx["net_premium_val"],
                "amount_lc":    ctx["net_premium_val"] * ctx["exrate"],
                "exrate":       ctx["exrate"],
                "cur_code":     ctx["currency_code"],
                "payee":        ctx["payee"],
                "created_by":   "Paymob",
                "due":          ctx["net_premium_val"],
                "amount_after": ctx["net_premium_val"] * ctx["exrate"],
                "cst_id":       ctx["cst_id_acc"],
            })
            conn.commit()
            print("[TRANSACTION] Step 5 complete — FCM_COL_PAYMENTS committed")
        except Exception as e:
            raise _StepError(f"Step 5 failed (FCM_COL_PAYMENTS): {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 6 — FCM_COL_SUMMARY
    # ─────────────────────────────────────────────────────────────────────────

    def _step6_col_summary(self, cur, conn, new_id: int, fgl_trn_id: int,
                           fgl_inv_serial: int, ctx: Dict) -> None:
        print(f"\n[TRANSACTION] Step 6: Creating FCM_COL_SUMMARY")
        try:
            cur.execute("SELECT ERP.FCM_COL_SUMMARY_SEQ.NEXTVAL FROM DUAL")
            summary_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO ERP.FCM_COL_SUMMARY (
                    ID, FCM_COL_ID, FCM_PAY_SERIAL, DR_CR, AMOUNT, AMOUNT_LC,
                    EXRATE, FCS_CST_ID, FCS_AGT_ID, FGL_COA_ID, FGL_TRN_ID,
                    FGL_TRD_SERIAL, CREATED_BY, CREATION_DATE, TRN_FGL_TRD_SERIAL,
                    CRG_CUR_CODE, FGL_INV_SERIAL, IS_DELETED, DOE_AMOUNT,
                    AMOUNT2, AMOUNT_LC_ROUNDED, AMOUNT_LC_ALTERED
                ) VALUES (
                    :id, :col_id, 1, 2, :amount, :amount_lc,
                    :exrate, :cst_id, :agt_id, 4430, :trn_id,
                    1, :created_by, SYSDATE, NULL,
                    :cur_code, :inv_serial, 0, 0,
                    :amount2, 0, 0
                )
            """, {
                "id":         summary_id,
                "col_id":     new_id,
                "amount":     ctx["net_premium_val"],
                "amount_lc":  ctx["net_premium_val"] * ctx["exrate"],
                "exrate":     ctx["exrate"],
                "cst_id":     ctx["cst_id_acc"],
                "agt_id":     ctx["fcs_agt_id"],
                "trn_id":     fgl_trn_id,
                "created_by": "Paymob",
                "cur_code":   ctx["currency_code"],
                "inv_serial": fgl_inv_serial,
                "amount2":    ctx["net_premium_val"],
            })
            conn.commit()
            print(f"[TRANSACTION] Step 6 complete — FCM_COL_SUMMARY (ID={summary_id})")
        except Exception as e:
            raise _StepError(f"Step 6 failed (FCM_COL_SUMMARY): {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 7 — Fetch cost center
    # ─────────────────────────────────────────────────────────────────────────

    def _step7_cost_center(self, cur, ctx: Dict) -> int:
        print(f"\n[TRANSACTION] Step 7: Fetching FGL_CPC_ID")
        cost_center_id = 0
        try:
            cur.execute("""
                SELECT det.FGL_CPC_ID
                  FROM ERP.FGL_TRANSACTIONS    TRN
             LEFT JOIN IGENERAL.GPD_VOUCHERS   VSH ON TRN.ID    = VSH.FGL_TRN_ID
             LEFT JOIN IGENERAL.GPD_INSTALLMENTS INS ON VSH.GPD_INS_ID = INS.ID
             LEFT JOIN IGENERAL.GPD_POLICIES   POL ON INS.GPD_PLC_ID = POL.ID
             LEFT JOIN ERP.FGL_INVOICES        INV ON TRN.ID    = INV.FGL_TRN_ID
             LEFT JOIN ERP.FGL_TRANS_DETAILS   det ON det.FGL_TRN_ID = TRN.ID
                 WHERE POL.ID = :id AND det.FGL_CPC_ID IS NOT NULL
                 FETCH FIRST 1 ROW ONLY
            """, {"id": ctx["policy_id"]})
            r = cur.fetchone()
            if r and r[0]:
                cost_center_id = r[0]
        except Exception as e:
            print(f"[TRANSACTION WARNING] Step 7: could not fetch FGL_CPC_ID: {e} — using 0")
        print(f"[TRANSACTION] Step 7 complete — cost_center_id={cost_center_id}")
        return cost_center_id

    # ─────────────────────────────────────────────────────────────────────────
    # Step 8 — Commission eligibility check
    # ─────────────────────────────────────────────────────────────────────────

    def _step8_check_commission(self, cur, conn, policy_number: str,
                                ctx: Dict, fgl_trn_id: int,
                                new_id: int) -> Tuple[bool, List]:
        """Return (skip_commission, commission_rows)."""
        print(f"\n[TRANSACTION] Step 8: Checking commission eligibility")
        skip_commission = False
        commission_rows = []
        try:
            # Check FGL_COA_ID = 4430 presence
            cur.execute("""
                SELECT det.FGL_COA_ID
                  FROM ERP.FGL_TRANSACTIONS    TRN
             LEFT JOIN IGENERAL.GPD_VOUCHERS   VSH ON TRN.ID    = VSH.FGL_TRN_ID
             LEFT JOIN IGENERAL.GPD_INSTALLMENTS INS ON VSH.GPD_INS_ID = INS.ID
             LEFT JOIN IGENERAL.GPD_POLICIES   POL ON INS.GPD_PLC_ID = POL.ID
             LEFT JOIN ERP.FGL_INVOICES        INV ON TRN.ID    = INV.FGL_TRN_ID
             LEFT JOIN ERP.FGL_TRANS_DETAILS   det ON det.FGL_TRN_ID = TRN.ID
                 WHERE POL.ID = :pol_id AND det.FGL_COA_ID = 4430
            """, {"pol_id": ctx["policy_id"]})
            detail_results = cur.fetchall()
            skip_commission = any(r[0] is None for r in detail_results) if detail_results else False

            # Check FCS_AGT_ID presence
            if not skip_commission:
                cur.execute("""
                    SELECT det.fcs_agt_id
                      FROM ERP.FGL_TRANSACTIONS    TRN
                 LEFT JOIN IGENERAL.GPD_VOUCHERS   VSH ON TRN.ID    = VSH.FGL_TRN_ID
                 LEFT JOIN IGENERAL.GPD_INSTALLMENTS INS ON VSH.GPD_INS_ID = INS.ID
                 LEFT JOIN IGENERAL.GPD_POLICIES   POL ON INS.GPD_PLC_ID = POL.ID
                 LEFT JOIN ERP.FGL_INVOICES        INV ON TRN.ID    = INV.FGL_TRN_ID
                 LEFT JOIN ERP.FGL_TRANS_DETAILS   det ON det.FGL_TRN_ID = TRN.ID
                     WHERE TRN.ID = :id AND det.FGL_COA_ID = 4430
                """, {"id": fgl_trn_id})
                agt_results = cur.fetchall()
                if agt_results and any(r[0] is None for r in agt_results):
                    skip_commission = True
                    print("[TRANSACTION] Commission ineligible — FCS_AGT_ID is NULL")

            # Calculate commissions
            if not skip_commission:
                payment_date_str = str(datetime.datetime.now().date())
                comm_ops         = CommCalc(conn, policy_number, payment_date_str)
                commission_rows  = comm_ops.calc_commission_amount()
                print(f"[TRANSACTION] {len(commission_rows)} commission row(s) from CommCalc")
                if not commission_rows:
                    skip_commission = True
                    print("[TRANSACTION] No commission rows — treating as no-commission policy")

        except Exception as e:
            raise _StepError(f"Step 8 (commission check) failed: {e}")

        return skip_commission, commission_rows

    # ─────────────────────────────────────────────────────────────────────────
    # Step 8A — Commission path
    # ─────────────────────────────────────────────────────────────────────────

    def _step8a_process_commissions(self, cur, conn, policy_number: str,
                                    new_id: int, new_serial: int,
                                    fgl_trn_id: int, fgl_inv_serial: int,
                                    cost_center_id: int, ctx: Dict,
                                    commission_rows: List) -> None:
        print(f"\n[TRANSACTION] >> BRANCH A: Commission exists")
        try:
            calculated     = self._calculate_commission_details(commission_rows, ctx["exrate"], ctx["currency_code"])
            serial_mapping = {6: 1, 1: 2, 2: 3}  # Basic:1, Early:2, Collection:3

            # Phase 1: Insert VIRTUAL_COMMISSION + DETAILS
            print(f"\n[TRANSACTION] Phase 1: Inserting VIRTUAL_COMMISSION + DETAILS")
            vcd_ids = {}
            for comm_type, row, comm_amount, comm_amount_lc, tax_amount, sales_tax_amount, comm_perc, tax_perc in calculated:
                if comm_type not in serial_mapping:
                    continue
                serial         = serial_mapping[comm_type]
                comm_type_name = {6: "Basic", 1: "Early", 2: "Collection"}.get(comm_type, f"T{comm_type}")

                dr_coa, cr_coa = {
                    6: (4616, 4507),
                    1: (4617, 4508),
                    2: (4618, 4508),
                }[comm_type]

                fcs_agt_id_for_insert = ctx["fcs_agt_id"] or ctx["cst_id_acc"]

                cur.execute("""
                    INSERT INTO ERP.FCM_VIRTUAL_COMMISSION (
                        FCM_COL_ID, SERIAL, PARENT_FGL_TRN_ID, FCS_CST_ID, FCS_AGT_ID,
                        DR_FGL_COA_ID, DR_FGL_CPC_ID, CR_FGL_COA_ID, CR_FGL_CPC_ID,
                        AMOUNT, AMOUNT_LC, EXRATE, CRG_CUR_CODE, PAID_AMOUNT,
                        INCLUDE_IN_COLLECTION, COMM_TYPE, FCM_VC_SERIAL, COMM_PERC,
                        VIRTUAL_DUE_AMOUNT, IS_DELETED, ORIGINAL_AMOUNT, ORIGINAL_AMOUNT_LC,
                        DISCOUNT_AMOUNT, DISCOUNT_AMOUNT_LC, MAX_DISCOUNT_PERC, DISCOUNT_PERC
                    ) VALUES (
                        :col_id, :serial, :parent_trn, :cst_id, :agt_id,
                        :dr_coa, :dr_cpc, :cr_coa, NULL,
                        :amount, :amount_lc, :exrate, :cur_code, :paid,
                        0, :comm_type, NULL, :comm_perc,
                        NULL, 0, 0, 0, 0, 0, 0, 0
                    )
                """, {
                    "col_id":     new_id,
                    "serial":     serial,
                    "parent_trn": fgl_trn_id,
                    "cst_id":     ctx["cst_id_acc"],
                    "agt_id":     fcs_agt_id_for_insert,
                    "dr_coa":     dr_coa,
                    "dr_cpc":     cost_center_id,
                    "cr_coa":     cr_coa,
                    "amount":     comm_amount,
                    "amount_lc":  comm_amount_lc,
                    "exrate":     ctx["exrate"],
                    "cur_code":   ctx["currency_code"],
                    "paid":       comm_amount_lc,
                    "comm_type":  comm_type,
                    "comm_perc":  comm_perc,
                })

                cur.execute("SELECT ERP.FCM_VIRTUAL_COMM_DETAILS_SEQ.NEXTVAL FROM DUAL")
                fcm_vcd_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO ERP.FCM_VIRTUAL_COMM_DETAILS (
                        ID, FCM_COL_ID, FCM_COL_SERIAL, PAYMENT_METHOD, AMOUNT,
                        TAX_PERCENTAGE, TAX_AMOUNT, DUE_DATE,
                        SALES_TAX_AMOUNT, FCM_PAY_SERIAL, FGL_TRN_ID, IS_DELETED
                    ) VALUES (
                        :id, :col_id, :col_serial, :pay_method, :amount,
                        :tax_perc, :tax_amount, SYSDATE,
                        NULL, 1, NULL, 0
                    )
                """, {
                    "id":         fcm_vcd_id,
                    "col_id":     new_id,
                    "col_serial": serial,
                    "pay_method": ctx["payment_method"],
                    "amount":     comm_amount,
                    "tax_perc":   tax_perc,
                    "tax_amount": tax_amount,
                })
                vcd_ids[serial] = fcm_vcd_id
                print(f"[TRANSACTION]   {comm_type_name}: VIRTUAL_COMMISSION + DETAILS inserted (vcd_id={fcm_vcd_id})")

            conn.commit()
            print(f"[TRANSACTION] Phase 1 committed — {len(vcd_ids)} commission(s)")

            self._update_status_and_history(cur, conn, new_id, 2)
            self._update_status_and_history(cur, conn, new_id, 3)

            # Phase 2: One CN transaction per commission type
            print(f"\n[TRANSACTION] Phase 2: Creating CN transactions")
            for comm_type, row, comm_amount, comm_amount_lc, tax_amount, sales_tax_amount, comm_perc, tax_perc in calculated:
                if comm_type not in serial_mapping:
                    continue
                serial         = serial_mapping[comm_type]
                comm_type_name = {6: "Basic", 1: "Early", 2: "Collection"}.get(comm_type, f"T{comm_type}")
                self._phase2_create_cn(
                    cur, conn, policy_number, new_id, new_serial,
                    fgl_trn_id, fgl_inv_serial, cost_center_id, ctx,
                    comm_type, comm_type_name, serial,
                    comm_amount, comm_amount_lc, tax_amount, tax_perc,
                    vcd_ids.get(serial),
                )
            print(f"[TRANSACTION] Phase 2 complete — {len(vcd_ids)} CN transaction(s) created")

        except _StepError:
            raise
        except Exception as e:
            raise _StepError(f"Step 8A (commissions) failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2 — Single CN transaction
    # ─────────────────────────────────────────────────────────────────────────

    def _phase2_create_cn(self, cur, conn, policy_number: str,
                          new_id: int, new_serial: int,
                          fgl_trn_id: int, fgl_inv_serial: int,
                          cost_center_id: int, ctx: Dict,
                          comm_type: int, comm_type_name: str, serial: int,
                          comm_amount: float, comm_amount_lc: float,
                          tax_amount: float, tax_perc: float,
                          fcm_vcd_id: Optional[int]) -> None:
        print(f"\n[TRANSACTION]   Phase 2 CN: {comm_type_name}")
        try:
            total_amount = round(float(comm_amount_lc), 5)
            tax_rounded  = round(float(tax_amount), 5)
            before_tax   = round(float(comm_amount_lc - tax_amount), 5)

            result = fetch_main_infos(
                cur, conn, new_serial, new_id,
                ctx["net_premium_val"], ctx["exrate"],
                status="credit_note", cn_type=comm_type,
            )
            if not result.get("success"):
                raise _StepError(f"fetch_main_infos failed for CN ({comm_type_name}): {result.get('error')}")

            data             = result["main_info"]
            id_retrieved     = data["id_retrieved"]
            journal_no       = data["journal_no"]
            segment_code_val = data["segment_code"]
            note             = data["note"]
            exrate_cn        = data["exrate"]
            username         = data["username"]
            current_year     = data["current_year"]
            current_month    = data["current_month"]

            fcr_trt_code = {6: 4, 1: 1}.get(comm_type, 13)
            coa_id_total = {6: 4616, 1: 4617, 2: 4618}.get(comm_type, 4616)
            fcs_agt_id   = ctx["fcs_agt_id"]
            fgl_sty_id   = fcr_trt_code  # mirrors FCR_TRT_CODE

            cur.execute(f"""
                INSERT INTO ERP.FGL_TRANSACTIONS (
                    ID, JOURNAL_NO, SEGMENT_CODE, JOURNAL_DATE, SOURCE, TOTAL_AMOUNT, NOTES,
                    IS_REVERSE, IS_POSTED, IS_PRINTED, IS_DELETED, CRG_COM_ID, CREATED_BY, CREATION_DATE,
                    POSTING_DATE, POSTED_BY, FCR_TRT_CODE, FCR_FYR_YEAR, FCR_PRD_MONTH,
                    TRN_SERIAL, CRG_BRN_ID, FGL_CSH_ID, FCS_CST_ID, IS_E_ENVOICE, EXTRA_ATTRIBUTE,
                    MODIFIED_BY, MODIFICATION_DATE, FGL_STY_ID
                ) VALUES (
                    {id_retrieved}, {journal_no}, '{segment_code_val}', SYSDATE,
                    'COL', {total_amount}, '{note}',
                    0, 1, 0, 0, 1, '{username}', SYSDATE,
                    SYSDATE, 'PAYMOB', 7, {current_year}, {current_month},
                    1, 1241, NULL, NULL, 0, 0,
                    NULL, NULL, {fgl_sty_id}
                )
            """)

            # FGL_TRANS_DETAILS — Tax (serial 2, DR_CR 2)
            # Only inserted when Income Tax > 0; zero-tax rows are rejected by the ERP.
            if tax_rounded > 0:
                cur.execute(f"""
                    INSERT INTO erp.fgl_trans_details VALUES(
                        2, 2, SYSDATE,
                        ABS({tax_rounded}), ABS({tax_rounded}),
                        1, NULL, 'Income Tax', 0, 1, '{username}', SYSDATE,
                        NULL, NULL, {id_retrieved}, {current_year}, {current_month},
                        4522, NULL, 'EGP', 1,
                        1241, 2, NULL, 0,
                        NULL, NULL, NULL, NULL, NULL, NULL,
                        {cost_center_id}, NULL, NULL, NULL,
                        '{policy_number}', 1,
                        ABS({tax_rounded}), NULL, ABS({tax_perc}),
                        NULL, 2, NULL, NULL, NULL, NULL, NULL,
                        NULL, NULL, NULL, NULL, NULL, NULL,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
                    )
                """)

            # FGL_TRANS_DETAILS — Total (serial 3, DR_CR 1)
            cur.execute(f"""
                INSERT INTO erp.fgl_trans_details VALUES(
                    3, 1, SYSDATE,
                    {total_amount}, {total_amount},
                    1, NULL, NULL, 0, 1, '{username}', SYSDATE,
                    NULL, NULL, {id_retrieved}, {current_year}, {current_month},
                    {coa_id_total}, NULL, 'EGP', 1,
                    1241, 2, NULL, 0,
                    NULL, NULL, NULL, NULL, NULL, NULL,
                    {cost_center_id}, NULL, NULL, NULL,
                    '{policy_number}', NULL,
                    {total_amount}, NULL, NULL,
                    NULL, 4, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
                )
            """)

            # FGL_TRANS_DETAILS — Net (serial 1, DR_CR 2)
            cur.execute(f"""
                INSERT INTO erp.fgl_trans_details VALUES(
                    1, 2, SYSDATE,
                    {before_tax}, {before_tax},
                    1, NULL, '{note}', 0, 1, '{username}', SYSDATE,
                    NULL, NULL, {id_retrieved}, {current_year}, {current_month},
                    4508, NULL, 'EGP', 1,
                    1241, 7, {fcs_agt_id}, 0,
                    NULL, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL,
                    '{policy_number}', NULL,
                    {before_tax}, NULL, NULL,
                    NULL, 5, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
                )
            """)

            # FGL_INVOICES
            cur.execute("SELECT COALESCE(MAX(SERIAL), 0) + 1 FROM ERP.FGL_INVOICES")
            inv_serial = cur.fetchone()[0]
            cur.execute(f"""
                INSERT INTO erp.fgl_invoices (
                    SERIAL, DUE_DATE, AMOUNT, AMOUNT_LC, EXRATE, STATUS, STATUS_DATE,
                    DUE_AMOUNT, FGL_TRN_ID, FGL_TRD_SERIAL, FCS_CST_ID, FCR_TRT_CODE, DUE_AMOUNT_FC
                ) VALUES (
                    {inv_serial}, SYSDATE, {before_tax}, {before_tax},
                    {exrate_cn}, 0, SYSDATE,
                    {before_tax}, {id_retrieved}, 1, {fcs_agt_id}, 7, 0
                )
            """)

            # FGL_INVOICE_DETAILS
            cur.execute("SELECT ERP.FGL_INVOICE_DETAILS_SEQ.NEXTVAL FROM DUAL")
            inv_det_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO ERP.FGL_INVOICE_DETAILS (
                    ID, ORIGINAL_POLICY_NO, DN_FGL_TRN_ID, CN_FGL_TRN_ID,
                    CREATED_BY, CREATION_DATE, DN_FGL_INV_SERIAL, CN_FGL_INV_SERIAL,
                    FLAT_COMM_PERCENTAGE, FLAT_COMM_AMOUNT, INCLUDE_IN_PRODUCATION
                ) VALUES (
                    :id, :orig_pol, :dn_trn, :cn_trn,
                    :created_by, SYSDATE, :dn_inv, :cn_inv,
                    NULL, NULL, NULL
                )
            """, {
                "id":         inv_det_id,
                "orig_pol":   policy_number,
                "dn_trn":     fgl_trn_id,
                "cn_trn":     id_retrieved,
                "created_by": username,
                "dn_inv":     fgl_inv_serial,
                "cn_inv":     inv_serial,
            })

            # Update FCM_VIRTUAL_COMM_DETAILS FGL_TRN_ID
            if fcm_vcd_id:
                cur.execute("""
                    UPDATE ERP.FCM_VIRTUAL_COMM_DETAILS
                       SET FGL_TRN_ID = :trn_id
                     WHERE ID = :vcd_id
                """, {"trn_id": id_retrieved, "vcd_id": fcm_vcd_id})

            conn.commit()
            print(f"[TRANSACTION]   CN for {comm_type_name} committed (TRN_ID={id_retrieved})")

            # ── Save to COLLECTION_TRANSACTION_LOG ────────────────────────────
            try:
                cur.execute("""
                    INSERT INTO COLLECTION_TRANSACTION_LOG (
                        COLLECTION_ID, COLLECTION_SERIAL, POLICY_NUMBER,
                        TRAN_ID, TRAN_TYPE, COMM_TYPE,
                        SEGMENT_CODE, AMOUNT, CREATED_AT
                    ) VALUES (
                        :col_id, :col_serial, :policy_no,
                        :tran_id, 'CN', :comm_type,
                        :seg_code, :amount, SYSTIMESTAMP
                    )
                """, {
                    "col_id":     new_id,
                    "col_serial": new_serial,
                    "policy_no":  policy_number,
                    "tran_id":    id_retrieved,
                    "comm_type":  comm_type,
                    "seg_code":   segment_code_val,
                    "amount":     total_amount,
                })
                conn.commit()
                print(f"[LOG] CN saved -- col={new_id}, tran={id_retrieved}, comm_type={comm_type}")
            except Exception as log_err:
                print(f"[LOG WARNING] CN log insert failed: {log_err}")

            commit_journal_no(cur, conn, data["journal_trt_code"], data["journal_fyr_year"])

        except _StepError:
            raise
        except Exception as e:
            raise _StepError(f"Phase 2 CN ({comm_type_name}) failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 8B — No commission path
    # ─────────────────────────────────────────────────────────────────────────

    def _step8b_no_commission(self, cur, conn, new_id: int) -> None:
        print(f"\n[TRANSACTION] >> BRANCH B: No commission — status 1→2→3 directly")
        try:
            self._update_status_and_history(cur, conn, new_id, 2)
            self._update_status_and_history(cur, conn, new_id, 3)
        except _StepError:
            raise
        except Exception as e:
            raise _StepError(f"Step 8B failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 9 — RCT transaction + FGL_NETTINGS
    # ─────────────────────────────────────────────────────────────────────────

    def _step9_rct_transaction(self, cur, conn, new_id: int, new_serial: int,
                               fgl_trn_id: int, fgl_inv_serial: int,
                               ctx: Dict, cost_center_id: int) -> None:
        print(f"\n[TRANSACTION] Step 9: Creating RCT financial transaction")
        try:
            result = fetch_main_infos(
                cur, conn, new_serial, new_id,
                ctx["net_premium_val"], ctx["exrate"],
                status="cashier",
            )
            if not result.get("success"):
                raise _StepError(f"fetch_main_infos (cashier) failed: {result.get('error')}")

            data             = result["main_info"]
            id_retrieved     = data["id_retrieved"]
            journal_no       = data["journal_no"]
            segment_code_val = data["segment_code"]
            notes_val        = data["notes"]
            insert_amount_lc = data["insert_amount_lc"]
            exrate_rct       = data["exrate"]
            username         = data["username"]
            current_year     = data["current_year"]
            current_month    = data["current_month"]
            total_amount_rct = insert_amount_lc

            # FGL_TRANSACTIONS (RCT)
            cur.execute(f"""
                INSERT INTO ERP.FGL_TRANSACTIONS (
                    ID, JOURNAL_NO, SEGMENT_CODE, JOURNAL_DATE, SOURCE, TOTAL_AMOUNT, NOTES,
                    CREATED_BY, CREATION_DATE, FCR_TRT_CODE, CRG_COM_ID, CRG_BRN_ID, FCS_CST_ID,
                    IS_REVERSE, IS_POSTED, IS_PRINTED, IS_DELETED, FCR_FYR_YEAR, FCR_PRD_MONTH,
                    TRN_SERIAL, POSTING_DATE, POSTED_BY, IS_E_ENVOICE, FGL_CSH_ID, FGL_STY_ID
                ) VALUES (
                    {id_retrieved}, {journal_no}, '{segment_code_val}', SYSDATE, 'RCT',
                    {total_amount_rct}, '{notes_val}',
                    '{username}', SYSDATE, 2, 1, {ctx['crg_brn_id']}, NULL,
                    0, 1, 0, 0, {current_year}, {current_month},
                    1, SYSDATE, 'Paymob', 0, 1904, NULL
                )
            """)
            print(f"[TRANSACTION] FGL_TRANSACTIONS (RCT) inserted — ID={id_retrieved}")

            # FGL_TRANS_DETAILS serial 1: Debit
            try:
                cur.execute("""
                    INSERT INTO erp.fgl_trans_details (
                        SERIAL, FGL_TRN_ID, JOURNAL_DATE, AMOUNT, AMOUNT_LC, EXRATE,
                        PAYMENT_METHOD, FCS_AGT_ID, NOTES, DR_CR, IS_DELETED, IS_POSTED,
                        CREATED_BY, CREATION_DATE, FCR_FYR_YEAR, FCR_PRD_MONTH,
                        FGL_COA_ID, CRG_CUR_CODE, CRG_BRN_ID, FCS_CST_ID,
                        TAX_PERCENTAGE, SALES_TAX_PERCENTAGE,
                        DEPOSIT_DATE, PAYEE, BANK_DATE
                    ) VALUES (
                        1, :trn_id, SYSDATE, :amount, :amount_lc, :exrate,
                        :pay_method, NULL, NULL, 1, 0, 1,
                        :created_by, SYSDATE, :yr, :mo,
                        4825, :cur_code, 1241, NULL,
                        NULL, NULL,
                        TO_DATE('01/01/0001','MM/DD/YYYY'), :payee, TO_DATE('01/01/0001','MM/DD/YYYY')
                    )
                """, {
                    "trn_id":     id_retrieved,
                    "amount":     total_amount_rct,
                    "amount_lc":  total_amount_rct,
                    "exrate":     round(float(exrate_rct), 5),
                    "pay_method": ctx["payment_method"],
                    "created_by": username,
                    "yr":         current_year,
                    "mo":         current_month,
                    "cur_code":   ctx["currency_code"],
                    "payee":      ctx["payee"],
                })
            except Exception as e:
                print(f"[TRANSACTION WARNING] RCT serial 1 insert failed: {e} — continuing")

            # FGL_TRANS_DETAILS serial 2: Credit
            cn_fcs_cst_id = ctx["cst_id_acc"]
            try:
                cur.execute("""
                    SELECT inv.FCS_CST_ID FROM ERP.FGL_INVOICES inv
                      JOIN ERP.FGL_TRANSACTIONS trn ON inv.FGL_TRN_ID = trn.ID
                     WHERE trn.SOURCE = 'COL' AND trn.SEGMENT_CODE LIKE '%CN-COL-01%'
                     ORDER BY trn.ID DESC FETCH FIRST 1 ROW ONLY
                """)
                r = cur.fetchone()
                if r:
                    cn_fcs_cst_id = r[0]
            except Exception:
                pass

            try:
                cur.execute("""
                    INSERT INTO erp.fgl_trans_details (
                        SERIAL, FGL_TRN_ID, JOURNAL_DATE, AMOUNT, AMOUNT_LC, EXRATE,
                        PAYMENT_METHOD, FCS_AGT_ID, NOTES, DR_CR, IS_DELETED, IS_POSTED,
                        CREATED_BY, CREATION_DATE, FCR_FYR_YEAR, FCR_PRD_MONTH,
                        FGL_COA_ID, CRG_CUR_CODE, CRG_BRN_ID, FCS_CST_ID,
                        TAX_PERCENTAGE, SALES_TAX_PERCENTAGE,
                        DEPOSIT_DATE, EXTRA_ATTRIBUTE, PAYEE, BANK_DATE
                    ) VALUES (
                        2, :trn_id, SYSDATE, :amount, :amount_lc, :exrate,
                        :pay_method, :agt_id, '', 2, 0, 1,
                        :created_by, SYSDATE, :yr, :mo,
                        4430, :cur_code, 1241, :cst_id,
                        NULL, NULL,
                        TO_DATE('01/01/0001','MM/DD/YYYY'), 1, :payee, TO_DATE('01/01/0001','MM/DD/YYYY')
                    )
                """, {
                    "trn_id":     id_retrieved,
                    "amount":     total_amount_rct,
                    "amount_lc":  total_amount_rct,
                    "exrate":     round(float(exrate_rct), 5),
                    "pay_method": ctx["payment_method"],
                    "agt_id":     cn_fcs_cst_id,
                    "created_by": username,
                    "yr":         current_year,
                    "mo":         current_month,
                    "cur_code":   ctx["currency_code"],
                    "cst_id":     ctx["cst_id_acc"],
                    "payee":      ctx["payee"],
                })
            except Exception as e:
                print(f"[TRANSACTION WARNING] RCT serial 2 insert failed: {e} — continuing")

            # RCT invoice
            try:
                cur.execute("SELECT COALESCE(MAX(SERIAL), 0) + 1 FROM ERP.FGL_INVOICES")
                rct_inv_serial = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO ERP.FGL_INVOICES (
                        SERIAL, DUE_DATE, AMOUNT, AMOUNT_LC, EXRATE, STATUS, STATUS_DATE,
                        DUE_AMOUNT, FGL_TRN_ID, FGL_TRD_SERIAL, FCS_CST_ID, FCR_TRT_CODE, DUE_AMOUNT_FC
                    ) VALUES (
                        :serial, SYSDATE, :amount, :amount_lc, :exrate, 0, SYSDATE,
                        :due, :trn_id, 2, :cst_id, 7, 0
                    )
                """, {
                    "serial":    rct_inv_serial,
                    "amount":    total_amount_rct,
                    "amount_lc": total_amount_rct,
                    "exrate":    round(float(exrate_rct), 5),
                    "due":       total_amount_rct,
                    "trn_id":    id_retrieved,
                    "cst_id":    ctx["cst_id_acc"],
                })
            except Exception as e:
                print(f"[TRANSACTION WARNING] RCT invoice insert failed: {e} — continuing")

            conn.commit()
            print("[TRANSACTION] RCT committed — FGL_NETTINGS trigger can now see FGL_TRANS_DETAILS")

            # ── Save to COLLECTION_TRANSACTION_LOG ────────────────────────────
            try:
                cur.execute("""
                    INSERT INTO COLLECTION_TRANSACTION_LOG (
                        COLLECTION_ID, COLLECTION_SERIAL, POLICY_NUMBER,
                        TRAN_ID, TRAN_TYPE, COMM_TYPE,
                        SEGMENT_CODE, AMOUNT, CREATED_AT
                    ) VALUES (
                        :col_id, :col_serial, :policy_no,
                        :tran_id, 'RV', NULL,
                        :seg_code, :amount, SYSTIMESTAMP
                    )
                """, {
                    "col_id":     new_id,
                    "col_serial": new_serial,
                    "policy_no":  ctx["seg_code"],
                    "tran_id":    id_retrieved,
                    "seg_code":   segment_code_val,
                    "amount":     total_amount_rct,
                })
                conn.commit()
                print(f"[LOG] RV saved -- col={new_id}, tran={id_retrieved}")
            except Exception as log_err:
                print(f"[LOG WARNING] RV log insert failed: {log_err}")

            commit_journal_no(cur, conn, data["journal_trt_code"], data["journal_fyr_year"])

            self._step9_nettings(
                cur, conn, new_id, new_serial, fgl_trn_id, fgl_inv_serial,
                id_retrieved, total_amount_rct, ctx,
            )

        except _StepError:
            raise
        except Exception as e:
            raise _StepError(f"Step 9 (RCT) failed: {e}")

    def _step9_nettings(self, cur, conn, new_id: int, new_serial: int,
                        fgl_trn_id_original: int, fgl_inv_serial: int,
                        fgl_trn_id_rct: int, total_amount_rct: float,
                        ctx: Dict) -> None:
        print(f"\n[TRANSACTION] Step 9.3: Inserting FGL_NETTINGS")
        try:
            cur.execute("SELECT TOTAL_DEBIT, TOTAL_CREDIT FROM ERP.FCM_COLLECTION WHERE ID = :id", {"id": new_id})
            fcm_row         = cur.fetchone()
            fcm_total_debit = fcm_row[0] if fcm_row else total_amount_rct

            cur.execute("""
                SELECT trn.id FROM ERP.FGL_TRANSACTIONS TRN
           LEFT JOIN IGENERAL.GPD_VOUCHERS    VSH ON TRN.ID    = VSH.FGL_TRN_ID
           LEFT JOIN IGENERAL.GPD_INSTALLMENTS INS ON VSH.GPD_INS_ID = INS.ID
           LEFT JOIN IGENERAL.GPD_POLICIES    POL ON INS.GPD_PLC_ID = POL.ID
                 WHERE POL.ID = :pol_id AND TRN.FCR_TRT_CODE = 6
            """, {"pol_id": ctx["policy_id"]})
            r                   = cur.fetchone()
            original_fgl_trn_id = r[0] if r else fgl_trn_id_original

            if not fgl_inv_serial:
                print("[TRANSACTION WARNING] FGL_INV_SERIAL is NULL — skipping FGL_NETTINGS")
                return

            # Validate invoice exists; fall back if needed
            cur.execute("SELECT COUNT(*) FROM ERP.FGL_INVOICES WHERE SERIAL = :s", {"s": fgl_inv_serial})
            if cur.fetchone()[0] == 0:
                cur.execute("""
                    SELECT i.SERIAL FROM ERP.FGL_INVOICES i
                     WHERE i.FGL_TRN_ID = :trn_id ORDER BY i.SERIAL DESC
                """, {"trn_id": original_fgl_trn_id})
                alt = cur.fetchone()
                fgl_inv_serial = alt[0] if alt else None
                print(f"[TRANSACTION] FGL_INV_SERIAL fallback: {fgl_inv_serial}")

            if not fgl_inv_serial:
                print("[TRANSACTION WARNING] No valid FGL_INV_SERIAL — skipping FGL_NETTINGS")
                return

            try:
                cur.execute("SELECT ERP.FCM_RECONCILIATIONS_SEQ.NEXTVAL FROM DUAL")
                netting_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO ERP.FGL_NETTINGS (
                        ID, NETTING_NO, NETTING_TYPE, AMOUNT_LC, NETTING_DATE,
                        FGL_TRN_ID, FGL_TRD_SERIAL, FGL_TRN_ID2, FGL_TRD_SERIAL2,
                        FGL_INV_SERIAL, FGL_INV_SERIAL2, FGL_CHI_ID, IS_DELETED,
                        ADJ_FGL_TRN_ID, CREATED_BY, MODIFIED_BY, MODIFICATION_DATE,
                        CREATION_DATE, AMOUNT, AMOUNT2, UDOE_FGL_TRN_ID
                    ) VALUES (
                        :id, 1, 1, :amount_lc, SYSDATE,
                        :trn1, 1, :trn2, 2,
                        :inv_serial, NULL, NULL, 0,
                        NULL, :created_by, NULL, NULL,
                        SYSDATE, :amount, :amount2, NULL
                    )
                """, {
                    "id":          netting_id,
                    "amount_lc":   ctx["net_premium_val"],
                    "trn1":        original_fgl_trn_id,
                    "trn2":        fgl_trn_id_rct,
                    "inv_serial":  fgl_inv_serial,
                    "created_by":  "Paymob",
                    "amount":      fcm_total_debit,
                    "amount2":     fcm_total_debit,
                })
                print(f"[TRANSACTION] FGL_NETTINGS inserted (ID={netting_id})")

                cur.execute("SELECT ERP.FCM_COLLECTION_LOG_SEQ.NEXTVAL FROM DUAL")
                log_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO ERP.FGL_NETTING_LOG (
                        ID, FCM_PA_ID, FGL_NET_ID, SET_FGL_TRN_ID, CREATION_DATE, COMM_FGL_TRN_ID
                    ) VALUES (:id, :pa_id, :net_id, :trn_id, SYSDATE, NULL)
                """, {"id": log_id, "pa_id": new_id, "net_id": netting_id, "trn_id": fgl_trn_id_rct})
                print(f"[TRANSACTION] FGL_NETTING_LOG inserted (ID={log_id})")

                # Zero DUE_AMOUNTs
                cur.execute("""
                    UPDATE ERP.FGL_INVOICES inv SET inv.DUE_AMOUNT = 0
                     WHERE EXISTS (
                         SELECT 1 FROM ERP.FGL_NETTINGS net
                          WHERE net.FGL_INV_SERIAL = :s AND net.FGL_INV_SERIAL = inv.SERIAL
                     )
                """, {"s": fgl_inv_serial})

                cur.execute("""
                    UPDATE FGL_DUE_AMOUNT du SET du.DUE_AMOUNT_FC = 0, du.DUE_AMOUNT_LC = 0
                     WHERE du.FGL_TRN_ID IN (
                         SELECT TRN.ID FROM ERP.FGL_TRANSACTIONS TRN
                    LEFT JOIN IGENERAL.GPD_VOUCHERS   VSH ON TRN.ID    = VSH.FGL_TRN_ID
                    LEFT JOIN IGENERAL.GPD_INSTALLMENTS INS ON VSH.GPD_INS_ID = INS.ID
                    LEFT JOIN IGENERAL.GPD_POLICIES   POL ON INS.GPD_PLC_ID = POL.ID
                         WHERE POL.ID = :pol_id AND TRN.FCR_TRT_CODE = 6
                     )
                """, {"pol_id": ctx["policy_id"]})

                cur.execute("""
                    UPDATE FGL_DUE_AMOUNT_DET dud SET dud.DUE_AMOUNT_FC = 0, dud.DUE_AMOUNT_LC = 0
                     WHERE dud.FGL_TRN_ID IN (
                         SELECT TRN.ID FROM ERP.FGL_TRANSACTIONS TRN
                    LEFT JOIN IGENERAL.GPD_VOUCHERS   VSH ON TRN.ID    = VSH.FGL_TRN_ID
                    LEFT JOIN IGENERAL.GPD_INSTALLMENTS INS ON VSH.GPD_INS_ID = INS.ID
                    LEFT JOIN IGENERAL.GPD_POLICIES   POL ON INS.GPD_PLC_ID = POL.ID
                         WHERE POL.ID = :pol_id AND TRN.FCR_TRT_CODE = 6
                     )
                """, {"pol_id": ctx["policy_id"]})

                cur.execute("""
                    UPDATE ERP.FCM_COL_DETAILS fcd SET fcd.DUE_AMOUNT = 0
                     WHERE fcd.FCM_COL_ID = :col_id
                """, {"col_id": new_id})

                cur.execute("""
                    UPDATE ERP.FCM_COLLECTION col SET col.FGL_TRN_ID = :trn_id
                     WHERE col.ID = :id
                       AND EXISTS (
                               SELECT 1 FROM ERP.FGL_TRANSACTIONS trn
                                WHERE trn.SOURCE LIKE '%RCT%' AND trn.ID = :trn_id
                           )
                       AND col.COLLECTION_TYPE = 1
                       AND col.STATUS = 3
                """, {"trn_id": fgl_trn_id_rct, "id": new_id})
                print(f"[TRANSACTION] FCM_COLLECTION FGL_TRN_ID updated: {cur.rowcount} row(s)")

                # Status → 5
                cur.execute("UPDATE ERP.FCM_COLLECTION SET STATUS = 5 WHERE SERIAL = :serial", {"serial": new_serial})
                self._insert_history(cur, new_id, 5)
                conn.commit()
                print("[TRANSACTION] Status → 5 committed")

            except Exception as e:
                print(f"[TRANSACTION WARNING] FGL_NETTINGS insert failed: {e} — continuing")

        except Exception as e:
            print(f"[TRANSACTION WARNING] Netting section error: {e} — continuing")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 10 — Accept policy
    # ─────────────────────────────────────────────────────────────────────────

    def _step10_accept_policy(self, cur, conn, new_id: int, new_serial: int,
                              policy_number: str, ctx: Dict) -> None:
        print(f"\n[TRANSACTION] Step 10: Recording acceptance")
        try:
            cur.execute("SELECT ERP.FCM_COL_DETAILS_SEQ.NEXTVAL FROM DUAL")
            accept_id = cur.fetchone()[0]
            cur.execute(f"""
                INSERT INTO {self.policy_repo.conn_repo._stg}ACCEPT_POLICIES (ID, POLICY_NUMBER, FCM_COLLECTION_ID, CREATED_AT)
                VALUES (:id, :pn, :serial, SYSDATE)
            """, {"id": accept_id, "pn": ctx["seg_code"], "serial": new_serial})

            cur.execute(f"DELETE FROM {self.policy_repo.conn_repo._stg}STG_POLICIES WHERE POLICY_NUMBER = :pn", {"pn": ctx["seg_code"]})
            print(f"[TRANSACTION] STG_POLICIES deleted: {cur.rowcount} row(s)")
            print("[TRANSACTION] Step 10 complete")
        except Exception as e:
            raise _StepError(f"Step 10 failed (ACCEPT_POLICIES): {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Small helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _update_status_and_history(self, cur, conn, new_id: int, status: int) -> None:
        cur.execute("""
            UPDATE ERP.FCM_COLLECTION SET STATUS = :s, STATUS_DATE = SYSDATE WHERE ID = :id
        """, {"s": status, "id": new_id})
        self._insert_history(cur, new_id, status)
        conn.commit()
        print(f"[TRANSACTION] Status → {status} committed")

    def _insert_history(self, cur, new_id: int, status: int) -> None:
        cur.execute("SELECT ERP.FCM_COL_HISTORY_SEQ.NEXTVAL FROM DUAL")
        hist_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO ERP.FCM_COL_HISTORY (ID, FCM_COL_ID, USERNAME, STATUS, CREATION_DATE)
            VALUES (:id, :col_id, 'Paymob', :status, SYSDATE)
        """, {"id": hist_id, "col_id": new_id, "status": status})

    def _calculate_commission_details(
        self, commission_rows: List, exrate: float, currency_code: str
    ) -> List[Tuple]:
        calculated = []
        for row in commission_rows:
            comm_type      = row["COMM_TYPE"]
            comm_perc      = row["PERCENTAGE"] / 100
            tax_perc       = row.get("TAX_PER", 0) / 100 if row.get("IS_TAXABLE") else 0
            comm_amount    = round(float(row.get("COMMISSION_AMOUNT", 0)), 5)
            comm_amount_lc = round(float(comm_amount * exrate), 5)
            tax_amount     = round(float(
                (row.get("COMMISSION_AMOUNT_TAX", 0) - comm_amount) if row.get("IS_TAXABLE") else 0
            ), 5)
            sales_tax_amount = round(float(comm_amount), 5)
            calculated.append((
                comm_type, row, comm_amount, comm_amount_lc,
                tax_amount, sales_tax_amount, comm_perc, tax_perc,
            ))
        return calculated

    # ─────────────────────────────────────────────────────────────────────────
    # Bulk processing
    # ─────────────────────────────────────────────────────────────────────────

    def process_policies(self, request) -> Dict[str, Any]:
        """Process all pending policies from STG_POLICIES."""
        try:
            pending   = self.policy_repo.get_pending_policies()
            accepted  = set(
                row[0] for row in self.policy_repo.conn_repo.execute_query(
                    f"SELECT POLICY_NUMBER FROM {self.policy_repo.conn_repo._stg}ACCEPT_POLICIES"
                )
            )
            results = []

            for _, policy_number, element_id, created_at in pending:
                if policy_number in accepted:
                    results.append({
                        "POLICY_NUMBER": policy_number,
                        "STATUS":        "DUPLICATE",
                        "ERROR_MSG":     "الوثيقة موجودة مسبقاً في النظام",
                    })
                    continue

                result = self.process_single_policy(policy_number)
                if result.get("success"):
                    results.append({
                        "POLICY_NUMBER": policy_number,
                        "STATUS":        "SUCCESS",
                        "COLLECTION_ID": result.get("collection_id"),
                        "SERIAL":        result.get("serial"),
                        "ERROR_MSG":     "",
                    })
                else:
                    results.append({
                        "POLICY_NUMBER": policy_number,
                        "STATUS":        "ERROR",
                        "ERROR_MSG":     result.get("error", "خطأ غير معروف"),
                    })

            return {
                "results":         results,
                "total_count":     len(results),
                "success_count":   sum(1 for r in results if r["STATUS"] == "SUCCESS"),
                "error_count":     sum(1 for r in results if r["STATUS"] == "ERROR"),
                "duplicate_count": sum(1 for r in results if r["STATUS"] == "DUPLICATE"),
            }

        except Exception as e:
            raise Exception(f"حدث خطأ أثناء معالجة policies: {e}")

    def get_stats(self) -> Dict[str, int]:
        return {"total": 0, "success": 0, "errors": 0}


# ─────────────────────────────────────────────────────────────────────────────
# ReportService
# ─────────────────────────────────────────────────────────────────────────────

class ReportService:
    """Retrieve and format upload / process reports."""

    def __init__(self, report_repo: ReportRepository):
        self.report_repo = report_repo

    def get_upload_report(self, selected_date: str = None) -> Dict[str, Any]:
        try:
            if not selected_date:
                cached = cache.get("LAST_UPLOAD_REPORT")
                if cached:
                    return cached
            report = self.report_repo.get_upload_report(selected_date)
            if report and "rows" in report:
                for row in report["rows"]:
                    if "CREATED_AT" in row and row["CREATED_AT"]:
                        row["CREATED_AT"] = row["CREATED_AT"].strftime("%Y-%m-%d %H:%M:%S")
            return report or {
                "timestamp": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "filename": "", "success_count": 0, "error_count": 0, "rows": [],
            }
        except Exception as e:
            return {
                "timestamp": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "filename": "", "success_count": 0, "error_count": 0, "rows": [], "error": str(e),
            }

    def reprocess_policy(self, policy_number: str, policy_repo: PolicyRepository) -> Dict[str, Any]:
        try:
            result = ProcessService(policy_repo).process_single_policy(policy_number)
            if result.get("success"):
                return {"status": "SUCCESS", "message": f"Successfully reprocessed {policy_number}", "collection_id": result.get("collection_id")}
            return {"status": "ERROR", "message": result.get("error", "Unknown error")}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_process_report(self, selected_date: str = None) -> Dict[str, Any]:
        try:
            return self.report_repo.get_process_report(selected_date) or {
                "timestamp": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_count": 0, "success_count": 0, "error_count": 0, "duplicate_count": 0, "rows": [],
            }
        except Exception as e:
            return {
                "timestamp": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_count": 0, "success_count": 0, "error_count": 0, "duplicate_count": 0, "rows": [], "error": str(e),
            }


# ─────────────────────────────────────────────────────────────────────────────
# DashboardService
# ─────────────────────────────────────────────────────────────────────────────

class DashboardService:
    """Fetch and expose daily collection metrics."""

    def __init__(self, dashboard_repo: DashboardRepository):
        self.dashboard_repo = dashboard_repo

    def fetch_daily_dashboard(self, target_date_str: str = None) -> Dict[str, int]:
        try:
            return self.dashboard_repo.fetch_daily_dashboard(target_date_str) or {
                "total": 0, "success": 0, "errors": 0, "duplicate": 0,
            }
        except Exception as e:
            print(f"Error fetching dashboard: {e}")
            return {"total": 0, "success": 0, "errors": 0, "duplicate": 0}

    def get_stats(self) -> Dict[str, int]:
        try:
            stats = self.dashboard_repo.get_collection_stats()
            if not stats:
                stats = {"total": 0, "success": 0, "errors": 0}
            stats.setdefault("total", 0)
            stats.setdefault("success", 0)
            stats.setdefault("errors", 0)
            return stats
        except Exception as e:
            print(f"Error getting stats: {e}")
            return {"total": 0, "success": 0, "errors": 0}


# ─────────────────────────────────────────────────────────────────────────────
# TemplateService
# ─────────────────────────────────────────────────────────────────────────────

class TemplateService:
    """Generate downloadable CSV templates and report exports."""

    def download_template(self) -> Tuple[bytes, str]:
        df  = pd.DataFrame({"POLICY_NUMBER": [None, None], "ELEMENT_ID": [None, None]})
        buf = io.BytesIO()
        df.to_csv(buf, index=False, encoding="utf-8-sig")
        buf.seek(0)
        return buf.getvalue(), "policy_template.csv"

    def download_upload_report(self, report: Dict[str, Any], selected_date: str = None) -> Tuple[bytes, str]:
        rows = report.get("rows", [])
        if selected_date:
            rows = [r for r in rows if r.get("CREATED_AT", "").startswith(selected_date)]
        df        = pd.DataFrame(rows)
        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        buf       = io.BytesIO()
        df.to_csv(buf, index=False, encoding="utf-8-sig")
        buf.seek(0)
        return buf.getvalue(), f"upload_report_{selected_date or ''}_{timestamp}.csv"