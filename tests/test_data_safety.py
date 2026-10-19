"""Regression contracts for persistence, unknown values and no Recorder writes."""

import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_billing import COMPONENT, bill_list, setup_sensor, single_detail, success, sensor_environment


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def historical_bill(index, amount=10, period="2024-01"):
    return {"applicationNo": f"synthetic-{index}", "billYm": period, "billAmt": amount, "gasAmt": 2}


class MemoryStore:
    """Awaitable Store boundary with simulated durable data and write failures."""
    def __init__(self, hass, version, key):
        self.hass, self.version, self.key = hass, version, key

    async def async_load(self):
        return deepcopy(self.hass.persisted.get(self.key))

    async def async_save(self, data):
        await asyncio.sleep(0)
        if self.hass.fail_save:
            raise OSError("synthetic write failure")
        self.hass.persisted[self.key] = deepcopy(data)


class HistorySafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = sensor_environment()
        self.sensor = self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        storage_api = types.ModuleType("homeassistant.helpers.storage")
        storage_api.Store = MemoryStore
        self.patch = patch.dict(sys.modules, {"homeassistant.helpers.storage": storage_api})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.history = load_module("custom_components.crcgas.history_under_test", COMPONENT / "history_storage.py")
        self.hass = types.SimpleNamespace(data={}, persisted={}, fail_save=False)

    async def store(self, records=None):
        if records is not None or "crcgas_history_test" not in self.hass.persisted:
            self.hass.persisted["crcgas_history_test"] = {"bill_history": records or [], "last_fetch_complete": True}
        return await self.history.async_setup_history_storage(self.hass, "test")

    async def test_missing_and_old_timestamps_do_not_expire_bills(self):
        records = [historical_bill(1), {**historical_bill(2), "timestamp": "2000-01-01T00:00:00"}]
        store = await self.store(records)
        original = historical_bill(3)
        await store.async_add_bill_record(original)
        await store.async_add_usage_record({"gas_used": 1})
        self.assertEqual(len(store.get_bill_history(None)), 3)
        self.assertEqual(store.get_issued_cost(), 30)
        self.assertNotIn("timestamp", original)

    async def test_shared_store_serializes_refresh_and_button_updates(self):
        first, second = await asyncio.gather(self.store(), self.store())
        self.assertIs(first, second)
        await asyncio.gather(
            first.async_add_bill_record(historical_bill(1)),
            second.async_add_bill_record(historical_bill(2)),
        )
        restarted = types.SimpleNamespace(data={}, persisted=self.hass.persisted, fail_save=False)
        restored = await self.history.async_setup_history_storage(restarted, "test")
        self.assertEqual(len(restored.get_bill_history(None)), 2)
        self.assertEqual(restored.get_issued_cost(), 20)

    async def test_repeated_bills_do_not_double_count_and_same_period_bills_remain_distinct(self):
        store = await self.store()
        await store.async_merge_bill_records([historical_bill(1), historical_bill(1), historical_bill(2, 20)])
        self.assertEqual(len(store.get_bill_history(None)), 2)
        self.assertEqual(store.get_issued_cost(), 30)
        await store.async_merge_bill_records([historical_bill(1)])
        self.assertEqual(store.get_issued_cost(), 30)

    async def test_missing_amount_does_not_erase_a_known_charge(self):
        store = await self.store([historical_bill(1, 25)])
        await store.async_add_bill_record(historical_bill(1, None))
        self.assertEqual(store.get_issued_cost(), 25)
        await store.async_add_bill_record(historical_bill(2, None))
        self.assertIsNone(store.get_issued_cost())

    async def test_real_zero_charge_is_preserved(self):
        store = await self.store()
        await store.async_add_bill_record(historical_bill(1, "0.00"))
        self.assertEqual(store.get_issued_cost(), 0)

    async def test_decreased_issued_total_is_unknown_and_baseline_survives_restart(self):
        store = await self.store([historical_bill(1, 25)])
        await store.async_add_bill_record(historical_bill(1, 20))
        self.assertIsNone(store.get_issued_cost())
        self.assertEqual(store.get_bill_history()[0]["billAmt"], 20)
        restarted = types.SimpleNamespace(data={}, persisted=self.hass.persisted, fail_save=False)
        restored = await self.history.async_setup_history_storage(restarted, "test")
        self.assertIsNone(restored.get_issued_cost())
        self.assertEqual(restored._data["issued_cost_baseline"], 25)

    async def test_save_failure_is_reported_and_does_not_leave_unpersisted_state(self):
        store = await self.store([historical_bill(1, 25)])
        self.hass.fail_save = True
        with self.assertRaises(OSError):
            await store.async_add_bill_record(historical_bill(2))
        self.assertEqual(len(store.get_bill_history(None)), 1)
        self.assertEqual(store.get_issued_cost(), 25)

    async def test_history_load_failure_is_not_replaced_with_an_empty_store(self):
        self.hass.persisted["crcgas_history_test"] = ["corrupt"]
        with self.assertRaises(ValueError):
            await self.store()
        self.assertEqual(self.hass.persisted["crcgas_history_test"], ["corrupt"])

    async def test_paginated_history_has_timestamps_and_retains_older_local_records(self):
        store = await self.store([historical_bill("old")])
        pages = [[historical_bill(i) for i in range(6)], [historical_bill(6), historical_bill(7)]]
        api = types.SimpleNamespace(async_get_gas_bill_list=AsyncMock(side_effect=[success({"data": p}) for p in pages]))
        result = await store.async_fetch_all_bills(api, "synthetic-cons")
        self.assertTrue(result["complete"])
        self.assertEqual(result["total_fetched"], 8)
        self.assertEqual(result["total_stored"], 9)
        self.assertTrue(all(b.get("timestamp") for b in store.get_bill_history(None) if b["applicationNo"] != "synthetic-old"))
        self.assertEqual(store.get_issued_cost(), 90)

    async def test_failed_or_repeated_history_page_is_incomplete_not_zero_or_full_success(self):
        for failure in (RuntimeError("private error"), success({"data": [historical_bill(i) for i in range(6)]})):
            with self.subTest(failure=type(failure).__name__):
                self.hass = types.SimpleNamespace(data={}, persisted={}, fail_save=False)
                store = await self.store([historical_bill("old")])
                page = success({"data": [historical_bill(i) for i in range(6)]})
                api = types.SimpleNamespace(async_get_gas_bill_list=AsyncMock(side_effect=[page, failure]))
                with self.assertLogs("custom_components.crcgas.history_under_test", logging.WARNING):
                    result = await store.async_fetch_all_bills(api, "synthetic-cons")
                self.assertFalse(result["complete"])
                self.assertEqual(len(store.get_bill_history(None)), 7)
                self.assertIsNone(store.get_issued_cost())

    async def test_empty_successful_fetch_does_not_delete_saved_history(self):
        store = await self.store([historical_bill(1)])
        api = types.SimpleNamespace(async_get_gas_bill_list=AsyncMock(return_value=success({"data": []})))
        result = await store.async_fetch_all_bills(api, "synthetic-cons")
        self.assertTrue(result["complete"])
        self.assertEqual(store.get_issued_cost(), 10)

    async def test_all_history_is_used_instead_of_a_999_record_limit(self):
        store = await self.store([historical_bill(i, 1) for i in range(1100)])
        self.assertEqual(len(store.get_bill_history(None)), 1100)
        self.assertEqual(store.get_issued_cost(), 1100)

    async def test_refresh_gap_is_unknown_until_full_history_is_fetched(self):
        store = await self.store([historical_bill("old")])
        page = [historical_bill(i) for i in range(6)]
        await store.async_merge_bill_records(page)
        self.assertIsNone(store.get_issued_cost())
        api = types.SimpleNamespace(async_get_gas_bill_list=AsyncMock(side_effect=[success({"data": page}), success({"data": []})]))
        self.assertTrue((await store.async_fetch_all_bills(api, "synthetic-cons"))["complete"])
        self.assertEqual(store.get_issued_cost(), 70)

    async def test_startup_and_history_service_use_real_local_storage_without_recorder(self):
        self.sensor.async_setup_history_storage = self.history.async_setup_history_storage
        tasks = []
        api, coordinator, _ = await setup_sensor(self.sensor, tasks=tasks)
        coordinator.hass.persisted = {}
        coordinator.hass.fail_save = False
        await tasks.pop()
        self.assertTrue(coordinator.data["history_complete"])
        self.assertEqual(coordinator.data["integration_status"], "normal")
        self.assertEqual(coordinator.data["total_gas_cost"], 33.20)
        handler = coordinator.hass.services.registered["fetch_history"]
        result = await handler(types.SimpleNamespace(data={"config_entry_id": "test-entry"}))
        self.assertTrue(result["success"])
        self.assertFalse(result["statistics_imported"])
        self.assertEqual(result["result"]["total_stored"], 1)
        self.assertEqual(coordinator.data["total_gas_cost"], 33.20)
        with self.assertRaises(ValueError):
            await handler(types.SimpleNamespace(data={"config_entry_id": "other-entry"}))

    async def test_incomplete_history_service_reports_failure_and_retains_records(self):
        self.sensor.async_setup_history_storage = self.history.async_setup_history_storage
        api, coordinator, _ = await setup_sensor(self.sensor)
        coordinator.hass.persisted = {}
        coordinator.hass.fail_save = False
        coordinator.data = await coordinator.update_method()
        api.async_get_gas_bill_list.side_effect = [{"success": False}, bill_list()]
        handler = coordinator.hass.services.registered["fetch_history"]
        with self.assertLogs("custom_components.crcgas", logging.WARNING):
            with self.assertRaises(RuntimeError):
                await handler(types.SimpleNamespace(data={}))
        stored = coordinator.hass.data["crcgas"]["test-entry_history_storage"]
        self.assertEqual(len(stored.get_bill_history(None)), 1)
        self.assertEqual(coordinator.data["integration_status"], "api_error")
        self.assertIsNone(coordinator.data["total_gas_cost"])

    async def test_coordinator_storage_failure_keeps_other_valid_data_and_marks_error(self):
        self.sensor.async_setup_history_storage = self.history.async_setup_history_storage
        _, coordinator, _ = await setup_sensor(self.sensor)
        coordinator.hass.persisted = {}
        coordinator.hass.fail_save = True
        with self.assertLogs("custom_components.crcgas", logging.WARNING):
            coordinator.data = await coordinator.update_method()
        self.assertEqual(coordinator.data["bill_amount"], 33.20)
        self.assertEqual(coordinator.data["this_read"], 208)
        self.assertIsNone(coordinator.data["total_gas_cost"])
        self.assertEqual(coordinator.data["integration_status"], "api_error")
        self.assertEqual(coordinator.hass.persisted, {})


class DataFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = sensor_environment()
        self.sensor = self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)

    async def test_issued_cost_does_not_add_the_same_bills_estimate(self):
        detail = single_detail(gasStepList=[{"stepType": "单一气价", "gasUsed": 8, "stepAmt": 33.20, "catPrc": 4.15}])
        _, coordinator, entities = await setup_sensor(self.sensor, details=detail)
        coordinator.data = await coordinator.update_method()
        self.assertEqual(coordinator.data["estimated_gas_bill_amount"], 33.20)
        self.assertEqual(entities["total_gas_cost"].native_value, 33.20)
        coordinator.data = await coordinator.update_method()
        self.assertEqual(entities["total_gas_cost"].native_value, 33.20)

    async def test_balance_failures_and_missing_fields_stay_unknown_and_mark_error(self):
        for response in ({"success": False}, success({}), success({"totalAmt": 0, "totalBal": None})):
            api, coordinator, entities = await setup_sensor(self.sensor)
            api.async_query_arrears.return_value = response
            with self.assertLogs("custom_components.crcgas", logging.WARNING):
                coordinator.data = await coordinator.update_method()
            self.assertIsNone(entities["account_balance"].native_value)
            self.assertEqual(coordinator.data["integration_status"], "api_error")
            self.assertEqual(coordinator.data["bill_amount"], 33.20)

    async def test_real_zero_balance_and_no_payments_are_valid_zeroes(self):
        _, coordinator, entities = await setup_sensor(self.sensor)
        coordinator.data = await coordinator.update_method()
        self.assertEqual(entities["account_balance"].native_value, 0)
        self.assertEqual(entities["arrears"].native_value, 0)
        self.assertEqual(entities["annual_pay_count"].native_value, 0)
        self.assertIsNone(entities["last_pay_amount"].native_value)

    async def test_each_partial_endpoint_failure_is_not_normal(self):
        for endpoint in ("async_query_pay_history", "async_get_gas_bill_list4chart", "async_get_binding_cons"):
            api, coordinator, _ = await setup_sensor(self.sensor)
            getattr(api, endpoint).side_effect = RuntimeError("secret-token")
            with self.assertLogs("custom_components.crcgas", logging.WARNING) as logs:
                coordinator.data = await coordinator.update_method()
            self.assertEqual(coordinator.data["integration_status"], "api_error")
            self.assertNotIn("secret-token", "\n".join(logs.output))

    async def test_annual_payment_count_reads_all_pages_and_deduplicates_ids(self):
        api, coordinator, _ = await setup_sensor(self.sensor)
        first = [{"id": str(i), "payTime": f"2026-01-{i+1:02d}", "payAmount": i} for i in range(12)]
        second = [first[-1], {"id": "13", "payTime": "2026-02-01", "payAmount": 13},
                  {"id": "old", "payTime": "2025-12-01", "payAmount": 100}]
        api.async_query_pay_history.side_effect = [success(first), success(second)]
        coordinator.data = await coordinator.update_method()
        self.assertEqual(coordinator.data["annual_pay_count"], 13)
        self.assertEqual(coordinator.data["last_pay_amount"], 13)
        self.assertEqual(api.async_query_pay_history.await_count, 2)

    async def test_incomplete_payment_count_is_unknown_but_known_last_payment_is_kept(self):
        api, coordinator, entities = await setup_sensor(self.sensor)
        first = [{"id": str(i), "payTime": f"2026-01-{i+1:02d}", "payAmount": i} for i in range(12)]
        api.async_query_pay_history.side_effect = [success(first), RuntimeError("failure")]
        with self.assertLogs("custom_components.crcgas", logging.WARNING):
            coordinator.data = await coordinator.update_method()
        self.assertIsNone(entities["annual_pay_count"].native_value)
        self.assertEqual(entities["last_pay_amount"].native_value, 11)
        self.assertEqual(coordinator.data["integration_status"], "api_error")

    async def test_repeated_payment_page_and_invalid_dates_do_not_produce_guessed_counts(self):
        for repeated in (True, False):
            api, coordinator, entities = await setup_sensor(self.sensor)
            page = [{"id": str(i), "payTime": f"2026-01-{i+1:02d}", "payAmount": i} for i in range(12)]
            api.async_query_pay_history.side_effect = [success(page), success(page)] if repeated else [success([{"payTime": "unknown"}])]
            with self.assertLogs("custom_components.crcgas", logging.WARNING):
                coordinator.data = await coordinator.update_method()
            self.assertIsNone(entities["annual_pay_count"].native_value)

    async def test_previous_usage_is_previous_billing_period_including_true_zero(self):
        response = bill_list()
        response["dataResult"]["data"].extend([
            historical_bill("previous", 0, "2024-02"), historical_bill("old", 10, "2023-12"),
        ])
        response["dataResult"]["data"][1]["gasAmt"] = 0
        _, coordinator, entities = await setup_sensor(self.sensor, bills=response)
        coordinator.data = await coordinator.update_method()
        self.assertEqual(entities["last_month_gas"].native_value, 0)
        self.assertEqual(coordinator.data["previous_bill_period"], "2024-02")

    async def test_chart_annual_mean_requires_complete_data_and_keeps_true_zero_month(self):
        chart = success({"lastGas": [0] + [12] * 11, "thisGas": [None] * 9 + [0, None, None]})
        _, coordinator, entities = await setup_sensor(self.sensor, chart=chart)
        coordinator.data = await coordinator.update_method()
        self.assertEqual(entities["monthly_gas_used"].native_value, 0)
        self.assertEqual(entities["year_avg_gas"].native_value, 11)
        self.assertEqual(coordinator.data["year_avg_year"], 2025)
        self.assertEqual(coordinator.data["this_gas_used"], 8)

    async def test_meter_total_failure_does_not_publish_a_zero_before_first_valid_read(self):
        _, coordinator, entities = await setup_sensor(self.sensor, details=success([]))
        coordinator.data = await coordinator.update_method()
        for key in ("total_gas_consumption", "gas_energy_dashboard"):
            self.assertIsNone(entities[key].native_value)
        coordinator.data["this_read"] = 208
        self.assertEqual(entities["total_gas_consumption"].native_value, 208)
        coordinator.data["this_read"] = None
        self.assertEqual(entities["total_gas_consumption"].native_value, 208)

    async def test_restore_ignores_legacy_zero_and_wrong_unit_but_keeps_valid_meter(self):
        _, coordinator, entities = await setup_sensor(self.sensor, details=success([]))
        coordinator.data = await coordinator.update_method()
        for value, unit, expected in ((0, "m³", None), (208, "ft³", None), (208, "m³", 208)):
            entity = entities["total_gas_consumption"]
            entity._last_good_value = None
            entity.async_get_last_sensor_data = AsyncMock(return_value=types.SimpleNamespace(native_value=value, native_unit_of_measurement=unit))
            await entity.async_added_to_hass()
            self.assertEqual(entity.native_value, expected)

    async def test_token_refresh_failure_is_not_logged_as_normal(self):
        api, coordinator, entities = await setup_sensor(self.sensor)
        api.is_token_expiring_soon = lambda **kwargs: True
        api.async_refresh_token = AsyncMock(return_value={})
        with self.assertLogs("custom_components.crcgas", logging.WARNING):
            coordinator.data = await coordinator.update_method()
        self.assertEqual(coordinator.data["integration_status"], "api_error")
        self.assertIn("token_refresh", entities["integration_status"].extra_state_attributes["failed_endpoints"])

    async def test_history_button_targets_its_original_config_entry(self):
        button_api = types.ModuleType("homeassistant.components.button")
        button_api.ButtonEntity = object
        entity_api = types.ModuleType("homeassistant.helpers.entity")
        entity_api.DeviceInfo = dict
        with patch.dict(sys.modules, {"homeassistant.components.button": button_api, "homeassistant.helpers.entity": entity_api}):
            buttons = load_module("custom_components.crcgas.buttons_under_test", COMPONENT / "button.py")
        hass = types.SimpleNamespace(services=types.SimpleNamespace(async_call=AsyncMock()))
        entry = types.SimpleNamespace(entry_id="test-entry")
        button = buttons.FetchHistoryButton(hass, entry)
        await button.async_press()
        self.assertEqual(button._attr_unique_id, "test-entry_fetch_history")
        hass.services.async_call.assert_awaited_once_with(
            "crcgas", "fetch_history", {"config_entry_id": "test-entry"}, blocking=True,
        )

    async def test_entity_id_contract_and_statistics_attributes(self):
        _, _, entities = await setup_sensor(self.sensor)
        self.assertEqual(entities["total_gas_cost"]._attr_unique_id, "crcgas_total_gas_cost")
        self.assertEqual(entities["total_gas_consumption"]._attr_unique_id, "crcgas_total_gas_consumption")
        self.assertEqual(entities["total_gas_cost"]._attr_device_class, "monetary")
        self.assertEqual(entities["total_gas_cost"]._attr_state_class, "total")
        self.assertEqual(entities["total_gas_cost"]._attr_native_unit_of_measurement, "CNY")
        for key in ("bill_amount", "account_balance", "last_month_gas", "annual_pay_count", "year_avg_gas", "gas_price_step1"):
            self.assertIsNone(entities[key]._attr_state_class)
        self.assertIsNone(entities["gas_price_step1"]._attr_device_class)

    def test_active_code_has_no_recorder_writer_or_registration_migration(self):
        for name in ("sensor.py", "history_storage.py", "button.py", "__init__.py"):
            text = (COMPONENT / name).read_text()
            tree = ast.parse(text)
            imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            self.assertFalse(any("recorder" in (module or "") for module in imported), name)
            for forbidden in ("sqlite3", "DELETE FROM statistics", "UPDATE statistics", "async_import_statistics", "async_update_entity"):
                self.assertNotIn(forbidden, text, name)


class TokenLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_success_and_failures_do_not_log_credentials(self):
        with sensor_environment():
            with patch.dict(sys.modules, {"aiohttp": types.SimpleNamespace(ClientTimeout=lambda **kwargs: None)}):
                api_module = load_module("custom_components.crcgas.token_api_test", COMPONENT / "api.py")
            secret = "synthetic-secret-token"
            payloads = [
                success({"refresh-token": secret, "bo-token": secret, "wxCode": secret}),
                {"success": False, "msg": secret},
                {"success": True, "msg": "会话超时 " + secret, "statusCode": "SESSION_TIMEOUT"},
                success(secret), success({}),
            ]
            for payload in payloads:
                class Response:
                    status = 200

                    async def json(self):
                        return payload

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        return False

                session = types.SimpleNamespace(get=Mock(return_value=Response()))
                client = api_module.HuarunGasApi(secret, secret, secret, session=session)
                with self.assertLogs(api_module._LOGGER, logging.DEBUG) as logs:
                    try:
                        await client.async_refresh_token()
                    except Exception:
                        pass
                self.assertNotIn(secret, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
