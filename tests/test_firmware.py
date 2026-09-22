"""Compile each firmware (ESP32, Nano) for the host with stub Arduino headers and run its test scenarios.

Protocol checks plus closed-loop balance/gait/fall scenarios against a simulated biped with a
simulated MPU6050 (see tests/firmware/harness.cpp).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent / "firmware"
FIRMWARE_DIR = Path(__file__).resolve().parents[1] / "firmware"
SKETCHES = {
    "esp32": FIRMWARE_DIR / "emo_esp32" / "emo_esp32.ino",
    "nano": FIRMWARE_DIR / "emo_nano" / "emo_nano.ino",
}
SCENARIOS = ["protocol", "imu_sign", "slope", "walking", "watchdog", "fall", "wrong_sign", "calibration",
             "no_imu", "imu_fail", "telemetry"]

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")


@pytest.fixture(scope="module", params=sorted(SKETCHES))
def harness(request, tmp_path_factory):
    exe = tmp_path_factory.mktemp(f"fw_{request.param}") / "fw_harness"
    sketch = f'-DFIRMWARE_SKETCH="{SKETCHES[request.param].as_posix()}"'
    subprocess.run(["g++", "-std=c++11", "-O1", "-Wall", "-Wextra", sketch, "-I", str(HERE),
                    str(HERE / "harness.cpp"), "-o", str(exe)], check=True)
    listed = subprocess.run([str(exe), "list"], capture_output=True, text=True, check=True).stdout.split()
    assert listed == SCENARIOS  # keep this list in sync with harness.cpp
    return exe


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_firmware_scenario(harness, scenario):
    result = subprocess.run([str(harness), scenario], capture_output=True, text=True, timeout=120)
    print(result.stdout)
    assert result.returncode == 0, result.stdout
