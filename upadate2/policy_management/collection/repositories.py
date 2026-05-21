"""
Collection Repositories Module

Data access layer (DAL) for all Oracle database operations.
Each repository class is responsible for one domain area.

Classes:
    OracleConnectionRepository  — connection management, generic query/transaction execution
    PolicyRepository            — policy lookup, staging, collection CRUD
    DashboardRepository         — daily metrics
    ReportRepository            — upload / process reports and collection details

Standalone view:
    delete_collection(request)  — Django view that delegates to PolicyRepository
"""

import traceback
import datetime

import oracledb
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# OracleConnectionRepository
# ─────────────────────────────────────────────────────────────────────────────

class OracleConnectionRepository:
    """
    Manages Oracle connections and provides generic query / transaction helpers.

    All other repositories receive an instance of this class and call
    get_connection(), execute_query(), or execute_transaction() as needed.
    """

    def __init__(self):
        # Ensure port is integer
        port = settings.ORACLE_PORT if isinstance(settings.ORACLE_PORT, int) else int(settings.ORACLE_PORT)
        self.dsn      = f"{settings.ORACLE_HOST_ALT}:{port}/{settings.ORACLE_SERVICE_ALT}"
        self.user     = settings.ORACLE_USER_ALT
        self.password = settings.ORACLE_PASS_ALT
        self._stg     = ""  # Table name prefix (empty string for default tables)

    def get_connection(self) -> oracledb.Connection:
        print(f"[REPOSITORY] Connecting — DSN: {self.dsn}, User: {self.user}")
        try:
            conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
            print("[REPOSITORY] Connection established successfully")
            return conn
        except Exception as e:
            error_msg = f"Connection failed: {str(e)}"
            print(f"[REPOSITORY ERROR] {error_msg}")
            print(f"[REPOSITORY ERROR] DSN: {self.dsn}")
            print(f"[REPOSITORY ERROR] User: {self.user}")
            print(f"[REPOSITORY ERROR] Traceback:\n{traceback.format_exc()}")
            raise ConnectionError(error_msg)

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Tuple]:
        conn = cur = None
        try:
            print(f"\n[REPOSITORY] Executing query")
            print(f"[REPOSITORY] SQL: {query.strip()[:200]}{'...' if len(query.strip()) > 200 else ''}")
            print(f"[REPOSITORY] Params: {params or {}}")
            conn = self.get_connection()
            cur  = conn.cursor()
            cur.execute(query, params or [])
            rows = cur.fetchall()
            print(f"[REPOSITORY] Returned {len(rows)} row(s)")
            if rows and len(rows) <= 5:
                for i, row in enumerate(rows, 1):
                    print(f"[REPOSITORY]   Row {i}: {row}")
            elif rows:
                print(f"[REPOSITORY]   First row: {rows[0]} (+ {len(rows) - 1} more)")
            return rows
        except Exception as e:
            print(f"[REPOSITORY ERROR] Query failed: {e}\n{traceback.format_exc()}")
            raise
        finally:
            if cur:  cur.close()
            if conn: conn.close()

    def execute_transaction(self, queries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute multiple SQL statements atomically."""
        conn = cur = None
        try:
            print(f"\n[REPOSITORY] Starting transaction ({len(queries)} queries)")
            conn = self.get_connection()
            cur  = conn.cursor()
            results = []
            for idx, q in enumerate(queries, 1):
                sql          = q["sql"]
                params       = q.get("params", {})
                should_fetch = q.get("fetch", False)
                print(f"[REPOSITORY] Query {idx}/{len(queries)} — fetch={should_fetch}")
                print(f"[REPOSITORY] SQL: {sql.strip()[:200]}")
                cur.execute(sql, params)
                if should_fetch:
                    rows = cur.fetchall()
                    results.append(rows)
                    print(f"[REPOSITORY] Fetched {len(rows)} row(s)")
                else:
                    print(f"[REPOSITORY] Affected {cur.rowcount} row(s)")
            conn.commit()
            print("[REPOSITORY] Transaction committed")
            return {"success": True, "results": results}
        except Exception as e:
            print(f"[REPOSITORY ERROR] Transaction failed: {e}\n{traceback.format_exc()}")
            if conn:
                conn.rollback()
                print("[REPOSITORY ERROR] Transaction rolled back")
            return {"success": False, "error": str(e)}
        finally:
            if cur:  cur.close()
            if conn: conn.close()
            print("[REPOSITORY] Transaction completed\n")


# ─────────────────────────────────────────────────────────────────────────────
# PolicyRepository
# ─────────────────────────────────────────────────────────────────────────────

class PolicyRepository:
    """
    Data access for policy-related operations.

    Responsibilities:
    - Policy lookup and validation
    - Staging (STG_POLICIES) management
    - Collection creation, deletion
    - Policy status management
    """

    def __init__(self, conn_repo: OracleConnectionRepository):
        self.conn_repo = conn_repo

    # ── Policy lookup ─────────────────────────────────────────────────────────

    def get_policy_by_number(self, policy_number: str) -> Optional[Dict[str, Any]]:
        query = """
            SELECT POL.*, SUB.*, c.NAME AS CUSTOMER_NAME
              FROM IGENERAL.GPD_POLICIES POL
              JOIN IGENERAL.GPD_SUBJECTS SUB ON SUB.GPD_PLC_ID = POL.ID
         LEFT JOIN ERP.FCS_CUSTOMERS     c   ON POL.FCS_CST_ID_ACC = c.ID
             WHERE POL.SEGMENT_CODE = :policy_number
        """
        rows = self.conn_repo.execute_query(query, {"policy_number": policy_number})
        if not rows:
            return None

        conn = cur = None
        try:
            conn = self.conn_repo.get_connection()
            cur  = conn.cursor()
            cur.execute(query, {"policy_number": policy_number})
            columns = [col[0] for col in cur.description]
            return dict(zip(columns, rows[0]))
        finally:
            if cur:  cur.close()
            if conn: conn.close()

    def check_existing_policy(self, policy_number: str) -> bool:
        rows = self.conn_repo.execute_query(
            "SELECT POLICY_NUMBER FROM ACCEPT_POLICIES WHERE POLICY_NUMBER = :policy_number",
            {"policy_number": policy_number},
        )
        return len(rows) > 0

    def is_policy_duplicate(self, policy_number: str) -> bool:
        if self.check_existing_policy(policy_number):
            return True
        rows = self.conn_repo.execute_query(
            "SELECT POLICY_NUMBER FROM STG_POLICIES WHERE POLICY_NUMBER = :policy_number AND PROCESSED_FLAG = 1",
            {"policy_number": policy_number},
        )
        return len(rows) > 0

    # ── Staging ───────────────────────────────────────────────────────────────

    def insert_stg_policy(self, policy_number: str, element_id: str = "") -> bool:
        """
        Insert a new STG_POLICIES record, or mark it as duplicate.

        Returns True if the policy was staged for processing, False otherwise.
        """
        conn = cur = None
        try:
            print(f"\n[REPOSITORY] insert_stg_policy — policy={policy_number}")
            conn = self.conn_repo.get_connection()
            cur  = conn.cursor()

            # Is it already fully accepted?
            cur.execute(
                "SELECT COUNT(*) FROM ACCEPT_POLICIES WHERE POLICY_NUMBER = :pn",
                {"policy_number": policy_number},
            )
            already_accepted = cur.fetchone()[0] > 0
            print(f"[REPOSITORY] Already in ACCEPT_POLICIES: {already_accepted}")

            if already_accepted:
                # Mark as duplicate in STG_POLICIES
                cur.execute("""
                    MERGE INTO STG_POLICIES stg
                    USING (SELECT :policy_number AS pnum FROM DUAL) src
                       ON (stg.POLICY_NUMBER = src.pnum)
                     WHEN MATCHED THEN
                         UPDATE SET PROCESSED_FLAG = 3,
                                    PROCESSED_AT   = SYSDATE,
                                    ERROR_MSG      = 'Duplicate policy number',
                                    ELEMENT_ID     = :element_id
                     WHEN NOT MATCHED THEN
                         INSERT (ID, ELEMENT_ID, POLICY_NUMBER, CREATED_AT, PROCESSED_FLAG, PROCESSED_AT, ERROR_MSG)
                         VALUES (
                             (SELECT NVL(MAX(ID), 0) + 1 FROM STG_POLICIES),
                             :element_id, :policy_number, SYSDATE, 3, SYSDATE, 'Duplicate policy number'
                         )
                """, {"policy_number": policy_number, "element_id": element_id})
                conn.commit()
                print("[REPOSITORY] Marked as duplicate in STG_POLICIES")
                return False

            # Not yet accepted — delete stale records and insert fresh
            cur.execute(
                "DELETE FROM STG_POLICIES WHERE POLICY_NUMBER = :pn",
                {"policy_number": policy_number},
            )
            print(f"[REPOSITORY] Deleted {cur.rowcount} stale STG_POLICIES row(s)")

            cur.execute("SELECT NVL(MAX(ID), 0) + 1 FROM STG_POLICIES")
            new_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO STG_POLICIES (ID, ELEMENT_ID, POLICY_NUMBER, CREATED_AT, PROCESSED_FLAG)
                VALUES (:id, :element_id, :policy_number, SYSDATE, 1)
            """, {"id": new_id, "element_id": element_id, "policy_number": policy_number})
            conn.commit()
            print("[REPOSITORY] New STG_POLICIES record inserted")
            return True

        except Exception as e:
            print(f"[REPOSITORY ERROR] insert_stg_policy failed: {e}\n{traceback.format_exc()}")
            if conn:
                conn.rollback()
            return False
        finally:
            if cur:  cur.close()
            if conn: conn.close()

    def get_pending_policies(self) -> List[Tuple]:
        return self.conn_repo.execute_query("""
            SELECT id, policy_number, element_id, created_at
              FROM stg_policies
             WHERE processed_flag = 1
               AND policy_number NOT IN (SELECT POLICY_NUMBER FROM ACCEPT_POLICIES)
             ORDER BY created_at ASC, id ASC
        """)

    def get_unique_pending_policies(self) -> List[Tuple]:
        """Return one record per policy_number (most recent by created_at)."""
        return self.conn_repo.execute_query("""
            SELECT id, policy_number, element_id, created_at
              FROM (
                  SELECT id, policy_number, element_id, created_at,
                         ROW_NUMBER() OVER (
                             PARTITION BY policy_number
                             ORDER BY created_at DESC, id DESC
                         ) AS rn
                    FROM stg_policies
                   WHERE processed_flag = 1
                     AND policy_number NOT IN (SELECT POLICY_NUMBER FROM ACCEPT_POLICIES)
              )
             WHERE rn = 1
             ORDER BY created_at ASC
        """)

    def cleanup_duplicate_staging(self, policy_number: str) -> None:
        """Keep only the latest STG_POLICIES entry for a policy; delete the rest."""
        conn = cur = None
        try:
            print(f"\n[REPOSITORY] Cleaning up duplicate staging for {policy_number}")
            conn = self.conn_repo.get_connection()
            cur  = conn.cursor()

            cur.execute(
                "SELECT id FROM STG_POLICIES WHERE POLICY_NUMBER = :pn ORDER BY created_at DESC, id DESC",
                {"pn": policy_number},
            )
            all_ids = [row[0] for row in cur.fetchall()]

            if len(all_ids) <= 1:
                print("[REPOSITORY] Only one entry — nothing to clean up")
                return

            for stg_id in all_ids[1:]:
                cur.execute("DELETE FROM STG_POLICIES WHERE id = :id", {"id": stg_id})

            conn.commit()
            print(f"[REPOSITORY] Deleted {len(all_ids) - 1} duplicate row(s)")

        except Exception as e:
            print(f"[REPOSITORY ERROR] Cleanup failed: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if cur:  cur.close()
            if conn: conn.close()

    # ── Collection ────────────────────────────────────────────────────────────

    def create_collection(self, policy_number: str) -> Dict[str, Any]:
        """Create an FCM_COLLECTION + FCM_COLLECTION_DET + ACCEPT_POLICIES record."""
        policy = self.get_policy_by_number(policy_number)
        if not policy:
            return {"success": False, "error": "بوليصة غير موجودة"}

        try:
            exrate        = self._get_exchange_rate(policy.get("CRG_CUR_CODE"))
            collection_id = self._get_next_id("ERP.FCM_COLLECTION")
            serial        = self._get_next_id("ERP.FCM_COLLECTION_SERIAL")
            net_premium   = float(policy.get("NET_PREMIUM", 0)) * exrate

            queries = [
                {
                    "sql": """
                        INSERT INTO ERP.FCM_COLLECTION (
                            ID, SERIAL, STATUS_DATE, STATUS,
                            TOTAL_DEBIT, FCS_CST_ID, CRG_COM_ID,
                            CREATED_BY, CREATION_DATE,
                            MODIFIED_BY, MODIFICATION_DATE,
                            POLICY_NO, CRG_BRN_ID, NOTES
                        ) VALUES (
                            :id, :serial, SYSDATE, 1,
                            :total_debit, :cst_id, 1,
                            1, SYSDATE, 1, SYSDATE,
                            :policy_no, :brn_id, :notes
                        )
                    """,
                    "params": {
                        "id":          collection_id,
                        "serial":      serial,
                        "total_debit": net_premium,
                        "cst_id":      policy.get("FCS_CST_ID_ACC"),
                        "policy_no":   policy_number,
                        "brn_id":      policy.get("CRG_BRN_ID", 1241),
                        "notes":       f"Collection - {policy.get('CUSTOMER_NAME', 'Unknown')}",
                    },
                },
                {
                    "sql": """
                        INSERT INTO ERP.FCM_COLLECTION_DET (
                            ID, FCM_COL_ID, LINE_NUM,
                            DEBIT_AMOUNT, CREDIT_AMOUNT,
                            CREATED_BY, CREATION_DATE,
                            MODIFIED_BY, MODIFICATION_DATE,
                            POLICY_NO, FCS_CST_ID
                        ) VALUES (
                            ERP.FCM_COLLECTION_DET_SEQ.NEXTVAL,
                            :col_id, 1,
                            :amount, 0,
                            1, SYSDATE,
                            1, SYSDATE,
                            :policy_no, :cst_id
                        )
                    """,
                    "params": {
                        "col_id":    collection_id,
                        "amount":    net_premium,
                        "policy_no": policy_number,
                        "cst_id":    policy.get("FCS_CST_ID_ACC"),
                    },
                },
                {
                    "sql": """
                        INSERT INTO ACCEPT_POLICIES (
                            ID, POLICY_NUMBER, ELEMENT_ID,
                            CREATED_AT, PROCESSED_FLAG, FCM_COLLECTION_ID
                        ) VALUES (
                            :new_id, :policy_number, :element_id,
                            SYSDATE, 2, :collection_id
                        )
                    """,
                    "params": {
                        "new_id":        collection_id,
                        "policy_number": policy_number,
                        "element_id":    str(collection_id),
                        "collection_id": collection_id,
                    },
                },
            ]

            result = self.conn_repo.execute_transaction(queries)
            if result["success"]:
                return {
                    "success":       True,
                    "collection_id": collection_id,
                    "serial":        serial,
                    "policy_number": policy_number,
                    "amount":        net_premium,
                    "customer_name": policy.get("CUSTOMER_NAME"),
                }
            return result

        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_collection(self, collection_id: str) -> Dict[str, Any]:
        """
        Delete a collection and reset all related records.

        Tries three lookup strategies (exact string → stripped zeros → numeric)
        before giving up.
        """
        conn = cur = None
        try:
            print(f"\n[REPOSITORY] delete_collection — ID={collection_id}")
            conn = self.conn_repo.get_connection()
            cur  = conn.cursor()

            collection_row = self.__find_collection(cur, collection_id)
            if not collection_row:
                return {"success": False, "error": f"Collection ID {collection_id} not found"}

            actual_id      = collection_row[0]
            policy_number  = collection_row[1]
            print(f"[REPOSITORY] Found — actual_id={actual_id}, policy_no={policy_number}")

            # Reset ACCEPT_POLICIES + STG_POLICIES
            self.__reset_staging(cur, actual_id)

            # Delete from child tables then the collection itself
            tables_to_delete = [
                ("ERP.FCM_VIRTUAL_COMM_DETAILS", "FCM_COL_ID"),
                ("ERP.FCM_VIRTUAL_COMMISSIONS",  "FCM_COL_ID"),
                ("ERP.FCM_COL_HISTORY",           "FCM_COL_ID"),
                ("ERP.FCM_COL_PAYMENTS",          "FCM_COL_ID"),
                ("ERP.FCM_COL_SUMMARY",           "FCM_COL_ID"),
                ("ERP.FCM_COL_DETAILS",           "FCM_COL_ID"),
                ("ERP.FCM_COLLECTION_DET",        "FCM_COL_ID"),
                ("ERP.FCM_COLLECTION",            "ID"),
            ]
            affected_policies = self.__get_affected_policies(cur, actual_id)

            for table, col in tables_to_delete:
                try:
                    cur.execute(f"DELETE FROM {table} WHERE {col} = :id", {"id": actual_id})
                    print(f"[REPOSITORY] Deleted {cur.rowcount} row(s) from {table}")
                except Exception as del_err:
                    print(f"[REPOSITORY ERROR] Could not delete from {table}: {del_err}")

            conn.commit()
            print("[REPOSITORY] Deletion committed")
            return {
                "success":          True,
                "message":          f"Successfully deleted collection {actual_id}",
                "affected_policies": affected_policies,
            }

        except Exception as e:
            print(f"[REPOSITORY ERROR] delete_collection failed: {e}\n{traceback.format_exc()}")
            if conn:
                conn.rollback()
            return {"success": False, "error": str(e)}
        finally:
            if cur:  cur.close()
            if conn: conn.close()

    # ── Private helpers ───────────────────────────────────────────────────────

    def __find_collection(self, cur, collection_id: str):
        """Try three ID formats; return the first matching row or None."""
        strategies = [
            ("exact string",  lambda: ("TO_CHAR(ID) = :id", {"id": collection_id})),
            ("stripped zeros", lambda: ("TO_CHAR(ID) = :id", {"id": collection_id.lstrip("0") or "0"})),
            ("numeric",       lambda: ("ID = :id",           {"id": int(collection_id)})),
        ]
        for name, builder in strategies:
            try:
                condition, params = builder()
                cur.execute(
                    f"SELECT ID, POLICY_NO FROM ERP.FCM_COLLECTION WHERE {condition}",
                    params,
                )
                row = cur.fetchone()
                if row:
                    print(f"[REPOSITORY] Found via {name} — ID={row[0]}, POLICY_NO={row[1]}")
                    return row
            except Exception as e:
                print(f"[REPOSITORY] Strategy '{name}' failed: {e}")
        return None

    def __get_affected_policies(self, cur, collection_id: int) -> List[str]:
        cur.execute(
            "SELECT POLICY_NUMBER FROM ACCEPT_POLICIES WHERE FCM_COLLECTION_ID = :id",
            {"id": collection_id},
        )
        return [row[0] for row in cur.fetchall()]

    def __reset_staging(self, cur, collection_id: int) -> None:
        """Reset STG_POLICIES and remove ACCEPT_POLICIES for this collection."""
        policies = self.__get_affected_policies(cur, collection_id)

        if policies:
            placeholders = ", ".join([f":{i}" for i in range(len(policies))])
            cur.execute(
                f"""
                UPDATE STG_POLICIES
                   SET PROCESSED_FLAG = 0,
                       PROCESSED_AT   = NULL,
                       ERROR_MSG      = NULL,
                       ELEMENT_ID     = NULL
                 WHERE POLICY_NUMBER IN ({placeholders})
                """,
                dict(enumerate(policies)),
            )
            print(f"[REPOSITORY] Reset {cur.rowcount} STG_POLICIES row(s)")

        cur.execute(
            "DELETE FROM ACCEPT_POLICIES WHERE FCM_COLLECTION_ID = :id",
            {"id": collection_id},
        )
        print(f"[REPOSITORY] Deleted {cur.rowcount} ACCEPT_POLICIES row(s)")

    def _get_exchange_rate(self, currency_code: str) -> float:
        if not currency_code:
            return 1.0
        rows = self.conn_repo.execute_query(
            "SELECT RATE FROM ERP.EXCHANGE_RATES WHERE CUR_CODE = :cc AND RATE_DATE = TRUNC(SYSDATE)",
            {"cc": currency_code},
        )
        return float(rows[0][0]) if rows and rows[0][0] else 1.0

    def _get_next_id(self, table: str) -> int:
        return self.conn_repo.execute_query(f"SELECT NVL(MAX(ID), 0) + 1 FROM {table}")[0][0]


# ─────────────────────────────────────────────────────────────────────────────
# DashboardRepository
# ─────────────────────────────────────────────────────────────────────────────

class DashboardRepository:
    """Return daily collection metrics for the dashboard."""

    def __init__(self, conn_repo: OracleConnectionRepository):
        self.conn_repo = conn_repo

    def fetch_daily_dashboard(self, target_date_str: str = None) -> Dict[str, int]:
        if not target_date_str:
            target_date_str = datetime.datetime.now().strftime("%Y-%m-%d")

        stats = {"total": 0, "success": 0, "errors": 0}
        try:
            stats["total"] = int(
                self.conn_repo.execute_query(
                    "SELECT COUNT(*) FROM ERP.FCM_COLLECTION WHERE TRUNC(CREATION_DATE) = TO_DATE(:d, 'YYYY-MM-DD')",
                    {"d": target_date_str},
                )[0][0] or 0
            )
            stats["success"] = int(
                self.conn_repo.execute_query(
                    "SELECT COUNT(*) FROM ERP.FCM_COLLECTION WHERE TRUNC(CREATION_DATE) = TO_DATE(:d, 'YYYY-MM-DD') AND STATUS = 3",
                    {"d": target_date_str},
                )[0][0] or 0
            )
            stats["errors"] = max(0, stats["total"] - stats["success"])
        except Exception:
            pass
        return stats


# ─────────────────────────────────────────────────────────────────────────────
# ReportRepository
# ─────────────────────────────────────────────────────────────────────────────

class ReportRepository:
    """Retrieve upload / process reports and collection detail data."""

    def __init__(self, conn_repo: OracleConnectionRepository):
        self.conn_repo = conn_repo

    def get_upload_report(self, selected_date: str = None) -> Dict[str, Any]:
        report = {
            "timestamp":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rows":          [],
            "success_count": 0,
            "error_count":   0,
            "skipped_count": 0,
        }
        if not selected_date:
            return report

        rows = self.conn_repo.execute_query(
            """
            SELECT fc.id, fc.policy_no, fc.creation_date, fc.status, fc.serial
              FROM ERP.FCM_COLLECTION fc
             WHERE TRUNC(fc.creation_date) = TO_DATE(:date_str, 'YYYY-MM-DD')
             ORDER BY fc.creation_date DESC
            """,
            {"date_str": selected_date},
        )
        for row in rows:
            status = "SUCCESS" if row[3] == 3 else "PENDING"
            report["rows"].append({
                "POLICY_NUMBER": row[1],
                "CREATED_AT":    row[2],
                "STATUS":        status,
                "COLLECTION_ID": str(row[0]),
                "SERIAL":        str(row[4]),
            })
            if status == "SUCCESS":
                report["success_count"] += 1
            else:
                report["skipped_count"] += 1
        return report

    def get_collection_details(self, collection_id: int) -> Dict[str, Any]:
        """Return full collection info including line items."""
        try:
            rows = self.conn_repo.execute_query(
                """
                SELECT c.ID,         c.SERIAL,       c.STATUS_DATE,
                       c.STATUS,     c.TOTAL_DEBIT,  c.POLICY_NO,
                       cust.NAME,    cd.LINE_NUM,    cd.DEBIT_AMOUNT,
                       cd.CREDIT_AMOUNT
                  FROM ERP.FCM_COLLECTION     c
             LEFT JOIN ERP.FCS_CUSTOMERS      cust ON c.FCS_CST_ID = cust.ID
             LEFT JOIN ERP.FCM_COLLECTION_DET cd   ON c.ID = cd.FCM_COL_ID
                 WHERE c.ID = :collection_id
                 ORDER BY cd.LINE_NUM
                """,
                {"collection_id": collection_id},
            )
            if not rows:
                return {"success": False, "error": "Collection not found"}

            collection = {
                "collection_id": rows[0][0],
                "serial":        rows[0][1],
                "status_date":   rows[0][2],
                "status":        rows[0][3],
                "total_amount":  float(rows[0][4]),
                "policy_number": rows[0][5],
                "customer_name": rows[0][6],
                "details": [
                    {
                        "line_num":      row[7],
                        "debit_amount":  float(row[8]),
                        "credit_amount": float(row[9]),
                    }
                    for row in rows
                ],
            }
            return {"success": True, "data": collection}

        except Exception as e:
            return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Django view helper
# ─────────────────────────────────────────────────────────────────────────────

def delete_collection(request):
    """Django POST view — delegates deletion to PolicyRepository."""
    if request.method == "POST":
        collection_id = request.POST.get("collection_id")
        if collection_id:
            try:
                result = PolicyRepository(OracleConnectionRepository()).delete_collection(str(collection_id))
                if result.get("success"):
                    messages.success(request, f"تم حذف الحافظة {collection_id} بنجاح")
                else:
                    messages.error(request, f"فشل حذف الحافظة: {result.get('error')}")
            except Exception as e:
                messages.error(request, f"حدث خطأ: {e}")
        else:
            messages.error(request, "لم يتم تحديد رقم الحافظة")

    return redirect("collection:process_result")