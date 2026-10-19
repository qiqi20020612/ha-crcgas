"""Synthetic regressions for current-bill parsing; no captures or credentials.

Run: python -m unittest discover -s tests -v
The HA framework, API and history storage are replaced with in-memory doubles.
The real sensor setup, coordinator update callback and entity properties execute.
"""

import importlib
import importlib.util
import logging
from copy import deepcopy
from datetime import datetime, timezone
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "crcgas"


def success(data):
    return {"success": True, "statusCode": "200", "dataResult": data}


def bill_list(price_info=None, **changes):
    bill = {
        "billYm": "2024-04", "applicationNo": "synthetic-application",
        "id": "synthetic-bill", "billAmt": "33.20", "gasAmt": 8,
        "revblAmt": "93.20", "penaltyAmt": "1.10", "settleFlag": "全部结清",
        "penaltyDate": "2024-06-01",
    }
    bill.update(changes)
    return success({"data": [bill], "consPrcInfo": price_info})


def single_detail(**changes):
    detail = {
        "gasUsed": 8, "lastRead": 200, "thisRead": 208,
        "lastReadTime": "2024-02-01", "thisReadTime": "2024-04-01",
        "gasStepList": [{"stepType": "单一气价", "gasUsed": 8, "stepAmt": 33.20}],
        "id": "synthetic-detail",
    }
    detail.update(changes)
    return success([detail])


def tier_prices(count=3):
    return {"consPrcDetailList": [
        {
            "ruleCode": f"020{tier}", "catPrc": str(price),
            "levGq": "1,000", "levGqRemain": "100", "levGqSum": "50",
        }
        for tier, price in enumerate((2.1, 2.7, 3.3)[:count], 1)
    ]}


def tier_detail():
    return single_detail(gasUsed=27, thisRead=227, gasStepList=[
        {"stepType": "第一阶梯", "gasUsed": "20", "stepAmt": "42.00"},
        {"stepType": "第二阶梯", "gasUsed": 5, "stepAmt": 13.50},
        {"stepType": "第三阶梯", "gasUsed": 2, "stepAmt": 6.60},
    ])


class SessionTimeoutError(Exception):
    """Test API's session failure."""


@contextmanager
def sensor_environment():
    """Import the real sensor with only its external framework boundaries mocked."""
    modules = {}

    def module(name, **attributes):
        item = types.ModuleType(name)
        item.__dict__.update(attributes)
        modules[name] = item
        return item

    class Coordinator:
        def __init__(self, hass, logger, **kwargs):
            self.config_entry = kwargs.get("config_entry")
            self.update_method = kwargs["update_method"]
            self.data = None
            self.last_update_success = True
            self.hass = hass

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        async def async_request_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

        def async_add_listener(self, callback):
            return lambda: None

    class Sensor:
        async def async_added_to_hass(self):
            pass

        async def async_get_last_sensor_data(self):
            return None

        def async_on_remove(self, callback):
            pass

        def async_write_ha_state(self):
            pass

    class Storage:
        def __init__(self, hass, entry_id):
            self.records = deepcopy([r for r in (hass.history_records or []) if isinstance(r, dict)])
            self._data = {"last_fetch_complete": True}

        async def async_load(self):
            pass

        def get_bill_history(self, limit=None):
            return self.records

        async def async_merge_bill_records(self, records):
            from custom_components.crcgas.data import bill_key
            indexed = {bill_key(b): b for b in self.records}
            for record in records:
                key = bill_key(record)
                if key in indexed:
                    indexed[key].update(record)
                else:
                    self.records.append(dict(record))

        def get_issued_cost(self):
            from custom_components.crcgas.data import issued_cost
            return issued_cost(self.records)

    async def history_storage(hass, entry_id):
        key = f"{entry_id}_history_storage"
        if key not in hass.data["crcgas"]:
            hass.data["crcgas"][key] = Storage(hass, entry_id)
        return hass.data["crcgas"][key]

    module("custom_components", __path__=[str(COMPONENT.parent)])
    module("custom_components.crcgas", __path__=[str(COMPONENT)])
    module("custom_components.crcgas.api", HuarunGasApi=object, SessionTimeoutError=SessionTimeoutError)
    module("custom_components.crcgas.history_storage", CRCGasHistoryStorage=Storage, async_setup_history_storage=history_storage)
    module("homeassistant")
    module("homeassistant.config_entries", ConfigEntry=object)
    module("homeassistant.core", HomeAssistant=object)
    module("homeassistant.const", Platform=types.SimpleNamespace(SENSOR="sensor", BUTTON="button"))
    module("homeassistant.util")
    module("homeassistant.util.dt", now=lambda: datetime(2026, 10, 19, tzinfo=timezone.utc), as_local=lambda value: value.astimezone(timezone.utc))
    module("homeassistant.helpers")
    module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=Coordinator)
    module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
    module("homeassistant.helpers.event", async_call_later=lambda *args: None)
    module("homeassistant.helpers.entity_registry")
    module("homeassistant.components")
    module("homeassistant.components.sensor", SensorEntity=Sensor, RestoreSensor=Sensor)
    module("homeassistant.components.sensor.const", SensorStateClass=types.SimpleNamespace(
        TOTAL_INCREASING="total_increasing", TOTAL="total", MEASUREMENT="measurement",
    ))
    # Avoid reusing sensor modules from a previous isolated test environment.
    with patch.dict(sys.modules, modules):
        for suffix in ("sensor", "billing", "const", "data"):
            sys.modules.pop(f"custom_components.crcgas.{suffix}", None)
        yield importlib.import_module("custom_components.crcgas.sensor")


