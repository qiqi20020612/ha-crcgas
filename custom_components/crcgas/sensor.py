"""华润燃气 传感器平台 - v1.2.7+

基于v1.2.7 | 新增: 三档用气量/气价, 上期读数/时间, 结算状态, 燃气表总读数(能源面板),
保持上次有效值, 历史数据注入(HA统计+SQLite), 燃气累计用量传感器
"""

import json
import logging
from datetime import timedelta, datetime, timezone
from typing import Any, Dict, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:
    from homeassistant.components.sensor import SensorEntity as BaseSensor, RestoreSensor
except ImportError:
    from homeassistant.helpers.entity import Entity as BaseSensor
    RestoreSensor = BaseSensor

from homeassistant.components.sensor.const import SensorStateClass as _SSC

from .api import HuarunGasApi, SessionTimeoutError
from .billing import _number
from .data import bill_key, chart_values, parse_payment_time, previous_usage
from .history_storage import async_setup_history_storage
from .billing import (
    BILL_ATTRIBUTES,
    BILL_NUMERIC_FIELDS,
    bill_response_ok,
    finalize_bill,
    parse_bill_detail,
    parse_bill_summary,
    parse_price_info,
)
from .const import (
    CONF_BO_TOKEN,
    CONF_CONS_NO,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_UNIT,
    CONF_WX_CODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INTEGRATION_STATUS,
    SENSOR_TYPES,
    TOKEN_EXPIRE_THRESHOLD,
)

_LOGGER = logging.getLogger(__name__)


def _safe_float(value, default=0):
    """安全转换数字为float，去除千分位逗号"""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return default


_STATE_CLASS_MAP = {
    "this_read": _SSC.TOTAL_INCREASING,
    "total_gas_consumption": _SSC.TOTAL_INCREASING,
    "gas_energy_dashboard": _SSC.TOTAL_INCREASING,
    "total_gas_cost": _SSC.TOTAL,
}
_NUMERIC_FIELDS = set(BILL_NUMERIC_FIELDS) | {
    "arrears", "account_balance", "last_pay_amount", "annual_pay_count",
    "last_month_gas", "year_avg_gas", "total_gas_cost",
}
_METER_TOTALS = ("total_gas_consumption", "gas_energy_dashboard")


