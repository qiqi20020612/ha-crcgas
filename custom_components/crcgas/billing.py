"""Parse current bills without inventing missing usage, charges or tariffs."""

import logging
from math import isclose, isfinite

_LOGGER = logging.getLogger(__name__)

TIER_FIELDS = tuple(
    field
    for tier in range(1, 4)
    for field in (
        f"step{tier}_gas_used", f"step{tier}_gas_limit",
        f"step{tier}_gas_sum", f"step{tier}_remain", f"gas_price_step{tier}",
    )
)
BILL_NUMERIC_FIELDS = TIER_FIELDS + (
    "bill_amount", "revbl_amount", "penalty_amount", "this_gas_used",
    "this_read", "last_read", "monthly_gas_used", "estimated_gas_bill_amount",
    "single_gas_used", "single_gas_amount", "single_gas_price", "average_gas_price",
)
BILL_ATTRIBUTES = (
    "pricing_mode", "bill_amount_source", "gas_price_source", "gas_steps",
    "single_gas_used", "single_gas_amount", "single_gas_price",
    "average_gas_price", "average_gas_price_source",
)


def _number(value):
    """Keep actual zeroes; missing, malformed and non-finite values are unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ""))
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def bill_response_ok(response):
    """An HTTP-200 business failure must not be parsed as an empty bill."""
    return (
        isinstance(response, dict)
        and response.get("success") is True
        and response.get("statusCode") in (None, 200, "200")
    )


def parse_bill_summary(bill):
    """The bill's charge is billAmt, not the receivable or the penalty."""
    amount = _number(bill.get("billAmt"))
    gas = _number(bill.get("gasAmt"))
    penalty = _number(bill.get("penaltyAmt"))
    return {
        "_last_bill_ym": bill.get("billYm"),
        "_last_app_no": bill.get("applicationNo"),
        "bill_month": bill.get("billYm") or "未知",
        "bill_amount": amount,
        "bill_amount_source": "bill_list.billAmt" if amount is not None else None,
        "this_gas_used": gas,
        "last_bill_gas_amt": gas,
        "penalty_amount": penalty,
        "last_bill_penalty": penalty,
        "revbl_amount": _number(bill.get("revblAmt")),
        "settle_flag": bill.get("settleFlag") or "未知",
        "penalty_date": bill.get("penaltyDate") or "未知",
    }


def parse_price_info(price_info):
    """Optional tariff metadata must never prevent parsing the bill itself."""
    prices = price_info.get("consPrcDetailList") if isinstance(price_info, dict) else None
    entries = prices if isinstance(prices, list) else []
    result = {"gas_price_source": {}}
    for price in entries:
        if not isinstance(price, dict):
            continue
        rule = price.get("ruleCode")
        tier = {"0201": 1, "0202": 2, "0203": 3}.get(rule) if isinstance(rule, str) else None
        if tier is None:
            continue
        result["pricing_mode"] = "tiered"
        result[f"step{tier}_remain"] = _number(price.get("levGqRemain"))
        result[f"step{tier}_gas_limit"] = _number(price.get("levGq"))
        result[f"step{tier}_gas_sum"] = _number(price.get("levGqSum"))
        result[f"gas_price_step{tier}"] = _number(price.get("catPrc"))
        if result[f"gas_price_step{tier}"] is not None:
            result["gas_price_source"][f"step{tier}"] = "consPrcInfo.catPrc"
    _LOGGER.debug(
        "getGasBillList tariffs: info_type=%s, entries=%d, mode=%s",
        type(price_info).__name__, len(entries), result.get("pricing_mode", "unknown"),
    )
    return result


def _step_kind(label):
    if not isinstance(label, str):
        return None
    if label == "单一气价":
        return "single"
    for tier, name in enumerate(("一阶", "二阶", "三阶"), 1):
        if name in label:
            return f"step{tier}"
    return None


