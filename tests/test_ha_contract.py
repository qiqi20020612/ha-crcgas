"""Run against the real HA package in CI, without starting HA or a database."""

import importlib.util
import inspect
import tempfile
import types
import unittest


@unittest.skipUnless(importlib.util.find_spec("homeassistant"), "HA compatibility job installs the real package")
class HomeAssistantContractTests(unittest.IsolatedAsyncioTestCase):
    def test_actual_device_classes_state_classes_and_currency_units(self):
        from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass
        from homeassistant.components.sensor.const import DEVICE_CLASS_STATE_CLASSES
        from custom_components.crcgas.const import SENSOR_TYPES
        from custom_components.crcgas.sensor import HuarunGasSensor

        coordinator = types.SimpleNamespace(
            data=None, last_update_success=True, config_entry=types.SimpleNamespace(entry_id="synthetic-entry"),
        )
        self.assertTrue(issubclass(HuarunGasSensor, RestoreSensor))
        for key in SENSOR_TYPES:
            entity = HuarunGasSensor(coordinator, key)
            self.assertEqual(entity.unique_id, f"crcgas_{key}")
            self.assertIsNone(entity.native_value)
            device_class, state_class = entity.device_class, entity.state_class
            if device_class and state_class:
                self.assertIn(state_class, DEVICE_CLASS_STATE_CLASSES[SensorDeviceClass(device_class)])
            if device_class == SensorDeviceClass.MONETARY:
                self.assertEqual(entity.native_unit_of_measurement, "CNY")
            if key.startswith("gas_price_"):
                self.assertIsNone(device_class)
                self.assertEqual(entity.native_unit_of_measurement, "CNY/m³")

    def test_store_save_is_awaitable_in_supported_ha(self):
        from homeassistant.helpers.storage import Store
        self.assertTrue(inspect.iscoroutinefunction(Store.async_save))

    async def test_real_store_round_trip_uses_the_existing_storage_format(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers.storage import Store
        with tempfile.TemporaryDirectory() as path:
            hass = HomeAssistant(path)
            store = Store(hass, 1, "crcgas_history_contract")
            records = {"bill_history": [{"applicationNo": "synthetic", "billAmt": 0}]}
            await store.async_save(records)
            self.assertEqual(await Store(hass, 1, "crcgas_history_contract").async_load(), records)


if __name__ == "__main__":
    unittest.main()
