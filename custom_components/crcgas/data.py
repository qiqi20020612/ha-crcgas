"""Small helpers for issued bills and explicit API aggregates."""

from datetime import datetime

from .billing import _number


def bill_key(bill):
    identity = bill.get("applicationNo") or bill.get("id")
    return (identity, bill.get("billYm")) if identity else None


def unique_bills(bills):
    records = {}
    for index, bill in enumerate(bills):
        if isinstance(bill, dict):
            records[bill_key(bill) or ("unidentified", index)] = bill
    return list(records.values())


def issued_cost(bills):
    """Missing amounts make the total unknown, never a partial total or estimate."""
    values = [_number(bill.get("billAmt")) for bill in unique_bills(bills)]
    if not values or any(value is None for value in values):
        return None
    return round(sum(values), 2)


def previous_usage(bills, current_period):
    periods = {}
    for bill in unique_bills(bills):
        ym = bill.get("billYm")
        try:
            datetime.strptime(ym, "%Y-%m")
        except (TypeError, ValueError):
            continue
        if current_period and ym < current_period:
            periods.setdefault(ym, []).append(_number(bill.get("gasAmt")))
    if not periods:
        return None, None
    period = max(periods)
    values = periods[period]
    return (sum(values) if all(v is not None for v in values) else None), period


def chart_values(data, now):
    """The bill chart is grouped by posting month, not physical consumption month."""
    this_year, last_year = data.get("thisGas"), data.get("lastGas")
    if not isinstance(this_year, list) or len(this_year) != 12:
        raise ValueError("Invalid current-year chart")
    if not isinstance(last_year, list) or len(last_year) != 12:
        raise ValueError("Invalid previous-year chart")
    previous = [_number(value) for value in last_year]
    average = round(sum(previous) / 12, 2) if all(v is not None for v in previous) else None
    return {
        "monthly_gas_used": _number(this_year[now.month - 1]),
        "year_avg_gas": average,
        "chart_month": now.strftime("%Y-%m"),
        "year_avg_year": now.year - 1,
        "usage_period_type": "billing_month",
    }


def parse_payment_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