def parse_bill_detail(detail, result):
    """Enrich one current bill, keeping list totals and explicit zero values."""
    for source, target in (("thisRead", "this_read"), ("lastRead", "last_read")):
        result[target] = _number(detail.get(source))
    for source, target in (("thisReadTime", "this_read_time"), ("lastReadTime", "last_read_time")):
        result[target] = detail.get(source) or "未知"
    for source, target in (("gasUsed", "this_gas_used"), ("penaltyAmount", "penalty_amount")):
        value = _number(detail.get(source))
        if value is not None:
            result[target] = value

    raw_steps = detail.get("gasStepList")
    steps = raw_steps if isinstance(raw_steps, list) else []
    parsed = [
        {
            "step_type": _step_kind(step.get("stepType")),
            "gas_used": _number(step.get("gasUsed")),
            "amount": _number(step.get("stepAmt")),
            "unit_price": _number(step.get("catPrc")),
        }
        for step in steps if isinstance(step, dict)
    ]
    result["gas_steps"] = parsed
    if (
        result.get("this_gas_used") is None and parsed and len(parsed) == len(steps)
        and all(s["gas_used"] is not None for s in parsed)
    ):
        result["this_gas_used"] = sum(s["gas_used"] for s in parsed)

    if result.get("bill_amount") is None:
        for key in ("totalAmount", "billAmount"):
            amount = _number(detail.get(key))
            if amount is not None:
                result["bill_amount"] = amount
                result["bill_amount_source"] = f"bill_detail.{key}"
                break
        else:
            if parsed and len(parsed) == len(steps) and all(s["amount"] is not None for s in parsed):
                result["bill_amount"] = round(sum(s["amount"] for s in parsed), 2)
                result["bill_amount_source"] = "bill_detail.sum(stepAmt)"

    kinds = {s["step_type"] for s in parsed}
    complete = bool(parsed) and len(parsed) == len(steps) and None not in kinds
    if complete and kinds == {"single"}:
        result["pricing_mode"] = "single"
        for key in TIER_FIELDS:
            result[key] = None
        result["gas_price_source"] = {}
        for source, target in (("gas_used", "single_gas_used"), ("amount", "single_gas_amount")):
            if all(s[source] is not None for s in parsed):
                result[target] = round(sum(s[source] for s in parsed), 6)
        prices = {s["unit_price"] for s in parsed}
        if len(prices) == 1 and None not in prices:
            result["single_gas_price"] = prices.pop()
            result["gas_price_source"]["single"] = "bill_detail.catPrc"
    elif complete and "single" not in kinds:
        result["pricing_mode"] = "tiered"
        for tier in range(1, 4):
            matching = [s for s in parsed if s["step_type"] == f"step{tier}"]
            result[f"step{tier}_gas_used"] = (
                sum(s["gas_used"] for s in matching)
                if all(s["gas_used"] is not None for s in matching) else None
            )
            prices = {s["unit_price"] for s in matching}
            if len(prices) == 1 and None not in prices:
                result[f"gas_price_step{tier}"] = prices.pop()
                result.setdefault("gas_price_source", {})[f"step{tier}"] = "bill_detail.catPrc"

    _LOGGER.debug(
        "getBillDetail parsed: steps=%d, recognized=%d, mode=%s, amount_source=%s",
        len(steps), sum(s["step_type"] is not None for s in parsed),
        result.get("pricing_mode", "unknown"), result.get("bill_amount_source"),
    )


def finalize_bill(result):
    """Expose a calculated average separately; estimate only from official tariffs."""
    gas = result.get("this_gas_used")
    amount = result.get("bill_amount")
    result["average_gas_price"] = None
    result["average_gas_price_source"] = None
    if gas is not None and gas > 0 and amount is not None:
        result["average_gas_price"] = round(amount / gas, 6)
        result["average_gas_price_source"] = "calculated: bill_amount / this_gas_used"

    result["estimated_gas_bill_amount"] = None
    if result.get("pricing_mode") == "single":
        price = result.get("single_gas_price")
        if gas is not None and price is not None:
            result["estimated_gas_bill_amount"] = round(gas * price, 2)
    elif result.get("pricing_mode") == "tiered":
        usage = [result.get(f"step{tier}_gas_used") for tier in range(1, 4)]
        prices = [result.get(f"gas_price_step{tier}") for tier in range(1, 4)]
        if (
            gas is not None and all(u is not None for u in usage)
            and isclose(sum(usage), gas, abs_tol=0.000001)
            and all(u == 0 or p is not None for u, p in zip(usage, prices))
        ):
            result["estimated_gas_bill_amount"] = round(
                sum(u * p for u, p in zip(usage, prices) if u != 0), 2
            )