class HuarunGasSensor(RestoreSensor):
    """华润燃气传感器基类"""

    def __init__(self, coordinator: DataUpdateCoordinator, sensor_type: str):
        self.coordinator = coordinator
        self.sensor_type = sensor_type
        self._attr_unique_id = f"{DOMAIN}_{sensor_type}"
        self._attr_name = SENSOR_TYPES[sensor_type]["name"]
        self._attr_icon = SENSOR_TYPES[sensor_type].get("icon")
        self._attr_native_unit_of_measurement = SENSOR_TYPES[sensor_type].get("unit")
        self._attr_state_class = _STATE_CLASS_MAP.get(sensor_type)
        self._attr_device_class = SENSOR_TYPES[sensor_type].get("device_class")
        self._last_good_value = None

    @property
    def device_info(self) -> Dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.config_entry.entry_id)},
            "name": "华润燃气",
            "manufacturer": "华润燃气",
        }

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self):
        """Keep official tariffs separate from a bill's calculated average."""
        if self.sensor_type in (
            "bill_amount", "this_gas_used", "gas_price_step1",
            "gas_price_step2", "gas_price_step3",
        ):
            data = self.coordinator.data or {}
            return {key: data.get(key) for key in BILL_ATTRIBUTES}
        if self.sensor_type in ("monthly_gas_used", "year_avg_gas", "last_month_gas"):
            data = self.coordinator.data or {}
            return {key: data.get(key) for key in (
                "usage_period_type", "chart_month", "year_avg_year", "previous_bill_period",
            )}
        if self.sensor_type == "total_gas_cost":
            return {"cost_basis": "issued_bills", "history_complete": (self.coordinator.data or {}).get("history_complete")}
        if self.sensor_type == "integration_status":
            return {"failed_endpoints": (self.coordinator.data or {}).get("failed_endpoints", [])}
        return None

    async def async_added_to_hass(self):
        """Restore only valid physical totals; a legacy placeholder zero is not a baseline."""
        await super().async_added_to_hass()
        if self.sensor_type in _METER_TOTALS:
            restored = await self.async_get_last_sensor_data()
            if restored and restored.native_unit_of_measurement == "m³":
                value = _number(restored.native_value)
                if value is not None and value > 0:
                    self._last_good_value = value
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
        if self.coordinator.data is not None:
            self.async_write_ha_state()

    @property
    def native_value(self):
        """获取传感器值"""
        data = self.coordinator.data
        if not data:
            if self.sensor_type in (*_METER_TOTALS, "total_gas_cost"):
                return self._last_good_value
            return None

        if self.sensor_type in _METER_TOTALS:
            value = _number(data.get("this_read"))
            if value is not None and value >= 0:
                self._last_good_value = value
            return self._last_good_value
        if self.sensor_type in _NUMERIC_FIELDS:
            value = _number(data.get(self.sensor_type))
            if self.sensor_type == "total_gas_cost" and value is not None:
                self._last_good_value = value
            return value

        if self.sensor_type == "arrears":
            v = data.get("arrears")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "account_balance":
            v = data.get("account_balance")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "last_pay_time":
            v = data.get("last_pay_time")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "last_pay_amount":
            v = data.get("last_pay_amount")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "annual_pay_count":
            v = data.get("annual_pay_count")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "this_read":
            return float(data["this_read"])

        elif self.sensor_type == "total_gas_consumption":
            v = data.get("this_read")
            val = float(v) if v is not None else 0.0
            if val > 0:
                self._last_good_value = val
                return val
            return self._last_good_value if self._last_good_value is not None else 0.0

        elif self.sensor_type == "total_gas_cost":
            v = data.get("total_gas_cost")
            val = float(v) if v is not None else 0.0
            if val > 0:
                self._last_good_value = val
                return val
            return self._last_good_value if self._last_good_value is not None else 0.0

        elif self.sensor_type == "this_read_time":
            v = data.get("this_read_time")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "step1_gas_used":
            v = data.get("step1_gas_used")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step2_gas_used":
            v = data.get("step2_gas_used")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step3_gas_used":
            v = data.get("step3_gas_used")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "this_gas_used":
            v = data.get("this_gas_used")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "bill_amount":
            v = data.get("bill_amount")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step1_remain":
            v = data.get("step1_remain")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step2_remain":
            v = data.get("step2_remain")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step3_remain":
            v = data.get("step3_remain")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step1_gas_limit":
            v = data.get("step1_gas_limit")
            return float(v) if v is not None else 330.0

        elif self.sensor_type == "step1_gas_sum":
            v = data.get("step1_gas_sum")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step2_gas_limit":
            v = data.get("step2_gas_limit")
            return float(v) if v is not None else 170.0

        elif self.sensor_type == "penalty_amount":
            v = data.get("penalty_amount")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "last_read":
            v = data.get("last_read")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "last_read_time":
            v = data.get("last_read_time")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "settle_flag":
            v = data.get("settle_flag")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "cons_addr":
            v = data.get("cons_addr")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "org_name":
            v = data.get("org_name")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "gas_nature":
            v = data.get("gas_nature")
            return v if v and v != "未知" else "天然气"

        elif self.sensor_type == "purchase_style":
            v = data.get("purchase_style")
            style_map = {"01": "IC卡", "02": "物联网表", "03": "普通表"}
            return style_map.get(v, v) if v else "未知"

        elif self.sensor_type == "last_month_gas":
            v = data.get("last_month_gas")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "year_avg_gas":
            v = data.get("year_avg_gas")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "integration_status":
            v = data.get("integration_status")
            return INTEGRATION_STATUS.get(v, v) if v else "未知"

        elif self.sensor_type == "monthly_gas_used":
            v = data.get("monthly_gas_used")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "gas_energy_dashboard":
            v = data.get("this_read")
            val = float(v) if v is not None else 0.0
            if val > 0:
                self._last_good_value = val
                return val
            return self._last_good_value if self._last_good_value is not None else 0.0

        elif self.sensor_type == "gas_price_step1":
            v = data.get("gas_price_step1")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "gas_price_step2":
            v = data.get("gas_price_step2")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "gas_price_step3":
            v = data.get("gas_price_step3")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step2_gas_sum":
            v = data.get("step2_gas_sum")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step3_gas_sum":
            v = data.get("step3_gas_sum")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "step3_gas_limit":
            v = data.get("step3_gas_limit")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "estimated_gas_bill_amount":
            v = data.get("estimated_gas_bill_amount")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "cons_name":
            v = data.get("cons_name")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "mobile":
            v = data.get("mobile")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "meter_no":
            v = data.get("meter_no")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "area_name":
            v = data.get("area_name")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "account_status":
            v = data.get("account_status")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "revbl_amount":
            v = data.get("revbl_amount")
            return float(v) if v is not None else 0.0

        elif self.sensor_type == "penalty_date":
            v = data.get("penalty_date")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "last_fetch_time":
            v = data.get("last_fetch_time")
            return v if v and v != "未知" else "未知"

        elif self.sensor_type == "bill_month":
            v = data.get("bill_month")
            return v if v and v != "未知" else "未知"

        return None


    # === 补充传感器类型（数据从API现有字段提取） ===
    def _is_missing_sensor(self):
        return self.sensor_type in ("cons_name", "mobile", "meter_no", "area_name",
                                     "account_status", "revbl_amount", "penalty_date",
                                     "last_fetch_time", "bill_month")


