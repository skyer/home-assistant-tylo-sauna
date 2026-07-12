"""Tylo Sauna integration constants."""

DOMAIN = "tylo_sauna"

# Config/option keys (entry.data / entry.options)
CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"
CONF_GUID = "guid"
CONF_PIN = "pin"
CONF_RELAXED_TELEMETRY = "relaxed_telemetry"

# Sauna's connection PIN, set on its own control panel. "0000" is the factory default.
DEFAULT_PIN = "0000"

# Экспериментальные функции (по умолчанию выключены).
# Включать только для отладки конкретных установок (например, steam + aroma).
CONF_EXPERIMENTAL_AROMA = "experimental_aroma"
CONF_DEBUG_RECORDING = "debug_recording"

# Legacy / fallback control port candidate.
# Important: the controller's effective control/telemetry UDP port is dynamic and may change after reboot.
# We keep a historical/observed port here only as a last-resort probe candidate.
DEFAULT_CONTROL_PORT = 42156
UDP_DISCOVERY_PORTS = (54377, 54378)

# Timing (observed official app behavior)
KEEPALIVE_INTERVAL = 15  # seconds
ONLINE_TIMEOUT_S = 300   # consider online if a packet was received within the last N seconds


