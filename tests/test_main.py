"""Smoke test: the whole runtime starts and drives (simulated) servos without any hardware."""
import asyncio
import logging

import config
import main


def test_main_runs_in_sim_mode(monkeypatch, caplog):
    monkeypatch.setattr(config, "SERIAL_PORT", "sim")
    monkeypatch.setattr(config, "ENABLE_VISION", False)
    monkeypatch.setattr(config, "ENABLE_AUDIO", False)
    monkeypatch.setattr(config, "MQTT_PORT", 1)  # no broker: paho retries quietly in the background

    async def run():
        task = asyncio.create_task(main.main())
        await asyncio.sleep(1.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.INFO):
        asyncio.run(run())

    assert "EMO-Bot running" in caplog.text
    assert "SIM serial -> S" in caplog.text  # the legs were told to stand and balance
    assert "crashed" not in caplog.text