class HuarunGasMeterHistorySensor(BaseSensor):
    """燃气累计用量传感器 — 用于能源面板显示完整历史数据"""

    def __init__(self, coordinator, entry_id):
        self._coordinator = coordinator
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_gas_meter_history"
        self._attr_name = "燃气累计用量"
        self._attr_icon = "mdi:counter"
        self._attr_native_unit_of_measurement = "m\u00b3"
        self._attr_state_class = _SSC.TOTAL_INCREASING
        self._attr_device_class = "gas"
        self._last_good_value = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "name": "华润燃气",
            "manufacturer": "华润燃气",
        }

    @property
    def available(self):
        return self._coordinator.last_update_success

    @property
    def native_value(self):
        data = self._coordinator.data
        if data:
            v = data.get("this_read")
            val = float(v) if v is not None else None
            if val is not None and val > 0:
                self._last_good_value = val
                return val
        return self._last_good_value

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
        if self._coordinator.data is not None:
            self.async_write_ha_state()

async def async_setup_entry(

    hass: HomeAssistant,

    config_entry: ConfigEntry,

    async_add_entities,

):

    """设置传感器"""

    cons_no = config_entry.data.get(CONF_CONS_NO, "")



    # 复用 __init__.py 中创建的 API 实例（共享 Token 刷新状态）

    api = hass.data[DOMAIN].get(f"{config_entry.entry_id}_api")

    if api is None:

        # 兜底：如果 __init__ 未创建（不应发生），本地创建

        _LOGGER.warning("未找到共享API实例，本地创建（Token刷新可能不同步）")

        refresh_token = config_entry.data[CONF_REFRESH_TOKEN]

        bo_token = config_entry.data[CONF_BO_TOKEN]

        wx_code = config_entry.data[CONF_WX_CODE]



        async def on_token_refresh(new_refresh_token: str, new_bo_token: str):

            """Token刷新后保存"""

            _LOGGER.info("保存新Token到config_entry")

            new_data = {**config_entry.data}

            new_data[CONF_REFRESH_TOKEN] = new_refresh_token

            new_data[CONF_BO_TOKEN] = new_bo_token

            hass.config_entries.async_update_entry(config_entry, data=new_data)



        session = async_get_clientsession(hass)

        api = HuarunGasApi(refresh_token, bo_token, wx_code, on_token_refresh, session=session)



    # 获取配置的扫描间隔，默认1小时

    # 优先读取 options（用户通过选项流程修改），其次 data（初始配置）

    scan_interval_val = config_entry.options.get(CONF_SCAN_INTERVAL, config_entry.data.get(CONF_SCAN_INTERVAL, 1))

    scan_interval_unit = config_entry.options.get(CONF_SCAN_INTERVAL_UNIT, config_entry.data.get(CONF_SCAN_INTERVAL_UNIT, "hour"))



    # 计算实际刷新间隔（hour=小时数，day=固定1小时仅支持定时，week=固定1小时，month=固定1小时）

    # day/week/month 模式下实际刷新由 coordinator 内部逻辑决定，这里统一设为1小时兜底

    if scan_interval_unit == "hour":

        scan_interval = timedelta(hours=scan_interval_val)

    elif scan_interval_unit == "day":

        # 每天模式：每 N 天抓一次（按天级周期轮询，至少1天）

        scan_interval = timedelta(days=max(1, int(scan_interval_val)))

    elif scan_interval_unit == "week":

        # 每周模式：每 N 周抓一次

        scan_interval = timedelta(weeks=max(1, int(scan_interval_val)))

    elif scan_interval_unit == "month":

        # 每月模式：按 30 天近似为一个月周期

        scan_interval = timedelta(days=30 * max(1, int(scan_interval_val)))

    else:

        scan_interval = timedelta(hours=1)

    _LOGGER.info(f"数据刷新间隔: {scan_interval_val} {scan_interval_unit}（实际: {scan_interval}）")



    # ========== 2. 数据更新协调器 ==========

    async def async_update_data():

        """

        更新数据 - 各接口独立容错，任一失败不影响其他。

        """

        result = {

            "arrears": 0,

            "account_balance": 0,

            "last_pay_time": "未知",

            "last_pay_amount": 0,

            "annual_pay_count": 0,

            "this_read": 0,

            "this_read_time": "未知",

            "step1_gas_used": 0,

            "step2_gas_used": 0,

            "this_gas_used": 0,

            "bill_amount": 0,

            "step1_remain": 0,

            "step2_remain": 0,
            "step3_remain": 0,

            "penalty_amount": 0,
            "last_read": 0,
            "last_read_time": "未知",
            "settle_flag": "未知",

            "cons_addr": "未知",

            "org_name": "未知",

            "gas_nature": "未知",

            "purchase_style": "未知",

            "last_month_gas": 0,

            "year_avg_gas": 0,

            "integration_status": "unknown",

            "monthly_gas_used": 0,

            "cons_name": "未知",
            "mobile": "未知",
            "meter_no": "未知",
            "area_name": "未知",
            "account_status": "未知",
            "revbl_amount": 0.0,
            "penalty_date": "未知",
            "last_fetch_time": "未知",
            "bill_month": "未知",

        }

        # Missing billing data is unknown, never a synthetic zero or default tariff.
        result.update({key: None for key in _NUMERIC_FIELDS})
        result.update({key: None for key in BILL_ATTRIBUTES})
        api_errors = []
        bills = []
        history = None
        try:
            history = await async_setup_history_storage(hass, config_entry.entry_id)
        except Exception as exc:
            api_errors.append("history_storage")
            _LOGGER.warning("History load failed: error_type=%s", type(exc).__name__)

        session_timeout_count = 0

        def failed(endpoint, exc):
            nonlocal session_timeout_count
            api_errors.append(endpoint)
            if isinstance(exc, SessionTimeoutError):
                session_timeout_count += 1
            _LOGGER.warning("Data request failed: endpoint=%s, error_type=%s", endpoint, type(exc).__name__)



        # 检查token是否即将过期

        if api.is_token_expiring_soon(threshold_seconds=int(TOKEN_EXPIRE_THRESHOLD.total_seconds())):

            _LOGGER.warning(f"Token即将过期，强制刷新...")

            try:

                if not await api.async_refresh_token():
                    raise ValueError("Token refresh returned no credentials")

            except Exception as e:

                failed("token_refresh", e)



        # 1. Balances are snapshots: missing and failed values stay unknown.
        try:
            response = await api.async_query_arrears(cons_no)
            if not bill_response_ok(response) or not isinstance(response.get("dataResult"), dict):
                raise ValueError("Unsuccessful balance response")
            data = response["dataResult"]
            result["arrears"] = _number(data.get("totalAmt"))
            result["account_balance"] = _number(data.get("totalBal"))
            if result["arrears"] is None or result["account_balance"] is None:
                raise ValueError("Missing balance fields")
        except Exception as exc:
            failed("arrears", exc)

        # 2. Count only after all pages have succeeded. Keep a known latest payment
        # if a later page fails, but never report a partial annual count.
        try:
            current_year = dt_util.now().year
            seen_pages = set()
            seen_payments = set()
            count = 0
            latest_time = ""
            for page in range(1, 101):
                response = await api.async_query_pay_history(cons_no, page=page, page_num=12)
                if not bill_response_ok(response) or not isinstance(response.get("dataResult"), list):
                    raise ValueError("Unsuccessful payment history response")
                payments = response["dataResult"]
                fingerprint = json.dumps(payments, sort_keys=True)
                if payments and fingerprint in seen_pages:
                    raise ValueError("Repeated payment history page")
                seen_pages.add(fingerprint)
                for payment in payments:
                    if not isinstance(payment, dict) or not (stamp := parse_payment_time(payment.get("payTime"))):
                        raise ValueError("Invalid payment date")
                    identity = payment.get("id") or payment.get("payNo")
                    if identity is not None:
                        if identity in seen_payments:
                            continue
                        seen_payments.add(identity)
                    if stamp.tzinfo is not None:
                        stamp = dt_util.as_local(stamp).replace(tzinfo=None)
                    time_key = stamp.isoformat()
                    if time_key > latest_time:
                        latest_time = time_key
                        result["last_pay_time"] = payment["payTime"]
                        result["last_pay_amount"] = _number(payment.get("payAmount"))
                    count += stamp.year == current_year
                if len(payments) < 12:
                    result["annual_pay_count"] = count
                    break
            else:
                raise ValueError("Payment pagination limit reached")
        except Exception as exc:
            failed("pay_history", exc)

        # 3. 获取账单列表（获取抄表日期和阶梯剩余气量）

        try:

            bill_data = await api.async_get_gas_bill_list(cons_no, page=1, page_num=6)

            if bill_response_ok(bill_data):
                data_result = bill_data.get("dataResult")
                if not isinstance(data_result, dict) or not isinstance(data_result.get("data"), list):
                    raise ValueError("Unexpected bill list shape")
                bills = data_result["data"]
                _LOGGER.debug(
                    "getGasBillList parsed: bills=%d, price_info_type=%s",
                    len(bills), type(data_result.get("consPrcInfo")).__name__,
                )
                result.update(parse_price_info(data_result.get("consPrcInfo")))
                if bills:

                    last_bill = bills[0]
                    if not isinstance(last_bill, dict):
                        raise ValueError("Unexpected bill entry shape")
                    result.update(parse_bill_summary(last_bill))

            else:
                raise ValueError("Unsuccessful bill list response")

        except Exception as e:

            failed("bill_list", e)

        # 4. Posting-month chart values are not interchangeable with billing periods.
        try:
            response = await api.async_get_gas_bill_list4chart(cons_no)
            if not bill_response_ok(response) or not isinstance(response.get("dataResult"), dict):
                raise ValueError("Unsuccessful chart response")
            result.update(chart_values(response["dataResult"], dt_util.now()))
        except Exception as exc:
            failed("bill_chart", exc)

        # 5. 获取账单详情；列表金额/用量已就绪，详情失败也不清零。
        try:
            bill_ym = result.get("_last_bill_ym")
            app_no = result.get("_last_app_no")
            if bill_ym and app_no:
                detail_data = await api.async_get_bill_detail(cons_no, bill_ym, app_no)
                if not bill_response_ok(detail_data):
                    raise ValueError("Unsuccessful bill detail response")
                details = detail_data.get("dataResult")
                if not isinstance(details, list):
                    raise ValueError("Unexpected bill detail shape")
                _LOGGER.debug("getBillDetail response: detail_count=%d", len(details))
                if details:
                    if not isinstance(details[0], dict):
                        raise ValueError("Unexpected bill detail entry shape")
                    parse_bill_detail(details[0], result)
            else:
                _LOGGER.debug(
                    "getBillDetail skipped: bill_month_present=%s, application_present=%s",
                    bool(bill_ym), bool(app_no),
                )
        except Exception as e:
            failed("bill_detail", e)

        # 6. Match the configured account; do not borrow another binding's data.
        try:
            response = await api.async_get_binding_cons()
            if not bill_response_ok(response) or not isinstance(response.get("dataResult"), list):
                raise ValueError("Unsuccessful binding response")
            bindings = response["dataResult"]
            matching = [c for c in bindings if isinstance(c, dict) and c.get("consNo") == cons_no]
            if not matching and len(bindings) == 1 and isinstance(bindings[0], dict) and not bindings[0].get("consNo"):
                matching = bindings
            if not matching:
                raise ValueError("Configured binding unavailable")
            cons_info = matching[0]
            for source, target in (
                ("consAddr", "cons_addr"), ("orgName", "org_name"),
                ("gasNature", "gas_nature"), ("purchaseGasStyle", "purchase_style"),
                ("consName", "cons_name"), ("mobile", "mobile"),
            ):
                result[target] = cons_info.get(source) or "未知"
        except Exception as exc:
            failed("binding", exc)

        finalize_bill(result)
        # Merge complete bill identities, never replace history with a response page.
        try:
            if history is not None:
                if bills:
                    records = [dict(bill) for bill in bills if isinstance(bill, dict)]
                    if records and result.get("bill_amount") is not None:
                        records[0]["billAmt"] = result["bill_amount"]
                    await history.async_merge_bill_records(records)
                stored = history.get_bill_history(limit=None)
                result["last_month_gas"], result["previous_bill_period"] = previous_usage(stored, result.get("_last_bill_ym"))
                result["history_complete"] = history._data.get("last_fetch_complete")
                result["total_gas_cost"] = history.get_issued_cost()
                if stored and result["total_gas_cost"] is None and result["history_complete"] is not None:
                    raise ValueError("Issued bill total is incomplete or decreased")
        except Exception as exc:
            result["total_gas_cost"] = None
            failed("history_storage", exc)

        result["integration_status"] = (
            "token_expired" if session_timeout_count else "api_error" if api_errors else "normal"
        )
        result["failed_endpoints"] = sorted(set(api_errors))

        _LOGGER.debug(
            "数据更新完成: status=%s, bill_amount_available=%s, reading_available=%s",
            result["integration_status"], result["bill_amount"] is not None,
            result["this_read"] is not None,
        )

        result["last_fetch_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return result



    coordinator = DataUpdateCoordinator(

        hass,

        _LOGGER,

        name=DOMAIN,

        update_method=async_update_data,
        config_entry=config_entry,

        update_interval=scan_interval,

    )



    # 立即存储 coordinator 和 api（供 button.py 使用）

    # 必须在创建传感器之前存储，这样即使平台并行加载，按钮也能获取到

    hass.data[DOMAIN][f"{config_entry.entry_id}_coordinator"] = coordinator

    hass.data[DOMAIN][f"{config_entry.entry_id}_api"] = api

    hass.data[DOMAIN][f"{config_entry.entry_id}_cons_no"] = cons_no



    # 创建传感器

    entities = [

        HuarunGasSensor(coordinator, sensor_type)

        for sensor_type in SENSOR_TYPES

    ]

    async_add_entities(entities)



    async def async_initial_setup():
        """Refresh and recover local bills; never modify Recorder on startup."""
        try:
            await coordinator.async_config_entry_first_refresh()
            storage = await async_setup_history_storage(hass, config_entry.entry_id)
            if not storage._data.get("last_fetch_complete"):
                fetched = await storage.async_fetch_all_bills(api, cons_no)
                data = dict(coordinator.data or {})
                data["total_gas_cost"] = storage.get_issued_cost()
                data["history_complete"] = fetched["complete"]
                if fetched["complete"] and data["total_gas_cost"] is not None:
                    remaining = set(data.get("failed_endpoints", [])) - {"history_storage", "history_fetch", "history_startup"}
                    data["failed_endpoints"] = sorted(remaining)
                    if data.get("integration_status") != "token_expired":
                        data["integration_status"] = "api_error" if remaining else "normal"
                if not fetched["complete"] and data.get("integration_status") != "token_expired":
                    data["integration_status"] = "api_error"
                    data["failed_endpoints"] = sorted(set(data.get("failed_endpoints", [])) | {"history_fetch"})
                coordinator.async_set_updated_data(data)
        except Exception as exc:
            _LOGGER.warning("Initial local history refresh failed: error_type=%s", type(exc).__name__)
            data = dict(coordinator.data or {})
            if data.get("integration_status") != "token_expired":
                data["integration_status"] = "api_error"
            data["total_gas_cost"] = None
            data["failed_endpoints"] = sorted(set(data.get("failed_endpoints", [])) | {"history_startup"})
            coordinator.async_set_updated_data(data)

    hass.async_create_task(async_initial_setup())

    async def async_fetch_history_service(call):
        """Fetch local history only; no external or entity statistics are injected."""
        storage = await async_setup_history_storage(hass, config_entry.entry_id)
        fetched = await storage.async_fetch_all_bills(api, cons_no)
        await coordinator.async_request_refresh()
        if not fetched["complete"]:
            raise RuntimeError("History fetch incomplete; saved records retained")
        return {"success": fetched["complete"], "result": fetched, "statistics_imported": False}

    hass.data[DOMAIN][f"{config_entry.entry_id}_fetch_history"] = async_fetch_history_service

    async def dispatch_history_service(call):
        entry_id = call.data.get("config_entry_id")
        handlers = {
            key.removesuffix("_fetch_history"): value
            for key, value in hass.data[DOMAIN].items() if key.endswith("_fetch_history")
        }
        if entry_id is None:
            if len(handlers) != 1:
                raise ValueError("Specify config_entry_id")
            entry_id = next(iter(handlers))
        if entry_id not in handlers:
            raise ValueError("Unknown config_entry_id")
        return await handlers[entry_id](call)

    hass.services.async_register(DOMAIN, "fetch_history", dispatch_history_service)
