"""Persistent account history; preserve legacy Store format and all issued bills."""

import asyncio
from copy import deepcopy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, HISTORY_STORAGE_VERSION, HISTORY_RETENTION_MONTHS
from .billing import _number, bill_response_ok
from .data import bill_key, issued_cost, unique_bills

_LOGGER = logging.getLogger(__name__)


class CRCGasHistoryStorage:
    """Account-scoped Store with serialized, atomic writes."""

    def __init__(self, hass: HomeAssistant, entry_id: str):
        self.hass = hass
        self.entry_id = entry_id
        self._store = Store(
            hass, 
            HISTORY_STORAGE_VERSION, 
            f"{DOMAIN}_history_{entry_id}"
        )
        self._lock = asyncio.Lock()
        self._loaded = False
        self._data = {
            "usage_history": [],  # 用气历史
            "payment_history": [],  # 缴费历史
            "bill_history": [],  # 账单历史
            "last_update": None,
        }

    async def async_load(self):
        async with self._lock:
            if self._loaded:
                return
            stored = await self._store.async_load()
            if stored is not None:
                if not isinstance(stored, dict) or any(
                    not isinstance(stored.get(key, []), list)
                    for key in ("usage_history", "payment_history", "bill_history")
                ):
                    raise ValueError("Invalid stored history")
                self._data.update(stored)
            self._advance_cost_baseline()
            self._loaded = True

    async def async_save(self):
        """Store.async_save awaits an atomic write; errors must reach the caller."""
        self._data["last_update"] = datetime.now(timezone.utc).isoformat()
        await self._store.async_save(deepcopy(self._data))

    async def _save_or_restore(self, before):
        try:
            await self.async_save()
        except Exception:
            self._data = before
            raise

    async def _add_record(self, key, record):
        async with self._lock:
            before = deepcopy(self._data)
            value = deepcopy(record)
            value.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            self._data[key].append(value)
            self._cleanup_old_data()
            await self._save_or_restore(before)

    async def async_add_usage_record(self, record: Dict[str, Any]):
        await self._add_record("usage_history", record)

    async def async_add_payment_record(self, record: Dict[str, Any]):
        await self._add_record("payment_history", record)

    async def async_add_bill_record(self, record: Dict[str, Any]):
        await self.async_merge_bill_records([record])

    def _merge_bills(self, records):
        bills = unique_bills(self._data.get("bill_history", []))
        indexed = {bill_key(b): b for b in bills if bill_key(b)}
        new = updated = 0
        if len(records) >= 6 and indexed and not any(bill_key(r) in indexed for r in records if isinstance(r, dict)):
            self._data["last_fetch_complete"] = False
        for record in records:
            if not isinstance(record, dict) or not (key := bill_key(record)):
                continue
            values = deepcopy(record)
            for field in ("billAmt", "gasAmt", "penaltyAmt", "revblAmt"):
                if field in values:
                    values[field] = _number(values[field])
            if key in indexed:
                indexed[key].update({k: v for k, v in values.items() if v is not None and k != "timestamp"})
                updated += 1
            else:
                bills.append(values)
                indexed[key] = values
                new += 1
            indexed[key].setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        self._data["bill_history"] = sorted(bills, key=lambda b: b.get("billYm") or "", reverse=True)
        self._advance_cost_baseline()
        return new, updated

    def _advance_cost_baseline(self):
        value = issued_cost(self._data.get("bill_history", []))
        baseline = _number(self._data.get("issued_cost_baseline"))
        if value is not None and (baseline is None or value >= baseline):
            self._data["issued_cost_baseline"] = value

    def get_issued_cost(self):
        if self._data.get("last_fetch_complete") is not True:
            return None
        value = issued_cost(self._data.get("bill_history", []))
        if not self._data.get("bill_history"):
            value = 0.0
        baseline = _number(self._data.get("issued_cost_baseline"))
        if value is None or (baseline is not None and value < baseline):
            return None
        return value

    async def async_merge_bill_records(self, records):
        async with self._lock:
            before = deepcopy(self._data)
            counts = self._merge_bills(records)
            await self._save_or_restore(before)
            return counts

    def _cleanup_old_data(self):
        """Bills underpin issued totals: never expire them, including legacy rows."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_MONTHS * 30)
        for key in ("usage_history", "payment_history"):
            self._data[key] = [
                record for record in self._data.get(key, [])
                if (stamp := self._parse_timestamp(record.get("timestamp"))) is None or stamp > cutoff
            ]

    def _parse_timestamp(self, value):
        try:
            stamp = datetime.fromisoformat(value)
            return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        except (ValueError, TypeError):
            return None

    def get_usage_history(self, limit: int = 12) -> List[Dict[str, Any]]:
        """获取用气历史"""
        return self._data.get("usage_history", [])[-limit:]

    def get_payment_history(self, limit: int = 12) -> List[Dict[str, Any]]:
        """获取缴费历史"""
        return self._data.get("payment_history", [])[-limit:]

    def get_bill_history(self, limit: Optional[int] = 12) -> List[Dict[str, Any]]:
        """获取账单历史"""
        records = sorted(self._data.get("bill_history", []), key=lambda b: b.get("billYm") or "", reverse=True)
        return deepcopy(records if limit is None else records[:limit])

    def get_monthly_usage(self, year: int, month: int) -> float:
        """获取指定月份用气量"""
        usage_records = self._data.get("usage_history", [])
        total = 0
        for record in usage_records:
            try:
                record_date = datetime.fromisoformat(record.get("timestamp", ""))
                if record_date.year == year and record_date.month == month:
                    total += float(record.get("gas_used", 0))
            except:
                continue
        return total

    def get_yearly_usage(self, year: int) -> float:
        """获取指定年度用气量"""
        usage_records = self._data.get("usage_history", [])
        total = 0
        for record in usage_records:
            try:
                record_date = datetime.fromisoformat(record.get("timestamp", ""))
                if record_date.year == year:
                    total += float(record.get("gas_used", 0))
            except:
                continue
        return total

    async def async_fetch_all_bills(self, api, cons_no: str) -> Dict[str, Any]:
        """Pagination failures are incomplete fetches, never an empty replacement."""
        async with self._lock:
            fetched = []
            seen_pages = set()
            complete = False
            error_type = None
            for page in range(1, 101):
                try:
                    response = await api.async_get_gas_bill_list(cons_no, page=page, page_num=6)
                    if not bill_response_ok(response):
                        raise ValueError("Unsuccessful history response")
                    data = response.get("dataResult")
                    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                        raise ValueError("Invalid history response shape")
                    bills = data["data"]
                    if any(not isinstance(b, dict) or not bill_key(b) for b in bills):
                        raise ValueError("Unidentified historical bill")
                    fingerprint = tuple(bill_key(b) for b in bills)
                    if bills and fingerprint in seen_pages:
                        raise ValueError("Repeated history page")
                    seen_pages.add(fingerprint)
                    fetched.extend(bills)
                    if len(bills) < 6:
                        complete = True
                        break
                except Exception as exc:
                    error_type = type(exc).__name__
                    _LOGGER.warning("History fetch failed: page=%d, error_type=%s", page, error_type)
                    break
            before = deepcopy(self._data)
            new, updated = self._merge_bills(fetched)
            self._data["last_fetch_complete"] = complete
            await self._save_or_restore(before)
            return {
                "new_bills": new, "updated_bills": updated, "total_fetched": len(fetched),
                "total_stored": len(self._data["bill_history"]), "complete": complete,
                "error_type": error_type if error_type else (None if complete else "PageLimit"),
            }

    def get_usage_trend(self, months: int = 12) -> List[Dict[str, Any]]:
        """获取用气趋势数据"""
        usage_records = self._data.get("usage_history", [])
        
        # 按月份分组
        monthly_data = {}
        for record in usage_records:
            try:
                record_date = datetime.fromisoformat(record.get("timestamp", ""))
                key = f"{record_date.year}-{record_date.month:02d}"
                if key not in monthly_data:
                    monthly_data[key] = 0
                monthly_data[key] += float(record.get("gas_used", 0))
            except:
                continue
        
        # 转换为列表并排序
        trend = [
            {"month": month, "usage": usage}
            for month, usage in sorted(monthly_data.items())
        ]
        
        return trend[-months:]


async def async_setup_history_storage(hass: HomeAssistant, entry_id: str) -> CRCGasHistoryStorage:
    """设置历史数据存储"""
    domain = hass.data.setdefault(DOMAIN, {})
    key = f"{entry_id}_history_storage"
    if key not in domain:
        domain[key] = CRCGasHistoryStorage(hass, entry_id)
    storage = domain[key]
    await storage.async_load()
    return storage
