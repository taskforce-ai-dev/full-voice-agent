"""BSL backend tool implementations — called by tools.py execute_tool dispatcher."""
import logging

import aiohttp

import account_state
import mock_db

logger = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None


def is_configured() -> bool:
    return True


async def verify_customer_identity(
    call_sid: str,
    account_no: str,
    nic: str,
    dob: str,
    mothers_maiden_name: str,
) -> dict:
    state = account_state.get(call_sid)

    # Account-not-found path: do NOT burn a verification attempt — the caller
    # likely misspoke the account number, not their identity. Signal back so
    # the agent re-asks the account number instead of NIC/DOB/maiden name.
    record = mock_db.lookup_account(account_no)
    if record is None:
        attempts_used = state.get("verification_attempts", 0)
        logger.info(
            "verify_customer_identity | account_not_found | account_no_last4=%s",
            account_no[-4:] if len(account_no) >= 4 else account_no,
        )
        return {
            "verified": False,
            "account_not_found": True,
            "attempts_used": attempts_used,
            "attempts_remaining": max(0, 3 - attempts_used),
            "mismatched_fields": [],
            "account_holder": None,
            "is_business": False,
        }

    state["verification_attempts"] = min(state.get("verification_attempts", 0) + 1, 3)
    attempts_used = state["verification_attempts"]

    result = mock_db.verify_identity(account_no, nic, dob, mothers_maiden_name)

    if result["verified"]:
        state["verified_account"] = True

    account_holder = record["account_holder"]
    is_business = record.get("is_business", False)

    logger.info(
        "verify_customer_identity | account_no_last4=%s | verified=%s | attempt=%d",
        account_no[-4:] if len(account_no) >= 4 else account_no,
        result["verified"],
        attempts_used,
    )

    return {
        "verified": result["verified"],
        "account_not_found": False,
        "attempts_used": attempts_used,
        "attempts_remaining": max(0, 3 - attempts_used),
        "mismatched_fields": result.get("mismatched_fields", []),
        "account_holder": account_holder,
        "is_business": is_business,
    }


async def get_account_balance(call_sid: str, account_no: str) -> dict:
    record = mock_db.lookup_account(account_no)
    if record is None:
        return {"error": "Account not found", "account_no": account_no}

    state = account_state.get(call_sid)
    card_block_state = state.get("card_block_state", {})
    card_status = card_block_state.get(record["account_no"], record["card"]["status"])

    logger.info("get_account_balance | account_no_last4=%s", record["account_no_last4"])

    return {
        "account_no_last4": record["account_no_last4"],
        "account_holder": record["account_holder"],
        "company_name": record.get("company_name"),
        "account_type": record["account_type"],
        "is_business": record["is_business"],
        "closing_balance": record["summary"]["closing_balance"],
        "statement_period": record["summary"]["statement_period"],
        "card_status": card_status,
    }


async def block_debit_card(call_sid: str, account_no: str) -> dict:
    record = mock_db.lookup_account(account_no)
    if record is None:
        return {"error": "Account not found"}

    state = account_state.get(call_sid)
    if "card_block_state" not in state or state["card_block_state"] is None:
        state["card_block_state"] = {}
    state["card_block_state"][record["account_no"]] = "Blocked"

    logger.info(
        "block_debit_card | account_no_last4=%s | card_type=%s",
        record["account_no_last4"],
        record["card"]["card_type"],
    )

    return {
        "blocked": True,
        "account_no_last4": record["account_no_last4"],
        "card_type": record["card"]["card_type"],
        "card_number_masked": record["card"]["number_masked"],
        "is_business": record["is_business"],
        "account_holder": record["account_holder"],
        "company_name": record.get("company_name"),
    }


async def get_account_details(call_sid: str, account_no: str) -> dict:
    record = mock_db.lookup_account(account_no)
    if record is None:
        return {"error": "Account not found"}

    logger.info("get_account_details | account_no_last4=%s", record["account_no_last4"])

    return {
        "account_no_last4": record["account_no_last4"],
        "account_holder": record["account_holder"],
        "company_name": record.get("company_name"),
        "account_type": record["account_type"],
        "is_business": record["is_business"],
        "branch": record["branch"],
        "opened_date": record["opened_date"],
        "internet_banking": record["summary"]["internet_banking"],
        "mobile_banking": record["summary"]["mobile_banking"],
        "registered_mobile": record["summary"]["registered_mobile"],
        "closing_balance": record["summary"]["closing_balance"],
    }


async def get_recent_transactions(
    call_sid: str, account_no: str, limit: int = 10
) -> dict:
    record = mock_db.lookup_account(account_no)
    if record is None:
        return {"error": "Account not found"}

    logger.info(
        "get_recent_transactions | account_no_last4=%s | limit=%d",
        record["account_no_last4"],
        limit,
    )

    return {
        "account_no_last4": record["account_no_last4"],
        "account_holder": record["account_holder"],
        "is_business": record["is_business"],
        "statement_period": record["summary"]["statement_period"],
        "transactions": list(reversed(record["transactions"]))[:limit],
        "total_transactions": len(record["transactions"]),
    }


async def get_loans(call_sid: str, account_no: str) -> dict:
    record = mock_db.lookup_account(account_no)
    if record is None:
        return {"error": "Account not found"}

    loans = record.get("loans", [])
    logger.info(
        "get_loans | account_no_last4=%s | loan_count=%d",
        record["account_no_last4"],
        len(loans),
    )

    return {
        "account_no_last4": record["account_no_last4"],
        "account_holder": record["account_holder"],
        "is_business": record["is_business"],
        "loans": loans,
        "loan_count": len(loans),
    }


async def get_standing_orders(call_sid: str, account_no: str) -> dict:
    record = mock_db.lookup_account(account_no)
    if record is None:
        return {"error": "Account not found"}

    orders = record.get("standing_orders", [])
    logger.info(
        "get_standing_orders | account_no_last4=%s | order_count=%d",
        record["account_no_last4"],
        len(orders),
    )

    return {
        "account_no_last4": record["account_no_last4"],
        "account_holder": record["account_holder"],
        "is_business": record["is_business"],
        "standing_orders": orders,
        "order_count": len(orders),
    }


async def request_live_agent_handoff(caller_phone: str, reason: str) -> dict:
    logger.info(
        "request_live_agent_handoff | caller=%s | reason=%s", caller_phone, reason
    )

    if reason == "verification_failed":
        message = (
            "I'm afraid I'm unable to verify your identity at this stage. "
            "For your security, I'll transfer you to one of our team members who can "
            "assist you further. Please hold."
        )
    else:
        message = (
            "Of course. Please hold while I transfer you to one of our team members "
            "who can assist you further."
        )

    return {
        "handoff": True,
        "reason": reason,
        "message": message,
    }