async def setup_sensor(sensor, bills=None, details=None, chart=None, tasks=None):
    """Construct a real update callback without running startup history jobs."""
    bills = bill_list() if bills is None else bills
    details = single_detail() if details is None else details
    api = types.SimpleNamespace(
        is_token_expiring_soon=lambda **kwargs: False,
        async_query_arrears=AsyncMock(return_value=success({"totalAmt": 0, "totalBal": 0})),
        async_query_pay_history=AsyncMock(return_value=success([])),
        async_get_gas_bill_list=AsyncMock(return_value=bills),
        async_get_bill_detail=AsyncMock(return_value=details),
        async_get_gas_bill_list4chart=AsyncMock(return_value=chart or success({"thisGas": [None] * 12, "lastGas": [None] * 12})),
        async_get_binding_cons=AsyncMock(return_value=success([{"consNo": "synthetic-cons"}])),
    )
    entry = types.SimpleNamespace(entry_id="test-entry", data={"cons_no": "synthetic-cons"}, options={})
    records = bills.get("dataResult", {}).get("data", []) if isinstance(bills, dict) and isinstance(bills.get("dataResult"), dict) else []
    registered = {}
    hass = types.SimpleNamespace(
        data={"crcgas": {"test-entry_api": api}}, history_records=records,
        async_create_task=lambda coroutine: coroutine.close() if tasks is None else tasks.append(coroutine),
        services=types.SimpleNamespace(
            async_register=lambda domain, name, handler: registered.__setitem__(name, handler),
            registered=registered,
        ),
    )
    entities = []
    await sensor.async_setup_entry(hass, entry, entities.extend)
    coordinator = hass.data["crcgas"]["test-entry_coordinator"]
    return api, coordinator, {entity.sensor_type: entity for entity in entities}


class BillingRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = sensor_environment()
        self.sensor = self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        self.billing = sys.modules["custom_components.crcgas.billing"]

    async def refresh(self, bills=None, details=None, chart=None):
        api, coordinator, entities = await setup_sensor(self.sensor, bills, details, chart)
        coordinator.data = await coordinator.update_method()
        return coordinator.data, api, entities

    async def test_null_tariffs_do_not_block_single_price_bill_or_readings(self):
        data, api, entities = await self.refresh()
        api.async_get_bill_detail.assert_awaited_once_with("synthetic-cons", "2024-04", "synthetic-application")
        self.assertEqual(data["bill_amount"], 33.20)
        self.assertEqual(data["revbl_amount"], 93.20)
        self.assertEqual(data["this_gas_used"], 8)
        self.assertEqual(data["this_read"], 208)
        self.assertEqual(data["last_read"], 200)
        self.assertEqual(data["this_read_time"], "2024-04-01")
        self.assertEqual(data["last_read_time"], "2024-02-01")
        self.assertEqual(data["penalty_amount"], 1.10)
        self.assertEqual(data["pricing_mode"], "single")
        self.assertEqual(data["single_gas_amount"], 33.20)
        self.assertEqual(data["single_gas_used"], 8)
        self.assertEqual(data["average_gas_price"], 4.15)
        self.assertTrue(data["average_gas_price_source"].startswith("calculated:"))
        self.assertEqual(data["bill_amount_source"], "bill_list.billAmt")
        self.assertIsNone(data["single_gas_price"])
        self.assertIsNone(data["estimated_gas_bill_amount"])
        for tier in range(1, 4):
            self.assertIsNone(entities[f"gas_price_step{tier}"].native_value)
            self.assertIsNone(entities[f"step{tier}_gas_used"].native_value)
        self.assertEqual(entities["bill_amount"].extra_state_attributes["average_gas_price"], 4.15)
        self.assertEqual(data["integration_status"], "normal")

    async def test_optional_price_shapes_cannot_break_bill_parsing(self):
        for price in (None, {}, [], "synthetic", {"consPrcDetailList": None},
                      {"consPrcDetailList": [None, "bad", {"ruleCode": []}]}):
            with self.subTest(price=price):
                data, api, _ = await self.refresh(bill_list(price))
                self.assertEqual(data["bill_amount"], 33.20)
                self.assertEqual(data["this_read"], 208)
                api.async_get_bill_detail.assert_awaited_once()

    async def test_genuine_zero_bill_wins_over_receivable_and_detail(self):
        data, _, entities = await self.refresh(
            bill_list(billAmt="0.00", gasAmt=0), single_detail(gasUsed=0, totalAmount=99),
        )
        self.assertEqual(entities["bill_amount"].native_value, 0.0)
        self.assertEqual(data["this_gas_used"], 0.0)
        self.assertIsNone(data["average_gas_price"])

    async def test_zero_usage_and_meter_readings_are_not_truthy_fallbacks(self):
        data, _, entities = await self.refresh(details=single_detail(gasUsed=0, thisRead=0, lastRead=0))
        self.assertEqual(data["this_gas_used"], 0.0)
        self.assertEqual(entities["this_read"].native_value, 0.0)
        self.assertEqual(entities["last_read"].native_value, 0.0)
        self.assertIsNone(data["average_gas_price"])

    async def test_detail_totals_are_fallbacks_only_when_bill_amount_is_missing(self):
        for field in ("totalAmount", "billAmount"):
            for value in (0, "75.60"):
                with self.subTest(field=field, value=value):
                    data, _, _ = await self.refresh(bill_list(billAmt=None), single_detail(**{field: value}))
                    self.assertEqual(data["bill_amount"], float(value))
                    self.assertEqual(data["bill_amount_source"], f"bill_detail.{field}")

    async def test_sum_step_charges_when_no_total_is_provided(self):
        data, _, _ = await self.refresh(bill_list(billAmt=None), single_detail())
        self.assertEqual(data["bill_amount"], 33.20)
        self.assertEqual(data["bill_amount_source"], "bill_detail.sum(stepAmt)")
        data, _, _ = await self.refresh(bill_list(billAmt=None, gasAmt=27), tier_detail())
        self.assertEqual(data["bill_amount"], 62.10)

    async def test_incomplete_charges_are_unknown_not_partial_or_receivable_totals(self):
        detail = single_detail(gasStepList=[
            {"stepType": "单一气价", "gasUsed": 4, "stepAmt": 16.60},
            {"stepType": "单一气价", "gasUsed": 4, "stepAmt": None},
        ])
        data, _, entities = await self.refresh(bill_list(billAmt=None), detail)
        self.assertIsNone(entities["bill_amount"].native_value)
        self.assertIsNone(data["single_gas_amount"])
        self.assertIsNone(data["average_gas_price"])

    async def test_three_tier_usage_and_official_prices_are_preserved(self):
        data, _, _ = await self.refresh(bill_list(tier_prices(), billAmt="62.10", gasAmt=27), tier_detail())
        self.assertEqual(data["pricing_mode"], "tiered")
        self.assertEqual([data[f"step{i}_gas_used"] for i in range(1, 4)], [20, 5, 2])
        self.assertEqual([data[f"gas_price_step{i}"] for i in range(1, 4)], [2.1, 2.7, 3.3])
        self.assertEqual(data["step1_gas_limit"], 1000)
        self.assertEqual(data["step1_remain"], 100)
        self.assertEqual(data["estimated_gas_bill_amount"], 62.10)

    async def test_two_tiers_do_not_require_an_unused_third_price(self):
        detail = tier_detail()
        detail["dataResult"][0]["gasStepList"].pop()
        detail["dataResult"][0]["gasUsed"] = 25
        data, _, _ = await self.refresh(bill_list(tier_prices(2), gasAmt=25), detail)
        self.assertEqual(data["step3_gas_used"], 0)
        self.assertIsNone(data["gas_price_step3"])
        self.assertEqual(data["estimated_gas_bill_amount"], 55.50)

    async def test_missing_used_tier_price_does_not_produce_partial_estimate(self):
        data, _, entities = await self.refresh(bill_list(tier_prices(2), gasAmt=27), tier_detail())
        self.assertIsNone(data["gas_price_step3"])
        self.assertIsNone(entities["estimated_gas_bill_amount"].native_value)
        self.assertEqual(data["step3_gas_used"], 2)

    async def test_official_single_price_and_calculated_average_are_separate(self):
        detail = single_detail(gasStepList=[{
            "stepType": "单一气价", "gasUsed": 8, "stepAmt": 33.20, "catPrc": "4.15",
        }])
        data, _, _ = await self.refresh(bill_list(tier_prices(), billAmt="100"), detail)
        self.assertEqual(data["single_gas_price"], 4.15)
        self.assertEqual(data["gas_price_source"], {"single": "bill_detail.catPrc"})
        self.assertEqual(data["average_gas_price"], 12.5)
        self.assertEqual(data["estimated_gas_bill_amount"], 33.20)
        self.assertTrue(all(data[key] is None for key in self.billing.TIER_FIELDS))

    async def test_repeated_tier_lines_are_added_instead_of_overwritten(self):
        detail = single_detail(gasUsed=8, gasStepList=[
            {"stepType": "第一阶梯", "gasUsed": 3, "stepAmt": 6.30},
            {"stepType": "第一阶梯", "gasUsed": 5, "stepAmt": 10.50},
        ])
        data, _, _ = await self.refresh(bill_list(tier_prices()), detail)
        self.assertEqual(data["step1_gas_used"], 8)
        self.assertEqual(data["estimated_gas_bill_amount"], 16.80)

    async def test_empty_or_unknown_steps_never_invent_tier_usage(self):
        for steps in (None, [], [None], [{"stepType": "unknown", "gasUsed": 8, "stepAmt": 33.20}]):
            with self.subTest(steps=steps):
                data, _, _ = await self.refresh(bill_list(tier_prices()), single_detail(gasStepList=steps))
                self.assertIsNone(data["step1_gas_used"])
                self.assertIsNone(data["estimated_gas_bill_amount"])
                self.assertEqual(data["bill_amount"], 33.20)

    async def test_empty_details_keep_summary_amount_usage_and_penalty(self):
        data, _, entities = await self.refresh(details=success([]))
        self.assertEqual(data["bill_amount"], 33.20)
        self.assertEqual(data["this_gas_used"], 8)
        self.assertEqual(data["penalty_amount"], 1.10)
        self.assertIsNone(entities["this_read"].native_value)
        self.assertIsNone(entities["last_read"].native_value)

    async def test_bill_failures_are_unknown_and_do_not_request_detail(self):
        failures = (
            {}, {"success": False}, success(None), success([]),
            success({"data": None}), success({"data": [None]}),
            {"success": True, "statusCode": "503", "dataResult": {}},
        )
        for response in failures:
            with self.subTest(response=response):
                data, api, entities = await self.refresh(bills=response)
                self.assertEqual(data["integration_status"], "api_error")
                for key in ("bill_amount", "this_gas_used", "this_read", "gas_price_step1", "estimated_gas_bill_amount"):
                    self.assertIsNone(entities[key].native_value)
                api.async_get_bill_detail.assert_not_awaited()

    async def test_detail_failures_keep_valid_summary_and_unknown_readings(self):
        for response in ({"success": False}, success(None), success({}), success([None])):
            with self.subTest(response=response):
                data, _, entities = await self.refresh(details=response)
                self.assertEqual(data["integration_status"], "api_error")
                self.assertEqual(data["bill_amount"], 33.20)
                self.assertEqual(data["this_gas_used"], 8)
                self.assertIsNone(entities["this_read"].native_value)
                self.assertIsNone(entities["total_gas_consumption"].native_value)

    async def test_network_and_session_errors_keep_summary_without_logging_exception_text(self):
        for error in (RuntimeError("secret-token synthetic-cons"), SessionTimeoutError("secret-token")):
            with self.subTest(error=type(error).__name__):
                api, coordinator, _ = await setup_sensor(self.sensor)
                api.async_get_bill_detail.side_effect = error
                with self.assertLogs("custom_components.crcgas", logging.DEBUG) as logs:
                    data = await coordinator.update_method()
                self.assertEqual(data["bill_amount"], 33.20)
                self.assertIsNone(data["this_read"])
                self.assertEqual(data["integration_status"], "token_expired" if isinstance(error, SessionTimeoutError) else "api_error")
                output = "\n".join(logs.output)
                self.assertNotIn("secret-token", output)
                self.assertNotIn("synthetic-cons", output)

    async def test_bill_list_session_failure_is_unknown_and_marks_token_expired(self):
        api, coordinator, entities = await setup_sensor(self.sensor)
        api.async_get_gas_bill_list.side_effect = SessionTimeoutError("secret-token")
        with self.assertLogs("custom_components.crcgas", logging.WARNING) as logs:
            coordinator.data = await coordinator.update_method()
        self.assertEqual(coordinator.data["integration_status"], "token_expired")
        self.assertIsNone(entities["bill_amount"].native_value)
        api.async_get_bill_detail.assert_not_awaited()
        self.assertNotIn("secret-token", "\n".join(logs.output))

    async def test_failed_refresh_does_not_zero_or_reuse_another_bills_prices(self):
        api, coordinator, entities = await setup_sensor(self.sensor)
        coordinator.data = await coordinator.update_method()
        self.assertEqual(entities["total_gas_consumption"].native_value, 208)
        api.async_get_gas_bill_list.side_effect = RuntimeError("synthetic-cons secret-token")
        with self.assertLogs("custom_components.crcgas", logging.WARNING):
            coordinator.data = await coordinator.update_method()
        self.assertIsNone(entities["bill_amount"].native_value)
        self.assertIsNone(entities["gas_price_step1"].native_value)
        self.assertIsNone(entities["this_read"].native_value)
        self.assertEqual(entities["total_gas_consumption"].native_value, 208)

    async def test_diagnostics_log_shapes_and_sources_without_private_fields(self):
        with self.assertLogs("custom_components.crcgas", logging.DEBUG) as logs:
            await self.refresh()
        output = "\n".join(logs.output)
        self.assertIn("price_info_type=NoneType", output)
        self.assertIn("mode=single", output)
        self.assertIn("amount_source=bill_list.billAmt", output)
        for value in ("synthetic-cons", "synthetic-application", "synthetic-bill", "synthetic-detail", "33.2", "208", "2024-04"):
            self.assertNotIn(value, output)

    async def test_missing_detail_identifiers_are_not_logged(self):
        with self.assertLogs("custom_components.crcgas", logging.DEBUG) as logs:
            data, api, _ = await self.refresh(bill_list(applicationNo=None))
        self.assertEqual(data["bill_amount"], 33.20)
        api.async_get_bill_detail.assert_not_awaited()
        self.assertNotIn("2024-04", "\n".join(logs.output))

    async def test_chart_nulls_and_real_zeroes_remain_supported(self):
        chart = success({"lastGas": [None, 0, 80] + [None] * 9, "thisGas": [8] + [None] * 11})
        data, api, entities = await self.refresh(chart=chart)
        api.async_get_gas_bill_list4chart.assert_awaited_once_with("synthetic-cons")
        self.assertIsNone(data["year_avg_gas"])
        self.assertIsNone(entities["last_month_gas"].native_value)
        self.assertIsNone(data["monthly_gas_used"])
        self.assertEqual(data["usage_period_type"], "billing_month")

    async def test_usage_can_fall_back_to_complete_step_lines(self):
        data, _, _ = await self.refresh(bill_list(gasAmt=None), single_detail(gasUsed=None))
        self.assertEqual(data["this_gas_used"], 8)

    async def test_http_client_diagnostics_do_not_log_response_bodies_or_exception_values(self):
        spec = importlib.util.spec_from_file_location("custom_components.crcgas.api_diagnostics", COMPONENT / "api.py")
        api_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"aiohttp": types.SimpleNamespace(ClientTimeout=lambda **kwargs: None)}):
            spec.loader.exec_module(api_module)
        private = "synthetic-cons secret-token private-response"
        response = types.SimpleNamespace(
            status=500, text=AsyncMock(return_value=private),
        )
        # Special methods are resolved on a type, so use a small real context manager.
        class RequestContext:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *args):
                return False

        session = types.SimpleNamespace(request=Mock(return_value=RequestContext()))
        client = api_module.HuarunGasApi("synthetic-refresh", "synthetic-bo", "synthetic-wx", session=session)
        for error in (None, RuntimeError(private), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                session.request.side_effect = error
                with self.assertLogs(api_module._LOGGER, logging.DEBUG) as logs:
                    with self.assertRaises(Exception):
                        await client.async_get_gas_bill_list("synthetic-cons")
                for value in (private, "synthetic-cons", "secret-token", "private-response", "synthetic-bo"):
                    self.assertNotIn(value, "\n".join(logs.output))

    def test_numeric_parser_preserves_zero_and_rejects_invalid_values(self):
        for value in (None, "", "bad", "NaN", "Infinity", float("nan"), True, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(self.billing._number(value))
        self.assertEqual(self.billing._number("0.00"), 0)
        self.assertEqual(self.billing._number("1,234.50"), 1234.50)


if __name__ == "__main__":
    unittest.main()
