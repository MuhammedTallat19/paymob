"""
Commission Calculation Module

Handles commission calculations for policies based on:
- Day difference between issue date and payment date
- Cost center and account information
- Agent information
- Commission setup rules from FGL_NETTING_SETUP

Commission types:
    1 — Early Commission
    2 — Collection Commission
    6 — Basic Commission
"""

import datetime
from typing import Any


class CommCalc:
    """
    Commission Calculator for policies.

    Calculates commissions from FGL_NETTING_SETUP using:
    - Policy segment code
    - Payment date
    - Day difference between issue and payment dates
    - Cost center, account, and agent information
    """

    def __init__(self, conn, segment_code: str, payment_date: str):
        """
        Args:
            conn:          Oracle database connection
            segment_code:  Policy segment code (policy number)
            payment_date:  Payment date in 'YYYY-MM-DD' format
        """
        self.conn = conn
        self.segment_code = segment_code
        self.payment_date = payment_date
        print(f"\n[COMMISSION] Initializing CommCalc — segment_code={segment_code}, payment_date={payment_date}")

    # ─────────────────────────────────────────────────────────────────────────
    # Public helpers
    # ─────────────────────────────────────────────────────────────────────────

    def get_day_difference_issue_paydate(self, issue_date, payment_date: str) -> int:
        """Return the number of days between issue_date and payment_date."""
        delta = (datetime.datetime.strptime(payment_date, "%Y-%m-%d") - issue_date).days
        print(f"[COMMISSION] Day difference: payment={payment_date}, issue={issue_date}, diff={delta}")
        return delta

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry points
    # ─────────────────────────────────────────────────────────────────────────

    def compute_commissions(self) -> list[dict]:
        """
        Calculate commission setups for all three commission types.

        Returns a flat list of commission dicts with keys:
            FROM_DAY, TO_DAY, PERCENTAGE, IS_TAXABLE, COMM_TYPE, TAX_PER
        """
        print(f"\n{'=' * 80}")
        print(f"[COMMISSION] compute_commissions — segment_code={self.segment_code}")
        print(f"{'=' * 80}")

        policy_rows = self.__get_cost_center_agent_account_agent_id()
        if not policy_rows:
            print("[COMMISSION] No policy rows found — returning empty list")
            print(f"{'=' * 80}\n")
            return []

        fcs_agt_id = self.__get_agent_id()
        print(f"[COMMISSION] Agent ID: {fcs_agt_id}")

        commissions: dict[int, list] = {1: [], 2: [], 6: []}

        for idx, row in enumerate(policy_rows, 1):
            print(f"\n[COMMISSION] Processing row {idx}/{len(policy_rows)}")
            fgl_cpc_id     = row["FGL_CPC_ID"]
            fgl_coa_id     = row["FGL_COA_ID"]
            effective_date = row["EFFECTIVE_DATE"]

            day_diff = self.get_day_difference_issue_paydate(effective_date, self.payment_date)

            for comm_type in (1, 2, 6):
                type_name = {1: "Early", 2: "Collection", 6: "Basic"}[comm_type]
                print(f"\n[COMMISSION] --- {type_name} Commission (Type {comm_type}) ---")

                comm = self.__calc_day_commission(comm_type, fgl_cpc_id, fgl_coa_id, fcs_agt_id, day_diff)
                if not comm:
                    print("[COMMISSION] No result with agent ID — retrying without agent ID")
                    comm = self.__calc_day_commission(comm_type, fgl_cpc_id, fgl_coa_id, None, day_diff)

                if not comm:
                    print(f"[COMMISSION] No commission found for {type_name}")
                    continue

                # Resolve tax percentage once for this batch
                tax_percentage = 0
                if any(item.get("IS_TAXABLE") for item in comm):
                    tax_result = self.__get_tax_per(fcs_agt_id, fgl_coa_id)
                    if tax_result:
                        tax_percentage = tax_result[0]["PERCENTAGE"]
                    print(f"[COMMISSION] Tax percentage: {tax_percentage}")
                else:
                    print("[COMMISSION] Not taxable — tax percentage = 0")

                for item in comm:
                    item["TAX_PER"] = tax_percentage if item.get("IS_TAXABLE") else 0

                commissions[comm_type].extend(comm)
                print(f"[COMMISSION] Added {len(comm)} {type_name} commission(s)")

        result = commissions[1] + commissions[2] + commissions[6]
        print(f"\n[COMMISSION] Done — total={len(result)} "
              f"(Early={len(commissions[1])}, Collection={len(commissions[2])}, Basic={len(commissions[6])})")
        print(f"{'=' * 80}\n")
        return result

    def calc_commission_amount(self) -> list[dict]:
        """
        Return commission dicts enriched with COMMISSION_AMOUNT and COMMISSION_AMOUNT_TAX.
        """
        commission_rows = self.compute_commissions()
        net_premium, issue_date = self.__get_net_premium_issue_date()
        print(f"[COMMISSION] Net premium={net_premium}, Issue date={issue_date}")

        result = []
        for comm in commission_rows:
            comm_amount = (comm["PERCENTAGE"] / 100) * net_premium
            comm["COMMISSION_AMOUNT"] = comm_amount

            if comm["IS_TAXABLE"]:
                tax_amount = (comm_amount * comm["TAX_PER"]) / 100
                comm["COMMISSION_AMOUNT_TAX"] = comm_amount + tax_amount
            else:
                comm["COMMISSION_AMOUNT_TAX"] = comm_amount

            result.append(comm)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Private DB helpers
    # ─────────────────────────────────────────────────────────────────────────

    def __get_cost_center_agent_account_agent_id(self) -> list[dict[str, Any]]:
        """
        Return FGL_CPC_ID, FGL_COA_ID, FCS_AGT_ID, EFFECTIVE_DATE for this policy.

        Uses EXISTS instead of a JOIN on gpd_ins_shares to avoid duplicate rows
        when a single installment has multiple matching ROLE_TYPE shares.
        """
        print("[COMMISSION] Fetching cost center / account / agent info")
        query = """
            SELECT DETL.FGL_CPC_ID,
                   DETL.FGL_COA_ID,
                   DETL.FCS_AGT_ID,
                   POL.EFFECTIVE_DATE
              FROM igeneral.GPD_POLICIES POL
              JOIN igeneral.gpd_installments INS  ON POL.ID       = INS.GPD_PLC_ID
              JOIN igeneral.gpd_vouchers     VOU  ON INS.ID       = VOU.gpd_ins_id
              JOIN ERP.fgl_transactions      FGL  ON VOU.FGL_TRN_ID = FGL.ID
              JOIN ERP.fgl_trans_details     DETL ON FGL.ID       = DETL.FGL_TRN_id
             WHERE EXISTS (
                       SELECT 1
                         FROM igeneral.gpd_ins_shares SHAR
                        WHERE SHAR.GPD_INS_ID = INS.ID
                          AND SHAR.ROLE_TYPE  IN (1, 2)
                   )
               AND POL.segment_code    = :seg_code
               AND DETL.serial         = 2
               AND DETL.is_deleted     = 0
               AND DETL.FCR_TRT_CODE   = 6
        """
        cursor = self.conn.cursor()
        cursor.execute(query, {"seg_code": self.segment_code})
        columns = [col[0] for col in cursor.description]
        rows: list[dict[str, Any]] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        print(f"[COMMISSION] Found {len(rows)} row(s)")
        return rows

    def __get_agent_id(self) -> int | None:
        """Return the agent customer ID (ROLE_TYPE=3) for this policy."""
        print("[COMMISSION] Fetching agent ID")
        query = """
            SELECT gps.FCS_CST_ID
              FROM IGENERAL.GPD_POLICIES   gp
              JOIN IGENERAL.GPD_PLC_SHARES gps ON gp.ID = gps.GPD_PLC_ID
             WHERE gps.ROLE_TYPE    = 3
               AND gp.SEGMENT_CODE  = :seg_code
        """
        cursor = self.conn.cursor()
        cursor.execute(query, {"seg_code": self.segment_code})
        row = cursor.fetchone()
        agent_id = row[0] if row else None
        print(f"[COMMISSION] Agent ID: {agent_id}")
        return agent_id

    def __get_net_premium_issue_date(self) -> tuple:
        """Return (net_premium, issue_date) for this policy."""
        query = """
            SELECT net_premium, issue_date
              FROM igeneral.gpd_policies
             WHERE segment_code = :seg_code
        """
        cursor = self.conn.cursor()
        cursor.execute(query, {"seg_code": self.segment_code})
        row = cursor.fetchone()
        if row:
            return row[0], row[1]
        return None, None

    def __calc_day_commission(
        self,
        type_code: int,
        fgl_cpc_id,
        fgl_coa_id,
        fcs_agt_id,
        day_difference: int,
    ) -> list[dict[str, Any]]:
        """Query FGL_NETTING_SETUP for a matching commission rule."""
        print(f"[COMMISSION] Querying FGL_NETTING_SETUP — type={type_code}, days={day_difference}, "
              f"cpc={fgl_cpc_id}, coa={fgl_coa_id}, agt={fcs_agt_id}")

        query = """
            SELECT DISTINCT
                   fns.FROM_DAY,
                   fns.TO_DAY,
                   fns.PERCENTAGE,
                   fns.IS_TAXABLE,
                   fns.COMM_TYPE
              FROM erp.FGL_NETTING_SETUP fns
             WHERE fns.FGL_CPC_ID    = :cpc_id
               AND fns.REV_FGL_COA_ID = :fgl_coal_id
               AND :diff_days BETWEEN fns.FROM_DAY AND fns.TO_DAY
               AND fns.COMM_TYPE     = :type
        """
        params: dict[str, Any] = {
            "cpc_id":     fgl_cpc_id,
            "fgl_coal_id": fgl_coa_id,
            "diff_days":  day_difference,
            "type":       type_code,
        }
        if fcs_agt_id is not None:
            query += " AND fns.FCS_AGT_ID = :fcs_agt_id"
            params["fcs_agt_id"] = fcs_agt_id
        else:
            query += " AND fns.FCS_AGT_ID IS NULL"

        cursor = self.conn.cursor()
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        rows: list[dict[str, Any]] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        print(f"[COMMISSION] FGL_NETTING_SETUP matched {len(rows)} row(s)")
        return rows

    def __get_tax_per(self, customer_id, account_id) -> list[dict[str, Any]]:
        """Return the tax PERCENTAGE for a given customer and account."""
        print(f"[COMMISSION] Fetching tax percentage — customer={customer_id}, account={account_id}")
        query = """
            SELECT fct.PERCENTAGE
              FROM erp.FCS_CUSTOMERS      fc
              JOIN erp.FCS_CUSTOMER_TAXES fct ON fc.ID = fct.FCS_CST_ID
             WHERE fc.ID          = :cust_id
               AND fct.FGL_COA_ID = :account_id
        """
        cursor = self.conn.cursor()
        cursor.execute(query, {"cust_id": customer_id, "account_id": account_id})
        columns = [col[0] for col in cursor.description]
        rows: list[dict[str, Any]] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        if rows:
            print(f"[COMMISSION] Tax percentage: {rows[0].get('PERCENTAGE')}")
        else:
            print("[COMMISSION] No tax percentage found")
        return rows