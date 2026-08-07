"""WiFi credentials template.

Copy this to `secrets.py` and fill in your network. `secrets.py` is
gitignored, so your credentials never reach a commit.

    cp secrets.example.py secrets.py

Then copy it to the board alongside the smoke test:

    mpremote connect auto fs cp secrets.py :secrets.py
"""

WIFI_SSID = "your-network-name"
WIFI_PASSWORD = "your-network-password"

# Optional. Sets the DHCP hostname the board announces on your LAN, which
# makes it easier to find in your router's client list. Leave as None to
# keep the MicroPython default.
HOSTNAME = "pico2w"
